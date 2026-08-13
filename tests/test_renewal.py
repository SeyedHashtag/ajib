import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RENEWAL_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "renewal.py"
UTILS_DIR = RENEWAL_PATH.parent
GB_BYTES = 1024 ** 3


class FakeClient:
    def __init__(self, server_id, users=None, available=True, reset_status="succeeded", mutate_on_failed_reset=False):
        self.server_id = server_id
        self.users = dict(users or {})
        self.available = available
        self.reset_status = reset_status
        self.mutate_on_failed_reset = mutate_on_failed_reset
        self.reset_calls = []

    def get_user(self, username):
        if not self.available:
            return None
        return self.users.get(username)

    def get_user_result(self, username):
        if not self.available:
            return {"status": "unavailable", "data": None, "http_status": None, "error": "timeout"}
        user = self.users.get(username)
        return {
            "status": "found" if user is not None else "missing",
            "data": user,
            "http_status": 200 if user is not None else 404,
            "error": None if user is not None else "not_found",
        }

    def reset_user(self, username):
        self.reset_calls.append(username)
        user = self.users.get(username)
        if user is None:
            return None
        user.update({
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "status": "active",
        })
        return {"ok": True}

    def reset_user_result(self, username):
        if self.reset_status != "succeeded":
            if self.mutate_on_failed_reset:
                self.reset_user(username)
            else:
                self.reset_calls.append(username)
            return {
                "status": self.reset_status,
                "data": None,
                "http_status": None,
                "error": "connection_error" if self.reset_status == "unavailable" else "reset_failed",
            }
        result = self.reset_user(username)
        return {
            "status": "succeeded" if result is not None else "failed",
            "data": result,
            "http_status": 200 if result is not None else 400,
            "error": None if result is not None else "reset_failed",
        }

    def get_user_uri(self, username):
        return {"normal_sub": f"https://sub.example/{username}", "ipv4": ""}


class FakeMultiAPI:
    def __init__(self, clients):
        self.clients = dict(clients)

    def find_user(self, username, preferred_server_id=None):
        if preferred_server_id:
            client = self.clients.get(preferred_server_id)
            if client and client.get_user(username):
                return client, client.get_user(username)
        for client in self.clients.values():
            user = client.get_user(username)
            if user:
                return client, user
        return None, None

    def find_user_on_server(self, username, server_id):
        client = self.clients.get(server_id)
        if client is None:
            result = {"status": "unavailable", "data": None, "http_status": None, "error": "server_not_configured"}
            return None, None, result
        result = client.get_user_result(username)
        return client, result.get("data"), result


