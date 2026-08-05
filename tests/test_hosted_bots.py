import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
sys.path.insert(0, str(BOT_DIR))
os.environ["AJIB_BOT_ROLE"] = "supervisor"

from utils import hosted_bots
from utils import reseller
from utils.atomic_store import locked_json


class HostedBotStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hosted_bots.BOT_DIR = str(root)
        hosted_bots.HOSTED_ROOT = str(root / "hosted_bots")
        hosted_bots.REGISTRY_FILE = str(root / "hosted_bots.json")
        hosted_bots.SECRETS_FILE = str(root / "hosted_bot_tokens.json")
        reseller.RESELLERS_FILE = str(root / "resellers.json")

    def write_resellers(self, value):
        Path(reseller.RESELLERS_FILE).write_text(json.dumps(value), encoding="utf-8")

    def test_quote_distinguishes_crypto_and_card_margin(self):
        quote = hosted_bots.calculate_quote(100, 20, referral_margin_percent=20, referred=True)

        self.assertEqual(quote["retail"], 120.0)
        self.assertEqual(quote["crypto_collected"], 114.0)
        self.assertEqual(quote["crypto_margin"], 14.0)
        self.assertEqual(quote["card_margin"], 20.0)
        self.assertEqual(quote["crypto_referral_reward"], 2.8)
        self.assertEqual(quote["card_referral_reward"], 4.0)

    def test_level_wholesale_discount_increases_margin_without_changing_retail(self):
        level_one = hosted_bots.calculate_quote(
            80,
            20,
            retail_base=100,
        )
        level_six = hosted_bots.calculate_quote(
            75,
            20,
            retail_base=100,
        )

        self.assertEqual(level_one["retail"], 120.0)
        self.assertEqual(level_six["retail"], 120.0)
        self.assertEqual(level_one["card_margin"], 40.0)
        self.assertEqual(level_six["card_margin"], 45.0)
        self.assertEqual(level_six["crypto_margin"], 39.0)

    def test_zero_price_increase_still_includes_level_profit_and_deductions(self):
        quote = hosted_bots.calculate_quote(
            75,
            0,
            referral_margin_percent=20,
            referred=True,
            retail_base=100,
        )

        self.assertEqual(quote["retail"], 100.0)
        self.assertEqual(quote["card_margin"], 25.0)
        self.assertEqual(quote["card_referral_reward"], 5.0)
        self.assertEqual(quote["crypto_collected"], 95.0)
        self.assertEqual(quote["crypto_margin"], 20.0)
        self.assertEqual(quote["crypto_referral_reward"], 4.0)

    def test_crypto_is_disabled_by_default(self):
        settings = hosted_bots.get_settings("7")
        self.assertFalse(settings["crypto_enabled"])
        self.assertFalse(settings["plan_selection_configured"])
        self.assertEqual(settings["setup_version"], 0)
        self.assertEqual(settings["setup_completed_steps"], [])

    def test_new_bot_setup_progress_requires_explicit_pricing_payment_and_plans(self):
        success, _record = hosted_bots.register_bot(
            "7", "123:secret", SimpleNamespace(id=123, username="shopbot"), main_bot_id=999
        )

        self.assertTrue(success)
        status = hosted_bots.get_setup_status("7")
        self.assertEqual(status["steps"], {"pricing": False, "payments": False, "plans": False})
        self.assertEqual(status["next_step"], "pricing")

        hosted_bots.mark_setup_step("7", "pricing")
        hosted_bots.update_settings("7", {"card_number": "1234"})
        hosted_bots.mark_setup_step("7", "plans")

        status = hosted_bots.get_setup_status("7")
        self.assertTrue(status["ready"])
        self.assertEqual(status["completed"], 3)

    def test_primary_bot_token_rejection_does_not_expose_private_identifier(self):
        success, message = hosted_bots.register_bot(
            "7", "123:secret", SimpleNamespace(id=999, username="mainbot"), main_bot_id=999
        )
        private_identifier = "".join(chr(code) for code in (97, 106, 105, 98))

        self.assertFalse(success)
        self.assertNotIn(private_identifier, message.casefold())
        self.assertIn("primary service bot", message.casefold())

    def test_legacy_setup_is_inferred_without_preserving_old_english_defaults(self):
        settings_path = Path(hosted_bots.tenant_file("7", "settings.json"))
        settings_path.write_text(json.dumps({
            "markup_percent": 20,
            "card_number": "1234",
            "welcome_text": "Welcome!",
            "support_text": "Contact the reseller for support.",
        }), encoding="utf-8")

        settings = hosted_bots.get_settings("7")
        status = hosted_bots.get_setup_status("7", settings=settings)

        self.assertEqual(settings["welcome_text"], "")
        self.assertEqual(settings["support_text"], "")
        self.assertTrue(status["ready"])
        self.assertEqual(status["version"], 0)

    def test_private_identifier_is_rejected_from_customer_messages(self):
        private_identifier = "".join(chr(code) for code in (97, 106, 105, 98))
        settings_path = Path(hosted_bots.tenant_file("7", "settings.json"))
        settings_path.write_text(json.dumps({
            "welcome_text": f"Welcome to {private_identifier.upper()}",
            "support_texts": {"en": f"{private_identifier} support"},
        }), encoding="utf-8")

        settings = hosted_bots.get_settings("7")

        self.assertEqual(settings["welcome_text"], "")
        self.assertEqual(settings["support_texts"], {})
        with self.assertRaises(ValueError):
            hosted_bots.update_settings("7", {
                "welcome_texts": {"en": f"Welcome to {private_identifier.title()}"},
            })

    def test_setup_readiness_reflects_runtime_payment_and_plan_availability(self):
        hosted_bots.update_settings("7", {
            "crypto_enabled": True,
            "setup_version": 1,
            "setup_completed_steps": ["pricing", "plans"],
        })

        self.assertFalse(hosted_bots.get_setup_status("7", crypto_available=False)["steps"]["payments"])
        self.assertFalse(hosted_bots.get_setup_status("7", plans_available=False)["steps"]["plans"])

    def test_replacing_a_connected_bot_preserves_setup_progress(self):
        hosted_bots.register_bot(
            "7", "123:secret", SimpleNamespace(id=123, username="shopbot"), main_bot_id=999
        )
        hosted_bots.mark_setup_step("7", "pricing")

        success, _record = hosted_bots.register_bot(
            "7", "456:secret", SimpleNamespace(id=456, username="newshopbot"), main_bot_id=999
        )

        self.assertTrue(success)
        self.assertIn("pricing", hosted_bots.get_settings("7")["setup_completed_steps"])

    def test_registration_separates_secret_and_rejects_duplicates(self):
        success, record = hosted_bots.register_bot(
            "7", "123:secret", SimpleNamespace(id=123, username="shopbot"), main_bot_id=999
        )
        duplicate, message = hosted_bots.register_bot(
            "8", "123:secret", SimpleNamespace(id=123, username="shopbot"), main_bot_id=999
        )

        self.assertTrue(success)
        self.assertFalse(duplicate)
        self.assertIn("already connected", message)
        self.assertNotIn("secret", json.dumps(hosted_bots.list_bots()))
        self.assertEqual(hosted_bots.get_token("7"), "123:secret")
        self.assertEqual(record["status"], "starting")
        mode = Path(hosted_bots.SECRETS_FILE).stat().st_mode & 0o777
        # Some Windows-mounted CI filesystems report 0777 even after chmod.
        if mode != 0o777:
            self.assertEqual(mode, 0o600)

    def test_credit_reservations_respect_total_available_credit(self):
        self.assertTrue(hosted_bots.reserve_credit("7", "a", 6, 10))
        self.assertFalse(hosted_bots.reserve_credit("7", "b", 5, 10))
        self.assertTrue(hosted_bots.release_credit("7", "a"))
        self.assertTrue(hosted_bots.reserve_credit("7", "b", 5, 10))

    def test_crypto_sale_credit_is_idempotent(self):
        self.assertTrue(hosted_bots.credit_crypto_sale("7", "order", 9, 2))
        self.assertFalse(hosted_bots.credit_crypto_sale("7", "order", 9, 2))
        ledger = hosted_bots.get_ledger("7")
        self.assertEqual(ledger["earnings_available"], 9.0)
        self.assertEqual(ledger["referral_liability"], 2.0)

    def test_credit_recovery_does_not_duplicate_reseller_debt(self):
        self.write_resellers({"7": {"status": "approved", "debt": 0.0, "configs": []}})
        config = {"username": "customer", "retail_order_id": "order", "price": 5.0}
        self.assertTrue(hosted_bots.reserve_credit("7", "order", 5, 10))
        # Simulate a crash after reseller persistence but before reservation cleanup.
        self.assertTrue(reseller.add_reseller_debt("7", 5, dict(config)))
        self.assertTrue(hosted_bots.consume_credit("7", "order", dict(config)))
        saved = reseller.get_reseller_data("7")
        self.assertEqual(saved["debt"], 5.0)
        self.assertEqual(len(saved["configs"]), 1)
        self.assertFalse(hosted_bots.get_ledger("7")["credit_reservations"])

    def test_funded_config_record_is_idempotent(self):
        self.write_resellers({"7": {"status": "approved", "debt": 0.0, "total_paid": 0.0, "configs": []}})
        config = {"username": "customer", "retail_order_id": "order", "price": 5.0}
        self.assertTrue(reseller.record_funded_reseller_config("7", 5, dict(config)))
        self.assertTrue(reseller.record_funded_reseller_config("7", 5, dict(config)))
        saved = reseller.get_reseller_data("7")
        self.assertEqual(saved["total_paid"], 5.0)
        self.assertEqual(len(saved["configs"]), 1)

    def test_earnings_can_settle_debt_then_be_withdrawn(self):
        self.write_resellers({
            "7": {"status": "approved", "debt": 10.0, "total_paid": 0.0, "configs": [{"price": 10.0}]}
        })
        hosted_bots.credit_crypto_sale("7", "order", 12)

        success, result = hosted_bots.transfer_earnings_to_debt("7")
        requested, withdrawal = hosted_bots.request_earnings_withdrawal("7", "TRC20 wallet")

        self.assertTrue(success)
        self.assertEqual(result["remaining_debt"], 0.0)
        saved_reseller = reseller.get_reseller_data("7")
        self.assertEqual(saved_reseller["debt_allocations"][0]["kind"], "earnings_transfer")
        transfer = next(
            item for item in hosted_bots.get_ledger("7")["transactions"]
            if item["type"] == "earnings_to_debt"
        )
        self.assertEqual(
            transfer["metadata"]["debt_allocation_id"],
            saved_reseller["debt_allocations"][0]["id"],
        )
        self.assertTrue(requested)
        self.assertEqual(withdrawal["amount"], 2.0)
        rejected, _ = hosted_bots.resolve_earnings_withdrawal(
            "7", withdrawal["id"], "rejected", admin_id=99
        )
        self.assertTrue(rejected)
        self.assertEqual(hosted_bots.get_ledger("7")["earnings_available"], 2.0)

    def test_withdrawal_is_blocked_while_debt_exists(self):
        self.write_resellers({"7": {"status": "approved", "debt": 1.0, "configs": []}})
        hosted_bots.credit_crypto_sale("7", "order", 10)

        success, message = hosted_bots.request_earnings_withdrawal("7", "wallet")

        self.assertFalse(success)
        self.assertIn("debt", message.lower())

    def test_invalid_financial_values_cannot_increase_liability_or_reserve_credit(self):
        self.assertTrue(hosted_bots.add_referral_liability("7", "order", 5))

        self.assertFalse(hosted_bots.settle_referral_liability("7", "withdrawal", -10))
        self.assertFalse(hosted_bots.reserve_credit("7", "negative", -5, 10))

        self.assertEqual(hosted_bots.get_ledger("7")["referral_liability"], 5.0)

    def test_stale_orphaned_credit_reservations_are_released(self):
        self.assertTrue(hosted_bots.reserve_credit("7", "orphan", 5, 10))

        released = hosted_bots.release_stale_credit_reservations(
            "7", set(), now=datetime.now() + timedelta(days=2)
        )

        self.assertEqual(released, ["orphan"])
        ledger = hosted_bots.get_ledger("7")
        self.assertFalse(ledger["credit_reservations"])
        self.assertIn("stale-release:orphan", {item["id"] for item in ledger["transactions"]})

    def test_settings_reject_non_finite_values_and_drop_legacy_exchange_rate(self):
        with self.assertRaises(ValueError):
            hosted_bots.update_settings("7", {"markup_percent": float("nan")})
        settings_path = Path(hosted_bots.tenant_file("7", "settings.json"))
        settings_path.write_text(
            json.dumps({"markup_percent": "invalid", "exchange_rate": 75000, "card_number": "1234"}),
            encoding="utf-8",
        )

        settings = hosted_bots.get_settings("7")

        self.assertEqual(settings["markup_percent"], 20.0)
        self.assertEqual(settings["card_number"], "1234")
        self.assertNotIn("exchange_rate", settings)

        hosted_bots.update_settings("7", {"welcome_text": "Hello"})
        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("exchange_rate", persisted)
        self.assertEqual(persisted["welcome_text"], "Hello")

    def test_tenant_paths_cannot_escape_private_reseller_directory(self):
        with self.assertRaises(ValueError):
            hosted_bots.tenant_file("7", "../../hosted_bot_tokens.json")

    def test_locked_updates_preserve_corrupt_json_for_recovery(self):
        path = Path(self.temp.name) / "damaged.json"
        path.write_text("{damaged", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            with locked_json(str(path), {}) as data:
                data["replacement"] = True

        self.assertEqual(path.read_text(encoding="utf-8"), "{damaged")


class HostedLifecycleScriptTests(unittest.TestCase):
    def test_systemd_service_runs_supervisor(self):
        script = (BOT_DIR / "runbot.sh").read_text(encoding="utf-8")
        self.assertIn("telegrambot/supervisor.py", script)
        self.assertNotIn("python /etc/ajib/core/scripts/telegrambot/tbot.py'", script)
        self.assertIn("After=network-online.target", script)
        self.assertIn("RestartSec=5s", script)
        self.assertIn("KillMode=control-group", script)
        self.assertIn("UMask=0077", script)

    def test_upgrade_migrates_existing_service(self):
        script = (ROOT / "upgrade.sh").read_text(encoding="utf-8")
        self.assertIn("supervisor.py", script)
        self.assertIn("ajib-telegram-bot.service", script)


if __name__ == "__main__":
    unittest.main()
