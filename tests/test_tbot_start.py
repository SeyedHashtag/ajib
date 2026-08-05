import importlib.util
import sys
import types
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "tbot.py"
)


class DummyBot:
    def __init__(self, events):
        self.events = events
        self.replies = []
        self.sent_messages = []
        self.message_handlers = []
        self.callback_handlers = []

    def message_handler(self, *args, **kwargs):
        def decorator(func):
            self.message_handlers.append((func, args, kwargs))
            return func

        return decorator

    def callback_query_handler(self, *args, **kwargs):
        def decorator(func):
            self.callback_handlers.append((func, args, kwargs))
            return func

        return decorator

    def reply_to(self, *args, **kwargs):
        self.events.append("reply")
        self.replies.append((args, kwargs))

    def send_message(self, *args, **kwargs):
        self.events.append("send")
        self.sent_messages.append((args, kwargs))

    def polling(self, *args, **kwargs):
        return None


def load_tbot_module():
    for name in list(sys.modules):
        if name == "telebot" or name == "tbot_under_test" or name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)

    events = []
    bot = DummyBot(events)

    telebot_stub = types.ModuleType("telebot")
    telebot_stub.types = types.SimpleNamespace()
    sys.modules["telebot"] = telebot_stub

    utils_stub = types.ModuleType("utils")
    utils_stub.__path__ = []
    utils_stub.bot = bot
    utils_stub.GROWTH_FUNNEL_BUTTON_TEXT = "📈 Growth Funnel"
    utils_stub.ADMIN_CATEGORIES = {
        "users": {"text": "👥 Users", "style": "primary"},
        "sales": {"text": "💳 Sales", "style": "primary"},
        "resellers": {"text": "💼 Resellers", "style": "primary"},
        "system": {"text": "⚙️ System", "style": "primary"},
        "reports": {"text": "📊 Reports", "style": "primary"},
        "messaging": {"text": "📣 Messaging", "style": "primary"},
    }
    admin_views = {
        category["text"]: view
        for view, category in utils_stub.ADMIN_CATEGORIES.items()
    }
    admin_views["🏠 Admin Menu"] = "root"
    utils_stub.resolve_admin_menu_view = lambda text: admin_views.get(text)
    utils_stub.create_admin_markup = lambda view="root": {"admin_view": view}
    utils_stub.process_referral = lambda *args, **kwargs: (False, None)
    utils_stub.record_main_growth_event = lambda *args, **kwargs: None
    utils_stub.get_user_language = lambda user_id: "en"
    utils_stub.get_message_text = lambda language, key: key
    utils_stub.get_button_text = lambda language, key: key
    utils_stub.is_admin = lambda user_id: False
    utils_stub.create_main_markup = lambda *args, **kwargs: {"markup": kwargs}
    utils_stub.resolve_user_language = lambda user_id, telegram_language_code=None: (
        telegram_language_code if telegram_language_code in {"en", "fa", "ru", "tk"} else None
    )
    utils_stub.build_language_selection_markup = lambda: {"languages": True}
    utils_stub.build_customer_welcome = lambda user_id, language: (
        f"welcome_{language}",
        {"welcome": user_id},
    )
    utils_stub.show_plans = lambda *args, **kwargs: events.append("show_plans")
    utils_stub.my_configs = lambda *args, **kwargs: events.append("my_configs")
    utils_stub.has_used_test_config = lambda user_id: False
    utils_stub.is_test_creation_disabled = lambda: False
    utils_stub.add_to_waiting_list = lambda *args, **kwargs: events.append("waitlist")
    utils_stub.create_test_config = lambda *args, **kwargs: events.append("create_test_config")
    sys.modules["utils"] = utils_stub

    telegram_safe_stub = types.ModuleType("utils.telegram_safe")
    telegram_safe_stub.safe_reply_to = lambda bot_obj, *args, **kwargs: bot_obj.reply_to(*args, **kwargs)
    telegram_safe_stub.safe_send_message = lambda bot_obj, *args, **kwargs: bot_obj.send_message(*args, **kwargs)
    telegram_safe_stub.safe_answer_callback_query = lambda *args, **kwargs: events.append("answer")
    sys.modules["utils.telegram_safe"] = telegram_safe_stub

    spec = importlib.util.spec_from_file_location("tbot_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, bot, events


class TBotStartTests(unittest.TestCase):
    def make_message(self, user_id=123, text="/start", language_code="en"):
        return types.SimpleNamespace(
            text=text,
            from_user=types.SimpleNamespace(
                id=user_id,
                username="buyer",
                first_name="Buyer",
                last_name="Example",
                language_code=language_code,
            ),
            chat=types.SimpleNamespace(id=456),
        )

    def test_start_shows_explicit_welcome_without_automatic_test_creation(self):
        module, bot, events = load_tbot_module()

        module.send_welcome(self.make_message())

        self.assertEqual(events, ["reply", "send"])
        self.assertEqual(bot.replies[0][0][1], "welcome_en")
        self.assertEqual(bot.replies[0][1]["reply_markup"], {"welcome": 123})
        self.assertEqual(bot.sent_messages[0][0][1], "main_menu_ready")
        self.assertNotIn("create_test_config", events)

    def test_start_requests_language_when_telegram_language_is_unsupported(self):
        module, bot, events = load_tbot_module()

        module.send_welcome(self.make_message(language_code="de"))

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "language_selection_prompt")
        self.assertEqual(bot.replies[0][1]["reply_markup"], {"languages": True})

    def test_admin_start_renders_the_admin_root_keyboard(self):
        module, bot, events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123

        module.send_welcome(self.make_message())

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "Welcome to the Admin Dashboard!")
        self.assertEqual(
            bot.replies[0][1]["reply_markup"],
            {"markup": {"is_admin": True}},
        )

    def test_referred_user_with_unsupported_language_sees_language_prompt_first(self):
        module, bot, events = load_tbot_module()
        module.process_referral = lambda *args, **kwargs: (True, "referrer-456")
        deferred = []
        language_stub = types.ModuleType("utils.language")
        language_stub.defer_referral_confirmation = lambda user_id: deferred.append(user_id)
        sys.modules["utils.language"] = language_stub

        module.send_welcome(
            self.make_message(text="/start invite-code", language_code="de")
        )

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "language_selection_prompt")
        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(deferred, [123])

    def test_successful_referral_records_attribution_growth_event_once(self):
        module, bot, _events = load_tbot_module()
        captured = []
        module.process_referral = lambda *args, **kwargs: (True, "referrer-456")
        module.record_main_growth_event = (
            lambda event_type, user_id, **fields:
            captured.append((event_type, user_id, fields))
        )

        module.send_welcome(self.make_message(text="/start invite-code"))

        self.assertEqual(len(captured), 1)
        event_type, user_id, fields = captured[0]
        self.assertEqual(event_type, "referral_attributed")
        self.assertEqual(user_id, 123)
        self.assertEqual(fields["referral_campaign"], "main_invite")
        self.assertEqual(fields["referrer_id"], "referrer-456")
        self.assertEqual(
            fields["deduplication_key"],
            "main:referral_attributed:123",
        )
        self.assertTrue(any(
            args[1] == "referral_registered"
            for args, _kwargs in bot.sent_messages
        ))

    def test_orphaned_admin_cancel_restores_admin_main_keyboard(self):
        module, bot, events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        message = self.make_message(text="❌ Cancel")

        module.handle_admin_cancel_fallback(message)

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "Operation canceled.")
        self.assertEqual(
            bot.replies[0][1]["reply_markup"],
            {"markup": {"is_admin": True}},
        )

    def test_admin_cancel_fallback_filter_is_admin_and_exact_text_only(self):
        module, bot, _events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        handler = next(
            item for item in bot.message_handlers
            if item[0] is module.handle_admin_cancel_fallback
        )
        predicate = handler[2]["func"]

        self.assertTrue(predicate(self.make_message(text="❌ Cancel")))
        self.assertFalse(predicate(self.make_message(user_id=999, text="❌ Cancel")))
        self.assertFalse(predicate(self.make_message(text="Cancel")))

    def test_admin_category_navigation_renders_requested_view(self):
        module, bot, events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        message = self.make_message(text="👥 Users")

        module.handle_admin_menu_navigation(message)

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "👥 Users\nChoose an action:")
        self.assertEqual(
            bot.replies[0][1]["reply_markup"],
            {"admin_view": "users"},
        )

    def test_admin_home_navigation_restores_root_view(self):
        module, bot, events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123

        module.handle_admin_menu_navigation(
            self.make_message(text="🏠 Admin Menu")
        )

        self.assertEqual(events, ["reply"])
        self.assertEqual(bot.replies[0][0][1], "Admin dashboard is ready.")
        self.assertEqual(
            bot.replies[0][1]["reply_markup"],
            {"admin_view": "root"},
        )

    def test_admin_navigation_filter_rejects_non_admins_and_quick_actions(self):
        module, bot, _events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        handler = next(
            item for item in bot.message_handlers
            if item[0] is module.handle_admin_menu_navigation
        )
        predicate = handler[2]["func"]

        self.assertTrue(predicate(self.make_message(text="📣 Messaging")))
        self.assertFalse(
            predicate(self.make_message(user_id=999, text="📣 Messaging"))
        )
        self.assertFalse(predicate(self.make_message(text="✅ Confirmations")))

    def test_growth_funnel_handler_is_private_and_uses_reporting_api(self):
        module, bot, _events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        calls = []
        reporting = types.ModuleType("utils.growth_reporting")
        reporting.main_growth_comparison = lambda **kwargs: (
            calls.append(("report", kwargs)) or {"surface": "main"}
        )
        reporting.format_growth_comparison = lambda report, **kwargs: (
            calls.append(("format", report, kwargs)) or "private aggregate funnel"
        )
        sys.modules[reporting.__name__] = reporting

        message = self.make_message(text="📈 Growth Funnel")
        module.show_admin_growth_funnel(message)

        self.assertEqual(calls[0], ("report", {"days": 30}))
        self.assertEqual(calls[1][2]["title"], "Main bot growth funnel")
        self.assertEqual(bot.replies[-1][0][1], "private aggregate funnel")
        self.assertEqual(bot.replies[-1][1]["parse_mode"], "Markdown")

        reply_count = len(bot.replies)
        module.show_admin_growth_funnel(self.make_message(user_id=999, text="📈 Growth Funnel"))
        self.assertEqual(len(bot.replies), reply_count)

        handler = next(
            item for item in bot.message_handlers
            if item[0] is module.show_admin_growth_funnel
        )
        predicate = handler[2]["func"]
        self.assertTrue(predicate(message))
        self.assertFalse(predicate(self.make_message(user_id=999, text="📈 Growth Funnel")))
        self.assertFalse(predicate(self.make_message(text="Growth Funnel")))

    def test_growth_funnel_handler_fails_closed_without_breaking_admin_menu(self):
        module, bot, _events = load_tbot_module()
        module.is_admin = lambda user_id: user_id == 123
        reporting = types.ModuleType("utils.growth_reporting")
        reporting.main_growth_comparison = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("database unavailable")
        )
        reporting.format_growth_comparison = lambda *_args, **_kwargs: "unused"
        sys.modules[reporting.__name__] = reporting

        module.show_admin_growth_funnel(self.make_message(text="📈 Growth Funnel"))

        self.assertEqual(
            bot.replies[-1][0][1],
            "Growth funnel data is temporarily unavailable.",
        )
        self.assertEqual(
            bot.replies[-1][1]["reply_markup"],
            {"markup": {"is_admin": True}},
        )

if __name__ == "__main__":
    unittest.main()
