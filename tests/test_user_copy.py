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


def xui_source_user(**overrides):
    user = source_user(
        panel_type="3x-ui",
        server_id="src",
        unlimited_ip=False,
        inbound_ids=[4],
        credential_metadata={
            "panel": "3x-ui",
            "fields_present": ["auth"],
            "selected_field": "auth",
        },
    )
    user.update(overrides)
    return user


class BlitzDestination:
    panel_type = "blitz"
    server_id = "dst"
    server_name = "Destination"
    server_config = {"default_limit_ip": 0}

    def __init__(self, uri=True, note_update=True, null_traffic=False):
        self.created = False
        self.deleted = False
        self.blocked = False
        self.add_args = None
        self.create_args = None
        self.uri = uri
        self.note_update = note_update
        self.null_traffic = null_traffic
        self.update_calls = []

    def get_user_result(self, username):
        if not self.created:
            return {"status": "missing", "data": None}
        traffic_gib = self.add_args[1]
        return {"status": "found", "data": {
            "username": username,
            "password": self.add_args[5]["password"],
            "max_download_bytes": traffic_gib * api_client.GIB,
            "upload_bytes": None if self.null_traffic else 0,
            "download_bytes": None if self.null_traffic else 0,
            "blocked": self.blocked,
            "expiration_days": self.add_args[2],
            "account_creation_date": self.add_args[5].get("creation_date"),
            "delayed_start": self.add_args[5].get("creation_date") is None,
            "status": "On-hold" if self.add_args[5].get("creation_date") is None else "Offline",
            "note": self.add_args[4],
            "unlimited_user": self.add_args[3],
        }}

    def add_user(self, username, traffic_limit, expiration_days, unlimited=False, note=None, **kwargs):
        self.add_args = (username, traffic_limit, expiration_days, unlimited, note, kwargs)
        self.create_args = self.add_args
        self.created = True
        return {"created": True}

    def get_user_uri(self, username):
        return {"normal_sub": "https://dst.example/sub/alice"} if self.uri else None

    def update_user(self, username, data):
        self.update_calls.append(dict(data))
        if "note" in data and not self.note_update:
            return None
        self.blocked = bool(data.get("blocked"))
        if "note" in data:
            args = list(self.add_args)
            args[4] = data["note"]
            self.add_args = tuple(args)
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
            "unlimited_ip": self.spec.unlimited_ip,
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


