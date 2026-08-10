import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UTILS_DIR = ROOT / "core" / "scripts" / "telegrambot" / "utils"


class PurchaseIncentiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "utils" or name.startswith("utils.")
        }
        for name in list(sys.modules):
            if name == "utils" or name.startswith("utils."):
                sys.modules.pop(name, None)
        package = types.ModuleType("utils")
        package.__path__ = [str(UTILS_DIR)]
        sys.modules["utils"] = package
        self.referral = importlib.import_module("utils.referral")
        self.incentives = importlib.import_module("utils.purchase_incentives")
        self.credit = importlib.import_module("utils.account_credit")
        self.database = importlib.import_module("utils.database")
        self.referral.REFERRALS_FILE = str(Path(self.temp.name) / "referrals.json")
        self.path = str(Path(self.temp.name) / "ajib.db")
        self.addCleanup(self.restore_modules)

    def restore_modules(self):
        try:
            self.database.close_connections()
        finally:
            for name in list(sys.modules):
                if name == "utils" or name.startswith("utils."):
                    sys.modules.pop(name, None)
            sys.modules.update(self.saved_modules)

    def refer(self, referrer="10", invitee="20"):
        code = self.referral.get_or_create_referral_code(referrer)
        self.assertTrue(self.referral.process_referral(invitee, code)[0])

    def test_crypto_invite_discount_stacks_to_cap_and_uses_collected_reward_base(self):
        self.refer()
        self.credit.credit_account("20", 2, "seed", path=self.path)

        quote = self.incentives.reserve_main_checkout(
            "20",
            "checkout-1",
            10,
            payment_method="crypto",
            payment_discount_percent=5,
            path=self.path,
        )

        self.assertEqual(quote["invite_discount_percent"], 5.0)
        self.assertEqual(quote["total_discount_percent"], 10.0)
        self.assertEqual(quote["discounted_total"], 9.0)
        self.assertEqual(quote["account_credit_reserved"], 2.0)
        self.assertEqual(quote["collected_amount"], 7.0)
        result = self.incentives.finalize_main_checkout(
            "payment-1",
            {"user_id": "20", "plan_gb": "10", **quote},
            path=self.path,
        )
        repeated = self.incentives.finalize_main_checkout(
            "payment-1",
            {"user_id": "20", "plan_gb": "10", **quote},
            path=self.path,
        )

        self.assertEqual(result["credit_consumed"], 2.0)
        self.assertTrue(result["invite_redeemed"])
        self.assertEqual(result["reward_amount"], 1.4)
        self.assertFalse(repeated["reward_created"])
        self.assertEqual(self.credit.get_account_credit("20", path=self.path)["reserved"], 0)
        self.assertEqual(self.referral.get_referral_stats("10")["available_balance"], 1.4)
        self.assertFalse(self.referral.get_invitee_discount_eligibility("20")["eligible"])

    def test_maximum_main_incentives_retain_at_least_seventy_two_percent(self):
        self.refer()
        quote = self.incentives.reserve_main_checkout(
            "20",
            "checkout-retention",
            10,
            payment_method="crypto",
            payment_discount_percent=5,
            allow_account_credit=False,
            path=self.path,
        )
        result = self.incentives.finalize_main_checkout(
            "payment-retention",
            {"user_id": "20", "plan_gb": "10", **quote},
            path=self.path,
        )

        retained = quote["collected_amount"] - result["reward_amount"]
        self.assertEqual(quote["total_discount_percent"], 10.0)
        self.assertEqual(result["reward_amount"], 1.8)
        self.assertEqual(retained, 7.2)
        self.assertGreaterEqual(retained / quote["original_price"], 0.72)

    def test_cancel_releases_discount_and_credit_for_a_new_checkout(self):
        self.refer()
        self.credit.credit_account("20", 5, "seed", path=self.path)
        quote = self.incentives.reserve_main_checkout(
            "20",
            "checkout-1",
            10,
            payment_method="card",
            path=self.path,
        )
        self.assertEqual(quote["account_credit_reserved"], 5.0)

        released = self.incentives.release_main_checkout(
            "20", "checkout-1", path=self.path
        )

        self.assertTrue(released["invite_released"])
        self.assertTrue(released["credit_released"])
        self.assertEqual(self.credit.get_account_credit("20", path=self.path)["available"], 5.0)
        self.assertTrue(self.referral.get_invitee_discount_eligibility("20")["eligible"])

    def test_non_referred_card_quote_has_no_discount(self):
        quote = self.incentives.reserve_main_checkout(
            "20",
            "checkout-1",
            10,
            payment_method="card",
            allow_account_credit=False,
            path=self.path,
        )

        self.assertEqual(quote["original_price"], 10.0)
        self.assertEqual(quote["total_discount_percent"], 0.0)
        self.assertEqual(quote["collected_amount"], 10.0)
        self.assertIsNone(quote["referrer_id"])

    def test_direct_renewal_card_and_crypto_discounts_use_catalog_price(self):
        card_quote = self.incentives.reserve_main_checkout(
            "20",
            "renewal-card",
            12,
            payment_method="card",
            renewal_discount_percent=10,
            discount_cap_percent=15,
            allow_invite_discount=False,
            allow_account_credit=False,
            path=self.path,
        )
        crypto_quote = self.incentives.reserve_main_checkout(
            "20",
            "renewal-crypto",
            12,
            payment_method="crypto",
            payment_discount_percent=5,
            renewal_discount_percent=10,
            discount_cap_percent=15,
            allow_invite_discount=False,
            allow_account_credit=False,
            path=self.path,
        )

        self.assertEqual(card_quote["original_price"], 12.0)
        self.assertEqual(card_quote["renewal_discount_percent"], 10.0)
        self.assertEqual(card_quote["renewal_discount_amount"], 1.2)
        self.assertEqual(card_quote["crypto_discount_percent"], 0.0)
        self.assertEqual(card_quote["total_discount_percent"], 10.0)
        self.assertEqual(card_quote["discounted_total"], 10.8)
        self.assertEqual(crypto_quote["original_price"], 12.0)
        self.assertEqual(crypto_quote["renewal_discount_percent"], 10.0)
        self.assertEqual(crypto_quote["renewal_discount_amount"], 1.2)
        self.assertEqual(crypto_quote["crypto_discount_percent"], 5.0)
        self.assertEqual(crypto_quote["crypto_discount_amount"], 0.6)
        self.assertEqual(crypto_quote["total_discount_percent"], 15.0)
        self.assertEqual(crypto_quote["discount_amount"], 1.8)
        self.assertEqual(crypto_quote["total_discount_amount"], 1.8)
        self.assertEqual(crypto_quote["discounted_total"], 10.2)

    def test_renewal_credit_is_applied_after_the_full_crypto_discount(self):
        self.credit.credit_account("20", 12, "seed", path=self.path)

        quote = self.incentives.reserve_main_checkout(
            "20",
            "renewal-credit",
            12,
            payment_method="crypto",
            payment_discount_percent=5,
            renewal_discount_percent=10,
            discount_cap_percent=15,
            allow_invite_discount=False,
            path=self.path,
        )

        self.assertEqual(quote["discounted_total"], 10.2)
        self.assertEqual(quote["account_credit_reserved"], 10.2)
        self.assertEqual(quote["collected_amount"], 0.0)
        self.assertEqual(quote["total_discount_percent"], 15.0)

    def test_renewal_rounding_keeps_discount_amount_components_exact(self):
        quote = self.incentives.reserve_main_checkout(
            "20",
            "renewal-rounding",
            0.05,
            payment_method="crypto",
            payment_discount_percent=5,
            renewal_discount_percent=10,
            discount_cap_percent=15,
            allow_invite_discount=False,
            allow_account_credit=False,
            path=self.path,
        )

        self.assertEqual(quote["discount_amount"], 0.01)
        self.assertEqual(
            quote["renewal_discount_amount"] + quote["payment_discount_amount"],
            quote["discount_amount"],
        )
        self.assertEqual(quote["discounted_total"], 0.04)

    def test_capped_components_are_actual_and_sum_after_cent_rounding(self):
        self.refer()
        with patch.dict(os.environ, {
            "AJIB_REFERRAL_BUYER_DISCOUNT_PERCENT": "7",
            "AJIB_COMBINED_DISCOUNT_CAP_PERCENT": "10",
        }):
            quote = self.incentives.reserve_main_checkout(
                "20",
                "checkout-capped",
                0.05,
                payment_method="crypto",
                payment_discount_percent=5,
                allow_account_credit=False,
                path=self.path,
            )

        self.assertEqual(quote["total_discount_percent"], 10.0)
        self.assertEqual(quote["invite_discount_percent"], 7.0)
        self.assertEqual(quote["payment_discount_percent"], 3.0)
        self.assertEqual(quote["discount_amount"], 0.01)
        self.assertEqual(
            quote["invite_discount_amount"] + quote["payment_discount_amount"],
            quote["discount_amount"],
        )
        self.assertEqual(quote["collected_amount"], 0.04)


if __name__ == "__main__":
    unittest.main()
