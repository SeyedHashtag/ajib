import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "vpn_servers.py"


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *args, **kwargs):
        self.buttons.extend(args)
        return self


class DummyButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")


class DummyBot:
    def __init__(self):
        self.replies = []
        self.edits = []
        self.answers = []
        self.sent_messages = []

    def message_handler(self, *args, **kwargs):
        return lambda func: func

    def callback_query_handler(self, *args, **kwargs):
        return lambda func: func

    def reply_to(self, *args, **kwargs):
        self.replies.append((args, kwargs))
        message = args[0]
        return types.SimpleNamespace(chat=message.chat, message_id=100 + len(self.replies))

    def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))


class HoldingExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        self.jobs.append((fn, args, kwargs))
        return types.SimpleNamespace(done=lambda: False)

    def run_next(self):
        fn, args, kwargs = self.jobs.pop(0)
        return fn(*args, **kwargs)


class FakeMultiServerAPI:
    calls = []

    def get_server_statuses(self):
        self.__class__.calls.append("statuses")
        return [
            {
                "id": "s1",
                "name": "Server 1",
                "enabled": True,
                "healthy": True,
                "active_count": 3,
                "allocated_count": 5,
                "connected_count": 3,
                "hold_count": 2,
                "blocked_count": 1,
                "unknown_count": 1,
                "weight": 1,
                "load_ratio": 3.0,
            }
        ]


def load_vpn_servers_module():
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
    sys.modules.pop("telebot", None)

    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace(
        InlineKeyboardMarkup=DummyMarkup,
        InlineKeyboardButton=DummyButton,
    )
    sys.modules["telebot"] = telebot_stub

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = []
    sys.modules["utils"] = utils_pkg

    bot = DummyBot()
    command_stub = types.ModuleType("utils.command")
    command_stub.bot = bot
    command_stub.is_admin = lambda user_id: user_id == 1
    sys.modules["utils.command"] = command_stub

    common_stub = types.ModuleType("utils.common")
    common_stub.admin_action_text = lambda key: {
        "vpn_servers": "⚖️ VPN Servers",
    }[key]
    sys.modules["utils.common"] = common_stub

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.MultiServerAPI = FakeMultiServerAPI
    api_client_stub.get_server_configs = lambda: [{"id": "s1", "name": "Server 1", "enabled": True}]
    api_client_stub.update_server_config = lambda _server_id, **_changes: True
    sys.modules["utils.api_client"] = api_client_stub

    telegram_safe_stub = types.ModuleType("utils.telegram_safe")
    telegram_safe_stub.safe_answer_callback_query = lambda bot_obj, *args, **kwargs: bot_obj.answer_callback_query(*args, **kwargs)
    telegram_safe_stub.safe_edit_message_text = lambda bot_obj, *args, **kwargs: bot_obj.edit_message_text(*args, **kwargs)
    telegram_safe_stub.safe_reply_to = lambda bot_obj, *args, **kwargs: bot_obj.reply_to(*args, **kwargs)
    telegram_safe_stub.safe_send_message = lambda bot_obj, *args, **kwargs: bot_obj.send_message(*args, **kwargs)
    sys.modules["utils.telegram_safe"] = telegram_safe_stub

    FakeMultiServerAPI.calls = []
    spec = importlib.util.spec_from_file_location("vpn_servers_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, bot


class VpnServersTests(unittest.TestCase):
    def test_show_vpn_servers_queues_status_snapshot(self):
        module, bot = load_vpn_servers_module()
        executor = HoldingExecutor()
        module.VPN_SERVER_MENU_EXECUTOR = executor
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=1),
            chat=types.SimpleNamespace(id=10),
            message_id=20,
            text="VPN Servers",
        )

        module.show_vpn_servers(message)

        self.assertEqual(bot.replies, [])
        self.assertEqual(FakeMultiServerAPI.calls, [])
        self.assertEqual(len(executor.jobs), 1)

        executor.run_next()

        self.assertEqual(bot.replies[0][0][1], "Loading VPN servers...")
        self.assertIn("Server 1", bot.edits[0][0][0])
        self.assertIn("Allocated: `5`", bot.edits[0][0][0])
        self.assertIn("Hold: `2`", bot.edits[0][0][0])
        self.assertIn("Unknown: `1`", bot.edits[0][0][0])
        self.assertEqual(FakeMultiServerAPI.calls, ["statuses"])

    def test_zero_weight_status_is_rendered_as_paused(self):
        module, _bot = load_vpn_servers_module()

        text = module._format_status_line({
            "id": "s1",
            "name": "Server 1",
            "enabled": True,
            "healthy": True,
            "creation_ready": True,
            "accepting_new_users": False,
            "placement_reason": "weight_zero",
            "allocated_count": 5,
            "weight": 0,
            "load_ratio": None,
        })

        self.assertIn("Placement: `paused (weight 0)`", text)
        self.assertIn("Weight: `0` | Load: `N/A`", text)

    def test_weight_editor_accepts_zero_and_persists_it(self):
        module, bot = load_vpn_servers_module()
        module.VPN_SERVER_MENU_EXECUTOR = HoldingExecutor()
        servers = [{"id": "s1", "name": "Server 1", "enabled": True, "weight": 1}]
        saved = []
        module.get_server_configs = lambda: servers
        module.update_server_config = lambda server_id, **changes: saved.append((server_id, changes)) or True
        module.server_admin_state[1] = {"state": "waiting_server_weight", "server_id": "s1"}
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=1),
            chat=types.SimpleNamespace(id=10),
            message_id=20,
            text="0",
        )

        module.handle_server_weight_input(message)

        self.assertEqual(saved[0], ("s1", {"weight": 0.0}))
        self.assertNotIn(1, module.server_admin_state)
        self.assertEqual(len(module.VPN_SERVER_MENU_EXECUTOR.jobs), 1)

    def test_weight_editor_rejects_negative_and_non_finite_values(self):
        module, bot = load_vpn_servers_module()

        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                module.server_admin_state[1] = {
                    "state": "waiting_server_weight",
                    "server_id": "s1",
                }
                message = types.SimpleNamespace(
                    from_user=types.SimpleNamespace(id=1),
                    chat=types.SimpleNamespace(id=10),
                    message_id=20,
                    text=value,
                )

                module.handle_server_weight_input(message)

        self.assertEqual(len(bot.replies), 3)
        self.assertTrue(all("finite non-negative" in args[1] for args, _kwargs in bot.replies))

    def test_toggle_reports_persistence_failure(self):
        module, bot = load_vpn_servers_module()
        module.get_server_configs = lambda: [{"id": "s1", "name": "Server 1", "enabled": True}]
        module.update_server_config = lambda _server_id, **_changes: False
        call = types.SimpleNamespace(
            id="callback",
            data="vpn_server:toggle:s1",
            from_user=types.SimpleNamespace(id=1),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=10), message_id=20),
        )

        module.handle_vpn_server_callback(call)

        self.assertIn("Failed to update server.", bot.answers[0][0])
        self.assertTrue(bot.answers[0][1]["show_alert"])


if __name__ == "__main__":
    unittest.main()
