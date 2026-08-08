import datetime
import os
import types
from unittest.mock import patch

from test_crypto_payment_discount import DummyBot, load_purchase_plan, make_call


def test_quick_picks_are_factual_and_deduplicated():
    module = load_purchase_plan(DummyBot(), [])
    plans = {
        "10": {"price": 2, "days": 30},
        "20": {"price": 3, "days": 30, "recommended": True},
        "40": {"price": 4, "days": 30},
        "100": {"price": 1, "days": 30, "target": "reseller"},
    }

    picks = module.select_quick_pick_plans(plans)

    assert [(label, plan_id) for label, plan_id, _details in picks] == [
        ("quick_pick_cheapest", "10"),
        ("quick_pick_recommended", "20"),
        ("quick_pick_best_value", "40"),
    ]

    plans["40"]["recommended"] = True
    plans["20"].pop("recommended")
    deduplicated = module.select_quick_pick_plans(plans)
    assert [plan_id for _label, plan_id, _details in deduplicated] == ["10", "40"]


def test_missing_recommendation_uses_truthful_balanced_label():
    module = load_purchase_plan(DummyBot(), [])
    plans = {
        "10": {"price": 2, "days": 30},
        "20": {"price": 3, "days": 30},
        "40": {"price": 6, "days": 30},
    }

    picks = module.select_quick_pick_plans(plans)

    assert any(label == "quick_pick_balanced" and plan_id == "20" for label, plan_id, _ in picks)
    assert all(label != "quick_pick_recommended" for label, _plan_id, _ in picks)


def test_plan_selector_lists_every_customer_plan_once_on_one_page():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module.load_plans = lambda: {
        "60": {"price": 5, "days": 30},
        "10": {"price": 2, "days": 30},
        "20": {"price": 3, "days": 30, "recommended": True},
        "40": {"price": 4, "days": 30},
        "100": {"price": 1, "days": 30, "target": "reseller"},
        "invalid": {"price": 1, "days": 30},
    }
    module.get_exchange_rate = lambda: 1
    messages = {
        "all_plans_title": "All available plans",
        "customer_plan_button": "{label}{plan_gb} GB · {price_pair} · {days} days",
        "plan_price_pair_usd_first": "${usd} / {toman}",
        "quick_pick_cheapest": "Lowest price",
        "quick_pick_recommended": "Recommended",
        "quick_pick_balanced": "Balanced",
        "quick_pick_best_value": "Best value",
    }
    module.get_message_text = lambda _language, key: messages[key]
    growth_events = []
    module.record_main_growth_event = lambda *args, **kwargs: growth_events.append(
        (args, kwargs)
    )

    with patch.dict(os.environ, {"AJIB_RECOMMENDED_PLAN_ID": ""}):
        module.show_plans(555, 1988)

    args, kwargs = bot.sent_messages[0]
    assert args == (555, "All available plans")
    buttons = kwargs["reply_markup"].buttons
    assert [button.kwargs["callback_data"] for button in buttons] == [
        "purchase:10",
        "purchase:20",
        "purchase:40",
        "purchase:60",
    ]
    assert "Lowest price" in buttons[0].args[0]
    assert "Recommended" in buttons[1].args[0]
    assert buttons[2].args[0].startswith("40 GB")
    assert "Best value" in buttons[3].args[0]
    assert all(button.kwargs["callback_data"] != "show_all_plans" for button in buttons)
    assert growth_events == [
        (("plan_viewed", 1988), {
            "language": "en",
            "deduplication_key": "main:plan_viewed:1988:catalog",
            "catalog": "all",
        })
    ]


def test_legacy_all_plans_callback_opens_the_unified_selector():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module.load_plans = lambda: {
        "20": {"price": 3, "days": 30},
        "10": {"price": 2, "days": 30},
    }
    module.get_exchange_rate = lambda: 1

    module.handle_show_all_plans(make_call("show_all_plans"))

    assert len(bot.edited_messages) == 1
    buttons = bot.edited_messages[0][1]["reply_markup"].buttons
    assert [button.kwargs["callback_data"] for button in buttons] == [
        "purchase:10",
        "purchase:20",
    ]
    assert len(bot.callback_answers) == 1


