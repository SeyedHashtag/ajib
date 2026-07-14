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


if __name__ == "__main__":
    unittest.main()
