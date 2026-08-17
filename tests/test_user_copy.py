import importlib.util
import sys
import types
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core" / "scripts" / "telegrambot" / "utils" / "api_client.py"
)
if str(MODULE_PATH.parent) not in sys.path:
    sys.path.insert(0, str(MODULE_PATH.parent))
if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub
spec = importlib.util.spec_from_file_location("user_copy_api_under_test", MODULE_PATH)
api_client = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api_client
spec.loader.exec_module(api_client)


def source_user(**overrides):
    user = {
        "username": "alice",
        "password": "same-secret",
        "max_download_bytes": 10 * api_client.GIB,
        "upload_bytes": api_client.GIB,
        "download_bytes": 1,
        "expiration_days": 30,
        "account_creation_date": "2026-08-01T00:00:00+00:00",
        "status": "Offline",
        "blocked": False,
        "note": "customer note",
        "unlimited_user": False,
    }
    user.update(overrides)
    return user


class BlitzDestination:
    panel_type = "blitz"
    server_id = "dst"
    server_name = "Destination"
    server_config = {"default_limit_ip": 0}

    def __init__(self, uri=True):
        self.created = False
        self.deleted = False
        self.blocked = False
        self.add_args = None
        self.uri = uri

    def get_user_result(self, username):
        if not self.created:
            return {"status": "missing", "data": None}
        traffic_gib = self.add_args[1]
        return {"status": "found", "data": {
            "username": username,
            "password": self.add_args[5]["password"],
            "max_download_bytes": traffic_gib * api_client.GIB,
            "upload_bytes": 0,
            "download_bytes": 0,
            "blocked": self.blocked,
            "expiration_days": self.add_args[2],
            "account_creation_date": self.add_args[5].get("creation_date"),
            "delayed_start": self.add_args[5].get("creation_date") is None,
            "status": "On-hold" if self.add_args[5].get("creation_date") is None else "Offline",
            "note": self.add_args[4],
        }}

    def add_user(self, username, traffic_limit, expiration_days, unlimited=False, note=None, **kwargs):
        self.add_args = (username, traffic_limit, expiration_days, unlimited, note, kwargs)
        self.created = True
        return {"created": True}

    def get_user_uri(self, username):
        return {"normal_sub": "https://dst.example/sub/alice"} if self.uri else None

    def update_user(self, username, data):
        self.blocked = bool(data.get("blocked"))
        return {"updated": True}

    def delete_user(self, username):
        self.deleted = True
        self.created = False
        return {"deleted": True}


class ThreeXDestination:
    panel_type = "3x-ui"
    server_id = "x3"
    server_name = "X Three"
    server_config = {"default_limit_ip": 2}

    def __init__(self, uri=True, rollback=True):
        self.created = False
        self.deleted = False
        self.blocked = False
        self.spec = None
        self.traffic = None
        self.uri = uri
        self.rollback = rollback

    def get_user_result(self, username):
        if not self.created:
            return {"status": "missing", "data": None}
        return {"status": "found", "data": {
            "username": username,
            "password": self.spec.password,
            "max_download_bytes": self.spec.traffic_limit_bytes,
            "upload_bytes": self.traffic[0],
            "download_bytes": self.traffic[1],
            "blocked": self.blocked,
            "delayed_start": self.spec.delayed_start,
            "inbound_ids": list(self.spec.inbound_ids),
            "expiration_days": self.spec.expiration_days,
            "note": self.spec.note,
        }}

    def get_inbound_options(self):
        return [
            {"id": 4, "remark": "HY2", "protocol": "hysteria"},
            {"id": 8, "remark": "VLESS", "protocol": "vless"},
        ]

    def create_from_spec(self, spec):
        self.spec = spec
        self.created = True
        return {"created": True}

    def update_traffic(self, username, upload, download):
        self.traffic = (upload, download)
        return {"updated": True}

    def get_user_uri(self, username):
        return {"normal_sub": "hy2://direct", "direct": True} if self.uri else None

    def update_user(self, username, data):
        self.blocked = bool(data.get("blocked"))
        return {"updated": True}

    def delete_user(self, username):
        self.deleted = True
        if self.rollback:
            self.created = False
            return {"deleted": True}
        return None


