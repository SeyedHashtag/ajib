import sys
import pytest
import datetime
import os
import types
from unittest.mock import patch
from pathlib import Path

from test_crypto_payment_discount import DummyBot, load_purchase_plan, make_call


@pytest.fixture(autouse=True)
def isolate_purchase_modules():
    saved = {key: value for key, value in sys.modules.items()
             if key == 'utils' or key.startswith('utils.') or key in {'telebot', 'qrcode', 'dotenv'}}
    yield
    for key in list(sys.modules):
        if key == 'utils' or key.startswith('utils.') or key in {'telebot', 'qrcode', 'dotenv'}:
            sys.modules.pop(key)
    sys.modules.update(saved)


def test_recommendation_prefers_stored_customer_plan_and_deduplicates_legacy_flags():
    module = load_purchase_plan(DummyBot(), [])
    plans = {
        "10": {"price": 2, "days": 30, "recommended": True},
        "20": {"price": 3, "days": 30, "recommended": True},
        "40": {"price": 4, "days": 30},
        "5": {"price": 1, "days": 30, "target": "reseller", "recommended": True},
    }

    with patch.dict(os.environ, {"AJIB_RECOMMENDED_PLAN_ID": "40"}):
        assert module.select_recommended_plan_id(plans) == "10"


def test_recommendation_uses_valid_environment_fallback_without_automatic_default():
    module = load_purchase_plan(DummyBot(), [])
    plans = {
        "10": {"price": 2, "days": 30},
        "20": {"price": 3, "days": 30},
        "40": {"price": 6, "days": 30},
        "100": {"price": 1, "days": 30, "target": "reseller"},
    }

    with patch.dict(os.environ, {"AJIB_RECOMMENDED_PLAN_ID": "40"}):
        assert module.select_recommended_plan_id(plans) == "40"
    with patch.dict(os.environ, {"AJIB_RECOMMENDED_PLAN_ID": "100"}):
        assert module.select_recommended_plan_id(plans) is None
    with patch.dict(os.environ, {"AJIB_RECOMMENDED_PLAN_ID": ""}):
        assert module.select_recommended_plan_id(plans) is None


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
        "plan_price_usd_only": "${usd}",
        "quick_pick_recommended": "Recommended",
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
    assert buttons[0].args[0].startswith("10 GB")
    assert "Recommended" in buttons[1].args[0]
    assert buttons[2].args[0].startswith("40 GB")
    assert buttons[3].args[0].startswith("60 GB")
    assert sum("Recommended" in button.args[0] for button in buttons) == 1
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


def test_persian_price_pair_and_totals_are_usd_first_and_russian_is_crypto_only():
    module = load_purchase_plan(DummyBot(), [])
    messages = {
        "plan_price_pair_usd_first": "${usd} / {toman} toman",
        "plan_price_usd_only": "${usd}",
        "plan_payment_totals_usd_first": "base=${original_usd};crypto=${crypto_total};card={card_total}",
        "plan_payment_totals_crypto_only": "base=${original_usd};crypto=${crypto_total}",
        "renewal_payment_totals_crypto_only": "base=${original_usd};renew-crypto=${crypto_total}",
    }
    module.get_message_text = lambda _language, key: messages[key]

    assert module._plan_price_pair("fa", 10, 60_000) == "$10.00 / 600000 toman"
    assert module._plan_price_pair("en", 10, 60_000) == "$10.00 / 600000 toman"
    assert module.build_plan_payment_totals("fa", "40", 10, 60_000).startswith("base=$10.00")
    assert "crypto=$9.50" in module.build_plan_payment_totals("fa", "40", 10, 60_000)
    assert module._plan_price_pair("ru", 10) == "$10.00"
    assert module.build_plan_payment_totals("ru", "40", 10, None) == "base=$10.00;crypto=$9.50"
    assert module.build_plan_payment_totals(
        "tk",
        "40",
        10,
        None,
        renewal_discount_percent=10,
        discount_cap_percent=15,
    ) == "base=$10.00;renew-crypto=$8.50"


