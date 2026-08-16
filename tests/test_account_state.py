import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "account_state.py"


def load_module():
    spec = importlib.util.spec_from_file_location("account_state_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AccountStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AJIB_TIMEZONE"] = "Asia/Tehran"
        cls.state = load_module()

    def test_hold_status_variants_are_normalized(self):
        for status in ("On-hold", "On Hold", "on_hold", " ON   HOLD "):
            with self.subTest(status=status):
                snapshot = self.state.inspect_account({
                    "status": status,
                    "blocked": False,
                    "expiration_days": 30,
                })
                self.assertEqual(snapshot.panel_state.value, "hold")
                self.assertEqual(snapshot.state, "hold")
                self.assertFalse(snapshot.timer_started)

    def test_blocked_takes_precedence_over_hold(self):
        snapshot = self.state.inspect_account({
            "status": "On-hold",
            "blocked": True,
            "expiration_days": 30,
        })
        self.assertEqual(snapshot.panel_state.value, "blocked")

    def test_absolute_expiry_and_explicit_timer_support_three_x_records(self):
        snapshot = self.state.inspect_account({
            "status": "Offline",
            "blocked": False,
            "expiration_days": 30,
            "timer_started": True,
            "account_expiration_date": "2026-09-15T00:00:00+00:00",
        }, now=datetime(2026, 9, 1, tzinfo=timezone.utc))

        self.assertEqual(snapshot.panel_state.value, "connected")
        self.assertTrue(snapshot.timer_started)
        self.assertEqual(snapshot.panel_deadline, datetime(2026, 9, 15, tzinfo=timezone.utc))
        self.assertEqual(snapshot.panel_days_remaining, 14)

    def test_inconsistent_or_missing_live_fields_fail_closed(self):
        inconsistent = self.state.inspect_account({
            "status": "On-hold",
            "blocked": False,
            "account_creation_date": "2026-01-01 00:00:00",
            "expiration_days": 30,
        })
        missing = self.state.inspect_account({"status": "Offline"})
        unavailable = self.state.inspect_account(None, available=False)
        self.assertEqual(inconsistent.panel_state.value, "unknown")
        self.assertEqual(missing.panel_state.value, "unknown")
        self.assertEqual(unavailable.panel_state.value, "unknown")
        self.assertTrue(unavailable.stale)

        malformed_duration = self.state.inspect_account({
            "status": "Offline",
            "blocked": False,
            "account_creation_date": "2026-01-01 00:00:00",
            "expiration_days": "not-a-duration",
        })
        unsupported_status = self.state.inspect_account({
            "status": "Active",
            "blocked": False,
            "account_creation_date": "2026-01-01 00:00:00",
            "expiration_days": 30,
        })
        self.assertEqual(malformed_duration.state, "unknown")
        self.assertEqual(unsupported_status.state, "unknown")

    def test_panel_duration_is_not_treated_as_remaining_days(self):
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        snapshot = self.state.inspect_account({
            "status": "Offline",
            "blocked": False,
            "account_creation_date": "2026-01-20T00:00:00+00:00",
            "expiration_days": 30,
        }, now=now)
        self.assertEqual(snapshot.configured_days, 30)
        self.assertEqual(snapshot.panel_days_remaining, 18)

    def test_naive_timestamp_uses_bot_timezone(self):
        parsed = self.state.parse_timestamp("2026-01-01 03:30:00")
        self.assertEqual(parsed, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))

    def test_cycle_uses_latest_successful_applied_record(self):
        records = {
            "initial": {
                "username": "alice",
                "server_id": "s1",
                "days": 30,
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            "pending": {
                "renewal_username": "alice",
                "renewal_server_id": "s1",
                "days": 60,
                "status": "completed",
                "renewal_mode": "reserved",
                "renewal_status": "reserved",
                "updated_at": "2026-02-01T00:00:00+00:00",
            },
            "applied": {
                "renewal_username": "alice",
                "renewal_server_id": "s1",
                "days": 60,
                "status": "completed",
                "renewal_mode": "reserved",
                "renewal_status": "applied",
                "renewal_applied_at": "2026-02-02T00:00:00+00:00",
            },
        }
        cycle = self.state.resolve_service_cycle(
            records, username="alice", server_id="s1", source="customer"
        )
        self.assertEqual(cycle.record_id, "applied")
        self.assertEqual(cycle.duration_days, 60)
        self.assertEqual(cycle.deadline, datetime(2026, 4, 3, tzinfo=timezone.utc))

    def test_cycle_requires_exact_username_and_server(self):
        records = {
            "wrong-server": {
                "username": "alice", "server_id": "s2", "days": 30,
                "status": "completed", "created_at": "2026-01-01",
            },
            "wrong-user": {
                "username": "bob", "server_id": "s1", "days": 30,
                "status": "completed", "created_at": "2026-01-01",
            },
        }
        self.assertIsNone(self.state.resolve_service_cycle(
            records, username="alice", server_id="s1", source="customer"
        ))

    def test_entitlement_boundary_is_strict_and_timezone_aware(self):
        cycle = self.state.resolve_service_cycle({
            "p1": {
                "username": "alice", "server_id": "s1", "days": 30,
                "status": "completed", "created_at": "2026-01-01T00:00:00+00:00",
            }
        }, username="alice", server_id="s1", source="customer")
        before = self.state.inspect_account(
            {"status": "On-hold", "blocked": False, "expiration_days": 30},
            cycle=cycle,
            now=cycle.deadline - timedelta(seconds=1),
        )
        at_deadline = self.state.inspect_account(
            {"status": "On-hold", "blocked": False, "expiration_days": 30},
            cycle=cycle,
            now=cycle.deadline,
        )
        self.assertEqual(before.entitlement_state.value, "current")
        self.assertEqual(at_deadline.entitlement_state.value, "expired")
        self.assertEqual(at_deadline.state, "expired")
        self.assertEqual(at_deadline.deadline_source.value, "issuance")
        self.assertEqual(at_deadline.service_days_remaining, 0)

    def test_connected_account_ignores_old_issuance_and_uses_panel_deadline(self):
        cycle = self.state.resolve_service_cycle({
            "old": {
                "username": "alice",
                "server_id": "s1",
                "days": 30,
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        }, username="alice", server_id="s1", source="customer")
        snapshot = self.state.inspect_account({
            "status": "Online",
            "blocked": False,
            "account_creation_date": "2026-07-21T00:00:00+00:00",
            "expiration_days": 60,
        }, cycle=cycle, now=datetime(2026, 8, 12, tzinfo=timezone.utc))

        self.assertEqual(snapshot.entitlement_state.value, "current")
        self.assertEqual(snapshot.deadline_source.value, "panel")
        self.assertEqual(snapshot.service_days_remaining, 38)
        self.assertEqual(snapshot.service_deadline, snapshot.panel_deadline)
        self.assertNotEqual(snapshot.service_marker, cycle.fingerprint)

    def test_manual_block_is_not_expired(self):
        snapshot = self.state.inspect_account({
            "status": "Offline",
            "blocked": True,
            "account_creation_date": "2026-08-01T00:00:00+00:00",
            "expiration_days": 30,
            "max_download_bytes": 1024,
            "upload_bytes": 1,
            "download_bytes": 0,
        }, now=datetime(2026, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(snapshot.panel_state.value, "blocked")
        self.assertEqual(snapshot.entitlement_state.value, "unknown")
        self.assertEqual(snapshot.state, "blocked")

    def test_verified_blocked_panel_expiration_is_shared_state(self):
        snapshot = self.state.inspect_account({
            "status": "Offline",
            "blocked": True,
            "account_creation_date": "2026-01-01T00:00:00+00:00",
            "expiration_days": 30,
            "max_download_bytes": 1024,
            "upload_bytes": 1024,
            "download_bytes": 0,
        }, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(snapshot.entitlement_state.value, "expired")
        self.assertEqual(snapshot.state, "expired")


if __name__ == "__main__":
    unittest.main()
