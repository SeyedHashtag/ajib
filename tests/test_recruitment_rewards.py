import importlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UTILS_DIR = ROOT / "core" / "scripts" / "telegrambot" / "utils"


class RecruitmentRewardTests(unittest.TestCase):
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
        package = importlib.import_module("types").ModuleType("utils")
        package.__path__ = [str(UTILS_DIR)]
        sys.modules["utils"] = package
        self.recruitment = importlib.import_module("utils.recruitment")
        self.database = importlib.import_module("utils.database")
        self.path = str(Path(self.temp.name) / "ajib.db")
        self.attribution = {
            "referrer_user_id": "10",
            "referral_code": "CODE",
            "campaign_type": "reseller",
        }
        self.addCleanup(self.restore_modules)

    def restore_modules(self):
        try:
            self.database.close_connections()
        finally:
            for name in list(sys.modules):
                if name == "utils" or name.startswith("utils."):
                    sys.modules.pop(name, None)
            sys.modules.update(self.saved_modules)

    def reseller(self, sales=5, total_paid=100):
        configs = [
            {"username": f"r20{chr(97 + index)}", "plan_gb": "10"}
            for index in range(sales)
        ]
        return {"status": "approved", "configs": configs, "total_paid": total_paid}

    def test_only_non_test_non_removed_configs_count_as_sales(self):
        data = self.reseller(sales=3)
        data["configs"].extend([
            {"username": "ht20", "plan_gb": "1"},
            {"username": "r20z", "plan_gb": "10", "removed": True},
        ])

        self.assertEqual(self.recruitment.productive_sales_count(data), 3)

    def test_milestone_tracks_progress_and_qualifies_once(self):
        tracking = self.recruitment.evaluate_recruitment_milestone(
            "20",
            self.reseller(sales=4, total_paid=99),
            attribution=self.attribution,
            path=self.path,
        )
        qualified = self.recruitment.evaluate_recruitment_milestone(
            "20",
            self.reseller(),
            attribution=self.attribution,
            path=self.path,
        )
        repeated = self.recruitment.evaluate_recruitment_milestone(
            "20",
            self.reseller(sales=6, total_paid=120),
            attribution=self.attribution,
            path=self.path,
        )

        self.assertEqual(tracking["status"], "tracking")
        self.assertFalse(tracking["newly_qualified"])
        self.assertEqual(qualified["status"], "qualified")
        self.assertTrue(qualified["newly_qualified"])
        self.assertFalse(repeated["newly_qualified"])
        self.assertEqual(repeated["reward_amount"], 5.0)
        self.assertEqual(
            self.recruitment.claimable_recruitment_rewards("10", path=self.path)[0]["reseller_id"],
            "20",
        )

    def test_attribution_is_immutable_and_self_referral_is_rejected(self):
        original = self.recruitment.evaluate_recruitment_milestone(
            "20",
            self.reseller(sales=4, total_paid=90),
            attribution=self.attribution,
            path=self.path,
        )
        changed = self.recruitment.evaluate_recruitment_milestone(
            "20",
            self.reseller(),
            attribution={
                "referrer_user_id": "11",
                "referral_code": "OTHER",
                "campaign_type": "reseller",
            },
            path=self.path,
        )
        self_referral = self.recruitment.evaluate_recruitment_milestone(
            "30",
            self.reseller(),
            attribution={"referrer_user_id": "30"},
            path=self.path,
        )

        self.assertEqual(original["referrer_id"], "10")
        self.assertEqual(changed["referrer_id"], "10")
        self.assertEqual(changed["metadata"]["referral_code"], "CODE")
        self.assertEqual(
            [item["reseller_id"] for item in self.recruitment.claimable_recruitment_rewards(
                "10", path=self.path
            )],
            ["20"],
        )
        self.assertEqual(
            self.recruitment.claimable_recruitment_rewards("11", path=self.path),
            [],
        )
        self.assertIsNone(self_referral)

    def test_cash_claim_is_idempotent_and_choice_is_immutable(self):
        self.recruitment.evaluate_recruitment_milestone(
            "20", self.reseller(), attribution=self.attribution, path=self.path
        )
        calls = []

        def cash_creditor(user_id, amount, reward_id, metadata):
            calls.append((user_id, amount, reward_id, metadata))
            return True

        claimed = self.recruitment.claim_recruitment_reward(
            "10", "20", "cash", path=self.path, cash_creditor=cash_creditor
        )
        repeated = self.recruitment.claim_recruitment_reward(
            "10", "20", "cash", path=self.path, cash_creditor=cash_creditor
        )

        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(repeated["choice"], "cash")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(
            self.recruitment.claim_recruitment_reward(
                "10", "20", "credit", path=self.path, cash_creditor=cash_creditor
            )
        )

    def test_purchase_credit_claim_uses_account_credit_ledger(self):
        self.recruitment.evaluate_recruitment_milestone(
            "20", self.reseller(), attribution=self.attribution, path=self.path
        )

        claimed = self.recruitment.claim_recruitment_reward(
            "10", "20", "credit", path=self.path
        )
        credit = importlib.import_module("utils.account_credit").get_account_credit(
            "10", path=self.path
        )

        self.assertEqual(claimed["choice"], "credit")
        self.assertEqual(credit["available"], 5.0)


if __name__ == "__main__":
    unittest.main()
