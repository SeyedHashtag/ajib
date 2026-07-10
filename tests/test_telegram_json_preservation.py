import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_SCRIPTS = {
    REPO_ROOT / "upgrade.sh",
    REPO_ROOT / "core/scripts/ajib/backup.sh",
    REPO_ROOT / "core/scripts/ajib/restore.sh",
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
    def test_runtime_telegram_json_files_are_preserved_by_directory_patterns(self):
        referenced_json_files = referenced_telegram_json_files()
        self.assertTrue(referenced_json_files)

        upgrade_text = (REPO_ROOT / "upgrade.sh").read_text()
        backup_text = (REPO_ROOT / "core/scripts/ajib/backup.sh").read_text()
        restore_text = (REPO_ROOT / "core/scripts/ajib/restore.sh").read_text()

        for script_text in (upgrade_text, backup_text):
            self.assertIn('"$BOT_DIR"/*.env', script_text)
            self.assertIn('"$BOT_DIR"/*.json', script_text)
            self.assertNotIn("/etc/ajib/*.env", script_text)
            self.assertNotIn("/etc/ajib/*.json", script_text)

        self.assertIn('"$RESTORE_DIR/core/scripts/telegrambot"/*.env', restore_text)
        self.assertIn('"$RESTORE_DIR/core/scripts/telegrambot"/*.json', restore_text)
        self.assertNotIn(".configs.env", restore_text)

        maintenance_text = "\n".join((upgrade_text, backup_text, restore_text))
        for path in referenced_json_files:
            self.assertNotIn(path, maintenance_text)
            self.assertNotIn(path[len("/etc/ajib/"):], maintenance_text)

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


if __name__ == "__main__":
    unittest.main()
