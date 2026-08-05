import importlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UTILS_DIR = ROOT / "core" / "scripts" / "telegrambot" / "utils"


class AccountCreditTests(unittest.TestCase):
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
        self.credit = importlib.import_module("utils.account_credit")
        self.database = importlib.import_module("utils.database")
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

    def test_credit_is_idempotent_and_rejects_transaction_reuse(self):
        first = self.credit.credit_account(
            "7", 5, "recruitment:9", source="recruitment", path=self.path
        )
        second = self.credit.credit_account(
            "7", 5, "recruitment:9", source="recruitment", path=self.path
        )

        self.assertEqual(first["available"], 5.0)
        self.assertEqual(second["available"], 5.0)
        with self.assertRaises(ValueError):
            self.credit.credit_account("7", 6, "recruitment:9", path=self.path)

    def test_reserve_release_and_partial_reservation(self):
        self.credit.credit_account("7", 5, "seed", path=self.path)

        reserved = self.credit.reserve_account_credit(
            "7", "checkout-1", 8, order_id="order-1", path=self.path
        )
        state = self.credit.get_account_credit("7", path=self.path)
        self.assertEqual(reserved, 5.0)
        self.assertEqual(state["available"], 0.0)
        self.assertEqual(state["reserved"], 5.0)
        self.assertEqual(
            self.credit.reserve_account_credit("7", "checkout-1", 8, path=self.path),
            5.0,
        )

        self.assertTrue(
            self.credit.release_account_credit("7", "checkout-1", path=self.path)
        )
        self.assertFalse(
            self.credit.release_account_credit("7", "checkout-1", path=self.path)
        )
        state = self.credit.get_account_credit("7", path=self.path)
        self.assertEqual(state["available"], 5.0)
        self.assertEqual(state["reserved"], 0.0)

    def test_consumption_is_idempotent_and_audited(self):
        self.credit.credit_account("7", 5, "seed", path=self.path)
        self.credit.reserve_account_credit(
            "7", "checkout-1", 3, order_id="order-1", path=self.path
        )

        self.assertEqual(
            self.credit.consume_account_credit(
                "7", "checkout-1", order_id="order-1", path=self.path
            ),
            3.0,
        )
        self.assertEqual(
            self.credit.consume_account_credit(
                "7", "checkout-1", order_id="order-1", path=self.path
            ),
            3.0,
        )
        state = self.credit.get_account_credit("7", path=self.path)
        self.assertEqual(state["available"], 2.0)
        self.assertEqual(state["reserved"], 0.0)
        history = self.credit.list_account_credit_transactions("7", path=self.path)
        self.assertEqual([item["kind"] for item in history], ["credit", "consume"])
        self.assertEqual(history[-1]["amount"], -3.0)


if __name__ == "__main__":
    unittest.main()