def test_russian_catalog_and_purchase_skip_exchange_rate_and_card_method():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module.get_user_language = lambda _user_id: "ru"
    module.get_exchange_rate = lambda: (_ for _ in ()).throw(AssertionError("rate requested"))
    module.load_plans = lambda: {
        "40": {"price": 10, "days": 30, "recommended": True},
    }
    original_get_message = module.get_message_text
    module.get_message_text = lambda language, key: {
        "all_plans_title": "progress\nquality",
        "customer_plan_button": "{label}{plan_gb} GB · {price_pair} · {days}d",
        "plan_price_usd_only": "${usd}",
        "quick_pick_recommended": "Recommended",
        "plan_payment_totals_crypto_only": "base=${original_usd};crypto=${crypto_total}",
    }.get(key, original_get_message(language, key))

    with patch.dict(os.environ, {"CRYPTO_MERCHANT_ID": "merchant", "CRYPTO_API_KEY": "key"}):
        module.show_plans(555, 1988)
        module.handle_purchase_selection(make_call("purchase:40"))

    catalog_button = bot.sent_messages[0][1]["reply_markup"].buttons[0]
    assert "$10.00" in catalog_button.args[0]
    assert "toman" not in catalog_button.args[0].lower()
    purchase_text = bot.edited_messages[0][0][0]
    callbacks = [
        button.kwargs.get("callback_data")
        for button in bot.edited_messages[0][1]["reply_markup"].buttons
    ]
    assert "base=$10.00;crypto=$9.50" in purchase_text
    assert all("card_to_card" not in callback for callback in callbacks if callback)


def test_russian_card_callbacks_fail_before_purchase_or_renewal_checkout_work():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    module.get_user_language = lambda _user_id: "ru"
    module.get_exchange_rate = lambda: (_ for _ in ()).throw(AssertionError("rate requested"))
    module._reserve_checkout_incentives = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("discount reserved")
    )
    module._resolve_customer_renewal_offer_for_call = lambda *_args: (_ for _ in ()).throw(
        AssertionError("renewal token resolved")
    )

    module.handle_payment_method_selection(make_call("payment_method:card_to_card:40"))
    module.handle_customer_renewal_payment_method(
        make_call("renew_payment_method:card_to_card:renew-token")
    )

    assert len(bot.callback_answers) == 2
    assert all(answer[1]["text"] == "no_payment_methods" for answer in bot.callback_answers)
    assert all(answer[1]["show_alert"] is True for answer in bot.callback_answers)


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


def test_direct_renewal_shows_ten_percent_card_and_fifteen_percent_crypto_totals():
    module = load_purchase_plan(DummyBot(), [])
    messages = {
        "renewal_payment_totals_usd_first": (
            "base=${original_usd};card={card_percent}%:{card_total};"
            "crypto={crypto_percent}%:${crypto_total}"
        ),
    }
    module.get_message_text = lambda _language, key: messages[key]

    totals = module.build_plan_payment_totals(
        "en",
        "40",
        12,
        60_000,
        renewal_discount_percent=10,
        discount_cap_percent=15,
    )

    assert totals == "base=$12.00;card=10%:648000;crypto=15%:$10.20"


def test_direct_renewal_opens_customer_plan_picker_and_carries_target_plan():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    plans = {
        "5": {"price": 12, "days": 30, "target": "customer"},
        "10": {"price": 20, "days": 60, "target": "both"},
        "20": {"price": 30, "days": 90, "target": "reseller"},
    }
    module.load_plans = lambda: plans
    module._resolve_customer_renewal_offer_for_call = lambda *_args, **_kwargs: {
        "eligible": True, "username": "alice", "token": "renew-token",
    }
    original_message = module.get_message_text
    module.get_message_text = lambda language, key: {
        "renewal_choose_plan": "Choose for {username}",
        "renewal_plan_choice": "{plan_gb}GB/{days}d/${price}",
    }.get(key, original_message(language, key))
    renewal_stub = types.ModuleType("utils.renewal")
    renewal_stub.eligible_renewal_plans = lambda catalog, source: [
        (plan_id, plan)
        for plan_id, plan in catalog.items()
        if plan.get("target", "both") != "reseller"
    ]
    with patch.dict("sys.modules", {"utils.renewal": renewal_stub}):
        module.handle_customer_renewal_start(make_call("renew_plan:renew-token"))

    buttons = bot.edited_messages[0][1]["reply_markup"].buttons
    callbacks = [button.kwargs.get("callback_data") for button in buttons]
    assert callbacks[:2] == [
        "renew_plan_choice:renew-token:5",
        "renew_plan_choice:renew-token:10",
    ]
    assert all(":20" not in callback for callback in callbacks if callback)


