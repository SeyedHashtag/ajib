import json
import importlib
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core/scripts/telegrambot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


def _load_database_module():
    expected = (BOT_DIR / "utils").resolve()
    package = sys.modules.get("utils")
    package_paths = {
        Path(item).resolve()
        for item in (getattr(package, "__path__", None) or ())
    }
    if expected not in package_paths:
        for name in list(sys.modules):
            if name == "utils" or name.startswith("utils."):
                sys.modules.pop(name, None)
        # These top-level administrative modules retain references to the
        # utils package they imported, so discard them with a polluted stub.
        sys.modules.pop("migrate_state", None)
        sys.modules.pop("state_archive", None)
        importlib.invalidate_caches()
    return importlib.import_module("utils.database")


def _worker_environment(root):
    os.environ.update(
        {
            "AJIB_BOT_ROLE": "supervisor",
            "AJIB_BOT_DIR": root,
            "AJIB_DB_PATH": os.path.join(root, "ajib.db"),
            "AJIB_SQLITE_ACTIVE": "1",
            "AJIB_BACKUP_DIR": os.path.join(root, "backups"),
            "ADMIN_USER_IDS": "[]",
            "API_TOKEN": "123:testing",
        }
    )


def _claim_payment_worker(root, ready, start, results):
    _worker_environment(root)
    from utils import database
    from utils import payment_records

    database.close_connections()
    payment_records.PAYMENTS_FILE = os.path.join(root, "payments.json")
    ready.put(True)
    start.wait(10)
    results.put(payment_records.claim_payment_for_processing("pay-1"))


def _reserve_credit_worker(root, reservation_id, ready, start, results):
    _worker_environment(root)
    from utils import database, hosted_bots, reseller

    database.close_connections()
    hosted_bots.BOT_DIR = root
    hosted_bots.HOSTED_ROOT = os.path.join(root, "hosted_bots")
    reseller.RESELLERS_FILE = os.path.join(root, "resellers.json")
    ready.put(True)
    start.wait(10)
    results.put(hosted_bots.reserve_credit("7", reservation_id, 6, 10))


def _referral_code_worker(root, ready, start, results):
    _worker_environment(root)
    from utils import database, referral

    database.close_connections()
    referral.REFERRALS_FILE = os.path.join(root, "referrals.json")
    ready.put(True)
    start.wait(10)
    results.put(referral.get_or_create_referral_code("42"))


def _referral_reward_worker(root, ready, start, results):
    _worker_environment(root)
    from utils import database, referral

    database.close_connections()
    referral.REFERRALS_FILE = os.path.join(root, "referrals.json")
    ready.put(True)
    start.wait(10)
    results.put(referral.add_referral_reward("99", 10, "payment-1"))


def _test_claim_worker(root, ready, start, results):
    _worker_environment(root)
    from utils import database, test_config

    database.close_connections()
    test_config.TEST_CONFIGS_FILE = os.path.join(root, "test_configs.json")
    ready.put(True)
    start.wait(10)
    results.put(test_config._claim_test_config_creation(42))


class SQLiteStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "AJIB_BOT_ROLE",
                "AJIB_BOT_DIR",
                "AJIB_DB_PATH",
                "AJIB_SQLITE_ACTIVE",
                "AJIB_BACKUP_DIR",
            )
        }
        os.environ.update(
            {
                "AJIB_BOT_ROLE": "supervisor",
                "AJIB_BOT_DIR": str(self.root),
                "AJIB_DB_PATH": str(self.root / "ajib.db"),
                "AJIB_BACKUP_DIR": str(self.backups),
            }
        )
        os.environ.pop("AJIB_SQLITE_ACTIVE", None)
        self.database = _load_database_module()
        self.database.close_connections()

    def tearDown(self):
        self.database.close_connections()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def migrate(self, remove=True):
        from migrate_state import migrate_legacy_state

        result = migrate_legacy_state(
            self.root,
            self.root / "ajib.db",
            archive_dir=self.backups,
            remove_legacy=remove,
        )
        os.environ["AJIB_SQLITE_ACTIVE"] = "1"
        return result

    def test_managed_dictionary_patch_updates_only_selected_keys(self):
        self.write_json(
            "traffic_alerts.json",
            {
                "unchanged": {"notified": [80]},
                "changed": {"notified": [80]},
            },
        )
        self.migrate()
        from utils.atomic_store import patch_json_dict, read_json

        path = self.root / "traffic_alerts.json"
        patch_json_dict(
            path,
            {"changed": {"notified": [80, 90]}, "added": {"notified": [80]}},
        )

        self.assertEqual(
            read_json(path, {}),
            {
                "unchanged": {"notified": [80]},
                "changed": {"notified": [80, 90]},
                "added": {"notified": [80]},
            },
        )
        connection = sqlite3.connect(self.root / "ajib.db")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM kv_state WHERE namespace='traffic_alerts'"
                ).fetchone()[0],
                3,
            )
        finally:
            connection.close()

    def test_slow_outer_write_transaction_logs_operation_pid_and_role(self):
        path = self.root / "slow-write.db"
        with (
            mock.patch.object(self.database.time, "monotonic", side_effect=(0.0, 0.1, 0.5)),
            self.assertLogs("ajib.database", level="WARNING") as captured,
        ):
            with self.database.write_transaction(path, operation="slow_test"):
                pass

        output = "\n".join(captured.output)
        self.assertIn("operation=slow_test", output)
        self.assertIn("wait_ms=100", output)
        self.assertIn("hold_ms=400", output)
        self.assertIn("elapsed_ms=500", output)
        self.assertIn(f"pid={os.getpid()}", output)
        self.assertIn("role=supervisor", output)

    def test_locked_json_delta_updates_only_changed_kv_rows(self):
        self.write_json(
            "test_configs.json",
            {
                "a": {"value": 1, "unknown": {"kept": True}},
                "b": {"value": 2},
            },
        )
        self.migrate()
        from utils.atomic_store import locked_json, read_json

        path = self.root / "test_configs.json"
        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            before_b = connection.execute(
                """SELECT updated_at FROM kv_state
                   WHERE namespace='test_configs' AND state_key='b'"""
            ).fetchone()[0]
            connection.executescript(
                """
                CREATE TABLE delta_audit(state_key TEXT NOT NULL);
                CREATE TRIGGER audit_test_config_update
                AFTER UPDATE ON kv_state
                WHEN NEW.namespace='test_configs'
                BEGIN
                    INSERT INTO delta_audit(state_key) VALUES (NEW.state_key);
                END;
                """
            )

        with locked_json(path, {}) as configs:
            configs["a"]["value"] = 3

        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            self.assertEqual(
                connection.execute("SELECT state_key FROM delta_audit").fetchall(),
                [("a",)],
            )
            after_b = connection.execute(
                """SELECT updated_at FROM kv_state
                   WHERE namespace='test_configs' AND state_key='b'"""
            ).fetchone()[0]
        self.assertEqual(before_b, after_b)
        self.assertEqual(read_json(path, {})["a"]["unknown"], {"kept": True})

        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            connection.execute("DELETE FROM delta_audit")
            connection.commit()
        sys.modules.pop("utils.test_config_store", None)
        test_config_store = importlib.import_module("utils.test_config_store")

        test_config_store.update_test_configs(
            path,
            lambda configs: configs["b"].update({"value": 4}),
        )
        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            self.assertEqual(
                connection.execute("SELECT state_key FROM delta_audit").fetchall(),
                [("b",)],
            )

    def test_descriptor_operation_labels_kv_namespace_and_scope(self):
        from utils.state_store import StateDescriptor, descriptor_operation

        self.assertEqual(
            descriptor_operation(StateDescriptor("kv_dict", "main", "expired_cleanup")),
            "kv:expired_cleanup:main",
        )
        self.assertEqual(
            descriptor_operation(StateDescriptor("payments", "main")),
            "payments:main",
        )

    def test_payment_and_reseller_deltas_maintain_child_rows(self):
        self.write_json(
            "payments.json",
            {
                "p1": {
                    "status": "pending",
                    "unknown": "kept",
                    "updates": [{"status": "pending", "timestamp": "one"}],
                },
                "p2": {
                    "status": "paid",
                    "updates": [{"status": "paid", "timestamp": "two"}],
                },
            },
        )
        self.write_json(
            "resellers.json",
            {
                "7": {
                    "debt": 1,
                    "configs": [{"username": "r7", "retail_order_id": "o7"}],
                },
                "8": {
                    "debt": 2,
                    "configs": [{"username": "r8", "retail_order_id": "o8"}],
                },
            },
        )
        self.migrate()
        from utils.atomic_store import locked_json, read_json

        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            connection.executescript(
                """
                CREATE TABLE delta_audit(table_name TEXT, record_id TEXT);
                CREATE TRIGGER audit_payment_update AFTER UPDATE ON payments BEGIN
                    INSERT INTO delta_audit VALUES ('payments', NEW.payment_id);
                END;
                CREATE TRIGGER audit_payment_event_delete AFTER DELETE ON payment_events BEGIN
                    INSERT INTO delta_audit VALUES ('payment_events', OLD.payment_id);
                END;
                CREATE TRIGGER audit_reseller_update AFTER UPDATE ON resellers BEGIN
                    INSERT INTO delta_audit VALUES ('resellers', NEW.reseller_id);
                END;
                CREATE TRIGGER audit_reseller_config_delete AFTER DELETE ON reseller_configs BEGIN
                    INSERT INTO delta_audit VALUES ('reseller_configs', OLD.reseller_id);
                END;
                """
            )

        payments_path = self.root / "payments.json"
        with locked_json(payments_path, {}) as payments:
            payments["p1"]["status"] = "paid"
            payments["p1"]["updates"].append({"status": "paid", "timestamp": "three"})

        resellers_path = self.root / "resellers.json"
        with locked_json(resellers_path, {}) as resellers:
            resellers["7"]["debt"] = 3

        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            audit = connection.execute(
                "SELECT table_name, record_id FROM delta_audit ORDER BY rowid"
            ).fetchall()
            self.assertEqual(
                audit,
                [
                    ("payments", "p1"),
                    ("payment_events", "p1"),
                    ("resellers", "7"),
                    ("reseller_configs", "7"),
                ],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_events WHERE payment_id='p2'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM reseller_configs WHERE reseller_id='8'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(read_json(payments_path, {})["p1"]["unknown"], "kept")

        with locked_json(payments_path, {}) as payments:
            payments.pop("p1")
        with locked_json(resellers_path, {}) as resellers:
            resellers.pop("7")
        with closing(sqlite3.connect(self.root / "ajib.db")) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM payment_events WHERE payment_id='p1'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM reseller_configs WHERE reseller_id='7'"
                ).fetchone()[0],
                0,
            )

    def test_invalid_reseller_delta_rolls_back_prior_deletion(self):
        self.write_json(
            "resellers.json",
            {
                "7": {"debt": 1, "configs": []},
                "8": {"debt": 2, "configs": []},
            },
        )
        self.migrate()
        from utils.atomic_store import locked_json, read_json

        path = self.root / "resellers.json"
        with self.assertRaises(ValueError):
            with locked_json(path, {}) as resellers:
                resellers.pop("8")
                resellers["7"]["configs"] = [
                    {"username": "one", "retail_order_id": "duplicate"},
                    {"username": "two", "retail_order_id": "duplicate"},
                ]

        self.assertEqual(set(read_json(path, {})), {"7", "8"})

    def test_migration_imports_top_level_and_hosted_state_transactionally(self):
        payment = self.write_json(
            "payments.json",
            {
                "pay-1": {
                    "user_id": "legacy-user",
                    "status": "completed",
                    "price": 12.345,
                    "unknown_gateway_field": {"kept": True},
                    "updates": [
                        {
                            "status": "completed",
                            "previous_status": "processing",
                            "timestamp": "2026-01-01 00:00:00",
                        }
                    ],
                }
            },
        )
        reseller = self.write_json(
            "resellers.json",
            {
                "7": {
                    "status": "approved",
                    "debt": 4.56,
                    "total_paid": 10.01,
                    "configs": [
                        {
                            "username": "r7",
                            "server_id": "s1",
                            "retail_order_id": "order-1",
                            "price": 4.56,
                            "renewals": [
                                {
                                    "retail_order_id": "renewal-1",
                                    "price": 1.255,
                                }
                            ],
                        }
                    ],
                }
            },
        )
        self.write_json(
            "referrals.json",
            {
                "referrals": {"11": "10"},
                "stats": {
                    "10": {
                        "count": 1,
                        "total_earnings": 2.25,
                        "available_balance": 2.25,
                    }
                },
                "codes": {"code": "10"},
                "user_codes": {"10": "code"},
                "wallets": {"10": "wallet"},
            },
        )
        self.write_json(
            "checker_settlements.json",
            [{"id": "settlement", "amount_toman": 80000, "admin_user_id": 1}],
        )
        self.write_json("user_languages.json", {"11": "fa"})
        self.write_json("test_configs.json", {"11": {"username": "t11"}})
        self.write_json("waiting_test_users.json", {"12": {"telegram_id": 12}})
        self.write_json("expired_user_cleanup.json", {"s1:u": {"username": "u"}})
        self.write_json("expired_cleanup_schedule.json", {"last_started_at": "now"})
        self.write_json("traffic_alerts.json", {"u": {"notified": [80]}})
        self.write_json("broadcast_failed_users.json", ["11"])
        self.write_json(
            "hosted_bots.json",
            {
                "7": {
                    "reseller_id": "7",
                    "bot_id": "700",
                    "username": "shop",
                    "token_fingerprint": "fingerprint",
                    "status": "active",
                    "enabled": True,
                }
            },
        )
        self.write_json("hosted_bot_tokens.json", {"7": "700:secret"})
        self.write_json("hosted_bots/7/settings.json", {"markup_percent": 20})
        self.write_json(
            "hosted_bots/7/ledger.json",
            {
                "earnings_available": 9.99,
                "earnings_reserved": 1,
                "referral_liability": 2,
                "credit_reservations": {
                    "reserve": {"amount": 3, "created_at": "2026-01-01 00:00:00"}
                },
                "withdrawals": [],
                "transactions": [
                    {
                        "id": "sale:1",
                        "type": "crypto_sale",
                        "amount": 9.99,
                        "metadata": {"order": "1"},
                        "snapshot_version": 3,
                    }
                ],
            },
        )
        self.write_json(
            "hosted_bots/7/payments.json",
            {
                "hosted-pay": {
                    "user_id": 22,
                    "status": "pending",
                    "price": 5,
                    "receipt_path": str(
                        self.root / "hosted_bots/7/receipts/hosted-pay.jpg"
                    ),
                }
            },
        )
        self.write_json("hosted_bots/7/languages.json", {"22": "ru"})
        self.write_json("hosted_bots/7/renewal_tokens.json", {"token": {"user_id": 22}})
        self.write_json("hosted_bots/7/notifications.json", {"traffic:u": 80})
        (self.root / "plans.json").write_text('{"1": {"price": 5}}', encoding="utf-8")
        (self.root / "support_info.json").write_text('{"text": "help"}', encoding="utf-8")

        result = self.migrate(remove=True)

        self.assertEqual(result["status"], "migrated")
        self.assertFalse(payment.exists())
        self.assertFalse(reseller.exists())
        self.assertTrue((self.root / "plans.json").exists())
        self.assertTrue((self.root / "support_info.json").exists())
        self.assertTrue(Path(result["archive"]).is_file())
        with sqlite3.connect(self.root / "ajib.db") as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute(
                    "SELECT amount_cents FROM payments WHERE scope='main' AND payment_id='pay-1'"
                ).fetchone()[0],
                1235,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT debt_cents, total_paid_cents FROM resellers WHERE reseller_id='7'"
                ).fetchone(),
                (456, 1001),
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reseller_configs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reseller_renewals").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT token FROM hosted_bots").fetchone()[0], "700:secret")
            self.assertEqual(connection.execute("SELECT earnings_available_cents FROM ledger_accounts").fetchone()[0], 999)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM referral_links").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT amount_toman FROM checker_settlements").fetchone()[0], 80000)
            payload = json.loads(
                connection.execute(
                    "SELECT payload_json FROM payments WHERE payment_id='pay-1'"
                ).fetchone()[0]
            )
            self.assertEqual(payload["unknown_gateway_field"], {"kept": True})
            hosted_payload = json.loads(
                connection.execute(
                    """
                    SELECT payload_json FROM payments
                    WHERE scope='hosted:7' AND payment_id='hosted-pay'
                    """
                ).fetchone()[0]
            )
            self.assertEqual(
                hosted_payload["receipt_path"],
                "hosted_bots/7/receipts/hosted-pay.jpg",
            )
        os.environ["AJIB_SQLITE_ACTIVE"] = "1"
        from utils.atomic_store import read_json

        loaded_payment = read_json(self.root / "payments.json", {})["pay-1"]
        loaded_reseller = read_json(self.root / "resellers.json", {})["7"]
        loaded_ledger = read_json(
            self.root / "hosted_bots/7/ledger.json",
            {},
        )
        self.assertEqual(loaded_payment["price"], 12.35)
        self.assertEqual(loaded_payment["user_id"], "legacy-user")
        self.assertEqual(
            loaded_reseller["configs"][0]["renewals"][0]["price"],
            1.26,
        )
        self.assertEqual(
            loaded_ledger["transactions"][0]["snapshot_version"],
            3,
        )
        loaded_hosted_payment = read_json(
            self.root / "hosted_bots/7/payments.json",
            {},
        )["hosted-pay"]
        self.assertEqual(
            loaded_hosted_payment["receipt_path"],
            str(self.root / "hosted_bots/7/receipts/hosted-pay.jpg"),
        )

    def test_malformed_json_aborts_without_application_rows_or_marker(self):
        (self.root / "payments.json").write_text('{"pay":', encoding="utf-8")

        with self.assertRaisesRegex(Exception, "Unable to read legacy state"):
            self.migrate()

        with sqlite3.connect(self.root / "ajib.db") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM state_metadata WHERE key='legacy_import_v1'"
                ).fetchone()[0],
                0,
            )
        self.assertTrue((self.root / "payments.json").exists())
        self.assertEqual(list(self.backups.glob("*.zip")), [])

    def test_duplicate_order_ids_roll_back_the_entire_import(self):
        self.write_json(
            "resellers.json",
            {
                "7": {
                    "status": "approved",
                    "debt": 0,
                    "configs": [
                        {"username": "a", "retail_order_id": "same"},
                        {"username": "b", "retail_order_id": "same"},
                    ],
                }
            },
        )
        self.write_json("payments.json", {"pay": {"status": "pending", "price": 1}})

        with self.assertRaisesRegex(Exception, "Duplicate reseller order ID"):
            self.migrate()

        with sqlite3.connect(self.root / "ajib.db") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM resellers").fetchone()[0], 0)
        self.assertTrue((self.root / "payments.json").exists())
        self.assertTrue((self.root / "resellers.json").exists())

    def test_non_finite_financial_string_is_rejected(self):
        self.write_json("payments.json", {"pay": {"status": "pending", "price": "NaN"}})

        with self.assertRaisesRegex(Exception, "non-finite"):
            self.migrate()

    def test_escaping_receipt_path_and_unmarked_database_data_are_rejected(self):
        self.write_json(
            "hosted_bots/7/payments.json",
            {
                "pay": {
                    "status": "pending_approval",
                    "price": 1,
                    "receipt_path": "../../outside.jpg",
                }
            },
        )
        with self.assertRaisesRegex(Exception, "escapes the bot state directory"):
            self.migrate()

        (self.root / "hosted_bots/7/payments.json").unlink()
        connection = self.database.get_connection(self.root / "ajib.db")
        connection.execute(
            "INSERT INTO hosted_settings(reseller_id, payload_json) VALUES ('7', '{}')"
        )
        with self.assertRaisesRegex(Exception, "without a legacy import marker"):
            self.migrate()

    def test_second_migration_is_idempotent(self):
        source = self.write_json(
            "payments.json",
            {"pay": {"status": "pending", "price": 1}},
        )
        first = self.migrate(remove=False)
        second = self.migrate(remove=False)

        self.assertEqual(first["status"], "migrated")
        self.assertEqual(second["status"], "already_migrated")
        self.assertTrue(source.exists())
        with sqlite3.connect(self.root / "ajib.db") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 1)

    def _configure_runtime_modules(self):
        os.environ["AJIB_SQLITE_ACTIVE"] = "1"
        from utils import hosted_bots, payment_records, reseller, state_store

        self.database.close_connections()
        payment_records.PAYMENTS_FILE = str(self.root / "payments.json")
        reseller.RESELLERS_FILE = str(self.root / "resellers.json")
        hosted_bots.BOT_DIR = str(self.root)
        hosted_bots.HOSTED_ROOT = str(self.root / "hosted_bots")
        hosted_bots.REGISTRY_FILE = str(self.root / "hosted_bots.json")
        hosted_bots.SECRETS_FILE = str(self.root / "hosted_bot_tokens.json")
        return hosted_bots, payment_records, reseller, state_store

    def test_compound_credit_failure_rolls_back_reseller_and_ledger(self):
        self.migrate()
        hosted_bots, _payments, reseller, state_store = self._configure_runtime_modules()
        reseller.save_resellers(
            {"7": {"status": "approved", "debt": 0, "total_paid": 0, "configs": []}}
        )
        self.assertTrue(hosted_bots.reserve_credit("7", "order", 5, 10))
        original_save = state_store.save_descriptor

        def fail_ledger(connection, descriptor, data):
            if descriptor.kind == "ledger" and not data.get("credit_reservations"):
                raise RuntimeError("injected ledger write failure")
            return original_save(connection, descriptor, data)

        with mock.patch.object(state_store, "save_descriptor", side_effect=fail_ledger):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                hosted_bots.consume_credit(
                    "7",
                    "order",
                    {"username": "customer", "retail_order_id": "order", "price": 5},
                )

        self.assertEqual(reseller.get_reseller_data("7")["debt"], 0)
        self.assertIn("order", hosted_bots.get_ledger("7")["credit_reservations"])

    def test_account_credit_debt_settlement_rolls_back_as_one_transaction(self):
        self.migrate()
        _hosted, payment_records, reseller, _state_store = self._configure_runtime_modules()
        from utils import account_credit

        reseller.save_resellers({
            "7": {
                "status": "approved",
                "debt": 5,
                "total_paid": 0,
                "configs": [],
            }
        })
        account_credit.credit_account("7", 5, "seed")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            with self.database.write_transaction(operation="test_credit_settlement"):
                self.assertEqual(
                    account_credit.reserve_account_credit("7", "settlement", 5),
                    5,
                )
                payment_records.add_payment_record(
                    "settlement",
                    {"user_id": "7", "status": "processing"},
                )
                self.assertTrue(
                    reseller.apply_reseller_payment(
                        "7", 5, payment_id="settlement"
                    )[0]
                )
                raise RuntimeError("injected failure before credit consumption")

        self.assertEqual(reseller.get_reseller_data("7")["debt"], 5)
        self.assertIsNone(payment_records.get_payment_record("settlement"))
        balance = account_credit.get_account_credit("7")
        self.assertEqual(balance["available"], 5)
        self.assertEqual(balance["reserved"], 0)

    def test_multiprocess_claims_and_reservations_are_serialized(self):
        self.migrate()
        hosted_bots, payment_records, reseller, _state_store = self._configure_runtime_modules()
        payment_records.add_payment_record("pay-1", {"user_id": 1, "status": "pending"})
        reseller.save_resellers(
            {"7": {"status": "approved", "debt": 0, "total_paid": 0, "configs": []}}
        )

        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_claim_payment_worker,
                args=(str(self.root), ready, start, results),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for _ in workers:
            self.assertTrue(ready.get(timeout=15))
        start.set()
        claims = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(claims), [False, True])

        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_reserve_credit_worker,
                args=(str(self.root), reservation_id, ready, start, results),
            )
            for reservation_id in ("a", "b")
        ]
        for worker in workers:
            worker.start()
        for _ in workers:
            self.assertTrue(ready.get(timeout=15))
        start.set()
        reservations = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(reservations), [False, True])
        self.assertEqual(
            len(hosted_bots.get_ledger("7")["credit_reservations"]),
            1,
        )

    def test_multiprocess_referral_and_test_claims_are_idempotent(self):
        self.migrate()
        self._configure_runtime_modules()
        from utils import referral

        context = multiprocessing.get_context("spawn")

        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_referral_code_worker,
                args=(str(self.root), ready, start, results),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for _ in workers:
            self.assertTrue(ready.get(timeout=15))
        start.set()
        codes = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(codes[0], codes[1])

        referral.save_referrals(
            {
                **referral._default_referrals_data(),
                "referrals": {"99": "42"},
                "stats": {
                    "42": {
                        "count": 1,
                        "total_earnings": 0,
                        "available_balance": 0,
                    }
                },
            }
        )
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_referral_reward_worker,
                args=(str(self.root), ready, start, results),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for _ in workers:
            self.assertTrue(ready.get(timeout=15))
        start.set()
        rewards = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum(bool(result) for result in rewards), 1)
        self.assertEqual(referral.get_referral_stats("42")["available_balance"], 2)

        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_test_claim_worker,
                args=(str(self.root), ready, start, results),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for _ in workers:
            self.assertTrue(ready.get(timeout=20))
        start.set()
        claims = [results.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(20)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(claims), [False, True])

    def test_database_permissions_and_hosted_tokens_are_private(self):
        self.migrate()
        hosted_bots, _payments, reseller, _state_store = self._configure_runtime_modules()
        reseller.save_resellers({"7": {"status": "approved", "debt": 0, "configs": []}})
        success, public_record = hosted_bots.register_bot(
            "7",
            "700:secret",
            {"id": 700, "username": "shop"},
        )

        self.assertTrue(success)
        self.assertNotIn("secret", json.dumps(public_record))
        self.assertNotIn("token", public_record)
        self.assertEqual(hosted_bots.get_token("7"), "700:secret")
        mode = (self.root / "ajib.db").stat().st_mode & 0o777
        if mode != 0o777:  # Windows-mounted CI filesystems do not preserve chmod.
            self.assertEqual(mode, 0o600)
        directory_mode = self.root.stat().st_mode & 0o777
        if directory_mode != 0o777:
            self.assertEqual(directory_mode, 0o700)

    def test_active_runtime_writes_only_database_for_mutable_state(self):
        self.migrate()
        self._configure_runtime_modules()
        from utils.atomic_store import read_json, write_json

        mutable = self.root / "payments.json"
        static = self.root / "plans.json"
        write_json(
            mutable,
            {"pay": {"user_id": "non-numeric-id", "status": "pending", "price": 1}},
        )
        write_json(static, {"1": {"price": 2}})

        self.assertFalse(mutable.exists())
        self.assertEqual(read_json(mutable, {})["pay"]["user_id"], "non-numeric-id")
        self.assertTrue(static.is_file())
        self.assertEqual(json.loads(static.read_text()), {"1": {"price": 2}})
        connection = self.database.get_connection()
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_supervisor_discovers_multiple_hosted_workers_on_one_database(self):
        self.migrate()
        hosted_bots, _payments, reseller, _state_store = self._configure_runtime_modules()
        reseller.save_resellers(
            {
                "7": {"status": "approved", "debt": 0, "configs": []},
                "8": {"status": "approved", "debt": 0, "configs": []},
            }
        )
        self.assertTrue(
            hosted_bots.register_bot(
                "7", "700:secret", {"id": 700, "username": "shop7"}
            )[0]
        )
        self.assertTrue(
            hosted_bots.register_bot(
                "8", "800:secret", {"id": 800, "username": "shop8"}
            )[0]
        )
        sys.modules.pop("supervisor", None)
        supervisor = importlib.import_module("supervisor")

        eligible = supervisor._eligible_hosted_bots()
        self.assertEqual([item[0] for item in eligible], ["7", "8"])
        workers = [
            supervisor._hosted_worker(reseller_id, record)
            for reseller_id, record in eligible
        ]
        self.assertEqual(
            {worker.env["AJIB_DB_PATH"] for worker in workers},
            {str(self.root / "ajib.db")},
        )


if __name__ == "__main__":
    unittest.main()
