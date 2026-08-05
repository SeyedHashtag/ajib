import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "test_config.py"
)
TEST_CONFIG_STORE_PATH = MODULE_PATH.with_name("test_config_store.py")


class DummyBot:
    def __init__(self):
        self.answers = []
        self.edits = []
        self.replies = []
        self.sent_messages = []
        self.sent_photos = []

    def message_handler(self, *args, **kwargs):
        return lambda func: func

    def callback_query_handler(self, *args, **kwargs):
        return lambda func: func

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    def reply_to(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))
        return None

    def send_photo(self, *args, **kwargs):
        self.sent_photos.append((args, kwargs))
        return None


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *buttons, **kwargs):
        self.buttons.extend(buttons)


class DummyButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")


class HoldingExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))
        return types.SimpleNamespace(done=lambda: False)

    def run_next(self):
        fn, args, kwargs = self.jobs.pop(0)
        return fn(*args, **kwargs)


def install_stubs():
    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace(
        InlineKeyboardMarkup=DummyMarkup,
        InlineKeyboardButton=DummyButton,
    )
    sys.modules["telebot"] = telebot_stub

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    sys.modules["utils"] = utils_pkg

    store_spec = importlib.util.spec_from_file_location("utils.test_config_store", TEST_CONFIG_STORE_PATH)
    store_module = importlib.util.module_from_spec(store_spec)
    sys.modules[store_spec.name] = store_module
    store_spec.loader.exec_module(store_module)
    utils_pkg.test_config_store = store_module

    command_stub = types.ModuleType("utils.command")
    command_stub.bot = DummyBot()
    command_stub.is_admin = lambda user_id: False
    sys.modules["utils.command"] = command_stub

    common_stub = types.ModuleType("utils.common")
    common_stub.create_main_markup = lambda *args, **kwargs: None
    sys.modules["utils.common"] = common_stub

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.MultiServerAPI = object
    sys.modules["utils.api_client"] = api_client_stub

    translations_stub = types.ModuleType("utils.translations")
    translations_stub.BUTTON_TRANSLATIONS = {"en": {"test_config": "Test Config"}}
    translations_stub.get_message_text = lambda language, key: key
    sys.modules["utils.translations"] = translations_stub

    language_stub = types.ModuleType("utils.language")
    language_stub.get_user_language = lambda user_id: "en"
    sys.modules["utils.language"] = language_stub

    username_utils_stub = types.ModuleType("utils.username_utils")
    username_utils_stub.RecordedUsernameLoadError = RuntimeError
    username_utils_stub.allocate_username = lambda prefix, user_id, existing: f"{prefix}{user_id}"
    username_utils_stub.build_user_note = lambda **kwargs: ""
    username_utils_stub.load_recorded_usernames = lambda *args, **kwargs: set()
    sys.modules["utils.username_utils"] = username_utils_stub

    telegram_safe_stub = types.ModuleType("utils.telegram_safe")
    telegram_safe_stub.safe_answer_callback_query = lambda bot, *args, **kwargs: bot.answer_callback_query(*args, **kwargs)
    telegram_safe_stub.safe_edit_message_text = lambda bot, *args, **kwargs: bot.edit_message_text(*args, **kwargs)
    telegram_safe_stub.safe_send_message = lambda bot, *args, **kwargs: bot.send_message(*args, **kwargs)
    telegram_safe_stub.safe_send_photo = lambda bot, *args, **kwargs: bot.send_photo(*args, **kwargs)
    sys.modules["utils.telegram_safe"] = telegram_safe_stub

    download_guidance_stub = types.ModuleType("utils.download_guidance")
    download_guidance_stub.send_download_prompt_safely = lambda *args, **kwargs: None
    sys.modules["utils.download_guidance"] = download_guidance_stub

    sys.modules["qrcode"] = types.SimpleNamespace(make=lambda *args, **kwargs: None)


install_stubs()
spec = importlib.util.spec_from_file_location("test_config_queue_under_test", MODULE_PATH)
test_config_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = test_config_module
spec.loader.exec_module(test_config_module)


class TestConfigQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        test_config_module.TEST_CONFIGS_FILE = str(Path(self.tmpdir.name) / "test_configs.json")
        self.executor = HoldingExecutor()
        self.create_calls = []
        test_config_module.TEST_CONFIG_EXECUTOR = self.executor
        test_config_module.TEST_CONFIG_INFLIGHT.clear()
        test_config_module.bot.answers = []
        test_config_module.bot.edits = []
        test_config_module.bot.sent_messages = []
        test_config_module.bot.sent_photos = []
        test_config_module.is_test_creation_disabled = lambda: False
        test_config_module.has_used_test_config = lambda user_id: False
        test_config_module.create_test_config = (
            lambda *args, **kwargs: self.create_calls.append((args, kwargs))
        )

    def make_call(self, user_id=123):
        return types.SimpleNamespace(
            id="callback-1",
            data="confirm_test_config",
            from_user=types.SimpleNamespace(id=user_id, username="buyer"),
            message=types.SimpleNamespace(
                chat=types.SimpleNamespace(id=456),
                message_id=789,
            ),
        )

    def test_confirm_test_config_queues_creation_and_dedupes_duplicate_taps(self):
        call = self.make_call()

        test_config_module.handle_confirm_test_config(call)
        test_config_module.handle_confirm_test_config(call)

        self.assertEqual(len(self.executor.jobs), 1)
        self.assertEqual(self.create_calls, [])
        self.assertIn(123, test_config_module.TEST_CONFIG_INFLIGHT)

        self.executor.run_next()

        self.assertEqual(len(self.create_calls), 1)
        args, kwargs = self.create_calls[0]
        self.assertEqual(args[:2], (123, 456))
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["telegram_username"], "buyer")
        self.assertEqual(test_config_module.TEST_CONFIG_INFLIGHT, set())
        self.assertEqual(test_config_module.bot.edits[0][0][0], "test_config_creating")

    def test_creation_claim_prevents_duplicates_and_stale_claim_recovers(self):
        now = datetime(2026, 6, 9, 12, 0, 0)

        self.assertTrue(test_config_module._claim_test_config_creation(123, now=now))
        self.assertFalse(test_config_module._claim_test_config_creation(123, now=now + timedelta(minutes=1)))
        self.assertTrue(test_config_module._claim_test_config_creation(123, now=now + timedelta(minutes=16)))

    def test_releasing_failed_creation_removes_placeholder(self):
        self.assertTrue(test_config_module._claim_test_config_creation(123))

        test_config_module._release_test_config_creation(123)

        self.assertEqual(test_config_module.load_test_configs(), {})

    def test_finalizing_creation_preserves_recovered_history_and_clears_claim(self):
        Path(test_config_module.TEST_CONFIGS_FILE).write_text(json.dumps({
            "123": {
                "telegram_id": 123,
                "used_at": "2026-05-01 12:00:00",
                "reset_at": "2026-06-09 11:00:00",
                "historical_configs": [{"username": "t123", "server_id": "s1"}],
            }
        }), encoding="utf-8")
        self.assertTrue(test_config_module._claim_test_config_creation(123))

        test_config_module.mark_test_config_used(123, username="t123a", server_id="s2")

        entry = test_config_module.load_test_configs()["123"]
        self.assertEqual(entry["username"], "t123a")
        self.assertEqual(entry["server_id"], "s2")
        self.assertEqual(entry["historical_configs"][0]["username"], "t123")
        self.assertNotIn("creation_pending_at", entry)

    def test_recovered_historical_user_is_ineligible_until_admin_reset(self):
        Path(test_config_module.TEST_CONFIGS_FILE).write_text(json.dumps({
            "123": {
                "telegram_id": 123,
                "used_at": "2026-01-01 12:00:00",
                "historical_configs": [{"username": "t123", "server_id": "s1"}],
            }
        }), encoding="utf-8")
        self.assertTrue(test_config_module._has_used_test_config_from(test_config_module.load_test_configs(), 123))

        count = test_config_module.reset_test_users(mode="expired")

        self.assertEqual(count, 1)
        self.assertFalse(test_config_module._has_used_test_config_from(test_config_module.load_test_configs(), 123))

    def test_connected_marker_drives_persisted_trial_journey_state(self):
        used_at = "2026-06-09 12:00:00"
        Path(test_config_module.TEST_CONFIGS_FILE).write_text(json.dumps({
            "123": {
                "telegram_id": 123,
                "used_at": used_at,
                "username": "t123",
            }
        }), encoding="utf-8")
        now = datetime(2026, 6, 10, 12, 0, 0)

        before = test_config_module.get_test_config_journey(123, now=now)
        self.assertIsNone(before["connected_at"])
        self.assertEqual(before["remaining_days"], 29)

        self.assertTrue(test_config_module.mark_test_config_connected(
            123,
            connected_at="2026-06-10 12:00:00",
        ))
        after = test_config_module.get_test_config_journey(123, now=now)
        self.assertEqual(after["connected_at"], "2026-06-10 12:00:00")

    def test_successful_test_config_sends_three_step_activation_flow(self):
        class DummyQR:
            def save(self, target, image_format):
                target.write(b"qr")

        original_make = test_config_module.qrcode.make
        try:
            test_config_module.qrcode.make = lambda value: DummyQR()
            test_config_module._send_created_test_config(
                456,
                "t123",
                {"normal_sub": "https://example.com/sub"},
            )
        finally:
            test_config_module.qrcode.make = original_make

        self.assertEqual(len(test_config_module.bot.sent_photos), 1)
        self.assertEqual(test_config_module.bot.sent_messages[0][0], (456, "trial_activation_steps"))
        markup = test_config_module.bot.sent_messages[0][1]["reply_markup"]
        self.assertEqual(
            [button.callback_data for button in markup.buttons],
            ["trial_connected", "trial_need_help", "trial_see_plans"],
        )

    def test_need_help_opens_download_guidance_on_demand(self):
        calls = []
        original_guidance = test_config_module.send_download_prompt_safely
        try:
            test_config_module.send_download_prompt_safely = (
                lambda *args, **kwargs: calls.append((args, kwargs))
            )
            call = self.make_call()
            call.data = "trial_need_help"
            test_config_module.handle_trial_need_help(call)
        finally:
            test_config_module.send_download_prompt_safely = original_guidance

        self.assertEqual(calls[0][0][1:], (456, "en"))

    def test_test_config_without_a_url_does_not_send_download_guidance(self):
        calls = []
        original_guidance = test_config_module.send_download_prompt_safely
        try:
            test_config_module.send_download_prompt_safely = (
                lambda *args, **kwargs: calls.append((args, kwargs))
            )
            test_config_module._send_created_test_config(456, "t123", None)
        finally:
            test_config_module.send_download_prompt_safely = original_guidance

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