def make_multi(source, destination, source_panel="blitz", source_options=None):
    multi = api_client.MultiServerAPI.__new__(api_client.MultiServerAPI)
    multi.servers = [
        {"id": "src", "panel": "blitz"},
        {"id": destination.server_id, "panel": destination.panel_type},
    ]
    source_client = types.SimpleNamespace(
        server_id="src",
        server_name="Source",
        panel_type=source_panel,
        get_inbound_options=lambda: (
            [{"id": 4, "remark": "HY2", "protocol": "hysteria"}]
            if source_options is None
            else source_options
        ),
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
        self.assertTrue(destination.add_args[3])
        self.assertEqual(destination.add_args[5]["password"], "same-secret")
        self.assertEqual(destination.add_args[5]["creation_date"], "2026-08-01")
        self.assertIsNone(destination.create_args[4])
        self.assertEqual(destination.add_args[4], "customer note")
        self.assertEqual(result["blitz_quota_gib"], 9)
        self.assertEqual(result["source_panel_type"], "blitz")

    def test_limited_on_hold_blitz_copy_preserves_access_type(self):
        destination = BlitzDestination()
        source = source_user(
            status="On-hold",
            account_creation_date=None,
            delayed_start=True,
            note=None,
            unlimited_user=False,
        )
        multi = make_multi(source, destination)

        result = multi.copy_blitz_user(
            api_client.UserRef("src", "alice", "blitz"), "dst"
        )

        self.assertTrue(result["ok"])
        self.assertFalse(destination.create_args[3])
        self.assertIsNone(destination.create_args[5].get("creation_date"))
        self.assertFalse(result["expiry_rounded"])

    def test_blitz_note_patch_failure_rolls_back_started_copy(self):
        destination = BlitzDestination(note_update=False)
        multi = make_multi(source_user(), destination)

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "blitz"),
            destination_server_id="dst",
        ))

        self.assertEqual(result["error"], "destination_note_failed")
        self.assertTrue(result["rolled_back"])
        self.assertTrue(destination.deleted)

    def test_started_blitz_copy_accepts_fresh_null_traffic_as_zero(self):
        destination = BlitzDestination(null_traffic=True)
        multi = make_multi(source_user(), destination)

        result = multi.copy_blitz_user(
            api_client.UserRef("src", "alice", "blitz"), "dst"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(destination.update_calls, [{"note": "customer note"}])

    def test_blank_note_is_canonicalized_for_three_x_destination(self):
        destination = ThreeXDestination()
        source = source_user(status="On-hold", account_creation_date=None, note="   ")
        multi = make_multi(source, destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [4])

        self.assertTrue(result["ok"])
        self.assertIsNone(destination.spec.note)
        self.assertFalse(destination.spec.unlimited_ip)
        self.assertIsNone(destination.spec.limit_ip)

    def test_three_x_destination_preserves_unlimited_access(self):
        destination = ThreeXDestination()
        source = source_user(
            status="On-hold",
            account_creation_date=None,
            unlimited_user=True,
        )
        multi = make_multi(source, destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "x3", [4])

        self.assertTrue(result["ok"])
        self.assertTrue(destination.spec.unlimited_ip)
        self.assertIsNone(destination.spec.limit_ip)

    def test_blitz_to_three_x_preserves_unlimited_duration_as_active(self):
        destination = ThreeXDestination()
        source = source_user(
            expiration_days=0,
            status="On-hold",
            account_creation_date=None,
            delayed_start=True,
        )
        multi = make_multi(source, destination)

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "blitz"),
            destination_server_id="x3",
            inbound_ids=(4,),
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.spec.expiration_days, 0)
        self.assertFalse(destination.spec.delayed_start)
        self.assertIsNone(destination.spec.absolute_expiry)
        self.assertFalse(result["expiry_rounded"])

    def test_blocked_unlimited_duration_is_blocked_after_copy(self):
        destination = ThreeXDestination()
        source = source_user(
            expiration_days=0,
            blocked=True,
            status="Disabled",
            delayed_start=True,
        )
        multi = make_multi(source, destination)

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "blitz"),
            destination_server_id="x3",
            inbound_ids=(4,),
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.spec.expiration_days, 0)
        self.assertFalse(destination.spec.delayed_start)
        self.assertTrue(destination.blocked)

    def test_blitz_to_blitz_preserves_unlimited_duration(self):
        destination = BlitzDestination()
        source = source_user(expiration_days=0)
        multi = make_multi(source, destination)

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "blitz"),
            destination_server_id="dst",
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.add_args[2], 0)
        self.assertEqual(destination.add_args[5]["creation_date"], "2026-08-01")

    def test_three_x_to_blitz_preserves_unlimited_duration(self):
        destination = BlitzDestination()
        source = xui_source_user(expiration_days=0)
        multi = make_multi(source, destination, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.add_args[2], 0)
        self.assertFalse(result["expiry_rounded"])

    def test_destination_collision_stops_before_creation(self):
        destination = BlitzDestination()
        destination.created = True
        destination.add_args = ("alice", 1, 30, False, None, {"password": "existing"})
        multi = make_multi(source_user(), destination)

        result = multi.copy_blitz_user(api_client.UserRef("src", "alice"), "dst")

        self.assertEqual(result["error"], "destination_exists")
        self.assertEqual(destination.add_args[5]["password"], "existing")

    def test_blitz_to_three_x_prefixes_password_and_preserves_state(self):
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
        self.assertEqual(destination.spec.password, "alice:same-secret")
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

    def test_hysteria_three_x_source_copies_to_blitz_and_preserves_access(self):
        destination = BlitzDestination()
        source = xui_source_user(
            status="On-hold",
            account_creation_date=None,
            delayed_start=True,
            unlimited_ip=True,
            note=" imported ",
        )
        multi = make_multi(source, destination, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.create_args[5]["password"], "same-secret")
        self.assertTrue(destination.create_args[3])
        self.assertEqual(destination.create_args[4], "imported")
        self.assertEqual(result["source_panel_type"], "3x-ui")
        self.assertFalse(result["expiry_rounded"])

    def test_started_three_x_source_rounds_blitz_expiry_outward(self):
        destination = BlitzDestination()
        source = xui_source_user(
            account_creation_date="2026-08-01T12:30:00+00:00",
            absolute_expiry="2026-08-31T12:30:00+00:00",
            delayed_start=False,
            note=None,
        )
        multi = make_multi(source, destination, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertTrue(result["ok"])
        self.assertEqual(destination.create_args[5]["creation_date"], "2026-08-02")
        self.assertTrue(result["expiry_rounded"])
        self.assertEqual(result["expiry_extension_seconds"], 11.5 * 60 * 60)
        self.assertLess(result["expiry_extension_seconds"], 24 * 60 * 60)

    def test_blocked_three_x_source_is_blocked_after_blitz_copy(self):
        destination = BlitzDestination()
        source = xui_source_user(
            status="On-hold",
            account_creation_date=None,
            delayed_start=True,
            blocked=True,
        )
        multi = make_multi(source, destination, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertTrue(result["ok"])
        self.assertTrue(destination.blocked)

    def test_three_x_source_requires_auth_and_hysteria_inbound(self):
        destination = BlitzDestination()
        missing_auth = xui_source_user(
            credential_metadata={
                "panel": "3x-ui",
                "fields_present": ["password"],
                "selected_field": "password",
            }
        )
        multi = make_multi(missing_auth, destination, source_panel="3x-ui")
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "source_auth_missing")

        blank_auth = xui_source_user(password="   ")
        multi = make_multi(blank_auth, BlitzDestination(), source_panel="3x-ui")
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "source_auth_missing")

        non_hysteria = xui_source_user()
        multi = make_multi(
            non_hysteria,
            BlitzDestination(),
            source_panel="3x-ui",
            source_options=[{"id": 4, "protocol": "vless"}],
        )
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "source_not_hysteria2")

    def test_three_x_source_rejects_unavailable_inbounds_and_blitz_allowance_shapes(self):
        source = xui_source_user()
        multi = make_multi(
            source,
            BlitzDestination(),
            source_panel="3x-ui",
            source_options=False,
        )
        multi.get_client("src").get_inbound_options = lambda: None
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "source_inbounds_unavailable")

        unlimited_traffic = xui_source_user(max_download_bytes=0)
        multi = make_multi(unlimited_traffic, BlitzDestination(), source_panel="3x-ui")
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "blitz_unlimited_not_representable")

        exhausted = xui_source_user(
            max_download_bytes=api_client.GIB,
            upload_bytes=api_client.GIB,
            download_bytes=0,
        )
        multi = make_multi(exhausted, BlitzDestination(), source_panel="3x-ui")
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))
        self.assertEqual(result["error"], "blitz_allowance_exhausted")

    def test_three_x_to_three_x_is_not_supported(self):
        destination = ThreeXDestination()
        multi = make_multi(xui_source_user(), destination, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="x3",
            inbound_ids=(4,),
        ))

        self.assertEqual(result["error"], "destination_panel_not_supported")
        self.assertFalse(destination.created)

    def test_three_x_source_collision_and_unavailability_fail_closed(self):
        collision = BlitzDestination()
        collision.created = True
        collision.add_args = (
            "alice", 1, 30, False, None, {"password": "existing-secret"}
        )
        multi = make_multi(xui_source_user(), collision, source_panel="3x-ui")

        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertEqual(result["error"], "destination_exists")
        self.assertEqual(collision.add_args[5]["password"], "existing-secret")

        destination = BlitzDestination()
        multi = make_multi(xui_source_user(), destination, source_panel="3x-ui")
        source_client = multi.get_client("src")
        multi.find_user_on_server = lambda _username, _server_id: (
            source_client,
            None,
            {"status": "unavailable", "data": None},
        )
        result = multi.copy_user(api_client.UserCopySpec(
            source=api_client.UserRef("src", "alice", "3x-ui"),
            destination_server_id="dst",
        ))

        self.assertEqual(result["error"], "source_unavailable")
        self.assertFalse(destination.created)


if __name__ == "__main__":
    unittest.main()
