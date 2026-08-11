import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
sys.path.insert(0, str(BOT_DIR))

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def isolate_utils_modules():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "utils" or name.startswith("utils.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    return saved


def restore_utils_modules(saved):
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)
    sys.modules.update(saved)


class HostedWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)

        env_updates = {
            "AJIB_BOT_DIR": str(root),
            "AJIB_HOSTED_RESELLER_ID": "7",
            "AJIB_HOSTED_BOT_ID": "123",
            "AJIB_HOSTED_BOT_USERNAME": "shopbot",
            "AJIB_BOT_ROLE": "hosted",
        }
        old_env = {key: os.environ.get(key) for key in env_updates}

        def restore_env():
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore_env)
        os.environ.update(env_updates)
        saved_modules = isolate_utils_modules()
        self.addCleanup(restore_utils_modules, saved_modules)

        self.hosted_bots = importlib.import_module("utils.hosted_bots")
        self.reseller = importlib.import_module("utils.reseller")
        self.hosted_bots.BOT_DIR = str(root)
        self.hosted_bots.HOSTED_ROOT = str(root / "hosted_bots")
        self.hosted_bots.REGISTRY_FILE = str(root / "hosted_bots.json")
        self.hosted_bots.SECRETS_FILE = str(root / "hosted_bot_tokens.json")
        self.reseller.RESELLERS_FILE = str(root / "resellers.json")
        Path(self.hosted_bots.SECRETS_FILE).write_text(json.dumps({"7": "123:abc"}), encoding="utf-8")
        Path(self.hosted_bots.REGISTRY_FILE).write_text(
            json.dumps({"7": {"status": "starting", "enabled": True}}), encoding="utf-8"
        )
        Path(self.reseller.RESELLERS_FILE).write_text(
            json.dumps({"7": {"status": "approved", "debt": 0, "configs": []}}), encoding="utf-8"
        )
        self.worker = load_module(
            "hosted_worker_hardening_test", BOT_DIR / "hosted_worker.py"
        )
        self.addCleanup(
            lambda: sys.modules.pop("hosted_worker_hardening_test", None)
        )

    def test_hosted_quote_uses_level_wholesale_and_catalog_based_retail(self):
        Path(self.reseller.RESELLERS_FILE).write_text(
            json.dumps({
                "7": {
                    "status": "approved",
                    "debt": 0,
                    "total_paid": 50,
                    "configs": [],
                }
            }),
            encoding="utf-8",
        )

        quote = self.worker._hosted_plan_quote(
            {"price": 100.0},
            {
                "markup_percent": 20,
                "referral_margin_percent": 0,
            },
        )

        self.assertEqual(quote["wholesale"], 75.0)
        self.assertEqual(quote["retail"], 120.0)
        self.assertEqual(quote["reseller_level"], 6)
        self.assertEqual(quote["discount_percent"], 25)

    def test_hosted_customer_config_guidance_is_success_only_and_owner_can_opt_out(self):
        client = mock.Mock()
        client.get_user_uri.return_value = {
            "normal_sub": "https://example.com/sub",
            "ipv4": "",
        }

        with (
            mock.patch.object(self.worker.bot, "send_photo"),
            mock.patch.object(self.worker.bot, "send_message"),
            mock.patch.object(self.worker, "send_download_prompt_safely") as guidance,
        ):
            self.worker._deliver_config(100, "hs7", client)
            guidance.assert_called_once_with(
                self.worker.bot,
                100,
                "en",
                callback_prefix="hb:download",
            )

            guidance.reset_mock()
            self.worker._deliver_config(7, "h7", client, include_downloads=False)
            guidance.assert_not_called()

            guidance.reset_mock()
            client.get_user_uri.return_value = None
            self.worker._deliver_config(100, "hs7", client)
            guidance.assert_not_called()

    def age_payment_claim(self, payment_id):
        path = self.hosted_bots.tenant_file("7", "payments.json")
        with self.worker.locked_json(path, {}) as payments:
            payments[payment_id]["processing_started_at"] = (
                datetime.now() - timedelta(hours=1)
            ).strftime("%Y-%m-%d %H:%M:%S")

    def test_stale_payment_claim_can_be_retried_after_a_crash(self):
        self.worker._save_payment("payment", {"status": "pending", "gateway_payment_id": "gateway"})

        first = self.worker._claim_payment("payment", {"pending"})
        duplicate = self.worker._claim_payment("payment", {"pending"})
        self.age_payment_claim("payment")
        recovered = self.worker._claim_payment("payment", {"pending"})

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["processing_attempts"], 2)

    def test_startup_recovers_legacy_or_stale_processing_records(self):
        self.worker._save_payment("payment", {"status": "pending_approval"})
        self.worker._claim_payment("payment", {"pending_approval"})
        self.age_payment_claim("payment")

        recovered = self.worker._recover_stale_payment_claims()

        self.assertEqual(recovered, ["payment"])
        self.assertEqual(self.worker._tenant_payments()["payment"]["status"], "pending_approval")

    def test_provisioning_exception_releases_claim_for_retry(self):
        self.worker._save_payment("payment", {"status": "pending"})
        record = self.worker._claim_payment("payment", {"pending"})

        with mock.patch.object(self.worker, "_provision_payment", side_effect=RuntimeError("boom")):
            success, detail = self.worker._provision_claimed_payment(
                "payment", record, funded=True, retry_status="paid_provision_failed"
            )

        self.assertFalse(success)
        self.assertIn("RuntimeError", detail)
        self.assertEqual(self.worker._tenant_payments()["payment"]["status"], "paid_provision_failed")

    def test_invite_discount_reservation_release_and_redemption_are_idempotent(self):
        referrals_path = self.hosted_bots.tenant_file("7", "referrals.json")
        with self.worker.locked_json(referrals_path, self.worker._referral_data()) as data:
            data.setdefault("referrals", {})["100"] = "200"

        with mock.patch.object(self.worker, "BUYER_DISCOUNTS_ENABLED", True):
            self.assertTrue(self.worker._reserve_invite_discount(100, "first"))
            self.assertTrue(self.worker._reserve_invite_discount(100, "first"))
            self.assertFalse(self.worker._reserve_invite_discount(100, "second"))
            self.assertFalse(self.worker._release_invite_discount(100, "wrong"))
            self.assertTrue(self.worker._release_invite_discount(100, "first"))
            self.assertFalse(self.worker._release_invite_discount(100, "first"))

            self.assertTrue(self.worker._reserve_invite_discount(100, "second"))
            self.assertTrue(self.worker._redeem_invite_discount(100, "second"))
            self.assertTrue(self.worker._redeem_invite_discount(100, "second"))
            self.assertFalse(self.worker._release_invite_discount(100, "second"))
            self.assertFalse(self.worker._reserve_invite_discount(100, "third"))

        data = self.worker._referral_data()
        self.assertFalse(data["buyer_discount_reservations"])
        self.assertEqual(data["buyer_discount_redeemed"]["100"]["order_id"], "second")

    def test_settlement_recomputes_margin_and_rejects_financial_invariant_violations(self):
        valid = {
            "payment_method": "crypto",
            "original_price": 120,
            "collected_amount": 108,
            "crypto_collected": 108,
            "wholesale_price": 80,
            "margin": 28,
            "reward_calculation_base": 28,
            "referral_reward": 5.6,
            "invite_discount_percent": 5,
            "crypto_discount_percent": 5,
            "total_discount_percent": 10,
            "invite_discount_amount": 6,
            "crypto_discount_amount": 6,
            "total_discount_amount": 12,
        }

        settlement = self.worker._settlement_financials(valid)
        self.assertEqual(settlement["collected_amount"], 108.0)
        self.assertEqual(settlement["margin"], 28.0)
        self.assertEqual(settlement["reward_calculation_base"], 28.0)

        invalid_records = (
            {**valid, "collected_amount": 79.99, "margin": 0, "reward_calculation_base": 0},
            {**valid, "referral_reward": 28.01},
            {**valid, "reward_calculation_base": 27.99},
            {**valid, "crypto_discount_percent": 5.01, "total_discount_percent": 10.01},
            {**valid, "total_discount_amount": 11.99},
            {**valid, "invite_discount_amount": 5.99},
            {**valid, "account_credit_reserved": 0.01},
            {**valid, "payment_method": "account_credit"},
        )
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.worker._settlement_financials(record)

    def test_hosted_completion_never_applies_main_account_credit_before_side_effects(self):
        record = {
            "user_id": 100,
            "payment_method": "card",
            "collected_amount": 6,
            "retail_price": 6,
            "wholesale_price": 5,
            "margin": 1,
            "referral_reward": 0,
            "account_credit_consumed": 1,
        }

        with mock.patch.object(self.worker, "get_reseller_data") as reseller_lookup:
            success, detail = self.worker._provision_payment("order", record, funded=False)

        self.assertFalse(success)
        self.assertIn("credit", detail.lower())
        reseller_lookup.assert_not_called()

    def test_referral_accounting_is_idempotent_and_missing_ledger_writes_fail_closed(self):
        referrals_path = self.hosted_bots.tenant_file("7", "referrals.json")
        with self.worker.locked_json(referrals_path, self.worker._referral_data()) as data:
            data.setdefault("referrals", {})["100"] = "200"

        with mock.patch.object(self.worker.bot, "send_message"):
            first = self.worker._credit_sale_and_referral(
                "card-order",
                100,
                2,
                {"retail_order_id": "card-order"},
                funded=False,
                margin=10,
            )
            ledger_path = self.hosted_bots.tenant_file("7", "ledger.json")
            with self.worker.locked_json(ledger_path, {}) as ledger:
                ledger.setdefault("transactions", []).append({
                    "id": "unrelated-debit",
                    "type": "withdrawal_requested",
                    "amount": -3,
                })
            duplicate = self.worker._credit_sale_and_referral(
                "card-order",
                100,
                2,
                {"retail_order_id": "card-order"},
                funded=False,
                margin=10,
            )

        self.assertEqual(first, 2.0)
        self.assertEqual(duplicate, 2.0)
        ledger = self.hosted_bots.get_ledger("7")
        self.assertEqual(ledger["referral_liability"], 2.0)
        stats = self.worker._referral_data()["stats"]["200"]
        self.assertEqual(stats["available_balance"], 2.0)

        with (
            mock.patch.object(self.worker, "add_referral_liability", return_value=False),
            self.assertRaisesRegex(RuntimeError, "not persisted"),
        ):
            self.worker._credit_sale_and_referral(
                "missing-order",
                100,
                1,
                {"retail_order_id": "missing-order"},
                funded=False,
                margin=10,
            )

    def growth_comparison_report(self):
        end = datetime(2026, 8, 2, tzinfo=timezone.utc)
        current_start = end - timedelta(days=30)
        baseline_start = current_start - timedelta(days=30)
        funnels = {
            name: {
                "started": 4,
                "completed": 2,
                "conversion_percent": 50.0,
                "baseline_started": 4,
                "baseline_completed": 1,
                "baseline_conversion_percent": 25.0,
                "relative_change_percent": 100.0,
                "customer_ids": ["must-not-render"],
            }
            for name in ("trial_to_paid", "checkout", "renewal", "referral")
        }
        return {
            "surface": "hosted",
            "hosted_tenant_id": "7",
            "days": 30,
            "baseline_start": baseline_start,
            "current_start": current_start,
            "end_at": end,
            "funnels": funnels,
        }

    def test_owner_growth_comparison_is_localized_and_aggregate_only(self):
        report = self.growth_comparison_report()
        rendered = {}

        for language in ("en", "fa", "ru", "tk"):
            with self.subTest(language=language):
                self.worker._set_language(self.worker.OWNER_ID, language)
                text = self.worker._owner_growth_comparison_text(report)
                rendered[language] = text
                self.assertIn("2026-07-03", text)
                self.assertIn("2026-08-01", text)
                self.assertIn("2/4", text)
                self.assertIn("1/4", text)
                self.assertIn("+100.0", text)
                self.assertNotIn("must-not-render", text)

        for language in ("fa", "ru", "tk"):
            self.assertNotEqual(rendered[language], rendered["en"])

    def test_owner_stats_uses_tenant_scoped_prior_period_comparison(self):
        reporting = importlib.import_module("utils.growth_reporting")
        report = self.growth_comparison_report()
        self.worker._set_language(self.worker.OWNER_ID, "en")

        with (
            mock.patch.object(self.worker, "_owner_stats_chunks", return_value=["base stats"]),
            mock.patch.object(
                reporting,
                "hosted_growth_comparison",
                return_value=report,
            ) as comparison,
            mock.patch.object(self.worker.bot, "send_message") as send_message,
        ):
            sent = self.worker._send_owner_stats(
                self.worker.OWNER_ID,
                end_date=date(2026, 8, 1),
            )

        self.assertEqual(sent, 2)
        comparison.assert_called_once_with(
            self.worker.OWNER_ID,
            end_at=datetime(2026, 8, 2),
            days=30,
        )
        comparison_text = send_message.call_args_list[1].args[1]
        self.assertIn("Available prior 30-day baseline", comparison_text)
        self.assertIn("relative change", comparison_text)

    def test_paid_provisioning_persists_hs_allocation_before_vpn_creation(self):
        record = {
            "user_id": 100,
            "telegram_username": "buyer",
            "plan_gb": "30",
            "days": 30,
            "wholesale_price": 5,
            "retail_price": 6,
            "margin": 1,
            "referral_reward": 0,
        }
        client = mock.Mock(server_id="server-1")

        def create_user(plan, note, **kwargs):
            self.assertEqual(note, "")
            self.assertEqual(kwargs["customer_id"], 100)
            self.assertEqual(kwargs["operation_id"], "order")
            self.assertEqual(kwargs["username_prefix"], "hs")
            kwargs["on_username_allocated"]("hs7", client)
            pending = self.worker._tenant_payments()["order"]
            self.assertEqual(pending["provisioned_username"], "hs7")
            self.assertEqual(pending["provisioned_server_id"], "server-1")
            return "hs7", {"created": True}, client

        with (
            mock.patch.object(self.worker, "get_reseller_data", return_value={"configs": []}),
            mock.patch.object(self.worker, "_create_user", side_effect=create_user),
            mock.patch.object(self.worker, "record_funded_reseller_config", return_value=True),
            mock.patch.object(self.worker, "credit_crypto_sale", return_value=True),
            mock.patch.object(self.worker, "_deliver_config_safely", return_value=True),
        ):
            success, username = self.worker._provision_payment("order", record, funded=True)

        completed = self.worker._tenant_payments()["order"]
        self.assertTrue(success)
        self.assertEqual(username, "hs7")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["provisioned_username"], "hs7")
        self.assertEqual(completed["provisioned_server_id"], "server-1")

    def test_reserved_renewal_settles_finances_without_resetting_the_live_config(self):
        Path(self.reseller.RESELLERS_FILE).write_text(
            json.dumps({
                "7": {
                    "status": "approved",
                    "debt": 0,
                    "total_paid": 0,
                    "configs": [{
                        "username": "customer1",
                        "server_id": "server-1",
                        "gb": "5",
                        "days": 30,
                        "price": 4,
                    }],
                }
            }),
            encoding="utf-8",
        )
        record = {
            "user_id": 100,
            "telegram_username": "buyer",
            "renew_username": "customer1",
            "server_id": "server-1",
            "plan_gb": "5",
            "days": 30,
            "unlimited": False,
            "wholesale_price": 4,
            "retail_price": 5,
            "list_price": 5,
            "reseller_level": 1,
            "discount_percent": 20,
            "margin": 1,
            "referral_reward": 0,
            "renewal_mode": "reserved",
            "renewal_baseline": {"status": "active", "gb_used": 1},
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "price": 4},
            "status": "processing",
        }
        self.worker._save_payment("order", record)

        with (
            mock.patch.object(self.worker, "_credit_sale_and_referral") as credit_sale,
            mock.patch.object(self.worker, "release_credit", return_value=True) as release_credit,
            mock.patch.object(self.worker.bot, "send_message"),
        ):
            success, username = self.worker._settle_hosted_reserved_renewal(
                "order", record, funded=False
            )

        self.assertTrue(success)
        self.assertEqual(username, "customer1")
        release_credit.assert_called_once_with(7, "order", kind="renewal_credit_consumed")
        credit_sale.assert_called_once()
        payment = self.worker._tenant_payments()["order"]
        reseller = self.reseller.get_reseller_data("7")
        reservation = reseller["configs"][0]["renewals"][0]
        self.assertEqual(payment["status"], "completed")
        self.assertEqual(payment["renewal_status"], "reserved")
        self.assertEqual(reservation["renewal_status"], "reserved")
        self.assertEqual(reseller["debt"], 4.0)
        self.assertEqual(reservation["debt_charge_id"], reseller["debt_charges"][0]["id"])

    def test_funded_hosted_reservation_records_paid_wholesale_without_debt(self):
        Path(self.reseller.RESELLERS_FILE).write_text(
            json.dumps({
                "7": {
                    "status": "approved",
                    "debt": 0,
                    "total_paid": 0,
                    "configs": [{
                        "username": "customer1",
                        "server_id": "server-1",
                        "gb": "5",
                        "days": 30,
                        "price": 4,
                    }],
                }
            }),
            encoding="utf-8",
        )
        record = {
            "user_id": 100,
            "telegram_username": "buyer",
            "renew_username": "customer1",
            "server_id": "server-1",
            "plan_gb": "5",
            "days": 30,
            "unlimited": False,
            "wholesale_price": 4,
            "retail_price": 5,
            "list_price": 5,
            "reseller_level": 1,
            "discount_percent": 20,
            "margin": 1,
            "referral_reward": 0,
            "renewal_mode": "reserved",
            "renewal_baseline": {"status": "active", "gb_used": 1},
            "status": "processing",
        }
        self.worker._save_payment("crypto-order", record)

        with (
            mock.patch.object(self.worker, "_credit_sale_and_referral") as credit_sale,
            mock.patch.object(self.worker, "present_pending_reseller_level") as present_level,
            mock.patch.object(self.worker.bot, "send_message"),
        ):
            success, username = self.worker._settle_hosted_reserved_renewal(
                "crypto-order", record, funded=True
            )

        reseller = self.reseller.get_reseller_data("7")
        reservation = reseller["configs"][0]["renewals"][0]
        self.assertTrue(success)
        self.assertEqual(username, "customer1")
        self.assertEqual(reseller["debt"], 0.0)
        self.assertEqual(reseller["total_paid"], 4.0)
        self.assertTrue(reservation["funded_at_checkout"])
        self.assertNotIn("debt_charge_id", reservation)
        credit_sale.assert_called_once()
        present_level.assert_called_once()

    def test_hosted_outage_alerts_owner_before_customer_and_marks_audiences_separately(self):
        renewal = importlib.import_module("utils.renewal")
        payment_marks = []
        reseller_marks = []
        self.worker.bot = mock.Mock()
        self.worker._sync_hosted_renewal_event = mock.Mock(return_value=True)
        self.worker._message = lambda _user_id, key: key
        self.worker._hosted_message = lambda _user_id, key, **values: (
            f"{key}:{values.get('username')}"
        )
        event = {
            "payment_id": "reservation-1",
            "status": "attention",
            "reason": "server_unavailable",
            "operator_alert_due": True,
            "buyer_alert_due": False,
            "alert_due": True,
            "record": {
                "user_id": 99,
                "renew_username": "alice",
                "server_id": "s1",
            },
        }

        with mock.patch.object(
            renewal,
            "mark_payment_renewal_alerted",
            side_effect=lambda *args, **kwargs: payment_marks.append((args, kwargs)),
        ), mock.patch.object(
            self.reseller,
            "mark_reseller_renewal_alerted",
            side_effect=lambda *args, **kwargs: reseller_marks.append((args, kwargs)),
        ):
            self.worker._handle_hosted_renewal_event(event)
            first_calls = list(self.worker.bot.send_message.call_args_list)
            self.assertEqual(len(first_calls), 1)
            self.assertEqual(first_calls[0].args[0], self.worker.OWNER_ID)
            self.assertIn("Server: `s1`", first_calls[0].args[1])
            markup = first_calls[0].kwargs["reply_markup"]
            buttons = getattr(markup, "buttons", None)
            if buttons is None:
                buttons = [button for row in markup.keyboard for button in row]
            callbacks = [
                getattr(button, "callback_data", None) or getattr(button, "kwargs", {}).get("callback_data")
                for button in buttons
            ]
            self.assertEqual(callbacks, ["hb:rr:retry:reservation-1"])
            self.assertEqual(payment_marks[-1][1]["audience"], "operator")
            self.assertEqual(reseller_marks[-1][1]["audience"], "operator")

            self.worker.bot.reset_mock()
            event.update({"operator_alert_due": False, "buyer_alert_due": True})
            self.worker._handle_hosted_renewal_event(event)

        second_calls = list(self.worker.bot.send_message.call_args_list)
        self.assertEqual(len(second_calls), 1)
        self.assertEqual(second_calls[0].args[0], 99)
        self.assertIn("renewal_reserved_server_unavailable", second_calls[0].args[1])
        self.assertEqual(payment_marks[-1][1]["audience"], "buyer")
        self.assertEqual(reseller_marks[-1][1]["audience"], "buyer")

    def test_hosted_outage_recovery_syncs_the_reseller_mirror_back_to_reserved(self):
        with mock.patch.object(
            self.worker,
            "sync_reseller_renewal_reservation",
            return_value=True,
        ) as sync:
            result = self.worker._sync_hosted_renewal_event({
                "payment_id": "reservation-1",
                "status": "waiting",
            })

        self.assertTrue(result)
        sync.assert_called_once_with(
            self.worker.OWNER_ID,
            "reservation-1",
            "reserved",
            fields={},
        )

    def test_stale_hosted_test_recovers_persisted_ht_allocation(self):
        message = mock.Mock()
        message.from_user.id = 100
        message.chat.id = 100
        client = mock.Mock(server_id="server-1")

        def interrupted_create(plan, note, **kwargs):
            self.assertEqual(note, "")
            self.assertEqual(kwargs["customer_id"], 100)
            self.assertEqual(kwargs["username_prefix"], "ht")
            kwargs["on_username_allocated"]("ht7", client)
            raise RuntimeError("simulated worker crash")

        with (
            mock.patch.object(self.worker, "_create_user", side_effect=interrupted_create),
            mock.patch.object(self.worker.bot, "reply_to"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated worker crash"):
                self.worker.free_test(message)

        with self.worker.locked_json(self.worker.GLOBAL_TEST_FILE, {}) as tests:
            pending = tests["100"]
            self.assertEqual(pending["username"], "ht7")
            self.assertEqual(pending["server_id"], "server-1")
            pending["creation_pending_at"] = (
                datetime.now()
                - timedelta(seconds=self.worker.TEST_CREATION_LEASE_SECONDS + 1)
            ).strftime("%Y-%m-%d %H:%M:%S")

        live = {"username": "ht7"}

        class RecoveryMultiServerAPI:
            def find_user(self, username, preferred_server_id=None):
                self_test.assertEqual(username, "ht7")
                self_test.assertEqual(preferred_server_id, "server-1")
                return client, live

        self_test = self
        with (
            mock.patch.object(self.worker, "MultiServerAPI", RecoveryMultiServerAPI),
            mock.patch.object(self.worker, "_create_user") as create_user,
            mock.patch.object(self.worker, "_deliver_config_safely") as deliver,
            mock.patch.object(self.worker.bot, "reply_to"),
        ):
            self.worker.free_test(message)

        create_user.assert_not_called()
        deliver.assert_called_once_with(100, "ht7", client)
        recovered = self.worker.read_json(self.worker.GLOBAL_TEST_FILE, {})["100"]
        self.assertEqual(recovered["username"], "ht7")
        self.assertEqual(recovered["server_id"], "server-1")
        self.assertIsNone(recovered["creation_pending_at"])
        self.assertTrue(recovered["used_at"])

    def test_renewal_tokens_survive_process_memory_and_are_user_bound(self):
        token = self.worker._store_renewal_token(100, {"username": "customer", "server_id": "a"})

        self.assertIsNone(self.worker._consume_renewal_token(token, 101))
        second = self.worker._store_renewal_token(100, {"username": "customer", "server_id": "a"})
        renewal = self.worker._consume_renewal_token(second, 100)

        self.assertEqual(renewal, {"username": "customer", "server_id": "a"})
        self.assertIsNone(self.worker._consume_renewal_token(second, 100))

    def test_russian_hosted_purchase_and_renewal_are_crypto_only_without_exchange_rate(self):
        settings = {
            "card_number": "1234",
            "crypto_enabled": True,
            "referral_margin_percent": 20,
        }
        quote = {
            "card_supported": True,
            "crypto_supported": True,
            "card_collected": 10.0,
            "crypto_collected": 9.5,
            "buyer_discount_percent": 0,
        }
        renewal = {"username": "customer", "server_id": "s1"}

        with (
            mock.patch.object(self.worker, "_language", return_value="ru"),
            mock.patch.object(self.worker, "_reseller", return_value={"status": "approved"}),
            mock.patch.object(self.worker, "_sellable_plans", return_value={
                "10": {"price": 10, "gb": 10, "days": 30}
            }),
            mock.patch.object(self.worker, "get_settings", return_value=settings),
            mock.patch.object(self.worker, "_hosted_plan_quote", return_value=quote),
            mock.patch.object(self.worker, "_invite_discount_preview", return_value=0),
            mock.patch.object(self.worker, "_referral_data", return_value={"referrals": {}}),
            mock.patch.object(self.worker, "_claim_risk_disclosure", return_value=False),
            mock.patch.object(self.worker, "_record_growth"),
            mock.patch.object(self.worker, "_store_renewal_token", return_value="renew-token"),
            mock.patch.object(self.worker, "get_exchange_rate", side_effect=AssertionError("rate requested")),
            mock.patch.object(self.worker.bot, "edit_message_text") as edit_message,
            mock.patch.dict(os.environ, {
                "CRYPTO_MERCHANT_ID": "merchant",
                "CRYPTO_API_KEY": "key",
            }),
        ):
            self.worker._purchase_options(555, 100, "10", message_id=90)
            self.worker._purchase_options(555, 100, "10", renewal=renewal, message_id=91)

        self.assertEqual(edit_message.call_count, 2)
        for checkout_call in edit_message.call_args_list:
            text = checkout_call.args[0]
            buttons = [
                button
                for row in checkout_call.kwargs["reply_markup"].keyboard
                for button in row
            ]
            self.assertIn("$9.50", text)
            self.assertNotIn("томан", text.casefold())
            self.assertFalse(any("hb:pay:card:" in button.callback_data for button in buttons))
            self.assertTrue(any("hb:pay:crypto:" in button.callback_data for button in buttons))

    def test_russian_hosted_card_callback_is_rejected_before_renewal_token_consumption(self):
        call = mock.Mock()
        call.data = "hb:pay:card:10:renew-token"
        call.id = "callback"
        call.from_user.id = 100

        with (
            mock.patch.object(self.worker, "_language", return_value="ru"),
            mock.patch.object(
                self.worker,
                "_consume_renewal_token",
                side_effect=AssertionError("renewal token consumed"),
            ),
            mock.patch.object(
                self.worker,
                "get_exchange_rate",
                side_effect=AssertionError("rate requested"),
            ),
            mock.patch.object(self.worker, "_reserve_invite_discount") as reserve_discount,
            mock.patch.object(self.worker.bot, "answer_callback_query") as answer,
        ):
            self.worker.payment_method(call)

        reserve_discount.assert_not_called()
        answer.assert_called_once()
        self.assertTrue(answer.call_args.kwargs["show_alert"])
        self.assertIn("метод", answer.call_args.args[1].casefold())

    def test_hosted_renewal_is_revalidated_and_becomes_immediate_if_now_expired(self):
        reseller_data = {
            "status": "approved",
            "debt": 0,
            "total_paid": 0,
            "configs": [{
                "username": "customer",
                "server_id": "s1",
                "customer_telegram_id": 100,
                "gb": "5",
                "days": 30,
                "unlimited": False,
                "price": 4,
            }],
        }
        live = {
            "blocked": True,
            "expiration_days": 0,
            "upload_bytes": 5 * 1024 ** 3,
            "download_bytes": 0,
            "max_download_bytes": 5 * 1024 ** 3,
            "status": "expired",
        }
        client = mock.Mock(server_id="s1")
        multi_api = mock.Mock()
        multi_api.find_user.return_value = client, live

        with (
            mock.patch.object(self.worker, "get_reseller_data", return_value=reseller_data),
            mock.patch.object(self.worker, "MultiServerAPI", return_value=multi_api),
            mock.patch.object(self.worker, "_sellable_plans", return_value={
                "5": {"price": 5, "days": 30, "unlimited": False, "target": "both"}
            }),
        ):
            renewal, error = self.worker._resolve_hosted_renewal_checkout(
                100,
                "5",
                {"username": "customer", "server_id": "s1", "config_index": 0},
            )

        self.assertIsNone(error)
        self.assertEqual(renewal["renewal_mode"], "immediate")
        self.assertEqual(renewal["renewal_baseline"]["status"], "expired")

    def test_multiple_live_checkouts_can_be_created_per_customer(self):
        record = {"user_id": 100, "payment_method": "crypto"}

        first = self.worker._start_checkout("one", record)
        second = self.worker._start_checkout("two", record)

        self.assertEqual(first, (True, "one"))
        self.assertEqual(second, (True, "two"))
        self.assertEqual(set(self.worker._tenant_payments()), {"one", "two"})

    def test_checkout_id_collision_cannot_replace_an_existing_order(self):
        self.worker._start_checkout("order", {"user_id": 100, "plan_gb": "10"})

        duplicate = self.worker._start_checkout("order", {"user_id": 200, "plan_gb": "50"})

        self.assertEqual(duplicate, (False, "order"))
        self.assertEqual(self.worker._tenant_payments()["order"]["user_id"], 100)

    def test_duplicate_taps_on_one_payment_button_reuse_the_live_checkout(self):
        record = {
            "user_id": 100,
            "payment_method": "crypto",
            "checkout_source": "100:50:crypto:10",
        }

        first = self.worker._start_checkout("one", record)
        duplicate = self.worker._start_checkout("two", record)
        self.worker._save_payment("one", {"status": "failed"})
        retry = self.worker._start_checkout("two", record)
        independent = self.worker._start_checkout(
            "three", {**record, "checkout_source": "100:51:crypto:10"}
        )

        self.assertEqual(first, (True, "one"))
        self.assertEqual(duplicate, (False, "one"))
        self.assertEqual(retry, (True, "two"))
        self.assertEqual(independent, (True, "three"))

    def test_reserved_checkout_is_unique_per_active_config_across_messages(self):
        record = {
            "user_id": 100,
            "payment_method": "crypto",
            "checkout_source": "100:50:crypto:10",
            "renewal_mode": "reserved",
            "renew_username": "active-config",
            "server_id": "s1",
        }

        first = self.worker._start_checkout("one", record)
        duplicate = self.worker._start_checkout(
            "two", {**record, "checkout_source": "100:51:card:10", "payment_method": "card"}
        )
        self.worker._save_payment("one", {"status": "completed", "renewal_status": "applied"})
        next_cycle = self.worker._start_checkout(
            "two", {**record, "checkout_source": "100:52:crypto:10"}
        )

        self.assertEqual(first, (True, "one"))
        self.assertEqual(duplicate, (False, "one"))
        self.assertEqual(next_cycle, (True, "two"))

    def test_multiple_card_receipts_use_active_latest_or_replied_checkout(self):
        for payment_id, message_id in (("one", 11), ("two", 22)):
            self.worker._save_payment(
                payment_id,
                {
                    "user_id": 100,
                    "payment_method": "card",
                    "status": "waiting_receipt",
                    "receipt_prompt_chat_id": 100,
                    "receipt_prompt_message_id": message_id,
                },
            )

        self.assertEqual(self.worker._receipt_checkout(100), "two")

        self.worker._set_input_state(100, {"kind": "receipt", "payment_id": "one"})

        self.assertEqual(self.worker._receipt_checkout(100), "one")
        self.assertEqual(
            self.worker._receipt_checkout(100, reply_message_id=22, chat_id=100),
            "two",
        )
        self.worker._save_payment("one", {"status": "processing"})
        self.assertEqual(self.worker._receipt_checkout(100), "one")
        self.assertFalse(
            self.worker._clear_input_state(100, kind="receipt", payment_id="two")
        )
        self.assertEqual(self.worker._receipt_checkout(100), "one")
        self.assertTrue(
            self.worker._clear_input_state(100, kind="receipt", payment_id="one")
        )

    def test_saved_receipt_survives_owner_notification_failure_and_retries(self):
        receipt_path = Path(self.hosted_bots.tenant_file("7", "receipts/order.jpg"))
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(b"receipt")
        self.worker._save_payment(
            "order",
            {
                "user_id": 100,
                "status": "pending_approval",
                "receipt_path": str(receipt_path),
                "plan_gb": "10",
                "days": 30,
                "retail_price": 5,
                "converted_amount": 500000,
            },
        )

        with mock.patch.object(
            self.worker.bot, "send_photo", side_effect=RuntimeError("owner unavailable")
        ), mock.patch("builtins.print"):
            notified = self.worker._notify_owner_of_receipt("order")

        failed = self.worker._tenant_payments()["order"]
        self.assertFalse(notified)
        self.assertEqual(failed["status"], "pending_approval")
        self.assertIn("owner unavailable", failed["owner_receipt_notification_error"])

        with mock.patch.object(self.worker.bot, "send_photo") as send_photo:
            notified = self.worker._notify_owner_of_receipt("order")

        completed = self.worker._tenant_payments()["order"]
        self.assertTrue(notified)
        send_photo.assert_called_once()
        self.assertIn("owner_receipt_notified_at", completed)
        self.assertNotIn("owner_receipt_notification_error", completed)
        self.assertEqual(completed["status"], "pending_approval")

    def test_startup_recovers_a_receipt_saved_by_the_legacy_caption_failure(self):
        receipt_path = Path(self.hosted_bots.tenant_file("7", "receipts/legacy.jpg"))
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(b"receipt")
        self.worker._save_payment(
            "legacy",
            {
                "user_id": 100,
                "status": "waiting_receipt",
                "receipt_path": str(receipt_path),
                "last_error": "Receipt upload failed: TypeError",
            },
        )

        with mock.patch.object(
            self.worker, "_notify_owner_of_receipt", return_value=True
        ) as notify_owner, mock.patch.object(self.worker.bot, "send_message") as send_message:
            recovered = self.worker._recover_saved_receipts()

        record = self.worker._tenant_payments()["legacy"]
        self.assertEqual(recovered, ["legacy"])
        self.assertEqual(record["status"], "pending_approval")
        self.assertIn("receipt_recovered_at", record)
        self.assertNotIn("last_error", record)
        notify_owner.assert_called_once_with("legacy")
        send_message.assert_called_once()


class HostedSupervisorHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_role = os.environ.get("AJIB_BOT_ROLE")
        cls.previous_bot_dir = os.environ.get("AJIB_BOT_DIR")
        os.environ["AJIB_BOT_ROLE"] = "supervisor"
        os.environ["AJIB_BOT_DIR"] = str(BOT_DIR)
        cls.saved_utils_modules = isolate_utils_modules()
        cls.supervisor = load_module("supervisor_hardening_test", BOT_DIR / "supervisor.py")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("supervisor_hardening_test", None)
        restore_utils_modules(cls.saved_utils_modules)
        if cls.previous_role is None:
            os.environ.pop("AJIB_BOT_ROLE", None)
        else:
            os.environ["AJIB_BOT_ROLE"] = cls.previous_role
        if cls.previous_bot_dir is None:
            os.environ.pop("AJIB_BOT_DIR", None)
        else:
            os.environ["AJIB_BOT_DIR"] = cls.previous_bot_dir

    def test_hosted_token_is_not_exposed_in_child_environment(self):
        worker = self.supervisor._hosted_worker("7", {"bot_id": "123", "username": "shopbot"})

        self.assertNotIn("AJIB_HOSTED_BOT_TOKEN", worker.env)
        self.assertEqual(worker.env["AJIB_HOSTED_RESELLER_ID"], "7")

    def test_spawn_failure_is_contained_and_backed_off(self):
        worker = self.supervisor.Worker("7", ["missing"], {}, hosted=True)
        with (
            mock.patch.object(self.supervisor.subprocess, "Popen", side_effect=OSError("missing")),
            mock.patch.object(self.supervisor, "set_bot_runtime_status") as set_status,
        ):
            started = worker.start()

        self.assertFalse(started)
        self.assertIsNone(worker.process)
        self.assertEqual(worker.failures, 1)
        self.assertEqual(set_status.call_args_list[0].args[1], "starting")
        self.assertEqual(set_status.call_args_list[-1].args[1], "error")

    def test_stable_uptime_resets_accumulated_restart_backoff(self):
        worker = self.supervisor.Worker("7", ["worker"], {}, hosted=True)
        worker.process = mock.Mock()
        worker.process.poll.return_value = None
        worker.started_at = 0
        worker.failures = 5
        worker.next_start = 100

        with mock.patch.object(
            self.supervisor.time, "monotonic", return_value=self.supervisor.STABLE_UPTIME_SECONDS + 1
        ):
            worker.poll()

        self.assertEqual(worker.failures, 0)
        self.assertEqual(worker.next_start, 0)


if __name__ == "__main__":
    unittest.main()
