import json
import hashlib
import os
import shutil
import sqlite3
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

    def rewrite_archive(self, source, destination, transform):
        with zipfile.ZipFile(source) as archive:
            entries = {
                info.filename: archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
        transform(entries)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)

    def test_backup_contains_only_bot_state(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=secret\n")
        (self.bot_dir / "payments.json").write_text(
            '{"payment": {"status": "pending", "price": 1}}'
        )
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
                    "core/scripts/telegrambot/ajib.db",
                    "core/scripts/telegrambot/backup_manifest.json",
                },
            )
            manifest = json.loads(
                archive.read("core/scripts/telegrambot/backup_manifest.json")
            )
            self.assertEqual(manifest["format_version"], 2)
            database_path = Path(self.temp_dir.name) / "backup.db"
            database_path.write_bytes(
                archive.read("core/scripts/telegrambot/ajib.db")
            )
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "SELECT payment_id, status, amount_cents FROM payments"
            ).fetchone()
            self.assertEqual(row, ("payment", "pending", 100))

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

    def test_backup_rejects_corrupt_json_state(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=secret\n")
        (self.bot_dir / "payments.json").write_text('{"payment":')

        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to read legacy state", result.stderr)
        self.assertEqual(list(self.backup_dir.glob("*.zip")), [])

    def test_restore_rejects_corrupt_json_before_replacing_state(self):
        payments_path = self.bot_dir / "payments.json"
        payments_path.write_text('{"payment": 1}')
        archive_path = Path(self.temp_dir.name) / "corrupt-state.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "core/scripts/telegrambot/payments.json",
                '{"payment":',
            )

        result = subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid JSON backup entry", result.stderr)
        self.assertEqual(payments_path.read_text(), '{"payment": 1}')
        self.assertEqual(list(self.backup_dir.glob("restore_pre_backup_*")), [])

    def test_restore_replaces_bot_state_and_keeps_safety_copy(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=old\n")
        (self.bot_dir / "payments.json").write_text(
            '{"old": {"status": "pending", "price": 1}}'
        )
        archive_path = Path(self.temp_dir.name) / "bot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("core/scripts/telegrambot/.env", "API_TOKEN=new\n")
            archive.writestr(
                "core/scripts/telegrambot/payments.json",
                '{"payment": {"status": "completed", "price": 2}}',
            )

        subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual((self.bot_dir / ".env").read_text(), "API_TOKEN=new\n")
        self.assertFalse((self.bot_dir / "payments.json").exists())
        with sqlite3.connect(self.bot_dir / "ajib.db") as connection:
            row = connection.execute(
                "SELECT payment_id, status, amount_cents FROM payments"
            ).fetchone()
            self.assertEqual(row, ("payment", "completed", 200))
        safety_copies = list(self.backup_dir.glob("ajib_bot_backup_*.zip"))
        self.assertEqual(len(safety_copies), 1)
        with zipfile.ZipFile(safety_copies[0]) as archive:
            self.assertIn("core/scripts/telegrambot/ajib.db", archive.namelist())

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
            self.assertIn("core/scripts/telegrambot/ajib.db", archive.namelist())
            self.assertNotIn("core/scripts/telegrambot/hosted_bots/1988/settings.json", archive.namelist())
            self.assertIn("core/scripts/telegrambot/hosted_bots/1988/receipt.jpg", archive.namelist())

        (tenant_dir / "settings.json").write_text("{}")
        subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(archive_path)],
            check=True, capture_output=True, text=True, env=self.env,
        )
        self.assertFalse((tenant_dir / "settings.json").exists())
        self.assertEqual((tenant_dir / "receipt.jpg").read_bytes(), b"receipt")
        with sqlite3.connect(self.bot_dir / "ajib.db") as connection:
            payload = connection.execute(
                "SELECT payload_json FROM hosted_settings WHERE reseller_id='1988'"
            ).fetchone()[0]
            self.assertEqual(json.loads(payload), {"markup_percent": 20})

    def test_restore_rejects_v2_checksum_failure_before_replacing_state(self):
        (self.bot_dir / "payments.json").write_text(
            '{"current": {"status": "pending", "price": 1}}'
        )
        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        valid = Path(result.stdout.strip())
        damaged = Path(self.temp_dir.name) / "checksum-failure.zip"

        def corrupt_manifest(entries):
            name = "core/scripts/telegrambot/backup_manifest.json"
            manifest = json.loads(entries[name])
            manifest["files"]["core/scripts/telegrambot/ajib.db"] = "0" * 64
            entries[name] = json.dumps(manifest).encode()

        self.rewrite_archive(valid, damaged, corrupt_manifest)
        restore = subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(damaged)],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(restore.returncode, 0)
        self.assertIn("checksum mismatch", restore.stderr)
        self.assertTrue((self.bot_dir / "payments.json").exists())

    def test_restore_rejects_corrupt_v2_database_with_valid_checksum(self):
        (self.bot_dir / "payments.json").write_text(
            '{"current": {"status": "pending", "price": 1}}'
        )
        result = subprocess.run(
            ["bash", str(BACKUP_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        valid = Path(result.stdout.strip())
        damaged = Path(self.temp_dir.name) / "corrupt-database.zip"

        def corrupt_database(entries):
            database_name = "core/scripts/telegrambot/ajib.db"
            entries[database_name] = b"not a sqlite database"
            manifest_name = "core/scripts/telegrambot/backup_manifest.json"
            manifest = json.loads(entries[manifest_name])
            manifest["files"][database_name] = hashlib.sha256(
                entries[database_name]
            ).hexdigest()
            entries[manifest_name] = json.dumps(manifest).encode()

        self.rewrite_archive(valid, damaged, corrupt_database)
        restore = subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(damaged)],
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertNotEqual(restore.returncode, 0)
        self.assertIn("Invalid SQLite backup", restore.stderr)
        self.assertTrue((self.bot_dir / "payments.json").exists())

    def test_restore_failure_after_mutation_rolls_back_safety_snapshot(self):
        (self.bot_dir / ".env").write_text("API_TOKEN=old\n")
        (self.bot_dir / "payments.json").write_text(
            '{"old": {"status": "pending", "price": 1}}'
        )
        incoming = Path(self.temp_dir.name) / "incoming.zip"
        with zipfile.ZipFile(incoming, "w") as archive:
            archive.writestr(
                "core/scripts/telegrambot/.env",
                "API_TOKEN=new\n",
            )
            archive.writestr(
                "core/scripts/telegrambot/payments.json",
                '{"new": {"status": "completed", "price": 2}}',
            )

        fake_bin = Path(self.temp_dir.name) / "bin"
        fake_bin.mkdir()
        count_file = Path(self.temp_dir.name) / "cp-count"
        real_cp = shutil.which("cp")
        wrapper = fake_bin / "cp"
        wrapper.write_text(
            "#!/bin/bash\n"
            f"count_file={str(count_file)!r}\n"
            "count=0\n"
            "[ ! -f \"$count_file\" ] || count=$(<\"$count_file\")\n"
            "count=$((count + 1))\n"
            "printf '%s' \"$count\" >\"$count_file\"\n"
            "if [ \"$count\" -eq 2 ]; then exit 77; fi\n"
            f"exec {real_cp!r} \"$@\"\n"
        )
        wrapper.chmod(0o700)
        failing_env = {
            **self.env,
            "PATH": f"{fake_bin}{os.pathsep}{self.env['PATH']}",
        }

        restore = subprocess.run(
            ["bash", str(RESTORE_SCRIPT), str(incoming)],
            capture_output=True,
            text=True,
            env=failing_env,
        )

        self.assertNotEqual(restore.returncode, 0)
        self.assertEqual((self.bot_dir / ".env").read_text(), "API_TOKEN=old\n")
        self.assertFalse((self.bot_dir / "payments.json").exists())
        with sqlite3.connect(self.bot_dir / "ajib.db") as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT payment_id, status, amount_cents FROM payments"
                ).fetchone(),
                ("old", "pending", 100),
            )


if __name__ == "__main__":
    unittest.main()