def test_persian_price_pair_and_totals_are_toman_first():
    module = load_purchase_plan(DummyBot(), [])
    messages = {
        "plan_price_pair_toman_first": "{toman} toman / ${usd}",
        "plan_price_pair_usd_first": "${usd} / {toman} toman",
        "plan_payment_totals_toman_first": "card={card_total};crypto=${crypto_total};base=${original_usd}",
        "plan_payment_totals_usd_first": "base=${original_usd};crypto=${crypto_total};card={card_total}",
    }
    module.get_message_text = lambda _language, key: messages[key]

    assert module._plan_price_pair("fa", 10, 60_000) == "600000 toman / $10.00"
    assert module._plan_price_pair("en", 10, 60_000) == "$10.00 / 600000 toman"
    assert module.build_plan_payment_totals("fa", "40", 10, 60_000).startswith("card=600000")
    assert "crypto=$9.50" in module.build_plan_payment_totals("fa", "40", 10, 60_000)


def test_referred_first_purchase_shows_exact_card_and_capped_crypto_totals():
    module = load_purchase_plan(DummyBot(), [])
    messages = {
        "plan_payment_totals_usd_first": (
            "base=${original_usd};crypto={crypto_percent}%:${crypto_total};card={card_total}"
        ),
    }
    module.get_message_text = lambda _language, key: messages[key]

    totals = module.build_plan_payment_totals(
        "en",
        "40",
        10,
        60_000,
        invite_discount_percent=5,
    )

    assert totals == "base=$10.00;crypto=10%:$9.00;card=570000"


def test_network_disclosure_is_shown_only_on_first_plan_detail():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module._PURCHASE_DISCLOSURE_FALLBACK.clear()
    original_get_message = module.get_message_text
    module.get_message_text = lambda language, key: (
        "\n\nVPN warning\n\n"
        if key == "purchase_connection_warning"
        else original_get_message(language, key)
    )

    with patch.dict(os.environ, {"CRYPTO_MERCHANT_ID": "merchant", "CRYPTO_API_KEY": "key"}):
        module.handle_purchase_selection(make_call("purchase:40"))
        module.handle_purchase_selection(make_call("purchase:40"))

    first = bot.edited_messages[0][0][0]
    second = bot.edited_messages[1][0][0]
    assert "VPN warning" in first
    assert "VPN warning" not in second


def test_checkout_reminder_is_sent_once_and_persisted():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    now = datetime.datetime(2026, 8, 5, 12, 0, 0)
    record = {
        "status": "pending",
        "user_id": 1988,
        "plan_gb": "40",
        "price": 95,
        "payment_url": "https://pay.example/checkout",
        "created_at": "2026-08-05 11:29:00",
    }

    def persist(_payment_id, fields):
        record.update(fields)
        return True

    module.update_payment_record_fields = persist
    assert module.maybe_send_checkout_reminder("payment-1", record, now=now)
    assert not module.maybe_send_checkout_reminder("payment-1", record, now=now)
    assert len(bot.sent_messages) == 1
    assert record["checkout_reminded_at"] == "2026-08-05 12:00:00"


def test_card_checkout_reminder_is_durable_once_and_uses_exact_total():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module._CARD_CHECKOUT_FALLBACK.clear()
    original_get_message = module.get_message_text
    module.get_message_text = lambda language, key: (
        "Plan {plan_gb}; exact card total {final_amount} toman"
        if key == "abandoned_card_checkout_reminder"
        else original_get_message(language, key)
    )
    started = datetime.datetime(2026, 8, 5, 11, 29, 0)
    now = datetime.datetime(2026, 8, 5, 12, 0, 0)
    checkout_id = module._register_card_checkout(
        1988,
        555,
        "40",
        6_000_000,
        "payment_method:card_to_card:40",
        now=started,
    )

    assert module.send_due_card_checkout_reminders(now=now) == 1
    assert module.send_due_card_checkout_reminders(now=now) == 0
    assert len(bot.sent_messages) == 1
    assert "6000000 toman" in bot.sent_messages[0][0][1]
    assert (
        module._CARD_CHECKOUT_FALLBACK[checkout_id]["checkout_reminded_at"]
        == "2026-08-05 12:00:00"
    )


def test_card_checkout_cancel_closes_durable_reminder_state():
    module = load_purchase_plan(DummyBot(), [])
    module._CARD_CHECKOUT_FALLBACK.clear()
    checkout_id = module._register_card_checkout(
        1988,
        555,
        "40",
        6_000_000,
        "payment_method:card_to_card:40",
        now=datetime.datetime(2026, 8, 5, 11, 0, 0),
    )

    assert module._close_card_checkout(
        checkout_id,
        "canceled",
        now=datetime.datetime(2026, 8, 5, 11, 1, 0),
    )
    assert module.send_due_card_checkout_reminders(
        now=datetime.datetime(2026, 8, 5, 12, 0, 0)
    ) == 0
    assert module._CARD_CHECKOUT_FALLBACK[checkout_id]["status"] == "canceled"


