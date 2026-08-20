import ast
import re
import shlex
import subprocess
import unittest
from pathlib import Path
from unittest import mock


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
        menu_text = (REPO_ROOT / "menu.sh").read_text(encoding="utf-8")
        path_text = (REPO_ROOT / "core" / "scripts" / "path.sh").read_text(encoding="utf-8")
        helper_body = shell_function_body(menu_text, "run_ajib_cli")

        self.assertIn('AJIB_PYTHON="/etc/ajib/ajib_venv/bin/python"', path_text)
        self.assertIn('"$AJIB_PYTHON" "$CLI_PATH" "$@"', helper_body)
        self.assertEqual(menu_text.count("run_ajib_cli telegram"), 3)
        self.assertNotIn('python3 "$CLI_PATH"', menu_text)

    def test_inline_json_parser_keeps_using_system_python(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text(encoding="utf-8")

        self.assertIn('python3 - "$TELEGRAM_ENV"', menu_text)

    def test_server_weight_reader_accepts_zero(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text(encoding="utf-8")
        helper_body = shell_function_body(menu_text, "read_nonnegative_number")

        self.assertIn('$value >= 0', helper_body)
        self.assertNotIn('$value > 0', helper_body)
        self.assertEqual(menu_text.count('read_nonnegative_number "Balancing weight'), 2)
        self.assertIn("0 pauses automatic placement", menu_text)

    def test_missing_virtual_environment_has_actionable_error(self):
        menu_text = (REPO_ROOT / "menu.sh").read_text(encoding="utf-8")
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

    def test_telegram_maintenance_uses_ajib_virtual_environment(self):
        command_text = (
            REPO_ROOT / "core" / "scripts" / "telegrambot" / "utils" / "command.py"
        ).read_text()
        backup_text = (
            REPO_ROOT / "core" / "scripts" / "telegrambot" / "utils" / "backup.py"
        ).read_text()
        version_text = (
            REPO_ROOT / "core" / "scripts" / "telegrambot" / "utils" / "check_version.py"
        ).read_text()

        self.assertIn("'/etc/ajib/ajib_venv/bin/python'", command_text)
        self.assertIn('[AJIB_PYTHON, CLI_PATH, "backup-ajib"]', backup_text)
        self.assertIn('[AJIB_PYTHON, CLI_PATH, "check-version"]', version_text)
        self.assertNotIn("python3 {CLI_PATH}", backup_text + version_text)

    def test_cli_runner_executes_argument_lists_without_a_shell(self):
        command_path = REPO_ROOT / "core" / "scripts" / "telegrambot" / "utils" / "command.py"
        source = command_path.read_text()
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_cli_command"
        )
        namespace = {"subprocess": subprocess, "shlex": shlex}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(command_path), "exec"), namespace)

        completed = subprocess.CompletedProcess([], 0, stdout="/opt/ajib-backups/backup.zip\n")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            result = namespace["run_cli_command"](
                ["/etc/ajib/ajib_venv/bin/python", "/etc/ajib/core/cli.py", "backup-ajib"]
            )

        self.assertEqual(result, "/opt/ajib-backups/backup.zip")
        self.assertEqual(
            run.call_args.args[0],
            ["/etc/ajib/ajib_venv/bin/python", "/etc/ajib/core/cli.py", "backup-ajib"],
        )
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
