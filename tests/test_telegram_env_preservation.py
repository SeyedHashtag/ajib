import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def shell_function_body(script_text, function_name):
    start_marker = f"{function_name}() {{"
    start = script_text.index(start_marker)
    next_function = re.search(r"\n[a-zA-Z_][a-zA-Z0-9_]*\(\) \{", script_text[start + len(start_marker):])
    if not next_function:
        return script_text[start:]
    end = start + len(start_marker) + next_function.start()
    return script_text[start:end]


class TelegramEnvPreservationTests(unittest.TestCase):
    def test_menu_routes_setup_and_server_changes_through_secure_cli(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text(encoding="utf-8")

        self.assertIn("run_ajib_cli setup", menu_text)
        self.assertIn("run_ajib_cli server manage", menu_text)
        self.assertNotIn("telegram -a", menu_text)

    def test_runbot_stop_preserves_telegram_env_file(self):
        runbot_text = (REPO_ROOT / "core" / "scripts" / "telegrambot" / "runbot.sh").read_text(encoding="utf-8")
        stop_body = shell_function_body(runbot_text, "stop_service")

        self.assertNotIn("rm -f", stop_body)
        self.assertNotIn("/etc/ajib/core/scripts/telegrambot/.env", stop_body)
        self.assertIn("Configuration preserved", stop_body)

    def test_runtime_loads_only_the_canonical_telegram_env(self):
        utils_dir = REPO_ROOT / "core/scripts/telegrambot/utils"
        for module_name in ("command.py", "payments.py"):
            module_text = (utils_dir / module_name).read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_ENV_PATH", module_text)
            self.assertIn("load_dotenv(TELEGRAM_ENV_PATH)", module_text)
            self.assertNotIn("load_dotenv()", module_text)


if __name__ == "__main__":
    unittest.main()
