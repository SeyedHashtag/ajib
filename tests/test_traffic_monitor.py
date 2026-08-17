import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "traffic_monitor.py"
)

GB = 1024 ** 3


class FakeBot:
    def __init__(self):
        self.sent_messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))


class FakeButton:
    def __init__(self, text, callback_data=None, **_kwargs):
        self.text = text
        self.callback_data = callback_data


class FakeMarkup:
    def __init__(self, *args, **kwargs):
        self.rows = []

    def add(self, *buttons):
        self.rows.append(list(buttons))
        return self


class FakeClient:
    server_id = "primary"


class FakeMultiServerAPI:
    def __init__(self, users):
        self.servers = [{"id": "enabled", "enabled": True}]
        self.users = users
        self.include_disabled_calls = []

    def iter_all_users(self, include_disabled=True):
        self.include_disabled_calls.append(include_disabled)
        for enabled, username, data in self.users:
            if include_disabled or enabled:
                yield FakeClient(), username, data


def install_stubs():
    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace(
        InlineKeyboardMarkup=FakeMarkup,
        InlineKeyboardButton=FakeButton,
    )
    sys.modules["telebot"] = telebot_stub

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(MODULE_PATH.parent)]
    sys.modules["utils"] = utils_pkg

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.MultiServerAPI = lambda: FakeMultiServerAPI([])
    sys.modules["utils.api_client"] = api_client_stub

    command_stub = types.ModuleType("utils.command")
    command_stub.bot = FakeBot()
    sys.modules["utils.command"] = command_stub

    language_stub = types.ModuleType("utils.language")
    language_stub.get_user_language = lambda user_id: "en"
    sys.modules["utils.language"] = language_stub

    translations_stub = types.ModuleType("utils.translations")
    translations_stub.get_message_text = lambda language, key: {
        "traffic_quota_alert": "regular {username} {percent}",
        "time_quota_alert": "regular-time {username} {percent} {days_used} {total_days} {days_remaining}",
        "reseller_client_traffic_alert": "reseller-gb {customer_name} {username} {percent}",
        "reseller_client_days_alert": "reseller-days {customer_name} {username} {percent} {days_used} {total_days} {days_remaining}",
    }[key]
    translations_stub.get_button_text = lambda language, key: key
    sys.modules["utils.translations"] = translations_stub


