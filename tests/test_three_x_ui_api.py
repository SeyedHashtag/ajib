import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
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
spec = importlib.util.spec_from_file_location("three_x_ui_api_under_test", MODULE_PATH)
api_client = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api_client
spec.loader.exec_module(api_client)


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = api_client.requests.exceptions.HTTPError(str(self.status_code))
            error.response = self
            raise error

    def json(self):
        return self.payload


def make_client(default_inbounds=None):
    return api_client.ThreeXUIAPIClient({
        "id": "x1",
        "name": "X One",
        "url": "https://x.example/panel/api/",
        "token": "token-value",
        "panel": "3x-ui",
        "enabled": True,
        "weight": 1,
        "default_inbound_ids": default_inbounds or [4],
        "default_limit_ip": 2,
    })


class ThreeXUIAdapterTests(unittest.TestCase):
    def test_blitz_records_receive_panel_neutral_metadata(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })
        client._request = lambda *args, **kwargs: Response({
            "username": "alice",
            "password": "secret",
            "status": "Offline",
            "blocked": False,
            "expiration_days": 30,
            "account_creation_date": "2026-08-01T00:00:00+00:00",
        })

        user = client.get_user("alice")

        self.assertEqual(user["panel_type"], "blitz")
        self.assertTrue(user["timer_started"])
        self.assertEqual(user["account_expiration_date"], "2026-08-31T00:00:00+00:00")
        self.assertEqual(user["credential_metadata"]["fields_present"], ["password"])

    def test_factory_defaults_legacy_to_blitz_and_uses_bearer_for_3x(self):
        legacy = api_client._normalise_server_config({
            "id": "old", "url": "https://old.example", "token": "t"
        })
        self.assertEqual(legacy["panel"], "blitz")
        self.assertEqual(legacy["default_inbound_ids"], [])

        client = make_client()
        self.assertEqual(client.api_base, "https://x.example/panel/api/")
        self.assertEqual(client.headers["Authorization"], "Bearer token-value")
        self.assertIsInstance(api_client.create_panel_client(client.server_config), api_client.ThreeXUIAPIClient)

    def test_list_normalises_traffic_online_expiry_and_credentials(self):
        client = make_client()
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("clients/list"):
                return Response({"success": True, "obj": [
                    {
                        "email": "active",
                        "auth": "do-not-display",
                        "subId": "sub1",
                        "totalGB": 10 * api_client.GIB,
                        "expiryTime": 1893456000000,
                        "enable": True,
                        "comment": "note [ajib-duration:30d]",
                        "inboundIds": [4],
                        "traffic": {"up": 10, "down": 20},
                    },
                    {
                        "email": "waiting",
                        "auth": "pw",
                        "totalGB": 5 * api_client.GIB,
                        "expiryTime": -15 * api_client.MILLISECONDS_PER_DAY,
                        "enable": True,
                        "comment": "[ajib-duration:15d]",
                        "inboundIds": [4],
                        "traffic": {"up": 0, "down": 0},
                    },
                ]})
            return Response({"success": True, "obj": ["active"]})

        client._request = request
        users = client.get_users()

        self.assertEqual([user["status"] for user in users], ["Online", "On-hold"])
        self.assertEqual(users[0]["upload_bytes"], 10)
        self.assertEqual(users[0]["download_bytes"], 20)
        self.assertEqual(users[0]["note"], "note")
        self.assertEqual(users[0]["panel_type"], "3x-ui")
        self.assertTrue(users[1]["delayed_start"])
        self.assertEqual(users[1]["expiration_days"], 15)
        self.assertIn("auth", users[0]["credential_metadata"]["fields_present"])
        self.assertEqual(calls[0][0:2], ("GET", "https://x.example/panel/api/clients/list"))

    def test_create_uses_defaults_password_marker_and_negative_expiry(self):
        client = make_client([4, 7])
        calls = []
        client._request = lambda method, url, **kwargs: (
            calls.append((method, url, kwargs)) or Response({"success": True, "msg": "added"})
        )

        result = client.add_user("alice", 8, 30, password="same-password", note="customer")

        self.assertIsNotNone(result)
        payload = calls[0][2]["data"]
        self.assertEqual(payload["inboundIds"], [4, 7])
        self.assertEqual(payload["client"]["auth"], "same-password")
        self.assertEqual(payload["client"]["totalGB"], 8 * api_client.GIB)
        self.assertEqual(payload["client"]["expiryTime"], -30 * api_client.MILLISECONDS_PER_DAY)
        self.assertIn("[ajib-duration:30d]", payload["client"]["comment"])
        self.assertEqual(payload["client"]["limitIp"], 2)

    def test_update_sends_full_record_and_preserves_auth(self):
        client = make_client()
        calls = []
        full = {
            "client": {
                "email": "alice", "auth": "keep-this", "subId": "sub",
                "totalGB": api_client.GIB, "expiryTime": -86400000,
                "comment": "[ajib-duration:1d]", "enable": True,
            },
            "inboundIds": [4],
        }

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return Response({"success": True, "obj": full})
            return Response({"success": True, "msg": "updated"})

        client._request = request
        result = client.update_user("alice", {"blocked": True})

        self.assertIsNotNone(result)
        update_call = calls[-1]
        self.assertTrue(update_call[1].endswith("clients/update/alice"))
        self.assertEqual(update_call[2]["data"]["auth"], "keep-this")
        self.assertFalse(update_call[2]["data"]["enable"])
        self.assertNotIn("inboundIds", update_call[2]["data"])

    def test_reset_fails_closed_before_mutation_without_duration_marker(self):
        client = make_client()
        calls = []
        client._request = lambda method, url, **kwargs: (
            calls.append((method, url))
            or Response({"success": True, "obj": {"client": {
                "email": "legacy", "auth": "pw", "expiryTime": 1893456000000,
                "enable": True,
            }, "inboundIds": [4]}})
        )

        result = client.reset_user_result("legacy")

        self.assertEqual(result["error"], "duration_unknown")
        self.assertEqual(len(calls), 1)

    def test_subscription_and_readiness_require_public_settings(self):
        client = make_client([4])

        def request(method, url, **kwargs):
            if url.endswith("clients/get/alice"):
                return Response({"success": True, "obj": {"client": {
                    "email": "alice", "subId": "abc", "auth": "pw"
                }, "inboundIds": [4]}})
            if url.endswith("inbounds/options"):
                return Response({"success": True, "obj": [{"id": 4, "remark": "HY2", "protocol": "hysteria"}]})
            if url.endswith("setting/all"):
                return Response({"success": True, "obj": {"subURI": "https://sub.example/s/", "subEnable": True}})
            raise AssertionError(url)

        client._request = request
        self.assertEqual(client.get_user_uri("alice")["normal_sub"], "https://sub.example/s/abc")
        self.assertEqual(client.is_creation_ready(verify_remote=True), (True, None))

    def test_invalid_success_envelope_is_rejected(self):
        client = make_client()
        client._request = lambda *args, **kwargs: Response({"obj": []})
        self.assertIsNone(client.get_users())


if __name__ == "__main__":
    unittest.main()
