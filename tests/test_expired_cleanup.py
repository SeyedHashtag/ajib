import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "expired_cleanup.py"
)
TRANSLATIONS_PATH = MODULE_PATH.with_name("translations.py")
TEST_CONFIG_STORE_PATH = MODULE_PATH.with_name("test_config_store.py")
UTILS_DIR = MODULE_PATH.parent


class DummyBot:
    def __init__(self):
        self.sent_messages = []
        self.sent_documents = []
        self.replies = []
        self.edited_messages = []
        self.answered_callbacks = []

    def message_handler(self, *args, **kwargs):
        return lambda func: func

    def callback_query_handler(self, *args, **kwargs):
        return lambda func: func

    def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text, kwargs))

    def send_document(self, chat_id, document, **kwargs):
        self.sent_documents.append((chat_id, document, kwargs))

    def reply_to(self, message, text, **kwargs):
        self.replies.append((message, text, kwargs))

    def edit_message_text(self, text, **kwargs):
        self.edited_messages.append((text, kwargs))

    def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        self.answered_callbacks.append((callback_query_id, text, kwargs))


_DEFAULT_DELETE_RESULT = object()


class FakeClient:
    def __init__(self, server_id, users=None, delete_result=_DEFAULT_DELETE_RESULT, unavailable=False):
        self.server_id = server_id
        self.users = dict(users or {})
        self.delete_result = {"ok": True} if delete_result is _DEFAULT_DELETE_RESULT else delete_result
        self.unavailable = unavailable
        self.deleted = []
        self.get_user_calls = []
        self.get_users_calls = 0

    def get_user(self, username):
        self.get_user_calls.append(username)
        if self.unavailable:
            return None
        return self.users.get(username)

    def get_users(self):
        self.get_users_calls += 1
        if self.unavailable:
            return None
        return self.users

    def delete_user(self, username):
        self.deleted.append(username)
        if self.delete_result is not None:
            self.users.pop(username, None)
        return self.delete_result


class BulkOnlyFakeClient(FakeClient):
    def get_user(self, username):
        self.get_user_calls.append(username)
        return None


class FakeMultiAPI:
    def __init__(self, clients):
        self.clients = clients

    def get_client(self, server_id=None):
        if server_id:
            return self.clients.get(server_id)
        return next(iter(self.clients.values()), None)

    def iter_clients(self, include_disabled=False):
        for server_id, client in self.clients.items():
            yield {"id": server_id, "enabled": True}, client


class ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return types.SimpleNamespace()