def test_direct_renewal_duplicate_tap_is_acknowledged_without_second_lookup():
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    lookups = []
    module._resolve_customer_renewal_offer_for_call = (
        lambda *_args, **_kwargs: lookups.append(True)
    )
    module.RENEWAL_CALLBACK_INFLIGHT.add(("1988", "renew-token"))

    module.handle_customer_renewal_start(make_call("renew_plan:renew-token"))

    assert lookups == []
    assert len(bot.callback_answers) == 1
    assert bot.callback_answers[0][1].get("text")


@pytest.mark.parametrize("eligible", [True, False])
def test_renewal_from_config_photo_sends_text_without_editing_connection_caption(eligible):
    bot = DummyBot()
    module = load_purchase_plan(bot, [])
    call = make_call("renew_plan:renew-token")
    call.message.photo = [types.SimpleNamespace(file_id="qr-photo")]
    call.message.text = None
    call.message.caption = "Connection details and subscription link"
    module._resolve_customer_renewal_offer_for_call = lambda *_args, **_kwargs: {
        "eligible": eligible, "username": "alice", "reason": "renewal_already_reserved",
    }
    original_message = module.get_message_text
    module.get_message_text = lambda language, key: {
        "renewal_choose_plan": "Choose for {username}",
        "renewal_plan_choice": "{plan_gb}GB/{days}d/${price}",
        "renewal_unavailable": "Unavailable: {reason}",
    }.get(key, original_message(language, key))
    renewal_stub = types.ModuleType("utils.renewal")
    renewal_stub.eligible_renewal_plans = lambda *_args: [("100", {"days": 60, "price": 3})]

    def reject_photo_text_edit(*_args, **_kwargs):
        raise RuntimeError("Bad Request: there is no text in the message to edit")

    bot.edit_message_text = reject_photo_text_edit
    with patch.dict("sys.modules", {"utils.renewal": renewal_stub}):
        module.handle_customer_renewal_start(call)

    assert len(bot.sent_messages) == 1
    _, kwargs = bot.sent_messages[0]
    assert kwargs["chat_id"] == call.message.chat.id
    if eligible:
        assert kwargs["text"] == "Choose for alice"
        assert kwargs["reply_markup"].buttons[0].kwargs["callback_data"] == (
            "renew_plan_choice:renew-token:100"
        )
    else:
        assert "Unavailable:" in kwargs["text"]
    assert bot.edited_captions == []
    assert bot.deleted_messages == []
    assert call.message.caption == "Connection details and subscription link"
    assert module.RENEWAL_CALLBACK_INFLIGHT == set()


def test_direct_renewal_crypto_quote_and_copy_show_both_discount_components():
    module = load_purchase_plan(DummyBot(), [])
    messages = {
        "renewal_crypto_discount_button": "renewal crypto {percent}% total",
        "renewal_crypto_discount_summary": (
            "renewal={renewal_percent}%:-${renewal_discount_amount};"
            "crypto={crypto_percent}%:-${crypto_discount_amount};"
            "total={total_percent}%:-${total_discount_amount};"
            "base=${original_price};final=${discounted_price}"
        ),
    }
    module.get_message_text = lambda _language, key: messages[key]

    quote = module._reserve_checkout_incentives(
        1988,
        "renewal-crypto",
        12,
        "crypto",
        renewal_discount_percent=10,
        discount_cap_percent=15,
        allow_invite_discount=False,
        allow_account_credit=False,
    )
    display = module.build_crypto_discount_display("en", quote)

    assert quote["renewal_discount_amount"] == 1.2
    assert quote["crypto_discount_amount"] == 0.6
    assert quote["discount_amount"] == 1.8
    assert quote["discounted_total"] == 10.2
    assert display["button_text"] == "renewal crypto 15% total"
    assert display["summary"] == (
        "renewal=10%:-$1.20;crypto=5%:-$0.60;total=15%:-$1.80;"
        "base=$12.00;final=$10.20"
    )


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
    assert record["checkout_reminded_at"] == "2026-08-05T12:00:00.000000Z"


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
        == "2026-08-05T12:00:00.000000Z"
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


