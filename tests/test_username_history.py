import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "username_utils.py"
)
SPEC = importlib.util.spec_from_file_location("username_utils_history_under_test", MODULE_PATH)
username_utils = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(username_utils)


class RecordedUsernameHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)

    def write_json(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_loads_account_usernames_from_every_record_shape(self):
        payments = self.write_json("payments.json", {
            "paid": {
                "username": "s123",
                "renewal_username": "s456",
                "telegram_username": "must_not_be_reserved",
            },
        })
        tests = self.write_json("test_configs.json", {
            "123": {
                "username": "t123a",
                "historical_configs": [{"username": "t123"}],
            },
        })
        resellers = self.write_json("resellers.json", {
            "321": {
                "configs": [{
                    "username": "r321",
                    "customer_name": "not_a_vpn_username",
                    "removed_from_vpn": True,
                }],
            },
        })
        cleanup = self.write_json("expired_user_cleanup.json", {
            "s9:orphan": {
                "username": "orphan",
                "cleanup_status": "deleted",
            },
        })
        hosted = self.write_json("hosted_payments.json", {
            "one": {
                "provisioned_username": "hs321",
                "renew_username": "hs654",
                "customer_telegram_username": "also_not_reserved",
            },
        })

        usernames = username_utils.load_recorded_usernames(
            record_paths=(payments, tests, resellers, cleanup),
            extra_paths=(hosted,),
        )

        self.assertEqual(
            usernames,
            {
                "s123",
                "s456",
                "t123a",
                "t123",
                "r321",
                "orphan",
                "hs321",
                "hs654",
            },
        )

    def test_missing_files_are_allowed_and_duplicate_paths_are_read_once(self):
        missing = self.base / "missing.json"
        payments = self.write_json("payments.json", {"one": {"username": "s123"}})

        usernames = username_utils.load_recorded_usernames(
            record_paths=(missing, payments, payments),
        )

        self.assertEqual(usernames, {"s123"})

    def test_malformed_json_fails_closed_with_the_path(self):
        damaged = self.base / "payments.json"
        damaged.write_text("{broken", encoding="utf-8")

        with self.assertRaises(username_utils.RecordedUsernameLoadError) as raised:
            username_utils.load_recorded_usernames(record_paths=(damaged,))

        self.assertIn(str(damaged), str(raised.exception))

    def test_non_object_json_fails_closed(self):
        invalid = self.write_json("test_configs.json", [{"username": "t123"}])

        with self.assertRaises(username_utils.RecordedUsernameLoadError):
            username_utils.load_recorded_usernames(record_paths=(invalid,))

    def test_unreadable_path_fails_closed(self):
        unreadable = self.base / "resellers.json"
        unreadable.mkdir()

        with self.assertRaises(username_utils.RecordedUsernameLoadError):
            username_utils.load_recorded_usernames(record_paths=(unreadable,))

    def test_allocator_treats_recorded_names_case_insensitively(self):
        self.assertEqual(
            username_utils.allocate_username("s", 123, {"S123", "s123A"}),
            "s123b",
        )


if __name__ == "__main__":
    unittest.main()