def load_module():
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
    sys.modules.pop("telebot", None)

    bot = DummyBot()
    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(UTILS_DIR)]
    sys.modules["utils"] = utils_pkg

    store_spec = importlib.util.spec_from_file_location("utils.test_config_store", TEST_CONFIG_STORE_PATH)
    store_module = importlib.util.module_from_spec(store_spec)
    sys.modules[store_spec.name] = store_module
    store_spec.loader.exec_module(store_module)
    utils_pkg.test_config_store = store_module

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.MultiServerAPI = lambda: FakeMultiAPI({})
    sys.modules["utils.api_client"] = api_client_stub

    command_stub = types.ModuleType("utils.command")
    command_stub.bot = bot
    command_stub.is_admin = lambda user_id: user_id == 1
    sys.modules["utils.command"] = command_stub

    language_stub = types.ModuleType("utils.language")
    language_stub.get_user_language = lambda user_id: "en"
    sys.modules["utils.language"] = language_stub

    translations_stub = types.ModuleType("utils.translations")
    translations_stub.get_message_text = lambda language, key: (
        "{account_type}|{customer_name}|{username}|{grace_hours}|{state_summary}"
        if key == "expired_cleanup_reseller_notice"
        else "{account_type}|{username}|{grace_hours}|{state_summary}"
    )
    translations_stub.get_button_text = lambda language, key: "Renew Plan" if key == "renew_plan" else key
    sys.modules["utils.translations"] = translations_stub

    edit_plans_stub = types.ModuleType("utils.edit_plans")
    edit_plans_stub.load_plans = lambda: {}
    sys.modules["utils.edit_plans"] = edit_plans_stub

    renewal_stub = types.ModuleType("utils.renewal")
    renewal_stub.find_customer_renewal_offer = lambda *args, **kwargs: {"eligible": False}
    renewal_stub.find_reseller_renewal_offer = lambda *args, **kwargs: {"eligible": False}
    renewal_stub.find_customer_reservation = lambda user_id, username, server_id=None, payments=None: next((
        {"payment_id": payment_id, **record}
        for payment_id, record in (payments or {}).items()
        if isinstance(record, dict)
        and str(record.get("user_id")) == str(user_id)
        and str(record.get("renewal_username") or record.get("username") or "").lower() == str(username).lower()
        and record.get("renewal_mode") == "reserved"
        and record.get("renewal_status") in {"reserved", "processing", "attention"}
    ), None)
    renewal_stub.find_reseller_reservation = lambda config: next((
        record
        for record in (config or {}).get("renewals", [])
        if isinstance(record, dict)
        and record.get("renewal_mode") == "reserved"
        and record.get("renewal_status") in {"reserved", "processing", "attention"}
    ), None)
    sys.modules["utils.renewal"] = renewal_stub

    growth_events_stub = types.ModuleType("utils.growth_events")
    growth_events_stub.EVENT_RENEWAL_PROMPTED = "renewal_prompted"
    growth_events_stub.record_growth_event = lambda *args, **kwargs: None
    sys.modules["utils.growth_events"] = growth_events_stub

    spec = importlib.util.spec_from_file_location("expired_cleanup_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._test_bot = bot
    return module


class ExpiredCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)
        self.cleanup = load_module()
        self.cleanup.TEST_CONFIGS_FILE = str(self.base / "test_configs.json")
        self.cleanup.PAYMENTS_FILE = str(self.base / "payments.json")
        self.cleanup.RESELLERS_FILE = str(self.base / "resellers.json")
        self.cleanup.STATE_FILE = str(self.base / "expired_user_cleanup.json")
        self.cleanup.SCHEDULE_FILE = str(self.base / "expired_cleanup_schedule.json")
        self.now = datetime(2026, 6, 9, 12, 0, 0)

    def write_json(self, path, data):
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def write_default_files(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})

    def callback_data_from_markup(self, markup):
        callbacks = []
        for row in getattr(markup, "keyboard", []):
            for button in row:
                callback_data = getattr(button, "callback_data", None)
                if callback_data is None and hasattr(button, "to_dict"):
                    callback_data = button.to_dict().get("callback_data")
                if callback_data is None:
                    callback_data = getattr(button, "kwargs", {}).get("callback_data")
                callbacks.append(callback_data)
        for button in getattr(markup, "buttons", []):
            callback_data = getattr(button, "callback_data", None)
            if callback_data is None and hasattr(button, "to_dict"):
                callback_data = button.to_dict().get("callback_data")
            if callback_data is None:
                callback_data = getattr(button, "kwargs", {}).get("callback_data")
            callbacks.append(callback_data)
        return callbacks

    def expired_user(self):
        return {
            "blocked": True,
            "expiration_days": 30,
            "account_creation_date": "2026-05-01T00:00:00+00:00",
            "upload_bytes": self.cleanup.GB_BYTES,
            "download_bytes": 2 * self.cleanup.GB_BYTES,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
            "status": "expired",
        }

    def recovered_test_user(self, created="2026-05-01", note_time="2026-05-01 12:34"):
        return {
            "blocked": True,
            "expiration_days": 30,
            "account_creation_date": created,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": self.cleanup.GB_BYTES,
            "status": "Offline",
            "note": f"📅 {note_time} | 📝 test_config | ✏️ ",
        }

    def stale_on_hold_test_user(self, note_time="2026-04-01 12:00", status="On-hold"):
        return {
            "blocked": False,
            "expiration_days": 30,
            "account_creation_date": None,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": self.cleanup.GB_BYTES,
            "status": status,
            "note": f"📅 {note_time} | 📝 test_config | ✏️ ",
        }

    def test_customer_cleanup_notice_includes_renewal_button_when_eligible(self):
        edit_plans_stub = types.ModuleType("utils.edit_plans")
        edit_plans_stub.load_plans = lambda: {"5": {"price": 10.0, "days": 30, "unlimited": False}}
        sys.modules["utils.edit_plans"] = edit_plans_stub

        renewal_stub = types.ModuleType("utils.renewal")
        renewal_stub.find_customer_renewal_offer = lambda *args, **kwargs: {"eligible": True, "token": "renew-token"}
        renewal_stub.find_reseller_renewal_offer = lambda *args, **kwargs: {"eligible": False}
        sys.modules["utils.renewal"] = renewal_stub

        error = self.cleanup._notify_candidate(
            {
                "source": "customer",
                "telegram_user_id": "1988",
                "username": "alice",
                "server_id": "s1",
                "_api_client": object(),
                "_user_data": self.expired_user(),
            },
            grace_hours=24,
            last_state=self.expired_user(),
        )

        self.assertIsNone(error)
        chat_id, _message, kwargs = self.cleanup._test_bot.sent_messages[-1]
        self.assertEqual(chat_id, 1988)
        callbacks = self.callback_data_from_markup(kwargs["reply_markup"])
        self.assertEqual(callbacks, ["renew_plan:renew-token"])

    def test_customer_cleanup_notice_records_one_expiry_recovery_prompt(self):
        events = []
        growth_events_stub = sys.modules["utils.growth_events"]
        growth_events_stub.record_growth_event = (
            lambda *args, **kwargs: events.append((args, kwargs))
        )
        last_state = {
            **self.expired_user(),
            "account_creation_date": "2026-05-01 12:00:00",
        }

        error = self.cleanup._notify_candidate(
            {
                "source": "customer",
                "telegram_user_id": "1988",
                "username": "alice",
                "server_id": "s1",
                "_api_client": object(),
                "_user_data": self.expired_user(),
            },
            grace_hours=24,
            last_state=last_state,
        )

        self.assertIsNone(error)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], ("renewal_prompted",))
        self.assertEqual(events[0][1]["user_id"], 1988)
        self.assertIn(
            "expiry-recovery:s1:alice:2026-05-01 12:00:00",
            events[0][1]["deduplication_key"],
        )
        self.assertEqual(events[0][1]["metadata"]["basis"], "expired")

    def test_reseller_cleanup_notice_includes_customer_name_and_renewal_button_when_eligible(self):
        edit_plans_stub = types.ModuleType("utils.edit_plans")
        edit_plans_stub.load_plans = lambda: {"5": {"price": 10.0, "days": 30, "unlimited": False}}
        sys.modules["utils.edit_plans"] = edit_plans_stub

        renewal_stub = types.ModuleType("utils.renewal")
        renewal_stub.find_customer_renewal_offer = lambda *args, **kwargs: {"eligible": False}
        renewal_stub.find_reseller_renewal_offer = lambda *args, **kwargs: {
            "eligible": True,
            "token": "renew-token",
        }
        sys.modules["utils.renewal"] = renewal_stub

        error = self.cleanup._notify_candidate(
            {
                "source": "reseller_customer",
                "reseller_id": "303",
                "username": "r303",
                "customer_name": "ali123",
                "_record_ref": ("reseller", "303", 0),
                "_api_client": object(),
                "_user_data": self.expired_user(),
            },
            grace_hours=24,
            last_state=self.expired_user(),
        )

        self.assertIsNone(error)
        chat_id, message, kwargs = self.cleanup._test_bot.sent_messages[-1]
        self.assertEqual(chat_id, 303)
        self.assertIn("|ali123|r303|", message)
        callbacks = self.callback_data_from_markup(kwargs["reply_markup"])
        self.assertEqual(callbacks, ["reseller:renew:renew-token"])

    def test_user_must_be_blocked_to_be_expired_by_days_or_traffic(self):
        unblocked_expired_days = {
            "blocked": False,
            "expiration_days": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        unblocked_exhausted_traffic = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 2 * self.cleanup.GB_BYTES,
            "download_bytes": 3 * self.cleanup.GB_BYTES,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        blocked_active_user = {
            "blocked": True,
            "expiration_days": 30,
            "upload_bytes": self.cleanup.GB_BYTES,
            "download_bytes": self.cleanup.GB_BYTES,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        blocked_unlimited_duration = {
            "blocked": True,
            "expiration_days": 0,
            "upload_bytes": self.cleanup.GB_BYTES,
            "download_bytes": self.cleanup.GB_BYTES,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }

        self.assertFalse(self.cleanup.is_user_expired(unblocked_expired_days))
        self.assertFalse(self.cleanup.is_user_expired(unblocked_exhausted_traffic))
        self.assertFalse(self.cleanup.is_user_expired(blocked_active_user))
        self.assertFalse(self.cleanup.is_user_expired(blocked_unlimited_duration))

        blocked_unlimited_duration["download_bytes"] = 4 * self.cleanup.GB_BYTES
        self.assertTrue(self.cleanup.is_user_expired(blocked_unlimited_duration))
        summary = self.cleanup._format_state_summary(
            {
                "status": "expired",
                "configured_days": 0,
                "days_remaining": None,
                "gb_used": 5,
                "gb_limit": 5,
            },
            "en",
        )
        self.assertIn("Days remaining: Unlimited", summary)

    def test_stale_on_hold_test_requires_more_than_sixty_full_days(self):
        exactly_sixty_days = self.stale_on_hold_test_user(note_time="2026-04-10 12:00:00")
        older_than_sixty_days = self.stale_on_hold_test_user(note_time="2026-04-10 11:59:59")

        self.assertFalse(
            self.cleanup.is_stale_on_hold_test("t12345", exactly_sixty_days, now=self.now)
        )
        self.assertTrue(
            self.cleanup.is_stale_on_hold_test("t12345", older_than_sixty_days, now=self.now)
        )

    def test_stale_on_hold_test_normalizes_panel_status_spelling(self):
        for status in ("On-hold", "On Hold", "on_hold"):
            with self.subTest(status=status):
                self.assertTrue(
                    self.cleanup.is_stale_on_hold_test(
                        "t12345",
                        self.stale_on_hold_test_user(status=status),
                        now=self.now,
                    )
                )

    def test_stale_on_hold_test_accepts_explicit_null_traffic_with_zero_online_count(self):
        cases = {
            "both_null": {
                "upload_bytes": None,
                "download_bytes": None,
                "online_count": 0,
            },
            "upload_null": {
                "upload_bytes": None,
                "download_bytes": 0,
                "online_count": "0",
            },
            "download_null": {
                "upload_bytes": "0",
                "download_bytes": None,
                "online_count": 0,
            },
            "numeric_strings": {
                "upload_bytes": "0",
                "download_bytes": "0",
                "online_count": "0",
            },
        }

        for label, traffic in cases.items():
            with self.subTest(case=label):
                user = {**self.stale_on_hold_test_user(), **traffic}
                self.assertTrue(
                    self.cleanup.is_stale_on_hold_test("t12345", user, now=self.now)
                )

    def test_stale_on_hold_test_rejects_unsafe_null_or_online_evidence(self):
        null_traffic = {
            **self.stale_on_hold_test_user(),
            "upload_bytes": None,
            "download_bytes": None,
        }
        missing_upload = dict(null_traffic)
        missing_upload.pop("upload_bytes")
        missing_download = dict(null_traffic)
        missing_download.pop("download_bytes")
        cases = {
            "null_without_online_count": null_traffic,
            "missing_upload": {**missing_upload, "online_count": 0},
            "missing_download": {**missing_download, "online_count": 0},
            "null_online_count": {**null_traffic, "online_count": None},
            "boolean_online_count": {**null_traffic, "online_count": False},
            "negative_online_count": {**null_traffic, "online_count": -1},
            "malformed_online_count": {**null_traffic, "online_count": "none"},
            "nonzero_online_count": {**null_traffic, "online_count": 1},
            "zero_traffic_but_online": {
                **self.stale_on_hold_test_user(),
                "online_count": 1,
            },
            "boolean_traffic": {
                **self.stale_on_hold_test_user(),
                "upload_bytes": False,
                "online_count": 0,
            },
            "negative_traffic": {
                **self.stale_on_hold_test_user(),
                "download_bytes": -1,
                "online_count": 0,
            },
            "malformed_traffic": {
                **self.stale_on_hold_test_user(),
                "download_bytes": "none",
                "online_count": 0,
            },
            "used_traffic": {
                **self.stale_on_hold_test_user(),
                "download_bytes": 1,
                "online_count": 0,
            },
        }

        for label, user in cases.items():
            with self.subTest(case=label):
                self.assertFalse(
                    self.cleanup.is_stale_on_hold_test("t12345", user, now=self.now)
                )

    def test_stale_on_hold_test_fails_closed_when_any_signature_is_missing(self):
        missing_blocked = self.stale_on_hold_test_user()
        missing_blocked.pop("blocked")
        missing_upload = self.stale_on_hold_test_user()
        missing_upload.pop("upload_bytes")
        missing_download = self.stale_on_hold_test_user()
        missing_download.pop("download_bytes")
        cases = {
            "blocked": ("t12345", {**self.stale_on_hold_test_user(), "blocked": True}),
            "missing_blocked": ("t12345", missing_blocked),
            "used": ("t12345", {**self.stale_on_hold_test_user(), "download_bytes": 1}),
            "missing_upload": ("t12345", missing_upload),
            "missing_download": ("t12345", missing_download),
            "wrong_limit": ("t12345", {**self.stale_on_hold_test_user(), "max_download_bytes": 2 * self.cleanup.GB_BYTES}),
            "wrong_duration": ("t12345", {**self.stale_on_hold_test_user(), "expiration_days": 7}),
            "wrong_status": ("t12345", {**self.stale_on_hold_test_user(), "status": "Offline"}),
            "missing_note_marker": ("t12345", {**self.stale_on_hold_test_user(), "note": "📅 2026-04-01 12:00 | 📝 other | ✏️ "}),
            "missing_note_time": ("t12345", {**self.stale_on_hold_test_user(), "note": "📝 test_config | ✏️ "}),
            "malformed_note_time": ("t12345", {**self.stale_on_hold_test_user(), "note": "📅 not-a-date | 📝 test_config | ✏️ "}),
            "wrong_username": ("trial12345", self.stale_on_hold_test_user()),
        }

        for label, (username, user_data) in cases.items():
            with self.subTest(case=label):
                self.assertFalse(
                    self.cleanup.is_stale_on_hold_test(username, user_data, now=self.now)
                )

    def test_time_expiry_uses_creation_date_plus_plan_duration(self):
        expired_by_date = {
            "blocked": True,
            "expiration_days": 30,
            "account_creation_date": "2026-05-01",
            "upload_bytes": self.cleanup.GB_BYTES,
            "download_bytes": self.cleanup.GB_BYTES,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        manually_blocked_but_not_due = {
            **expired_by_date,
            "account_creation_date": "2026-06-01",
        }

        self.assertTrue(self.cleanup.is_user_expired(expired_by_date, now=self.now))
        self.assertFalse(self.cleanup.is_user_expired(manually_blocked_but_not_due, now=self.now))
        self.assertLessEqual(
            self.cleanup.capture_last_state(expired_by_date, now=self.now)["days_remaining"],
            0,
        )

    def test_stale_on_hold_test_is_backfilled_warned_and_labeled(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.stale_on_hold_test_user()})
        requested_message_keys = []
        original_get_message_text = self.cleanup.get_message_text
        self.cleanup.get_message_text = lambda language, key: (
            requested_message_keys.append(key) or original_get_message_text(language, key)
        )
        self.addCleanup(setattr, self.cleanup, "get_message_text", original_get_message_text)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        configs = self.read_json(self.cleanup.TEST_CONFIGS_FILE)
        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        records = self.cleanup.get_expired_cleanup_records(filter_key="pending", now=self.now)
        self.assertEqual(configs["12345"]["historical_configs"][0]["username"], "t12345")
        self.assertEqual(state["cleanup_status"], "notified")
        self.assertEqual(state["cleanup_reason"], "stale_on_hold_test")
        self.assertEqual(state["delete_after"], "2026-06-11T12:00:00.000000Z")
        self.assertIn("stale_test_cleanup_notice", requested_message_keys)
        self.assertEqual(records[0]["cleanup_reason"], "stale_on_hold_test")
        self.assertEqual(records[0]["reason_code"], "stale_on_hold_test")

    def test_stale_on_hold_current_test_keeps_stale_reason(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "12345": {
                "telegram_id": 12345,
                "username": "t12345",
                "server_id": "s1",
                "used_at": "2026-04-01 12:00:00",
            },
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t12345": self.stale_on_hold_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["12345"]
        self.assertEqual(state["cleanup_reason"], "stale_on_hold_test")
        self.assertEqual(saved_test["cleanup_status"], "notified")

    def test_stale_on_hold_test_is_deleted_after_grace_when_still_eligible(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.stale_on_hold_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(client.deleted, ["t12345"])
        self.assertEqual(state["cleanup_status"], "deleted")
        self.assertEqual(state["cleanup_reason"], "stale_on_hold_test")

    def test_null_counter_test_waits_silently_then_revalidates_and_deletes(self):
        self.write_default_files()
        user = {
            **self.stale_on_hold_test_user(),
            "upload_bytes": None,
            "download_bytes": None,
            "online_count": 0,
        }
        client = FakeClient("s1", {"t12345": user})

        with (
            mock.patch.object(self.cleanup.CLEANUP_LOGGER, "log") as transition_log,
            mock.patch.object(self.cleanup.CLEANUP_LOGGER, "info") as summary_log,
        ):
            self.cleanup.run_expired_user_cleanup(
                now=self.now,
                multi_api=FakeMultiAPI({"s1": client}),
            )

            state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
            self.assertEqual(state["cleanup_status"], "notified")
            self.assertEqual(state["delete_after"], "2026-06-11T12:00:00.000000Z")
            self.assertEqual(client.deleted, [])
            self.assertTrue(transition_log.called)
            self.assertTrue(summary_log.called)
            summary_args = summary_log.call_args.args
            self.assertIn("null_counter_accepted=%d", summary_args[0])
            self.assertEqual(summary_args[2], 1)
            for call in transition_log.call_args_list:
                self.assertNotIn("t12345", call.args)
                self.assertNotIn("12345", call.args)
                self.assertFalse(any(isinstance(value, dict) for value in call.args))

            transition_log.reset_mock()
            summary_log.reset_mock()
            self.cleanup.run_expired_user_cleanup(
                now=self.now + timedelta(hours=1),
                multi_api=FakeMultiAPI({"s1": client}),
            )
            self.assertFalse(transition_log.called)
            self.assertFalse(summary_log.called)
            self.assertEqual(client.deleted, [])

            self.cleanup.run_expired_user_cleanup(
                now=self.now + timedelta(hours=49),
                multi_api=FakeMultiAPI({"s1": client}),
            )
            self.assertEqual(client.deleted, ["t12345"])
            self.assertEqual(
                self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]["cleanup_status"],
                "deleted",
            )
            self.assertTrue(any(call.args[-1] == "deleted" for call in transition_log.call_args_list))

    def test_null_counter_test_usage_during_grace_cancels_deletion(self):
        self.write_default_files()
        user = {
            **self.stale_on_hold_test_user(),
            "upload_bytes": None,
            "download_bytes": None,
            "online_count": 0,
        }
        client = FakeClient("s1", {"t12345": user})

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )
        client.users["t12345"].update({
            "upload_bytes": 0,
            "download_bytes": 1,
        })
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})

    def test_stale_on_hold_test_connection_during_grace_cancels_deletion(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.stale_on_hold_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        client.users["t12345"].update({
            "status": "Offline",
            "account_creation_date": "2026-06-10 10:00:00",
        })
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})

    def test_unmarked_one_gb_on_hold_account_is_ignored(self):
        self.write_default_files()
        user = self.stale_on_hold_test_user()
        user.update({
            "expiration_days": 7,
            "note": "📅 2026-04-01 12:00 | 📝 manually_created | ✏️ ",
        })
        client = FakeClient("s1", {"trial12345": user})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})

    def test_stale_on_hold_test_due_on_unavailable_server_remains_retryable(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.stale_on_hold_test_user()})
        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        unavailable_client = FakeClient("s1", unavailable=True)
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": unavailable_client}),
        )

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(unavailable_client.deleted, [])
        self.assertEqual(state["cleanup_status"], "server_unavailable")
        self.assertEqual(state["cleanup_reason"], "stale_on_hold_test")

    def test_paid_hold_uses_issuance_deadline_warning_and_grace(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "p1": {
                "status": "completed",
                "user_id": 1988,
                "username": "s1988",
                "server_id": "s1",
                "days": 30,
                "completed_at": "2026-04-01 12:00:00",
            }
        })
        live = {
            "blocked": False,
            "status": "On Hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 20 * self.cleanup.GB_BYTES,
        }
        client = FakeClient("s1", {"s1988": live})

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        state = self.read_json(self.cleanup.STATE_FILE)["s1:s1988"]
        self.assertEqual(state["cleanup_status"], "notified")
        self.assertEqual(state["cleanup_reason"], "issue_deadline_expired")
        self.assertTrue(state["cycle_fingerprint"])
        self.assertEqual(state["last_state"]["panel_state"], "hold")
        self.assertEqual(state["last_state"]["entitlement_state"], "expired")
        exported = self.cleanup.get_expired_cleanup_export_records(
            filter_key="all", now=self.now
        )[0]
        self.assertEqual(exported["panel_state"], "hold")
        self.assertEqual(exported["entitlement_state"], "expired")
        self.assertEqual(exported["normalized_state"], "expired")
        self.assertEqual(exported["cleanup_reason"], "issue_deadline_expired")
        self.assertEqual(client.deleted, [])

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, ["s1988"])
        deleted = self.read_json(self.cleanup.STATE_FILE)["s1:s1988"]
        self.assertEqual(deleted["cleanup_reason"], "issue_deadline_expired")

    def test_connected_paid_account_ignores_expired_issuance_cycle(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "p1": {
                "status": "completed",
                "user_id": 1988,
                "username": "s1988",
                "server_id": "s1",
                "days": 30,
                "completed_at": "2026-04-01 12:00:00",
            }
        })
        live = {
            "blocked": False,
            "status": "Online",
            "account_creation_date": "2026-05-20 12:00:00",
            "expiration_days": 60,
            "upload_bytes": 0,
            "download_bytes": self.cleanup.GB_BYTES,
            "max_download_bytes": 20 * self.cleanup.GB_BYTES,
        }
        client = FakeClient("s1", {"s1988": live})

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_connecting_cancels_pending_issuance_deadline_cleanup(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "p1": {
                "status": "completed",
                "user_id": 1988,
                "username": "s1988",
                "server_id": "s1",
                "days": 30,
                "completed_at": "2026-04-01 12:00:00",
            }
        })
        live = {
            "blocked": False,
            "status": "On Hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 20 * self.cleanup.GB_BYTES,
        }
        client = FakeClient("s1", {"s1988": live})
        api = FakeMultiAPI({"s1": client})
        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=api)

        live.update({
            "status": "Offline",
            "account_creation_date": "2026-06-09 13:00:00",
            "download_bytes": 1,
        })
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=api,
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        saved = self.read_json(self.cleanup.PAYMENTS_FILE)["p1"]
        self.assertEqual(saved["cleanup_status"], "renewed")

    def test_reused_reseller_username_uses_newest_local_record(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {
                "configs": [
                    {
                        "username": "r303",
                        "server_id": "s1",
                        "days": 7,
                        "created_at": "2026-04-01 12:00:00",
                    },
                    {
                        "username": "r303",
                        "server_id": "s1",
                        "days": 60,
                        "created_at": "2026-06-01 12:00:00",
                    },
                ]
            }
        })
        live = {
            "blocked": False,
            "status": "On-hold",
            "account_creation_date": None,
            "expiration_days": 60,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 100 * self.cleanup.GB_BYTES,
        }
        client = FakeClient("s1", {"r303": live})

        candidates = self.cleanup.discover_cleanup_candidates()
        self.assertEqual(candidates[0]["_record_ref"], ("reseller", "303", 1))

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_new_successful_cycle_cancels_old_issue_deadline_cleanup(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        payments = {
            "p1": {
                "status": "completed",
                "user_id": 1988,
                "username": "s1988",
                "server_id": "s1",
                "days": 30,
                "completed_at": "2026-04-01 12:00:00",
            }
        }
        self.write_json(self.cleanup.PAYMENTS_FILE, payments)
        live = {
            "blocked": False,
            "status": "On-hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 20 * self.cleanup.GB_BYTES,
        }
        client = FakeClient("s1", {"s1988": live})
        api = FakeMultiAPI({"s1": client})
        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=api)

        payments["p2"] = {
            "status": "completed",
            "user_id": 1988,
            "username": "s1988",
            "server_id": "s1",
            "days": 30,
            "renewal_applied_at": "2026-06-10 12:00:00",
        }
        self.write_json(self.cleanup.PAYMENTS_FILE, payments)
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=api,
        )

        self.assertEqual(client.deleted, [])
        self.assertNotIn("s1:s1988", self.read_json(self.cleanup.STATE_FILE))

    def test_superseded_test_is_deleted_after_grace_even_if_connected(self):
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "12345": {
                "telegram_id": 12345,
                "username": "t12345a",
                "server_id": "s1",
                "used_at": "2026-06-09 11:00:00",
                "historical_configs": [{
                    "username": "t12345",
                    "server_id": "s1",
                    "used_at": "2026-05-01 12:00:00",
                    "superseded_at": "2026-06-09 11:00:00",
                    "cleanup_reason": "superseded_on_hold_test",
                }],
            }
        })
        connected_old_test = {
            **self.stale_on_hold_test_user(note_time="2026-05-01 12:00:00"),
            "status": "Offline",
            "account_creation_date": "2026-06-09 11:30:00",
            "download_bytes": 100,
        }
        client = FakeClient("s1", {"t12345": connected_old_test})
        api = FakeMultiAPI({"s1": client})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=api)
        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_reason"], "superseded_on_hold_test")
        self.assertEqual(state["cleanup_status"], "notified")

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=api,
        )
        self.assertEqual(client.deleted, ["t12345"])

    def test_verified_orphan_test_is_backfilled_notified_and_given_full_grace(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        configs = self.read_json(self.cleanup.TEST_CONFIGS_FILE)
        recovered = configs["12345"]
        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertNotIn("username", recovered)
        self.assertEqual(recovered["telegram_id"], 12345)
        self.assertEqual(recovered["used_at"], "2026-05-01T12:34:00.000000Z")
        self.assertEqual(recovered["historical_configs"][0]["username"], "t12345")
        self.assertEqual(recovered["historical_configs"][0]["server_id"], "s1")
        self.assertEqual(state["source"], "test")
        self.assertEqual(state["cleanup_status"], "notified")
        self.assertEqual(state["telegram_user_id"], "12345")
        self.assertEqual(state["delete_after"], "2026-06-11T12:00:00.000000Z")
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)

    def test_recovered_history_preserves_newer_current_test(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "12345": {
                "telegram_id": 12345,
                "username": "t12345a",
                "server_id": "s2",
                "used_at": "2026-06-08 10:00:00",
            }
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        recovered = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["12345"]
        self.assertEqual(recovered["username"], "t12345a")
        self.assertEqual(recovered["server_id"], "s2")
        self.assertEqual(recovered["used_at"], "2026-06-08 10:00:00")
        self.assertEqual(recovered["historical_configs"][0]["username"], "t12345")

    def test_unverified_orphan_remains_manual_and_is_not_backfilled(self):
        self.write_default_files()
        user = self.recovered_test_user()
        user["note"] = "manually created"
        client = FakeClient("s1", {"t12345": user})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(self.read_json(self.cleanup.TEST_CONFIGS_FILE), {})
        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "manual_review")
        self.assertEqual(state["manual_review_reason"], "unowned_server_user")
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_verified_orphan_waits_for_all_servers_then_enters_automatic_recovery(self):
        self.write_default_files()
        clients = {
            "s1": FakeClient("s1", {"t12345": self.recovered_test_user()}),
            "s2": FakeClient("s2", unavailable=True),
        }

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI(clients))

        self.assertEqual(self.read_json(self.cleanup.TEST_CONFIGS_FILE), {})
        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "manual_review")
        self.assertEqual(state["manual_review_reason"], "unowned_server_user")
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=1),
            multi_api=FakeMultiAPI({"s1": clients["s1"]}),
        )

        recovered = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(recovered["cleanup_status"], "notified")
        self.assertEqual(recovered["recovery_source"], "verified_orphan_test")
        self.assertNotIn("manual_review_reason", recovered)
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)

    def test_recovered_test_notification_failure_stays_pending_until_retry_succeeds(self):
        self.write_default_files()
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = lambda *args, **kwargs: "telegram blocked"
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        failed = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(failed["cleanup_status"], "notification_pending")
        self.assertEqual(failed["cleanup_error"], "notification_failed")
        self.assertNotIn("delete_after", failed)

        self.cleanup._notify_candidate = lambda *args, **kwargs: None
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=1),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        notified = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(notified["cleanup_status"], "notified")
        self.assertEqual(notified["delete_after"], "2026-06-11T13:00:00.000000Z")
        self.assertEqual(notified["recovery_attempts"], 2)

    def test_legacy_recovered_manual_state_is_adopted_without_resetting_metadata(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "manual_review",
                "cleanup_error": "notification_failed",
                "notification_error": "latest blocked",
                "recovery_first_notification_error": "first blocked",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 2,
                "first_seen_at": "2026-06-07 11:00:00",
                "recovery_discovered_at": "2026-06-07 10:59:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = lambda *args, **kwargs: None
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "notified")
        self.assertEqual(state["recovery_attempts"], 3)
        self.assertEqual(state["first_seen_at"], "2026-06-07 11:00:00")
        self.assertEqual(state["recovery_discovered_at"], "2026-06-07 10:59:00")
        self.assertEqual(state["recovery_first_notification_error"], "first blocked")
        self.assertIsNone(state["notification_error"])

    def test_legacy_recovered_manual_retry_logs_automatic_pending_transition(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "manual_review",
                "cleanup_error": "notification_failed",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 1,
                "first_seen_at": "2026-06-09 12:00:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = lambda *args, **kwargs: "temporary failure"
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        with mock.patch.object(self.cleanup.CLEANUP_LOGGER, "log") as transition_log:
            self.cleanup.run_expired_user_cleanup(
                now=self.now,
                multi_api=FakeMultiAPI({"s1": client}),
            )

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "notification_pending")
        self.assertEqual(state["recovery_attempts"], 2)
        self.assertTrue(any(
            call.args[-2:] == ("manual_review", "notification_pending")
            for call in transition_log.call_args_list
        ))

    def test_recovered_permanent_telegram_failure_waits_for_retry_threshold(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "manual_review",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 1,
                "first_seen_at": "2026-06-06 12:00:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = (
            lambda *args, **kwargs:
            "A request to the Telegram API was unsuccessful. Error code: 403. "
            "Description: Forbidden: bot was blocked by the user"
        )
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "notification_pending")
        self.assertEqual(state["cleanup_error"], "notification_failed")
        self.assertEqual(state["recovery_attempts"], 2)
        self.assertNotIn("delete_after", state)
        self.assertEqual(client.deleted, [])

    def test_recovered_permanent_telegram_failure_waits_for_unreachable_age(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "manual_review",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 2,
                "first_seen_at": "2026-06-08 13:00:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = (
            lambda *args, **kwargs:
            "A request to the Telegram API was unsuccessful. Error code: 403. "
            "Description: Forbidden: user is deactivated"
        )
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "notification_pending")
        self.assertEqual(state["cleanup_error"], "notification_failed")
        self.assertEqual(state["recovery_attempts"], 3)
        self.assertNotIn("delete_after", state)
        self.assertEqual(client.deleted, [])

    def test_recovered_permanent_telegram_failure_enters_deletion_queue_then_deletes_next_scan(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 2,
                "first_seen_at": "2026-06-07 11:00:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = (
            lambda *args, **kwargs:
            "A request to the Telegram API was unsuccessful. Error code: 403. "
            "Description: Forbidden: bot was blocked by the user"
        )
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        queued = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(queued["source"], "test")
        self.assertEqual(queued["cleanup_status"], "notification_unreachable")
        self.assertEqual(queued["cleanup_error"], "notification_unreachable")
        self.assertEqual(queued["delete_after"], "2026-06-09T12:00:00.000000Z")
        self.assertEqual(queued["recovery_unreachable_queued_at"], "2026-06-09T12:00:00.000000Z")
        self.assertEqual(queued["recovery_attempts"], 3)
        self.assertEqual(client.deleted, [])
        due_records = self.cleanup.get_expired_cleanup_records(filter_key="due", now=self.now)
        self.assertEqual([record["username"] for record in due_records], ["t12345"])
        self.assertEqual(due_records[0]["reason_code"], "notification_unreachable")

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(minutes=1),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        deleted = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(client.deleted, ["t12345"])
        self.assertEqual(deleted["cleanup_status"], "deleted")
        self.assertEqual(deleted["delete_result"], "deleted")
        self.assertEqual(deleted["notification_error"], queued["notification_error"])
        self.assertEqual(deleted["recovery_unreachable_queued_at"], "2026-06-09T12:00:00.000000Z")

    def test_recovered_temporary_notification_failure_never_enters_deletion_queue(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "manual_review",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 9,
                "first_seen_at": "2026-06-01 12:00:00",
            },
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = lambda *args, **kwargs: "Too Many Requests: retry after 10"
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "notification_pending")
        self.assertEqual(state["cleanup_error"], "notification_failed")
        self.assertEqual(state["recovery_attempts"], 10)
        self.assertNotIn("delete_after", state)
        self.assertEqual(client.deleted, [])

    def test_recovered_unreachable_queued_user_is_removed_from_queue_if_renewed(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "notification_unreachable",
                "cleanup_error": "notification_unreachable",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 3,
                "recovery_unreachable_queued_at": "2026-06-09 10:00:00",
                "delete_after": "2026-06-09 10:00:00",
                "last_state": self.expired_user(),
            },
        })
        active_user = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": self.cleanup.GB_BYTES,
            "status": "active",
        }
        client = FakeClient("s1", {"t12345": active_user})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(client.deleted, [])

    def test_notification_pending_resumes_after_server_outage(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "notification_pending",
                "cleanup_error": "notification_failed",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 1,
                "first_seen_at": "2026-06-08 12:00:00",
            },
        })

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": FakeClient("s1", unavailable=True)}),
        )
        unavailable = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(unavailable["cleanup_status"], "server_unavailable")
        self.assertEqual(unavailable["cleanup_error"], "server_unavailable")

        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=1),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        recovered = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(recovered["cleanup_status"], "notified")
        self.assertEqual(recovered["recovery_attempts"], 2)
        self.assertEqual(recovered["first_seen_at"], "2026-06-08 12:00:00")

    def test_unreachable_deletion_survives_server_outage_without_renotifying(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "notification_unreachable",
                "cleanup_error": "notification_unreachable",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 3,
                "recovery_unreachable_queued_at": "2026-06-09 10:00:00",
                "delete_after": "2026-06-09 10:00:00",
            },
        })

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": FakeClient("s1", unavailable=True)}),
        )
        unavailable = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(unavailable["cleanup_status"], "server_unavailable")
        self.assertEqual(unavailable["delete_after"], "2026-06-09 10:00:00")

        client = FakeClient("s1", {"t12345": self.recovered_test_user()})
        original_notify = self.cleanup._notify_candidate
        self.cleanup._notify_candidate = mock.Mock(return_value=None)
        self.addCleanup(setattr, self.cleanup, "_notify_candidate", original_notify)
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=1),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        deleted = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(deleted["cleanup_status"], "deleted")
        self.assertEqual(client.deleted, ["t12345"])
        self.cleanup._notify_candidate.assert_not_called()

    def test_admin_kept_verified_orphan_remains_manual(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "review_status": "kept",
                "reviewed_by": "1",
            }
        })
        client = FakeClient("s1", {"t12345": self.recovered_test_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)["s1:t12345"]
        self.assertEqual(state["cleanup_status"], "manual_review")
        self.assertEqual(state["review_status"], "kept")
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_recovered_test_batch_is_limited_to_twenty_five_recipients(self):
        self.write_default_files()
        users = {
            f"t{10000 + index}": self.recovered_test_user()
            for index in range(30)
        }
        client = FakeClient("s1", users)

        with mock.patch.object(self.cleanup.CLEANUP_LOGGER, "log") as transition_log:
            self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        statuses = [entry["cleanup_status"] for entry in state.values()]
        self.assertEqual(statuses.count("notified"), 25)
        self.assertEqual(statuses.count("notification_pending"), 5)
        self.assertEqual(statuses.count("manual_review"), 0)
        self.assertTrue(any(
            call.args[-1] == "notification_pending"
            for call in transition_log.call_args_list
        ))
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 25)
        self.assertEqual(len(self.read_json(self.cleanup.TEST_CONFIGS_FILE)), 30)

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=1),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        second_state = self.read_json(self.cleanup.STATE_FILE)
        second_statuses = [entry["cleanup_status"] for entry in second_state.values()]
        self.assertEqual(second_statuses.count("notified"), 30)
        self.assertEqual(second_statuses.count("notification_pending"), 0)
        self.assertEqual(second_statuses.count("manual_review"), 0)
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 30)
        histories = sum(
            len(entry.get("historical_configs", []))
            for entry in self.read_json(self.cleanup.TEST_CONFIGS_FILE).values()
        )
        self.assertEqual(histories, 30)

    def test_recovered_batch_notifies_same_telegram_recipient_once_per_scan(self):
        self.write_default_files()
        client = FakeClient("s1", {
            "t12345": self.recovered_test_user(),
            "t12345a": self.recovered_test_user(created="2026-05-02", note_time="2026-05-02 12:34"),
        })

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        statuses = [entry["cleanup_status"] for entry in state.values()]
        self.assertEqual(statuses.count("notified"), 1)
        self.assertEqual(statuses.count("notification_pending"), 1)
        self.assertEqual(statuses.count("manual_review"), 0)
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        recovered = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["12345"]
        self.assertEqual(len(recovered["historical_configs"]), 2)

    def test_legacy_record_without_server_id_is_persisted_to_unique_live_server(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s2", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s2": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertIn("s2:t101", state)
        self.assertNotIn("primary:t101", state)
        self.assertEqual(state["s2:t101"]["cleanup_status"], "notified")
        self.assertEqual(saved_test["server_id"], "s2")
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)

    def test_legacy_primary_pending_state_moves_to_unique_live_server(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {
                "telegram_id": 101,
                "username": "t101",
                "cleanup_status": "notified",
            }
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.STATE_FILE, {
            "primary:t101": {
                "username": "t101",
                "server_id": "primary",
                "source": "test",
                "telegram_user_id": "101",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-11 08:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        client = FakeClient("s2", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s2": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertNotIn("primary:t101", state)
        self.assertEqual(state["s2:t101"]["cleanup_status"], "notified")
        self.assertEqual(state["s2:t101"]["notified_at"], "2026-06-09 08:00:00")
        self.assertEqual(saved_test["server_id"], "s2")
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_ambiguous_serverless_record_routes_expired_user_to_manual_review(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "shared"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        active_user = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        clients = {
            "primary": FakeClient("primary", {"shared": active_user}),
            "s2": FakeClient("s2", {"shared": self.expired_user()}),
        }

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI(clients))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(state["s2:shared"]["cleanup_status"], "manual_review")
        self.assertEqual(state["s2:shared"]["source"], "server_user")
        self.assertNotIn("primary:shared", state)
        self.assertNotIn("server_id", saved_test)
        self.assertNotIn("cleanup_status", saved_test)
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_serverless_record_is_not_inferred_while_any_server_is_unavailable(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        clients = {
            "s2": FakeClient("s2", {"t101": self.expired_user()}),
            "s3": FakeClient("s3", unavailable=True),
        }

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI(clients))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(state["s2:t101"]["cleanup_status"], "manual_review")
        self.assertNotIn("server_id", saved_test)
        self.assertNotIn("cleanup_status", saved_test)
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_reappeared_deleted_user_starts_a_fresh_grace_period(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {
                "telegram_id": 101,
                "username": "t101",
                "server_id": "s1",
                "cleanup_status": "deleted",
                "cleanup_deleted_at": "2026-06-01 12:00:00",
                "cleanup_delete_result": "deleted",
            }
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t101": {
                "username": "t101",
                "server_id": "s1",
                "source": "test",
                "telegram_user_id": "101",
                "cleanup_status": "deleted",
                "delete_result": "deleted",
                "deleted_at": "2026-06-01 12:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        pending_state = self.read_json(self.cleanup.STATE_FILE)["s1:t101"]
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(client.deleted, [])
        self.assertEqual(pending_state["cleanup_status"], "notified")
        self.assertEqual(pending_state["delete_after"], "2026-06-11T12:00:00.000000Z")
        self.assertEqual(saved_test["cleanup_status"], "notified")
        self.assertNotIn("cleanup_deleted_at", saved_test)
        self.assertNotIn("cleanup_delete_result", saved_test)
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)

        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertEqual(client.deleted, ["t101"])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE)["s1:t101"]["cleanup_status"], "deleted")

    def test_cleanup_renews_pending_user_when_unblocked_even_if_days_expired(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        client.users["t101"] = {
            "blocked": False,
            "expiration_days": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.get_user_calls, [])
        self.assertEqual(client.get_users_calls, 2)
        self.assertEqual(self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]["cleanup_status"], "renewed")

    def test_candidate_discovery_includes_test_customer_and_reseller_configs(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "pay1": {"status": "completed", "user_id": 202, "username": "s202", "server_id": "s1"},
            "settlement": {"status": "completed", "type": "settlement", "username": "ignored"},
        })
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{
                "username": "r303c184",
                "server_id": "s2",
                "provisioning_source": "external_bulk",
                "financially_excluded": True,
            }]}
        })

        candidates = self.cleanup.discover_cleanup_candidates()

        self.assertEqual(
            {(candidate["source"], candidate["username"]) for candidate in candidates},
            {("test", "t101"), ("customer", "s202"), ("reseller_customer", "r303c184")},
        )

    def test_reseller_cleanup_metadata_save_preserves_new_configs_added_during_scan(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {
                "status": "approved",
                "debt": 1.0,
                "configs": [
                    {"username": "r303", "server_id": "s1", "price": 1.0}
                ],
            }
        })
        client = FakeClient("s1", {"r303": self.expired_user()})

        def append_new_config(*_args, **_kwargs):
            resellers = self.read_json(self.cleanup.RESELLERS_FILE)
            resellers["303"]["configs"].append({
                "username": "r303a",
                "server_id": "s1",
                "price": 1.0,
            })
            self.write_json(self.cleanup.RESELLERS_FILE, resellers)
            return None

        self.cleanup._notify_candidate = append_new_config

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        saved_configs = self.read_json(self.cleanup.RESELLERS_FILE)["303"]["configs"]
        self.assertEqual([config["username"] for config in saved_configs], ["r303", "r303a"])
        self.assertEqual(saved_configs[0]["cleanup_status"], "notified")
        self.assertNotIn("cleanup_status", saved_configs[1])

    def test_payment_cleanup_metadata_save_preserves_concurrent_payment_updates(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "pay1": {
                "status": "completed",
                "user_id": 202,
                "username": "s202",
                "server_id": "s1",
            }
        })
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"s202": self.expired_user()})

        def add_concurrent_payment_update(*_args, **_kwargs):
            payments = self.read_json(self.cleanup.PAYMENTS_FILE)
            payments["pay1"]["accounting_marker"] = "preserve-me"
            payments["pay2"] = {
                "status": "processing",
                "user_id": 303,
            }
            self.write_json(self.cleanup.PAYMENTS_FILE, payments)
            return None

        self.cleanup._notify_candidate = add_concurrent_payment_update

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        saved = self.read_json(self.cleanup.PAYMENTS_FILE)
        self.assertEqual(saved["pay1"]["accounting_marker"], "preserve-me")
        self.assertEqual(saved["pay1"]["cleanup_status"], "notified")
        self.assertEqual(saved["pay2"]["status"], "processing")
        self.assertNotIn("cleanup_status", saved["pay2"])

    def test_reserved_customer_renewal_is_protected_from_expired_cleanup(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "reserved-payment": {
                "status": "completed",
                "type": "renewal",
                "user_id": 202,
                "username": "s202",
                "server_id": "s1",
                "renewal_username": "s202",
                "renewal_server_id": "s1",
                "renewal_mode": "reserved",
                "renewal_status": "reserved",
            }
        })
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"s202": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(
            now=self.now,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        saved = self.read_json(self.cleanup.PAYMENTS_FILE)["reserved-payment"]
        self.assertEqual(saved["cleanup_status"], "renewal_reserved")
        self.assertEqual(client.deleted, [])
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])

    def test_first_expired_detection_notifies_and_waits_to_delete(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.get_user_calls, [])
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        self.assertEqual(state["s1:t101"]["cleanup_status"], "notified")
        self.assertEqual(saved_test["cleanup_status"], "notified")
        self.assertEqual(saved_test["cleanup_notified_at"], "2026-06-09T12:00:00.000000Z")
        self.assertEqual(saved_test["cleanup_last_state"]["status"], "expired")
        self.assertIn("your test account", self.cleanup._test_bot.sent_messages[0][1])
        self.assertIn("|48|", self.cleanup._test_bot.sent_messages[0][1])
        self.assertIn("Status: expired", self.cleanup._test_bot.sent_messages[0][1])
        self.assertNotIn("Blocked:", self.cleanup._test_bot.sent_messages[0][1])
        self.assertIn("Days remaining: 0", self.cleanup._test_bot.sent_messages[0][1])
        self.assertIn("GB used: 3.0/5.0", self.cleanup._test_bot.sent_messages[0][1])
        self.assertNotIn("GB remaining:", self.cleanup._test_bot.sent_messages[0][1])

    def test_server_only_expired_user_goes_to_manual_review_without_local_record(self):
        self.write_default_files()
        client = FakeClient("s1", {"orphan": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, [])
        self.assertEqual(client.get_users_calls, 1)
        self.assertEqual(client.get_user_calls, [])
        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 0)
        self.assertEqual(state["s1:orphan"]["cleanup_status"], "manual_review")
        self.assertEqual(state["s1:orphan"]["source"], "server_user")
        self.assertEqual(state["s1:orphan"]["manual_review_reason"], "unowned_server_user")
        self.assertNotIn("delete_after", state["s1:orphan"])
        self.assertNotIn("notification_error", state["s1:orphan"])
        self.assertEqual(state["s1:orphan"]["last_state"]["status"], "expired")

    def test_server_only_manual_review_user_does_not_auto_delete_after_grace(self):
        self.write_default_files()
        client = FakeClient("s1", {"orphan": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, [])
        self.assertEqual(state["s1:orphan"]["cleanup_status"], "manual_review")
        self.assertEqual(state["s1:orphan"]["manual_review_reason"], "unowned_server_user")
        self.assertEqual(state["s1:orphan"]["last_state"]["status"], "expired")

    def test_server_only_manual_review_user_is_marked_renewed_when_renewed(self):
        self.write_default_files()
        client = FakeClient("s1", {"orphan": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        client.users["orphan"] = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(state["s1:orphan"]["cleanup_status"], "renewed")
        self.assertEqual(client.deleted, [])

    def test_legacy_server_only_notified_record_is_migrated_to_manual_review(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:orphan": {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "notification_error": "missing_recipient",
                "last_state": {"status": "expired", "days_remaining": 0},
            }
        })
        client = FakeClient("s1", {"orphan": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, [])
        self.assertEqual(state["s1:orphan"]["cleanup_status"], "manual_review")
        self.assertEqual(state["s1:orphan"]["manual_review_reason"], "unowned_server_user")
        self.assertNotIn("delete_after", state["s1:orphan"])
        self.assertNotIn("notification_error", state["s1:orphan"])
        self.assertNotIn("notified_at", state["s1:orphan"])

    def test_renewed_server_only_user_reexpiring_returns_to_manual_review(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:orphan": {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "renewed",
                "last_state": {"status": "active", "days_remaining": 30},
            }
        })
        client = FakeClient("s1", {"orphan": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, [])
        self.assertEqual(state["s1:orphan"]["cleanup_status"], "manual_review")
        self.assertEqual(state["s1:orphan"]["manual_review_reason"], "unowned_server_user")
        self.assertEqual(state["s1:orphan"]["last_state"]["status"], "expired")

    def test_paid_customer_notification_includes_account_type(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "pay1": {"status": "completed", "user_id": 202, "username": "p202", "server_id": "s1"}
        })
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"p202": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        self.assertIn("your paid account", self.cleanup._test_bot.sent_messages[0][1])
        saved_payment = self.read_json(self.cleanup.PAYMENTS_FILE)["pay1"]
        self.assertEqual(saved_payment["cleanup_last_state"]["status"], "expired")

    def test_reseller_customer_notification_includes_account_type(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{"username": "r303", "customer_name": "ali123", "server_id": "s1"}]}
        })
        client = FakeClient("s1", {"r303": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        self.assertEqual(self.cleanup._test_bot.sent_messages[0][0], 303)
        self.assertIn("your customer account", self.cleanup._test_bot.sent_messages[0][1])
        self.assertIn("|ali123|r303|", self.cleanup._test_bot.sent_messages[0][1])
        saved_config = self.read_json(self.cleanup.RESELLERS_FILE)["303"]["configs"][0]
        self.assertEqual(saved_config["cleanup_last_state"]["status"], "expired")

    def test_reseller_customer_notification_recovers_legacy_name_from_note(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{"username": "r303", "server_id": "s1"}]}
        })
        user_data = self.expired_user()
        user_data["note"] = "📅 2026-05-30 07:01 | 📝 sara88 | ✏️ "
        client = FakeClient("s1", {"r303": user_data})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        self.assertIn("|sara88|r303|", self.cleanup._test_bot.sent_messages[0][1])

    def test_reseller_customer_notification_uses_neutral_fallback_for_missing_name(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{"username": "r303", "customer_name": "too-long-name", "server_id": "s1"}]}
        })
        client = FakeClient("s1", {"r303": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(len(self.cleanup._test_bot.sent_messages), 1)
        self.assertIn("|—|r303|", self.cleanup._test_bot.sent_messages[0][1])

    def test_bot_record_missing_from_vpn_is_ignored_without_notification(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "missing101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])
        self.assertEqual(state, {})
        self.assertNotIn("cleanup_status", saved_test)
        self.assertNotIn("cleanup_delete_result", saved_test)
        self.assertNotIn("cleanup_deleted_at", saved_test)
        self.assertNotIn("cleanup_notified_at", saved_test)
        self.assertNotIn("cleanup_last_state", saved_test)

    def test_non_expired_bot_record_does_not_trigger_per_user_vpn_lookup(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "missing101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {
            "pay1": {"status": "completed", "user_id": 202, "username": "missing202", "server_id": "s1"}
        })
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {
            "active": {
                "blocked": False,
                "expiration_days": 30,
                "upload_bytes": 0,
                "download_bytes": 0,
                "max_download_bytes": 5 * self.cleanup.GB_BYTES,
            }
        })

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(client.get_users_calls, 1)
        self.assertEqual(client.get_user_calls, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})

    def test_state_cleanup_uses_bulk_scan_for_renewal_without_per_user_lookup(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t101": {
                "username": "t101",
                "server_id": "s1",
                "source": "test",
                "telegram_user_id": "101",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        client = FakeClient("s1", {
            "t101": {
                "blocked": False,
                "expiration_days": 30,
                "upload_bytes": 0,
                "download_bytes": 0,
                "max_download_bytes": 5 * self.cleanup.GB_BYTES,
            }
        })

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(client.get_users_calls, 1)
        self.assertEqual(client.get_user_calls, [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]["cleanup_status"], "renewed")

    def test_deletes_after_grace_and_saves_last_state(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=25), multi_api=FakeMultiAPI({"s1": client}))

        pending_state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, [])
        self.assertEqual(pending_state["s1:t101"]["cleanup_status"], "notified")
        self.assertEqual(pending_state["s1:t101"]["delete_after"], "2026-06-11T12:00:00.000000Z")

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        last_state = saved_test["cleanup_last_state"]
        self.assertEqual(client.deleted, ["t101"])
        self.assertEqual(saved_test["cleanup_status"], "deleted")
        self.assertEqual(state["s1:t101"]["delete_result"], "deleted")
        self.assertEqual(last_state["days_remaining"], 0)
        self.assertEqual(last_state["gb_limit"], 5.0)
        self.assertEqual(last_state["gb_used"], 3.0)
        self.assertEqual(last_state["gb_remaining"], 2.0)

    def test_existing_24_hour_pending_record_is_extended_to_default_48_hours(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1", "cleanup_status": "notified"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t101": {
                "username": "t101",
                "server_id": "s1",
                "source": "test",
                "telegram_user_id": "101",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=25), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        record = self.cleanup.get_expired_cleanup_records(filter_key="pending", now=self.now + timedelta(hours=25))[0]
        self.assertEqual(client.deleted, [])
        self.assertEqual(state["s1:t101"]["cleanup_status"], "notified")
        self.assertEqual(state["s1:t101"]["delete_after"], "2026-06-11T08:00:00.000000Z")
        self.assertEqual(record["delete_after"], "2026-06-11T08:00:00.000000Z")

    def test_cleanup_batches_local_record_writes_for_multiple_deletions(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1", "cleanup_status": "notified"},
            "102": {"telegram_id": 102, "username": "t102", "server_id": "s1", "cleanup_status": "notified"},
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t101": {
                "username": "t101",
                "server_id": "s1",
                "source": "test",
                "telegram_user_id": "101",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:t102": {
                "username": "t102",
                "server_id": "s1",
                "source": "test",
                "telegram_user_id": "102",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": {"days_remaining": 0},
            },
        })
        client = FakeClient("s1", {"t101": self.expired_user(), "t102": self.expired_user()})
        save_counts = {}
        original_save = self.cleanup._save_json_file
        store_module = sys.modules["utils.test_config_store"]
        original_store_update = store_module.update_test_configs

        def counted_save(path, data):
            save_counts[path] = save_counts.get(path, 0) + 1
            original_save(path, data)

        self.cleanup._save_json_file = counted_save
        self.addCleanup(setattr, self.cleanup, "_save_json_file", original_save)

        def counted_store_update(path, mutator):
            save_counts[path] = save_counts.get(path, 0) + 1
            return original_store_update(path, mutator)

        store_module.update_test_configs = counted_store_update
        self.addCleanup(setattr, store_module, "update_test_configs", original_store_update)

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(client.deleted, ["t101", "t102"])
        self.assertEqual(save_counts.get(self.cleanup.TEST_CONFIGS_FILE), 1)
        self.assertEqual(save_counts.get(self.cleanup.STATE_FILE), 1)
        saved_tests = self.read_json(self.cleanup.TEST_CONFIGS_FILE)
        self.assertEqual(saved_tests["101"]["cleanup_status"], "deleted")
        self.assertEqual(saved_tests["102"]["cleanup_status"], "deleted")

    def test_cleanup_metadata_does_not_overwrite_newer_test_username(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {
                "telegram_id": 101,
                "username": "t101a",
                "server_id": "s2",
                "used_at": "2026-06-09 12:00:00",
            }
        })
        stale = {
            "101": {
                "telegram_id": 101,
                "username": "t101",
                "server_id": "s1",
                "cleanup_status": "deleted",
                "cleanup_deleted_at": "2026-06-09 12:00:00",
            }
        }

        self.cleanup._save_test_cleanup_metadata(stale, {"101"})

        saved = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(saved["username"], "t101a")
        self.assertEqual(saved["server_id"], "s2")
        self.assertNotIn("cleanup_status", saved)

    def test_renewed_user_clears_pending_cleanup_state(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        client.users["t101"] = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * self.cleanup.GB_BYTES,
        }
        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        self.assertEqual(self.read_json(self.cleanup.STATE_FILE), {})
        self.assertEqual(client.deleted, [])
        self.assertEqual(self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]["cleanup_status"], "renewed")

    def test_delete_failure_keeps_retryable_state(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()}, delete_result=None)

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        with mock.patch.object(self.cleanup.CLEANUP_LOGGER, "log") as transition_log:
            self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(client.deleted, ["t101"])
        self.assertEqual(state["s1:t101"]["cleanup_status"], "delete_failed")
        self.assertEqual(saved_test["cleanup_status"], "delete_failed")
        self.assertIn("cleanup_last_state", saved_test)
        self.assertTrue(any(
            call.args[0] == self.cleanup.logging.WARNING
            and call.args[-1] == "delete_failed"
            for call in transition_log.call_args_list
        ))

    def test_unavailable_server_keeps_pending_cleanup_retryable(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {
            "101": {"telegram_id": 101, "username": "t101", "server_id": "s1"}
        })
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {})
        client = FakeClient("s1", {"t101": self.expired_user()})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))
        unavailable_client = FakeClient("s1", unavailable=True)
        self.cleanup.run_expired_user_cleanup(
            now=self.now + timedelta(hours=49),
            multi_api=FakeMultiAPI({"s1": unavailable_client}),
        )

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_test = self.read_json(self.cleanup.TEST_CONFIGS_FILE)["101"]
        self.assertEqual(unavailable_client.deleted, [])
        self.assertEqual(state["s1:t101"]["cleanup_status"], "server_unavailable")
        self.assertEqual(state["s1:t101"]["cleanup_error"], "server_unavailable")
        self.assertEqual(saved_test["cleanup_status"], "notified")

    def test_already_missing_after_grace_is_reported_with_null_last_state(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t101": {
                "username": "t101",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": None,
            }
        })
        client = FakeClient("s1", {})

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        exported = self.cleanup.get_deleted_users_for_json(now=self.now + timedelta(hours=49))
        self.assertEqual(self.cleanup._test_bot.sent_messages, [])
        self.assertEqual(exported[0]["delete_result"], "already_missing")
        self.assertIsNone(exported[0]["last_state"])

    def test_due_cleanup_uses_bulk_scan_when_single_user_lookup_misses_existing_user(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{"username": "r303", "server_id": "s1"}]}
        })
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:r303": {
                "username": "r303",
                "server_id": "s1",
                "source": "reseller_customer",
                "reseller_id": "303",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        traffic_exhausted_user = {
            "blocked": True,
            "expiration_days": 40,
            "upload_bytes": 6 * self.cleanup.GB_BYTES,
            "download_bytes": 4 * self.cleanup.GB_BYTES,
            "max_download_bytes": 10 * self.cleanup.GB_BYTES,
            "status": "Offline",
        }
        client = BulkOnlyFakeClient("s1", {"r303": traffic_exhausted_user})

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_config = self.read_json(self.cleanup.RESELLERS_FILE)["303"]["configs"][0]
        self.assertEqual(client.deleted, ["r303"])
        self.assertEqual(state["s1:r303"]["cleanup_status"], "deleted")
        self.assertEqual(state["s1:r303"]["delete_result"], "deleted")
        self.assertEqual(saved_config["cleanup_status"], "deleted")

    def test_refresh_repairs_already_missing_when_bulk_scan_finds_existing_user(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{
                "username": "r303",
                "server_id": "s1",
                "cleanup_status": "already_missing",
                "cleanup_deleted_at": "2026-06-09 12:00:00",
                "cleanup_delete_result": "already_missing",
            }]}
        })
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:r303": {
                "username": "r303",
                "server_id": "s1",
                "source": "reseller_customer",
                "reseller_id": "303",
                "cleanup_status": "already_missing",
                "delete_result": "already_missing",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 11:00:00",
                "deleted_at": "2026-06-09 12:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        traffic_exhausted_user = {
            "blocked": True,
            "expiration_days": 40,
            "upload_bytes": 6 * self.cleanup.GB_BYTES,
            "download_bytes": 4 * self.cleanup.GB_BYTES,
            "max_download_bytes": 10 * self.cleanup.GB_BYTES,
            "status": "Offline",
        }
        client = BulkOnlyFakeClient("s1", {"r303": traffic_exhausted_user})

        self.cleanup.run_expired_user_cleanup(now=self.now + timedelta(hours=49), multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_config = self.read_json(self.cleanup.RESELLERS_FILE)["303"]["configs"][0]
        self.assertEqual(client.deleted, ["r303"])
        self.assertEqual(state["s1:r303"]["cleanup_status"], "deleted")
        self.assertEqual(state["s1:r303"]["delete_result"], "deleted")
        self.assertEqual(saved_config["cleanup_status"], "deleted")
        self.assertEqual(saved_config["cleanup_delete_result"], "deleted")

    def test_refresh_clears_stale_missing_reason_when_repaired_user_is_still_pending(self):
        self.write_json(self.cleanup.TEST_CONFIGS_FILE, {})
        self.write_json(self.cleanup.PAYMENTS_FILE, {})
        self.write_json(self.cleanup.RESELLERS_FILE, {
            "303": {"configs": [{
                "username": "r303",
                "server_id": "s1",
                "cleanup_status": "already_missing",
                "cleanup_deleted_at": "2026-06-09 12:00:00",
                "cleanup_delete_result": "already_missing",
            }]}
        })
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:r303": {
                "username": "r303",
                "server_id": "s1",
                "source": "reseller_customer",
                "reseller_id": "303",
                "cleanup_status": "already_missing",
                "delete_result": "already_missing",
                "notified_at": "2026-06-09 08:00:00",
                "delete_after": "2026-06-09 14:00:00",
                "deleted_at": "2026-06-09 12:00:00",
                "last_state": {"days_remaining": 0},
            }
        })
        traffic_exhausted_user = {
            "blocked": True,
            "expiration_days": 40,
            "upload_bytes": 6 * self.cleanup.GB_BYTES,
            "download_bytes": 4 * self.cleanup.GB_BYTES,
            "max_download_bytes": 10 * self.cleanup.GB_BYTES,
            "status": "Offline",
        }
        client = BulkOnlyFakeClient("s1", {"r303": traffic_exhausted_user})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        saved_config = self.read_json(self.cleanup.RESELLERS_FILE)["303"]["configs"][0]
        records = self.cleanup.get_expired_cleanup_records(filter_key="pending", now=self.now)
        record = next(item for item in records if item["username"] == "r303")
        self.assertEqual(client.deleted, [])
        self.assertEqual(state["s1:r303"]["cleanup_status"], "notified")
        self.assertNotIn("delete_result", state["s1:r303"])
        self.assertNotIn("deleted_at", state["s1:r303"])
        self.assertNotIn("cleanup_delete_result", saved_config)
        self.assertNotIn("cleanup_deleted_at", saved_config)
        self.assertEqual(record["reason_code"], "traffic_exhausted")

    def test_deleted_users_json_filters_to_past_sixty_days(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:new": {
                "username": "new",
                "server_id": "s1",
                "source": "customer",
                "telegram_user_id": "101",
                "notified_at": "2026-06-08 12:00:00",
                "deleted_at": "2026-06-09 12:00:00",
                "delete_result": "deleted",
                "last_state": {"days_remaining": 0},
            },
            "s1:old": {
                "username": "old",
                "server_id": "s1",
                "source": "customer",
                "telegram_user_id": "202",
                "notified_at": "2026-03-01 12:00:00",
                "deleted_at": "2026-03-02 12:00:00",
                "delete_result": "deleted",
                "last_state": {"days_remaining": 0},
            },
        })

        exported = self.cleanup.get_deleted_users_for_json(days=60, now=self.now)

        self.assertEqual([entry["username"] for entry in exported], ["new"])
        self.assertEqual(exported[0]["reason_code"], "time_expired")
        self.assertIn("reason", exported[0])

    def test_admin_cleanup_counts_split_pending_due_and_history(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:pending": {
                "username": "pending",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "notified_at": "2026-06-09 10:00:00",
                "delete_after": "2026-06-09 14:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:notice-retry": {
                "username": "notice-retry",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "notification_pending",
                "cleanup_error": "notification_failed",
                "cleanup_reason": "stale_on_hold_test",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 2,
                "first_seen_at": "2026-06-08 10:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:due": {
                "username": "due",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "notified_at": "2026-06-07 08:00:00",
                "delete_after": "2026-06-08 08:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:deleted": {
                "username": "deleted",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "deleted",
                "delete_result": "deleted",
                "deleted_at": "2026-06-09 09:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:failed": {
                "username": "failed",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "delete_failed",
                "last_state": {"days_remaining": 0},
            },
            "s1:unavailable": {
                "username": "unavailable",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "server_unavailable",
                "last_state": {"days_remaining": 0},
            },
            "s1:renewed": {
                "username": "renewed",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "renewed",
                "last_state": {"days_remaining": 10},
            },
            "s1:review": {
                "username": "review",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "unowned_server_user",
                "last_state": {"days_remaining": 0},
            },
            "s1:duplicate": {
                "username": "duplicate",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "duplicate_payment",
                "last_state": {"days_remaining": 30},
            },
        })

        counts = self.cleanup.get_expired_cleanup_counts(now=self.now)

        self.assertEqual(counts["manual_review"], 1)
        self.assertEqual(counts["duplicate_payment"], 1)
        self.assertEqual(counts["pending"], 2)
        self.assertEqual(counts["due"], 1)
        self.assertEqual(counts["deleted"], 1)
        self.assertEqual(counts["delete_failed"], 1)
        self.assertEqual(counts["server_unavailable"], 1)
        self.assertEqual(counts["renewed"], 1)

    def test_cleanup_export_includes_reason_codes(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:time": {
                "username": "time",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "delete_after": "2026-06-09 14:00:00",
                "last_state": {"days_remaining": 0, "upload_bytes": 0, "download_bytes": 0, "max_download_bytes": 10},
            },
            "s1:traffic": {
                "username": "traffic",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "delete_after": "2026-06-09 14:00:00",
                "last_state": {"days_remaining": 5, "upload_bytes": 6, "download_bytes": 4, "max_download_bytes": 10},
            },
            "s1:missing": {
                "username": "missing",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "already_missing",
                "delete_result": "already_missing",
                "deleted_at": "2026-06-09 12:00:00",
                "last_state": None,
            },
            "s1:unavailable": {
                "username": "unavailable",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "server_unavailable",
                "last_state": {"days_remaining": 0},
            },
            "s1:failed": {
                "username": "failed",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "delete_failed",
                "last_state": {"days_remaining": 0},
            },
        })

        exported = self.cleanup.get_expired_cleanup_export_records(filter_key="all", now=self.now)
        reasons = {record["username"]: record["reason_code"] for record in exported}

        self.assertEqual(reasons["time"], "time_expired")
        self.assertEqual(reasons["traffic"], "traffic_exhausted")
        self.assertEqual(reasons["missing"], "missing_on_server")
        self.assertEqual(reasons["unavailable"], "server_unavailable")
        self.assertEqual(reasons["failed"], "delete_failed")

    def test_admin_cleanup_default_ui_shows_pending_not_history(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:pending": {
                "username": "pending",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "delete_after": "2099-06-09 14:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:deleted": {
                "username": "deleted",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "deleted",
                "delete_result": "deleted",
                "deleted_at": "2026-06-09 12:00:00",
                "last_state": {"days_remaining": 0},
            },
        })

        text = self.cleanup._build_admin_cleanup_text("en", filter_key="queue", page=0, now=self.now)
        markup = self.cleanup._build_admin_cleanup_markup(filter_key="queue", page=0, now=self.now)
        callbacks = self.callback_data_from_markup(markup)

        self.assertIn("Pending: *1*", text)
        self.assertIn("View: *Pending*", text)
        self.assertIn("waiting for the grace period", text)
        self.assertIn("`pending`", text)
        self.assertNotIn("`deleted`", text)
        self.assertIn("admin_expired_cleanup:list:duplicate_payment:0", callbacks)
        self.assertIn("admin_expired_cleanup:list:deleted:0", callbacks)
        self.assertNotIn("admin_expired_cleanup:list:queue:0", callbacks)
        self.assertIn("admin_expired_cleanup:export:pending", callbacks)
        self.assertIn("admin_expired_cleanup:export:all", callbacks)

    def test_notification_pending_is_pending_without_manual_review_actions(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:t12345": {
                "username": "t12345",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "notification_pending",
                "cleanup_error": "notification_failed",
                "cleanup_reason": "stale_on_hold_test",
                "recovery_source": "verified_orphan_test",
                "recovery_attempts": 2,
                "first_seen_at": "2026-06-08 10:00:00",
                "last_state": {"status": "On-hold"},
            },
        })

        pending = self.cleanup.get_expired_cleanup_records(
            filter_key="pending",
            now=self.now,
        )
        manual = self.cleanup.get_expired_cleanup_records(
            filter_key="manual_review",
            now=self.now,
        )
        text = self.cleanup._build_admin_cleanup_text(
            "en",
            filter_key="pending",
            page=0,
            now=self.now,
        )
        callbacks = self.callback_data_from_markup(
            self.cleanup._build_admin_cleanup_markup(
                filter_key="pending",
                page=0,
                now=self.now,
            )
        )
        exported = self.cleanup.get_expired_cleanup_export_records(
            filter_key="pending",
            now=self.now,
        )

        self.assertEqual([record["effective_status"] for record in pending], ["pending"])
        self.assertEqual(pending[0]["cleanup_status"], "notification_pending")
        self.assertEqual(pending[0]["reason_code"], "stale_on_hold_test")
        self.assertEqual(manual, [])
        self.assertIn("retrying notification", text)
        self.assertFalse(any(callback and callback.startswith("aec:") for callback in callbacks))
        self.assertEqual(exported[0]["cleanup_status"], "notification_pending")

    def test_manual_review_ui_shows_records_and_review_actions(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:orphan": {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "unowned_server_user",
                "last_state": {"days_remaining": 0},
            },
        })

        text = self.cleanup._build_admin_cleanup_text("en", filter_key="manual_review", page=0, now=self.now)
        markup = self.cleanup._build_admin_cleanup_markup(filter_key="manual_review", page=0, now=self.now)
        callbacks = self.callback_data_from_markup(markup)

        self.assertIn("Manual Review: *1*", text)
        self.assertIn("`orphan`", text)
        self.assertIn("No matching bot ownership record", text)
        review_callbacks = [callback for callback in callbacks if callback and callback.startswith("aec:")]
        self.assertTrue(any(callback.startswith("aec:rd:mr:") for callback in review_callbacks))
        self.assertTrue(any(callback.startswith("aec:rk:mr:") for callback in review_callbacks))
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in review_callbacks))

    def test_duplicate_payment_manual_review_stays_visible_for_active_user(self):
        self.write_default_files()
        state_key = "s1:duplicate-user-b"
        self.write_json(self.cleanup.STATE_FILE, {
            state_key: {
                "username": "duplicate-user-b",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "duplicate_payment",
                "payment_id": "duplicate-payment-id",
                "keeper_username": "duplicate-user-a",
                "review_note": "Duplicate generated by repeated receipt approval; keep duplicate-user-a.",
                "last_state": {"status": "active"},
            },
        })
        active_duplicate = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 100 * self.cleanup.GB_BYTES,
            "status": "active",
        }
        client = FakeClient("s1", {"duplicate-user-b": active_duplicate})

        self.cleanup.run_expired_user_cleanup(now=self.now, multi_api=FakeMultiAPI({"s1": client}))

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(state[state_key]["cleanup_status"], "manual_review")
        self.assertEqual(state[state_key]["manual_review_reason"], "duplicate_payment")
        self.assertEqual(state[state_key]["last_state"]["status"], "active")

        manual_text = self.cleanup._build_admin_cleanup_text("en", filter_key="manual_review", page=0, now=self.now)
        duplicate_text = self.cleanup._build_admin_cleanup_text("en", filter_key="duplicate_payment", page=0, now=self.now)
        duplicate_markup = self.cleanup._build_admin_cleanup_markup(filter_key="duplicate_payment", page=0, now=self.now)
        callbacks = self.callback_data_from_markup(duplicate_markup)

        self.assertNotIn("`duplicate-user-b`", manual_text)
        self.assertIn("`duplicate-user-b`", duplicate_text)
        self.assertIn("Duplicate payment review", duplicate_text)
        self.assertIn("Manual reason: `duplicate\\_payment`", duplicate_text)
        self.assertIn("Duplicate configs from repeated payment creation", duplicate_text)
        review_callbacks = [callback for callback in callbacks if callback and callback.startswith("aec:")]
        self.assertTrue(any(callback.startswith("aec:rd:dp:") for callback in review_callbacks))
        self.assertTrue(any(callback.startswith("aec:rk:dp:") for callback in review_callbacks))
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in review_callbacks))

    def test_manual_review_action_callbacks_fit_telegram_limit(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:orphan": {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "last_state": {"days_remaining": 0},
            },
            "s1:duplicate": {
                "username": "duplicate",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "duplicate_payment",
                "last_state": {"days_remaining": 30},
            },
        })

        manual_callbacks = self.callback_data_from_markup(
            self.cleanup._build_admin_cleanup_markup(filter_key="manual_review", page=0, now=self.now)
        )
        duplicate_callbacks = self.callback_data_from_markup(
            self.cleanup._build_admin_cleanup_markup(filter_key="duplicate_payment", page=0, now=self.now)
        )
        review_callbacks = [
            callback
            for callback in manual_callbacks + duplicate_callbacks
            if callback and callback.startswith("aec:")
        ]

        self.assertEqual(len(review_callbacks), 4)
        self.assertTrue(all(len(callback.encode("utf-8")) <= 64 for callback in review_callbacks))

    def test_manual_review_keep_updates_metadata_but_keeps_record_visible(self):
        self.write_default_files()
        self.cleanup.ADMIN_CLEANUP_REVIEW_EXECUTOR = ImmediateExecutor()
        state_key = "s1:orphan"
        self.write_json(self.cleanup.STATE_FILE, {
            state_key: {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "last_state": {"days_remaining": 0},
            },
        })
        record_id = self.cleanup._state_record_id(state_key)
        call = types.SimpleNamespace(
            id="callback-1",
            data=f"aec:rk:mr:{record_id}",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )

        self.cleanup.handle_admin_expired_cleanup(call)

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(state[state_key]["cleanup_status"], "manual_review")
        self.assertEqual(state[state_key]["review_status"], "kept")
        self.assertEqual(state[state_key]["reviewed_by"], "1")
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Review action started.")
        self.assertEqual(self.cleanup._test_bot.sent_messages[-1][1], "Expired cleanup review: Kept for later review.")
        text = self.cleanup._test_bot.edited_messages[-1][0]
        self.assertIn("`orphan`", text)
        self.assertIn("Review: `kept`", text)

    def test_manual_review_delete_rechecks_and_deletes_expired_user(self):
        self.write_default_files()
        self.cleanup.ADMIN_CLEANUP_REVIEW_EXECUTOR = ImmediateExecutor()
        state_key = "s1:orphan"
        self.write_json(self.cleanup.STATE_FILE, {
            state_key: {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "last_state": {"days_remaining": 0},
            },
        })
        client = FakeClient("s1", {"orphan": self.expired_user()})
        original_multi_api = self.cleanup.MultiServerAPI
        self.cleanup.MultiServerAPI = lambda: FakeMultiAPI({"s1": client})
        self.addCleanup(setattr, self.cleanup, "MultiServerAPI", original_multi_api)
        record_id = self.cleanup._state_record_id(state_key)
        call = types.SimpleNamespace(
            id="callback-1",
            data=f"aec:rd:mr:{record_id}",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )

        self.cleanup.handle_admin_expired_cleanup(call)

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(client.deleted, ["orphan"])
        self.assertEqual(state[state_key]["cleanup_status"], "deleted")
        self.assertEqual(state[state_key]["delete_result"], "deleted")
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Review action started.")
        self.assertEqual(self.cleanup._test_bot.sent_messages[-1][1], "Expired cleanup review: User deleted.")

    def test_manual_review_action_dedupes_inflight_record(self):
        self.write_default_files()

        class HoldingExecutor:
            def __init__(self):
                self.submissions = []

            def submit(self, fn, *args, **kwargs):
                self.submissions.append((fn, args, kwargs))
                return types.SimpleNamespace()

        executor = HoldingExecutor()
        self.cleanup.ADMIN_CLEANUP_REVIEW_EXECUTOR = executor
        state_key = "s1:orphan"
        self.write_json(self.cleanup.STATE_FILE, {
            state_key: {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "last_state": {"days_remaining": 0},
            },
        })
        record_id = self.cleanup._state_record_id(state_key)
        call = types.SimpleNamespace(
            id="callback-1",
            data=f"aec:rd:mr:{record_id}",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )

        self.cleanup.handle_admin_expired_cleanup(call)
        self.cleanup.handle_admin_expired_cleanup(call)

        self.assertEqual(len(executor.submissions), 1)
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-2][1], "Review action started.")
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Review action is already running.")

    def test_legacy_manual_review_callback_formats_still_work(self):
        self.write_default_files()
        self.cleanup.ADMIN_CLEANUP_REVIEW_EXECUTOR = ImmediateExecutor()
        state_key = "s1:orphan"
        self.write_json(self.cleanup.STATE_FILE, {
            state_key: {
                "username": "orphan",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "last_state": {"days_remaining": 0},
            },
        })
        record_id = self.cleanup._state_record_id(state_key)

        legacy_three_part = types.SimpleNamespace(
            id="callback-1",
            data=f"admin_expired_cleanup:review_keep:{record_id}",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )
        self.cleanup.handle_admin_expired_cleanup(legacy_three_part)

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(state[state_key]["review_status"], "kept")
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Review action started.")
        self.assertEqual(self.cleanup._test_bot.sent_messages[-1][1], "Expired cleanup review: Kept for later review.")

        legacy_four_part = types.SimpleNamespace(
            id="callback-2",
            data=f"admin_expired_cleanup:review_keep:manual_review:{record_id}",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=21),
        )
        self.cleanup.handle_admin_expired_cleanup(legacy_four_part)

        state = self.read_json(self.cleanup.STATE_FILE)
        self.assertEqual(state[state_key]["review_status"], "kept")
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Review action started.")
        self.assertEqual(self.cleanup._test_bot.sent_messages[-1][1], "Expired cleanup review: Kept for later review.")

    def test_admin_cleanup_pagination_and_callback_route(self):
        self.write_default_files()
        state = {}
        for index in range(9):
            state[f"s1:user{index}"] = {
                "username": f"user{index}",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "delete_after": "2026-06-09 10:00:00",
                "last_state": {"days_remaining": 0},
            }
        self.write_json(self.cleanup.STATE_FILE, state)

        markup = self.cleanup._build_admin_cleanup_markup(filter_key="due", page=0, now=self.now)
        self.assertIn("admin_expired_cleanup:list:due:1", self.callback_data_from_markup(markup))

        call = types.SimpleNamespace(
            id="callback-1",
            data="admin_expired_cleanup:list:due:1",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )
        self.cleanup.handle_admin_expired_cleanup(call)

        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][0], "callback-1")
        self.assertEqual(self.cleanup._test_bot.edited_messages[-1][1]["chat_id"], 10)
        self.assertIn("Page *2/2*", self.cleanup._test_bot.edited_messages[-1][0])

    def test_expired_cleanup_startup_delay_uses_recent_finished_scan(self):
        self.write_json(self.cleanup.SCHEDULE_FILE, {
            "last_started_at": "2026-06-09 11:00:00",
            "last_finished_at": "2026-06-09 11:30:00",
            "last_success_at": "2026-06-09 11:30:00",
            "last_error": None,
        })

        delay = self.cleanup.get_expired_cleanup_startup_delay(now=self.now)

        self.assertEqual(delay, 1800)
        self.assertEqual(
            self.cleanup.get_expired_cleanup_startup_delay(
                now=self.now,
                metadata={"last_started_at": "2026-06-09 11:45:00"},
            ),
            2700,
        )

    def test_expired_cleanup_startup_delay_allows_immediate_run_without_valid_metadata(self):
        self.assertEqual(self.cleanup.get_expired_cleanup_startup_delay(now=self.now), 0)

        self.write_json(self.cleanup.SCHEDULE_FILE, {
            "last_started_at": "not-a-date",
            "last_finished_at": None,
        })

        self.assertEqual(self.cleanup.get_expired_cleanup_startup_delay(now=self.now), 0)

    def test_failed_cleanup_scan_records_finished_at_and_error(self):
        original_run = self.cleanup.run_expired_user_cleanup
        self.cleanup.run_expired_user_cleanup = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, self.cleanup, "run_expired_user_cleanup", original_run)

        with self.assertRaises(RuntimeError):
            self.cleanup.run_expired_user_cleanup_with_metadata(now=self.now)

        metadata = self.read_json(self.cleanup.SCHEDULE_FILE)
        self.assertEqual(metadata["last_started_at"], "2026-06-09T12:00:00.000000Z")
        self.assertEqual(metadata["last_finished_at"], "2026-06-09T12:00:00.000000Z")
        self.assertEqual(metadata["last_error"], "boom")
        self.assertNotIn("last_success_at", metadata)

    def test_admin_cleanup_menu_does_not_auto_start_scan(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {})
        started = []
        original_start = self.cleanup._start_cleanup_refresh_for_dashboard
        self.cleanup._start_cleanup_refresh_for_dashboard = lambda: started.append(True) or True
        self.addCleanup(setattr, self.cleanup, "_start_cleanup_refresh_for_dashboard", original_start)
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=1),
            chat=types.SimpleNamespace(id=10),
        )

        self.cleanup.admin_expired_cleanup_menu(message)

        self.assertEqual(started, [])
        self.assertEqual(self.cleanup._test_bot.replies[-1][0], message)
        self.assertIn("View: *Pending*", self.cleanup._test_bot.replies[-1][1])

    def test_admin_cleanup_refresh_starts_scan_without_blocking_render(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {})
        started = []
        original_start = self.cleanup._start_cleanup_refresh_for_dashboard
        self.cleanup._start_cleanup_refresh_for_dashboard = lambda: started.append(True) or True
        self.addCleanup(setattr, self.cleanup, "_start_cleanup_refresh_for_dashboard", original_start)

        call = types.SimpleNamespace(
            id="callback-1",
            data="admin_expired_cleanup:refresh:queue:0",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )
        self.cleanup.handle_admin_expired_cleanup(call)

        self.assertEqual(started, [True])
        self.assertEqual(self.cleanup._test_bot.edited_messages[-1][1]["chat_id"], 10)
        self.assertIn("View: *Pending*", self.cleanup._test_bot.edited_messages[-1][0])
        self.assertEqual(self.cleanup._test_bot.answered_callbacks[-1][1], "Scan started.")

    def test_admin_cleanup_text_shows_running_scan_state(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {})
        with self.cleanup._cleanup_refresh_lock:
            self.cleanup._cleanup_refresh_state.update({
                "running": True,
                "started_at": "2026-06-09 12:00:00",
                "finished_at": None,
                "error": None,
            })

        text = self.cleanup._build_admin_cleanup_text("en", filter_key="queue", page=0, now=self.now)

        self.assertIn("Scan: *running*", text)

    def test_admin_cleanup_export_current_filter_and_all_records(self):
        self.write_default_files()
        self.write_json(self.cleanup.STATE_FILE, {
            "s1:pending": {
                "username": "pending",
                "server_id": "s1",
                "source": "customer",
                "cleanup_status": "notified",
                "delete_after": "2099-06-09 14:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:deleted": {
                "username": "deleted",
                "server_id": "s1",
                "source": "test",
                "cleanup_status": "deleted",
                "delete_result": "deleted",
                "deleted_at": "2026-06-09 12:00:00",
                "last_state": {"days_remaining": 0},
            },
            "s1:duplicate": {
                "username": "duplicate",
                "server_id": "s1",
                "source": "server_user",
                "cleanup_status": "manual_review",
                "manual_review_reason": "duplicate_payment",
                "last_state": {"days_remaining": 30},
            },
        })

        self.cleanup._send_cleanup_export(10, filter_key="queue")
        pending_payload = json.loads(self.cleanup._test_bot.sent_documents[-1][1].getvalue().decode("utf-8"))
        self.cleanup._send_cleanup_export(10, filter_key="duplicate_payment")
        duplicate_payload = json.loads(self.cleanup._test_bot.sent_documents[-1][1].getvalue().decode("utf-8"))
        self.cleanup._send_cleanup_export(10, filter_key="all")
        all_payload = json.loads(self.cleanup._test_bot.sent_documents[-1][1].getvalue().decode("utf-8"))

        self.assertEqual([record["username"] for record in pending_payload], ["pending"])
        self.assertEqual([record["username"] for record in duplicate_payload], ["duplicate"])
        self.assertEqual({record["username"] for record in all_payload}, {"pending", "deleted", "duplicate"})
        self.assertIn("reason_code", pending_payload[0])

    def test_expired_cleanup_notices_do_not_offer_renewal(self):
        spec = importlib.util.spec_from_file_location("translations_under_test", TRANSLATIONS_PATH)
        translations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(translations)

        renewal_terms = ("renew", "продл", "تمدید", "uzald")
        notice_keys = ("expired_cleanup_customer_notice", "expired_cleanup_reseller_notice")
        for language, messages in translations.MESSAGE_TRANSLATIONS.items():
            for key in notice_keys:
                notice = messages[key].lower()
                for term in renewal_terms:
                    self.assertNotIn(term, notice, f"{language}.{key} mentions renewal")

    def test_stale_test_cleanup_notice_is_complete_in_every_language(self):
        spec = importlib.util.spec_from_file_location("translations_under_test", TRANSLATIONS_PATH)
        translations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(translations)

        for language, messages in translations.MESSAGE_TRANSLATIONS.items():
            notice = messages["stale_test_cleanup_notice"]
            self.assertIn("{username}", notice, language)
            self.assertIn("{grace_hours}", notice, language)
            self.assertIn("{state_summary}", notice, language)

    def test_reseller_expired_cleanup_translations_include_customer_and_config(self):
        spec = importlib.util.spec_from_file_location("translations_under_test", TRANSLATIONS_PATH)
        translations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(translations)

        for language, messages in translations.MESSAGE_TRANSLATIONS.items():
            notice = messages["expired_cleanup_reseller_notice"]
            self.assertIn("{customer_name}", notice, language)
            self.assertIn("{username}", notice, language)


if __name__ == "__main__":
    unittest.main()
