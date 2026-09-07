import importlib
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest import mock

import test_hosted_runtime_hardening as runtime


class StopMonitor(BaseException):
    pass


class HostedPaymentFollowupTests(unittest.TestCase):
    def setUp(self):
        runtime.HostedWorkerRecoveryTests.setUp(self)
        self.worker.bot = mock.Mock()
        self.client = mock.Mock(server_id="private-server-id")
        self.client.get_user_uri.return_value = {"normal_sub": "https://private.example/sub"}
        self.renewal = importlib.import_module("utils.renewal")
        self.patch(self.renewal, "execute_hosted_renewal", return_value={"success": True, "api_client": self.client})
        self.configs = self.patch(self.worker, "get_reseller_data", return_value={"configs": []})
        self.create = self.patch(self.worker, "_create_user", return_value=("hs100", {}, self.client))
        self.patch(self.worker, "_resolve_hosted_user", return_value=(
            self.client, {"username": "hs100"}, {"status": "found", "uniqueness_verified": True},
        ))
        self.account = self.patch(self.worker, "record_funded_reseller_config", return_value=True)
        self.patch(self.worker, "record_funded_reseller_renewal", return_value=True)
        self.patch(self.worker, "consume_credit", return_value=True)
        self.patch(self.worker, "consume_renewal_credit", return_value=True)
        self.patch(self.worker, "reserve_reseller_renewal", return_value=(True, {}))
        self.patch(self.worker, "release_credit", return_value=True)
        self.credit = self.patch(self.worker, "_credit_sale_and_referral")
        self.patch(self.worker, "present_pending_reseller_level")
        self.patch(self.worker, "_record_hosted_prepaid_good")
        self.patch(self.worker, "_record_completed_growth")
        self.gateway = self.patch(self.worker, "CryptoPayment")
        self.gateway.return_value.check_payment_status.return_value = {"result": {"status": "paid"}}

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def record(self, method="crypto", kind="new"):
        record = {
            "user_id": 100, "telegram_username": "buyer", "plan_gb": "plan-id",
            "plan_allowance_gb": 30, "days": 30, "unlimited": False,
            "retail_price": 5.7, "collected_amount": 5.7, "original_price": 6,
            "wholesale_price": 4, "margin": 1.7, "referral_reward": 0.34,
            "payment_method": method, "gateway_payment_id": "private-gateway-id",
            "status": "pending_approval" if method == "card" else "pending",
            "server_id": "private-server-id", "server_name": "private-server-name",
            "ip": "192.0.2.111", "subscription_url": "https://private.example/sub",
            "location": "private-server-location",
        }
        if method == "card":
            record.update(converted_amount=570000, converted_currency="TOMAN")
        if kind in {"renewal", "reserved"}:
            record.update(renew_username="hs100", renewal_mode="reserved" if kind == "reserved" else "immediate")
        return record

    def queue(self, order="order", **kwargs):
        record = self.record(**kwargs)
        self.worker._save_payment(order, record)
        snapshot = self.worker._owner_payment_snapshot(
            order, record, "hs100", kwargs.get("kind", "new"), self.worker._settlement_financials(record),
        )
        saved = self.worker._save_payment(order, {"status": "completed"}, owner_followup=snapshot)
        return saved["owner_payment_followup"]["snapshot"]

    def owner_messages(self):
        return [call for call in self.worker.bot.send_message.call_args_list
                if call.args[0] == 7 and call.kwargs.get("parse_mode") == "HTML"]

    def monitor_once(self):
        with (
            mock.patch.object(self.worker, "_recover_stale_payment_claims"),
            mock.patch.object(self.worker, "_recover_saved_receipts"),
            mock.patch.object(self.worker, "_reconcile_credit_reservations"),
            mock.patch.object(self.worker, "_reconcile_invite_discount_reservations"),
            mock.patch.object(self.worker, "_process_hosted_reserved_renewals"),
            mock.patch.object(self.worker.time, "sleep", side_effect=StopMonitor),
            self.assertRaises(StopMonitor),
        ):
            self.worker._crypto_monitor()

    def complete(self, route, order):
        call = SimpleNamespace(
            id="callback", from_user=SimpleNamespace(id=7 if route == "card" else 100),
            message=SimpleNamespace(chat=SimpleNamespace(id=7), message_id=1),
            data=f"hb:approve:{order}" if route == "card" else f"hb:check:{order}",
        )
        if route == "card":
            self.worker.owner_receipt(call)
        elif route == "manual":
            self.worker.check_crypto(call)
        else:
            self.monitor_once()

    def test_all_completion_routes_queue_and_deliver_once_before_customer(self):
        for route in ("card", "manual", "poller"):
            for kind in ("new", "renewal", "reserved", "recovered"):
                with self.subTest(route=route, kind=kind):
                    order = f"{route}-{kind}"
                    record = self.record("card" if route == "card" else "crypto", kind)
                    self.worker._save_payment(order, record)
                    self.worker.bot.reset_mock()
                    self.credit.reset_mock()
                    self.configs.return_value = {"configs": ([{
                        "username": "hs100", "server_id": "private-server-id", "retail_order_id": order,
                    }] if kind == "recovered" else [])}
                    customer_messages = []

                    def send_message(chat_id, text, **kwargs):
                        if chat_id == 100:
                            self.assertEqual(len(self.owner_messages()), 1)
                            customer_messages.append(text)

                    self.worker.bot.send_message.side_effect = send_message
                    self.complete(route, order)
                    self.complete(route, order)
                    if route != "card":
                        self.complete("manual", order)
                        self.complete("poller", order)
                    state = self.worker._tenant_payments()[order]
                    self.assertEqual(state["status"], "completed")
                    self.assertIn("delivered_at", state["owner_payment_followup"])
                    self.assertEqual(len(self.owner_messages()), 1)
                    self.assertEqual(self.credit.call_count, 1)
                    self.assertTrue(customer_messages)
                    expected_kind = "new" if kind == "recovered" else kind
                    self.assertEqual(state["owner_payment_followup"]["snapshot"]["kind"], expected_kind)
                    if kind == "reserved":
                        self.worker._save_payment(order, {"renewal_status": "applied"})
                        self.worker._retry_owner_payment_followups()
                        self.assertEqual(len(self.owner_messages()), 1)

    def test_rendering_localizes_permitted_fields_and_escapes_dynamic_values(self):
        for language in ("en", "fa", "ru", "tk"):
            self.worker._set_language(7, language)
            for method in ("card", "crypto"):
                with self.subTest(language=language, method=method):
                    record = self.record(method)
                    record["telegram_username"] = "buyer<&>"
                    snapshot = self.worker._owner_payment_snapshot(
                        "order<&>", record, "hs<&>", "renewal", self.worker._settlement_financials(record),
                    )
                    snapshot["completed_at"] = "2026-09-07T12:00:00Z"
                    text = self.worker._format_owner_payment_followup(snapshot)
                    for value in ("$5.70", "$4.00", "$0.34", "$1.36", "buyer&lt;&amp;&gt;", "hs&lt;&amp;&gt;",
                                  "order&lt;&amp;&gt;", "2026-09-07", "12:00:00"):
                        self.assertIn(value, text)
                    self.assertIn(self.worker._hosted_message(7, "payment_followup_title"), text)
                    self.assertIn(self.worker._hosted_message(7, "payment_followup_renewal"), text)
                    self.assertNotIn("payment_followup_", text)
                    self.assertEqual("570,000" in text, method == "card")
                    self.assertNotIn("plan-id", text)
                    for hidden in ("server_id", "private-server-id", "private-server-name", "private-server-location",
                                   "192.0.2.111", "private.example", "private-gateway-id", "gateway_payment_id"):
                        self.assertNotIn(hidden, text)
                        self.assertNotIn(hidden, json.dumps(snapshot))

    def test_unlimited_missing_username_and_no_referral_share(self):
        snapshot = self.queue()
        snapshot.update(unlimited=True, telegram_username=None, referral_reward=0, profit=1.7)
        for language in ("en", "fa", "ru", "tk"):
            self.worker._set_language(7, language)
            text = self.worker._format_owner_payment_followup(snapshot)
            self.assertIn(self.worker._hosted_message(7, "payment_followup_unlimited"), text)
            self.assertNotIn("30 GB", text)
            self.assertNotIn("@", text)
            self.assertFalse(any(
                line.startswith(self.worker._hosted_message(7, "payment_followup_referral") + ":")
                for line in text.splitlines()
            ))
            self.assertIn("$1.70", text)

    def test_failed_notification_retries_after_restart_with_original_snapshot(self):
        snapshot = self.queue(kind="reserved")
        self.worker.bot.send_message.side_effect = RuntimeError("owner unavailable")
        self.assertFalse(self.worker._notify_owner_payment("order"))
        self.worker._save_payment("order", {
            "collected_amount": 999, "username": "changed", "renewal_status": "applied",
        })
        restarted = runtime.load_module("hosted_followup_restart_test", runtime.BOT_DIR / "hosted_worker.py")
        self.addCleanup(lambda: sys.modules.pop("hosted_followup_restart_test", None))
        restarted.bot = mock.Mock()
        restarted._retry_owner_payment_followups()
        restarted._retry_owner_payment_followups()
        restarted.bot.send_message.assert_called_once()
        args, kwargs = restarted.bot.send_message.call_args
        self.assertEqual(args[0], 7)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Renewal reserved", args[1])
        self.assertIn("$5.70", args[1])
        self.assertNotIn("999", args[1])
        state = restarted._tenant_payments()["order"]
        self.assertEqual(state["owner_payment_followup"]["snapshot"], snapshot)
        self.assertEqual(state["owner_payment_followup"]["attempts"], 2)
        self.assertNotIn("last_error", state["owner_payment_followup"])
        self.credit.assert_not_called()

    def test_live_claim_blocks_concurrent_sends_and_stale_claim_is_recovered(self):
        self.queue()
        started, release = Event(), Event()

        def send(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=10))

        self.worker.bot.send_message.side_effect = send
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.worker._notify_owner_payment, "order")
            try:
                self.assertTrue(started.wait(timeout=10))
                second = executor.submit(self.worker._notify_owner_payment, "order")
                self.assertFalse(second.result(timeout=10))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=10))
        self.assertEqual(len(self.owner_messages()), 1)

        self.queue("stale")
        old_claim, _ = self.worker._claim_owner_payment_followup("stale")
        future = self.worker.utc_now() + timedelta(seconds=self.worker.PROCESSING_LEASE_SECONDS + 1)
        with mock.patch.object(self.worker, "utc_now", return_value=future):
            new_claim, _ = self.worker._claim_owner_payment_followup("stale")
        self.assertNotEqual(old_claim, new_claim)
        self.assertFalse(self.worker._finish_owner_payment_followup("stale", old_claim))
        self.assertTrue(self.worker._finish_owner_payment_followup("stale", new_claim, "retry"))
        self.worker.bot.send_message.side_effect = None
        self.worker._retry_owner_payment_followups()
        self.assertIn("delivered_at", self.worker._tenant_payments()["stale"]["owner_payment_followup"])

    def test_delivery_and_persistence_errors_do_not_reopen_successful_payments(self):
        for failure in ("owner", "customer", "completion_marker"):
            for kind in ("new", "reserved"):
                with self.subTest(failure=failure, kind=kind):
                    order = f"{failure}-{kind}"
                    self.worker._save_payment(order, self.record(kind=kind))

                    def send(chat_id, *args, **kwargs):
                        if (failure == "owner" and chat_id == 7) or (failure == "customer" and chat_id == 100):
                            raise RuntimeError("Telegram unavailable")

                    self.worker.bot.send_message.side_effect = send
                    self.credit.reset_mock()
                    original_finish = self.worker._finish_owner_payment_followup
                    with mock.patch.object(self.worker, "_finish_owner_payment_followup", side_effect=(
                        OSError("storage unavailable") if failure == "completion_marker" else original_finish
                    )):
                        self.complete("manual", order)
                    self.complete("manual", order)
                    self.assertEqual(self.worker._tenant_payments()[order]["status"], "completed")
                    self.credit.assert_called_once()

    def test_failed_fulfillment_and_historical_completions_do_not_queue_followups(self):
        for failure in ("creation", "accounting"):
            order = failure
            record = self.record()
            self.worker._save_payment(order, record)
            with (
                mock.patch.object(self.worker, "_create_user", return_value=(
                    "hs100", None if failure == "creation" else {}, self.client,
                )),
                mock.patch.object(self.worker, "record_funded_reseller_config", return_value=failure != "accounting"),
            ):
                self.complete("manual", order)
            self.assertNotIn("owner_payment_followup", self.worker._tenant_payments()[order])
        self.worker._save_payment("legacy", {**self.record(), "status": "completed"})
        self.worker._retry_owner_payment_followups()
        self.assertEqual(self.owner_messages(), [])


if __name__ == "__main__":
    unittest.main()
