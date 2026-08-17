import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "payment_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("payment_lifecycle_under_test", MODULE_PATH)
PAYMENT_LIFECYCLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAYMENT_LIFECYCLE)


class PaymentLifecycleTimestampTests(unittest.TestCase):
    def resolve(self, record):
        return PAYMENT_LIFECYCLE.payment_lifecycle_timestamp(record)

    def test_completed_at_wins_over_later_operational_update(self):
        self.assertEqual(
            self.resolve({
                "status": "completed",
                "completed_at": "2026-08-12 17:33:57",
                "updated_at": "2026-08-13 17:39:22",
                "created_at": "2026-08-12 17:25:07",
            }),
            datetime(2026, 8, 12, 17, 33, 57, tzinfo=timezone.utc),
        )

    def test_legacy_completed_record_uses_earliest_successful_event(self):
        self.assertEqual(
            self.resolve({
                "status": "completed",
                "completed_at": "malformed",
                "updated_at": "2026-08-13 17:39:22",
                "created_at": "2026-08-12 17:25:07",
                "updates": [
                    {"status": "processing", "timestamp": "2026-08-12 17:33:55"},
                    {"status": "completed", "timestamp": "2026-08-12 17:33:57"},
                    {"status": "paid", "timestamp": "2026-08-12 17:34:00"},
                ],
            }),
            datetime(2026, 8, 12, 17, 33, 57, tzinfo=timezone.utc),
        )

    def test_legacy_completed_record_without_event_uses_updated_at(self):
        self.assertEqual(
            self.resolve({
                "status": "completed",
                "updated_at": "2026-08-12 17:33:57",
                "created_at": "2026-08-12 17:25:07",
            }),
            datetime(2026, 8, 12, 17, 33, 57, tzinfo=timezone.utc),
        )

    def test_terminal_record_uses_latest_matching_category_event(self):
        self.assertEqual(
            self.resolve({
                "status": "failed",
                "updated_at": "2026-08-14 12:00:00",
                "updates": [
                    {"status": "rejected", "timestamp": "2026-08-12 10:00:00"},
                    {"status": "processing", "timestamp": "2026-08-13 10:00:00"},
                    {"status": "failed", "timestamp": "2026-08-13 11:00:00"},
                ],
            }),
            datetime(2026, 8, 13, 11, 0, 0, tzinfo=timezone.utc),
        )

    def test_open_record_uses_creation_before_mutable_update(self):
        self.assertEqual(
            self.resolve({
                "status": "processing",
                "created_at": "2026-08-12 10:00:00",
                "updated_at": "2026-08-13 10:00:00",
            }),
            datetime(2026, 8, 12, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_expired_record_uses_its_latest_expiry_event(self):
        self.assertEqual(
            self.resolve({
                "status": "expired",
                "created_at": "2026-06-01 10:00:00",
                "updated_at": "2026-08-14 10:00:00",
                "updates": [
                    {"status": "expired", "timestamp": "2026-08-12 10:00:00"},
                    {"status": "expired", "timestamp": "2026-08-13 10:00:00"},
                ],
            }),
            datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc),
        )

    def test_all_malformed_timestamps_return_none(self):
        self.assertIsNone(self.resolve({
            "status": "expired",
            "created_at": "bad",
            "updated_at": "also-bad",
            "updates": [{"status": "expired", "timestamp": "still-bad"}],
        }))


if __name__ == "__main__":
    unittest.main()
