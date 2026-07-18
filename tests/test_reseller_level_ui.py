import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
UTILS_DIR = BOT_DIR / "utils"


class DummyBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def send_message(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("Telegram unavailable")
        self.messages.append((args, kwargs))
        return object()


class ResellerLevelPresentationTests(unittest.TestCase):
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
        self.reseller = importlib.import_module("utils.reseller")
        self.level_ui = importlib.import_module("utils.reseller_level_ui")
        self.reseller.RESELLERS_FILE = str(
            Path(self.temp.name) / "resellers.json"
        )
        self.addCleanup(self.restore_modules)

    def restore_modules(self):
        for name in list(sys.modules):
            if name == "utils" or name.startswith("utils."):
                sys.modules.pop(name, None)
        sys.modules.update(self.saved_modules)

    def write_reseller(self, data):
        Path(self.reseller.RESELLERS_FILE).write_text(
            json.dumps({"7": data}),
            encoding="utf-8",
        )

    def read_reseller(self):
        return json.loads(
            Path(self.reseller.RESELLERS_FILE).read_text(encoding="utf-8")
        )["7"]

    def test_failed_delivery_releases_claim_and_success_is_presented_once(self):
        self.write_reseller({
            "status": "approved",
            "debt": 0,
            "total_paid": 20,
            "configs": [],
        })

        self.assertFalse(
            self.level_ui.present_pending_reseller_level(
                DummyBot(fail=True),
                "7",
                "en",
            )
        )
        after_failure = self.read_reseller()
        self.assertEqual(after_failure["last_presented_reseller_level"], 0)
        self.assertIsNone(after_failure["reseller_level_presentation_claim"])

        bot = DummyBot()
        self.assertTrue(
            self.level_ui.present_pending_reseller_level(bot, "7", "en")
        )
        self.assertFalse(
            self.level_ui.present_pending_reseller_level(bot, "7", "en")
        )
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Level 3/6", bot.messages[0][0][1])
        self.assertEqual(
            self.read_reseller()["last_presented_reseller_level"],
            3,
        )

    def test_multi_level_jump_presents_only_the_final_level(self):
        self.write_reseller({
            "status": "approved",
            "debt": 0,
            "total_paid": 20,
            "last_presented_reseller_level": 3,
            "configs": [],
        })
        saved = self.read_reseller()
        saved["total_paid"] = 50
        self.write_reseller(saved)

        bot = DummyBot()
        self.assertTrue(
            self.level_ui.present_pending_reseller_level(
                bot,
                "7",
                "en",
                allow_introduction=False,
            )
        )
        message = bot.messages[0][0][1]
        self.assertIn("from Level 3", message)
        self.assertIn("Level 6/6", message)
        self.assertEqual(len(bot.messages), 1)

    def test_profile_and_roadmap_render_in_every_supported_language(self):
        record = {
            "status": "approved",
            "debt": 2,
            "total_paid": 50,
            "configs": [],
        }
        for language in ("en", "fa", "ru", "tk"):
            with self.subTest(language=language):
                profile = self.level_ui.build_reseller_level_profile(
                    language,
                    record,
                    user_id=7,
                    joined_date="2026-07-18",
                    total_configs=12,
                    total_value=120,
                    current_debt=2,
                )
                roadmap = self.level_ui.build_reseller_level_roadmap(
                    language,
                    record,
                )
                self.assertNotIn("{", profile)
                self.assertNotIn("{", roadmap)
                self.assertIn("██████████", profile)
                rows = [
                    line
                    for line in roadmap.splitlines()
                    if line.startswith(("•", "➤"))
                ]
                self.assertEqual(len(rows), 6)


if __name__ == "__main__":
    unittest.main()
