import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITUSER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "edituser.py"


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *buttons):
        self.buttons.extend(buttons)


class DummyButton:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class DummyQR:
    def save(self, bio, *_args, **_kwargs):
        bio.write(b"qr")


class DummyBot:
    def __init__(self):
        self.sent_photos = []
        self.replies = []
        self.sent_messages = []
        self.next_steps = []

    def callback_query_handler(self, *args, **kwargs):
        return lambda func: func

    def message_handler(self, *args, **kwargs):
        return lambda func: func

    def send_chat_action(self, *args, **kwargs):
        return None

    def send_photo(self, *args, **kwargs):
        self.sent_photos.append((args, kwargs))

    def reply_to(self, *args, **kwargs):
        self.replies.append((args, kwargs))
        return types.SimpleNamespace(chat=getattr(args[0], "chat", None))

    def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=args[0]))

    def register_next_step_handler(self, *args, **kwargs):
        self.next_steps.append((args, kwargs))


def load_edituser():
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
    sys.modules.pop("telebot", None)
    sys.modules.pop("qrcode", None)

    bot = DummyBot()
    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace(
        InlineKeyboardMarkup=DummyMarkup,
        InlineKeyboardButton=DummyButton,
    )
    sys.modules["telebot"] = telebot_stub
    sys.modules["qrcode"] = types.SimpleNamespace(make=lambda *_args, **_kwargs: DummyQR())

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(EDITUSER_PATH.parent)]
    sys.modules["utils"] = utils_pkg

    command_stub = types.ModuleType("utils.command")
    command_stub.bot = bot
    command_stub.is_admin = lambda _user_id: True
    sys.modules["utils.command"] = command_stub

    common_stub = types.ModuleType("utils.common")
    common_stub.admin_action_text = lambda key: {
        "show_user": "👤 Show User",
    }[key]
    common_stub.create_admin_markup = lambda *args, **kwargs: DummyMarkup()
    common_stub.create_main_markup = lambda *args, **kwargs: DummyMarkup()
    common_stub.is_admin_main_menu_button = lambda _text: False
    common_stub.resolve_admin_menu_view = lambda _text: None
    sys.modules["utils.common"] = common_stub

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.APIClient = object
    api_client_stub.MultiServerAPI = object
    sys.modules["utils.api_client"] = api_client_stub

    spec = importlib.util.spec_from_file_location("edituser_server_display_under_test", EDITUSER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, bot


class AdminServerDisplayTests(unittest.TestCase):
    def test_show_user_displays_escaped_server_name_and_id(self):
        edituser, bot = load_edituser()
        api_client = types.SimpleNamespace(
            server_name="Germany *West*",
            server_id="de-1",
            get_user_uri=lambda _username: {"normal_sub": "https://sub.example/alice"},
        )
        user_details = {
            "upload_bytes": 0,
            "download_bytes": 0,
            "status": "active",
            "max_download_bytes": 10 * (1024 ** 3),
            "expiration_days": 30,
            "account_creation_date": "2026-07-01",
            "blocked": False,
        }
        edituser.MultiServerAPI = lambda: types.SimpleNamespace(
            find_user=lambda _username: (api_client, user_details)
        )
        message = types.SimpleNamespace(
            text="alice",
            chat=types.SimpleNamespace(id=555),
            from_user=types.SimpleNamespace(id=1),
        )

        edituser.process_show_user(message)

        caption = bot.sent_photos[0][1]["caption"]
        self.assertIn("🌐 Server: Germany \\*West\\* (`de-1`)", caption)

    def test_server_label_falls_back_to_id(self):
        edituser, _bot = load_edituser()

        label = edituser._format_server_label(types.SimpleNamespace(server_id="de-1"))

        self.assertEqual(label, "`de-1`")

    def test_duplicate_username_requires_exact_server_selection(self):
        edituser, bot = load_edituser()
        first = types.SimpleNamespace(server_id="s1", server_name="One", panel_type="blitz")
        second = types.SimpleNamespace(server_id="s2", server_name="Two", panel_type="blitz")
        edituser.MultiServerAPI = lambda: types.SimpleNamespace(
            find_user_matches=lambda *_args, **_kwargs: [
                {"client": first, "user": {}, "ref": edituser._make_ref(first, "alice")},
                {"client": second, "user": {}, "ref": edituser._make_ref(second, "alice")},
            ]
        )
        message = types.SimpleNamespace(
            text="alice",
            chat=types.SimpleNamespace(id=555),
            from_user=types.SimpleNamespace(id=1),
        )

        edituser.process_show_user(message)

        prompt = bot.replies[-1]
        self.assertIn("more than one server", prompt[0][1])
        callbacks = [button.kwargs["callback_data"] for button in prompt[1]["reply_markup"].buttons]
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(all(value.startswith("show_user_ref:") for value in callbacks))

    def test_selected_user_action_uses_only_recorded_server(self):
        edituser, bot = load_edituser()
        ref = types.SimpleNamespace(server_id="s2", username="alice", panel_type="blitz")
        token = edituser._store_user_context(ref)
        lookups = []
        resets = []
        selected_client = types.SimpleNamespace(
            reset_user=lambda username: resets.append(username) or {"ok": True}
        )

        def exact_lookup(username, server_id):
            lookups.append((username, server_id))
            return selected_client, {"username": username}, {"status": "found"}

        edituser.MultiServerAPI = lambda: types.SimpleNamespace(
            find_user_on_server=exact_lookup
        )
        call = types.SimpleNamespace(
            data=f"reset_user:{token}",
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=555)),
        )

        edituser.handle_edit_callback(call)

        self.assertEqual(lookups, [("alice", "s2")])
        self.assertEqual(resets, ["alice"])
        self.assertIn("reset successfully", bot.sent_messages[-1][0][1].lower())


if __name__ == "__main__":
    unittest.main()
