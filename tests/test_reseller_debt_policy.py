import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESELLER_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "reseller.py"
TRANSLATIONS_PATH = ROOT / "core" / "scripts" / "telegrambot" / "utils" / "translations.py"
if str(RESELLER_PATH.parent) not in sys.path:
    sys.path.insert(0, str(RESELLER_PATH.parent))


def load_reseller_module():
    spec = importlib.util.spec_from_file_location("reseller_policy_under_test", RESELLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResellerDebtPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.resellers_file = Path(self.tmpdir.name) / "resellers.json"
        self.reseller = load_reseller_module()
        self.reseller.RESELLERS_FILE = str(self.resellers_file)

    def write_resellers(self, data):
        self.resellers_file.write_text(json.dumps(data), encoding="utf-8")

    def read_resellers(self):
        return json.loads(self.resellers_file.read_text(encoding="utf-8"))

    def hours_ago(self, hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    def test_external_bulk_username_parser_is_exact_and_case_insensitive(self):
        self.assertEqual(
            self.reseller.parse_external_bulk_reseller_username("r7784615720c184"),
            ("7784615720", "184"),
        )
        self.assertEqual(
            self.reseller.parse_external_bulk_reseller_username("R7784615720C001"),
            ("7784615720", "001"),
        )
        for invalid in ("r7784615720c", "r7784615720x184", "r0c184", "7784615720c184"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    self.reseller.parse_external_bulk_reseller_username(invalid)
                )

    def test_external_bulk_adoption_is_atomic_idempotent_and_financially_neutral(self):
        self.write_resellers({
            "7784615720": {
                "status": "approved",
                "debt": 4.0,
                "total_paid": 20.0,
                "configs": [],
            },
            "999": {
                "status": "approved",
                "configs": [
                    {"username": "r7784615720c2", "server_id": "s1", "price": 7.0},
                ],
            },
        })
        candidates = [
            {
                "username": "r7784615720c184",
                "server_id": "s1",
                "user_data": {
                    "username": "r7784615720c184",
                    "server_id": "s1",
                    "max_download_bytes": 100 * 1024 ** 3,
                    "configured_duration_days": 60,
                    "expiration_days": 12,
                    "account_creation_date": "2026-08-01 12:00:00",
                    "note": "",
                },
            },
            {
                "username": "r7784615720c184",
                "server_id": "s2",
                "user_data": {
                    "max_download_bytes": 50 * 1024 ** 3,
                    "expiration_days": 30,
                },
            },
            {"username": "r7784615720c2", "server_id": "s1", "user_data": {}},
            {"username": "r111c1", "server_id": "s1", "user_data": {}},
        ]

        first = self.reseller.adopt_external_bulk_reseller_configs("7784615720", candidates)
        second = self.reseller.adopt_external_bulk_reseller_configs("7784615720", candidates)
        saved = self.read_resellers()["7784615720"]

        self.assertEqual(first["added"], 2)
        self.assertEqual(first["conflicts"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(saved["configs"]), 2)
        imported = saved["configs"][0]
        self.assertEqual(imported["username"], "r7784615720c184")
        self.assertEqual(imported["server_id"], "s1")
        self.assertEqual(imported["bulk_sequence"], "184")
        self.assertEqual(imported["provisioning_source"], "external_bulk")
        self.assertTrue(imported["financially_excluded"])
        self.assertEqual(imported["gb"], 100)
        self.assertEqual(imported["days"], 60)
        self.assertEqual(imported["timestamp"], "2026-08-01 12:00:00")
        self.assertNotIn("customer_name", imported)
        self.assertNotIn("price", imported)
        self.assertNotIn("note", imported)
        missing_metadata = saved["configs"][1]
        self.assertNotIn("timestamp", missing_metadata)
        self.assertIn("discovered_at", missing_metadata)
        self.assertEqual(saved["debt"], 4.0)
        self.assertEqual(saved["total_paid"], 20.0)
        self.assertEqual(self.reseller.get_reseller_config_value(imported), 0.0)
        self.assertFalse(self.reseller.is_reseller_sales_config(imported))

        imported_with_renewal = {
            **imported,
            "price": 99,
            "renewals": [{"price": 3.5}],
        }
        self.assertEqual(
            self.reseller.get_reseller_config_value(imported_with_renewal),
            3.5,
        )

    def test_external_bulk_imports_remain_normal_cleanup_candidates(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0,
                "configs": [],
            },
        })
        self.reseller.adopt_external_bulk_reseller_configs("1988", [{
            "username": "r1988c7",
            "server_id": "primary",
            "user_data": {"expiration_days": 30, "max_download_bytes": 1024 ** 3},
        }])
        saved = self.read_resellers()["1988"]

        candidates = self.reseller.get_banned_reseller_cleanup_candidates(saved)

        self.assertEqual([item["username"] for item in candidates], ["r1988c7"])
        self.assertEqual(candidates[0]["price"], 0.0)

    def test_trust_limit_tiers_cap_at_thirty(self):
        cases = [
            (0.0, 5.0),
            (9.99, 5.0),
            (10.0, 10.0),
            (20.0, 15.0),
            (30.0, 20.0),
            (40.0, 25.0),
            (50.0, 30.0),
            (200.0, 30.0),
        ]

        for total_paid, expected_limit in cases:
            with self.subTest(total_paid=total_paid):
                self.assertEqual(self.reseller.get_reseller_trust_limit(total_paid), expected_limit)

    def test_debt_lifecycle_and_wallet_messages_exist_in_every_language(self):
        spec = importlib.util.spec_from_file_location("debt_translations_under_test", TRANSLATIONS_PATH)
        translations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(translations)
        required = {
            "reseller_debt_opened",
            "reseller_debt_deletion_warning",
            "reseller_debt_services_held",
            "reseller_debt_services_removed",
            "reseller_debt_recovered",
            "reseller_wholesale_balance_screen",
            "admin_reseller_debt_services_held",
            "admin_reseller_debt_services_removed",
            "admin_reseller_debt_recovered",
            "admin_reseller_debt_action_retry",
            "admin_reseller_credit_outcomes_line",
        }

        for language in ("en", "ru", "fa", "tk"):
            with self.subTest(language=language):
                self.assertTrue(required.issubset(translations.MESSAGE_TRANSLATIONS[language]))

    def test_reseller_levels_discount_and_progress_share_trust_thresholds(self):
        cases = [
            (0.0, 1, 20, 5.0, 10.0),
            (9.99, 1, 20, 5.0, 0.01),
            (10.0, 2, 21, 10.0, 10.0),
            (20.0, 3, 22, 15.0, 10.0),
            (30.0, 4, 23, 20.0, 10.0),
            (40.0, 5, 24, 25.0, 10.0),
            (50.0, 6, 25, 30.0, 0.0),
            (200.0, 6, 25, 30.0, 0.0),
        ]

        for total_paid, level, discount, trust_limit, amount_to_next in cases:
            with self.subTest(total_paid=total_paid):
                summary = self.reseller.get_reseller_level_summary(
                    {"total_paid": total_paid}
                )
                self.assertEqual(summary["level"], level)
                self.assertEqual(summary["discount_percent"], discount)
                self.assertEqual(summary["trust_limit"], trust_limit)
                self.assertAlmostEqual(summary["amount_to_next"], amount_to_next)

        for invalid_total in (-10, float("nan"), float("inf"), "invalid"):
            with self.subTest(invalid_total=invalid_total):
                summary = self.reseller.get_reseller_level_summary({
                    "total_paid": invalid_total,
                })
                self.assertEqual(summary["level"], 1)
                self.assertEqual(summary["discount_percent"], 20)

    def test_reseller_wholesale_prices_use_level_discount_and_half_up_cents(self):
        self.assertEqual(
            self.reseller.calculate_reseller_wholesale_price(
                1.25625,
                {"total_paid": 0},
            ),
            1.01,
        )
        self.assertEqual(
            self.reseller.calculate_reseller_wholesale_price(
                100,
                {"total_paid": 50},
            ),
            75.0,
        )
        with self.assertRaises(ValueError):
            self.reseller.calculate_reseller_wholesale_price(
                -1,
                {"total_paid": 0},
            )

    def test_level_presentation_claim_is_atomic_releasable_and_completable(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0,
                "total_paid": 20,
                "configs": [],
            }
        })

        first = self.reseller.claim_reseller_level_presentation("1988")
        duplicate = self.reseller.claim_reseller_level_presentation("1988")

        self.assertEqual(first["kind"], "introduction")
        self.assertEqual(first["summary"]["level"], 3)
        self.assertIsNone(duplicate)

        self.assertTrue(
            self.reseller.release_reseller_level_presentation(
                "1988",
                first["id"],
            )
        )
        retry = self.reseller.claim_reseller_level_presentation("1988")
        self.assertNotEqual(retry["id"], first["id"])
        self.assertTrue(
            self.reseller.complete_reseller_level_presentation(
                "1988",
                retry["id"],
            )
        )
        self.assertIsNone(
            self.reseller.claim_reseller_level_presentation("1988")
        )

        saved = self.read_resellers()["1988"]
        saved["total_paid"] = 50
        self.write_resellers({"1988": saved})
        level_up = self.reseller.claim_reseller_level_presentation("1988")
        self.assertEqual(level_up["kind"], "level_up")
        self.assertEqual(level_up["from_level"], 3)
        self.assertEqual(level_up["summary"]["level"], 6)

    def test_missing_total_paid_is_derived_from_legacy_turnover_minus_debt(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 15.0,
                "configs": [
                    {"price": 20.0},
                    {"price": 10.0},
                ],
            }
        })

        data = self.reseller.get_reseller_data("1988")

        self.assertEqual(data["total_paid"], 15.0)
        self.assertEqual(data["trust_limit"], 10.0)

    def test_missing_total_paid_ignores_removed_cleanup_history(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 5.0,
                "configs": [
                    {"price": 20.0},
                    {
                        "price": 10.0,
                        "removed_from_vpn": True,
                        "removal_reason": "banned_reseller_cleanup",
                    },
                ],
            }
        })

        data = self.reseller.get_reseller_data("1988")

        self.assertEqual(data["total_paid"], 15.0)

    def test_missing_total_paid_includes_renewal_history_in_legacy_turnover(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 4.0,
                "configs": [
                    {
                        "username": "customer1",
                        "price": 10.0,
                        "renewals": [{"price": 5.0}],
                    },
                ],
            }
        })

        data = self.reseller.get_reseller_data("1988")

        self.assertEqual(self.reseller.get_reseller_config_value(data["configs"][0]), 15.0)
        self.assertEqual(data["total_paid"], 11.0)
        self.assertEqual(data["trust_limit"], 10.0)

    def test_add_reseller_renewal_debt_appends_history_without_duplicating_config(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0.0,
                "configs": [
                    {
                        "username": "customer1",
                        "server_id": "s1",
                        "price": 10.0,
                    },
                ],
            }
        })

        success = self.reseller.add_reseller_renewal_debt(
            "1988",
            "customer1",
            4.0,
            {
                "gb": "5",
                "days": 30,
                "before_state": {"status": "expired"},
                "after_state": {"status": "active"},
            },
            server_id="s1",
        )
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(saved["debt"], 4.0)
        self.assertEqual(len(saved["configs"]), 1)
        self.assertEqual(len(saved["configs"][0]["renewals"]), 1)
        self.assertEqual(saved["configs"][0]["renewals"][0]["price"], 4.0)
        self.assertEqual(saved["configs"][0]["cleanup_status"], "renewed")
        self.assertEqual(saved["configs"][0]["cleanup_last_state"], {"status": "active"})

    def test_reseller_config_is_recorded_matches_username_and_server(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 1.0,
                "configs": [
                    {"username": "r1988", "server_id": "s1", "price": 1.0},
                ],
            }
        })

        self.assertTrue(self.reseller.reseller_config_is_recorded("1988", "r1988", "s1"))
        self.assertFalse(self.reseller.reseller_config_is_recorded("1988", "r1988", "s2"))
        self.assertFalse(self.reseller.reseller_config_is_recorded("1988", "missing", "s1"))

    def test_successful_payment_increments_total_paid_by_debt_credit(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 15.0,
                "total_paid": 20.0,
                "configs": [
                    {"price": 20.0},
                    {"price": 15.0},
                ],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 10.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 5.0)
        self.assertEqual(saved["total_paid"], 30.0)
        self.assertEqual(saved["trust_limit"], 20.0)
        self.assertIsNotNone(saved["last_payment_at"])

    def test_overpayment_only_increments_total_paid_by_debt_reduction(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 8.0,
                "total_paid": 0.0,
                "configs": [{"price": 8.0}],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 20.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.0)
        self.assertEqual(saved["total_paid"], 8.0)
        self.assertEqual(saved["trust_limit"], 5.0)

    def test_fifo_settlement_pays_legacy_and_older_charges_before_reserved_renewal(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 5.0,
                "total_paid": 20.0,
                "configs": [{"username": "customer1", "server_id": "s1", "price": 5.0}],
            }
        })
        self.assertTrue(self.reseller.add_reseller_debt(
            "1988", 3.0, {"username": "customer2", "server_id": "s1"}
        ))
        reserved, reservation = self.reseller.reserve_reseller_renewal(
            "1988",
            "customer1",
            4.0,
            {
                "reservation_id": "reserved-1",
                "renewal_baseline": {"status": "active"},
                "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "price": 4.0},
            },
            server_id="s1",
        )
        self.assertTrue(reserved)

        success, debt = self.reseller.apply_reseller_payment("1988", 6.0, payment_id="partial-1")
        saved = self.read_resellers()["1988"]
        legacy, config_charge, renewal_charge = saved["debt_charges"]

        self.assertTrue(success)
        self.assertEqual(debt, 6.0)
        self.assertEqual(legacy["kind"], "legacy_balance")
        self.assertEqual(legacy["outstanding_amount"], 0.0)
        self.assertEqual(config_charge["outstanding_amount"], 2.0)
        self.assertEqual(renewal_charge["outstanding_amount"], 4.0)
        self.assertFalse(
            self.reseller.is_reseller_debt_charge_paid(saved, reservation["debt_charge_id"])
        )

        self.reseller.apply_reseller_payment("1988", 2.0, payment_id="partial-2")
        saved = self.read_resellers()["1988"]
        self.assertFalse(
            self.reseller.is_reseller_debt_charge_paid(saved, reservation["debt_charge_id"])
        )
        self.reseller.apply_reseller_payment("1988", 4.0, payment_id="partial-3")
        saved = self.read_resellers()["1988"]
        self.assertTrue(
            self.reseller.is_reseller_debt_charge_paid(saved, reservation["debt_charge_id"])
        )
        self.assertEqual(saved["debt"], 0.0)

    def test_reserved_renewal_credit_check_and_duplicate_are_atomic(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 1.0,
                "total_paid": 0.0,
                "configs": [{"username": "customer1", "server_id": "s1", "price": 4.0}],
            }
        })
        record = {
            "reservation_id": "reserved-atomic",
            "renewal_plan_snapshot": {"plan_gb": "5", "days": 30, "price": 4.0},
        }

        rejected, reason = self.reseller.reserve_reseller_renewal(
            "1988", "customer1", 4.01, record, server_id="s1"
        )
        self.assertFalse(rejected)
        self.assertEqual(reason["reason"], "credit_unavailable")
        self.assertEqual(self.read_resellers()["1988"]["debt"], 1.0)

        created, reservation = self.reseller.reserve_reseller_renewal(
            "1988", "customer1", 4.0, record, server_id="s1"
        )
        duplicate, duplicate_record = self.reseller.reserve_reseller_renewal(
            "1988", "customer1", 4.0, record, server_id="s1"
        )
        saved = self.read_resellers()["1988"]
        self.assertTrue(created)
        self.assertTrue(duplicate)
        self.assertEqual(duplicate_record["reservation_id"], reservation["reservation_id"])
        self.assertEqual(saved["debt"], 5.0)
        self.assertEqual(len(saved["configs"][0]["renewals"]), 1)
        self.assertEqual(len(saved["debt_charges"]), 2)

    def test_admin_debt_adjustments_use_the_fifo_ledger(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 2.0,
                "total_paid": 0.0,
                "configs": [],
            }
        })

        self.assertTrue(self.reseller.set_reseller_debt("1988", 5.0))
        increased = self.read_resellers()["1988"]
        self.assertEqual([item["outstanding_amount"] for item in increased["debt_charges"]], [2.0, 3.0])

        self.assertTrue(self.reseller.set_reseller_debt("1988", 1.0))
        reduced = self.read_resellers()["1988"]
        self.assertEqual([item["outstanding_amount"] for item in reduced["debt_charges"]], [0.0, 1.0])

        self.assertTrue(self.reseller.clear_reseller_debt("1988"))
        cleared = self.read_resellers()["1988"]
        self.assertEqual(cleared["debt"], 0.0)
        self.assertTrue(all(item["outstanding_amount"] == 0.0 for item in cleared["debt_charges"]))

    def test_manual_payment_validation_rejects_overpayment(self):
        valid, normalized, reason = self.reseller.validate_reseller_manual_payment_amount(20.0, 8.0)

        self.assertFalse(valid)
        self.assertEqual(normalized, 20.0)
        self.assertEqual(reason, "over_debt")

    def test_manual_payment_validation_rejects_non_positive_amounts(self):
        for amount in (0.0, -1.0):
            with self.subTest(amount=amount):
                valid, normalized, reason = self.reseller.validate_reseller_manual_payment_amount(amount, 8.0)

                self.assertFalse(valid)
                self.assertEqual(normalized, amount)
                self.assertEqual(reason, "invalid")

    def test_can_reseller_add_debt_uses_current_trust_limit(self):
        reseller_data = {
            "debt": 4.0,
            "total_paid": 0.0,
            "configs": [],
        }

        can_add, trust_limit, available_credit = self.reseller.can_reseller_add_debt(reseller_data, 1.0)
        self.assertTrue(can_add)
        self.assertEqual(trust_limit, 5.0)
        self.assertEqual(available_credit, 1.0)

        can_add, trust_limit, available_credit = self.reseller.can_reseller_add_debt(reseller_data, 1.01)
        self.assertFalse(can_add)
        self.assertEqual(trust_limit, 5.0)
        self.assertEqual(available_credit, 1.0)

    def test_credit_outcome_weighting_and_three_good_recovery(self):
        base = {"debt": 0.0, "total_paid": 30.0, "configs": []}

        half_credit = self.reseller.get_reseller_credit_policy({
            **base,
            "credit_outcomes": [{"outcome": "late"}],
        })
        prepaid = self.reseller.get_reseller_credit_policy({
            **base,
            "credit_outcomes": [{"outcome": "default"}],
        })
        recovered = self.reseller.get_reseller_credit_policy({
            **base,
            "credit_outcomes": [
                {"outcome": "default"},
                {"outcome": "good"},
                {"outcome": "good"},
                {"outcome": "good"},
            ],
        })

        self.assertEqual(half_credit["base_limit"], 20.0)
        self.assertEqual(half_credit["effective_limit"], 10.0)
        self.assertEqual(half_credit["mode"], "half_credit")
        self.assertEqual(prepaid["effective_limit"], 0.0)
        self.assertEqual(prepaid["mode"], "prepaid_only")
        self.assertEqual(recovered["effective_limit"], 20.0)
        self.assertEqual(recovered["mode"], "credit")
        self.assertEqual(len(recovered["outcomes"]), 3)

    def test_old_credit_outcome_callback_stays_idempotent_after_history_rolls(self):
        self.write_resellers({
            "1988": {"status": "approved", "debt": 0.0, "configs": []}
        })
        self.assertTrue(self.reseller.record_reseller_credit_outcome(
            "1988", "default", "test", reference_id="old-default"
        ))
        for index in range(3):
            self.assertTrue(self.reseller.record_reseller_credit_outcome(
                "1988", "good", "test", reference_id=f"good-{index}"
            ))

        self.assertFalse(self.reseller.record_reseller_credit_outcome(
            "1988", "default", "test", reference_id="old-default"
        ))
        saved = self.read_resellers()["1988"]
        self.assertEqual([item["outcome"] for item in saved["credit_outcomes"]], ["good"] * 3)

    def test_notification_claim_retries_after_failure_and_stops_after_delivery(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 2.0,
                "debt_since": self.hours_ago(1),
                "configs": [],
            }
        })

        first = self.reseller.evaluate_reseller_debt_policies()
        leased = self.reseller.evaluate_reseller_debt_policies()
        self.assertEqual([event["kind"] for event in first], ["opened"])
        self.assertEqual(leased, [])

        self.assertTrue(self.reseller.complete_reseller_debt_notification(
            "1988", "opened", "user", delivered=False
        ))
        retry = self.reseller.evaluate_reseller_debt_policies()
        self.assertEqual([event["kind"] for event in retry], ["opened"])
        self.assertTrue(self.reseller.complete_reseller_debt_notification(
            "1988", "opened", "user", delivered=True
        ))
        self.assertEqual(self.reseller.evaluate_reseller_debt_policies(), [])

    def test_reminder_and_deletion_warning_deadlines(self):
        cases = (
            (1, {}, "opened"),
            (25, {}, "reminder_24h"),
            (43, {}, "deadline_final"),
            (145, {"status": "suspended", "suspended_reason": "debt", "debt_services_held_at": self.hours_ago(72)}, "deletion_warning"),
            (169, {"status": "suspended", "suspended_reason": "debt", "debt_services_held_at": self.hours_ago(96)}, "remove_due"),
        )
        for age, fields, expected in cases:
            with self.subTest(age=age):
                record = {
                    "status": "approved",
                    "debt": 3.0,
                    "debt_since": self.hours_ago(age),
                    "configs": [],
                    **fields,
                }
                self.write_resellers({"1988": record})
                events = self.reseller.evaluate_reseller_debt_policies()
                self.assertEqual([event["kind"] for event in events], [expected])

    def test_post_deletion_reminders_are_weekly_and_user_only(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 3.0,
                "debt_since": self.hours_ago(400),
                "debt_cycle_id": "existing-cycle",
                "debt_services_held_at": self.hours_ago(328),
                "debt_services_removed_at": self.hours_ago(8 * 24),
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        self.assertEqual([event["kind"] for event in events], ["post_removal_week_1"])
        self.assertTrue(events[0]["notify_user"])
        self.assertFalse(events[0]["notify_admin"])
        self.assertTrue(self.reseller.complete_reseller_debt_notification(
            "1988", "post_removal_week_1", "user", delivered=True
        ))
        self.assertEqual(self.reseller.evaluate_reseller_debt_policies(), [])

    def test_partial_payment_does_not_reset_debt_cycle(self):
        started_at = self.hours_ago(30)
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 10.0,
                "debt_since": started_at,
                "configs": [],
            }
        })

        success, remaining = self.reseller.apply_reseller_payment("1988", 3.0, "partial")
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(remaining, 7.0)
        self.assertEqual(saved["debt_since"], started_at)

    def test_new_debt_cycle_resets_only_cycle_lifecycle_markers(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0.0,
                "debt_services_held_at": self.hours_ago(20),
                "debt_services_removed_at": self.hours_ago(10),
                "credit_outcomes": [{"outcome": "default", "reference_id": "old"}],
                "configs": [{"username": "historical", "removed_from_vpn": True}],
            }
        })

        self.assertTrue(self.reseller.set_reseller_debt("1988", 2.0))
        saved = self.read_resellers()["1988"]

        self.assertIsNotNone(saved["debt_cycle_id"])
        self.assertIsNone(saved["debt_services_held_at"])
        self.assertIsNone(saved["debt_services_removed_at"])
        self.assertEqual(saved["credit_outcomes"][0]["outcome"], "default")
        self.assertTrue(saved["configs"][0]["removed_from_vpn"])

    def test_duplicate_payment_callback_does_not_double_apply_or_create_excess(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 8.0,
                "total_paid": 0.0,
                "configs": [],
            }
        })

        first = self.reseller.apply_reseller_payment("1988", 8.0, "payment-1")
        duplicate = self.reseller.apply_reseller_payment("1988", 8.0, "payment-1")
        saved = self.read_resellers()["1988"]

        self.assertEqual(first, (True, 0.0))
        self.assertEqual(duplicate, (True, 0.0))
        self.assertEqual(saved["total_paid"], 8.0)
        self.assertEqual(len(saved["processed_debt_payments"]), 1)
        self.assertEqual(saved["pending_wholesale_credits"], [])

    def test_proration_overpayment_credit_is_durable_and_retry_safe(self):
        failing = types.ModuleType("utils.reseller_wholesale_credit")
        failing.credit_wholesale_balance = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError())
        previous = sys.modules.get("utils.reseller_wholesale_credit")
        sys.modules["utils.reseller_wholesale_credit"] = failing
        self.addCleanup(
            lambda: (
                sys.modules.__setitem__("utils.reseller_wholesale_credit", previous)
                if previous is not None
                else sys.modules.pop("utils.reseller_wholesale_credit", None)
            )
        )
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 4.0,
                "configs": [],
            }
        })

        self.assertEqual(
            self.reseller.apply_reseller_payment("1988", 10.0, "async-payment"),
            (True, 0.0),
        )
        pending = self.read_resellers()["1988"]["pending_wholesale_credits"]
        self.assertEqual(pending[0]["amount"], 6.0)

        credited = []
        failing.credit_wholesale_balance = lambda reseller_id, amount, transaction_id, **kwargs: credited.append(
            (reseller_id, amount, transaction_id)
        )
        self.assertEqual(self.reseller.flush_reseller_pending_wholesale_credits("1988"), 1)
        self.assertEqual(self.reseller.flush_reseller_pending_wholesale_credits("1988"), 0)
        self.assertEqual(credited, [("1988", 6.0, "settlement-excess:async-payment")])
        self.assertEqual(self.read_resellers()["1988"]["pending_wholesale_credits"], [])

    def test_only_outstanding_linked_configs_become_candidates(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 7.0,
                "debt_charges": [
                    {"id": "paid", "original_amount": 5.0, "outstanding_amount": 0.0},
                    {"id": "unpaid", "original_amount": 5.0, "outstanding_amount": 5.0},
                    {"id": "legacy", "original_amount": 2.0, "outstanding_amount": 2.0},
                ],
                "configs": [
                    {"username": "paid-user", "server_id": "s1", "debt_charge_id": "paid"},
                    {"username": "unpaid-user", "server_id": "s1", "debt_charge_id": "unpaid"},
                    {"username": "unrelated", "server_id": "s1"},
                ],
            }
        })

        candidates, manual_review = self.reseller.get_reseller_debt_service_candidates(
            self.reseller.get_reseller_data("1988")
        )

        self.assertEqual([item["username"] for item in candidates], ["unpaid-user"])
        self.assertEqual(manual_review, ["legacy"])

    def test_hold_delete_prorates_by_greater_of_time_and_traffic(self):
        gib = 1024 ** 3

        class FakeClient:
            server_id = "s1"

            def __init__(self):
                self.users = {
                    "customer": {
                        "username": "customer",
                        "blocked": False,
                        "upload_bytes": 2 * gib,
                        "download_bytes": 3 * gib,
                        "max_download_bytes": 10 * gib,
                    }
                }

            def update_user(self, username, payload):
                self.users[username].update(payload)
                return {"ok": True, **payload}

            def delete_user(self, username):
                self.users.pop(username, None)
                return {"ok": True}

        class FakeMultiAPI:
            def __init__(self):
                self.client = FakeClient()

            def find_user_on_server(self, username, server_id):
                user = self.client.users.get(username)
                if user is None:
                    return self.client, None, {"status": "missing"}
                return self.client, dict(user), {"status": "found"}

        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 10.0,
                "debt_since": self.hours_ago(72),
                "debt_charges": [{
                    "id": "charge-1",
                    "original_amount": 10.0,
                    "outstanding_amount": 10.0,
                }],
                "configs": [{
                    "username": "customer",
                    "server_id": "s1",
                    "debt_charge_id": "charge-1",
                    "timestamp": self.hours_ago(72),
                    "days": 10,
                    "gb": 10,
                    "price": 10.0,
                }],
            }
        })
        api = FakeMultiAPI()

        held, hold_result = self.reseller.process_reseller_debt_service_action("1988", api, "hold")
        removed, remove_result = self.reseller.process_reseller_debt_service_action("1988", api, "remove")
        saved = self.read_resellers()["1988"]
        calculation = saved["configs"][0]["debt_proration"][0]

        self.assertTrue(held)
        self.assertEqual(hold_result["completed"], 1)
        self.assertTrue(removed)
        self.assertEqual(remove_result["completed"], 1)
        self.assertAlmostEqual(calculation["time_fraction"], 0.3, places=2)
        self.assertEqual(calculation["traffic_fraction"], 0.5)
        self.assertEqual(calculation["used_fraction"], 0.5)
        self.assertEqual(remove_result["writeoff"], 5.0)
        self.assertEqual(saved["debt"], 5.0)
        self.assertTrue(saved["configs"][0]["removed_from_vpn"])
        self.assertEqual(saved["configs"][0]["removal_reason"], "reseller_debt_default")

    def test_proration_subtracts_payments_already_allocated_to_charge(self):
        calculation = self.reseller._prorated_collectible(
            {
                "id": "charge-1",
                "original_amount": 10.0,
                "outstanding_amount": 6.0,
            },
            {
                "kind": "config",
                "days": 10,
                "gb": 10,
                "unlimited": False,
                "started_at": self.hours_ago(5 * 24),
            },
            {
                "held_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "used_bytes": 0,
                "quota_bytes": 10 * 1024 ** 3,
            },
        )

        self.assertAlmostEqual(calculation["used_fraction"], 0.5, places=2)
        self.assertEqual(calculation["already_paid"], 4.0)
        self.assertEqual(calculation["collectible"], 1.0)
        self.assertEqual(calculation["writeoff"], 5.0)

    def test_unlimited_plan_proration_uses_time_only(self):
        calculation = self.reseller._prorated_collectible(
            {"original_amount": 10.0, "outstanding_amount": 10.0},
            {
                "kind": "config",
                "days": 10,
                "unlimited": True,
                "started_at": self.hours_ago(24),
            },
            {
                "held_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "used_bytes": 100 * 1024 ** 3,
                "quota_bytes": 1,
            },
        )

        self.assertAlmostEqual(calculation["used_fraction"], 0.1, places=2)
        self.assertEqual(calculation["traffic_fraction"], 0.0)

    def test_restore_unblocks_only_configs_changed_by_debt_policy(self):
        class FakeClient:
            server_id = "s1"

            def __init__(self):
                self.users = {
                    "active": {"blocked": False, "max_download_bytes": 1024},
                    "manual": {"blocked": True, "max_download_bytes": 1024},
                }

            def update_user(self, username, payload):
                self.users[username].update(payload)
                return {"ok": True}

        class FakeMultiAPI:
            def __init__(self):
                self.client = FakeClient()

            def find_user_on_server(self, username, server_id):
                user = self.client.users.get(username)
                return self.client, dict(user), {"status": "found"}

        charges = []
        configs = []
        for index, username in enumerate(("active", "manual"), 1):
            charge_id = f"charge-{index}"
            charges.append({
                "id": charge_id,
                "original_amount": 5.0,
                "outstanding_amount": 5.0,
            })
            configs.append({
                "username": username,
                "server_id": "s1",
                "debt_charge_id": charge_id,
                "timestamp": self.hours_ago(24),
                "days": 30,
                "gb": 1,
            })
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 10.0,
                "debt_since": self.hours_ago(73),
                "debt_charges": charges,
                "configs": configs,
            }
        })
        api = FakeMultiAPI()

        self.assertTrue(self.reseller.process_reseller_debt_service_action("1988", api, "hold")[0])
        self.assertTrue(self.reseller.apply_reseller_payment("1988", 10.0, "paid")[0])
        self.assertTrue(self.reseller.process_reseller_debt_service_action("1988", api, "restore")[0])
        saved = self.read_resellers()["1988"]

        self.assertFalse(api.client.users["active"]["blocked"])
        self.assertTrue(api.client.users["manual"]["blocked"])
        self.assertTrue(all(not item["debt_policy_blocked"] for item in saved["configs"]))

    def test_unsafe_proration_never_calls_delete(self):
        class FakeClient:
            server_id = "s1"

            def __init__(self):
                self.deleted = []

            def delete_user(self, username):
                self.deleted.append(username)
                return {"ok": True}

        class FakeMultiAPI:
            def __init__(self):
                self.client = FakeClient()

            def find_user_on_server(self, username, server_id):
                return self.client, {"blocked": True}, {"status": "found"}

        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 4.0,
                "debt_charges": [{
                    "id": "legacy",
                    "original_amount": 4.0,
                    "outstanding_amount": 4.0,
                }],
                "configs": [{
                    "username": "unsafe",
                    "server_id": "s1",
                    "debt_charge_id": "legacy",
                    "debt_policy_hold_snapshot": None,
                }],
            }
        })
        api = FakeMultiAPI()

        success, result = self.reseller.process_reseller_debt_service_action("1988", api, "remove")
        saved = self.read_resellers()["1988"]

        self.assertFalse(success)
        self.assertEqual(api.client.deleted, [])
        self.assertIn("legacy", result["manual_review"])
        self.assertFalse(saved["configs"][0].get("removed_from_vpn", False))
        self.assertEqual(saved["debt"], 4.0)

    def test_unavailable_exact_server_lookup_keeps_service_and_debt_pending(self):
        class UnavailableMultiAPI:
            def find_user_on_server(self, username, server_id):
                return None, None, {"status": "unavailable", "error": "server_down"}

        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 4.0,
                "debt_charges": [{
                    "id": "charge-1",
                    "original_amount": 4.0,
                    "outstanding_amount": 4.0,
                }],
                "configs": [{
                    "username": "pending",
                    "server_id": "s1",
                    "debt_charge_id": "charge-1",
                    "timestamp": self.hours_ago(24),
                    "days": 30,
                    "gb": 5,
                    "debt_policy_hold_snapshot": {
                        "held_at": self.hours_ago(1),
                        "used_bytes": 0,
                        "quota_bytes": 5 * 1024 ** 3,
                    },
                }],
            }
        })

        success, result = self.reseller.process_reseller_debt_service_action(
            "1988", UnavailableMultiAPI(), "remove"
        )
        saved = self.read_resellers()["1988"]

        self.assertFalse(success)
        self.assertEqual(result["failed"][0]["reason"], "server_down")
        self.assertEqual(saved["debt"], 4.0)
        self.assertFalse(saved["configs"][0].get("removed_from_vpn", False))

    def test_payment_and_deletion_are_serialized_without_overwriting_each_other(self):
        delete_started = threading.Event()
        allow_delete = threading.Event()

        class BlockingClient:
            server_id = "s1"

            def delete_user(self, username):
                delete_started.set()
                allow_delete.wait(timeout=2)
                return {"ok": True}

        class FakeMultiAPI:
            client = BlockingClient()

            def find_user_on_server(self, username, server_id):
                return self.client, {"blocked": True}, {"status": "found"}

        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 10.0,
                "debt_since": self.hours_ago(170),
                "debt_cycle_id": "cycle-race",
                "debt_charges": [{
                    "id": "charge-1",
                    "original_amount": 10.0,
                    "outstanding_amount": 10.0,
                }],
                "configs": [{
                    "username": "race-user",
                    "server_id": "s1",
                    "debt_charge_id": "charge-1",
                    "timestamp": self.hours_ago(5 * 24),
                    "days": 10,
                    "gb": 10,
                    "debt_policy_blocked": True,
                    "debt_policy_hold_snapshot": {
                        "held_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "used_bytes": 0,
                        "quota_bytes": 10 * 1024 ** 3,
                    },
                }],
            }
        })
        action_result = []
        payment_result = []
        action_thread = threading.Thread(target=lambda: action_result.append(
            self.reseller.process_reseller_debt_service_action("1988", FakeMultiAPI(), "remove")
        ))
        action_thread.start()
        self.assertTrue(delete_started.wait(timeout=1))
        payment_thread = threading.Thread(target=lambda: payment_result.append(
            self.reseller.apply_reseller_payment("1988", 5.0, "race-payment")
        ))
        payment_thread.start()
        payment_thread.join(timeout=0.05)
        self.assertTrue(payment_thread.is_alive())

        allow_delete.set()
        action_thread.join(timeout=2)
        payment_thread.join(timeout=2)
        saved = self.read_resellers()["1988"]

        self.assertTrue(action_result[0][0])
        self.assertEqual(payment_result[0], (True, 0.0))
        self.assertEqual(saved["debt"], 0.0)
        self.assertTrue(saved["configs"][0]["removed_from_vpn"])

    def test_unactivated_reserved_renewal_has_zero_usage_and_full_writeoff(self):
        class FakeClient:
            server_id = "s1"

            def __init__(self):
                self.user = {"blocked": False, "upload_bytes": 100, "download_bytes": 200}

            def update_user(self, username, payload):
                self.user.update(payload)
                return {"ok": True}

            def delete_user(self, username):
                self.user = None
                return {"ok": True}

        class FakeMultiAPI:
            def __init__(self):
                self.client = FakeClient()

            def find_user_on_server(self, username, server_id):
                if self.client.user is None:
                    return self.client, None, {"status": "missing"}
                return self.client, dict(self.client.user), {"status": "found"}

        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 4.0,
                "debt_since": self.hours_ago(72),
                "debt_cycle_id": "cycle-1",
                "debt_charges": [{
                    "id": "renewal-1",
                    "original_amount": 4.0,
                    "outstanding_amount": 4.0,
                }],
                "configs": [{
                    "username": "customer",
                    "server_id": "s1",
                    "timestamp": self.hours_ago(100),
                    "renewals": [{
                        "debt_charge_id": "renewal-1",
                        "renewal_mode": "reserved",
                        "renewal_status": "reserved",
                        "timestamp": self.hours_ago(24),
                        "days": 30,
                        "gb": 5,
                    }],
                }],
            }
        })
        api = FakeMultiAPI()

        self.assertTrue(self.reseller.process_reseller_debt_service_action("1988", api, "hold")[0])
        api.client.user = None
        success, result = self.reseller.process_reseller_debt_service_action("1988", api, "remove")
        saved = self.read_resellers()["1988"]
        calculation = saved["configs"][0]["debt_proration"][0]

        self.assertTrue(success)
        self.assertEqual(calculation["used_fraction"], 0.0)
        self.assertEqual(result["writeoff"], 4.0)
        self.assertEqual(saved["debt"], 0.0)
        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["debt_cycle_id"])
        self.assertEqual(saved["configs"][0]["removed_cleanup_status"], "already_missing")

    def test_approved_reseller_auto_suspends_after_suspend_deadline(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 30.0,
                "debt_since": self.hours_ago(49),
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "suspended")
        self.assertEqual(saved["suspended_reason"], "debt")
        self.assertTrue(any(event["auto_suspended"] for event in events))

    def test_low_debt_reseller_auto_suspends_after_suspend_deadline(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 9.70,
                "debt_since": self.hours_ago(49),
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "suspended")
        self.assertEqual(saved["debt_state"], "suspended")
        self.assertEqual(saved["suspended_reason"], "debt")
        auto_suspended_event = next(event for event in events if event["auto_suspended"])
        self.assertEqual(auto_suspended_event["unlock_amount"], 9.70)

    def test_large_new_debt_does_not_suspend_before_time_deadline(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 100.0,
                "debt_since": self.hours_ago(1),
                "configs": [],
            }
        })

        saved = self.reseller.get_reseller_data("1988")
        events = self.reseller.evaluate_reseller_debt_policies()

        self.assertEqual(saved["debt_state"], "active")
        self.assertEqual(saved["status"], "approved")
        self.assertEqual([event["kind"] for event in events], ["opened"])

    def test_auto_suspended_reseller_moves_to_hold_without_ban(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 30.0,
                "debt_since": self.hours_ago(73),
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "suspended")
        self.assertEqual(saved["suspended_reason"], "debt")
        self.assertTrue(saved["debt_service_hold_due"])
        self.assertTrue(any(event["kind"] == "hold_due" for event in events))
        self.assertFalse(any(event["auto_banned"] for event in events))
        self.assertEqual(saved["credit_outcomes"][-1]["outcome"], "default")

    def test_duplicate_hold_pass_does_not_duplicate_credit_outcomes(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 7.20,
                "debt_since": self.hours_ago(73),
                "configs": [],
            }
        })

        first_events = self.reseller.evaluate_reseller_debt_policies()
        first_saved = self.read_resellers()["1988"]

        self.assertEqual(first_saved["status"], "suspended")
        self.assertTrue(any(event["kind"] == "hold_due" for event in first_events))
        self.assertEqual(
            [item["outcome"] for item in first_saved["credit_outcomes"]],
            ["late", "default"],
        )

        leased_events = self.reseller.evaluate_reseller_debt_policies()
        self.assertEqual(leased_events, [])
        self.assertTrue(self.reseller.complete_reseller_debt_service_action_claim(
            "1988", "hold", completed=False
        ))
        second_events = self.reseller.evaluate_reseller_debt_policies()
        second_saved = self.read_resellers()["1988"]

        self.assertEqual(second_saved["status"], "suspended")
        self.assertTrue(any(event["kind"] == "hold_due" for event in second_events))
        self.assertEqual(
            [item["outcome"] for item in second_saved["credit_outcomes"]],
            ["late", "default"],
        )

    def test_debtless_banned_reseller_does_not_emit_debt_admin_alert(self):
        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 0.0,
                "debt_last_admin_alert_level": "banned",
                "configs": [],
            }
        })

        first_events = self.reseller.evaluate_reseller_debt_policies()
        first_saved = self.read_resellers()["1988"]
        second_events = self.reseller.evaluate_reseller_debt_policies()
        second_saved = self.read_resellers()["1988"]

        self.assertEqual(first_events, [])
        self.assertEqual(second_events, [])
        self.assertEqual(first_saved["debt_state"], "active")
        self.assertEqual(first_saved["debt_last_admin_alert_level"], "none")
        self.assertEqual(second_saved["debt_last_admin_alert_level"], "none")

    def test_existing_banned_reseller_is_untouched_by_debt_lifecycle(self):
        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 12.0,
                "debt_since": self.hours_ago(500),
                "configs": [{"username": "preserved", "server_id": "s1", "price": 12.0}],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(events, [])
        self.assertEqual(saved["status"], "banned")
        self.assertFalse(saved["debt_service_hold_due"])
        self.assertFalse(saved["debt_service_remove_due"])
        self.assertFalse(saved["configs"][0].get("removed_from_vpn", False))

    def test_zero_debt_unban_grace_is_repaired_to_approved_after_deadline(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
                "suspended_at": self.hours_ago(25),
                "debt": 0.0,
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["suspended_reason"])
        self.assertIsNone(saved["suspended_at"])
        self.assertEqual([event["kind"] for event in events], ["recovered"])

    def test_sub_threshold_debt_recovers_unban_grace_without_auto_ban(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
                "suspended_at": self.hours_ago(25),
                "debt": 0.50,
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["suspended_reason"])
        self.assertEqual([event["kind"] for event in events], ["recovered"])
        self.assertFalse(any(event["auto_banned"] for event in events))

    def test_unban_with_positive_debt_moves_banned_reseller_to_temporary_suspended(self):
        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 5.0,
                "configs": [],
            }
        })

        self.reseller.update_reseller_status(
            "1988",
            "suspended",
            suspended_reason=self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
        )
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "suspended")
        self.assertEqual(saved["suspended_reason"], self.reseller.SUSPENDED_REASON_UNBAN_GRACE)
        self.assertIsNotNone(saved["suspended_at"])

    def test_unban_with_zero_debt_approves_immediately(self):
        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 0.0,
                "configs": [],
            }
        })

        self.reseller.update_reseller_status(
            "1988",
            "suspended",
            suspended_reason=self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
        )
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["suspended_reason"])
        self.assertIsNone(saved["suspended_at"])

    def test_explicit_suspension_with_zero_debt_stays_suspended(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0.0,
                "configs": [],
            }
        })

        self.reseller.update_reseller_status("1988", "suspended")
        saved = self.read_resellers()["1988"]

        self.assertEqual(saved["status"], "suspended")
        self.assertIsNone(saved["suspended_reason"])
        self.assertIsNotNone(saved["suspended_at"])

    def test_suspension_restoration_uses_cent_rounded_zero(self):
        for debt, expected_status in ((0.004, "suspended"), (0.005, "suspended")):
            with self.subTest(debt=debt):
                self.write_resellers({
                    "1988": {
                        "status": "suspended",
                        "suspended_reason": None,
                        "suspended_at": self.hours_ago(1),
                        "debt": debt,
                        "configs": [],
                    }
                })

                self.reseller.evaluate_reseller_debt_policies()
                saved = self.read_resellers()["1988"]

                self.assertEqual(saved["status"], expected_status)

    def test_cleared_auto_suspension_restores_approved_status(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": "debt",
                "debt": 30.0,
                "debt_since": "2000-01-01 00:00:00",
                "configs": [],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 30.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.0)
        self.assertEqual(saved["status"], "approved")
        self.assertEqual(saved["debt"], 0.0)
        self.assertIsNone(saved["debt_since"])
        self.assertIsNotNone(saved["last_payment_at"])
        self.assertIsNone(saved["suspended_reason"])

    def test_manual_suspension_is_preserved_when_debt_is_cleared(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": None,
                "debt": 30.0,
                "debt_since": "2000-01-01 00:00:00",
                "configs": [],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 30.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.0)
        self.assertEqual(saved["status"], "suspended")
        self.assertIsNone(saved["suspended_reason"])

    def test_partial_payment_below_threshold_restores_debt_suspension(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
                "suspended_at": self.hours_ago(1),
                "debt": 1.50,
                "configs": [],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 1.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.50)
        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["suspended_reason"])
        self.assertIsNone(saved["suspended_at"])
        self.assertIsNone(saved["debt_cycle_id"])

    def test_debt_tracking_threshold_is_strictly_below_one_dollar(self):
        self.assertTrue(self.reseller._is_debt_fully_settled(0.99))
        self.assertFalse(self.reseller._is_debt_fully_settled(1.00))

    def test_sub_threshold_debt_does_not_open_collection_cycle(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 0.99,
                "debt_since": self.hours_ago(200),
                "debt_cycle_id": "legacy-cycle",
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual(events, [])
        self.assertEqual(saved["debt"], 0.99)
        self.assertEqual(saved["debt_state"], "active")
        self.assertIsNone(saved["debt_since"])
        self.assertIsNone(saved["debt_cycle_id"])

    def test_one_dollar_debt_opens_collection_cycle(self):
        self.write_resellers({
            "1988": {
                "status": "approved",
                "debt": 1.00,
                "configs": [],
            }
        })

        events = self.reseller.evaluate_reseller_debt_policies()
        saved = self.read_resellers()["1988"]

        self.assertEqual([event["kind"] for event in events], ["opened"])
        self.assertIsNotNone(saved["debt_since"])
        self.assertIsNotNone(saved["debt_cycle_id"])

    def test_full_payment_restores_unban_grace_suspension(self):
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
                "suspended_at": self.hours_ago(1),
                "debt": 5.0,
                "configs": [],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 5.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.0)
        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["suspended_reason"])
        self.assertIsNone(saved["suspended_at"])

    def test_full_payment_restoration_does_not_depend_on_settlement_threshold(self):
        self.reseller.DEBT_SETTLEMENT_THRESHOLD = 0.0
        self.write_resellers({
            "1988": {
                "status": "suspended",
                "suspended_reason": self.reseller.SUSPENDED_REASON_UNBAN_GRACE,
                "suspended_at": self.hours_ago(1),
                "debt_since": self.hours_ago(2),
                "debt": 5.0,
                "configs": [],
            }
        })

        success, new_debt = self.reseller.apply_reseller_payment("1988", 5.0)
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(new_debt, 0.0)
        self.assertEqual(saved["status"], "approved")
        self.assertIsNone(saved["debt_since"])

    def test_clear_and_zero_adjustment_restore_suspended_resellers(self):
        for operation in ("clear", "adjust"):
            with self.subTest(operation=operation):
                self.write_resellers({
                    "1988": {
                        "status": "suspended",
                        "suspended_reason": None,
                        "suspended_at": self.hours_ago(1),
                        "debt": 5.0,
                        "configs": [],
                    }
                })

                if operation == "clear":
                    self.assertTrue(self.reseller.clear_reseller_debt("1988"))
                else:
                    self.assertTrue(self.reseller.set_reseller_debt("1988", 0.0))

                saved = self.read_resellers()["1988"]
                self.assertEqual(saved["debt"], 0.0)
                self.assertEqual(saved["status"], "suspended")
                self.assertIsNone(saved["suspended_reason"])
                self.assertIsNotNone(saved["suspended_at"])

    def test_cleanup_candidates_include_only_configs_after_last_payment(self):
        candidates = self.reseller.get_banned_reseller_cleanup_candidates({
            "status": "banned",
            "last_payment_at": "2026-06-01 12:00:00",
            "configs": [
                {"username": "old", "timestamp": "2026-06-01 11:59:59", "price": 2},
                {"username": "new", "timestamp": "2026-06-01 12:00:01", "price": 3},
                {"username": "", "timestamp": "2026-06-01 12:00:02", "price": 4},
                {"timestamp": "2026-06-01 12:00:03", "price": 5},
            ],
        })

        self.assertEqual([candidate["username"] for candidate in candidates], ["new"])
        self.assertEqual(candidates[0]["config_index"], 1)

    def test_cleanup_candidates_without_payment_include_all_reseller_configs(self):
        candidates = self.reseller.get_banned_reseller_cleanup_candidates({
            "status": "banned",
            "last_payment_at": None,
            "configs": [
                {"username": "first", "timestamp": "2026-05-01 00:00:00", "price": 2},
                {"username": "second", "timestamp": "2026-06-01 00:00:00", "price": 3},
            ],
        })

        self.assertEqual([candidate["username"] for candidate in candidates], ["first", "second"])

    def test_cleanup_candidates_skip_already_tagged_removed_configs(self):
        candidates = self.reseller.get_banned_reseller_cleanup_candidates({
            "status": "banned",
            "last_payment_at": None,
            "configs": [
                {
                    "username": "already",
                    "timestamp": "2026-06-01 00:00:00",
                    "price": 2,
                    "removed_from_vpn": True,
                    "removal_reason": "banned_reseller_cleanup",
                },
                {"username": "new", "timestamp": "2026-06-02 00:00:00", "price": 3},
            ],
        })

        self.assertEqual([candidate["username"] for candidate in candidates], ["new"])

    def test_cleanup_deletes_success_and_missing_records_and_keeps_failures(self):
        class FakeClient:
            def __init__(self, delete_result):
                self.delete_result = delete_result
                self.deleted = []

            def delete_user(self, username):
                self.deleted.append(username)
                return self.delete_result

        class FakeMultiAPI:
            def __init__(self):
                self.success_client = FakeClient({"ok": True})
                self.failed_client = FakeClient(None)

            def find_user(self, username, preferred_server_id=None):
                if username == "deleted":
                    return self.success_client, {"username": username}
                if username == "failed":
                    return self.failed_client, {"username": username}
                return None, None

        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 15.0,
                "last_payment_at": "2026-06-01 12:00:00",
                "configs": [
                    {"username": "paid", "timestamp": "2026-06-01 11:00:00", "price": 4.0},
                    {"username": "deleted", "timestamp": "2026-06-01 13:00:00", "price": 5.0},
                    {"username": "missing", "timestamp": "2026-06-01 14:00:00", "price": 3.0},
                    {"username": "failed", "timestamp": "2026-06-01 15:00:00", "price": 2.0},
                ],
            }
        })

        success, result = self.reseller.cleanup_banned_reseller_users("1988", FakeMultiAPI())
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual([item["username"] for item in result["deleted"]], ["deleted"])
        self.assertEqual([item["username"] for item in result["already_missing"]], ["missing"])
        self.assertEqual([item["username"] for item in result["failed"]], ["failed"])
        self.assertEqual([config["username"] for config in saved["configs"]], ["paid", "deleted", "missing", "failed"])
        tagged_by_username = {config["username"]: config for config in saved["configs"]}
        self.assertFalse(tagged_by_username["paid"].get("removed_from_vpn", False))
        self.assertTrue(tagged_by_username["deleted"]["removed_from_vpn"])
        self.assertEqual(tagged_by_username["deleted"]["removal_reason"], "banned_reseller_cleanup")
        self.assertEqual(tagged_by_username["deleted"]["removed_cleanup_status"], "deleted_from_vpn")
        self.assertEqual(
            tagged_by_username["deleted"]["removal_note"],
            "Removed during banned reseller unpaid user cleanup",
        )
        self.assertIn("removed_at", tagged_by_username["deleted"])
        self.assertTrue(tagged_by_username["missing"]["removed_from_vpn"])
        self.assertEqual(tagged_by_username["missing"]["removed_cleanup_status"], "already_missing")
        self.assertFalse(tagged_by_username["failed"].get("removed_from_vpn", False))
        self.assertEqual(saved["debt"], 7.0)
        self.assertEqual(result["remaining_debt"], 7.0)
        self.assertEqual(result["tagged_count"], 2)

    def test_cleanup_reduces_debt_no_below_zero(self):
        class MissingMultiAPI:
            def find_user(self, username, preferred_server_id=None):
                return None, None

        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 2.0,
                "last_payment_at": None,
                "configs": [
                    {"username": "one", "timestamp": "2026-06-01 13:00:00", "price": 5.0},
                ],
            }
        })

        success, result = self.reseller.cleanup_banned_reseller_users("1988", MissingMultiAPI())
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual(saved["debt"], 0.0)
        self.assertEqual(len(saved["configs"]), 1)
        self.assertEqual(saved["configs"][0]["username"], "one")
        self.assertTrue(saved["configs"][0]["removed_from_vpn"])
        self.assertEqual(saved["configs"][0]["removed_cleanup_status"], "already_missing")
        self.assertEqual(saved["total_paid"], 0.0)
        self.assertEqual(result["remaining_debt"], 0.0)

    def test_cleanup_keeps_failed_config_value_in_debt(self):
        class FakeClient:
            def __init__(self, delete_result):
                self.delete_result = delete_result

            def delete_user(self, username):
                return self.delete_result

        class FakeMultiAPI:
            def find_user(self, username, preferred_server_id=None):
                if username == "removed":
                    return FakeClient({"ok": True}), {"username": username}
                return FakeClient(None), {"username": username}

        self.write_resellers({
            "1988": {
                "status": "banned",
                "debt": 4.0,
                "last_payment_at": None,
                "configs": [
                    {"username": "removed", "timestamp": "2026-06-01 13:00:00", "price": 5.0},
                    {"username": "failed", "timestamp": "2026-06-01 14:00:00", "price": 2.0},
                ],
            }
        })

        success, result = self.reseller.cleanup_banned_reseller_users("1988", FakeMultiAPI())
        saved = self.read_resellers()["1988"]

        self.assertTrue(success)
        self.assertEqual([config["username"] for config in saved["configs"]], ["removed", "failed"])
        self.assertTrue(saved["configs"][0]["removed_from_vpn"])
        self.assertEqual(saved["configs"][0]["removed_cleanup_status"], "deleted_from_vpn")
        self.assertFalse(saved["configs"][1].get("removed_from_vpn", False))
        self.assertEqual(saved["debt"], 2.0)
        self.assertEqual(result["remaining_debt"], 2.0)

    def test_reseller_claim_retry_boundaries_accept_aware_and_legacy_timestamps(self):
        current = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        reservation = {
            "reservation_id": "boundary",
            "renewal_mode": "reserved",
            "renewal_status": "processing",
            "renewal_claim_id": "worker",
            "renewal_claimed_at": "2026-08-02 15:20:01",
        }
        self.write_resellers({
            "1988": {
                "status": "approved",
                "configs": [{"username": "bob", "renewals": [reservation]}],
            }
        })

        self.assertIsNone(self.reseller.claim_reseller_renewal_reservation(
            "1988", "boundary", now=current
        ))

        reservation.update({
            "renewal_status": "attention",
            "renewal_attention_reason": "renewal_internal_error",
            "renewal_next_attempt_at": "2026-08-02T15:30:00+03:30",
        })
        self.write_resellers({
            "1988": {
                "status": "approved",
                "configs": [{"username": "bob", "renewals": [reservation]}],
            }
        })
        claimed = self.reseller.claim_reseller_renewal_reservation(
            "1988", "boundary", now=current
        )
        self.assertIsNotNone(claimed)

    def test_reseller_finish_clears_internal_error_metadata_on_recovery(self):
        current = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        reservation = {
            "reservation_id": "recovery",
            "renewal_mode": "reserved",
            "renewal_status": "processing",
            "renewal_claim_id": "claim",
            "renewal_internal_error_type": "RuntimeError",
            "renewal_internal_error_at": "2026-08-02 15:00:00",
        }
        self.write_resellers({
            "1988": {
                "status": "approved",
                "configs": [{"username": "bob", "renewals": [reservation]}],
            }
        })

        self.assertTrue(self.reseller.finish_reseller_renewal_reservation(
            "1988", "recovery", "claim", "reserved", now=current
        ))
        saved = self.read_resellers()["1988"]["configs"][0]["renewals"][0]
        self.assertEqual(saved["renewal_status"], "reserved")
        self.assertNotIn("renewal_internal_error_type", saved)
        self.assertNotIn("renewal_internal_error_at", saved)


if __name__ == "__main__":
    unittest.main()
