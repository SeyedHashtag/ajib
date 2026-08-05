import datetime
import importlib.util
import sys
import types
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "scripts"
    / "telegrambot"
    / "utils"
    / "common.py"
)


class DummyMarkup:
    def __init__(self, *args, **kwargs):
        self.buttons = []

    def add(self, *buttons, **kwargs):
        self.buttons.extend(buttons)


class DummyButton:
    def __init__(self, text, **kwargs):
        self.text = text
        self.callback_data = kwargs.get("callback_data")


def load_common(records=None, trial=None):
    for name in list(sys.modules):
        if name == "utils" or name.startswith("utils."):
            sys.modules.pop(name, None)

    telebot = types.ModuleType("telebot")
    telebot.types = types.SimpleNamespace(
        ReplyKeyboardMarkup=DummyMarkup,
        InlineKeyboardMarkup=DummyMarkup,
        InlineKeyboardButton=DummyButton,
    )
    sys.modules["telebot"] = telebot

    utils = types.ModuleType("utils")
    utils.__path__ = []
    sys.modules["utils"] = utils

    payments = types.ModuleType("utils.payment_records")
    payments.get_user_payments = lambda _user_id: records or {}
    sys.modules[payments.__name__] = payments

    tests = types.ModuleType("utils.test_config")
    tests.get_test_config_journey = lambda _user_id, now=None: trial
    sys.modules[tests.__name__] = tests

    renewal = types.ModuleType("utils.renewal")
    renewal.customer_renewal_token = (
        lambda user_id, payment_id, username, server_id:
        f"{user_id}:{payment_id}:{username}:{server_id}"
    )
    sys.modules[renewal.__name__] = renewal

    translations = types.ModuleType("utils.translations")
    translations.BUTTON_TRANSLATIONS = {"en": {}}
    translations.DEFAULT_LANGUAGE = "en"
    translations.get_button_text = lambda _language, key: key
    translations.get_message_text = lambda _language, key: key + " {remaining_days} {traffic_gb}"
    sys.modules[translations.__name__] = translations

    spec = importlib.util.spec_from_file_location("customer_common_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_customer_welcome_states_progress_from_new_to_activated_trial():
    common = load_common()
    assert common.get_customer_journey_state(123)["state"] == "new"

    unused = load_common(trial={"connected_at": None, "remaining_days": 29, "traffic_gb": 1})
    assert unused.get_customer_journey_state(123)["state"] == "unused_trial"

    activated = load_common(trial={"connected_at": "2026-08-05", "remaining_days": 29, "traffic_gb": 1})
    assert activated.get_customer_journey_state(123)["state"] == "activated_trial"
    _text, markup = activated.build_customer_welcome(123, "en")
    assert [button.callback_data for button in markup.buttons] == [
        "welcome:plans",
        "welcome:configs",
    ]


def test_paid_and_expired_states_offer_direct_renewal_token():
    current = datetime.datetime(2026, 8, 5, 12, 0, 0)
    active_record = {
        "p1": {
            "status": "completed",
            "plan_gb": "40",
            "days": 30,
            "completed_at": "2026-08-01 12:00:00",
            "username": "s123",
            "server_id": "main",
        }
    }
    active = load_common(records=active_record)
    state = active.get_customer_journey_state(123, now=current)
    assert state["state"] == "paid"
    assert state["renewal_token"] == "123:p1:s123:main"

    expired_record = {"p1": {**active_record["p1"], "completed_at": "2026-06-01 12:00:00"}}
    expired = load_common(records=expired_record)
    state = expired.get_customer_journey_state(123, now=current)
    assert state["state"] == "expired"
    _text, markup = expired.build_customer_welcome(123, "en")
    assert "renew_plan:123:p1:s123:main" in [button.callback_data for button in markup.buttons]


def test_admin_menu_contains_private_growth_funnel_button():
    common = load_common()

    assert common.GROWTH_FUNNEL_BUTTON_TEXT == "📈 Growth Funnel"
    assert common.GROWTH_FUNNEL_BUTTON_TEXT in common.ADMIN_MAIN_MENU_BUTTONS
    assert any(
        common.GROWTH_FUNNEL_BUTTON_TEXT in row
        for row in common.ADMIN_GROUP_MENU_ROWS["reports"]
    )


def test_main_growth_hook_promotes_referral_campaign_to_event_field():
    common = load_common()
    calls = []
    growth = types.ModuleType("utils.growth_events")
    growth.EVENT_ONBOARDING_VIEWED = "onboarding_viewed"
    growth.EVENT_TRIAL_STARTED = "trial_started"
    growth.EVENT_TRIAL_ACTIVATED = "trial_activated"
    growth.EVENT_PLAN_VIEWED = "plan_viewed"
    growth.EVENT_PLAN_SELECTED = "plan_selected"
    growth.EVENT_REFERRAL_ATTRIBUTED = "referral_attributed"
    growth.SURFACE_MAIN = "main"
    growth.record_growth_event = lambda event_type, **fields: calls.append(
        (event_type, fields)
    )
    sys.modules[growth.__name__] = growth
    setattr(sys.modules["utils"], "growth_events", growth)

    assert common.record_main_growth_event(
        "referral_attributed",
        123,
        language="fa",
        referral_campaign="main_invite",
        deduplication_key="main:referral_attributed:123",
        referrer_id="456",
    )

    event_type, fields = calls[0]
    assert event_type == "referral_attributed"
    assert fields["surface"] == "main"
    assert fields["hosted_tenant_id"] is None
    assert fields["referral_campaign"] == "main_invite"
    assert fields["deduplication_key"] == "main:referral_attributed:123"
    assert fields["metadata"] == {"referrer_id": "456"}
