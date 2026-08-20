import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "common.py"
DELETEUSER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "deleteuser.py"
EDITUSER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "edituser.py"
ADDUSER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "adduser.py"


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.rows = []
        self.buttons = []

    def row(self, *buttons):
        self.rows.append(buttons)
        return self

    def add(self, *buttons):
        self.buttons.extend(buttons)
        return self


class DummyButton:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class DummyBot:
    def __init__(self):
        self.callback_answers = []
        self.cleared_chat_ids = []
        self.edited_messages = []
        self.replies = []
        self.sent_messages = []
        self.sent_photos = []
        self.chat_actions = []

    def callback_query_handler(self, *args, **kwargs):
        return lambda func: func

    def message_handler(self, *args, **kwargs):
        return lambda func: func

    def answer_callback_query(self, callback_id, *args, **kwargs):
        self.callback_answers.append((callback_id, args, kwargs))

    def clear_step_handler_by_chat_id(self, chat_id):
        self.cleared_chat_ids.append(chat_id)

    def edit_message_text(self, *args, **kwargs):
        self.edited_messages.append((args, kwargs))

    def reply_to(self, *args, **kwargs):
        self.replies.append((args, kwargs))
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=kwargs.get("chat_id")), message_id=123)

    def send_chat_action(self, *args, **kwargs):
        self.chat_actions.append((args, kwargs))

    def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))

    def send_photo(self, *args, **kwargs):
        self.sent_photos.append((args, kwargs))


class ForbiddenMultiServerAPI:
    def __init__(self):
        raise AssertionError("MultiServerAPI should not be used for admin menu buttons")


def clear_test_modules():
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
    sys.modules.pop("telebot", None)
    sys.modules.pop("qrcode", None)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def install_stubs():
    clear_test_modules()
    bot = DummyBot()

    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace(
        InlineKeyboardMarkup=DummyMarkup,
        InlineKeyboardButton=DummyButton,
        ReplyKeyboardMarkup=DummyMarkup,
        KeyboardButton=DummyButton,
    )
    sys.modules["telebot"] = telebot_stub
    sys.modules["qrcode"] = types.SimpleNamespace(make=lambda *_args, **_kwargs: None)

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(COMMON_PATH.parent)]
    sys.modules["utils"] = utils_pkg

    command_stub = types.ModuleType("utils.command")
    command_stub.bot = bot
    command_stub.is_admin = lambda _user_id: True
    sys.modules["utils.command"] = command_stub

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.APIClient = object
    api_client_stub.MultiServerAPI = ForbiddenMultiServerAPI
    sys.modules["utils.api_client"] = api_client_stub

    common = load_module(COMMON_PATH, "utils.common")
    return bot, common


def load_deleteuser():
    bot, _common = install_stubs()
    return load_module(DELETEUSER_PATH, "deleteuser_under_test"), bot


def load_edituser():
    bot, _common = install_stubs()
    return load_module(EDITUSER_PATH, "edituser_under_test"), bot


def load_adduser():
    bot, _common = install_stubs()
    return load_module(ADDUSER_PATH, "adduser_under_test"), bot


def make_call():
    return types.SimpleNamespace(
        id="callback-id",
        message=types.SimpleNamespace(chat=types.SimpleNamespace(id=555), message_id=777),
    )


def make_add_user_call():
    return types.SimpleNamespace(
        id="callback-id",
        data="unlimited_user_choice:yes:alice:10:30",
        message=types.SimpleNamespace(
            chat=types.SimpleNamespace(id=555),
            message_id=777,
        ),
    )


def make_message(text):
    return types.SimpleNamespace(
        text=text,
        chat=types.SimpleNamespace(id=555),
        from_user=types.SimpleNamespace(id=1),
    )


def markup_text_rows(markup):
    return [
        [button.args[0] if isinstance(button, DummyButton) else button for button in row]
        for row in markup.rows
    ]


