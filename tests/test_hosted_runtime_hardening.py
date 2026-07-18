import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
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
        self.addCleanup(lambda: sys.modules.pop("hosted_worker_hardening_test", None))

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
