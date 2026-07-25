import ast
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_SCRIPTS = {
    REPO_ROOT / "upgrade.sh",
    REPO_ROOT / "core/scripts/ajib/backup.sh",
    REPO_ROOT / "core/scripts/ajib/restore.sh",
}
DIRECT_JSON_COMPATIBILITY_MODULES = {
    "core/scripts/telegrambot/migrate_state.py",
    "core/scripts/telegrambot/state_archive.py",
    "core/scripts/telegrambot/utils/atomic_store.py",
    "core/scripts/telegrambot/utils/broadcast.py",
    "core/scripts/telegrambot/utils/receipt_checker.py",
    "core/scripts/telegrambot/utils/referral.py",
    "core/scripts/telegrambot/utils/reseller.py",
    "core/scripts/telegrambot/utils/test_config.py",
    "core/scripts/telegrambot/utils/test_config_store.py",
    "core/scripts/telegrambot/utils/traffic_monitor.py",
    "core/scripts/telegrambot/utils/username_utils.py",
    "core/scripts/telegrambot/utils/edit_plans.py",
    "core/scripts/telegrambot/utils/edit_support.py",
}


def repo_text_files():
    for path in REPO_ROOT.rglob("*"):
        if ".git" in path.parts or not path.is_file():
            continue
        if "tests" in path.parts:
            continue
        if path in MAINTENANCE_SCRIPTS:
            continue
        if path.suffix in {".py", ".sh", ".md"} or path.name in {"upgrade.sh", "README.md"}:
            yield path


def referenced_telegram_json_files():
    pattern = re.compile(r"['\"](/etc/ajib/core/scripts/telegrambot/[^'\"]+\.json)['\"]")
    files = set()
    for path in repo_text_files():
        text = path.read_text(errors="ignore")
        files.update(pattern.findall(text))
    return files


class TelegramJsonPreservationTests(unittest.TestCase):
    def test_maintenance_scripts_use_versioned_sqlite_state_archives(self):
        referenced_json_files = referenced_telegram_json_files()
        self.assertTrue(referenced_json_files)

        upgrade_text = (REPO_ROOT / "upgrade.sh").read_text()
        backup_text = (REPO_ROOT / "core/scripts/ajib/backup.sh").read_text()
        restore_text = (REPO_ROOT / "core/scripts/ajib/restore.sh").read_text()

        self.assertIn("state_archive.py", backup_text)
        self.assertIn("prepare-restore", restore_text)
        self.assertIn("prepare-restore", upgrade_text)
        self.assertIn('bash "$INSTALL_DIR/core/scripts/ajib/backup.sh"', upgrade_text)
        self.assertIn('safety_backup=$(bash "$BACKUP_SCRIPT")', restore_text)
        self.assertIn('SYSTEM_PYTHON=${AJIB_SYSTEM_PYTHON:-/usr/bin/python3}', upgrade_text)
        self.assertIn('"$SYSTEM_PYTHON" -m venv "$INSTALL_DIR/ajib_venv"', upgrade_text)
        self.assertIn("ajib.db", restore_text)
        self.assertIn("ajib.db", upgrade_text)
        self.assertNotIn(".configs.env", "\n".join((upgrade_text, backup_text, restore_text)))

    def test_maintenance_scripts_do_not_manage_a_local_vpn_server(self):
        maintenance_text = "\n".join(path.read_text() for path in MAINTENANCE_SCRIPTS)

        for legacy_value in (
            "ajib-server.service",
            ".configs.env",
            "get.hy2.sh",
            "127.0.0.1:25413",
            "config.yaml",
        ):
            self.assertNotIn(legacy_value, maintenance_text)

    def test_direct_json_file_io_is_confined_to_compatibility_and_static_config(self):
        runtime = REPO_ROOT / "core/scripts/telegrambot"
        offenders = []
        for path in runtime.rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in DIRECT_JSON_COMPATIBILITY_MODULES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "json"
                    and node.func.attr in {"load", "dump"}
                ):
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
