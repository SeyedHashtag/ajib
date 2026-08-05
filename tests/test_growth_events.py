import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core/scripts/telegrambot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")
database = importlib.import_module("utils.database")
growth_events = importlib.import_module("utils.growth_events")
growth_features = importlib.import_module("utils.growth_features")


class GrowthEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "ajib.db")
        self.old_db_path = os.environ.get("AJIB_DB_PATH")
        os.environ["AJIB_DB_PATH"] = self.db_path
        database.close_connections()

    def tearDown(self):
        database.close_connections()
        if self.old_db_path is None:
            os.environ.pop("AJIB_DB_PATH", None)
        else:
            os.environ["AJIB_DB_PATH"] = self.old_db_path

    def record(self, event_type, key, user_id, timestamp, **values):
        return growth_events.record_growth_event(
            event_type,
            deduplication_key=key,
            user_id=user_id,
            occurred_at=timestamp,
            path=self.db_path,
            **values,
        )

    def test_schema_v1_is_upgraded_with_growth_and_credit_tables(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "CREATE TABLE schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'old')"
            )

        connection = database.get_connection(self.db_path)

        self.assertEqual(database.schema_version(self.db_path), 2)
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "growth_events",
                "account_credit_accounts",
                "account_credit_transactions",
                "account_credit_reservations",
                "recruitment_milestones",
            }.issubset(tables)
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(growth_events)")
        }
        self.assertTrue(
            {
                "event_type",
                "user_id",
                "surface",
                "hosted_tenant_id",
                "language",
                "plan_id",
                "payment_method",
                "referral_campaign",
                "occurred_at",
                "deduplication_key",
            }.issubset(columns)
        )

    def test_recording_is_immutable_and_idempotent_per_event_scope(self):
        first = self.record(
            growth_events.EVENT_CHECKOUT_STARTED,
            "payment-1",
            42,
            "2026-08-01T10:00:00+03:30",
            language="fa",
            plan_id="basic",
            payment_method="card",
            referral_campaign="invite-7",
            metadata={"amount": 10},
        )
        retry = self.record(
            growth_events.EVENT_CHECKOUT_STARTED,
            "payment-1",
            999,
            "2026-08-02T00:00:00Z",
            metadata={"amount": 999},
        )
        completion = self.record(
            growth_events.EVENT_CHECKOUT_COMPLETED,
            "payment-1",
            42,
            "2026-08-01T10:05:00Z",
        )
        hosted = self.record(
            growth_events.EVENT_CHECKOUT_STARTED,
            "payment-1",
            42,
            "2026-08-01T10:00:00Z",
            surface=growth_events.SURFACE_HOSTED,
            hosted_tenant_id=7,
        )

        self.assertTrue(first.created)
        self.assertFalse(retry.created)
        self.assertEqual(retry.event.event_id, first.event.event_id)
        self.assertEqual(retry.event.user_id, "42")
        self.assertEqual(retry.event.metadata, {"amount": 10})
        self.assertEqual(first.event.occurred_at, "2026-08-01T06:30:00.000000Z")
        self.assertTrue(completion.created)
        self.assertTrue(hosted.created)
        self.assertEqual(hosted.event.hosted_tenant_id, "7")
        self.assertEqual(
            database.get_connection(self.db_path)
            .execute("SELECT COUNT(*) FROM growth_events")
            .fetchone()[0],
            3,
        )
        self.assertEqual(database.user_table_row_count(self.db_path), 3)

    def test_funnel_summary_is_ordered_bounded_and_tenant_scoped(self):
        events = (
            (growth_events.EVENT_TRIAL_ACTIVATED, "u1-trial", 1, "2026-08-01T00:00:00Z"),
            (growth_events.EVENT_CHECKOUT_STARTED, "u1-start", 1, "2026-08-01T01:00:00Z"),
            (growth_events.EVENT_CHECKOUT_COMPLETED, "u1-done", 1, "2026-08-01T02:00:00Z"),
            (growth_events.EVENT_CHECKOUT_COMPLETED, "u2-early", 2, "2026-08-01T00:00:00Z"),
            (growth_events.EVENT_CHECKOUT_STARTED, "u2-late", 2, "2026-08-01T01:00:00Z"),
            (growth_events.EVENT_CHECKOUT_STARTED, "u3-start", 3, "2026-08-01T03:00:00Z"),
            # End bound is exclusive.
            (growth_events.EVENT_CHECKOUT_COMPLETED, "u3-end", 3, "2026-08-02T00:00:00Z"),
        )
        for event_type, key, user_id, timestamp in events:
            self.record(event_type, key, user_id, timestamp)

        self.record(
            growth_events.EVENT_CHECKOUT_STARTED,
            "h7-start",
            7,
            "2026-08-01T00:00:00Z",
            surface=growth_events.SURFACE_HOSTED,
            hosted_tenant_id=7,
        )
        self.record(
            growth_events.EVENT_CHECKOUT_COMPLETED,
            "h7-done",
            7,
            "2026-08-01T00:01:00Z",
            surface=growth_events.SURFACE_HOSTED,
            hosted_tenant_id=7,
        )
        self.record(
            growth_events.EVENT_CHECKOUT_STARTED,
            "h8-start",
            8,
            "2026-08-01T00:00:00Z",
            surface=growth_events.SURFACE_HOSTED,
            hosted_tenant_id=8,
        )

        main = growth_events.main_admin_funnel_summary(
            start_at="2026-08-01T00:00:00Z",
            end_at="2026-08-02T00:00:00Z",
            path=self.db_path,
        )
        checkout = main["funnels"]["checkout"]
        self.assertEqual(main["total_events"], 6)
        self.assertEqual(
            main["event_counts"][growth_events.EVENT_CHECKOUT_STARTED],
            {"events": 3, "unique_users": 3},
        )
        self.assertEqual(checkout["started_users"], 3)
        self.assertEqual(checkout["completed_users"], 1)
        self.assertEqual(checkout["conversion_percent"], 33.33)

        tenant = growth_events.hosted_owner_funnel_summary(
            7,
            start_at="2026-08-01",
            end_at="2026-08-02",
            path=self.db_path,
        )
        self.assertEqual(tenant["hosted_tenant_id"], "7")
        self.assertEqual(tenant["total_events"], 2)
        self.assertEqual(tenant["funnels"]["checkout"]["conversion_percent"], 100.0)
        self.assertNotIn("7", str(tenant["funnels"]))

    def test_invalid_event_input_and_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "event_type"):
            self.record("Checkout Started", "key", 1, "2026-08-01")
        with self.assertRaisesRegex(ValueError, "deduplication_key"):
            self.record(growth_events.EVENT_PLAN_VIEWED, "", 1, "2026-08-01")
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            self.record(
                growth_events.EVENT_PLAN_VIEWED,
                "key",
                1,
                "2026-08-01",
                metadata={"amount": float("nan")},
            )


