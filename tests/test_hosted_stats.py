import ast
import importlib.util
import os
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
UTILS_DIR = BOT_DIR / "utils"
if str(UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(UTILS_DIR))
STATS_PATH = BOT_DIR / "utils" / "hosted_stats.py"
STATS_SPEC = importlib.util.spec_from_file_location("hosted_stats", STATS_PATH)
HOSTED_STATS = importlib.util.module_from_spec(STATS_SPEC)
STATS_SPEC.loader.exec_module(HOSTED_STATS)

TRANSLATIONS_PATH = BOT_DIR / "utils" / "hosted_translations.py"
TRANSLATIONS_SPEC = importlib.util.spec_from_file_location(
    "hosted_translations_for_stats", TRANSLATIONS_PATH
)
HOSTED_TRANSLATIONS_MODULE = importlib.util.module_from_spec(TRANSLATIONS_SPEC)
TRANSLATIONS_SPEC.loader.exec_module(HOSTED_TRANSLATIONS_MODULE)

WORKER_PATH = BOT_DIR / "hosted_worker.py"
WORKER_SOURCE = WORKER_PATH.read_text(encoding="utf-8")
WORKER_TREE = ast.parse(WORKER_SOURCE)


def _worker_function(name):
    return next(
        node
        for node in WORKER_TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _worker_constant(name):
    for node in WORKER_TREE.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing worker constant: {name}")


def _scheduler_namespace(send=None):
    notification_state = {}

    @contextmanager
    def locked_json(_path, _default):
        yield notification_state

    sent = []

    def send_owner_stats(chat_id, end_date=None, scheduled=False):
        sent.append((chat_id, end_date, scheduled))
        if send is not None:
            return send(chat_id, end_date=end_date, scheduled=scheduled)
        return 1

    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "uuid": uuid,
        "locked_json": locked_json,
        "tenant_file": lambda owner_id, name: f"hosted/{owner_id}/{name}",
        "OWNER_ID": 7,
        "OWNER_STATS_SEND_HOUR": 0,
        "OWNER_STATS_SEND_MINUTE": 5,
        "OWNER_STATS_CLAIM_LEASE_SECONDS": 600,
        "_send_owner_stats": send_owner_stats,
    }
    functions = [
        "_parse_time",
        "_owner_stats_report_end",
        "_claim_owner_stats_report",
        "_finish_owner_stats_report",
        "_run_due_owner_stats",
    ]
    module = ast.Module(body=[_worker_function(name) for name in functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)
    return namespace, notification_state, sent


class HostedStatsAggregationTests(unittest.TestCase):
    def test_empty_snapshot_includes_seven_days_and_trailing_thirty_across_year(self):
        snapshot = HOSTED_STATS.build_hosted_stats({}, [], end_date=date(2026, 1, 2))

        self.assertEqual(snapshot["start_date"], "2025-12-27")
        self.assertEqual(snapshot["end_date"], "2026-01-02")
        self.assertEqual(snapshot["last30_start_date"], "2025-12-04")
        self.assertEqual(len(snapshot["days"]), 7)
        self.assertEqual(snapshot["days"][0]["date"], "2025-12-27")
        self.assertEqual(snapshot["days"][-1]["date"], "2026-01-02")
        self.assertEqual(snapshot["last30"]["completed"], 0)
        self.assertEqual(snapshot["last30"]["revenue"], 0)

    def test_full_funnel_financials_methods_buyers_and_manual_configs(self):
        payments = {
            "card": {
                "status": "completed",
                "created_at": "2026-07-30 08:00:00",
                "completed_at": "2026-07-31 09:00:00",
                "updated_at": "2026-08-01 00:00:00",
                "user_id": 10,
                "payment_method": "card",
                "retail_price": 120,
                "wholesale_price": 80,
                "referral_reward": 8,
            },
            "crypto-renewal": {
                "status": "completed",
                "created_at": "2026-07-31 08:00:00",
                "updated_at": "2026-08-01 10:00:00",
                "user_id": "10",
                "payment_method": "crypto",
                "retail_price": 100,
                "crypto_collected": 95,
                "wholesale_price": 80,
                "referral_reward": 3,
                "renew_username": "hs10",
            },
            "open": {
                "status": "pending_approval",
                "created_at": "2026-08-01 11:00:00",
                "updated_at": "2026-08-02 11:00:00",
            },
            "attention": {
                "status": "paid_provision_failed",
                "created_at": "2026-07-30 11:00:00",
                "updated_at": "2026-08-01 12:00:00",
                "crypto_collected": 200,
            },
            "failed": {
                "status": "rejected",
                "created_at": "2026-08-01 13:00:00",
                "updated_at": "2026-08-01 14:00:00",
            },
            "expired": {
                "status": "expired",
                "created_at": "2026-07-01 13:00:00",
                "updated_at": "2026-07-31 14:00:00",
            },
        }
        configs = [
            {
                "retail_order_id": "manual-one",
                "origin_bot_id": "700",
                "timestamp": "2026-08-01 15:00:00",
            },
            {
                "retail_order_id": "manual-other-bot",
                "origin_bot_id": "701",
                "timestamp": "2026-08-01 15:00:00",
            },
            {
                "retail_order_id": "storefront-order",
                "origin_bot_id": "700",
                "timestamp": "2026-08-01 15:00:00",
            },
        ]

        snapshot = HOSTED_STATS.build_hosted_stats(
            payments, configs, end_date="2026-08-01", origin_bot_id="700"
        )
        by_date = {item["date"]: item for item in snapshot["days"]}

        self.assertEqual(by_date["2026-07-30"]["started"], 2)
        self.assertEqual(by_date["2026-07-31"]["completed"], 1)
        self.assertEqual(by_date["2026-07-31"]["expired"], 1)
        today = by_date["2026-08-01"]
        self.assertEqual(today["started"], 2)
        self.assertEqual(today["completed"], 1)
        self.assertEqual(today["open"], 1)
        self.assertEqual(today["attention"], 1)
        self.assertEqual(today["failed"], 1)
        self.assertEqual(today["manual_configs"], 1)

        total = snapshot["last30"]
        self.assertEqual(total["completed"], 2)
        self.assertEqual(total["unique_buyers"], 1)
        self.assertEqual(total["new_configs"], 1)
        self.assertEqual(total["renewals"], 1)
        self.assertEqual(total["manual_configs"], 1)
        self.assertEqual(total["revenue"], 215)
        self.assertEqual(total["gross_profit"], 55)
        self.assertEqual(total["referral_payouts"], 11)
        self.assertEqual(total["net_profit"], 44)
        self.assertEqual(total["methods"]["card"], {"completed": 1, "revenue": 120.0})
        self.assertEqual(total["methods"]["crypto"], {"completed": 1, "revenue": 95.0})

    def test_thirty_day_window_is_inclusive_and_legacy_completion_uses_updated_at(self):
        snapshot = HOSTED_STATS.build_hosted_stats(
            {
                "first-day": {
                    "status": "completed",
                    "created_at": "2026-06-01 00:00:00",
                    "updated_at": "2026-07-04 12:00:00",
                    "retail_price": "10.005",
                    "wholesale_price": "4.005",
                    "referral_reward": "1",
                    "user_id": 1,
                },
                "outside": {
                    "status": "completed",
                    "created_at": "2026-06-01 00:00:00",
                    "updated_at": "2026-07-03 23:59:59",
                    "retail_price": 99,
                },
                "invalid-money": {
                    "status": "completed",
                    "created_at": "2026-08-02 00:00:00",
                    "updated_at": "2026-08-02 00:00:00",
                    "retail_price": float("nan"),
                    "wholesale_price": "bad",
                    "referral_reward": None,
                    "user_id": 2,
                },
            },
            [],
            end_date="2026-08-02",
        )

        self.assertEqual(snapshot["last30_start_date"], "2026-07-04")
        self.assertEqual(snapshot["last30"]["completed"], 2)
        self.assertEqual(snapshot["last30"]["revenue"], 10.01)
        self.assertEqual(snapshot["last30"]["gross_profit"], 6.0)
        self.assertEqual(snapshot["last30"]["net_profit"], 5.0)

    def test_completed_renewal_does_not_move_when_operational_timestamp_changes(self):
        snapshot = HOSTED_STATS.build_hosted_stats(
            {
                "renewal": {
                    "status": "completed",
                    "created_at": "2026-08-12 17:25:07",
                    "completed_at": "2026-08-12 17:33:57",
                    "updated_at": "2026-08-13 17:39:22",
                    "renewal_status": "processing",
                    "retail_price": 4.05,
                    "user_id": 1,
                    "renew_username": "s6985678513",
                },
            },
            [],
            end_date="2026-08-13",
        )
        by_date = {item["date"]: item for item in snapshot["days"]}

        self.assertEqual(by_date["2026-08-12"]["completed"], 1)
        self.assertEqual(by_date["2026-08-12"]["revenue"], 4.05)
        self.assertEqual(by_date["2026-08-13"]["completed"], 0)
        self.assertEqual(by_date["2026-08-13"]["revenue"], 0)


class HostedStatsWorkerTests(unittest.TestCase):
    def test_money_menu_routes_statistics_as_an_owner_action(self):
        self.assertIn(("stats",), _worker_constant("OWNER_MONEY_ROWS"))
        owner_handler = ast.get_source_segment(
            WORKER_SOURCE, _worker_function("owner_menu_action")
        )
        owner_action = ast.get_source_segment(
            WORKER_SOURCE, _worker_function("_handle_owner_action")
        )
        self.assertIn("m.from_user.id == OWNER_ID", WORKER_SOURCE)
        self.assertIn("_handle_owner_action", owner_handler)
        self.assertIn('if action == "stats"', owner_action)
        self.assertIn("_send_owner_stats(chat_id)", owner_action)

    def test_stats_templates_are_complete_and_format_in_every_language(self):
        catalogs = HOSTED_TRANSLATIONS_MODULE.HOSTED_TRANSLATIONS
        expected = set(catalogs["en"])
        values = {
            "label": "2026-08-02",
            "started": 1,
            "completed": 1,
            "open": 0,
            "attention": 0,
            "failed": 0,
            "expired": 0,
            "buyers": 1,
            "new_configs": 1,
            "renewals": 0,
            "manual_configs": 0,
            "card_count": 1,
            "card_revenue": "12.00",
            "crypto_count": 0,
            "crypto_revenue": "0.00",
            "other_count": 0,
            "other_revenue": "0.00",
            "revenue": "12.00",
            "gross": "4.00",
            "referrals": "1.00",
            "net": "3.00",
        }
        for language, catalog in catalogs.items():
            with self.subTest(language=language):
                self.assertEqual(set(catalog), expected)
                rendered = HOSTED_TRANSLATIONS_MODULE.hosted_text(
                    language, "stats_period", **values
                )
                self.assertIn("2026-08-02", rendered)
                self.assertIn("12.00", rendered)

    def test_live_report_renders_and_splits_safely_in_every_language(self):
        snapshot_payments = {
            "sale": {
                "status": "completed",
                "created_at": "2026-08-02 08:00:00",
                "completed_at": "2026-08-02 08:01:00",
                "user_id": 10,
                "payment_method": "card",
                "retail_price": 12,
                "wholesale_price": 8,
                "referral_reward": 1,
            }
        }
        for language in HOSTED_TRANSLATIONS_MODULE.HOSTED_TRANSLATIONS:
            with self.subTest(language=language):
                namespace = {
                    "datetime": datetime,
                    "os": os,
                    "OWNER_ID": 7,
                    "TELEGRAM_SAFE_TEXT_LIMIT": 3800,
                    "get_reseller_data": lambda _owner_id: {"configs": []},
                    "_tenant_payments": lambda: snapshot_payments,
                    "build_hosted_stats": HOSTED_STATS.build_hosted_stats,
                    "format_usd_amount": lambda value: f"{float(value):.2f}",
                    "_hosted_message": lambda _owner_id, key, **values: (
                        HOSTED_TRANSLATIONS_MODULE.hosted_text(language, key, **values)
                    ),
                }
                module = ast.Module(
                    body=[
                        _worker_function("_split_message_blocks"),
                        _worker_function("_stats_period_text"),
                        _worker_function("_owner_stats_chunks"),
                    ],
                    type_ignores=[],
                )
                exec(
                    compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"),
                    namespace,
                )
                chunks = namespace["_owner_stats_chunks"](
                    end_date=date(2026, 8, 2), scheduled=False
                )
                self.assertTrue(chunks)
                self.assertTrue(all(len(chunk) <= 3800 for chunk in chunks))
                self.assertIn("12.00", "\n".join(chunks))

    def test_completion_timestamp_is_written_once(self):
        payments = {"existing": {"status": "completed", "completed_at": "old"}}

        @contextmanager
        def locked_json(_path, _default):
            yield payments

        namespace = {
            "locked_json": locked_json,
            "tenant_file": lambda owner_id, name: f"{owner_id}/{name}",
            "OWNER_ID": 7,
            "_now": lambda: "2026-08-02 10:00:00",
        }
        module = ast.Module(body=[_worker_function("_save_payment")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)

        created = namespace["_save_payment"]("new", {"status": "completed"})
        existing = namespace["_save_payment"]("existing", {"status": "completed"})
        self.assertEqual(created["completed_at"], "2026-08-02 10:00:00")
        self.assertEqual(created["created_at"], "2026-08-02 10:00:00")
        self.assertEqual(existing["completed_at"], "old")

    def test_scheduler_waits_until_0005_and_sends_latest_completed_window_once(self):
        namespace, state, sent = _scheduler_namespace()
        run_due = namespace["_run_due_owner_stats"]

        self.assertFalse(run_due(datetime(2026, 8, 2, 0, 4, 59)))
        self.assertTrue(run_due(datetime(2026, 8, 2, 0, 5, 0)))
        self.assertEqual(sent, [(7, date(2026, 8, 1), True)])
        self.assertEqual(state["owner_daily_stats"]["last_sent_for"], "2026-08-01")
        self.assertFalse(run_due(datetime(2026, 8, 2, 18, 0, 0)))

        state["owner_daily_stats"]["last_sent_for"] = "2026-07-25"
        self.assertTrue(run_due(datetime(2026, 8, 3, 12, 0, 0)))
        self.assertEqual(sent[-1], (7, date(2026, 8, 2), True))
        run_source = ast.get_source_segment(WORKER_SOURCE, _worker_function("run"))
        self.assertIn("hosted-owner-stats", run_source)

    def test_scheduler_retries_failures_and_recovers_stale_claims(self):
        attempts = []

        def flaky_send(*_args, **_kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("Telegram unavailable")
            return 1

        namespace, state, _sent = _scheduler_namespace(send=flaky_send)
        run_due = namespace["_run_due_owner_stats"]
        now = datetime(2026, 8, 2, 0, 5, 0)

        self.assertFalse(run_due(now))
        self.assertNotIn("last_sent_for", state["owner_daily_stats"])
        self.assertTrue(run_due(now + timedelta(minutes=1)))
        self.assertEqual(state["owner_daily_stats"]["last_sent_for"], "2026-08-01")

        state["owner_daily_stats"] = {
            "claim_for": "2026-08-02",
            "claim_id": "abandoned",
            "claimed_at": "2026-08-03 00:00:00",
        }
        claim = namespace["_claim_owner_stats_report"](
            date(2026, 8, 2), now=datetime(2026, 8, 3, 0, 11, 0)
        )
        self.assertIsNotNone(claim)
        self.assertNotEqual(claim, "abandoned")


if __name__ == "__main__":
    unittest.main()
