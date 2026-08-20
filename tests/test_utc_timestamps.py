import importlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core/scripts/telegrambot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

os.environ.setdefault("AJIB_BOT_ROLE", "supervisor")
database = importlib.import_module("utils.database")
time_utils = importlib.import_module("utils.time_utils")
timestamp_migration = importlib.import_module("utils.timestamp_migration")
renewal_migration = importlib.import_module("utils.renewal_migration")
state_archive = importlib.import_module("state_archive")


class UTCTimestampUtilityTests(unittest.TestCase):
    def test_offset_and_z_values_normalize_to_aware_utc(self):
        offset = time_utils.parse_utc_timestamp("2026-08-17T14:18:54+03:30")
        canonical = time_utils.parse_utc_timestamp("2026-08-17T10:48:54Z")

        self.assertEqual(offset, canonical)
        self.assertEqual(offset.tzinfo, timezone.utc)
        self.assertEqual(
            time_utils.format_utc_timestamp(offset),
            "2026-08-17T10:48:54.000000Z",
        )

    def test_legacy_timezone_is_explicit_and_ordinary_naive_values_are_utc(self):
        old_timezone = os.environ.get("AJIB_TIMEZONE")
        os.environ["AJIB_TIMEZONE"] = "Asia/Tehran"
        try:
            ordinary = time_utils.parse_utc_timestamp("2026-08-17 14:18:54")
            legacy_local = time_utils.parse_utc_timestamp(
                "2026-08-17 14:18:54",
                legacy_naive_timezone=time_utils.legacy_timezone(),
            )
        finally:
            if old_timezone is None:
                os.environ.pop("AJIB_TIMEZONE", None)
            else:
                os.environ["AJIB_TIMEZONE"] = old_timezone

        self.assertEqual(ordinary, datetime(2026, 8, 17, 14, 18, 54, tzinfo=timezone.utc))
        self.assertEqual(legacy_local, datetime(2026, 8, 17, 10, 48, 54, tzinfo=timezone.utc))

    def test_utc_date_uses_utc_midnight_boundary(self):
        self.assertEqual(
            str(time_utils.utc_date("2026-08-18T00:15:00+03:30")),
            "2026-08-17",
        )
        self.assertEqual(
            time_utils.format_utc_filename("2026-08-17T13:00:43Z"),
            "20260817T130043Z",
        )

    def test_backup_names_and_manifests_identify_utc(self):
        backup_script = (ROOT / "core/scripts/ajib/backup.sh").read_text(encoding="utf-8")
        self.assertIn("date -u +%Y%m%dT%H%M%SZ", backup_script)

        with tempfile.TemporaryDirectory() as temp_name:
            install_dir = Path(temp_name) / "install"
            (install_dir / "core/scripts/telegrambot").mkdir(parents=True)
            output = Path(temp_name) / "backup.zip"
            state_archive.create_backup(install_dir, output)
            with zipfile.ZipFile(output) as archive:
                manifest = json.loads(
                    archive.read(state_archive.MANIFEST_NAME).decode("utf-8")
                )

        self.assertRegex(
            manifest["created_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
        )


class TimestampMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "ajib.db")
        self.old_db_path = os.environ.get("AJIB_DB_PATH")
        self.old_legacy_timezone = os.environ.get("AJIB_LEGACY_TIMEZONE")
        os.environ["AJIB_DB_PATH"] = self.db_path
        os.environ["AJIB_LEGACY_TIMEZONE"] = "Asia/Tehran"
        database.close_connections()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        database.close_connections()
        if self.old_db_path is None:
            os.environ.pop("AJIB_DB_PATH", None)
        else:
            os.environ["AJIB_DB_PATH"] = self.old_db_path
        if self.old_legacy_timezone is None:
            os.environ.pop("AJIB_LEGACY_TIMEZONE", None)
        else:
            os.environ["AJIB_LEGACY_TIMEZONE"] = self.old_legacy_timezone
        self.temp.cleanup()

    @staticmethod
    def _payload(payment_id, *, completed_at, reviewed_at, updated_at=None, reserved=True):
        return {
            "payment_id": payment_id,
            "type": "renewal",
            "renewal_mode": "reserved" if reserved else "immediate",
            "status": "completed",
            "completed_at": completed_at,
            "renewal_reserved_at": completed_at,
            "reviewed_at": reviewed_at,
            "incentives_finalized_at": "2026-08-17 10:48:55",
            "updated_at": updated_at or completed_at,
            "updates": [
                {
                    "status": "completed",
                    "previous_status": "processing",
                    "renewal_status": "reserved",
                    "timestamp": completed_at,
                }
            ],
        }

    def _insert_payment(self, connection, payment_id, payload):
        connection.execute(
            """
            INSERT INTO payments(
                scope, payment_id, status, kind, created_at, updated_at, payload_json
            ) VALUES ('main', ?, 'completed', 'renewal', ?, ?, ?)
            """,
            (
                payment_id,
                payload.get("created_at", "2026-08-17 10:46:06"),
                payload["updated_at"],
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        event = payload["updates"][0]
        connection.execute(
            """
            INSERT INTO payment_events(
                scope, payment_id, sequence, status, previous_status,
                occurred_at, payload_json
            ) VALUES ('main', ?, 0, 'completed', 'processing', ?, ?)
            """,
            (payment_id, event["timestamp"], json.dumps(event, sort_keys=True)),
        )

    def test_v3_repairs_aug17_and_preserves_ambiguous_or_unrelated_records(self):
        connection = database.get_connection(self.db_path)
        connection.execute(
            "DELETE FROM state_metadata WHERE key=?",
            (timestamp_migration.MIGRATION_METADATA_KEY,),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version>=3")

        aug17 = self._payload(
            "a6971cd0-590a-43bb-a205-fece95902da8",
            completed_at="2026-08-17 14:18:54",
            reviewed_at="2026-08-17 10:48:53",
            updated_at="2026-08-17 16:29:47",
        )
        synchronized = self._payload(
            "sync-columns",
            completed_at="2026-08-17 14:18:54",
            reviewed_at="2026-08-17 10:48:53",
        )
        ambiguous = self._payload(
            "ambiguous",
            completed_at="2026-08-17 14:18:54",
            reviewed_at="2026-08-17 12:48:53",
        )
        ambiguous["incentives_finalized_at"] = "2026-08-17 12:48:55"
        unrelated = self._payload(
            "immediate",
            completed_at="2026-08-17 14:18:54",
            reviewed_at="2026-08-17 10:48:53",
            reserved=False,
        )
        for payment_id, payload in (
            (aug17["payment_id"], aug17),
            (synchronized["payment_id"], synchronized),
            (ambiguous["payment_id"], ambiguous),
            (unrelated["payment_id"], unrelated),
        ):
            self._insert_payment(connection, payment_id, payload)
        database.reset_connection(self.db_path)

        migrated = database.get_connection(self.db_path)
        row = migrated.execute(
            "SELECT updated_at, payload_json FROM payments WHERE scope='main' AND payment_id=?",
            (aug17["payment_id"],),
        ).fetchone()
        repaired = json.loads(row["payload_json"])
        event = migrated.execute(
            """
            SELECT occurred_at, payload_json FROM payment_events
            WHERE scope='main' AND payment_id=? AND sequence=0
            """,
            (aug17["payment_id"],),
        ).fetchone()

        expected = "2026-08-17T10:48:54.000000Z"
        self.assertEqual(database.schema_version(self.db_path), 5)
        self.assertEqual(repaired["completed_at"], expected)
        self.assertEqual(repaired["renewal_reserved_at"], expected)
        self.assertEqual(repaired["updates"][0]["timestamp"], expected)
        self.assertEqual(event["occurred_at"], expected)
        self.assertEqual(json.loads(event["payload_json"])["timestamp"], expected)
        self.assertEqual(row["updated_at"], "2026-08-17 16:29:47")

        sync_row = migrated.execute(
            "SELECT updated_at FROM payments WHERE scope='main' AND payment_id='sync-columns'"
        ).fetchone()
        self.assertEqual(sync_row["updated_at"], expected)

        for payment_id in ("ambiguous", "immediate"):
            payload = json.loads(
                migrated.execute(
                    "SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?",
                    (payment_id,),
                ).fetchone()["payload_json"]
            )
            self.assertEqual(payload["completed_at"], "2026-08-17 14:18:54")

        metadata = json.loads(
            migrated.execute(
                "SELECT value FROM state_metadata WHERE key=?",
                (timestamp_migration.MIGRATION_METADATA_KEY,),
            ).fetchone()["value"]
        )
        self.assertEqual(metadata["scanned_count"], 4)
        self.assertEqual(metadata["changed_count"], 2)
        self.assertEqual(metadata["skipped_count"], 1)
        self.assertIn(f"main:{aug17['payment_id']}", metadata["affected_identifiers"])
        self.assertEqual(metadata["ambiguous_identifiers"], ["main:ambiguous"])

        before = migrated.execute(
            "SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?",
            (aug17["payment_id"],),
        ).fetchone()["payload_json"]
        second = timestamp_migration.migrate_v3_utc_timestamps(migrated)
        after = migrated.execute(
            "SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?",
            (aug17["payment_id"],),
        ).fetchone()["payload_json"]
        self.assertEqual(second, metadata)
        self.assertEqual(after, before)

    def test_v4_marks_external_attention_for_read_only_reinspection(self):
        connection = database.get_connection(self.db_path)
        connection.execute(
            "DELETE FROM state_metadata WHERE key=?",
            (renewal_migration.MIGRATION_METADATA_KEY,),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version>=4")
        payment = {
            "payment_id": "timezone-payment",
            "type": "renewal",
            "status": "completed",
            "renewal_mode": "reserved",
            "renewal_status": "attention",
            "renewal_attention_reason": "external_renewal",
            "renewal_next_attempt_at": "2099-01-01T00:00:00.000000Z",
        }
        connection.execute(
            """
            INSERT INTO payments(scope, payment_id, status, kind, payload_json)
            VALUES ('main', ?, 'completed', 'renewal', ?)
            """,
            (payment["payment_id"], json.dumps(payment)),
        )
        connection.execute(
            "INSERT INTO resellers(reseller_id, status, payload_json) VALUES ('7', 'approved', '{}')"
        )
        connection.execute(
            """
            INSERT INTO reseller_configs(
                reseller_id, config_index, username, server_id, payload_json
            ) VALUES ('7', 0, 'r7', 'primary', '{}')
            """
        )
        reseller_renewal = {
            "reservation_id": "timezone-reseller",
            "renewal_mode": "reserved",
            "renewal_status": "attention",
            "renewal_attention_reason": "external_renewal",
            "renewal_next_attempt_at": "2099-01-01T00:00:00.000000Z",
        }
        connection.execute(
            """
            INSERT INTO reseller_renewals(
                reseller_id, config_index, renewal_index, payload_json
            ) VALUES ('7', 0, 0, ?)
            """,
            (json.dumps(reseller_renewal),),
        )
        database.reset_connection(self.db_path)

        migrated = database.get_connection(self.db_path)
        payment_after = json.loads(migrated.execute(
            "SELECT payload_json FROM payments WHERE scope='main' AND payment_id='timezone-payment'"
        ).fetchone()["payload_json"])
        reseller_after = json.loads(migrated.execute(
            "SELECT payload_json FROM reseller_renewals WHERE reseller_id='7'"
        ).fetchone()["payload_json"])
        self.assertEqual(database.schema_version(self.db_path), 5)
        for record in (payment_after, reseller_after):
            self.assertEqual(record["renewal_status"], "attention")
            self.assertEqual(record["renewal_attention_reason"], "external_renewal")
            self.assertEqual(record["renewal_recheck_pending"], "v4_timezone_normalization")
            self.assertNotIn("renewal_next_attempt_at", record)

        metadata = renewal_migration.migrate_v4_renewal_timezone_rechecks(migrated)
        self.assertEqual(metadata["payment_rechecks"], 1)
        self.assertEqual(metadata["reseller_rechecks"], 1)


class NaiveTimestampRegressionGuardTests(unittest.TestCase):
    def test_runtime_has_no_direct_naive_now_persistence_paths(self):
        roots = [BOT_DIR, ROOT / "core/cli_api.py"]
        violations = []
        for root in roots:
            files = root.rglob("*.py") if root.is_dir() else (root,)
            for path in files:
                if path.name == "time_utils.py":
                    continue
                source = path.read_text(encoding="utf-8")
                for forbidden in ("datetime.now()", "datetime.datetime.now()", "datetime.utcnow()"):
                    if forbidden in source:
                        violations.append(f"{path.relative_to(ROOT)}: {forbidden}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
