import importlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UTILS_DIR = ROOT / "core" / "scripts" / "telegrambot" / "utils"


class GrowthReportingTests(unittest.TestCase):
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
        self.events = importlib.import_module("utils.growth_events")
        self.reporting = importlib.import_module("utils.growth_reporting")
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

    def record_checkout(self, user_id, when, completed):
        self.events.record_growth_event(
            "checkout_started",
            user_id=user_id,
            occurred_at=when,
            deduplication_key=f"start-{user_id}-{when.date()}",
            path=self.path,
        )
        if completed:
            self.events.record_growth_event(
                "checkout_completed",
                user_id=user_id,
                occurred_at=when + timedelta(minutes=1),
                deduplication_key=f"complete-{user_id}-{when.date()}",
                path=self.path,
            )

    def test_comparison_uses_prior_equal_period_and_formats_aggregates(self):
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.record_checkout("old-1", end - timedelta(days=40), True)
        self.record_checkout("old-2", end - timedelta(days=39), False)
        self.record_checkout("new-1", end - timedelta(days=10), True)
        self.record_checkout("new-2", end - timedelta(days=9), True)

        report = self.reporting.main_growth_comparison(
            end_at=end,
            path=self.path,
        )
        checkout = report["funnels"]["checkout"]
        text = self.reporting.format_growth_comparison(
            report,
            funnel_names=("checkout",),
        )

        self.assertEqual(checkout["conversion_percent"], 100.0)
        self.assertEqual(checkout["baseline_conversion_percent"], 50.0)
        self.assertEqual(checkout["relative_change_percent"], 100.0)
        self.assertIn("2/2", text)
        self.assertIn("customer identities are not included", text)


if __name__ == "__main__":
    unittest.main()