@pytest.mark.parametrize('language', ['en', 'fa', 'ru', 'tk'])
def test_main_selectors_use_usd_and_access_labels_without_exchange_lookup(language):
    module = load_purchase_plan(DummyBot(), [])
    from utils.reseller_experience import TEXT
    import importlib.util
    spec = importlib.util.spec_from_file_location('selector_translations',
        Path(module.__file__).with_name('translations.py'))
    translations = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(translations)
    module.get_message_text = translations.get_message_text
    module.get_user_language = lambda _: language
    module.load_plans = lambda: {'5': {'price': 3, 'days': 30},
                                '10': {'price': 5, 'days': 30, 'unlimited': True}}
    module.get_exchange_rate = lambda: (_ for _ in ()).throw(AssertionError('selector fetched currency'))
    module.show_plans(555, 1988)
    buttons = module.bot.sent_messages[-1][1]['reply_markup'].buttons
    assert '$3' in buttons[0].args[0] and '$5' in buttons[1].args[0]
    assert TEXT[language]['single'] in buttons[0].args[0]
    assert TEXT[language]['many'] in buttons[1].args[0]
    assert not any(word in button.args[0] for button in buttons for word in ('Toman', 'تومان', 'томан'))


@pytest.mark.parametrize('method', ['card', 'crypto', 'credit'])
def test_three_dollar_wholesale_checkout_uses_existing_payment_routes(method, monkeypatch):
    from test_crypto_payment_discount import load_reseller_handlers, FakeCryptoPayment
    bot, records = DummyBot(), []
    purchase = load_purchase_plan(bot, records)
    handlers = load_reseller_handlers(purchase)
    handlers.get_reseller_data = lambda _: {'status': 'approved', 'debt': 0}
    handlers.get_account_credit = lambda _: {'available': 3}
    handlers.get_wholesale_balance = lambda _: {'available': 0, 'reserved': 0}
    handlers.get_card_number_for_receipt_type = lambda _: '1234'
    handlers.get_exchange_rate = lambda: 100000
    monkeypatch.setenv('CRYPTO_MERCHANT_ID', 'test')
    monkeypatch.setenv('CRYPTO_API_KEY', 'test')
    handlers.handle_reseller_wholesale_balance(make_call('reseller:wholesale'))
    buttons = bot.edited_messages[-1][1]['reply_markup'].buttons
    assert any(b.kwargs.get('callback_data') == 'reseller:wholesale_fund:3.00' for b in buttons)
    handlers.handle_reseller_wholesale_fund(make_call('reseller:wholesale_fund:3.00'))
    buttons = bot.edited_messages[-1][1]['reply_markup'].buttons
    assert all(any(b.kwargs.get('callback_data') == f'reseller:wholesale_pay:{m}:3.00' for b in buttons)
               for m in ('card', 'crypto', 'credit'))
    transfers = []
    handlers.transfer_purchase_credit_to_wholesale = lambda *args, **kwargs: (
        transfers.append(args) or ({'available': 3}, True)
    )
    handlers.handle_reseller_wholesale_payment(make_call(f'reseller:wholesale_pay:{method}:3.00'))
    if method == 'card':
        assert handlers.user_data[1988]['wholesale_topup_amount'] == 3
        assert handlers.user_data[1988]['converted_amount'] == 300000
    elif method == 'crypto':
        assert FakeCryptoPayment.calls[-1]['amount'] == 2.85
        assert records[-1][1]['wholesale_topup_amount'] == 3
        assert records[-1][1]['price'] == 2.85
    else:
        assert transfers[-1][0:2] == (1988, 3)
    # An outstanding debt still prevents every funding route.
    handlers.get_reseller_data = lambda _: {'status': 'approved', 'debt': 1}
    before = (len(transfers), len(FakeCryptoPayment.calls), len(bot.edited_messages))
    handlers.handle_reseller_wholesale_payment(make_call(f'reseller:wholesale_pay:{method}:3.00'))
    assert before == (len(transfers), len(FakeCryptoPayment.calls), len(bot.edited_messages))



def test_prepaid_checkout_does_not_project_an_increase_in_debt():
    from test_crypto_payment_discount import load_reseller_handlers
    handlers = load_reseller_handlers(load_purchase_plan(DummyBot(), []))
    handlers.get_message_text = lambda language, key: (
        '{current_debt}/{projected_debt}' if key == 'reseller_purchase_details' else '')
    quote = {'price': 3, 'list_price': 4, 'level': 1, 'discount_percent': 20}
    details = handlers._build_reseller_purchase_details('en', '5', 30, quote, 2, 5, funding_mode='prepaid')
    assert details.startswith('2.00/2.00')