def load_renewal_module():
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(UTILS_DIR)]
    sys.modules["utils"] = utils_pkg

    api_client_stub = types.ModuleType("utils.api_client")
    api_client_stub.MultiServerAPI = lambda: FakeMultiAPI({})
    sys.modules["utils.api_client"] = api_client_stub

    edit_plans_stub = types.ModuleType("utils.edit_plans")
    edit_plans_stub.load_plans = lambda: {}
    sys.modules["utils.edit_plans"] = edit_plans_stub

    currency_stub = types.ModuleType("utils.currency_format")
    currency_stub.format_usd_amount = lambda value: f"{float(value):.2f}"
    sys.modules["utils.currency_format"] = currency_stub

    reseller_stub = types.ModuleType("utils.reseller")
    reseller_stub.get_reseller_level_summary = lambda data: {
        "level": min(6, 1 + int(float(data.get("total_paid", 0) or 0) // 10)),
        "discount_percent": min(
            25,
            20 + int(float(data.get("total_paid", 0) or 0) // 10),
        ),
    }
    reseller_stub.calculate_reseller_wholesale_price = lambda price, data: round(
        float(price)
        * (1 - reseller_stub.get_reseller_level_summary(data)["discount_percent"] / 100),
        2,
    )
    sys.modules["utils.reseller"] = reseller_stub

    translations_stub = types.ModuleType("utils.translations")
    translations_stub.get_message_text = lambda _language, key: {
        "renewal_offer_details": (
            "Renew {username} {plan_gb}GB {days}d ${price}\n"
            "{renewal_discount_details}"
            "Before\n{before}\nAfter\n{after}{payment_prompt}"
        ),
        "renewal_discount_offer_line": (
            "Catalog ${list_price}; renewal {percent}% (-${discount_amount})\n"
        ),
        "renewal_quota_reset_warning": "Quota resets",
        "renewal_success": (
            "Renewed {username} {plan_gb}GB {days}d\n"
            "Before\n{before}\nAfter\n{after}\n{ipv4_info}{sub_url}"
        ),
        "select_payment_method": "Select payment method",
        "renewal_state_summary": "Days remaining: {days_remaining}\nUsage: {gb_used} / {gb_limit}",
        "renewal_generic_unavailable_reason": "Renewal is currently unavailable.",
        "renewal_ipv4_line": "IPv4 URL: `{ipv4_url}`\n\n",
        "value_not_available": "Not available",
        "value_unknown": "Unknown",
        "value_unlimited": "Unlimited",
    }.get(key, key)
    sys.modules["utils.translations"] = translations_stub

    growth_events_stub = types.ModuleType("utils.growth_events")
    growth_events_stub.EVENT_RENEWAL_COMPLETED = "renewal_completed"
    growth_events_stub.SURFACE_MAIN = "main"
    growth_events_stub.SURFACE_HOSTED = "hosted"
    growth_events_stub.record_growth_event = lambda *args, **kwargs: None
    sys.modules["utils.growth_events"] = growth_events_stub

    spec = importlib.util.spec_from_file_location("renewal_under_test", RENEWAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)
        self.renewal = load_renewal_module()
        self.renewal.PAYMENTS_FILE = str(self.base / "payments.json")
        self.renewal.RESELLERS_FILE = str(self.base / "resellers.json")
        self.renewal.STATE_FILE = str(self.base / "expired_user_cleanup.json")
        self.plans = {
            "5": {"price": 12.0, "days": 30, "unlimited": False, "target": "both"},
            "10": {"price": 20.0, "days": 60, "unlimited": True, "target": "both"},
        }

    def write_json(self, path, data):
        Path(path).write_text(json.dumps(data), encoding="utf-8")

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def expired_user(self, max_gb=5):
        return {
            "blocked": True,
            "expiration_days": 0,
            "upload_bytes": GB_BYTES,
            "download_bytes": 2 * GB_BYTES,
            "max_download_bytes": max_gb * GB_BYTES,
            "status": "expired",
        }

    def base_payment(self, **overrides):
        data = {
            "user_id": 123,
            "username": "alice",
            "server_id": "s1",
            "plan_gb": "5",
            "days": 30,
            "unlimited": False,
            "status": "completed",
            "price": 10.0,
        }
        data.update(overrides)
        return data

    def test_customer_offer_is_eligible_for_expired_matching_current_plan(self):
        payments = {"base-1": self.base_payment()}
        client = FakeClient("s1", {"alice": self.expired_user()})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            self.plans,
            payments=payments,
        )

        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["username"], "alice")
        self.assertEqual(offer["base_record_id"], "base-1")
        self.assertEqual(offer["price"], 10.8)
        self.assertEqual(offer["full_price"], 12.0)
        self.assertEqual(offer["renewal_discount_percent"], 10.0)
        self.assertEqual(offer["renewal_discount_amount"], 1.2)
        self.assertEqual(offer["days"], 30)
        self.assertEqual(offer["before_state"]["gb_limit"], 5.0)
        self.assertEqual(offer["expected_after_state"]["gb_used"], 0.0)
        self.assertEqual(len(offer["token"]), 16)
        message = self.renewal.format_renewal_offer("en", offer)
        self.assertIn("Catalog $12.00; renewal 10% (-$1.20)", message)
        self.assertIn("$10.80", message)

    def test_customer_offer_accepts_time_expired_api_duration_shape(self):
        payments = {"base-1": self.base_payment()}
        time_expired_user = {
            "blocked": True,
            "expiration_days": 30,
            "account_creation_date": "2000-01-01",
            "upload_bytes": GB_BYTES,
            "download_bytes": GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "Offline",
        }
        client = FakeClient("s1", {"alice": time_expired_user})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            self.plans,
            payments=payments,
        )

        self.assertTrue(offer["eligible"])
        self.assertLessEqual(offer["before_state"]["days_remaining"], 0)

    def test_customer_offer_allows_issuance_expired_paid_hold(self):
        hold_user = {
            "blocked": False,
            "status": "On Hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * GB_BYTES,
        }
        payments = {"base-1": self.base_payment(
            completed_at="2026-01-01 12:00:00",
        )}
        client = FakeClient("s1", {"alice": hold_user})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            hold_user,
            self.plans,
            payments=payments,
        )

        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["renewal_mode"], "immediate")
        self.assertTrue(offer["business_expired"])
        self.assertEqual(offer["before_state"]["panel_state"], "hold")
        self.assertEqual(offer["before_state"]["entitlement_state"], "expired")

    def test_customer_offer_does_not_treat_current_paid_hold_as_expired(self):
        hold_user = {
            "blocked": False,
            "status": "on_hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * GB_BYTES,
        }
        payments = {"base-1": self.base_payment(
            completed_at="2099-01-01 12:00:00",
        )}
        client = FakeClient("s1", {"alice": hold_user})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            hold_user,
            self.plans,
            payments=payments,
        )

        self.assertFalse(offer["eligible"])
        self.assertEqual(offer["reason"], "renewal_ineligible_not_expired")
        self.assertEqual(offer["before_state"]["panel_state"], "hold")

    def test_connected_offer_ignores_expired_issuance_and_reserves_renewal(self):
        connected_user = {
            "blocked": False,
            "status": "Online",
            "account_creation_date": "2026-08-01T00:00:00+00:00",
            "expiration_days": 60,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * GB_BYTES,
        }
        payments = {"base-1": self.base_payment(
            completed_at="2026-01-01 12:00:00",
        )}
        client = FakeClient("s1", {"alice": connected_user})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            connected_user,
            self.plans,
            payments=payments,
            allow_reservation=True,
        )

        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["renewal_mode"], "reserved")
        self.assertFalse(offer["business_expired"])
        self.assertEqual(offer["before_state"]["deadline_source"], "panel")

    def test_hold_without_verifiable_issuance_cannot_reserve_renewal(self):
        hold_user = {
            "blocked": False,
            "status": "On Hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * GB_BYTES,
        }
        client = FakeClient("s1", {"alice": hold_user})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            hold_user,
            self.plans,
            payments={"base-1": self.base_payment()},
            allow_reservation=True,
        )

        self.assertFalse(offer["eligible"])
        self.assertEqual(offer["reason"], "renewal_ineligible_state_unknown")

    def test_latest_successful_cycle_prevents_offer_from_older_expired_cycle(self):
        hold_user = {
            "blocked": False,
            "status": "On-hold",
            "account_creation_date": None,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 5 * GB_BYTES,
        }
        payments = {
            "old": self.base_payment(completed_at="2026-01-01 12:00:00"),
            "new": self.base_payment(
                completed_at="2026-08-01 12:00:00",
                updated_at="2026-08-01 12:00:00",
            ),
        }
        client = FakeClient("s1", {"alice": hold_user})

        offer = self.renewal.find_customer_renewal_offer(
            123, "alice", client, hold_user, self.plans, payments=payments
        )

        self.assertFalse(offer["eligible"])
        self.assertEqual(offer["reason"], "renewal_ineligible_not_expired")
        self.assertEqual(offer["before_state"]["entitlement_state"], "current")

    def test_customer_offer_rejects_missing_active_blocked_active_deleted_and_plan_mismatch(self):
        payments = {"base-1": self.base_payment()}
        client = FakeClient("s1", {"alice": self.expired_user()})

        missing_offer = self.renewal.find_customer_renewal_offer(
            123, "alice", None, None, self.plans, payments=payments
        )
        self.assertEqual(missing_offer["reason"], "renewal_ineligible_missing")

        active_user = dict(
            self.expired_user(),
            blocked=False,
            status="Offline",
            account_creation_date="2026-08-01T00:00:00+00:00",
            expiration_days=60,
        )
        active_offer = self.renewal.find_customer_renewal_offer(
            123, "alice", client, active_user, self.plans, payments=payments
        )
        self.assertEqual(active_offer["reason"], "renewal_ineligible_not_expired")

        manually_blocked_user = dict(self.expired_user(), expiration_days=12, upload_bytes=0, download_bytes=0)
        blocked_offer = self.renewal.find_customer_renewal_offer(
            123, "alice", client, manually_blocked_user, self.plans, payments=payments
        )
        self.assertEqual(blocked_offer["reason"], "renewal_ineligible_state_unknown")

        deleted_offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            self.plans,
            payments={"base-1": self.base_payment(cleanup_status="deleted")},
        )
        self.assertEqual(deleted_offer["reason"], "renewal_ineligible_no_record")

        day_mismatch_offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            self.plans,
            payments={"base-1": self.base_payment(days=15)},
        )
        self.assertEqual(day_mismatch_offer["reason"], "renewal_ineligible_plan_mismatch")

        quota_mismatch_offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            self.expired_user(max_gb=4),
            self.plans,
            payments=payments,
        )
        self.assertEqual(quota_mismatch_offer["reason"], "renewal_ineligible_plan_mismatch")

    def test_active_customer_offer_is_reservable_and_duplicate_is_rejected(self):
        active_user = {
            "blocked": False,
            "expiration_days": 60,
            "upload_bytes": GB_BYTES,
            "download_bytes": 2 * GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "Offline",
            "account_creation_date": "2026-08-01T00:00:00+00:00",
        }
        client = FakeClient("s1", {"alice": active_user})
        payments = {"base-1": self.base_payment()}

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            active_user,
            self.plans,
            payments=payments,
            allow_reservation=True,
        )

        self.assertTrue(offer["eligible"])
        self.assertEqual(offer["renewal_mode"], "reserved")
        self.assertEqual(offer["before_state"]["gb_used"], 3.0)
        self.assertEqual(offer["price"], 10.8)
        metadata = self.renewal.customer_payment_metadata(offer)
        self.assertEqual(metadata["renewal_discount_percent"], 10.0)
        self.assertEqual(metadata["renewal_discount_amount"], 1.2)
        self.assertEqual(metadata["renewal_plan_snapshot"]["price"], 10.8)
        self.assertEqual(metadata["renewal_plan_snapshot"]["full_price"], 12.0)

        payments["reservation-1"] = {
            **self.base_payment(type="renewal"),
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
        }
        duplicate = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            active_user,
            self.plans,
            payments=payments,
            allow_reservation=True,
        )
        self.assertFalse(duplicate["eligible"])
        self.assertEqual(duplicate["reason"], "renewal_already_reserved")

    def test_customer_offer_rejects_reseller_only_plan(self):
        plans = {
            "1": {"price": 2.0, "days": 7, "unlimited": False, "target": "reseller"},
        }
        payments = {"base-1": self.base_payment(plan_gb="1", days=7, unlimited=False)}
        client = FakeClient("s1", {"alice": self.expired_user(max_gb=1)})

        offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            plans,
            payments=payments,
        )

        self.assertFalse(offer["eligible"])
        self.assertEqual(offer["reason"], "renewal_ineligible_plan_mismatch")

    def test_missing_legacy_unlimited_metadata_does_not_block_matching_renewal(self):
        plans = {
            "5": {"price": 12.0, "days": 30, "unlimited": True, "target": "both"},
        }
        customer_record = self.base_payment()
        customer_record.pop("unlimited", None)
        client = FakeClient("s1", {"alice": self.expired_user()})

        customer_offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            plans,
            payments={"base-1": customer_record},
        )

        reseller_data = {
            "configs": [{
                "username": "bob",
                "server_id": "s1",
                "gb": "5",
                "days": 30,
                "price": 9.6,
            }]
        }
        reseller_client = FakeClient("s1", {"bob": self.expired_user()})
        reseller_offer = self.renewal.find_reseller_renewal_offer(
            "1988",
            0,
            reseller_client,
            reseller_client.get_user("bob"),
            plans,
            reseller_data=reseller_data,
        )

        self.assertTrue(customer_offer["eligible"])
        self.assertTrue(reseller_offer["eligible"])

    def test_explicit_unlimited_mismatch_still_blocks_renewal(self):
        plans = {
            "5": {"price": 12.0, "days": 30, "unlimited": True, "target": "both"},
        }
        client = FakeClient("s1", {"alice": self.expired_user()})
        customer_offer = self.renewal.find_customer_renewal_offer(
            123,
            "alice",
            client,
            client.get_user("alice"),
            plans,
            payments={"base-1": self.base_payment(unlimited=False)},
        )

        reseller_data = {
            "configs": [{
                "username": "bob",
                "server_id": "s1",
                "gb": "5",
                "days": 30,
                "unlimited": False,
                "price": 9.6,
            }]
        }
        reseller_client = FakeClient("s1", {"bob": self.expired_user()})
        reseller_offer = self.renewal.find_reseller_renewal_offer(
            "1988",
            0,
            reseller_client,
            reseller_client.get_user("bob"),
            plans,
            reseller_data=reseller_data,
        )

        self.assertEqual(customer_offer["reason"], "renewal_ineligible_plan_mismatch")
        self.assertEqual(reseller_offer["reason"], "renewal_ineligible_plan_mismatch")

    def test_customer_renewal_resets_existing_user_and_clears_cleanup_state(self):
        events = []
        sys.modules["utils.growth_events"].record_growth_event = (
            lambda *args, **kwargs: events.append((args, kwargs))
        )
        client = FakeClient("s1", {"alice": self.expired_user()})
        multi_api = FakeMultiAPI({"s1": client})
        self.write_json(self.renewal.PAYMENTS_FILE, {"base-1": self.base_payment()})
        self.write_json(self.renewal.STATE_FILE, {
            "s1:alice": {"username": "alice", "server_id": "s1", "cleanup_status": "notified"}
        })
        payment_record = {
            "type": "renewal",
            "user_id": 123,
            "plan_gb": "5",
            "days": 30,
            "unlimited": False,
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_base_record_id": "base-1",
            "created_at": "2026-08-05 12:00:00",
        }

        result = self.renewal.execute_customer_renewal(payment_record, plans=self.plans, multi_api=multi_api)

        self.assertTrue(result["success"])
        self.assertEqual(client.reset_calls, ["alice"])
        self.assertFalse(client.get_user("alice")["blocked"])
        self.assertEqual(result["before_state"]["status"], "expired")
        self.assertEqual(result["after_state"]["status"], "active")
        saved_payments = self.read_json(self.renewal.PAYMENTS_FILE)
        self.assertEqual(saved_payments["base-1"]["cleanup_status"], "renewed")
        self.assertEqual(self.read_json(self.renewal.STATE_FILE), {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], ("renewal_completed",))
        self.assertEqual(events[0][1]["user_id"], 123)
        self.assertEqual(events[0][1]["surface"], "main")
        self.assertEqual(events[0][1]["plan_id"], "5")
        self.assertIn(
            "renewal-completed:customer:2026-08-05 12:00:00",
            events[0][1]["deduplication_key"],
        )

    def test_customer_renewal_rechecks_expiry_at_execution_time(self):
        active_user = dict(self.expired_user(), blocked=False, expiration_days=30)
        client = FakeClient("s1", {"alice": active_user})
        payment_record = {
            "type": "renewal",
            "plan_gb": "5",
            "days": 30,
            "unlimited": False,
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_base_record_id": "base-1",
        }

        result = self.renewal.execute_customer_renewal(
            payment_record,
            plans=self.plans,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "renewal_ineligible_not_expired")
        self.assertEqual(client.reset_calls, [])

    def test_reseller_offer_uses_discount_and_resets_existing_user(self):
        reseller_data = {
            "configs": [{
                "username": "bob",
                "server_id": "s1",
                "gb": "5",
                "days": 30,
                "unlimited": False,
                "price": 9.6,
            }]
        }
        client = FakeClient("s1", {"bob": self.expired_user()})

        offer = self.renewal.find_reseller_renewal_offer(
            "1988",
            0,
            client,
            client.get_user("bob"),
            self.plans,
            reseller_data=reseller_data,
        )
        result = self.renewal.execute_reseller_renewal(offer, multi_api=FakeMultiAPI({"s1": client}))

        self.assertTrue(offer["eligible"])
        self.assertAlmostEqual(offer["price"], 9.6)
        self.assertEqual(offer["full_price"], 12.0)
        self.assertIsNone(offer["renewal_discount_percent"])
        self.assertEqual(offer["renewal_discount_amount"], 0.0)
        self.assertTrue(result["success"])
        self.assertEqual(client.reset_calls, ["bob"])

    def test_reseller_offer_accepts_reseller_only_plan(self):
        reseller_data = {
            "configs": [{
                "username": "bob",
                "server_id": "s1",
                "gb": "1",
                "days": 7,
                "unlimited": False,
                "price": 1.6,
            }]
        }
        plans = {
            "1": {"price": 2.0, "days": 7, "unlimited": False, "target": "reseller"},
        }
        client = FakeClient("s1", {"bob": self.expired_user(max_gb=1)})

        offer = self.renewal.find_reseller_renewal_offer(
            "1988",
            0,
            client,
            client.get_user("bob"),
            plans,
            reseller_data=reseller_data,
        )

        self.assertTrue(offer["eligible"])
        self.assertAlmostEqual(offer["price"], 1.6)

    def test_reserved_payment_waits_then_applies_once_from_locked_snapshot(self):
        active_user = {
            "blocked": False,
            "expiration_days": 12,
            "upload_bytes": GB_BYTES,
            "download_bytes": 2 * GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        client = FakeClient("s1", {"alice": active_user})
        baseline = self.renewal.capture_user_state(active_user)
        payment = {
            **self.base_payment(type="renewal", price=12.0),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_base_record_id": "base-1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": baseline,
            "renewal_plan_snapshot": {
                "plan_gb": "5",
                "days": 30,
                "unlimited": False,
                "price": 12.0,
            },
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {
            "base-1": self.base_payment(),
            "reservation-1": payment,
        })
        multi_api = FakeMultiAPI({"s1": client})

        waiting = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api
        )
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(client.reset_calls, [])

        active_user.update({"blocked": True, "expiration_days": 0, "status": "expired"})
        applied = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api
        )
        duplicate = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api
        )

        self.assertEqual(applied["status"], "applied")
        self.assertIsNone(duplicate)
        self.assertEqual(client.reset_calls, ["alice"])
        saved = self.read_json(self.renewal.PAYMENTS_FILE)
        self.assertEqual(saved["reservation-1"]["status"], "completed")
        self.assertEqual(saved["reservation-1"]["renewal_status"], "applied")
        self.assertEqual(saved["reservation-1"]["renewal_plan_snapshot"]["price"], 12.0)

    def test_settlement_atomically_rejects_a_second_reservation_for_the_same_config(self):
        self.write_json(self.renewal.PAYMENTS_FILE, {
            "reservation-1": {
                **self.base_payment(type="renewal"),
                "renewal_username": "alice",
                "renewal_server_id": "s1",
                "renewal_mode": "reserved",
                "renewal_status": "reserved",
            },
            "reservation-2": {
                **self.base_payment(type="renewal", status="processing"),
                "renewal_username": "alice",
                "renewal_server_id": "s1",
                "renewal_mode": "reserved",
            },
        })

        settled = self.renewal.mark_payment_renewal_reserved(
            "reservation-2", payments_file=self.renewal.PAYMENTS_FILE
        )

        saved = self.read_json(self.renewal.PAYMENTS_FILE)
        self.assertFalse(settled)
        self.assertEqual(saved["reservation-2"]["status"], "processing")
        self.assertNotIn("renewal_status", saved["reservation-2"])

    def test_external_renewal_requires_review_and_keep_refreshes_baseline(self):
        active_user = {
            "blocked": False,
            "expiration_days": 12,
            "upload_bytes": GB_BYTES,
            "download_bytes": 2 * GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        baseline = self.renewal.capture_user_state(active_user)
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": baseline,
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})
        active_user.update({"upload_bytes": 0, "download_bytes": 0, "expiration_days": 30})
        client = FakeClient("s1", {"alice": active_user})

        attention = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({"s1": client}),
        )
        self.assertEqual(attention["status"], "attention")
        self.assertEqual(attention["reason"], "external_renewal")
        self.assertEqual(client.reset_calls, [])
        self.assertIsNone(self.renewal.claim_payment_renewal(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE
        ))

        self.assertTrue(self.renewal.refresh_payment_renewal_baseline(
            "reservation-1", active_user, payments_file=self.renewal.PAYMENTS_FILE
        ))
        saved = self.read_json(self.renewal.PAYMENTS_FILE)["reservation-1"]
        self.assertEqual(saved["renewal_status"], "reserved")
        self.assertEqual(saved["renewal_baseline"]["gb_used"], 0.0)

    def test_external_expiration_extension_is_detected_without_a_usage_reset(self):
        active_user = {
            "blocked": False,
            "expiration_days": 12,
            "upload_bytes": GB_BYTES,
            "download_bytes": GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        record = {"renewal_baseline": self.renewal.capture_user_state(active_user)}
        extended = {**active_user, "expiration_days": 30}

        self.assertTrue(self.renewal.reservation_generation_changed(record, extended))

    def test_production_offset_baseline_equal_to_date_only_live_deadline_waits(self):
        now = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
        active_user = {
            "account_creation_date": "2026-07-14",
            "expiration_days": 60,
            "blocked": False,
            "upload_bytes": GB_BYTES,
            "download_bytes": 2 * GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "Online",
        }
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "processing",
            "renewal_claim_id": "stale-production-worker",
            "renewal_claimed_at": "2026-08-13T17:49:59+00:00",
            "renewal_baseline": {
                **self.renewal.capture_user_state(active_user, now=now),
                "expiration_deadline": "2026-09-11T20:30:00+00:00",
            },
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"production-reservation": payment})
        client = FakeClient("s1", {"alice": active_user})

        with self.assertLogs("ajib.renewals", level="WARNING") as captured:
            result = self.renewal.process_payment_renewal_reservation(
                "production-reservation",
                payments_file=self.renewal.PAYMENTS_FILE,
                multi_api=FakeMultiAPI({"s1": client}),
                now=now,
            )

        saved = self.read_json(self.renewal.PAYMENTS_FILE)["production-reservation"]
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(saved["renewal_status"], "reserved")
        self.assertNotIn("renewal_claim_id", saved)
        self.assertEqual(client.reset_calls, [])
        self.assertIn("renewal_stale_claim_reclaimed", "\n".join(captured.output))

    def test_real_deadline_extension_is_detected_across_offsets(self):
        baseline_user = {
            "account_creation_date": "2026-07-14",
            "expiration_days": 60,
            "blocked": False,
            "upload_bytes": GB_BYTES,
            "download_bytes": GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        record = {
            "renewal_baseline": {
                **self.renewal.capture_user_state(baseline_user),
                "expiration_deadline": "2026-09-12T00:00:00+03:30",
            }
        }
        live_user = {**baseline_user, "expiration_days": 61}

        self.assertTrue(self.renewal.reservation_generation_changed(record, live_user))

    def test_apply_now_consumes_reservation_after_an_external_plan_change(self):
        active_changed_plan = {
            "blocked": False,
            "expiration_days": 30,
            "upload_bytes": 0,
            "download_bytes": 0,
            "max_download_bytes": 10 * GB_BYTES,
            "status": "active",
        }
        client = FakeClient("s1", {"alice": active_changed_plan})
        record = {
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }

        result = self.renewal.execute_reserved_renewal(
            record,
            multi_api=FakeMultiAPI({"s1": client}),
            force=True,
        )

        self.assertTrue(result["success"])
        self.assertEqual(client.reset_calls, ["alice"])

    def test_keep_for_next_expiry_honors_reviewed_external_plan_change(self):
        expired_changed_plan = {
            "blocked": True,
            "expiration_days": 0,
            "upload_bytes": 10 * GB_BYTES,
            "download_bytes": 0,
            "max_download_bytes": 10 * GB_BYTES,
            "status": "expired",
        }
        client = FakeClient("s1", {"alice": expired_changed_plan})
        record = {
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_reviewed_at": "2026-08-02 12:00:00",
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }

        result = self.renewal.execute_reserved_renewal(
            record,
            multi_api=FakeMultiAPI({"s1": client}),
        )

        self.assertTrue(result["success"])
        self.assertEqual(client.reset_calls, ["alice"])

    def test_server_outage_retries_hourly_and_splits_operator_and_buyer_alerts(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        active_user = {
            "blocked": False,
            "expiration_days": 1,
            "upload_bytes": GB_BYTES,
            "download_bytes": GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {
                **self.renewal.capture_user_state(active_user),
                "expiration_deadline": (now + timedelta(minutes=30)).isoformat(),
            },
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})
        client = FakeClient("s1", {"alice": active_user}, available=False)
        multi_api = FakeMultiAPI({"s1": client})

        first = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api, now=now
        )
        saved = self.read_json(self.renewal.PAYMENTS_FILE)["reservation-1"]
        self.assertEqual(first["reason"], "server_unavailable")
        self.assertTrue(first["operator_alert_due"])
        self.assertFalse(first["buyer_alert_due"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["renewal_status"], "attention")
        self.assertEqual(saved["renewal_api_error"], "timeout")
        self.assertEqual(saved["renewal_attempts"], 1)

        self.assertTrue(self.renewal.mark_payment_renewal_alerted(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, now=now, audience="operator"
        ))
        second = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=multi_api,
            now=now + timedelta(hours=1),
        )
        self.assertFalse(second["operator_alert_due"])
        self.assertTrue(second["buyer_alert_due"])

        self.assertTrue(self.renewal.mark_payment_renewal_alerted(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            now=now + timedelta(hours=1),
            audience="buyer",
        ))
        third = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=multi_api,
            now=now + timedelta(hours=2),
        )
        self.assertFalse(third["operator_alert_due"])
        self.assertFalse(third["buyer_alert_due"])

        client.available = True
        recovered = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=multi_api,
            now=now + timedelta(hours=3),
        )
        saved = self.read_json(self.renewal.PAYMENTS_FILE)["reservation-1"]
        self.assertEqual(recovered["status"], "waiting")
        self.assertEqual(saved["renewal_status"], "reserved")
        self.assertNotIn("renewal_api_error", saved)
        self.assertEqual(client.reset_calls, [])

    def test_server_outage_applies_after_recovery_when_account_is_expired(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        expired_user = self.expired_user()
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": self.renewal.capture_user_state(expired_user),
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})
        client = FakeClient("s1", {"alice": expired_user}, available=False)
        multi_api = FakeMultiAPI({"s1": client})

        unavailable = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api, now=now
        )
        client.available = True
        applied = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=multi_api,
            now=now + timedelta(hours=1),
        )

        self.assertEqual(unavailable["reason"], "server_unavailable")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(client.reset_calls, ["alice"])

    def test_strict_renewal_target_does_not_use_duplicate_username_on_another_server(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {},
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})
        assigned = FakeClient("s1", available=False)
        duplicate = FakeClient("s2", {"alice": self.expired_user()})

        result = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({"s1": assigned, "s2": duplicate}),
            now=now,
        )

        self.assertEqual(result["reason"], "server_unavailable")
        self.assertEqual(assigned.reset_calls, [])
        self.assertEqual(duplicate.reset_calls, [])

    def test_missing_user_is_distinct_from_an_unavailable_server(self):
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {},
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})

        result = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({"s1": FakeClient("s1")}),
        )

        self.assertEqual(result["reason"], "renewal_ineligible_missing")
        self.assertTrue(result["operator_alert_due"])
        self.assertTrue(result["buyer_alert_due"])

    def test_lost_reset_response_routes_changed_account_to_external_review(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        expired_user = self.expired_user()
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": self.renewal.capture_user_state(expired_user),
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})
        client = FakeClient(
            "s1",
            {"alice": expired_user},
            reset_status="unavailable",
            mutate_on_failed_reset=True,
        )
        multi_api = FakeMultiAPI({"s1": client})

        failed = self.renewal.process_payment_renewal_reservation(
            "reservation-1", payments_file=self.renewal.PAYMENTS_FILE, multi_api=multi_api, now=now
        )
        client.reset_status = "succeeded"
        reviewed = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=multi_api,
            now=now + timedelta(hours=1),
        )

        self.assertEqual(failed["reason"], "server_unavailable")
        self.assertEqual(reviewed["reason"], "external_renewal")
        self.assertEqual(client.reset_calls, ["alice"])

    def test_retry_failure_uses_hourly_lease_and_recovers_stale_claim(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_source": "customer",
            "renewal_username": "missing",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {},
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"reservation-1": payment})

        failed = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({}),
            now=now,
        )
        too_soon = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({}),
            now=now + timedelta(minutes=59),
        )
        retry = self.renewal.process_payment_renewal_reservation(
            "reservation-1",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({}),
            now=now + timedelta(hours=1),
        )
        self.assertEqual(failed["status"], "attention")
        self.assertTrue(failed["alert_due"])
        self.assertIsNone(too_soon)
        self.assertEqual(retry["status"], "attention")

        self.write_json(self.renewal.PAYMENTS_FILE, {
            "reservation-2": {
                **payment,
                "renewal_status": "processing",
                "renewal_claim_id": "dead-worker",
                "renewal_claimed_at": (now - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S"),
            }
        })
        recovered = self.renewal.claim_payment_renewal(
            "reservation-2", payments_file=self.renewal.PAYMENTS_FILE, now=now
        )
        self.assertIsNotNone(recovered)
        self.assertNotEqual(recovered["claim_id"], "dead-worker")

    def test_attention_reminders_are_limited_to_once_per_day(self):
        now = datetime(2026, 8, 2, 12, 0, 0)
        record = {"renewal_last_alert_at": now.strftime("%Y-%m-%d %H:%M:%S")}

        self.assertFalse(self.renewal.reservation_alert_due(
            record, now=now + timedelta(hours=23, minutes=59)
        ))
        self.assertTrue(self.renewal.reservation_alert_due(
            record, now=now + timedelta(days=1)
        ))

    def test_lease_retry_expiry_and_alert_boundaries_accept_mixed_timestamp_offsets(self):
        current = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_mode": "reserved",
            "renewal_status": "processing",
            "renewal_claim_id": "live-claim",
            "renewal_claimed_at": "2026-08-02T15:20:01+03:30",
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"boundary": payment})
        self.assertIsNone(self.renewal.claim_payment_renewal(
            "boundary", payments_file=self.renewal.PAYMENTS_FILE, now=current
        ))

        payment.update({
            "renewal_status": "attention",
            "renewal_attention_reason": "server_unavailable",
            "renewal_next_attempt_at": "2026-08-02T15:30:00+03:30",
        })
        self.write_json(self.renewal.PAYMENTS_FILE, {"boundary": payment})
        self.assertIsNotNone(self.renewal.claim_payment_renewal(
            "boundary", payments_file=self.renewal.PAYMENTS_FILE, now=current
        ))

        record = {
            "renewal_baseline": {"expiration_deadline": "2026-08-02T15:30:00+03:30"},
            "renewal_last_operator_alert_at": "2026-08-01 15:30:00",
        }
        self.assertTrue(self.renewal.reservation_expected_time_expired(record, now=current))
        self.assertTrue(self.renewal.reservation_alert_due(record, now=current, audience="operator"))

    def test_unexpected_payment_error_releases_claim_and_recovers_after_hourly_retry(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        active_user = {
            "blocked": False,
            "expiration_days": 12,
            "upload_bytes": GB_BYTES,
            "download_bytes": GB_BYTES,
            "max_download_bytes": 5 * GB_BYTES,
            "status": "active",
        }
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": self.renewal.capture_user_state(active_user, now=now),
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"internal-error": payment})

        class FailingMultiAPI:
            message = "private token at https://secret.example/api"

            def find_user_on_server(self, username, server_id):
                raise RuntimeError(self.message)

        with self.assertLogs("ajib.renewals", level="ERROR") as captured:
            failed = self.renewal.process_payment_renewal_reservation(
                "internal-error",
                payments_file=self.renewal.PAYMENTS_FILE,
                multi_api=FailingMultiAPI(),
                now=now,
            )
        saved = self.read_json(self.renewal.PAYMENTS_FILE)["internal-error"]
        logs = "\n".join(captured.output)
        self.assertEqual(failed["reason"], "renewal_internal_error")
        self.assertTrue(failed["operator_alert_due"])
        self.assertFalse(failed["buyer_alert_due"])
        self.assertEqual(saved["renewal_status"], "attention")
        self.assertEqual(saved["renewal_attempts"], 1)
        self.assertEqual(saved["renewal_internal_error_type"], "RuntimeError")
        self.assertNotIn("renewal_claim_id", saved)
        self.assertNotIn("secret.example", logs)
        self.assertIn("stage=lookup", logs)
        self.assertIn("retry_seconds=3600", logs)

        self.assertTrue(self.renewal.mark_payment_renewal_alerted(
            "internal-error",
            payments_file=self.renewal.PAYMENTS_FILE,
            now=now,
            audience="operator",
        ))
        alerted = self.read_json(self.renewal.PAYMENTS_FILE)["internal-error"]
        flags = self.renewal.reservation_alert_flags(
            alerted,
            "renewal_internal_error",
            now=now + timedelta(hours=1),
        )
        self.assertFalse(flags["operator_alert_due"])
        self.assertFalse(flags["buyer_alert_due"])

        with self.assertNoLogs("ajib.renewals", level="ERROR"):
            too_soon = self.renewal.process_payment_renewal_reservation(
                "internal-error",
                payments_file=self.renewal.PAYMENTS_FILE,
                multi_api=FailingMultiAPI(),
                now=now + timedelta(minutes=59),
            )
        self.assertIsNone(too_soon)

        client = FakeClient("s1", {"alice": active_user})
        recovered = self.renewal.process_payment_renewal_reservation(
            "internal-error",
            payments_file=self.renewal.PAYMENTS_FILE,
            multi_api=FakeMultiAPI({"s1": client}),
            now=now + timedelta(hours=1),
        )
        saved = self.read_json(self.renewal.PAYMENTS_FILE)["internal-error"]
        self.assertEqual(recovered["status"], "waiting")
        self.assertEqual(saved["renewal_status"], "reserved")
        self.assertNotIn("renewal_internal_error_type", saved)
        self.assertNotIn("renewal_internal_error_at", saved)

    def test_claim_recovery_persistence_failure_is_critical_and_remains_stale(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        payment = {
            **self.base_payment(type="renewal"),
            "renewal_username": "alice",
            "renewal_server_id": "s1",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {},
        }
        self.write_json(self.renewal.PAYMENTS_FILE, {"recovery-failure": payment})
        original_finish = self.renewal.finish_payment_renewal
        self.addCleanup(setattr, self.renewal, "finish_payment_renewal", original_finish)
        self.renewal.finish_payment_renewal = lambda *args, **kwargs: False

        with self.assertLogs("ajib.renewals", level="CRITICAL") as captured:
            with self.assertRaises(RuntimeError):
                self.renewal.process_payment_renewal_reservation(
                    "recovery-failure",
                    payments_file=self.renewal.PAYMENTS_FILE,
                    multi_api=FakeMultiAPI({}),
                    now=now,
                )

        saved = self.read_json(self.renewal.PAYMENTS_FILE)["recovery-failure"]
        self.assertEqual(saved["renewal_status"], "processing")
        self.assertIn("renewal_claim_id", saved)
        self.assertIn("claim_released=False", "\n".join(captured.output))

    def test_unexpected_reseller_error_releases_claim_for_hourly_retry(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        reseller_stub = sys.modules["utils.reseller"]
        finished = []
        reservation = {
            "reservation_id": "reseller-error",
            "renewal_mode": "reserved",
            "renewal_status": "reserved",
            "renewal_baseline": {},
        }
        reseller_stub.claim_reseller_renewal_reservation = lambda *args, **kwargs: {
            "claim_id": "claim-1",
            "reservation": reservation,
            "config": {"username": "bob", "server_id": "s1"},
            "reseller": {"status": "approved"},
        }
        reseller_stub.finish_reseller_renewal_reservation = (
            lambda *args, **kwargs: finished.append((args, kwargs)) or True
        )
        reseller_stub.is_reseller_debt_charge_paid = lambda *_args: True

        class FailingMultiAPI:
            def find_user_on_server(self, username, server_id):
                raise ValueError("private payload")

        with self.assertLogs("ajib.renewals", level="ERROR"):
            event = self.renewal.process_reseller_renewal_reservation(
                "1988", "reseller-error", multi_api=FailingMultiAPI(), now=now
            )

        self.assertEqual(event["reason"], "renewal_internal_error")
        self.assertFalse(event["buyer_alert_due"])
        self.assertEqual(finished[-1][0][3], "attention")
        self.assertTrue(finished[-1][1]["retry"])
        self.assertEqual(
            finished[-1][1]["fields"]["renewal_internal_error_type"],
            "ValueError",
        )

    def test_restricted_reseller_requires_its_linked_charge_to_be_paid(self):
        expired = self.expired_user()
        client = FakeClient("s1", {"bob": expired})
        reseller_stub = sys.modules["utils.reseller"]
        finished = []

        def run(charge_paid):
            reservation = {
                "reservation_id": "reserved-1",
                "renewal_mode": "reserved",
                "renewal_status": "reserved",
                "renewal_baseline": self.renewal.capture_user_state(expired),
                "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "unlimited": False},
                "debt_charge_id": "charge-1",
            }
            reseller_stub.claim_reseller_renewal_reservation = lambda *args, **kwargs: {
                "claim_id": "claim-1",
                "reservation": reservation,
                "config": {"username": "bob", "server_id": "s1"},
                "reseller": {"status": "suspended"},
            }
            reseller_stub.finish_reseller_renewal_reservation = (
                lambda *args, **kwargs: finished.append((args, kwargs)) or True
            )
            reseller_stub.is_reseller_debt_charge_paid = lambda *_args: charge_paid
            return self.renewal.process_reseller_renewal_reservation(
                "1988",
                "reserved-1",
                multi_api=FakeMultiAPI({"s1": client}),
            )

        unpaid = run(False)
        self.assertEqual(unpaid["status"], "attention")
        self.assertEqual(unpaid["reason"], "reseller_debt_review")
        self.assertEqual(client.reset_calls, [])

        paid = run(True)
        self.assertEqual(paid["status"], "applied")
        self.assertEqual(client.reset_calls, ["bob"])


if __name__ == "__main__":
    unittest.main()