def make_multi(source, destination):
    multi = api_client.MultiServerAPI.__new__(api_client.MultiServerAPI)
    multi.servers = [
        {"id": "src", "panel": "blitz"},
        {"id": destination.server_id, "panel": destination.panel_type},
    ]
    source_client = types.SimpleNamespace(
        server_id="src", server_name="Source", panel_type="blitz"
    )
    multi.find_user_on_server = lambda username, server_id: (
        source_client,
        source,
        {"status": "found", "data": source},
    )
    multi.get_client = lambda server_id: destination if server_id == destination.server_id else source_client
    return multi


class UserCopyTests(unittest.TestCase):
    def setUp(self):
        api_client.MultiServerAPI.invalidate_all_caches()

    def test_blitz_copy_uses_ceil_of_remaining_allowance_and_zero_counters(self):
        destination = BlitzDestination()
        multi = make_multi(source_user(unlimited_user=True), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice", "blitz"), "dst")

        self.assertTrue(result["ok"])
        self.assertEqual(destination.add_args[1], 9)
        self.assertEqual(destination.add_args[5]["password"], "same-secret")
        self.assertEqual(destination.add_args[5]["creation_date"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(result["blitz_quota_gib"], 9)

    def test_destination_collision_stops_before_creation(self):
        destination = BlitzDestination()
        destination.created = True
        destination.add_args = ("alice", 1, 30, False, None, {"password": "existing"})
        multi = make_multi(source_user(), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "dst")

        self.assertEqual(result["error"], "destination_exists")
        self.assertEqual(destination.add_args[5]["password"], "existing")

    def test_three_x_copy_preserves_password_counters_state_and_inbounds(self):
        destination = ThreeXDestination()
        source = source_user(
            status="On-hold",
            account_creation_date=None,
            blocked=True,
            upload_bytes=123,
            download_bytes=456,
        )
        multi = make_multi(source, destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [4])

        self.assertTrue(result["ok"])
        self.assertEqual(destination.spec.password, "same-secret")
        self.assertEqual(destination.spec.traffic_limit_bytes, 10 * api_client.GIB)
        self.assertTrue(destination.spec.delayed_start)
        self.assertEqual(destination.traffic, (123, 456))
        self.assertTrue(destination.blocked)
        self.assertEqual(result["inbound_ids"], [4])
        self.assertTrue(result["direct_link"])

    def test_non_hysteria_inbound_is_rejected(self):
        destination = ThreeXDestination()
        multi = make_multi(source_user(), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [8])

        self.assertEqual(result["error"], "inbounds_not_hysteria2")
        self.assertFalse(destination.created)

    def test_post_create_failure_rolls_back_new_destination(self):
        destination = ThreeXDestination(uri=False)
        multi = make_multi(source_user(status="On-hold", account_creation_date=None), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [4])

        self.assertEqual(result["error"], "destination_uri_failed")
        self.assertTrue(result["rolled_back"])
        self.assertTrue(destination.deleted)

    def test_rollback_failure_reports_partial_destination(self):
        destination = ThreeXDestination(uri=False, rollback=False)
        multi = make_multi(source_user(status="On-hold", account_creation_date=None), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [4])

        self.assertTrue(result["rollback_failed"])
        self.assertEqual(result["partial_destination"], "x3")

    def test_ambiguous_create_outcome_never_deletes_unproven_account(self):
        destination = BlitzDestination()

        def uncertain_add(username, traffic_limit, expiration_days, unlimited=False, note=None, **kwargs):
            destination.add_args = (username, traffic_limit, expiration_days, unlimited, note, kwargs)
            destination.created = True
            return None

        destination.add_user = uncertain_add
        multi = make_multi(source_user(), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "dst")

        self.assertEqual(result["error"], "destination_create_outcome_unknown")
        self.assertEqual(result["partial_destination"], "dst")
        self.assertFalse(destination.deleted)

    def test_blitz_rejects_unlimited_and_exhausted_allowances(self):
        destination = BlitzDestination()
        multi = make_multi(source_user(unlimited_user=True, max_download_bytes=0), destination)
        unlimited = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "dst")
        self.assertEqual(unlimited["error"], "blitz_unlimited_not_representable")

        destination = BlitzDestination()
        multi = make_multi(source_user(upload_bytes=10 * api_client.GIB, download_bytes=0), destination)
        exhausted = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "dst")
        self.assertEqual(exhausted["error"], "blitz_allowance_exhausted")


if __name__ == "__main__":
    unittest.main()