class GrowthFeatureTests(unittest.TestCase):
    def test_flags_default_on_and_accept_emergency_disable_values(self):
        self.assertTrue(
            growth_features.is_growth_feature_enabled(
                growth_features.BUYER_DISCOUNTS, environ={}
            )
        )
        flags = growth_features.growth_feature_flags(
            environ={
                "AJIB_GROWTH_BUYER_DISCOUNTS_ENABLED": "off",
                "AJIB_GROWTH_RECRUITMENT_REWARDS_ENABLED": "1",
                "AJIB_GROWTH_REMINDERS_ENABLED": "NO",
            }
        )
        self.assertEqual(
            flags,
            {
                growth_features.BUYER_DISCOUNTS: False,
                growth_features.RECRUITMENT_REWARDS: True,
                growth_features.REMINDERS: False,
            },
        )

    def test_unknown_feature_and_invalid_boolean_fail_loudly(self):
        with self.assertRaisesRegex(ValueError, "Unknown growth feature"):
            growth_features.is_growth_feature_enabled("typo", environ={})
        with self.assertRaisesRegex(ValueError, "Invalid boolean"):
            growth_features.is_growth_feature_enabled(
                growth_features.REMINDERS,
                environ={"AJIB_GROWTH_REMINDERS_ENABLED": "sometimes"},
            )


if __name__ == "__main__":
    unittest.main()
