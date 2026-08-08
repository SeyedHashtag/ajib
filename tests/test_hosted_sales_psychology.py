import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "core" / "scripts" / "telegrambot"
sys.path.insert(0, str(BOT_DIR))
os.environ["AJIB_BOT_ROLE"] = "supervisor"

from utils import hosted_bots


WORKER_PATH = BOT_DIR / "hosted_worker.py"
WORKER_SOURCE = WORKER_PATH.read_text(encoding="utf-8")
WORKER_TREE = ast.parse(WORKER_SOURCE)


def worker_function(name):
    return next(
        node
        for node in WORKER_TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


class HostedSalesPsychologyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        hosted_bots.BOT_DIR = str(root)
        hosted_bots.HOSTED_ROOT = str(root / "hosted_bots")
        hosted_bots.REGISTRY_FILE = str(root / "hosted_bots.json")
        hosted_bots.SECRETS_FILE = str(root / "hosted_bot_tokens.json")

    def test_localized_messages_override_legacy_text_and_keep_it_as_fallback(self):
        settings = hosted_bots.update_settings("7", {
            "welcome_text": "Legacy welcome",
            "support_text": "Legacy support",
            "welcome_texts": {"fa": "خوش آمدید"},
            "support_texts": {"ru": "Поддержка"},
            "recommended_plan_id": "30",
        })

        self.assertEqual(
            hosted_bots.localized_storefront_text(settings, "welcome", "fa"),
            "خوش آمدید",
        )
        self.assertEqual(
            hosted_bots.localized_storefront_text(settings, "welcome", "ru"),
            "Legacy welcome",
        )
        self.assertEqual(
            hosted_bots.localized_storefront_text(settings, "support", "ru"),
            "Поддержка",
        )
        self.assertEqual(settings["recommended_plan_id"], "30")

    def test_localized_settings_reject_unknown_languages_and_invalid_plan_ids(self):
        with self.assertRaises(ValueError):
            hosted_bots.update_settings("7", {"welcome_texts": {"de": "Hallo"}})
        with self.assertRaises(ValueError):
            hosted_bots.update_settings("7", {"recommended_plan_id": "../../30"})

    def test_invited_buyer_discount_stacks_to_ten_percent_and_uses_net_margin(self):
        quote = hosted_bots.calculate_quote(
            80,
            20,
            referral_margin_percent=20,
            referred=True,
            retail_base=100,
            buyer_discount_percent=5,
        )

        self.assertEqual(quote["original_price"], 120.0)
        self.assertEqual(quote["card_collected"], 114.0)
        self.assertEqual(quote["crypto_collected"], 108.0)
        self.assertEqual(quote["card_discount_percent"], 5.0)
        self.assertEqual(quote["crypto_discount_percent"], 10.0)
        self.assertEqual(quote["crypto_component_discount_percent"], 5.0)
        self.assertEqual(quote["buyer_discount_amount"], 6.0)
        self.assertEqual(quote["crypto_component_discount_amount"], 6.0)
        self.assertEqual(quote["card_referral_reward"], 6.8)
        self.assertEqual(quote["crypto_referral_reward"], 5.6)
        self.assertEqual(
            quote["crypto_referral_reward"],
            round((quote["crypto_collected"] - quote["wholesale"]) * 0.20, 2),
        )

    def test_crypto_component_yields_to_the_ten_percent_total_cap(self):
        quote = hosted_bots.calculate_quote(
            70,
            0,
            retail_base=100,
            buyer_discount_percent=10,
        )

        self.assertEqual(quote["card_discount_percent"], 10.0)
        self.assertEqual(quote["crypto_discount_percent"], 10.0)
        self.assertEqual(quote["crypto_component_discount_percent"], 0.0)
        self.assertEqual(quote["buyer_discount_amount"], 10.0)
        self.assertEqual(quote["crypto_component_discount_amount"], 0.0)

    def test_rounded_discount_components_sum_to_the_exact_collected_total(self):
        quote = hosted_bots.calculate_quote(
            0,
            0,
            retail_base=0.10,
            buyer_discount_percent=5,
        )

        self.assertEqual(
            round(quote["buyer_discount_amount"] + quote["crypto_component_discount_amount"], 2),
            quote["crypto_discount_amount"],
        )
        self.assertEqual(
            round(quote["original_price"] - quote["buyer_discount_amount"], 2),
            quote["card_collected"],
        )

    def test_discounted_routes_are_disabled_before_they_cross_wholesale_cost(self):
        quote = hosted_bots.calculate_quote(
            115,
            20,
            retail_base=100,
            buyer_discount_percent=5,
        )

        self.assertFalse(quote["card_supported"])
        self.assertFalse(quote["crypto_supported"])
        self.assertLess(quote["card_collected"], quote["wholesale"])
        self.assertLess(quote["crypto_collected"], quote["wholesale"])


class HostedSalesPsychologyWorkerTests(unittest.TestCase):
    def test_quick_picks_deduplicate_cheapest_recommended_and_best_value(self):
        namespace = {
            "_hosted_plan_quote": lambda plan, settings: {
                "retail": float(plan["price"]),
            },
        }
        module = ast.Module(body=[worker_function("_quick_pick_plans")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)

        choices = namespace["_quick_pick_plans"](
            {
                "5": {"price": 5, "gb": 5},
                "20": {"price": 12, "gb": 20},
                "50": {"price": 20, "gb": 50},
            },
            {"recommended_plan_id": "20"},
        )

        self.assertEqual(
            choices,
            [("5", "pick_cheapest"), ("20", "pick_recommended"), ("50", "pick_best_value")],
        )

    def test_plan_selector_lists_all_plans_in_size_order_with_labels(self):
        class Markup:
            def __init__(self, *args, **kwargs):
                self.keyboard = []

            def add(self, *buttons):
                self.keyboard.extend([button] for button in buttons)

        class Button:
            def __init__(self, text, callback_data):
                self.text = text
                self.callback_data = callback_data

        class Bot:
            def __init__(self):
                self.sent = []

            def send_message(self, *args, **kwargs):
                self.sent.append((args, kwargs))

        plans = {
            "50": {"price": 20, "gb": 50},
            "10": {"price": 18, "gb": 35},
            "90": {"price": 5, "gb": 5},
            "20": {"price": 12, "gb": 20},
        }
        bot = Bot()
        growth_events = []

        def plan_button_text(
            _user_id,
            _plan_id,
            plan,
            _quote,
            label_key=None,
            exchange_rate=None,
        ):
            return f"{label_key or 'plain'}:{plan['gb']}"

        namespace = {
            "types": type("Types", (), {
                "InlineKeyboardMarkup": Markup,
                "InlineKeyboardButton": Button,
            }),
            "OWNER_ID": 7,
            "get_settings": lambda _owner_id: {"recommended_plan_id": "20"},
            "_sellable_plans": lambda: plans,
            "get_exchange_rate": lambda: 1,
            "_hosted_plan_quote": lambda plan, _settings: {"retail": float(plan["price"])},
            "_plan_button_text": plan_button_text,
            "_hosted_message": lambda _user_id, key: key,
            "_message": lambda _user_id, key: key,
            "_record_growth": lambda *args, **kwargs: growth_events.append((args, kwargs)),
            "bot": bot,
        }
        module = ast.Module(
            body=[worker_function("_quick_pick_plans"), worker_function("_show_plans")],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), "hosted_worker.py", "exec"), namespace)

        namespace["_show_plans"](555, 1988)

        args, kwargs = bot.sent[0]
        self.assertEqual(args, (555, "all_plans_title"))
        buttons = [row[0] for row in kwargs["reply_markup"].keyboard]
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["hb:buy:90", "hb:buy:20", "hb:buy:10", "hb:buy:50"],
        )
        self.assertEqual(
            [button.text for button in buttons],
            ["pick_cheapest:5", "pick_recommended:20", "plain:35", "pick_best_value:50"],
        )
        self.assertNotIn("hb:plans:all", [button.callback_data for button in buttons])
        self.assertEqual(
            growth_events,
            [(("plan_viewed", 1988), {
                "deduplication_key": "hosted-plan-viewed:7:1988:direct:catalog",
            })],
        )

    def test_legacy_hosted_all_plans_callback_uses_unified_selector(self):
        source = ast.get_source_segment(WORKER_SOURCE, worker_function("plans_all"))

        self.assertIn(
            '@bot.callback_query_handler(func=lambda c: c.data == "hb:plans:all")',
            WORKER_SOURCE,
        )
        self.assertIn("_show_plans(", source)
        self.assertNotIn("show_all", source)

    def test_checkout_records_exact_discount_attribution_and_growth_hooks(self):
        payment_method = ast.get_source_segment(WORKER_SOURCE, worker_function("payment_method"))
        purchase_options = ast.get_source_segment(WORKER_SOURCE, worker_function("_purchase_options"))

        for field in (
            "original_price",
            "invite_discount_percent",
            "crypto_discount_percent",
            "total_discount_percent",
            "collected_amount",
            "reward_calculation_base",
            "referral_attribution",
        ):
            self.assertIn(f'"{field}"', payment_method)
        self.assertIn('"risk_disclosure"', purchase_options)
        self.assertNotIn('"purchase_connection_warning"', payment_method)
        self.assertIn('"checkout_started"', payment_method)
        self.assertIn('"checkout_completed"', WORKER_SOURCE)
        self.assertIn('quote["crypto_component_discount_percent"]', payment_method)
        self.assertNotIn('"account_credit_reserved":', payment_method)

    def test_completion_growth_includes_renewals_and_first_sale_in_reachable_code(self):
        completed = ast.get_source_segment(
            WORKER_SOURCE,
            worker_function("_record_completed_growth"),
        )
        reconcile = ast.get_source_segment(
            WORKER_SOURCE,
            worker_function("_reconcile_invite_discount_reservations"),
        )

        self.assertIn('"renewal_completed"', completed)
        self.assertIn('"hosted_first_sale"', completed)
        self.assertNotIn('"renewal_completed"', reconcile)

    def test_onboarding_is_language_and_customer_state_aware(self):
        start = ast.get_source_segment(WORKER_SOURCE, worker_function("start"))
        onboarding = ast.get_source_segment(WORKER_SOURCE, worker_function("_send_onboarding"))

        self.assertIn("_telegram_language", start)
        self.assertIn("_language_markup", start)
        for state in ("new", "trial", "trial_active", "paid", "expired"):
            self.assertIn(f'"{state}"', onboarding)
        self.assertIn('callback_data="hb:test:start"', onboarding)
        self.assertIn('callback_data="hb:renewcfg:0"', onboarding)


if __name__ == "__main__":
    unittest.main()