class AdminPromptCancellationTests(unittest.TestCase):
    def test_cancel_delete_clears_next_step_handler(self):
        deleteuser, bot = load_deleteuser()

        deleteuser.handle_cancel_delete(make_call())

        self.assertEqual(bot.callback_answers[0][0], "callback-id")
        self.assertEqual(bot.cleared_chat_ids, [555])
        self.assertEqual(bot.edited_messages[0][1]["chat_id"], 555)
        self.assertEqual(bot.edited_messages[0][1]["message_id"], 777)

    def test_cancel_show_user_clears_next_step_handler(self):
        edituser, bot = load_edituser()

        edituser.handle_cancel_show_user(make_call())

        self.assertEqual(bot.callback_answers[0][0], "callback-id")
        self.assertEqual(bot.cleared_chat_ids, [555])
        self.assertEqual(bot.edited_messages[0][1]["chat_id"], 555)
        self.assertEqual(bot.edited_messages[0][1]["message_id"], 777)

    def test_delete_user_prompt_treats_admin_menu_button_as_cancel(self):
        deleteuser, bot = load_deleteuser()

        deleteuser.process_delete_user(make_message("💼 Manage Resellers"))

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("Operation canceled.", bot.replies[0][0][1])
        self.assertEqual(bot.chat_actions, [])
        self.assertEqual(
            markup_text_rows(bot.replies[0][1]["reply_markup"])[0],
            ["✅ Confirmations", "📊 Server Info"],
        )

    def test_show_user_prompt_treats_admin_menu_button_as_cancel(self):
        edituser, bot = load_edituser()

        edituser.process_show_user(make_message("💼 Manage Resellers"))

        self.assertEqual(len(bot.replies), 1)
        self.assertIn("Operation canceled.", bot.replies[0][0][1])
        self.assertEqual(bot.chat_actions, [])
        self.assertEqual(
            markup_text_rows(bot.replies[0][1]["reply_markup"])[0],
            ["✅ Confirmations", "📊 Server Info"],
        )

    def test_prompt_category_navigation_opens_requested_admin_group(self):
        for load_handler, process_name in (
            (load_deleteuser, "process_delete_user"),
            (load_edituser, "process_show_user"),
        ):
            with self.subTest(process=process_name):
                module, bot = load_handler()

                getattr(module, process_name)(make_message("👥 Users"))

                markup = bot.replies[0][1]["reply_markup"]
                self.assertEqual(
                    markup_text_rows(markup),
                    [
                        ["➕ Add User", "👤 Show User"],
                        ["❌ Delete User", "🧪 Manage Test Accounts"],
                        ["🔁 Mass Copy / Migrate"],
                        ["🧹 Expired Cleanup"],
                        ["🏠 Admin Menu"],
                    ],
                )

    def test_prompt_home_navigation_restores_admin_root(self):
        for load_handler, process_name in (
            (load_deleteuser, "process_delete_user"),
            (load_edituser, "process_show_user"),
        ):
            with self.subTest(process=process_name):
                module, bot = load_handler()

                getattr(module, process_name)(make_message("🏠 Admin Menu"))

                self.assertEqual(
                    markup_text_rows(bot.replies[0][1]["reply_markup"])[0],
                    ["✅ Confirmations", "📊 Server Info"],
                )

    def test_add_user_cancel_paths_restore_admin_root_keyboard(self):
        adduser, bot = load_adduser()

        adduser.process_add_user_step1(make_message("❌ Cancel"))
        adduser.process_add_user_step2(make_message("❌ Cancel"), "alice")
        adduser.process_add_user_step3(make_message("❌ Cancel"), "alice", 10)

        self.assertEqual(len(bot.replies), 3)
        for _args, kwargs in bot.replies:
            self.assertEqual(
                markup_text_rows(kwargs["reply_markup"]),
                [
                    ["✅ Confirmations", "📊 Server Info"],
                    ["👥 Users", "💳 Sales"],
                    ["💼 Resellers", "⚙️ System"],
                    ["📊 Reports", "📣 Messaging"],
                ],
            )

    def test_add_user_callback_exit_clears_inline_controls_and_restores_root(self):
        adduser, bot = load_adduser()

        adduser._finish_add_user_callback(make_call(), "Failed to add user.")

        self.assertEqual(bot.edited_messages[0][0][0], "Failed to add user.")
        self.assertIsInstance(
            bot.edited_messages[0][1]["reply_markup"],
            DummyMarkup,
        )
        self.assertEqual(
            bot.sent_messages[0][0],
            (555, "Admin dashboard is ready."),
        )
        self.assertEqual(
            markup_text_rows(bot.sent_messages[0][1]["reply_markup"]),
            [
                ["✅ Confirmations", "📊 Server Info"],
                ["👥 Users", "💳 Sales"],
                ["💼 Resellers", "⚙️ System"],
                ["📊 Reports", "📣 Messaging"],
            ],
        )

    def test_add_user_callback_outcomes_restore_admin_navigation(self):
        class DummyQR:
            def save(self, buffer, _format):
                buffer.write(b"qr")

        class FakeClient:
            server_id = "main"

            def __init__(self, add_result=True, uri=None):
                self.add_result = add_result
                self.uri = {"normal_sub": "https://example.test/sub"} if uri is None else uri

            def add_user(self, *_args, **_kwargs):
                return self.add_result

            def get_user_uri(self, _username):
                return self.uri

        class FakeMultiServerAPI:
            def __init__(self, client):
                self.client = client

            def select_server_for_new_user(self):
                return self.client

            def record_created_user(self, *_args):
                return None

        scenarios = {
            "no_server": None,
            "api_failure": FakeClient(add_result=False),
            "missing_uri": FakeClient(uri={}),
            "success": FakeClient(),
        }

        for scenario, client in scenarios.items():
            with self.subTest(scenario=scenario):
                adduser, bot = load_adduser()
                adduser.MultiServerAPI = lambda: FakeMultiServerAPI(client)
                adduser.qrcode.make = lambda _url: DummyQR()

                adduser.process_add_user_step4(make_add_user_call())

                if scenario == "success":
                    markup = bot.sent_photos[-1][1]["reply_markup"]
                else:
                    self.assertEqual(
                        bot.sent_messages[-1][0],
                        (555, "Admin dashboard is ready."),
                    )
                    markup = bot.sent_messages[-1][1]["reply_markup"]
                self.assertEqual(
                    markup_text_rows(markup)[0],
                    ["✅ Confirmations", "📊 Server Info"],
                )

        adduser, bot = load_adduser()
        adduser.MultiServerAPI = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

        adduser.process_add_user_step4(make_add_user_call())

        self.assertEqual(
            bot.sent_messages[-1][0],
            (555, "Admin dashboard is ready."),
        )


if __name__ == "__main__":
    unittest.main()
