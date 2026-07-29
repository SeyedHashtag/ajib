import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
sys.path.insert(0, str(BOT_DIR))
os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")

from utils import exchange_rate, translations


class ExchangeRateTests(unittest.TestCase):
    def test_loader_reloads_the_canonical_env_file(self):
        def load_test_env(path, override=False):
            key, value = Path(path).read_text(encoding="utf-8").strip().split("=", 1)
            if override or key not in os.environ:
                os.environ[key] = value
            return True

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with patch.object(exchange_rate, "TELEGRAM_ENV_PATH", str(env_path)):
                with patch.object(exchange_rate, "load_dotenv", side_effect=load_test_env) as loader:
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop("EXCHANGE_RATE", None)
                        env_path.write_text("EXCHANGE_RATE=58000\n", encoding="utf-8")
                        self.assertEqual(exchange_rate.get_exchange_rate(), 58000.0)

                        env_path.write_text("EXCHANGE_RATE=61000\n", encoding="utf-8")
                        self.assertEqual(exchange_rate.get_exchange_rate(), 61000.0)

                    self.assertEqual(loader.call_count, 2)
                    loader.assert_called_with(str(env_path), override=True)

    def test_loader_falls_back_for_missing_or_invalid_values(self):
        with patch.object(exchange_rate, "load_dotenv", return_value=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EXCHANGE_RATE", None)
                self.assertEqual(exchange_rate.get_exchange_rate(), 1.0)

                os.environ["EXCHANGE_RATE"] = "invalid"
                self.assertEqual(exchange_rate.get_exchange_rate(), 1.0)

    def test_purchase_plan_keeps_the_compatible_shared_export(self):
        source = (BOT_DIR / "utils" / "purchase_plan.py").read_text(encoding="utf-8")
        self.assertIn("from utils.exchange_rate import get_exchange_rate", source)

    def test_card_payment_does_not_repeat_exchange_rate(self):
        for language, messages in translations.MESSAGE_TRANSLATIONS.items():
            with self.subTest(language=language):
                payment_message = messages["card_to_card_payment"]
                self.assertNotIn("{exchange_rate}", payment_message)
                self.assertIn("{price}", payment_message)
                self.assertIn("{card_number}", payment_message)
                self.assertIn("{exchange_rate}", messages["exchange_rate"])


if __name__ == "__main__":
    unittest.main()
