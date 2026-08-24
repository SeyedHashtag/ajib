import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "telegram_formatting.py"
)


def load_telegram_formatting():
    spec = importlib.util.spec_from_file_location(
        "telegram_formatting_under_test",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TelegramFormattingTests(unittest.TestCase):
    def test_legacy_markdown_text_escapes_dynamic_url_characters(self):
        formatting = load_telegram_formatting()
        value = r"https://sub.example/a_b*[x]`y\z?token=a+b&mode=direct#one"

        self.assertEqual(
            formatting.escape_markdown_text(value),
            r"https://sub.example/a\_b\*\[x\]\`y\\z?token=a+b&mode=direct#one",
        )

    def test_inline_code_escapes_only_code_delimiters(self):
        formatting = load_telegram_formatting()
        value = r"user_name*[x]`segment\end"

        self.assertEqual(
            formatting.escape_markdown_code(value),
            r"user_name*[x]\`segment\\end",
        )

    def test_none_is_rendered_as_empty_text(self):
        formatting = load_telegram_formatting()

        self.assertEqual(formatting.escape_markdown_text(None), "")
        self.assertEqual(formatting.escape_markdown_code(None), "")


if __name__ == "__main__":
    unittest.main()
