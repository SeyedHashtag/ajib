import ast
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
TRANSLATIONS_PATH = BOT_DIR / "utils" / "hosted_translations.py"
SPEC = importlib.util.spec_from_file_location("hosted_translations", TRANSLATIONS_PATH)
TRANSLATIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSLATIONS)
HOSTED_TRANSLATIONS = TRANSLATIONS.HOSTED_TRANSLATIONS
hosted_text = TRANSLATIONS.hosted_text
WORKER_SOURCE = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")
WORKER_TREE = ast.parse(WORKER_SOURCE)


def _worker_function(name):
    return next(node for node in WORKER_TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _worker_constant(name):
    for node in WORKER_TREE.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing worker constant: {name}")


def _button_translations():
    source = (BOT_DIR / "utils" / "translations.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BUTTON_TRANSLATIONS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("Missing BUTTON_TRANSLATIONS")


class HostedStorefrontTranslationTests(unittest.TestCase):
    def test_every_supported_language_has_the_complete_hosted_catalog(self):
        expected = set(HOSTED_TRANSLATIONS["en"])

        for language, catalog in HOSTED_TRANSLATIONS.items():
            with self.subTest(language=language):
                self.assertEqual(set(catalog), expected)
                self.assertTrue(all(str(value).strip() for value in catalog.values()))

    def test_dynamic_owner_messages_format_in_every_language(self):
        for language in HOSTED_TRANSLATIONS:
            with self.subTest(language=language):
                summary = hosted_text(
                    language, "owner_summary", markup=25, crypto="enabled", card="1234", referral=20
                )
                receipt = hosted_text(
                    language, "receipt_owner_caption", user_id=7, plan_gb=30, days=30,
                    toman_price="1,250,000",
                )
                self.assertIn("25", summary)
                self.assertIn("1,250,000", receipt)

    def test_non_english_owner_guides_are_translated(self):
        english = HOSTED_TRANSLATIONS["en"]["owner_guide"]

        for language in ("fa", "ru", "tk"):
            with self.subTest(language=language):
                self.assertNotEqual(HOSTED_TRANSLATIONS[language]["owner_guide"], english)

    def test_customer_copy_never_exposes_the_reseller_relationship(self):
        customer_keys = {
            "purchase_unavailable",
            "credit_unavailable",
            "crypto_disabled",
            "payment_processing",
            "paid_needs_attention",
            "receipt_rejected",
            "config_ready",
            "config_no_url",
            "support_default",
            "referral_withdrawal_result",
        }
        forbidden_terms = {
            "en": ("reseller", "wholesale", "operator"),
            "fa": ("نماینده", "عمده", "اپراتور"),
            "ru": ("реселлер", "оптов", "оператор"),
            "tk": ("satyjy", "lomaý", "operator"),
        }

        for language, terms in forbidden_terms.items():
            customer_copy = "\n".join(
                HOSTED_TRANSLATIONS[language][key] for key in customer_keys
            ).casefold()
            for term in terms:
                with self.subTest(language=language, term=term):
                    self.assertNotIn(term.casefold(), customer_copy)


class HostedStorefrontParityTests(unittest.TestCase):
    def test_checkout_keeps_main_bot_navigation_and_payment_context(self):
        source = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")

        for behavior in (
            'callback_data="hb:plans"',
            'callback_data=f"hb:cancel:{order_id}"',
            '"card_to_card_payment"',
            '"payment_instructions"',
            '"purchase_connection_warning"',
            "qrcode.make(url)",
            "format_toman_amount",
        ):
            with self.subTest(behavior=behavior):
                self.assertIn(behavior, source)

    def test_customer_handlers_do_not_use_old_reseller_disclosures(self):
        source = (BOT_DIR / "hosted_worker.py").read_text(encoding="utf-8")

        self.assertNotIn("Contact the reseller for support", source)
        self.assertNotIn("by the reseller", source)

    def test_owner_panel_uses_reply_keyboard_and_keeps_dynamic_controls_inline(self):
        owner_markup = ast.get_source_segment(WORKER_SOURCE, _worker_function("_owner_markup"))
        owner_menu_text = ast.get_source_segment(WORKER_SOURCE, _worker_function("_owner_menu_text"))

        self.assertIn("types.ReplyKeyboardMarkup(resize_keyboard=True)", owner_markup)
        self.assertNotIn("types.InlineKeyboardMarkup", owner_markup)
        self.assertIn("for row in OWNER_MENU_ROWS", owner_markup)
        self.assertIn('_button(user_id, "back", "🔙 Back")', owner_menu_text)
        for callback in ("hb:ogen:", "hb:plantoggle:", "hb:earn:", "hb:refresolve:"):
            with self.subTest(callback=callback):
                self.assertIn(callback, WORKER_SOURCE)

    def test_owner_reply_commands_map_every_localized_label(self):
        menu_rows = _worker_constant("OWNER_MENU_ROWS")
        setting_keys = _worker_constant("OWNER_SETTING_KEYS")
        menu_keys = tuple(key for row in menu_rows for key in row)
        action_keys = tuple(key for key in menu_keys if key not in setting_keys and key != "back")
        button_translations = _button_translations()
        namespace = {
            "HOSTED_TRANSLATIONS": HOSTED_TRANSLATIONS,
            "OWNER_MENU_ROWS": menu_rows,
            "OWNER_SETTING_KEYS": setting_keys,
            "_all_button_values": lambda key, fallback: {
                catalog.get(key, fallback) for catalog in button_translations.values()
            },
        }
        module = ast.Module(body=[_worker_function("_owner_menu_command")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)
        command_for = namespace["_owner_menu_command"]

        self.assertEqual(
            menu_rows,
            (
                ("generate", "customers"),
                ("debt", "markup"),
                ("card", "rate"),
                ("support", "welcome"),
                ("refpercent", "plans"),
                ("crypto", "earnings"),
                ("referrals", "back"),
            ),
        )
        self.assertTrue(all(len(row) == 2 for row in menu_rows))
        self.assertEqual(
            set(setting_keys),
            {"markup", "card", "rate", "support", "welcome", "refpercent"},
        )
        for language, catalog in HOSTED_TRANSLATIONS.items():
            for key in action_keys:
                with self.subTest(language=language, action=key):
                    self.assertEqual(command_for(catalog[key]), ("action", key))
            for key in setting_keys:
                with self.subTest(language=language, setting=key):
                    self.assertEqual(command_for(catalog[key]), ("setting", key))
            with self.subTest(language=language, action="back"):
                self.assertEqual(command_for(button_translations[language]["back"]), ("back", None))

    def test_owner_back_restores_main_menu_and_legacy_callbacks_remain(self):
        owner_menu = ast.get_source_segment(WORKER_SOURCE, _worker_function("owner_menu_action"))

        self.assertIn("INPUT_STATE.pop(OWNER_ID, None)", owner_menu)
        self.assertIn("reply_markup=_main_markup(OWNER_ID)", owner_menu)
        self.assertIn('c.data.startswith("hb:owner:")', WORKER_SOURCE)
        self.assertIn('c.data.startswith("hb:setting:")', WORKER_SOURCE)


if __name__ == "__main__":
    unittest.main()
