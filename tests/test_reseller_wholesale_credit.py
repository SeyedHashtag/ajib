import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
UTILS_DIR = ROOT / "core" / "scripts" / "telegrambot" / "utils"


class ResellerWholesaleCreditTests(unittest.TestCase):
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
        self.db_path = str(Path(self.temp.name) / "ajib.db")
        self.env = mock.patch.dict(os.environ, {"AJIB_DB_PATH": self.db_path})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.wholesale = importlib.import_module("utils.reseller_wholesale_credit")
        self.account_credit = importlib.import_module("utils.account_credit")
        self.database = importlib.import_module("utils.database")
        self.addCleanup(self.restore_modules)

    def restore_modules(self):
        try:
            self.database.close_connections()
        finally:
            for name in list(sys.modules):
                if name == "utils" or name.startswith("utils."):
                    sys.modules.pop(name, None)
            sys.modules.update(self.saved_modules)

    def test_nonwithdrawable_balance_reserve_consume_and_duplicate_callback(self):
        self.wholesale.credit_wholesale_balance("7", 10, "topup-1", source="crypto")

        self.assertEqual(self.wholesale.reserve_wholesale_balance("7", "order-1", 6), 6.0)
        self.assertEqual(self.wholesale.reserve_wholesale_balance("7", "order-1", 6), 6.0)
        reserved = self.wholesale.get_wholesale_balance("7")
        self.assertEqual(reserved["available"], 4.0)
        self.assertEqual(reserved["reserved"], 6.0)

        self.assertEqual(self.wholesale.consume_wholesale_balance("7", "order-1"), 6.0)
        self.assertEqual(self.wholesale.consume_wholesale_balance("7", "order-1"), 6.0)
        consumed = self.wholesale.get_wholesale_balance("7")
        self.assertEqual(consumed["available"], 4.0)
        self.assertEqual(consumed["reserved"], 0.0)
        self.assertFalse(self.wholesale.release_wholesale_balance("7", "order-1"))

    def test_failed_order_releases_the_exact_reservation(self):
        self.wholesale.credit_wholesale_balance("7", 5, "topup-1", source="card")
        self.assertEqual(self.wholesale.reserve_wholesale_balance("7", "failed-order", 4), 4.0)

        self.assertTrue(self.wholesale.release_wholesale_balance("7", "failed-order"))
        self.assertFalse(self.wholesale.release_wholesale_balance("7", "failed-order"))
        balance = self.wholesale.get_wholesale_balance("7")
        self.assertEqual(balance["available"], 5.0)
        self.assertEqual(balance["reserved"], 0.0)

    def test_purchase_credit_transfer_is_one_to_one_and_idempotent(self):
        self.account_credit.credit_account("7", 8, "purchase-refund")

        first = self.wholesale.transfer_purchase_credit_to_wholesale("7", 5, "move-1")
        duplicate = self.wholesale.transfer_purchase_credit_to_wholesale("7", 5, "move-1")

        self.assertEqual(first["available"], 5.0)
        self.assertEqual(duplicate["available"], 5.0)
        self.assertEqual(self.account_credit.get_account_credit("7")["available"], 3.0)
        self.assertEqual(self.wholesale.get_wholesale_balance("7")["available"], 5.0)


if __name__ == "__main__":
    unittest.main()
