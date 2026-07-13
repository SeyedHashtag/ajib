import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "core/scripts/ajib/backup.sh"
RESTORE_SCRIPT = ROOT / "core/scripts/ajib/restore.sh"


class BotStateBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.install_dir = Path(self.temp_dir.name) / "install"
        self.bot_dir = self.install_dir / "core/scripts/telegrambot"
        self.backup_dir = Path(self.temp_dir.name) / "backups"
        self.bot_dir.mkdir(parents=True)
        self.env = {
            **os.environ,
            "AJIB_INSTALL_DIR": str(self.install_dir),
            "AJIB_BACKUP_DIR": str(self.backup_dir),
            "AJIB_SKIP_SERVICE_RESTART": "1",
        }

    def test_backup_contains_only_bot_state(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=secret\n")
        (self.bot_dir / "payments.json").write_text('{"payment": 1}')
        (self.install_dir / "unrelated.json").write_text('{"server": true}')

        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        archive_path = Path(result.stdout.strip())

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "core/scripts/telegrambot/.env",
                    "core/scripts/telegrambot/payments.json",
                },
            )

    def test_restore_rejects_non_bot_entries(self):
        archive_path = Path(self.temp_dir.name) / "unsafe.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("server-config.json", "{}")

        result = subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported backup entry", result.stderr)

    def test_restore_replaces_bot_state_and_keeps_safety_copy(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=old\n")
        archive_path = Path(self.temp_dir.name) / "bot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("core/scripts/telegrambot/.env", "API_TOKEN=new\n")
            archive.writestr("core/scripts/telegrambot/payments.json", '{"payment": 2}')

        subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual((self.bot_dir / ".env").read_text(), "API_TOKEN=new\n")
        self.assertEqual((self.bot_dir / "payments.json").read_text(), '{"payment": 2}')
        safety_copies = list(self.backup_dir.glob("restore_pre_backup_*/.env"))
        self.assertEqual(len(safety_copies), 1)
        self.assertEqual(safety_copies[0].read_text(), "API_TOKEN=old\n")

    def test_backup_and_restore_include_nested_hosted_bot_state(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=secret\n")
        tenant_dir = self.bot_dir / "hosted_bots" / "1988"
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "settings.json").write_text('{"markup_percent": 20}')
        (tenant_dir / "receipt.jpg").write_bytes(b"receipt")

        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)], check=True, capture_output=True, text=True, env=self.env
        )
        archive_path = Path(result.stdout.strip())
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("core/scripts/telegrambot/hosted_bots/1988/settings.json", archive.namelist())
            self.assertIn("core/scripts/telegrambot/hosted_bots/1988/receipt.jpg", archive.namelist())

        (tenant_dir / "settings.json").write_text("{}")
        subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            check=True, capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(json.loads((tenant_dir / "settings.json").read_text()), {"markup_percent": 20})


if __name__ == "__main__":
    unittest.main()
