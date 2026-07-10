import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def shell_function_body(script_text, function_name):
    start_marker = f"{function_name}() {{"
    start = script_text.index(start_marker)
    next_function = re.search(
        r"\n[a-zA-Z_][a-zA-Z0-9_]*\(\) \{",
        script_text[start + len(start_marker):],
    )
    if not next_function:
        return script_text[start:]
    end = start + len(start_marker) + next_function.start()
    return script_text[start:end]


class MenuPythonEnvironmentTests(unittest.TestCase):
    def test_cli_actions_use_virtual_environment_helper(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text()
        path_text = (REPO_ROOT / "core" / "scripts" / "path.sh").read_text()
        helper_body = shell_function_body(menu_text, "run_ajib_cli")

        self.assertIn('AJIB_PYTHON="/etc/ajib/ajib_venv/bin/python"', path_text)
        self.assertIn('"$AJIB_PYTHON" "$CLI_PATH" "$@"', helper_body)
        self.assertEqual(menu_text.count("run_ajib_cli telegram"), 3)
        self.assertNotIn('python3 "$CLI_PATH"', menu_text)

    def test_inline_json_parser_keeps_using_system_python(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text()

        self.assertIn('python3 - "$TELEGRAM_ENV"', menu_text)

    def test_missing_virtual_environment_has_actionable_error(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text()
        helper_body = shell_function_body(menu_text, "run_ajib_cli")
        script = "\n".join(
            (
                'AJIB_PYTHON="/definitely/missing/ajib/python"',
                'CLI_PATH="/definitely/missing/ajib/cli.py"',
                helper_body,
                "run_ajib_cli telegram -a stop",
            )
        )

        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ajib Python environment is missing", result.stderr)
        self.assertIn("installer or upgrade", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
