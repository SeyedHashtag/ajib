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
        self.assertEqual(user["account_expiration_date"], "2026-08-31T00:00:00.000000Z")
        self.assertEqual(user["credential_metadata"]["fields_present"], ["password"])

    def test_blitz_on_hold_null_traffic_normalises_to_zero(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })
        client._request = lambda *args, **kwargs: Response({
            "username": "waiting",
            "status": "On-hold",
            "blocked": False,
            "expiration_days": 30,
            "account_creation_date": None,
            "upload_bytes": None,
            "download_bytes": None,
        })

        user = client.get_user("waiting")

        self.assertEqual(user["upload_bytes"], 0)
        self.assertEqual(user["download_bytes"], 0)
        self.assertTrue(user["delayed_start"])

    def test_blitz_500_not_found_is_confirmed_against_live_list(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })
        calls = []

        def request(method, url, **kwargs):
            calls.append(url)
            if url.endswith("api/v1/users/waiting"):
                return Response({
                    "status": 500,
                    "detail": "Command failed: User 'waiting' not found in the database.",
                }, status_code=500)
            return Response([])

        client._request = request
        result = client.get_user_result("waiting")

        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["source"], "confirmed_list_fallback")
        self.assertEqual(len(calls), 2)

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

    def test_create_unlimited_ip_keeps_quota_and_maps_limit_ip_to_zero(self):
        client = make_client()
        calls = []
        client._request = lambda method, url, **kwargs: (
            calls.append((method, url, kwargs)) or Response({"success": True, "msg": "added"})
        )

        result = client.add_user("alice", 8, 30, unlimited=True)

        self.assertIsNotNone(result)
        payload = calls[0][2]["data"]["client"]
        self.assertEqual(payload["totalGB"], 8 * api_client.GIB)
        self.assertEqual(payload["limitIp"], 0)

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

    def test_blitz_renewal_patches_then_resets_and_verifies(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "PATCH":
                return Response({"detail": "updated"})
            if url.endswith("/reset"):
                return Response({"detail": "reset"})
            return Response({
                "username": "alice",
                "max_download_bytes": 10 * api_client.GIB,
                "expiration_days": 60,
                "unlimited_user": True,
                "blocked": False,
                "upload_bytes": 0,
                "download_bytes": 0,
            })

        client._request = request
        result = client.renew_user_result("alice", 10, 60, True)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([call[0] for call in calls], ["PATCH", "GET", "GET"])
        self.assertEqual(calls[0][2]["data"], {
            "new_traffic_limit": 10,
            "new_expiration_days": 60,
            "unlimited_ip": True,
        })

    def test_blitz_renewal_reports_verification_failure(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })

        def request(method, url, **kwargs):
            if method == "PATCH" or url.endswith("/reset"):
                return Response({"detail": "ok"})
            return Response({
                "username": "alice",
                "max_download_bytes": 5 * api_client.GIB,
                "expiration_days": 30,
                "unlimited_user": False,
                "blocked": False,
                "upload_bytes": 1,
                "download_bytes": 0,
            })

        client._request = request
        result = client.renew_user_result("alice", 10, 60, True)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "verify")
        self.assertEqual(result["error"], "verification_failed")

    def test_blitz_renewal_recovers_a_lost_reset_response_by_verifying(self):
        client = api_client.APIClient({
            "id": "b1", "name": "Blitz", "url": "https://b.example", "token": "t"
        })

        def request(method, url, **kwargs):
            if method == "PATCH":
                return Response({"detail": "updated"})
            if url.endswith("/reset"):
                return Response({"detail": "gateway timeout"}, status_code=503)
            return Response({
                "username": "alice",
                "max_download_bytes": 10 * api_client.GIB,
                "expiration_days": 60,
                "unlimited_user": True,
                "blocked": False,
                "upload_bytes": 0,
                "download_bytes": 0,
            })

        client._request = request
        result = client.renew_user_result("alice", 10, 60, True)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stage"], "verify")

    def test_three_x_ui_renewal_preserves_full_client_and_verifies(self):
        client = make_client()
        calls = []
        stored_client = {
            "email": "alice",
            "auth": "keep-this",
            "subId": "keep-sub",
            "totalGB": api_client.GIB,
            "expiryTime": 1893456000000,
            "limitIp": 2,
            "enable": False,
            "comment": "customer note [ajib-duration:15d]",
            "protocolSettings": {"keep": True},
        }
        traffic = {"up": 9, "down": 11}

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("clients/get/alice"):
                return Response({
                    "success": True,
                    "obj": {"client": dict(stored_client), "inboundIds": [4, 7]},
                })
            if url.endswith("clients/update/alice"):
                stored_client.update(kwargs["data"])
                return Response({"success": True, "msg": "updated"})
            if url.endswith("clients/resetTraffic/alice"):
                traffic.update({"up": 0, "down": 0})
                return Response({"success": True, "msg": "reset"})
            if url.endswith("clients/traffic/alice"):
                return Response({"success": True, "obj": dict(traffic)})
            if url.endswith("clients/onlines"):
                return Response({"success": True, "obj": []})
            raise AssertionError(url)

        client._request = request
        result = client.renew_user_result("alice", 10, 60, True)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            [(method, url.rsplit("/", 2)[-2:]) for method, url, _ in calls],
            [
                ("GET", ["get", "alice"]),
                ("POST", ["update", "alice"]),
                ("POST", ["resetTraffic", "alice"]),
                ("GET", ["get", "alice"]),
                ("GET", ["traffic", "alice"]),
            ],
        )
        update_payload = calls[1][2]["data"]
        self.assertEqual(update_payload["auth"], "keep-this")
        self.assertEqual(update_payload["subId"], "keep-sub")
        self.assertEqual(update_payload["protocolSettings"], {"keep": True})
        self.assertEqual(update_payload["totalGB"], 10 * api_client.GIB)
        self.assertEqual(update_payload["expiryTime"], -60 * api_client.MILLISECONDS_PER_DAY)
        self.assertEqual(update_payload["limitIp"], 0)
        self.assertTrue(update_payload["enable"])
        self.assertIn("customer note", update_payload["comment"])
        self.assertIn("[ajib-duration:60d]", update_payload["comment"])

    def test_three_x_ui_renewal_classifies_failures_by_stage(self):
        client = make_client()
        client._request = lambda *_args, **_kwargs: Response({
            "success": False, "msg": "Client not found",
        })
        missing = client.renew_user_result("missing", 10, 60, False)
        self.assertEqual((missing["status"], missing["stage"]), ("failed", "reconfigure"))

        full = {"client": {
            "email": "alice", "auth": "keep", "totalGB": api_client.GIB,
            "expiryTime": -api_client.MILLISECONDS_PER_DAY,
            "limitIp": 1, "enable": True, "comment": "[ajib-duration:1d]",
        }}

        def reset_failure(method, url, **kwargs):
            if method == "GET":
                return Response({"success": True, "obj": full})
            if url.endswith("clients/update/alice"):
                return Response({"success": True, "msg": "updated"})
            return Response({"success": False, "msg": "reset rejected"})

        client._request = reset_failure
        reset = client.renew_user_result("alice", 10, 60, False)
        self.assertEqual((reset["status"], reset["stage"]), ("failed", "reset"))

        calls = []

        def verify_failure(method, url, **kwargs):
            calls.append(url)
            if url.endswith("clients/get/alice"):
                return Response({"success": True, "obj": full})
            if url.endswith("clients/traffic/alice"):
                return Response({"detail": "down"}, status_code=503)
            return Response({"success": True, "msg": "ok"})

        client._request = verify_failure
        verify = client.renew_user_result("alice", 10, 60, False)
        self.assertEqual((verify["status"], verify["stage"]), ("unavailable", "verify"))

    def test_three_x_ui_renewal_recovers_a_lost_reset_response_by_verifying(self):
        client = make_client()
        stored = {
            "email": "alice", "auth": "keep", "totalGB": api_client.GIB,
            "expiryTime": -api_client.MILLISECONDS_PER_DAY,
            "limitIp": 2, "enable": False, "comment": "[ajib-duration:1d]",
        }

        def request(method, url, **kwargs):
            if url.endswith("clients/get/alice"):
                return Response({"success": True, "obj": {"client": dict(stored)}})
            if url.endswith("clients/update/alice"):
                stored.update(kwargs["data"])
                return Response({"success": True, "msg": "updated"})
            if url.endswith("clients/resetTraffic/alice"):
                return Response({"success": False, "msg": "response lost"})
            if url.endswith("clients/traffic/alice"):
                return Response({"success": True, "obj": {"up": 0, "down": 0}})
            raise AssertionError(url)

        client._request = request
        result = client.renew_user_result("alice", 10, 60, True)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["stage"], "verify")

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
