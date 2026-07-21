import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
GUIDANCE_PATH = BOT_DIR / "utils" / "download_guidance.py"
TRANSLATIONS_PATH = BOT_DIR / "utils" / "translations.py"


class DummyButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")
        self.url = kwargs.get("url")


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *buttons, **kwargs):
        self.buttons.extend(buttons)


class DummyBot:
    def __init__(self, fail_send=False):
        self.fail_send = fail_send
        self.sent = []
        self.replies = []
        self.edits = []
        self.answers = []

    def send_message(self, *args, **kwargs):
        if self.fail_send:
            raise RuntimeError("telegram unavailable")
        self.sent.append((args, kwargs))

    def reply_to(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    def answer_callback_query(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def _translation_catalog():
    tree = ast.parse(TRANSLATIONS_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MESSAGE_TRANSLATIONS"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("MESSAGE_TRANSLATIONS was not found")


TRANSLATIONS = _translation_catalog()


def _function_node(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _named_call_count(function_node, name):
    return sum(
        1
        for node in ast.walk(function_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _load_guidance_module():
    saved = {
        name: sys.modules.get(name)
        for name in ("telebot", "utils", "utils.translations")
    }
    try:
        telebot_stub = types.ModuleType("telebot")
        telebot_stub.types = types.SimpleNamespace(
            InlineKeyboardMarkup=DummyMarkup,
            InlineKeyboardButton=DummyButton,
        )
        sys.modules["telebot"] = telebot_stub

        utils_stub = types.ModuleType("utils")
        utils_stub.__path__ = []
        sys.modules["utils"] = utils_stub

        translations_stub = types.ModuleType("utils.translations")
        translations_stub.get_message_text = lambda language, key: TRANSLATIONS.get(
            language, TRANSLATIONS["en"]
        ).get(key, TRANSLATIONS["en"].get(key, ""))
        sys.modules["utils.translations"] = translations_stub

        spec = importlib.util.spec_from_file_location(
            "download_guidance_under_test", GUIDANCE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


GUIDANCE = _load_guidance_module()


class DownloadCatalogTests(unittest.TestCase):
    def test_ios_keeps_happ_and_recommends_karing_first(self):
        ios_apps = GUIDANCE.DOWNLOAD_CATALOG["ios"]

        self.assertEqual([app["id"] for app in ios_apps], ["karing", "happ"])
        markup = GUIDANCE.build_app_markup("en", "ios")
        self.assertEqual(markup.buttons[0].text, "⭐ Karing — Recommended")
        self.assertEqual(markup.buttons[1].text, "Happ")
        self.assertIn("some ISPs", GUIDANCE.get_app_list_text("en", "ios"))

    def test_catalog_uses_official_stable_download_pages(self):
        self.assertEqual(
            GUIDANCE.DOWNLOAD_CATALOG["ios"][0]["url"],
            "https://apps.apple.com/us/app/karing/id6472431552",
        )
        self.assertEqual(
            GUIDANCE.DOWNLOAD_CATALOG["ios"][1]["url"],
            "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
        )
        self.assertTrue(GUIDANCE.DOWNLOAD_CATALOG["android"][0]["url"].endswith("/releases/latest"))
        self.assertTrue(GUIDANCE.DOWNLOAD_CATALOG["windows"][0]["url"].endswith("/releases/latest"))
        self.assertEqual(
            GUIDANCE.parse_download_callback("download:app:v2ray:android"),
            {"action": "app", "platform": "android", "app_id": "v2ray"},
        )

    def test_new_download_copy_exists_in_every_supported_language(self):
        keys = {
            "download_ios_app_list",
            "download_android_app_list",
            "download_windows_app_list",
            "download_karing_recommended",
            "download_happ",
            "download_open_link",
            "download_back_platforms",
            "download_back_apps",
            "download_invalid_selection",
            "download_error",
            "download_karing_ios_tutorial",
            "download_happ_ios_details",
            "download_v2rayng_android_details",
            "download_v2rayn_windows_details",
        }

        for language in ("en", "fa", "ru", "tk"):
            with self.subTest(language=language):
                self.assertTrue(keys.issubset(TRANSLATIONS[language]))
                self.assertTrue(all(TRANSLATIONS[language][key].strip() for key in keys))

    def test_karing_tutorial_has_the_requested_order_without_security_warning(self):
        tutorial = GUIDANCE.get_app_details_text("en", "ios", "karing")
        steps = (
            "country or region where you actually live",
            "Copy the config from Telegram",
            "`Add Profile` → `Import From Clipboard`",
            "open `Settings`",
            "`Novice Mode`",
            "`TLS`",
            "`Skip Certificate Validation`",
            "Return to the main page and connect",
        )

        positions = [tutorial.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

        for language in ("en", "fa", "ru", "tk"):
            with self.subTest(language=language):
                localized_tutorial = GUIDANCE.get_app_details_text(language, "ios", "karing")
                self.assertNotIn("⚠️", localized_tutorial)


class DownloadNavigationTests(unittest.TestCase):
    def make_call(self, data):
        return types.SimpleNamespace(
            data=data,
            id="callback",
            from_user=types.SimpleNamespace(id=10),
            message=types.SimpleNamespace(
                chat=types.SimpleNamespace(id=10),
                message_id=20,
            ),
        )

    def test_hosted_callback_namespace_navigates_platform_app_and_back(self):
        bot = DummyBot()

        GUIDANCE.render_download_callback(
            bot, self.make_call("hb:download:ios"), "en", "hb:download"
        )
        app_markup = bot.edits[-1][1]["reply_markup"]
        self.assertEqual(app_markup.buttons[0].callback_data, "hb:download:app:karing:ios")
        self.assertEqual(app_markup.buttons[1].callback_data, "hb:download:app:happ:ios")

        GUIDANCE.render_download_callback(
            bot,
            self.make_call("hb:download:app:karing:ios"),
            "en",
            "hb:download",
        )
        details_markup = bot.edits[-1][1]["reply_markup"]
        self.assertEqual(details_markup.buttons[0].url, GUIDANCE.DOWNLOAD_CATALOG["ios"][0]["url"])
        self.assertEqual(details_markup.buttons[1].callback_data, "hb:download:ios")

        GUIDANCE.render_download_callback(
            bot, self.make_call("hb:download:back"), "en", "hb:download"
        )
        platform_markup = bot.edits[-1][1]["reply_markup"]
        self.assertEqual(
            [button.callback_data for button in platform_markup.buttons],
            ["hb:download:ios", "hb:download:android", "hb:download:windows"],
        )

    def test_delivery_prompt_is_best_effort(self):
        bot = DummyBot(fail_send=True)

        result = GUIDANCE.send_download_prompt_safely(bot, 10, "en")

        self.assertIsNone(result)


class ConfigDeliveryWiringTests(unittest.TestCase):
    def test_each_primary_customer_delivery_category_uses_the_shared_prompt(self):
        utils_dir = BOT_DIR / "utils"
        expected = {
            utils_dir / "test_config.py": ("_send_created_test_config",),
            utils_dir / "my_configs.py": ("display_config",),
            utils_dir / "purchase_plan.py": (
                "_process_customer_renewal_payment",
                "_process_admin_approval_job",
                "_process_check_payment_job",
                "process_payment_webhook",
                "check_pending_payments",
            ),
        }

        for path, functions in expected.items():
            for function_name in functions:
                with self.subTest(file=path.name, function=function_name):
                    function = _function_node(path, function_name)
                    self.assertEqual(
                        _named_call_count(function, "send_download_prompt_safely"),
                        1,
                    )

    def test_hosted_owner_generation_explicitly_opts_out(self):
        worker_path = BOT_DIR / "hosted_worker.py"
        owner_function = _function_node(worker_path, "owner_generate_input")
        delivery_calls = [
            node
            for node in ast.walk(owner_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_deliver_config"
        ]

        self.assertEqual(len(delivery_calls), 1)
        keywords = {keyword.arg: keyword.value for keyword in delivery_calls[0].keywords}
        self.assertIn("include_downloads", keywords)
        self.assertIsInstance(keywords["include_downloads"], ast.Constant)
        self.assertIs(keywords["include_downloads"].value, False)


if __name__ == "__main__":
    unittest.main()