def test_card_checkout_persists_exact_incentive_quote_and_releases_on_cancel():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    released = []
    quote = {
        "price": 8.0,
        "original_price": 10.0,
        "invite_discount_percent": 5.0,
        "invite_discount_amount": 0.5,
        "payment_discount_percent": 0.0,
        "payment_discount_amount": 0.0,
        "discount_percent": 5.0,
        "discount_amount": 0.5,
        "discounted_total": 9.5,
        "account_credit_reserved": 1.5,
        "account_credit_reservation_id": "card-quote",
        "incentive_reservation_id": "card-quote",
        "collected_amount": 8.0,
        "referral_reward_base": 8.0,
        "fully_credit_funded": False,
    }
    module.load_plans = lambda: {
        "40": {"price": 10.0, "days": 30, "unlimited": False}
    }
    module.get_exchange_rate = lambda: 60_000.0
    module._reserve_checkout_incentives = lambda *_args, **_kwargs: dict(quote)
    module._release_checkout_incentives = (
        lambda user_id, reservation_id: released.append((user_id, reservation_id)) or True
    )
    with patch.object(module.uuid, "uuid4", return_value=types.SimpleNamespace(hex="card-quote")):
        module.handle_card_to_card_payment(make_call("payment_method:card_to_card:40"), "40")

    state = module.user_data[1988]
    assert state["price"] == 8.0
    assert state["converted_amount"] == 480_000.0
    assert state["incentive_metadata"]["referral_reward_base"] == 8.0
    assert module._CARD_CHECKOUT_FALLBACK["card-quote"]["final_amount"] == 480_000.0

    module.handle_cancel_purchase(make_call("cancel_purchase"))

    assert released == [(1988, "card-quote")]
    assert module._CARD_CHECKOUT_FALLBACK["card-quote"]["status"] == "canceled"


def test_fully_credit_funded_card_purchase_completes_without_receipt_checkout():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    store = {}
    finalized = []
    quote = {
        "price": 0.0,
        "original_price": 10.0,
        "invite_discount_percent": 0.0,
        "invite_discount_amount": 0.0,
        "payment_discount_percent": 0.0,
        "payment_discount_amount": 0.0,
        "discount_percent": 0.0,
        "discount_amount": 0.0,
        "discounted_total": 10.0,
        "account_credit_reserved": 10.0,
        "account_credit_reservation_id": "credit-quote",
        "incentive_reservation_id": "credit-quote",
        "collected_amount": 0.0,
        "referral_reward_base": 0.0,
        "fully_credit_funded": True,
    }

    class Client:
        server_id = "s1"
        server_name = "Primary"

        def get_user_uri(self, _username):
            return {"normal_sub": "https://sub.example/credit-user", "ipv4": ""}

    module.load_plans = lambda: {
        "40": {"price": 10.0, "days": 30, "unlimited": False}
    }
    module._reserve_checkout_incentives = lambda *_args, **_kwargs: dict(quote)
    module.APIClient = Client
    module.create_sale_user_with_note = (
        lambda *_args, **_kwargs: ("credit-user", True, Client())
    )
    module.add_payment_record = lambda payment_id, record: store.update(
        {payment_id: dict(record)}
    )
    module.get_payment_record = lambda payment_id: dict(store[payment_id])

    def complete(payment_id, fields):
        store[payment_id].update(fields)
        store[payment_id]["status"] = "completed"
        return True

    module.complete_payment_record = complete
    module._finalize_checkout_incentives = (
        lambda payment_id, record: finalized.append((payment_id, dict(record))) or {}
    )

    with patch.object(module.uuid, "uuid4", return_value=types.SimpleNamespace(hex="credit-quote")):
        module.handle_card_to_card_payment(make_call("payment_method:card_to_card:40"), "40")

    payment = store["credit-credit-quote"]
    assert payment["status"] == "completed"
    assert payment["payment_method"] == "Account Credit"
    assert payment["account_credit_reserved"] == 10.0
    assert finalized[0][1]["incentive_reservation_id"] == "credit-quote"
    assert 1988 not in module.user_data
    assert module._CARD_CHECKOUT_FALLBACK == {}
    assert len(bot.sent_photos) == 1