def load_traffic_monitor_module():
    install_stubs()
    module_name = "traffic_monitor_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class TrafficMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.monitor = load_traffic_monitor_module()
        self.monitor.ALERTS_FILE = str(Path(self.tmp_dir.name) / "traffic_alerts.json")
        self.monitor.RESELLERS_FILE = str(Path(self.tmp_dir.name) / "resellers.json")
        self.bot = FakeBot()
        self.monitor.bot = self.bot

    def tearDown(self):
        self.tmp_dir.cleanup()

    def run_monitor(self, users):
        normalized_users = [
            (
                enabled,
                username,
                {
                    "blocked": False,
                    "status": "Offline",
                    "account_creation_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "expiration_days": 30,
                    **data,
                },
            )
            for enabled, username, data in users
        ]
        multi_api = FakeMultiServerAPI(normalized_users)
        self.monitor.MultiServerAPI = lambda: multi_api
        self.monitor.monitor_user_traffic()
        return multi_api

    def read_alerts(self):
        with open(self.monitor.ALERTS_FILE, "r") as f:
            return json.load(f)

    def write_alerts(self, alerts):
        with open(self.monitor.ALERTS_FILE, "w") as f:
            json.dump(alerts, f)

    def write_reseller_config(self, reseller_id, username, days=100, customer_name="ali123"):
        config = {
            "username": username,
            "server_id": "primary",
            "days": days,
            "timestamp": (datetime.now(timezone.utc) - timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if customer_name is not None:
            config["customer_name"] = customer_name
        with open(self.monitor.RESELLERS_FILE, "w") as f:
            json.dump({str(reseller_id): {"configs": [config]}}, f)

    def install_customer_context(self, payments, offer=None, growth_events=None):
        edit_plans_stub = types.ModuleType("utils.edit_plans")
        edit_plans_stub.load_plans = lambda: {
            "100": {"price": 10, "days": 30, "unlimited": False, "target": "both"},
        }
        sys.modules["utils.edit_plans"] = edit_plans_stub

        payment_records_stub = types.ModuleType("utils.payment_records")
        payment_records_stub.load_payments = lambda: payments
        sys.modules["utils.payment_records"] = payment_records_stub

        renewal_stub = types.ModuleType("utils.renewal")
        renewal_stub.find_customer_renewal_offer = lambda *args, **kwargs: (
            offer or {"eligible": False}
        )
        renewal_stub.find_reseller_renewal_offer = lambda *args, **kwargs: {"eligible": False}
        sys.modules["utils.renewal"] = renewal_stub

        if growth_events is not None:
            growth_stub = types.ModuleType("utils.growth_events")
            growth_stub.EVENT_RENEWAL_PROMPTED = "renewal_prompted"
            growth_stub.record_growth_event = growth_events
            sys.modules["utils.growth_events"] = growth_stub

    @staticmethod
    def first_callback(markup):
        if markup is None:
            return None
        rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None)
        button = rows[0][0]
        return getattr(button, "callback_data", None)

    def test_regular_user_at_95_percent_gets_one_alert_and_marks_all_crossed_thresholds(self):
        self.run_monitor([
            (True, "s123", {"upload_bytes": 95 * GB, "download_bytes": 0, "max_download_bytes": 100 * GB}),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertEqual(self.bot.sent_messages[0][0], 123)
        self.assertIn("regular s123 95", self.bot.sent_messages[0][1])
        self.assertEqual(self.read_alerts()["s123"]["notified"], [80, 90])

    def test_reseller_client_at_95_percent_gb_gets_one_alert_and_marks_all_crossed_thresholds(self):
        self.write_reseller_config(456, "r456", customer_name="ali123")

        self.run_monitor([
            (True, "r456", {"upload_bytes": 95 * GB, "download_bytes": 0, "max_download_bytes": 100 * GB}),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertEqual(self.bot.sent_messages[0][0], 456)
        self.assertIn("reseller-gb ali123 r456 95", self.bot.sent_messages[0][1])
        self.assertEqual(self.read_alerts()["r456"]["gb_notified"], [80, 90])

    def test_external_bulk_reseller_username_routes_alert_with_empty_note(self):
        username = "r7784615720c184"
        self.write_reseller_config(7784615720, username, customer_name=None)

        self.run_monitor([
            (True, username, {
                "upload_bytes": 95 * GB,
                "download_bytes": 0,
                "max_download_bytes": 100 * GB,
                "note": "",
            }),
        ])

        self.assertEqual(self.monitor._extract_reseller_id(username), 7784615720)
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertEqual(self.bot.sent_messages[0][0], 7784615720)
        self.assertIn(f"reseller-gb — {username} 95", self.bot.sent_messages[0][1])

    def test_reseller_client_at_95_percent_days_gets_one_alert_and_marks_all_crossed_thresholds(self):
        self.write_reseller_config(456, "r456", days=100, customer_name="sara88")

        self.run_monitor([
            (True, "r456", {
                "account_creation_date": (datetime.now(timezone.utc) - timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S"),
                "expiration_days": 100,
                "max_download_bytes": 0,
            }),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertEqual(self.bot.sent_messages[0][0], 456)
        self.assertIn("reseller-days sara88 r456 95", self.bot.sent_messages[0][1])
        self.assertEqual(self.read_alerts()["r456"]["days_notified"], [80, 90])

    def test_reseller_customer_name_falls_back_to_legacy_note(self):
        self.write_reseller_config(456, "r456", customer_name=None)

        self.run_monitor([
            (
                True,
                "r456",
                {
                    "upload_bytes": 95 * GB,
                    "download_bytes": 0,
                    "max_download_bytes": 100 * GB,
                    "note": "📅 2026-05-30 07:01 | 📝 sara88 | ✏️ ",
                },
            ),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("reseller-gb sara88 r456 95", self.bot.sent_messages[0][1])

    def test_reseller_customer_name_uses_neutral_fallback_when_missing_or_invalid(self):
        self.write_reseller_config(456, "r456", customer_name="too-long-name")

        self.run_monitor([
            (True, "r456", {"upload_bytes": 95 * GB, "download_bytes": 0, "max_download_bytes": 100 * GB}),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("reseller-gb — r456 95", self.bot.sent_messages[0][1])

    def test_regular_user_already_notified_at_80_only_gets_90_alert(self):
        self.write_alerts({"s123": {"notified": [80], "max_download_bytes": 100 * GB}})

        self.run_monitor([
            (True, "s123", {"upload_bytes": 95 * GB, "download_bytes": 0, "max_download_bytes": 100 * GB}),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("regular s123 95", self.bot.sent_messages[0][1])
        self.assertEqual(self.read_alerts()["s123"]["notified"], [80, 90])

    def test_disabled_servers_are_skipped_by_quota_monitoring(self):
        multi_api = self.run_monitor([
            (False, "s123", {"upload_bytes": 95 * GB, "download_bytes": 0, "max_download_bytes": 100 * GB}),
        ])

        self.assertEqual(multi_api.include_disabled_calls, [False])
        self.assertEqual(self.bot.sent_messages, [])

    def test_unknown_live_state_sends_no_automated_alert(self):
        self.run_monitor([
            (
                True,
                "s123",
                {
                    "status": "unexpected",
                    "upload_bytes": 95 * GB,
                    "download_bytes": 0,
                    "max_download_bytes": 100 * GB,
                },
            ),
        ])

        self.assertEqual(self.bot.sent_messages, [])
        self.assertFalse(os.path.exists(self.monitor.ALERTS_FILE))

    def test_regular_time_threshold_uses_payment_duration_and_reserved_renewal_action(self):
        events = []
        self.install_customer_context(
            {
                "sale-1": {
                    "user_id": 123,
                    "username": "s123",
                    "server_id": "primary",
                    "status": "completed",
                    "plan_gb": "100",
                    "days": 30,
                    "created_at": (datetime.now(timezone.utc) - timedelta(days=24)).strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
            offer={
                "eligible": True,
                "token": "reserve-token",
                "renewal_mode": "reserved",
            },
            growth_events=lambda *args, **kwargs: events.append((args, kwargs)),
        )

        self.run_monitor([
            (
                True,
                "s123",
                {
                    "account_creation_date": (datetime.now(timezone.utc) - timedelta(days=24)).strftime("%Y-%m-%d %H:%M:%S"),
                    "expiration_days": 30,
                    "upload_bytes": 10 * GB,
                    "download_bytes": 0,
                    "max_download_bytes": 100 * GB,
                },
            ),
        ])

        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("regular-time s123 80", self.bot.sent_messages[0][1])
        self.assertEqual(
            self.first_callback(self.bot.sent_messages[0][2]["reply_markup"]),
            "renew_plan:reserve-token",
        )
        self.assertEqual(self.read_alerts()["s123"]["notified"], [80])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["metadata"]["basis"], "time")

    def test_time_and_traffic_share_thresholds_without_duplicate_alerts(self):
        payments = {
            "sale-1": {
                "user_id": 123,
                "username": "s123",
                "server_id": "primary",
                "status": "completed",
                "plan_gb": "100",
                "days": 30,
                "created_at": (datetime.now(timezone.utc) - timedelta(days=21)).strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        self.install_customer_context(payments)

        account_state = sys.modules["utils.account_state"]
        original_utc_now = account_state.utc_now
        base_now = original_utc_now()
        live = {
            "account_creation_date": (base_now - timedelta(days=21)).isoformat(),
            "expiration_days": 30,
            "upload_bytes": 85 * GB,
            "download_bytes": 0,
            "max_download_bytes": 100 * GB,
        }
        try:
            account_state.utc_now = lambda: base_now
            self.run_monitor([(True, "s123", live)])
            account_state.utc_now = lambda: base_now + timedelta(days=6)
            self.run_monitor([(True, "s123", live)])
            account_state.utc_now = lambda: base_now + timedelta(days=7)
            self.run_monitor([(True, "s123", live)])
        finally:
            account_state.utc_now = original_utc_now

        self.assertEqual(len(self.bot.sent_messages), 2)
        self.assertIn("regular s123 85", self.bot.sent_messages[0][1])
        self.assertIn("regular-time s123 90", self.bot.sent_messages[1][1])
        self.assertEqual(self.read_alerts()["s123"]["notified"], [80, 90])

    def test_reminders_feature_flag_suppresses_lifecycle_alerts(self):
        original = os.environ.get("AJIB_GROWTH_REMINDERS_ENABLED")
        os.environ["AJIB_GROWTH_REMINDERS_ENABLED"] = "false"
        try:
            multi_api = self.run_monitor([
                (
                    True,
                    "s123",
                    {
                        "upload_bytes": 95 * GB,
                        "download_bytes": 0,
                        "max_download_bytes": 100 * GB,
                    },
                ),
            ])
        finally:
            if original is None:
                os.environ.pop("AJIB_GROWTH_REMINDERS_ENABLED", None)
            else:
                os.environ["AJIB_GROWTH_REMINDERS_ENABLED"] = original

        self.assertEqual(multi_api.include_disabled_calls, [])
        self.assertEqual(self.bot.sent_messages, [])


if __name__ == "__main__":
    unittest.main()
