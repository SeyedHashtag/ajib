#!/usr/bin/env python3
"""Isolated polling worker for one reseller-owned Telegram bot."""

import io
import logging
import math
import os
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote as urlquote

os.environ["AJIB_BOT_ROLE"] = "hosted"

import qrcode
import telebot
from dotenv import load_dotenv
from telebot import types

BOT_DIR = os.getenv("AJIB_BOT_DIR", "/etc/ajib/core/scripts/telegrambot")
os.environ.setdefault("AJIB_BOT_DIR", BOT_DIR)
load_dotenv(os.path.join(BOT_DIR, ".env"))

if __name__ == "__main__":
    from migrate_state import bootstrap_storage

    bootstrap_storage(BOT_DIR)

from utils.api_client import MultiServerAPI
from utils.account_state import (
    EntitlementState,
    PanelState,
    bot_timezone,
    inspect_account,
    resolve_service_cycle,
)
from utils import database
from utils.atomic_store import locked_json, read_json
from utils.currency_format import format_toman_amount, format_usd_amount
from utils.exchange_rate import get_exchange_rate
from utils.hosted_bots import (
    add_referral_liability, calculate_quote, consume_credit,
    consume_renewal_credit, credit_crypto_sale, get_ledger, get_settings, get_setup_status, get_token,
    INVITED_BUYER_DISCOUNT_PERCENT,
    localized_storefront_text,
    mark_setup_step,
    release_credit, release_stale_credit_reservations, request_earnings_withdrawal, reserve_credit,
    set_bot_runtime_status, settle_referral_liability, tenant_file, transfer_earnings_to_debt,
    update_settings,
)
from utils.hosted_translations import HOSTED_TRANSLATIONS, hosted_text
from utils.hosted_stats import build_hosted_stats
from utils.growth_features import (
    BUYER_DISCOUNTS,
    REMINDERS,
    is_growth_feature_enabled,
)
from utils.download_guidance import (
    render_download_callback,
    send_download_prompt,
    send_download_prompt_safely,
)
from utils.payments import CryptoPayment
from utils.reseller import (
    calculate_reseller_wholesale_price, can_reseller_add_debt, get_reseller_data,
    get_reseller_level_summary, get_reseller_total_paid, get_reseller_trust_limit,
    record_funded_reseller_config,
    record_funded_reseller_renewal,
    reserve_reseller_renewal,
    sync_reseller_renewal_reservation,
)
from utils.reseller_level_ui import (
    build_reseller_level_compact,
    present_pending_reseller_level,
)
from utils.translations import BUTTON_TRANSLATIONS, LANGUAGES, get_button_text, get_message_text
from utils.username_utils import (
    allocate_username,
    build_user_note,
    load_recorded_usernames,
    RecordedUsernameLoadError,
)


OWNER_ID = int(os.environ["AJIB_HOSTED_RESELLER_ID"])
TOKEN = get_token(OWNER_ID)
if not TOKEN:
    raise SystemExit("Hosted bot token is missing")
BOT_USERNAME = os.getenv("AJIB_HOSTED_BOT_USERNAME", "").lstrip("@")
PLANS_FILE = os.path.join(BOT_DIR, "plans.json")
GLOBAL_TEST_FILE = os.path.join(BOT_DIR, "test_configs.json")
INPUT_STATE = {}
INPUT_STATE_LOCK = threading.RLock()


def _positive_int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


PROCESSING_LEASE_SECONDS = _positive_int_env("AJIB_HOSTED_PAYMENT_LEASE_SECONDS", 900, 60)
CHECKOUT_CREATION_LEASE_SECONDS = _positive_int_env(
    "AJIB_HOSTED_CHECKOUT_CREATION_LEASE_SECONDS", 300, 60
)
TEST_CREATION_LEASE_SECONDS = _positive_int_env("AJIB_HOSTED_TEST_CREATION_LEASE_SECONDS", 900, 60)
RENEWAL_TOKEN_TTL_SECONDS = _positive_int_env("AJIB_HOSTED_RENEWAL_TOKEN_TTL_SECONDS", 1800, 60)
CREDIT_RESERVATION_MAX_AGE_SECONDS = _positive_int_env(
    "AJIB_HOSTED_CREDIT_RESERVATION_MAX_AGE_SECONDS", 86400, 300
)
MAX_RECEIPT_BYTES = _positive_int_env("AJIB_HOSTED_MAX_RECEIPT_BYTES", 10 * 1024 * 1024, 1024)
INPUT_STATE_TTL_SECONDS = _positive_int_env("AJIB_HOSTED_INPUT_STATE_TTL_SECONDS", 3600, 60)
MAX_INPUT_STATES = _positive_int_env("AJIB_HOSTED_MAX_INPUT_STATES", 5000, 100)
TELEGRAM_SAFE_TEXT_LIMIT = 3800
OWNER_STATS_SEND_HOUR = 0
OWNER_STATS_SEND_MINUTE = 5
OWNER_STATS_CLAIM_LEASE_SECONDS = 600
OWNER_STATS_MONITOR_INTERVAL_SECONDS = 60
BUYER_DISCOUNTS_ENABLED = is_growth_feature_enabled(BUYER_DISCOUNTS)
REMINDERS_ENABLED = is_growth_feature_enabled(REMINDERS)

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=4)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _financial_amount(value, field):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Invalid hosted payment {field}") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Invalid hosted payment {field}")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _settlement_financials(record):
    """Validate immutable hosted checkout economics before side effects."""
    if not isinstance(record, dict):
        raise ValueError("Invalid hosted payment record")

    payment_method = str(record.get("payment_method") or "").strip().lower()
    if payment_method == "account_credit":
        raise ValueError("Main-account credit is not valid for hosted-store checkout")
    for field in (
        "account_credit_reserved",
        "account_credit_consumed",
        "account_credit_applied",
    ):
        if field in record and _financial_amount(record.get(field, 0), field) > 0:
            raise ValueError("Main-account credit cannot reduce hosted-store proceeds")

    collected_value = record.get("collected_amount")
    if collected_value is None and payment_method == "crypto":
        collected_value = record.get("crypto_collected")
    if collected_value is None:
        collected_value = record.get("retail_price")
    collected = _financial_amount(collected_value, "collected amount")
    wholesale = _financial_amount(record.get("wholesale_price"), "wholesale price")
    margin = (collected - wholesale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if margin < 0:
        raise ValueError("Hosted payment route falls below wholesale cost")

    reward = _financial_amount(record.get("referral_reward", 0), "referral reward")
    if reward > margin:
        raise ValueError("Hosted referral reward exceeds positive post-discount margin")

    if record.get("reward_calculation_base") is not None:
        reward_base = _financial_amount(
            record.get("reward_calculation_base"),
            "reward calculation base",
        )
        if reward_base != margin:
            raise ValueError("Hosted referral reward base is not the post-discount margin")

    if record.get("margin") is not None:
        recorded_margin = _financial_amount(record.get("margin"), "margin")
        if recorded_margin != margin:
            raise ValueError("Hosted payment margin does not match collected amount")

    component_fields = ("invite_discount_percent", "crypto_discount_percent")
    components = Decimal("0")
    for field in component_fields:
        if record.get(field) is not None:
            components += _financial_amount(record.get(field), field)
    if components > Decimal("10.00"):
        raise ValueError("Hosted customer discount components exceed the 10% cap")
    if record.get("total_discount_percent") is not None:
        total_discount = _financial_amount(
            record.get("total_discount_percent"),
            "total discount percent",
        )
        if total_discount > Decimal("10.00"):
            raise ValueError("Hosted customer discount exceeds the 10% cap")
        if any(record.get(field) is not None for field in component_fields) and total_discount != components:
            raise ValueError("Hosted customer discount components do not match the capped total")

    if record.get("original_price") is not None and record.get("total_discount_amount") is not None:
        original_price = _financial_amount(record.get("original_price"), "original price")
        total_discount_amount = _financial_amount(
            record.get("total_discount_amount"),
            "total discount amount",
        )
        if total_discount_amount > original_price or original_price - total_discount_amount != collected:
            raise ValueError("Hosted collected amount does not match the recorded discount")
        if (
            record.get("invite_discount_amount") is not None
            or record.get("crypto_discount_amount") is not None
        ):
            invite_amount = _financial_amount(
                record.get("invite_discount_amount", 0),
                "invite discount amount",
            )
            crypto_amount = _financial_amount(
                record.get("crypto_discount_amount", 0),
                "crypto discount amount",
            )
            if invite_amount + crypto_amount != total_discount_amount:
                raise ValueError("Hosted discount amounts do not match the collected total")

    return {
        "collected_amount": float(collected),
        "wholesale_price": float(wholesale),
        "margin": float(margin),
        "reward_calculation_base": float(margin),
        "referral_reward": float(reward),
    }


def _escape_markdown(value):
    text = str(value or "")
    for character in ("\\", "`", "*", "_", "[", "]"):
        text = text.replace(character, f"\\{character}")
    return text


def _set_input_state(user_id, state):
    now = time.time()
    with INPUT_STATE_LOCK:
        for key, item in list(INPUT_STATE.items()):
            if not isinstance(item, dict) or float(item.get("expires_at", 0) or 0) <= now:
                INPUT_STATE.pop(key, None)
        if len(INPUT_STATE) >= MAX_INPUT_STATES:
            oldest = min(INPUT_STATE, key=lambda key: float(INPUT_STATE[key].get("expires_at", 0) or 0))
            INPUT_STATE.pop(oldest, None)
        INPUT_STATE[user_id] = {**dict(state), "expires_at": now + INPUT_STATE_TTL_SECONDS}


def _get_input_state(user_id):
    with INPUT_STATE_LOCK:
        state = INPUT_STATE.get(user_id)
        if not isinstance(state, dict) or float(state.get("expires_at", 0) or 0) <= time.time():
            INPUT_STATE.pop(user_id, None)
            return None
        return dict(state)


def _pop_input_state(user_id):
    with INPUT_STATE_LOCK:
        state = INPUT_STATE.pop(user_id, None)
        return dict(state) if isinstance(state, dict) else None


def _clear_input_state(user_id, **expected):
    """Clear a prompt only when it still belongs to the completed action."""
    with INPUT_STATE_LOCK:
        state = INPUT_STATE.get(user_id)
        if not isinstance(state, dict):
            return False
        if any(state.get(key) != value for key, value in expected.items()):
            return False
        INPUT_STATE.pop(user_id, None)
        return True


def _reseller(active_only=False):
    data = get_reseller_data(OWNER_ID) or {}
    allowed = {"approved"} if active_only else {"approved", "suspended"}
    return data if data.get("status") in allowed else None


def _load_plans():
    plans = read_json(PLANS_FILE, {})
    return plans if isinstance(plans, dict) else {}


def _sellable_plans():
    settings = get_settings(OWNER_ID)
    enabled = {str(value) for value in settings.get("enabled_plan_ids", [])}
    result = {}
    for plan_id, plan in _load_plans().items():
        plan_key = str(plan_id).strip()
        if (
            not isinstance(plan, dict)
            or not plan_key.isdigit()
            or len(plan_key) > 12
            or plan.get("target", "both") == "customer"
        ):
            continue
        try:
            price = float(plan["price"])
            days = int(plan.get("days", 30))
            gigabytes = int(plan.get("gb", plan_key))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0 or days <= 0 or days > 3650 or gigabytes <= 0:
            continue
        if settings.get("plan_selection_configured") and plan_key not in enabled:
            continue
        result[plan_key] = {**plan, "price": price, "days": days, "gb": gigabytes}
    return result


def _reseller_plan_pricing(plan, reseller=None):
    reseller = reseller if reseller is not None else (get_reseller_data(OWNER_ID) or {})
    summary = get_reseller_level_summary(reseller)
    list_price = float(plan["price"])
    return {
        "list_price": list_price,
        "wholesale_price": calculate_reseller_wholesale_price(list_price, reseller),
        "reseller_level": summary["level"],
        "discount_percent": summary["discount_percent"],
    }


def _hosted_plan_quote(
    plan,
    settings,
    referral_margin_percent=0,
    referred=False,
    buyer_discount_percent=0,
):
    pricing = _reseller_plan_pricing(plan)
    quote = calculate_quote(
        pricing["wholesale_price"],
        settings["markup_percent"],
        referral_margin_percent,
        referred,
        retail_base=pricing["list_price"],
        buyer_discount_percent=buyer_discount_percent,
    )
    return {**quote, **pricing}


def _languages():
    return read_json(tenant_file(OWNER_ID, "languages.json"), {})


def _language(user_id):
    return _languages().get(str(user_id), "en")


def _has_language(user_id):
    return str(user_id) in _languages()


def _telegram_language(user):
    language = str(getattr(user, "language_code", "") or "").split("-", 1)[0].lower()
    return language if language in LANGUAGES else None


def _set_language(user_id, value):
    with locked_json(tenant_file(OWNER_ID, "languages.json"), {}) as languages:
        languages[str(user_id)] = value


def _button(user_id, key, fallback=None):
    default = BUTTON_TRANSLATIONS["en"].get(key, key) if fallback is None else fallback
    return BUTTON_TRANSLATIONS.get(_language(user_id), BUTTON_TRANSLATIONS["en"]).get(key, default)


def _message(user_id, key):
    return get_message_text(_language(user_id), key)


def _hosted_message(recipient_id, key, **values):
    return hosted_text(_language(recipient_id), key, **values)


def _storefront_setting(recipient_id, field):
    language = _language(recipient_id)
    return localized_storefront_text(
        get_settings(OWNER_ID),
        field,
        language,
        default=hosted_text(language, f"{field}_default"),
    )


def _all_button_values(key, fallback=None):
    default = BUTTON_TRANSLATIONS["en"].get(key, key) if fallback is None else fallback
    return {items.get(key, default) for items in BUTTON_TRANSLATIONS.values()}


def _all_hosted_values(key):
    return {items.get(key, key) for items in HOSTED_TRANSLATIONS.values()}


def _main_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(_button(user_id, "my_configs"), _button(user_id, "purchase_plan"))
    markup.row(_button(user_id, "downloads"), _button(user_id, "test_config"))
    markup.row(_hosted_message(user_id, "invite_and_earn_button"), _button(user_id, "support"))
    markup.row(_button(user_id, "language"))
    if user_id == OWNER_ID:
        markup.row(_hosted_message(user_id, "owner_panel"))
    return markup


def _tenant_payments():
    data = read_json(tenant_file(OWNER_ID, "payments.json"), {})
    return data if isinstance(data, dict) else {}


def _matching_reserved_checkout(payments, user_id, username, server_id=None):
    target_username = str(username or "").strip().lower()
    if not target_username:
        return None
    live_payment_statuses = {
        "creating", "waiting_receipt", "pending_approval", "pending", "processing",
        "paid_provision_failed",
    }
    live_renewal_statuses = {"reserved", "processing", "attention"}
    for existing_id, existing in (payments or {}).items():
        if not isinstance(existing, dict) or existing.get("renewal_mode") != "reserved":
            continue
        if str(existing.get("user_id")) != str(user_id):
            continue
        if str(existing.get("renew_username") or existing.get("username") or "").strip().lower() != target_username:
            continue
        existing_server = existing.get("server_id") or existing.get("renewal_server_id")
        if server_id and existing_server and str(existing_server) != str(server_id):
            continue
        if (
            existing.get("status") in live_payment_statuses
            or (
                existing.get("status") == "completed"
                and existing.get("renewal_status") in live_renewal_statuses
            )
        ):
            return str(existing_id), dict(existing)
    return None


def _save_payment(payment_id, record):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        current = payments.get(payment_id, {})
        timestamp = _now()
        current.update(record)
        if record.get("status") == "completed":
            current.setdefault("completed_at", timestamp)
        if record.get("status") and record.get("status") != "processing":
            current.pop("processing_started_at", None)
            current.pop("processing_from_status", None)
        current.setdefault("created_at", timestamp)
        current["updated_at"] = timestamp
        payments[payment_id] = current
        return dict(current)


def _claim_payment(payment_id, allowed):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        record = payments.get(payment_id)
        if not isinstance(record, dict):
            return None
        allowed_statuses = set(allowed)
        current_status = record.get("status")
        if current_status == "processing":
            started_at = _parse_time(record.get("processing_started_at"))
            stale = started_at is None or (datetime.now() - started_at).total_seconds() >= PROCESSING_LEASE_SECONDS
            original_status = record.get("processing_from_status")
            if not stale or original_status not in allowed_statuses:
                return None
        elif current_status not in allowed_statuses:
            return None
        record["status"] = "processing"
        record["processing_from_status"] = (
            record.get("processing_from_status") if current_status == "processing" else current_status
        )
        record["processing_started_at"] = _now()
        record["processing_attempts"] = int(record.get("processing_attempts", 0) or 0) + 1
        record["updated_at"] = _now()
        return dict(record)


def _start_checkout(payment_id, record):
    """Atomically create an independent checkout without replacing another order."""
    checkout_source = str(record.get("checkout_source") or "")
    live_statuses = {
        "creating", "waiting_receipt", "pending_approval", "pending", "processing",
        "paid_provision_failed",
    }
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        if payment_id in payments:
            return False, payment_id
        if record.get("renewal_mode") == "reserved":
            existing_reservation = _matching_reserved_checkout(
                payments,
                record.get("user_id"),
                record.get("renew_username") or record.get("username"),
                record.get("server_id") or record.get("renewal_server_id"),
            )
            if existing_reservation:
                return False, existing_reservation[0]
        if checkout_source:
            for existing_id, existing in payments.items():
                if (
                    isinstance(existing, dict)
                    and existing.get("checkout_source") == checkout_source
                    and existing.get("status") in live_statuses
                ):
                    return False, existing_id
        payment = dict(record)
        payment["status"] = "creating"
        payment["created_at"] = _now()
        payment["updated_at"] = _now()
        payments[payment_id] = payment
        return True, payment_id


def _receipt_checkout(user_id, reply_message_id=None, chat_id=None):
    """Route a receipt to its replied-to, active, or newest card checkout."""
    payments = _tenant_payments()
    if reply_message_id is not None:
        for candidate_id, record in payments.items():
            if (
                isinstance(record, dict)
                and str(record.get("user_id")) == str(user_id)
                and record.get("status") == "waiting_receipt"
                and str(record.get("receipt_prompt_message_id")) == str(reply_message_id)
                and str(record.get("receipt_prompt_chat_id")) == str(chat_id)
            ):
                return candidate_id

    state = _get_input_state(user_id)
    selected_id = state.get("payment_id") if state and state.get("kind") == "receipt" else None
    selected = payments.get(selected_id) if selected_id else None
    if selected_id:
        if isinstance(selected, dict) and str(selected.get("user_id")) == str(user_id):
            # Keep routing to the selected order even while another upload thread
            # has it claimed. Falling back here could attach a rapid second photo
            # to a different open checkout.
            return selected_id
        _clear_input_state(user_id, kind="receipt", payment_id=selected_id)

    candidates = [
        candidate_id
        for candidate_id, record in payments.items()
        if isinstance(record, dict)
        and str(record.get("user_id")) == str(user_id)
        and record.get("status") == "waiting_receipt"
    ]
    return candidates[-1] if candidates else None


def _claim_receipt_notification(payment_id):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        record = payments.get(payment_id)
        if (
            not isinstance(record, dict)
            or record.get("status") != "pending_approval"
            or not record.get("receipt_path")
            or record.get("owner_receipt_notified_at")
        ):
            return None
        started_at = _parse_time(record.get("owner_receipt_notification_started_at"))
        if (
            started_at is not None
            and (datetime.now() - started_at).total_seconds() < PROCESSING_LEASE_SECONDS
        ):
            return None
        record["owner_receipt_notification_started_at"] = _now()
        record["owner_receipt_notification_attempts"] = int(
            record.get("owner_receipt_notification_attempts", 0) or 0
        ) + 1
        record["updated_at"] = _now()
        return dict(record)


def _finish_receipt_notification(payment_id, error=None):
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        record = payments.get(payment_id)
        if not isinstance(record, dict):
            return False
        record.pop("owner_receipt_notification_started_at", None)
        if error is None:
            record["owner_receipt_notified_at"] = _now()
            record.pop("owner_receipt_notification_error", None)
        else:
            record["owner_receipt_notification_error"] = str(error)[:500]
        record["updated_at"] = _now()
        return True


def _notify_owner_of_receipt(payment_id):
    record = _claim_receipt_notification(payment_id)
    if not record:
        current = _tenant_payments().get(payment_id, {})
        return bool(isinstance(current, dict) and current.get("owner_receipt_notified_at"))
    try:
        caption = _hosted_message(
            OWNER_ID,
            "receipt_owner_caption",
            user_id=record["user_id"],
            plan_gb=record["plan_gb"],
            days=record["days"],
            toman_price=format_toman_amount(
                record.get("converted_amount", record["retail_price"])
            ),
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton(
                _hosted_message(OWNER_ID, "approved"),
                callback_data=f"hb:approve:{payment_id}",
            ),
            types.InlineKeyboardButton(
                _hosted_message(OWNER_ID, "rejected"),
                callback_data=f"hb:reject:{payment_id}",
            ),
        )
        with open(record["receipt_path"], "rb") as handle:
            bot.send_photo(
                OWNER_ID,
                handle,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=markup,
            )
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        _finish_receipt_notification(payment_id, detail)
        print(
            f"Hosted receipt notification failed for reseller {OWNER_ID}, "
            f"payment {payment_id}: {detail}",
            flush=True,
        )
        return False
    _finish_receipt_notification(payment_id)
    return True


def _store_receipt_photo(message, payment_id):
    temporary_path = None
    try:
        info = bot.get_file(message.photo[-1].file_id)
        content = bot.download_file(info.file_path)
        if len(content) > MAX_RECEIPT_BYTES:
            raise ValueError("receipt is too large")
        receipt_path = tenant_file(OWNER_ID, os.path.join("receipts", f"{payment_id}.jpg"))
        os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
        temporary_path = f"{receipt_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temporary_path, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, receipt_path)
        temporary_path = None
        return receipt_path
    finally:
        try:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass


def _recover_saved_receipts():
    """Recover receipts saved before the legacy owner-caption failure."""
    recovered = []
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        for payment_id, record in payments.items():
            if not isinstance(record, dict) or record.get("status") != "waiting_receipt":
                continue
            receipt_path = record.get("receipt_path")
            if not receipt_path or not os.path.isfile(receipt_path):
                continue
            record["status"] = "pending_approval"
            record.setdefault("receipt_received_at", record.get("updated_at") or _now())
            record["receipt_recovered_at"] = _now()
            record["updated_at"] = _now()
            record.pop("last_error", None)
            recovered.append((payment_id, record.get("user_id")))
    for payment_id, user_id in recovered:
        _notify_owner_of_receipt(payment_id)
        try:
            bot.send_message(user_id, _message(user_id, "receipt_submitted"))
        except Exception as error:
            print(
                f"Hosted recovered-receipt confirmation failed for reseller {OWNER_ID}, "
                f"payment {payment_id}: {type(error).__name__}: {error}",
                flush=True,
            )
    return [payment_id for payment_id, _user_id in recovered]


def _recover_stale_payment_claims():
    recovered = []
    failed_creations = []
    release_reservations = []
    with locked_json(tenant_file(OWNER_ID, "payments.json"), {}) as payments:
        for payment_id, record in payments.items():
            if not isinstance(record, dict):
                continue
            if record.get("status") == "creating":
                created_at = _parse_time(record.get("created_at"))
                if created_at is None or (datetime.now() - created_at).total_seconds() >= CHECKOUT_CREATION_LEASE_SECONDS:
                    record["status"] = "failed"
                    record["last_error"] = "Recovered an interrupted checkout creation"
                    record["recovered_at"] = _now()
                    record["updated_at"] = _now()
                    if record.get("reservation_id"):
                        release_reservations.append(str(record["reservation_id"]))
                    recovered.append(payment_id)
                    failed_creations.append((payment_id, record.get("user_id")))
                continue
            if record.get("status") != "processing":
                continue
            started_at = _parse_time(record.get("processing_started_at"))
            if started_at is not None and (datetime.now() - started_at).total_seconds() < PROCESSING_LEASE_SECONDS:
                continue
            retry_status = record.get("processing_from_status")
            if retry_status not in {"waiting_receipt", "pending_approval", "pending", "paid_provision_failed"}:
                retry_status = "paid_provision_failed" if record.get("gateway_payment_id") else "pending_approval"
            record["status"] = retry_status
            record["last_error"] = "Recovered an interrupted payment attempt"
            record["recovered_at"] = _now()
            record["updated_at"] = _now()
            record.pop("processing_started_at", None)
            record.pop("processing_from_status", None)
            recovered.append(payment_id)
    for reservation_id in release_reservations:
        release_credit(OWNER_ID, reservation_id, kind="credit_creation_recovered")
    for payment_id, user_id in failed_creations:
        _release_invite_discount(user_id, payment_id)
    return recovered


def _reconcile_credit_reservations():
    payments = _tenant_payments()
    active = {
        str(payment_id)
        for payment_id, record in payments.items()
        if isinstance(record, dict) and record.get("status") in {
            "creating", "waiting_receipt", "pending_approval", "processing"
        }
    }
    return release_stale_credit_reservations(
        OWNER_ID,
        active,
        max_age_seconds=CREDIT_RESERVATION_MAX_AGE_SECONDS,
    )


def _store_renewal_token(user_id, renewal):
    token = uuid.uuid4().hex[:12]
    now = time.time()
    with locked_json(tenant_file(OWNER_ID, "renewal_tokens.json"), {}) as tokens:
        for existing, item in list(tokens.items()):
            try:
                expires_at = float(item.get("expires_at", 0) or 0) if isinstance(item, dict) else 0
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at <= now:
                tokens.pop(existing, None)
        tokens[token] = {
            "user_id": str(user_id),
            "renewal": dict(renewal or {}),
            "expires_at": now + RENEWAL_TOKEN_TTL_SECONDS,
        }
    return token


def _consume_renewal_token(token, user_id):
    with locked_json(tenant_file(OWNER_ID, "renewal_tokens.json"), {}) as tokens:
        item = tokens.pop(str(token), None)
        if not isinstance(item, dict):
            return None
        try:
            expires_at = float(item.get("expires_at", 0) or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if str(item.get("user_id")) != str(user_id) or expires_at <= time.time():
            return None
        renewal = item.get("renewal")
        return dict(renewal) if isinstance(renewal, dict) else None


def _find_customer_configs(user_id):
    reseller = get_reseller_data(OWNER_ID) or {}
    return [
        {**item, "_config_index": index}
        for index, item in enumerate(reseller.get("configs", []))
        if isinstance(item, dict)
        and str(item.get("customer_telegram_id")) == str(user_id)
        and not item.get("removed_from_vpn")
    ]


def _hosted_service_cycle(config):
    """Resolve the exact successful tenant/reseller issuance cycle."""
    if not isinstance(config, dict):
        return None
    username = config.get("username")
    server_id = config.get("server_id")
    matching_records = []
    for record in _tenant_payments().values():
        if not isinstance(record, dict):
            continue
        record_username = record.get("renew_username") or record.get("username")
        record_server = record.get("renewal_server_id") or record.get("server_id")
        if str(record_username or "").strip().lower() != str(username or "").strip().lower():
            continue
        if str(record_server or "primary").strip().lower() != str(server_id or "primary").strip().lower():
            continue
        matching_records.append(record)
    records = matching_records if matching_records else config
    return resolve_service_cycle(
        records,
        username=username,
        server_id=server_id,
        source="hosted_customer",
    )


def _resolve_hosted_renewal_checkout(user_id, plan_id, renewal):
    from utils.renewal import find_reseller_renewal_offer, lookup_renewal_user

    reseller = get_reseller_data(OWNER_ID) or {}
    configs = reseller.get("configs", [])
    try:
        config_index = int((renewal or {}).get("config_index"))
        config = configs[config_index]
    except (IndexError, TypeError, ValueError):
        return None, "renewal_ineligible_missing"
    if (
        not isinstance(config, dict)
        or config.get("removed_from_vpn")
        or str(config.get("customer_telegram_id")) != str(user_id)
        or str(config.get("username") or "").lower() != str((renewal or {}).get("username") or "").lower()
        or str(config.get("plan_gb") or config.get("gb") or "") != str(plan_id)
    ):
        return None, "renewal_ineligible_missing"
    client, live, lookup_result = lookup_renewal_user(
        MultiServerAPI(),
        config.get("username"),
        server_id=config.get("server_id"),
    )
    offer = find_reseller_renewal_offer(
        OWNER_ID,
        config_index,
        client,
        live,
        _sellable_plans(),
        reseller_data=reseller,
        allow_reservation=True,
        lookup_result=lookup_result,
    )
    if not offer.get("eligible"):
        return None, offer.get("reason") or "renewal_ineligible_missing"
    return {
        "username": offer.get("username"),
        "server_id": offer.get("server_id"),
        "config_index": config_index,
        "renewal_mode": offer.get("renewal_mode", "immediate"),
        "renewal_baseline": offer.get("before_state"),
    }, None


def _referral_data():
    return read_json(tenant_file(OWNER_ID, "referrals.json"), {
        "referrals": {}, "codes": {}, "user_codes": {}, "stats": {}, "wallets": {},
        "pending_withdrawals": [], "payouts": [], "buyer_discount_reservations": {},
        "buyer_discount_redeemed": {},
    })


def _record_growth(event_type, user_id, **fields):
    """Best-effort hook for the shared growth-event store, when installed."""
    try:
        from utils import growth_events

        aliases = {
            "onboarding": growth_events.EVENT_ONBOARDING_VIEWED,
            "trial_activation": growth_events.EVENT_TRIAL_STARTED,
            "trial_connected": growth_events.EVENT_TRIAL_ACTIVATED,
            "plan_view": growth_events.EVENT_PLAN_VIEWED,
            "plan_selection": growth_events.EVENT_PLAN_SELECTED,
            "checkout": growth_events.EVENT_CHECKOUT_STARTED,
            "purchase_completed": growth_events.EVENT_CHECKOUT_COMPLETED,
            "renewal": growth_events.EVENT_RENEWAL_PROMPTED,
            "renewal_completed": growth_events.EVENT_RENEWAL_COMPLETED,
            "referral_attributed": growth_events.EVENT_REFERRAL_ATTRIBUTED,
            "referral_conversion": growth_events.EVENT_REFERRAL_CONVERTED,
            "hosted_first_sale": growth_events.EVENT_HOSTED_FIRST_SALE,
        }
        plan_id = fields.pop("plan", fields.pop("plan_id", None))
        deduplication_key = fields.pop("deduplication_key", None) or (
            f"hosted:{OWNER_ID}:{event_type}:{user_id}"
        )
        payment_method = fields.pop("payment_method", None)
        referral_campaign = fields.pop("referral_campaign", None)
        growth_events.record_growth_event(
            aliases.get(event_type, event_type),
            user_id=user_id,
            surface=growth_events.SURFACE_HOSTED,
            hosted_tenant_id=str(OWNER_ID),
            language=_language(user_id),
            plan_id=plan_id,
            payment_method=payment_method,
            referral_campaign=referral_campaign,
            deduplication_key=deduplication_key,
            metadata=fields or None,
        )
    except Exception:
        pass


def _journey_state(user_id):
    data = read_json(tenant_file(OWNER_ID, "customer_journey.json"), {})
    item = data.get(str(user_id), {}) if isinstance(data, dict) else {}
    return dict(item) if isinstance(item, dict) else {}


def _record_completed_growth(payment_id, record, renewed=False):
    user_id = record.get("user_id")
    common = {
        "plan": record.get("plan_gb"),
        "payment_method": record.get("payment_method"),
        "referral_campaign": "hosted_invite" if record.get("referral_attribution") else None,
    }
    _record_growth(
        "checkout_completed",
        user_id,
        **common,
        deduplication_key=f"hosted-checkout-completed:{OWNER_ID}:{payment_id}",
    )
    if renewed:
        _record_growth(
            "renewal_completed",
            user_id,
            **common,
            deduplication_key=f"hosted-renewal-completed:{OWNER_ID}:{payment_id}",
        )
    completed_count = sum(
        1
        for item in _tenant_payments().values()
        if isinstance(item, dict) and item.get("status") == "completed"
    )
    if completed_count == 1:
        _record_growth(
            "hosted_first_sale",
            user_id,
            **common,
            deduplication_key=f"hosted-first-sale:{OWNER_ID}",
        )


def _reconcile_invite_discount_reservations():
    payments = _tenant_payments()
    active_statuses = {
        "creating", "waiting_receipt", "pending_approval", "pending", "processing",
        "paid_provision_failed",
    }
    released = []
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        reservations = data.setdefault("buyer_discount_reservations", {})
        for user_id, reservation in list(reservations.items()):
            order_id = reservation.get("order_id") if isinstance(reservation, dict) else None
            payment = payments.get(str(order_id), {})
            if not isinstance(payment, dict) or payment.get("status") not in active_statuses:
                reservations.pop(user_id, None)
                released.append(str(order_id or ""))
    return released


def _update_journey_state(user_id, **updates):
    with locked_json(tenant_file(OWNER_ID, "customer_journey.json"), {}) as data:
        item = data.setdefault(str(user_id), {})
        if not isinstance(item, dict):
            item = {}
            data[str(user_id)] = item
        item.update(updates)
        item["updated_at"] = _now()
        return dict(item)


def _claim_risk_disclosure(user_id):
    with locked_json(tenant_file(OWNER_ID, "customer_journey.json"), {}) as data:
        item = data.setdefault(str(user_id), {})
        if not isinstance(item, dict):
            item = {}
            data[str(user_id)] = item
        if item.get("risk_disclosed_at"):
            return False
        item["risk_disclosed_at"] = _now()
        return True


def _customer_has_completed_order(user_id):
    return any(
        isinstance(record, dict)
        and str(record.get("user_id")) == str(user_id)
        and record.get("status") == "completed"
        for record in _tenant_payments().values()
    )


def _invite_discount_preview(user_id, renewal=False):
    if not BUYER_DISCOUNTS_ENABLED or renewal or _customer_has_completed_order(user_id):
        return 0.0
    data = _referral_data()
    key = str(user_id)
    if key not in data.get("referrals", {}) or key in data.get("buyer_discount_redeemed", {}):
        return 0.0
    return 5.0


def _reserve_invite_discount(user_id, order_id, renewal=False):
    if not BUYER_DISCOUNTS_ENABLED or renewal or _customer_has_completed_order(user_id):
        return False
    key = str(user_id)
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        if key not in data.setdefault("referrals", {}):
            return False
        if key in data.setdefault("buyer_discount_redeemed", {}):
            return False
        reservations = data.setdefault("buyer_discount_reservations", {})
        current = reservations.get(key)
        if isinstance(current, dict) and str(current.get("order_id")) != str(order_id):
            return False
        reservations[key] = {"order_id": str(order_id), "reserved_at": _now()}
        return True


def _release_invite_discount(user_id, order_id):
    key = str(user_id)
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        current = data.setdefault("buyer_discount_reservations", {}).get(key)
        if not isinstance(current, dict) or str(current.get("order_id")) != str(order_id):
            return False
        data["buyer_discount_reservations"].pop(key, None)
        return True


def _redeem_invite_discount(user_id, order_id):
    key = str(user_id)
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        redeemed = data.setdefault("buyer_discount_redeemed", {})
        if key in redeemed:
            return str(redeemed[key].get("order_id")) == str(order_id) if isinstance(redeemed[key], dict) else False
        current = data.setdefault("buyer_discount_reservations", {}).get(key)
        if not isinstance(current, dict) or str(current.get("order_id")) != str(order_id):
            return False
        redeemed[key] = {"order_id": str(order_id), "redeemed_at": _now()}
        data["buyer_discount_reservations"].pop(key, None)
        return True


def _ensure_referral_code(user_id):
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        key = str(user_id)
        if key in data.setdefault("user_codes", {}):
            return data["user_codes"][key]
        code = uuid.uuid4().hex[:8]
        while code in data.setdefault("codes", {}):
            code = uuid.uuid4().hex[:8]
        data["codes"][code] = key
        data["user_codes"][key] = code
        data.setdefault("stats", {}).setdefault(key, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        return code


def _register_referral(user_id, code):
    registered = False
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        key = str(user_id)
        referrer = data.setdefault("codes", {}).get(code)
        if not referrer or referrer == key or key in data.setdefault("referrals", {}):
            return False
        data["referrals"][key] = referrer
        stats = data.setdefault("stats", {}).setdefault(referrer, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        stats["count"] = int(stats.get("count", 0)) + 1
        registered = True
    if registered:
        _record_growth(
            "referral_attributed",
            user_id,
            referral_campaign="hosted_invite",
            deduplication_key=f"hosted-referral-attributed:{OWNER_ID}:{user_id}",
        )
    return registered


def _credit_referral(order_id, customer_id, reward):
    if float(reward or 0) <= 0:
        return 0.0
    credited_referrer = None
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        referrer = data.setdefault("referrals", {}).get(str(customer_id))
        if not referrer:
            return 0.0
        rewarded = data.setdefault("rewarded_orders", {})
        if order_id in rewarded:
            return float(rewarded[order_id])
        stats = data.setdefault("stats", {}).setdefault(referrer, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        stats["total_earnings"] = round(float(stats.get("total_earnings", 0)) + float(reward), 2)
        stats["available_balance"] = round(float(stats.get("available_balance", 0)) + float(reward), 2)
        rewarded[order_id] = float(reward)
        credited_referrer = referrer
    if credited_referrer:
        try:
            bot.send_message(
                int(credited_referrer),
                _hosted_message(
                    int(credited_referrer),
                    "referral_reward_ready",
                    amount=format_usd_amount(reward),
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    return float(reward)


def _credit_sale_and_referral(
    payment_id,
    customer_id,
    reward,
    metadata,
    *,
    funded,
    margin=0,
):
    margin_value = _financial_amount(margin, "margin")
    reward_value = _financial_amount(reward, "referral reward")
    if reward_value > margin_value:
        raise ValueError("Hosted referral reward exceeds positive post-discount margin")
    transaction = (
        database.write_transaction(operation="hosted_sale_referral_accounting")
        if os.getenv("AJIB_SQLITE_ACTIVE") == "1"
        else nullcontext()
    )
    with transaction:
        if funded:
            accounted = credit_crypto_sale(
                OWNER_ID,
                payment_id,
                float(margin_value),
                float(reward_value),
                metadata,
            )
            expected_transactions = {
                f"sale:{payment_id}": margin_value,
            }
            if reward_value > 0:
                expected_transactions[f"referral:{payment_id}"] = reward_value
        elif reward_value > 0:
            accounted = add_referral_liability(
                OWNER_ID,
                payment_id,
                float(reward_value),
                metadata,
            )
            expected_transactions = {f"referral:{payment_id}": reward_value}
        else:
            accounted = True
            expected_transactions = {}
        if not accounted:
            transactions = {
                str(item.get("id")): _financial_amount(item.get("amount", 0), "ledger amount")
                for item in get_ledger(OWNER_ID).get("transactions", [])
                if isinstance(item, dict) and str(item.get("id")) in expected_transactions
            }
            if any(transactions.get(transaction_id) != amount for transaction_id, amount in expected_transactions.items()):
                raise RuntimeError("Hosted sale accounting was not persisted")
        credited = _credit_referral(payment_id, customer_id, float(reward_value))
        payment = _tenant_payments().get(str(payment_id), {})
        if isinstance(payment, dict) and float(payment.get("invite_discount_percent", 0) or 0) > 0:
            _redeem_invite_discount(customer_id, payment_id)
            _record_growth(
                "referral_converted",
                customer_id,
                payment_method=payment.get("payment_method"),
                plan=payment.get("plan_gb"),
                referral_campaign="hosted_invite",
                deduplication_key=f"hosted-referral-conversion:{OWNER_ID}:{payment_id}",
            )
        return credited


def _create_user(
    plan,
    note,
    customer_id=None,
    operation_id=None,
    username_prefix="h",
    on_username_allocated=None,
    preferred_username=None,
):
    try:
        recorded_usernames = load_recorded_usernames(
            extra_paths=(tenant_file(OWNER_ID, "payments.json"),),
        )
    except RecordedUsernameLoadError as exc:
        logging.getLogger("ajib.usernames").error(
            "Hosted user creation blocked because username history could not be loaded. "
            "owner_id=%s operation_id=%s error=%s",
            OWNER_ID,
            operation_id,
            exc,
        )
        return preferred_username, None, None

    multi = MultiServerAPI()

    def allocate(existing):
        if preferred_username:
            return str(preferred_username)
        return allocate_username(
            username_prefix,
            OWNER_ID,
            set(existing) | recorded_usernames,
        )

    def create(client, username):
        note_parts = [str(note or "").strip()]
        if customer_id is not None:
            note_parts.append(f"customer=u{customer_id}")
        if operation_id is not None:
            operation_fragment = "".join(
                character for character in str(operation_id) if character.isalnum()
            )[:12]
            if operation_fragment:
                note_parts.append(f"order={operation_fragment}")
        note_text = "; ".join(part for part in note_parts if part)
        payload = build_user_note(username, plan["gb"], plan["days"], unlimited=plan.get("unlimited", False), note_text=note_text)
        result = client.add_user(username, int(plan["gb"]), int(plan["days"]), unlimited=plan.get("unlimited", False), note=payload)
        return result

    return multi.create_user_with_retry(
        allocate,
        create,
        on_username_allocated=on_username_allocated,
        reuse_username_on_retry=True,
    )


def _deliver_config(chat_id, username, client, renewed=False, include_downloads=True):
    uri = client.get_user_uri(username) if client else None
    action = _hosted_message(chat_id, "renewed" if renewed else "created")
    if not uri or not uri.get("normal_sub"):
        bot.send_message(chat_id, _hosted_message(chat_id, "config_no_url", action=action, username=username),
                         parse_mode="Markdown")
        return
    url = uri.get("ipv4") or uri["normal_sub"]
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.send_photo(chat_id, image,
                   caption=_hosted_message(chat_id, "config_ready", action=action, username=username,
                                           subscription=uri["normal_sub"]),
                   parse_mode="Markdown")
    if include_downloads:
        send_download_prompt_safely(
            bot,
            chat_id,
            _language(chat_id),
            callback_prefix="hb:download",
        )


def _deliver_config_safely(chat_id, username, client, renewed=False):
    try:
        _deliver_config(chat_id, username, client, renewed=renewed)
        return True
    except Exception as error:
        print(
            f"Hosted config delivery failed for reseller {OWNER_ID}: {type(error).__name__}",
            flush=True,
        )
        return False


def _settle_hosted_reserved_renewal(payment_id, record, funded, settlement=None):
    from utils.renewal import mark_payment_renewal_reserved

    try:
        settlement = settlement or _settlement_financials(record)
    except ValueError as error:
        return False, str(error)
    customer_id = int(record["user_id"])
    username = record.get("renew_username")
    server_id = record.get("server_id")
    if not username:
        return False, "Renewal target is missing"
    common = {
        "username": username,
        "customer_telegram_id": customer_id,
        "customer_telegram_username": record.get("telegram_username"),
        "server_id": server_id,
        "reseller_id": str(OWNER_ID),
        "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
        "retail_order_id": payment_id,
        "reservation_id": payment_id,
        "retail_price": record["retail_price"],
        "price": record["wholesale_price"],
        "list_price": record.get("list_price"),
        "reseller_level": record.get("reseller_level"),
        "discount_percent": record.get("discount_percent"),
        "gb": record["plan_gb"],
        "plan_gb": record["plan_gb"],
        "days": record["days"],
        "unlimited": record.get("unlimited", False),
        "renewal_source": "hosted_customer",
        "renewal_mode": "reserved",
        "renewal_status": "reserved",
        "renewal_reserved_at": _now(),
        "renewal_baseline": record.get("renewal_baseline") or {},
        "before_state": record.get("renewal_baseline") or {},
        "after_state": None,
        "renewal_plan_snapshot": record.get("renewal_plan_snapshot") or {
            "plan_gb": record.get("plan_gb"),
            "days": record.get("days"),
            "unlimited": record.get("unlimited", False),
            "price": record.get("wholesale_price"),
            "full_price": record.get("list_price"),
            "reseller_level": record.get("reseller_level"),
            "discount_percent": record.get("discount_percent"),
        },
        "renewal_attempts": 0,
    }
    reserved, detail = reserve_reseller_renewal(
        OWNER_ID,
        username,
        record["wholesale_price"],
        common,
        server_id=server_id,
        funded=funded,
        enforce_credit=False,
    )
    if not reserved:
        return False, (detail or {}).get("reason", "Reseller accounting failed")
    if not funded:
        release_credit(OWNER_ID, payment_id, kind="renewal_credit_consumed")
    _credit_sale_and_referral(
        payment_id,
        customer_id,
        settlement["referral_reward"],
        common,
        funded=funded,
        margin=settlement["margin"],
    )
    if not mark_payment_renewal_reserved(
        payment_id,
        payments_file=tenant_file(OWNER_ID, "payments.json"),
        fields={
            "username": username,
            "server_id": server_id,
            "reservation_id": payment_id,
        },
    ):
        return False, "Reservation persistence failed"
    _record_completed_growth(payment_id, record, renewed=True)
    if funded:
        present_pending_reseller_level(
            bot,
            OWNER_ID,
            _language(OWNER_ID),
            allow_introduction=False,
        )
    reserved_text = _hosted_message(customer_id, "renewal_reserved_success")
    bot.send_message(customer_id, reserved_text)
    return True, username


def _provision_payment(payment_id, record, funded):
    try:
        settlement = _settlement_financials(record)
    except ValueError as error:
        return False, str(error)
    customer_id = int(record["user_id"])
    username = record.get("renew_username")
    client = None
    renewed = bool(username)
    if renewed and record.get("renewal_mode") == "reserved":
        return _settle_hosted_reserved_renewal(
            payment_id,
            record,
            funded,
            settlement=settlement,
        )
    reseller_snapshot = get_reseller_data(OWNER_ID) or {}
    existing_config = None
    for item in reseller_snapshot.get("configs", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("retail_order_id") or "") == str(payment_id):
            existing_config = item
            break
        if any(str(renewal.get("retail_order_id") or "") == str(payment_id)
               for renewal in item.get("renewals", []) if isinstance(renewal, dict)):
            existing_config = item
            renewed = True
            break
    if existing_config:
        username = existing_config.get("username")
        client, _ = MultiServerAPI().find_user(username, preferred_server_id=existing_config.get("server_id"))
        metadata = {"username": username, "server_id": existing_config.get("server_id"),
                    "retail_order_id": payment_id, "customer_telegram_id": customer_id}
        if not funded:
            release_credit(OWNER_ID, payment_id, kind="credit_recovered")
        _credit_sale_and_referral(
            payment_id,
            customer_id,
            settlement["referral_reward"],
            metadata,
            funded=funded,
            margin=settlement["margin"],
        )
        _save_payment(payment_id, {"status": "completed", "username": username,
                                   "server_id": existing_config.get("server_id")})
        _record_completed_growth(payment_id, record, renewed=renewed)
        _deliver_config_safely(customer_id, username, client, renewed=renewed)
        if funded:
            present_pending_reseller_level(
                bot,
                OWNER_ID,
                _language(OWNER_ID),
                allow_introduction=False,
            )
        return True, username
    if renewed:
        from utils.renewal import lookup_renewal_user

        client, live, _lookup_result = lookup_renewal_user(
            MultiServerAPI(),
            username,
            server_id=record.get("server_id"),
        )
        reset_method = getattr(client, "reset_user_result", None) if client and live else None
        if callable(reset_method):
            reset_succeeded = reset_method(username).get("status") == "succeeded"
        else:
            reset_succeeded = bool(client and live and client.reset_user(username) is not None)
        if not client or not live or not reset_succeeded:
            return False, "VPN renewal failed"
        _save_payment(payment_id, {"provisioned_username": username,
                                   "provisioned_server_id": getattr(client, "server_id", None)})
    else:
        provisioned_username = record.get("provisioned_username")
        if provisioned_username:
            client, live = MultiServerAPI().find_user(provisioned_username,
                                                       preferred_server_id=record.get("provisioned_server_id"))
            username = provisioned_username if client and live else None
        if not username:
            plan = {"gb": record["plan_gb"], "days": record["days"], "unlimited": record.get("unlimited", False)}

            def persist_allocation(allocated_username, allocated_client):
                _save_payment(
                    payment_id,
                    {
                        "provisioned_username": allocated_username,
                        "provisioned_server_id": getattr(allocated_client, "server_id", None),
                    },
                )

            username, result, client = _create_user(
                plan,
                "",
                customer_id=customer_id,
                operation_id=payment_id,
                username_prefix="hs",
                on_username_allocated=persist_allocation,
                preferred_username=provisioned_username,
            )
            if result is None:
                return False, "VPN user creation failed"
            _save_payment(payment_id, {"provisioned_username": username,
                                       "provisioned_server_id": getattr(client, "server_id", None)})
    server_id = getattr(client, "server_id", None)
    common = {
        "username": username, "customer_telegram_id": customer_id,
        "customer_telegram_username": record.get("telegram_username"),
        "server_id": server_id, "reseller_id": str(OWNER_ID),
        "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"), "retail_order_id": payment_id,
        "retail_price": record["retail_price"], "price": record["wholesale_price"],
        "list_price": record.get("list_price"),
        "reseller_level": record.get("reseller_level"),
        "discount_percent": record.get("discount_percent"),
        "plan_gb": record["plan_gb"], "days": record["days"],
    }
    if funded:
        accounted = (record_funded_reseller_renewal(OWNER_ID, username, record["wholesale_price"], common, server_id)
                     if renewed else record_funded_reseller_config(OWNER_ID, record["wholesale_price"], common))
    else:
        accounted = (consume_renewal_credit(OWNER_ID, payment_id, username, common, server_id)
                     if renewed else consume_credit(OWNER_ID, payment_id, common))
    if not accounted:
        if not renewed and client:
            client.delete_user(username)
        return False, "Reseller accounting failed"
    if funded:
        present_pending_reseller_level(
            bot,
            OWNER_ID,
            _language(OWNER_ID),
            allow_introduction=False,
        )
    _credit_sale_and_referral(
        payment_id,
        customer_id,
        settlement["referral_reward"],
        common,
        funded=funded,
        margin=settlement["margin"],
    )
    _save_payment(payment_id, {"status": "completed", "username": username, "server_id": server_id})
    _record_completed_growth(payment_id, record, renewed=renewed)
    _deliver_config_safely(customer_id, username, client, renewed=renewed)
    return True, username


def _provision_claimed_payment(payment_id, record, funded, retry_status):
    try:
        success, detail = _provision_payment(payment_id, record, funded=funded)
    except Exception as error:
        success, detail = False, f"Provisioning raised {type(error).__name__}"
        print(
            f"Hosted payment provisioning failed for reseller {OWNER_ID}: {type(error).__name__}",
            flush=True,
        )
    if not success:
        _save_payment(payment_id, {"status": retry_status, "last_error": str(detail)[:500]})
    return success, detail


def _recommended_plan_id(plans, settings):
    recommended = str(settings.get("recommended_plan_id") or "")
    return recommended if recommended in plans else None


def _customer_card_pricing_enabled(language):
    return language in {"en", "fa"}


def _plan_button_text(user_id, plan_id, plan, quote, label_key=None, exchange_rate=None):
    language = _language(user_id)
    label = hosted_text(language, label_key) if label_key else ""
    if not _customer_card_pricing_enabled(language):
        return hosted_text(
            language,
            "plan_button_usd_only",
            label=(f"{label} · " if label else ""),
            gb=plan.get("gb", plan_id),
            days=plan.get("days", 30),
            usd=format_usd_amount(quote["retail"]),
        )
    exchange_rate = exchange_rate if exchange_rate is not None else get_exchange_rate()
    return hosted_text(
        language,
        "plan_button_usd_first",
        label=(f"{label} · " if label else ""),
        gb=plan.get("gb", plan_id),
        days=plan.get("days", 30),
        usd=format_usd_amount(quote["retail"]),
        toman=format_toman_amount(quote["retail"] * exchange_rate),
    )


def _show_plans(chat_id, user_id, message_id=None, event_key=None):
    markup = types.InlineKeyboardMarkup(row_width=1)
    settings = get_settings(OWNER_ID)
    plans = _sellable_plans()
    language = _language(user_id)
    exchange_rate = get_exchange_rate() if _customer_card_pricing_enabled(language) else None
    recommended_plan_id = _recommended_plan_id(plans, settings)
    choices = sorted(
        plans,
        key=lambda plan_id: (int(plans[plan_id].get("gb", plan_id)), int(plan_id)),
    )
    for plan_id in choices:
        plan = plans[plan_id]
        quote = _hosted_plan_quote(plan, settings)
        markup.add(types.InlineKeyboardButton(
            _plan_button_text(
                user_id,
                plan_id,
                plan,
                quote,
                label_key="pick_recommended" if plan_id == recommended_plan_id else None,
                exchange_rate=exchange_rate,
            ),
            callback_data=f"hb:buy:{plan_id}",
        ))
    text = (
        _hosted_message(user_id, "all_plans_title")
        if markup.keyboard else _message(user_id, "plan_not_found")
    )
    _record_growth(
        "plan_viewed",
        user_id,
        deduplication_key=(
            event_key
            or f"hosted-plan-viewed:{OWNER_ID}:{user_id}:{message_id or 'direct'}:catalog"
        ),
    )
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")


def _purchase_options(chat_id, user_id, plan_id, renewal=None, message_id=None):
    reseller = _reseller(active_only=True)
    if not reseller:
        bot.send_message(chat_id, _hosted_message(user_id, "purchase_unavailable"))
        return
    plan = _sellable_plans().get(str(plan_id))
    if not plan:
        bot.send_message(chat_id, _message(user_id, "plan_not_found"))
        return
    settings = get_settings(OWNER_ID)
    language = _language(user_id)
    buyer_discount_percent = _invite_discount_preview(user_id, renewal=bool(renewal))
    quote = _hosted_plan_quote(
        plan,
        settings,
        settings["referral_margin_percent"],
        referred=str(user_id) in _referral_data().get("referrals", {}),
        buyer_discount_percent=buyer_discount_percent,
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    suffix = ""
    if renewal:
        token = _store_renewal_token(user_id, renewal)
        suffix = f":{token}"
    card_pricing_enabled = _customer_card_pricing_enabled(language)
    exchange_rate = get_exchange_rate() if card_pricing_enabled else None
    crypto_available = bool(
        settings.get("crypto_enabled")
        and quote["crypto_supported"]
        and os.getenv("CRYPTO_MERCHANT_ID")
        and os.getenv("CRYPTO_API_KEY")
    )
    card_available = bool(
        card_pricing_enabled
        and settings.get("card_number")
        and quote["card_supported"]
    )
    if crypto_available:
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "crypto_method", amount=format_usd_amount(quote["crypto_collected"])),
            callback_data=f"hb:pay:crypto:{plan_id}{suffix}",
        ))
    if card_available:
        markup.add(types.InlineKeyboardButton(
            _hosted_message(
                user_id,
                "card_method",
                amount=format_toman_amount(quote["card_collected"] * exchange_rate),
            ),
            callback_data=f"hb:pay:card:{plan_id}{suffix}",
        ))
    if not markup.keyboard:
        bot.send_message(chat_id, _message(user_id, "no_payment_methods"))
        return
    markup.add(types.InlineKeyboardButton(get_button_text(language, "back"), callback_data="hb:plans"))
    text = _hosted_message(user_id, "checkout_progress")
    text += "\n\n" + _hosted_message(
        user_id,
        "plan_checkout_header",
        gb=plan.get("gb", plan_id),
        days=plan.get("days", 30),
    )
    if crypto_available:
        text += "\n" + _hosted_message(
            user_id,
            "crypto_total_usd_only",
            usd=format_usd_amount(quote["crypto_collected"]),
        )
    if card_available:
        text += "\n" + _hosted_message(
            user_id,
            "card_total_toman",
            toman=format_toman_amount(quote["card_collected"] * exchange_rate),
        )
    if buyer_discount_percent:
        text += "\n" + _hosted_message(
            user_id,
            "invite_discount_applied",
            percent=f"{buyer_discount_percent:g}",
        )
    text += "\n\n" + _hosted_message(user_id, "delivery_summary")
    text += "\n" + _hosted_message(user_id, "support_summary")
    if not renewal and _claim_risk_disclosure(user_id):
        text += "\n\n" + _hosted_message(user_id, "risk_disclosure")
    _record_growth(
        "plan_selected",
        user_id,
        plan=plan_id,
        deduplication_key=(
            f"hosted-plan-selected:{OWNER_ID}:{user_id}:{message_id or 'direct'}:{plan_id}"
        ),
    )
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


def _language_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        types.InlineKeyboardButton(name, callback_data=f"hb:lang:{code}")
        for code, name in LANGUAGES.items()
    ])
    return markup


def _test_record(user_id):
    tests = read_json(GLOBAL_TEST_FILE, {})
    record = tests.get(str(user_id), {}) if isinstance(tests, dict) else {}
    return dict(record) if isinstance(record, dict) else {}


def _customer_onboarding_state(user_id):
    configs = _find_customer_configs(user_id)
    if configs:
        states = set()
        for config in configs:
            client, live = MultiServerAPI().find_user(
                config.get("username"),
                preferred_server_id=config.get("server_id"),
            )
            if not client or not live:
                states.add("unknown")
                continue
            cycle = _hosted_service_cycle(config)
            account = inspect_account(live, cycle=cycle, source="hosted_onboarding")
            maximum = float(live.get("max_download_bytes", 0) or 0)
            used = float(live.get("upload_bytes", 0) or 0) + float(live.get("download_bytes", 0) or 0)
            remaining_traffic = maximum <= 0 or used < maximum
            if account.panel_state == PanelState.UNKNOWN or account.entitlement_state == EntitlementState.UNKNOWN:
                states.add("unknown")
            elif (
                account.panel_state == PanelState.BLOCKED
                or account.entitlement_state == EntitlementState.EXPIRED
                or not remaining_traffic
            ):
                states.add("expired")
            elif account.panel_state == PanelState.HOLD:
                states.add("paid_hold")
            else:
                states.add("paid")
        for state in ("paid", "paid_hold", "unknown", "expired"):
            if state in states:
                return state, configs
        return "unknown", configs
    test = _test_record(user_id)
    if test.get("used_at"):
        client, live = MultiServerAPI().find_user(
            test.get("username"),
            preferred_server_id=test.get("server_id"),
        )
        if not client or not live:
            return "unknown", test
        account = inspect_account(live, source="hosted_test_onboarding")
        if account.panel_state == PanelState.HOLD:
            return "trial", test
        if account.panel_state == PanelState.CONNECTED:
            return "trial_active", test
        if account.panel_state == PanelState.UNKNOWN:
            return "unknown", test
        return "expired", test
    return "new", None


def _send_onboarding(chat_id, user_id, reply_to=None):
    state, detail = _customer_onboarding_state(user_id)
    settings = get_settings(OWNER_ID)
    custom_welcome = localized_storefront_text(settings, "welcome", _language(user_id), default="")
    state_key = {
        "new": "welcome_new",
        "trial": "welcome_trial",
        "trial_active": "welcome_trial_active",
        "paid": "welcome_paid",
        "paid_hold": "welcome_paid_hold",
        "unknown": "welcome_unknown",
        "expired": "welcome_expired",
    }[state]
    text = _hosted_message(user_id, state_key)
    if state == "paid_hold" and isinstance(detail, list):
        for config in detail:
            cycle = _hosted_service_cycle(config)
            if cycle is None:
                continue
            text += "\n" + _hosted_message(
                user_id,
                "paid_hold_deadline",
                username=config.get("username"),
                deadline=cycle.deadline.astimezone(bot_timezone()).strftime("%Y-%m-%d %H:%M"),
            )
    if state == "trial_active" and isinstance(detail, dict) and detail.get("username"):
        _client, live = MultiServerAPI().find_user(
            detail.get("username"),
            preferred_server_id=detail.get("server_id"),
        )
        if live:
            account = inspect_account(live, source="hosted_test_onboarding")
            maximum = float(live.get("max_download_bytes", 0) or 0)
            used = float(live.get("upload_bytes", 0) or 0) + float(live.get("download_bytes", 0) or 0)
            remaining_gb = max(0.0, maximum - used) / (1024 ** 3) if maximum > 0 else 0.0
            if account.panel_days_remaining is not None:
                text += "\n\n" + _hosted_message(
                    user_id,
                    "trial_remaining",
                    days=account.panel_days_remaining,
                    gb=f"{remaining_gb:.1f}",
                )
    if custom_welcome:
        text = f"{custom_welcome}\n\n{text}"
    markup = types.InlineKeyboardMarkup(row_width=1)
    if state == "new":
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "start_free_test"),
            callback_data="hb:test:start",
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "see_plans"),
            callback_data="hb:plans",
        ))
    elif state == "trial":
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "need_help_action"),
            callback_data="hb:test:help",
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "connected_action"),
            callback_data="hb:test:connected",
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "see_plans"),
            callback_data="hb:plans",
        ))
    elif state == "expired" and isinstance(detail, list):
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "renew_action"),
            callback_data="hb:renewcfg:0",
        ))
    else:
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "my_configs_action"),
            callback_data="hb:configs",
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(user_id, "see_plans"),
            callback_data="hb:plans",
        ))
    _record_growth(
        "onboarding_viewed",
        user_id,
        deduplication_key=f"hosted-onboarding:{OWNER_ID}:{user_id}:{state}",
    )
    sender = bot.reply_to if reply_to is not None else bot.send_message
    if reply_to is not None:
        sender(reply_to, text, reply_markup=markup)
    else:
        sender(chat_id, text, reply_markup=markup)
    bot.send_message(
        chat_id,
        _hosted_message(user_id, "menu_updated"),
        reply_markup=_main_markup(user_id),
    )


@bot.message_handler(commands=["start"])
def start(message):
    parts = (message.text or "").split(maxsplit=1)
    start_payload = parts[1].strip() if len(parts) == 2 else ""
    if start_payload == "owner_setup" and message.from_user.id == OWNER_ID:
        _show_owner_dashboard(message.chat.id, reply_to=message)
        return
    if start_payload and start_payload != "owner_setup":
        _register_referral(message.from_user.id, parts[1].strip())
    if not _has_language(message.from_user.id):
        detected = _telegram_language(message.from_user)
        if detected:
            _set_language(message.from_user.id, detected)
        else:
            bot.reply_to(
                message,
                hosted_text("en", "choose_language"),
                reply_markup=_language_markup(),
            )
            return
    _send_onboarding(message.chat.id, message.from_user.id, reply_to=message)


@bot.message_handler(func=lambda m: m.text in _all_button_values("purchase_plan"))
def plans(message):
    _show_plans(
        message.chat.id,
        message.from_user.id,
        event_key=f"hosted-plan-viewed:{OWNER_ID}:{message.from_user.id}:message:{message.message_id}:catalog",
    )


@bot.callback_query_handler(func=lambda c: c.data == "hb:plans")
def plans_back(call):
    bot.answer_callback_query(call.id)
    _show_plans(
        call.message.chat.id,
        call.from_user.id,
        call.message.message_id,
        event_key=f"hosted-plan-viewed:{OWNER_ID}:{call.from_user.id}:callback:{call.message.message_id}:catalog",
    )


@bot.callback_query_handler(func=lambda c: c.data == "hb:plans:all")
def plans_all(call):
    bot.answer_callback_query(call.id)
    _show_plans(
        call.message.chat.id,
        call.from_user.id,
        call.message.message_id,
        event_key=f"hosted-plan-viewed:{OWNER_ID}:{call.from_user.id}:callback:{call.message.message_id}:catalog",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:buy:"))
def buy(call):
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, call.data.split(":")[2],
                      message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:pay:"))
def payment_method(call):
    parts = call.data.split(":")
    if len(parts) not in {4, 5} or parts[2] not in {"card", "crypto"}:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "invalid_checkout_action"),
            show_alert=True,
        )
        return
    method, plan_id = parts[2], parts[3]
    language = _language(call.from_user.id)
    if method == "card" and not _customer_card_pricing_enabled(language):
        bot.answer_callback_query(
            call.id,
            _message(call.from_user.id, "no_payment_methods"),
            show_alert=True,
        )
        return
    plan = _sellable_plans().get(plan_id)
    settings = get_settings(OWNER_ID)
    if not plan or not _reseller(active_only=True):
        bot.answer_callback_query(call.id, _message(call.from_user.id, "plan_not_found"), show_alert=True)
        return
    renewal = None
    if len(parts) >= 5:
        renewal = _consume_renewal_token(parts[4], call.from_user.id)
        if not renewal:
            bot.answer_callback_query(
                call.id,
                _hosted_message(call.from_user.id, "renewal_checkout_expired"),
                show_alert=True,
            )
            return
        renewal, renewal_error = _resolve_hosted_renewal_checkout(
            call.from_user.id,
            plan_id,
            renewal,
        )
        if not renewal:
            reason = _message(call.from_user.id, renewal_error)
            bot.answer_callback_query(
                call.id,
                _message(call.from_user.id, "renewal_unavailable").format(reason=reason),
                show_alert=True,
            )
            return
    referred = str(call.from_user.id) in _referral_data().get("referrals", {})
    order_id = str(uuid.uuid4())
    invite_discount_eligible = bool(
        _invite_discount_preview(call.from_user.id, renewal=bool(renewal))
    )
    invite_discount_reserved = _reserve_invite_discount(
        call.from_user.id,
        order_id,
        renewal=bool(renewal),
    )
    if invite_discount_eligible and not invite_discount_reserved:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "checkout_already_started"),
            show_alert=True,
        )
        return
    buyer_discount_percent = (
        float(INVITED_BUYER_DISCOUNT_PERCENT) if invite_discount_reserved else 0.0
    )
    quote = _hosted_plan_quote(
        plan,
        settings,
        settings["referral_margin_percent"],
        referred,
        buyer_discount_percent=buyer_discount_percent,
    )
    collected_amount = quote["card_collected"] if method == "card" else quote["crypto_collected"]
    margin = quote["card_margin"] if method == "card" else quote["crypto_margin"]
    customer_discount_percent = (
        quote["card_discount_percent"] if method == "card" else quote["crypto_discount_percent"]
    )
    customer_discount_amount = (
        quote["card_discount_amount"] if method == "card" else quote["crypto_discount_amount"]
    )
    referrer_id = _referral_data().get("referrals", {}).get(str(call.from_user.id))
    record = {
        "id": order_id, "user_id": call.from_user.id, "telegram_username": call.from_user.username,
        "reseller_id": str(OWNER_ID), "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
        "plan_gb": plan_id, "days": plan.get("days", 30), "unlimited": plan.get("unlimited", False),
        "wholesale_price": quote["wholesale"], "retail_price": collected_amount,
        "original_price": quote["original_price"], "collected_amount": collected_amount,
        "margin": margin,
        "list_price": quote["list_price"], "reseller_level": quote["reseller_level"],
        "discount_percent": quote["discount_percent"],
        "invite_discount_percent": quote["buyer_discount_percent"],
        "invite_discount_amount": quote["buyer_discount_amount"],
        "crypto_discount_percent": (
            quote["crypto_component_discount_percent"] if method == "crypto" else 0.0
        ),
        "crypto_discount_amount": (
            quote["crypto_component_discount_amount"] if method == "crypto" else 0.0
        ),
        "total_discount_percent": customer_discount_percent,
        "total_discount_amount": customer_discount_amount,
        "referral_attribution": str(referrer_id) if referrer_id else None,
        "reward_calculation_base": max(0.0, margin),
        "referral_reward": quote["card_referral_reward"] if method == "card" else quote["crypto_referral_reward"],
        "payment_method": method,
        "checkout_source": f"{call.message.chat.id}:{call.message.message_id}:{method}:{plan_id}",
        "renew_username": renewal and renewal["username"], "server_id": renewal and renewal.get("server_id"),
        "renewal_source": renewal and "hosted_customer",
        "renewal_mode": renewal and renewal.get("renewal_mode", "immediate"),
        "renewal_baseline": renewal and renewal.get("renewal_baseline"),
        "renewal_plan_snapshot": renewal and {
            "plan_gb": plan_id,
            "days": plan.get("days", 30),
            "unlimited": plan.get("unlimited", False),
            "price": quote["wholesale"],
            "full_price": quote["list_price"],
            "reseller_level": quote["reseller_level"],
            "discount_percent": quote["discount_percent"],
        },
    }
    if method == "card" and not settings.get("card_number"):
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(call.id, _message(call.from_user.id, "no_payment_methods"), show_alert=True)
        return
    if method == "card" and not quote["card_supported"]:
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(call.id, _message(call.from_user.id, "no_payment_methods"), show_alert=True)
        return
    if method == "crypto" and (not settings.get("crypto_enabled") or not quote["crypto_supported"]):
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "crypto_disabled"), show_alert=True)
        return
    if method == "card":
        record["reservation_id"] = order_id
    started, existing_id = _start_checkout(order_id, record)
    if not started:
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(
            call.id,
            _hosted_message(
                call.from_user.id,
                "checkout_already_started" if existing_id != order_id else "checkout_creation_failed",
            ),
            show_alert=True,
        )
        return
    if method == "card":
        reseller = get_reseller_data(OWNER_ID) or {}
        _, _, available = can_reseller_add_debt(reseller, 0)
        if not reserve_credit(OWNER_ID, order_id, quote["wholesale"], available):
            _release_invite_discount(call.from_user.id, order_id)
            _save_payment(order_id, {"status": "failed", "last_error": "Reseller credit is unavailable"})
            bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "credit_unavailable"),
                                      show_alert=True)
            return
        exchange_rate = get_exchange_rate()
        toman_price = quote["card_collected"] * exchange_rate
        record.update({"status": "waiting_receipt", "reservation_id": order_id,
                       "exchange_rate": exchange_rate, "converted_amount": toman_price,
                       "converted_currency": "TOMAN",
                       "receipt_prompt_chat_id": call.message.chat.id,
                       "receipt_prompt_message_id": call.message.message_id})
        _save_payment(order_id, record)
        _set_input_state(call.from_user.id, {"kind": "receipt", "payment_id": order_id})
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            get_button_text(_language(call.from_user.id), "cancel"),
            callback_data=f"hb:cancel:{order_id}",
        ))
        markup.add(types.InlineKeyboardButton(
            _button(call.from_user.id, "support"),
            callback_data="hb:support",
        ))
        bot.edit_message_text(
            _hosted_message(
                call.from_user.id,
                "card_checkout",
                gb=plan.get("gb", plan_id),
                days=plan.get("days", 30),
                amount=format_toman_amount(toman_price),
                card=settings["card_number"],
            ),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        _record_growth(
            "checkout_started",
            call.from_user.id,
            plan=plan_id,
            payment_method="card",
            referral_campaign="hosted_invite" if referred else None,
            deduplication_key=f"hosted-checkout:{OWNER_ID}:{order_id}",
        )
        return
    try:
        response = CryptoPayment().create_payment(
            quote["crypto_collected"], plan_id, call.from_user.id,
            additional_data={"reseller_id": str(OWNER_ID), "hosted_order_id": order_id},
        )
    except Exception as error:
        response = {"error": f"Gateway request failed ({type(error).__name__})"}
    if not isinstance(response, dict) or "error" in response:
        error_message = response.get("error", "Invalid gateway response") if isinstance(response, dict) else "Invalid gateway response"
        _save_payment(order_id, {"status": "failed", "last_error": str(error_message)[:500]})
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "checkout_creation_failed"),
            show_alert=True,
        )
        return
    gateway = response.get("result", {})
    gateway_id, url = gateway.get("uuid"), gateway.get("url")
    if not gateway_id or not url:
        _save_payment(order_id, {"status": "failed", "last_error": "Invalid gateway response"})
        _release_invite_discount(call.from_user.id, order_id)
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "checkout_creation_failed"),
            show_alert=True,
        )
        return
    record.update({"status": "pending", "gateway_payment_id": gateway_id, "payment_url": url,
                   "crypto_collected": quote["crypto_collected"], "margin": quote["crypto_margin"]})
    _save_payment(order_id, record)
    markup = types.InlineKeyboardMarkup()
    language = _language(call.from_user.id)
    markup.add(types.InlineKeyboardButton(get_button_text(language, "payment_link"), url=url),
               types.InlineKeyboardButton(get_button_text(language, "check_status"),
                                          callback_data=f"hb:check:{order_id}"))
    markup.add(types.InlineKeyboardButton(
        _button(call.from_user.id, "support"),
        callback_data="hb:support",
    ))
    caption = _hosted_message(
        call.from_user.id,
        "crypto_checkout",
        gb=plan.get("gb", plan_id),
        days=plan.get("days", 30),
        amount=format_usd_amount(quote["crypto_collected"]),
        payment_id=gateway_id,
    )
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.answer_callback_query(call.id)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_photo(call.message.chat.id, image, caption=caption, parse_mode="Markdown", reply_markup=markup)
    _record_growth(
        "checkout_started",
        call.from_user.id,
        plan=plan_id,
        payment_method="crypto",
        referral_campaign="hosted_invite" if referred else None,
        deduplication_key=f"hosted-checkout:{OWNER_ID}:{order_id}",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:cancel:"))
def cancel_card_payment(call):
    payment_id = call.data.split(":", 2)[2]
    current = _tenant_payments().get(payment_id)
    if not isinstance(current, dict) or str(current.get("user_id")) != str(call.from_user.id):
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "already_processed"),
                                  show_alert=True)
        return
    record = _claim_payment(payment_id, {"waiting_receipt", "pending_approval"})
    if not record:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "already_processed"),
                                  show_alert=True)
        return
    release_credit(OWNER_ID, payment_id, kind="credit_canceled")
    _release_invite_discount(call.from_user.id, payment_id)
    _save_payment(payment_id, {"status": "canceled"})
    _clear_input_state(call.from_user.id, kind="receipt", payment_id=payment_id)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(_message(call.from_user.id, "purchase_canceled"),
                          call.message.chat.id, call.message.message_id)


@bot.message_handler(content_types=["photo"])
def receipt_photo(message):
    reply = getattr(message, "reply_to_message", None)
    payment_id = _receipt_checkout(
        message.from_user.id,
        reply_message_id=getattr(reply, "message_id", None),
        chat_id=message.chat.id,
    )
    if not payment_id:
        return
    current = _tenant_payments().get(payment_id)
    if not isinstance(current, dict) or str(current.get("user_id")) != str(message.from_user.id):
        _clear_input_state(message.from_user.id, kind="receipt", payment_id=payment_id)
        return
    record = _claim_payment(payment_id, {"waiting_receipt"})
    if not record:
        _clear_input_state(message.from_user.id, kind="receipt", payment_id=payment_id)
        return
    try:
        receipt_path = _store_receipt_photo(message, payment_id)
        _save_payment(
            payment_id,
            {
                "status": "pending_approval",
                "receipt_path": receipt_path,
                "receipt_received_at": _now(),
            },
        )
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        _save_payment(
            payment_id,
            {"status": "waiting_receipt", "last_error": f"Receipt upload failed: {detail}"[:500]},
        )
        _set_input_state(message.from_user.id, {"kind": "receipt", "payment_id": payment_id})
        print(
            f"Hosted receipt upload failed for reseller {OWNER_ID}, "
            f"payment {payment_id}: {detail}",
            flush=True,
        )
        try:
            bot.reply_to(message, _hosted_message(message.from_user.id, "receipt_upload_failed"))
        except Exception:
            pass
        return

    _notify_owner_of_receipt(payment_id)
    try:
        bot.reply_to(message, _message(message.from_user.id, "receipt_submitted"))
    except Exception as error:
        print(
            f"Hosted receipt confirmation failed for reseller {OWNER_ID}, "
            f"payment {payment_id}: {type(error).__name__}: {error}",
            flush=True,
        )
    _clear_input_state(message.from_user.id, kind="receipt", payment_id=payment_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith(("hb:approve:", "hb:reject:")))
def owner_receipt(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "owner_panel"), show_alert=True)
        return
    action, payment_id = call.data.split(":")[1:]
    record = _claim_payment(payment_id, {"pending_approval"})
    if not record:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "already_processed"), show_alert=True)
        return
    if action == "reject":
        release_credit(OWNER_ID, payment_id)
        _release_invite_discount(record["user_id"], payment_id)
        _save_payment(payment_id, {"status": "rejected"})
        bot.send_message(record["user_id"], _hosted_message(record["user_id"], "receipt_rejected"))
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "rejected"))
        return
    success, result = _provision_claimed_payment(
        payment_id, record, funded=False, retry_status="pending_approval"
    )
    if not success:
        bot.answer_callback_query(call.id, result, show_alert=True)
        return
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "approved"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:check:"))
def check_crypto(call):
    payment_id = call.data.split(":")[2]
    current = _tenant_payments().get(payment_id)
    if not current or str(current.get("user_id")) != str(call.from_user.id):
        bot.answer_callback_query(call.id, _message(call.from_user.id, "payment_record_not_found"), show_alert=True)
        return
    if current.get("status") == "completed":
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "payment_completed"),
            show_alert=True,
        )
        return
    if current.get("status") not in {"pending", "paid_provision_failed", "processing"}:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "payment_closed"),
            show_alert=True,
        )
        return
    record = _claim_payment(payment_id, {"pending", "paid_provision_failed"})
    if not record:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "payment_processing"), show_alert=True)
        return
    retry_status = record.get("processing_from_status")
    if retry_status not in {"pending", "paid_provision_failed"}:
        retry_status = "pending"
    if not record.get("gateway_payment_id"):
        _save_payment(payment_id, {"status": retry_status, "last_error": "Gateway reference is missing"})
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "payment_gateway_missing"),
            show_alert=True,
        )
        return
    try:
        response = CryptoPayment().check_payment_status(record["gateway_payment_id"])
    except Exception as error:
        _save_payment(
            payment_id,
            {"status": retry_status, "last_error": f"Gateway status failed: {type(error).__name__}"},
        )
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "payment_status_unavailable"),
            show_alert=True,
        )
        return
    result = response.get("result", {}) if isinstance(response, dict) else {}
    status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
    if str(status).lower() != "paid":
        _save_payment(payment_id, {"status": retry_status})
        bot.answer_callback_query(
            call.id,
            _message(call.from_user.id, "payment_pending"),
            show_alert=True,
        )
        return
    success, detail = _provision_claimed_payment(
        payment_id, record, funded=True, retry_status="paid_provision_failed"
    )
    if not success:
        bot.send_message(OWNER_ID, f"⚠️ Paid order `{payment_id}` needs retry: {detail}", parse_mode="Markdown")
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "paid_needs_attention"),
                                  show_alert=True)
        return
    bot.answer_callback_query(
        call.id,
        _hosted_message(call.from_user.id, "payment_completed"),
        show_alert=True,
    )


@bot.message_handler(func=lambda m: m.text in _all_button_values("my_configs"))
def my_configs(message):
    _show_customer_configs(message.chat.id, message.from_user.id, reply_to=message)


def _show_customer_configs(chat_id, user_id, reply_to=None):
    configs = _find_customer_configs(user_id)
    if not configs:
        if reply_to is not None:
            bot.reply_to(reply_to, _hosted_message(user_id, "no_configs"))
        else:
            bot.send_message(chat_id, _hosted_message(user_id, "no_configs"))
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, config in enumerate(configs):
        fallback = _hosted_message(user_id, "config_fallback", number=index + 1)
        markup.add(types.InlineKeyboardButton(
            config.get("username", fallback),
            callback_data=f"hb:cfg:{index}",
        ))
    if reply_to is not None:
        bot.reply_to(reply_to, _hosted_message(user_id, "configs_title"), reply_markup=markup)
    else:
        bot.send_message(chat_id, _hosted_message(user_id, "configs_title"), reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data == "hb:configs")
def configs_callback(call):
    bot.answer_callback_query(call.id)
    _show_customer_configs(call.message.chat.id, call.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:cfg:"))
def config_detail(call):
    configs = _find_customer_configs(call.from_user.id)
    try:
        config = configs[int(call.data.split(":")[2])]
    except (IndexError, ValueError):
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "config_not_found"),
            show_alert=True,
        )
        return
    from utils.renewal import find_reseller_renewal_offer, find_reseller_reservation, lookup_renewal_user

    client, live, lookup_result = lookup_renewal_user(
        MultiServerAPI(),
        config.get("username"),
        server_id=config.get("server_id"),
    )
    if not client or not live:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "config_unavailable"),
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id)
    _deliver_config(call.message.chat.id, config["username"], client)
    existing = find_reseller_reservation(config) or _matching_reserved_checkout(
        _tenant_payments(),
        call.from_user.id,
        config.get("username"),
        config.get("server_id"),
    )
    if existing:
        status_text = _hosted_message(call.from_user.id, "renewal_reserved_status")
        bot.send_message(call.message.chat.id, status_text)
        return
    plan_id = str(config.get("plan_gb") or config.get("gb") or "")
    plans = _sellable_plans()
    reseller_data = get_reseller_data(OWNER_ID) or {}
    if reseller_data.get("status") != "approved":
        bot.send_message(call.message.chat.id, _hosted_message(call.from_user.id, "purchase_unavailable"))
        return
    offer = find_reseller_renewal_offer(
        OWNER_ID,
        config.get("_config_index"),
        client,
        live,
        plans,
        reseller_data=reseller_data,
        allow_reservation=True,
        lookup_result=lookup_result,
    )
    if plan_id in plans and offer.get("eligible"):
        renewal_mode = offer.get("renewal_mode", "immediate")
        markup = types.InlineKeyboardMarkup()
        renewal_token = _store_renewal_token(
            call.from_user.id,
            {
                "username": config["username"],
                "server_id": config.get("server_id"),
                "config_index": config.get("_config_index"),
                "renewal_mode": renewal_mode,
                "renewal_baseline": offer.get("before_state"),
            },
        )
        button_key = "renew_plan" if renewal_mode == "immediate" else "reserve_renewal"
        markup.add(types.InlineKeyboardButton(
            get_button_text(_language(call.from_user.id), button_key) or (
                "🔄 Renew" if renewal_mode == "immediate" else "🗓 Reserve renewal"
            ),
            callback_data=f"hb:renew:{plan_id}:{renewal_token}",
        ))
        message_key = "renewal_available" if renewal_mode == "immediate" else "renewal_reservation_available"
        message = _hosted_message(call.from_user.id, message_key)
        bot.send_message(call.message.chat.id, message, reply_markup=markup)
    elif plan_id in plans:
        reason_key = offer.get("reason") or "renewal_ineligible_plan_mismatch"
        reason = _message(call.from_user.id, reason_key)
        bot.send_message(
            call.message.chat.id,
            _message(call.from_user.id, "renewal_unavailable").format(reason=reason),
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:renew:"))
def renew(call):
    parts = call.data.split(":")
    if len(parts) != 4:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "invalid_renewal_action"),
            show_alert=True,
        )
        return
    plan_id, token = parts[2], parts[3]
    renewal = _consume_renewal_token(token, call.from_user.id)
    if not renewal:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "renewal_action_expired"),
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, plan_id, renewal)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:renewcfg:"))
def renew_config_direct(call):
    configs = _find_customer_configs(call.from_user.id)
    try:
        config = configs[int(call.data.split(":")[2])]
    except (IndexError, ValueError):
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "config_not_found"),
            show_alert=True,
        )
        return
    plan_id = str(config.get("plan_gb") or config.get("gb") or "")
    if plan_id not in _sellable_plans():
        bot.answer_callback_query(call.id, _message(call.from_user.id, "plan_not_found"), show_alert=True)
        return
    renewal, reason = _resolve_hosted_renewal_checkout(
        call.from_user.id,
        plan_id,
        {
            "config_index": config.get("_config_index"),
            "username": config.get("username"),
        },
    )
    if not renewal:
        localized_reason = _message(call.from_user.id, reason)
        bot.answer_callback_query(
            call.id,
            _message(call.from_user.id, "renewal_unavailable").format(reason=localized_reason),
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, plan_id, renewal)


@bot.message_handler(func=lambda m: m.text in _all_button_values("support"))
def support(message):
    bot.reply_to(message, _storefront_setting(message.from_user.id, "support"))


@bot.callback_query_handler(func=lambda c: c.data == "hb:support")
def support_callback(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, _storefront_setting(call.from_user.id, "support"))


@bot.message_handler(func=lambda m: m.text in _all_button_values("language"))
def language(message):
    bot.reply_to(
        message,
        _hosted_message(message.from_user.id, "choose_language"),
        reply_markup=_language_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:lang:"))
def language_set(call):
    code = call.data.split(":")[2]
    if code in LANGUAGES:
        _set_language(call.from_user.id, code)
    bot.answer_callback_query(
        call.id,
        _hosted_message(call.from_user.id, "language_updated"),
        show_alert=True,
    )
    _send_onboarding(call.message.chat.id, call.from_user.id)


@bot.message_handler(func=lambda m: m.text in _all_button_values("downloads"))
def downloads(message):
    send_download_prompt(
        bot,
        message.chat.id,
        _language(message.from_user.id),
        callback_prefix="hb:download",
        reply_to=message,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:download:"))
def download_selection(call):
    try:
        render_download_callback(
            bot,
            call,
            _language(call.from_user.id),
            callback_prefix="hb:download",
        )
    except Exception:
        try:
            bot.answer_callback_query(
                call.id,
                text=_message(call.from_user.id, "download_error"),
                show_alert=True,
            )
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.text in _all_button_values("test_config"))
def free_test(message, customer=None):
    customer = customer or message.from_user
    recovering_pending_test = False
    pending_username = None
    pending_server_id = None
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        key = str(customer.id)
        if key in tests:
            existing = tests.get(key)
            pending_at = _parse_time(existing.get("creation_pending_at")) if isinstance(existing, dict) else None
            pending_is_stale = (
                isinstance(existing, dict)
                and existing.get("creation_pending_at")
                and (pending_at is None or (datetime.now() - pending_at).total_seconds() >= TEST_CREATION_LEASE_SECONDS)
                and str(existing.get("reseller_id")) == str(OWNER_ID)
                and not existing.get("used_at")
            )
            if not pending_is_stale:
                bot.reply_to(message, _hosted_message(customer.id, "test_already_used"))
                return
            recovering_pending_test = True
            pending_username = existing.get("username")
            pending_server_id = existing.get("server_id")
        tests[key] = {
            **(dict(existing) if recovering_pending_test else {}),
            "telegram_id": customer.id,
            "creation_pending_at": _now(),
            "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
            "reseller_id": str(OWNER_ID),
        }
    plan = {"gb": 1, "days": 30, "unlimited": False}
    username = pending_username
    client, live = (
        MultiServerAPI().find_user(username, preferred_server_id=pending_server_id)
        if username
        else (None, None)
    )
    result = live if recovering_pending_test else None
    if not client or not live:
        def persist_test_allocation(allocated_username, allocated_client):
            with locked_json(GLOBAL_TEST_FILE, {}) as tests:
                current = tests.get(str(customer.id))
                if not isinstance(current, dict) or not current.get("creation_pending_at"):
                    raise RuntimeError("Hosted test creation claim is missing")
                current["username"] = allocated_username
                current["server_id"] = getattr(allocated_client, "server_id", None)

        username, result, client = _create_user(
            plan,
            "",
            customer_id=customer.id,
            username_prefix="ht",
            on_username_allocated=persist_test_allocation,
            preferred_username=pending_username,
        )
    if result is None:
        with locked_json(GLOBAL_TEST_FILE, {}) as tests:
            current = tests.get(str(customer.id))
            if not isinstance(current, dict) or not current.get("username"):
                tests.pop(str(customer.id), None)
        bot.reply_to(message, _hosted_message(customer.id, "test_creation_failed"))
        return
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        tests[str(customer.id)].update({"username": username, "server_id": getattr(client, "server_id", None),
                                        "used_at": _now(), "creation_pending_at": None})
    _deliver_config_safely(message.chat.id, username, client)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(
        _hosted_message(customer.id, "connected_action"),
        callback_data="hb:test:connected",
    ))
    markup.add(types.InlineKeyboardButton(
        _hosted_message(customer.id, "need_help_action"),
        callback_data="hb:test:help",
    ))
    markup.add(types.InlineKeyboardButton(
        _hosted_message(customer.id, "see_plans"),
        callback_data="hb:plans",
    ))
    bot.reply_to(
        message,
        _hosted_message(customer.id, "activation_steps"),
        parse_mode="Markdown",
        reply_markup=markup,
    )
    _record_growth(
        "trial_started",
        customer.id,
        deduplication_key=f"hosted-trial-created:{OWNER_ID}:{customer.id}",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:test:"))
def test_action(call):
    action = call.data.split(":")[2]
    if action == "start":
        bot.answer_callback_query(call.id)
        free_test(call.message, customer=call.from_user)
        return
    if action == "help":
        bot.answer_callback_query(call.id)
        send_download_prompt(
            bot,
            call.message.chat.id,
            _language(call.from_user.id),
            callback_prefix="hb:download",
        )
        return
    if action == "connected":
        _update_journey_state(call.from_user.id, trial_connected_at=_now())
        _record_growth(
            "trial_activated",
            call.from_user.id,
            deduplication_key=f"hosted-trial-connected:{OWNER_ID}:{call.from_user.id}",
        )
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "activation_confirmed"),
            show_alert=True,
        )
        _show_plans(call.message.chat.id, call.from_user.id)
        return
    bot.answer_callback_query(
        call.id,
        _hosted_message(call.from_user.id, "invalid_checkout_action"),
        show_alert=True,
    )


def _referral_first_purchase_count(referrer_id):
    data = _referral_data()
    invited = {
        customer_id
        for customer_id, owner in data.get("referrals", {}).items()
        if str(owner) == str(referrer_id)
    }
    completed = {
        str(record.get("user_id"))
        for record in _tenant_payments().values()
        if isinstance(record, dict)
        and record.get("status") == "completed"
        and str(record.get("user_id")) in invited
    }
    return len(completed)


@bot.message_handler(func=lambda m: (
    m.text in _all_button_values("referral")
    or m.text in _all_hosted_values("invite_and_earn_button")
))
def referral(message):
    code = _ensure_referral_code(message.from_user.id)
    data = _referral_data()
    stats = data.get("stats", {}).get(str(message.from_user.id), {})
    wallet = data.get("wallets", {}).get(str(message.from_user.id))
    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    share_text = _hosted_message(message.from_user.id, "referral_share_text")
    share_url = f"https://t.me/share/url?url={urlquote(link)}&text={urlquote(share_text)}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton(
        _hosted_message(message.from_user.id, "share_invite"),
        url=share_url,
    ))
    markup.add(
        types.InlineKeyboardButton(
            _hosted_message(message.from_user.id, "set_wallet"),
            callback_data="hb:refwallet",
        ),
        types.InlineKeyboardButton(
            _hosted_message(message.from_user.id, "withdraw"),
            callback_data="hb:refwithdraw",
        ),
    )
    intro = _hosted_message(
        message.from_user.id,
        "referral_intro",
        percent=f"{float(get_settings(OWNER_ID)['referral_margin_percent']):g}",
    )
    progress = _hosted_message(
        message.from_user.id,
        "referral_progress",
        invited=int(stats.get("count", 0) or 0),
        buyers=_referral_first_purchase_count(message.from_user.id),
        available=format_usd_amount(stats.get("available_balance", 0)),
        earned=format_usd_amount(stats.get("total_earnings", 0)),
        wallet=_escape_markdown(
            wallet or _hosted_message(message.from_user.id, "wallet_not_set")
        ),
        link=link,
    )
    bot.reply_to(message, f"{intro}\n\n{progress}", parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data in {"hb:refwallet", "hb:refwithdraw"})
def referral_action(call):
    if call.data == "hb:refwallet":
        _set_input_state(call.from_user.id, {"kind": "referral_wallet"})
        bot.send_message(call.message.chat.id, _hosted_message(call.from_user.id, "wallet_prompt"))
        bot.answer_callback_query(call.id)
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        stats = data.get("stats", {}).get(str(call.from_user.id), {})
        amount = round(float(stats.get("available_balance", 0)), 2)
        wallet = data.get("wallets", {}).get(str(call.from_user.id))
        if amount < 2 or not wallet:
            bot.answer_callback_query(
                call.id,
                _hosted_message(call.from_user.id, "withdrawal_requirements"),
                show_alert=True,
            )
            return
        request = {"id": str(uuid.uuid4()), "user_id": str(call.from_user.id), "amount": amount,
                   "wallet": wallet, "status": "pending", "requested_at": _now()}
        data.setdefault("pending_withdrawals", []).append(request)
        stats["available_balance"] = 0.0
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(_hosted_message(OWNER_ID, "mark_paid"), callback_data=f"hb:refresolve:paid:{request['id']}"),
        types.InlineKeyboardButton(_hosted_message(OWNER_ID, "rejected"), callback_data=f"hb:refresolve:rejected:{request['id']}"),
    )
    bot.send_message(
        OWNER_ID,
        _hosted_message(
            OWNER_ID,
            "referral_request_detail",
            user_id=call.from_user.id,
            amount=f"{amount:.2f}",
            wallet=wallet,
        ),
        parse_mode="Markdown",
        reply_markup=markup,
    )
    bot.answer_callback_query(
        call.id,
        _hosted_message(call.from_user.id, "withdrawal_requested"),
        show_alert=True,
    )


@bot.message_handler(func=lambda m: (_get_input_state(m.from_user.id) or {}).get("kind") == "referral_wallet")
def referral_wallet_input(message):
    destination = (message.text or "").strip()
    if not destination or len(destination) > 500 or "\x00" in destination:
        _pop_input_state(message.from_user.id)
        bot.reply_to(message, _hosted_message(message.from_user.id, "referral_destination_invalid"))
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        data.setdefault("wallets", {})[str(message.from_user.id)] = destination
    _pop_input_state(message.from_user.id)
    bot.reply_to(message, _hosted_message(message.from_user.id, "referral_destination_saved"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:refresolve:"))
def referral_resolve(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    _, _, action, request_id = call.data.split(":")
    if action not in {"paid", "rejected"}:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "invalid_payout"), show_alert=True)
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        request = next((item for item in data.get("pending_withdrawals", []) if item.get("id") == request_id), None)
        if not request or request.get("status") != "pending":
            bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "already_processed"), show_alert=True)
            return
        request["status"] = action
        request["resolved_at"] = _now()
        if action == "rejected":
            stats = data.setdefault("stats", {}).setdefault(request["user_id"], {})
            stats["available_balance"] = round(float(stats.get("available_balance", 0)) + float(request["amount"]), 2)
        else:
            data.setdefault("payouts", []).append(dict(request))
            settle_referral_liability(OWNER_ID, request_id, request["amount"])
    bot.send_message(int(request["user_id"]),
                     _hosted_message(int(request["user_id"]), "referral_withdrawal_result", action=action))
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "action_updated"), show_alert=True)


OWNER_MENU_ROWS = (
    ("owner_setup", "owner_customers"),
    ("owner_money", "customer_view"),
)
OWNER_SETUP_ROWS = (
    ("markup", "payment_methods"),
    ("plans", "messages"),
    ("owner_home",),
)
OWNER_CUSTOMER_ROWS = (
    ("generate", "customers"),
    ("owner_home",),
)
OWNER_MONEY_ROWS = (
    ("debt", "earnings"),
    ("refpercent", "referrals"),
    ("stats",),
    ("owner_home",),
)
LEGACY_OWNER_MENU_ROWS = (
    ("generate", "customers"),
    ("debt", "markup"),
    ("card", "support"),
    ("welcome", "refpercent"),
    ("plans", "crypto"),
    ("earnings", "referrals"),
    ("back",),
)
OWNER_SETTING_KEYS = ("markup", "card", "support", "welcome", "refpercent")
OWNER_GROUP_KEYS = {"owner_setup", "owner_customers", "owner_money"}
LEGACY_OWNER_LABELS = {
    "markup": {
        "📈 Retail Markup",
        "📈 درصد سود فروش",
        "📈 Розничная наценка",
        "📈 Bölek goşma baha",
    },
}


def _owner_menu_command(text):
    rows = OWNER_MENU_ROWS + OWNER_SETUP_ROWS + OWNER_CUSTOMER_ROWS + OWNER_MONEY_ROWS + LEGACY_OWNER_MENU_ROWS
    for row in rows:
        for key in row:
            if key == "back" and text in _all_button_values("back"):
                return "customer_view", None
            if text in LEGACY_OWNER_LABELS.get(key, set()):
                return "setting", key
            if any(catalog.get(key) == text for catalog in HOSTED_TRANSLATIONS.values()):
                if key in OWNER_GROUP_KEYS:
                    return "group", key
                if key == "owner_home":
                    return "home", None
                if key == "customer_view":
                    return "customer_view", None
                return ("setting" if key in OWNER_SETTING_KEYS else "action"), key
    return None


def _owner_menu_text(user_id, key):
    if key == "back":
        return _button(user_id, "back")
    return _hosted_message(user_id, key)


def _owner_markup(user_id=OWNER_ID, rows=OWNER_MENU_ROWS):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in rows:
        markup.row(*(_owner_menu_text(user_id, key) for key in row))
    return markup


def _owner_setup_status():
    settings = get_settings(OWNER_ID)
    sellable = _sellable_plans()
    crypto_available = bool(
        os.getenv("CRYPTO_MERCHANT_ID")
        and os.getenv("CRYPTO_API_KEY")
        and sellable
        and all(_hosted_plan_quote(plan, settings)["crypto_supported"] for plan in sellable.values())
    )
    return get_setup_status(
        OWNER_ID,
        settings=settings,
        crypto_available=crypto_available,
        plans_available=bool(_owner_plan_ids()),
    )


def _setup_progress_text():
    status = _owner_setup_status()
    marks = {key: "✅" if value else "⬜" for key, value in status["steps"].items()}
    next_key = f"setup_next_{status['next_step']}" if status["next_step"] else "setup_next_done"
    text = _hosted_message(
        OWNER_ID,
        "setup_dashboard",
        completed=status["completed"],
        total=status["total"],
        pricing=marks["pricing"],
        payments=marks["payments"],
        plans=marks["plans"],
        next_step=_hosted_message(OWNER_ID, next_key),
    )
    if status["ready"]:
        text += f"\n\n{_hosted_message(OWNER_ID, 'setup_ready')}"
    return text


def _pricing_plan_items(settings):
    visible = _sellable_plans()
    if visible or settings.get("plan_selection_configured"):
        return sorted(visible.items(), key=lambda item: int(item[0]))
    plans = _load_plans()
    return [
        (plan_id, plans[plan_id])
        for plan_id in sorted(_owner_plan_ids(), key=int)
    ]


def _split_message_blocks(blocks, max_length=TELEGRAM_SAFE_TEXT_LIMIT):
    if max_length <= 0:
        raise ValueError("Message length must be positive")
    chunks = []
    current = ""
    for raw_block in blocks:
        block = str(raw_block or "").strip()
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > max_length:
            split_at = block.rfind("\n", 0, max_length + 1)
            if split_at <= 0:
                split_at = max_length
            chunks.append(block[:split_at].rstrip())
            block = block[split_at:].lstrip()
        current = block
    if current:
        chunks.append(current)
    return chunks


def _stats_period_text(label, bucket):
    methods = bucket.get("methods", {})
    card = methods.get("card", {})
    crypto = methods.get("crypto", {})
    other = methods.get("other", {})
    return _hosted_message(
        OWNER_ID,
        "stats_period",
        label=label,
        started=bucket.get("started", 0),
        completed=bucket.get("completed", 0),
        open=bucket.get("open", 0),
        attention=bucket.get("attention", 0),
        failed=bucket.get("failed", 0),
        expired=bucket.get("expired", 0),
        buyers=bucket.get("unique_buyers", 0),
        new_configs=bucket.get("new_configs", 0),
        renewals=bucket.get("renewals", 0),
        manual_configs=bucket.get("manual_configs", 0),
        card_count=card.get("completed", 0),
        card_revenue=format_usd_amount(card.get("revenue", 0)),
        crypto_count=crypto.get("completed", 0),
        crypto_revenue=format_usd_amount(crypto.get("revenue", 0)),
        other_count=other.get("completed", 0),
        other_revenue=format_usd_amount(other.get("revenue", 0)),
        revenue=format_usd_amount(bucket.get("revenue", 0)),
        gross=format_usd_amount(bucket.get("gross_profit", 0)),
        referrals=format_usd_amount(bucket.get("referral_payouts", 0)),
        net=format_usd_amount(bucket.get("net_profit", 0)),
    )


def _owner_stats_chunks(end_date=None, scheduled=False):
    report_end = end_date or datetime.now().date()
    reseller = get_reseller_data(OWNER_ID) or {}
    snapshot = build_hosted_stats(
        _tenant_payments(),
        reseller.get("configs", []),
        end_date=report_end,
        origin_bot_id=os.getenv("AJIB_HOSTED_BOT_ID"),
    )
    blocks = [
        _hosted_message(
            OWNER_ID,
            "stats_scheduled_title" if scheduled else "stats_live_title",
        ),
        _hosted_message(
            OWNER_ID,
            "stats_window",
            start_date=snapshot["start_date"],
            end_date=snapshot["end_date"],
        ),
        _hosted_message(OWNER_ID, "stats_usd_note"),
        _hosted_message(OWNER_ID, "stats_daily_section"),
    ]
    blocks.extend(_stats_period_text(day["date"], day) for day in snapshot["days"])
    blocks.extend([
        _hosted_message(OWNER_ID, "stats_last30_section"),
        _stats_period_text(
            _hosted_message(
                OWNER_ID,
                "stats_last30_label",
                start_date=snapshot["last30_start_date"],
                end_date=snapshot["last30_end_date"],
            ),
            snapshot["last30"],
        ),
    ])
    return _split_message_blocks(blocks)


def _owner_growth_comparison_text(report):
    """Render tenant-scoped aggregate funnel comparison in the owner's language."""
    def display_percent(value):
        if value is None:
            return _hosted_message(OWNER_ID, "growth_value_unavailable")
        return _hosted_message(
            OWNER_ID,
            "growth_percent_value",
            value=f"{float(value):.1f}",
        )

    def display_delta(value):
        if value is None:
            return _hosted_message(OWNER_ID, "growth_value_unavailable")
        return _hosted_message(
            OWNER_ID,
            "growth_percent_value",
            value=f"{float(value):+.1f}",
        )

    current_start = report["current_start"]
    baseline_start = report["baseline_start"]
    end_at = report["end_at"]
    blocks = [
        _hosted_message(
            OWNER_ID,
            "growth_comparison_header",
            days=int(report.get("days", 30) or 30),
            current_start=current_start.date().isoformat(),
            current_end=(end_at - timedelta(days=1)).date().isoformat(),
            baseline_start=baseline_start.date().isoformat(),
            baseline_end=(current_start - timedelta(days=1)).date().isoformat(),
        )
    ]
    labels = {
        "trial_to_paid": "growth_label_trial_to_paid",
        "checkout": "growth_label_checkout",
        "renewal": "growth_label_renewal",
        "referral": "growth_label_referral",
    }
    funnels = report.get("funnels", {})
    for funnel_name, label_key in labels.items():
        item = funnels.get(funnel_name, {})
        blocks.append(
            _hosted_message(
                OWNER_ID,
                "growth_comparison_line",
                label=_hosted_message(OWNER_ID, label_key),
                completed=int(item.get("completed", 0) or 0),
                started=int(item.get("started", 0) or 0),
                rate=display_percent(item.get("conversion_percent")),
                baseline_completed=int(item.get("baseline_completed", 0) or 0),
                baseline_started=int(item.get("baseline_started", 0) or 0),
                baseline_rate=display_percent(item.get("baseline_conversion_percent")),
                delta=display_delta(item.get("relative_change_percent")),
            )
        )
    blocks.append(_hosted_message(OWNER_ID, "growth_aggregate_only"))
    return "\n\n".join(blocks)


def _send_owner_stats(chat_id, end_date=None, scheduled=False):
    chunks = _owner_stats_chunks(end_date=end_date, scheduled=scheduled)
    for chunk in chunks:
        bot.send_message(chat_id, chunk, parse_mode="Markdown")
    sent_count = len(chunks)
    try:
        from utils.growth_reporting import hosted_growth_comparison

        report_end = end_date or datetime.now().date()
        comparison_end = datetime.combine(
            report_end + timedelta(days=1),
            datetime.min.time(),
        )
        comparison = hosted_growth_comparison(
            OWNER_ID,
            end_at=comparison_end,
            days=30,
        )
        bot.send_message(
            chat_id,
            _owner_growth_comparison_text(comparison),
            parse_mode="Markdown",
        )
        sent_count += 1
    except Exception:
        pass
    return sent_count


def _owner_stats_report_end(now=None):
    current = now or datetime.now()
    due_at = current.replace(
        hour=OWNER_STATS_SEND_HOUR,
        minute=OWNER_STATS_SEND_MINUTE,
        second=0,
        microsecond=0,
    )
    return current.date() - timedelta(days=1) if current >= due_at else None


def _claim_owner_stats_report(report_end, now=None):
    current = now or datetime.now()
    report_key = report_end.isoformat()
    with locked_json(tenant_file(OWNER_ID, "notifications.json"), {}) as notifications:
        state = notifications.get("owner_daily_stats", {})
        if not isinstance(state, dict):
            state = {}
        if state.get("last_sent_for") == report_key:
            return None
        claimed_at = _parse_time(state.get("claimed_at"))
        claim_age = (current - claimed_at).total_seconds() if claimed_at is not None else None
        claim_is_live = (
            state.get("claim_for") == report_key
            and claim_age is not None
            and 0 <= claim_age < OWNER_STATS_CLAIM_LEASE_SECONDS
        )
        if claim_is_live:
            return None
        claim_id = uuid.uuid4().hex
        notifications["owner_daily_stats"] = {
            **state,
            "claim_for": report_key,
            "claim_id": claim_id,
            "claimed_at": current.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return claim_id


def _finish_owner_stats_report(report_end, claim_id, success, now=None):
    current = now or datetime.now()
    with locked_json(tenant_file(OWNER_ID, "notifications.json"), {}) as notifications:
        state = notifications.get("owner_daily_stats", {})
        if not isinstance(state, dict) or state.get("claim_id") != claim_id:
            return False
        state.pop("claim_for", None)
        state.pop("claim_id", None)
        state.pop("claimed_at", None)
        if success:
            state["last_sent_for"] = report_end.isoformat()
            state["last_sent_at"] = current.strftime("%Y-%m-%d %H:%M:%S")
        notifications["owner_daily_stats"] = state
        return True


def _run_due_owner_stats(now=None):
    current = now or datetime.now()
    report_end = _owner_stats_report_end(current)
    if report_end is None:
        return False
    claim_id = _claim_owner_stats_report(report_end, now=current)
    if not claim_id:
        return False
    success = False
    try:
        _send_owner_stats(OWNER_ID, end_date=report_end, scheduled=True)
        success = True
        return True
    except Exception as error:
        print(
            f"Hosted owner stats delivery failed for reseller {OWNER_ID}: {type(error).__name__}",
            flush=True,
        )
        return False
    finally:
        _finish_owner_stats_report(report_end, claim_id, success, now=current)


def _pricing_overview(max_length=TELEGRAM_SAFE_TEXT_LIMIT):
    settings = get_settings(OWNER_ID)
    referral_percent = float(settings["referral_margin_percent"])
    markup_percent = float(settings["markup_percent"])
    blocks = [
        _hosted_message(
            OWNER_ID,
            "pricing_current",
            markup=f"{markup_percent:g}",
            referral=f"{referral_percent:g}",
        )
    ]
    plan_items = _pricing_plan_items(settings)
    if not plan_items:
        blocks.append(_hosted_message(OWNER_ID, "pricing_no_plans"))
    for plan_id, plan in plan_items:
        quote = _hosted_plan_quote(
            plan,
            settings,
            referral_percent,
            referred=True,
        )
        if quote["crypto_supported"]:
            crypto = _hosted_message(
                OWNER_ID,
                "pricing_crypto",
                crypto_price=format_usd_amount(quote["crypto_collected"]),
                crypto_profit=format_usd_amount(quote["crypto_margin"]),
                crypto_referral=format_usd_amount(quote["crypto_referral_reward"]),
                crypto_net=format_usd_amount(
                    quote["crypto_margin"] - quote["crypto_referral_reward"]
                ),
            )
        else:
            crypto = _hosted_message(
                OWNER_ID,
                "pricing_crypto_unavailable",
                crypto_price=format_usd_amount(quote["crypto_collected"]),
                cost=format_usd_amount(quote["wholesale"]),
            )
        blocks.append(
            _hosted_message(
                OWNER_ID,
                "pricing_plan",
                plan_gb=plan.get("gb", plan_id),
                days=plan.get("days", 30),
                catalog=format_usd_amount(quote["retail_base"]),
                cost=format_usd_amount(quote["wholesale"]),
                card_price=format_usd_amount(quote["retail"]),
                card_profit=format_usd_amount(quote["card_margin"]),
                card_referral=format_usd_amount(quote["card_referral_reward"]),
                card_net=format_usd_amount(
                    quote["card_margin"] - quote["card_referral_reward"]
                ),
                crypto=crypto,
            )
        )
    blocks.append(_hosted_message(OWNER_ID, "pricing_accounting"))
    return _split_message_blocks(blocks, max_length=max_length)


def _show_owner_dashboard(chat_id, reply_to=None):
    reseller = get_reseller_data(OWNER_ID) or {}
    present_pending_reseller_level(bot, OWNER_ID, _language(OWNER_ID))
    settings = get_settings(OWNER_ID)
    summary = _hosted_message(
        OWNER_ID,
        "owner_summary",
        markup=f"{float(settings['markup_percent']):g}",
        crypto=_hosted_message(OWNER_ID, "enabled" if settings["crypto_enabled"] else "disabled"),
        card=settings["card_number"] or _hosted_message(OWNER_ID, "not_set"),
        referral=f"{float(settings['referral_margin_percent']):g}",
    )
    text = (
        f"{_hosted_message(OWNER_ID, 'owner_title')}\n\n"
        f"{build_reseller_level_compact(_language(OWNER_ID), reseller)}\n{summary}\n\n"
        f"{_setup_progress_text()}"
    )
    kwargs = {"parse_mode": "Markdown", "reply_markup": _owner_markup()}
    if reply_to is not None:
        bot.reply_to(reply_to, text, **kwargs)
    else:
        bot.send_message(chat_id, text, **kwargs)


def _show_owner_group(chat_id, group):
    if group == "owner_setup":
        text = f"{_hosted_message(OWNER_ID, 'setup_group_intro')}\n\n{_setup_progress_text()}"
        rows = OWNER_SETUP_ROWS
    elif group == "owner_customers":
        text = _hosted_message(OWNER_ID, "customers_group_intro")
        rows = OWNER_CUSTOMER_ROWS
    else:
        text = _hosted_message(OWNER_ID, "money_group_intro")
        rows = OWNER_MONEY_ROWS
    bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=_owner_markup(rows=rows))


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and m.text in {
    catalog["owner_panel"] for catalog in HOSTED_TRANSLATIONS.values()
})
def owner_panel(message):
    _show_owner_dashboard(message.chat.id, reply_to=message)


def _begin_owner_setting(chat_id, field, customer_language=None):
    state = {"kind": "owner_setting", "field": field}
    if field in {"welcome", "support"}:
        state["customer_language"] = customer_language or _language(OWNER_ID)
    _set_input_state(OWNER_ID, state)
    markup = None
    if field == "markup":
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "pricing_keep"),
            callback_data="hb:setup:keeppricing",
        ))
        prompt = f"{_hosted_message(OWNER_ID, 'prompt_markup')}\n\n/cancel"
        overview_limit = max(1, TELEGRAM_SAFE_TEXT_LIMIT - len(prompt) - 2)
        chunks = _pricing_overview(max_length=overview_limit)
        for chunk in chunks[:-1]:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        final_text = f"{chunks[-1]}\n\n{prompt}" if chunks else prompt
        bot.send_message(
            chat_id,
            final_text,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return
    bot.send_message(
        chat_id,
        f"{_hosted_message(OWNER_ID, f'prompt_{field}')}\n\n/cancel",
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:setting:"))
def owner_setting(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    field = call.data.split(":")[2]
    if field not in OWNER_SETTING_KEYS:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "invalid_setting"), show_alert=True)
        return
    _begin_owner_setting(call.message.chat.id, field)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and _owner_menu_command(m.text) is not None)
def owner_menu_action(message):
    command, action = _owner_menu_command(message.text)
    _pop_input_state(OWNER_ID)
    if command == "group":
        _show_owner_group(message.chat.id, action)
        return
    if command == "home":
        _show_owner_dashboard(message.chat.id)
        return
    if command == "customer_view":
        bot.reply_to(
            message,
            _storefront_setting(OWNER_ID, "welcome"),
            reply_markup=_main_markup(OWNER_ID),
        )
        return
    if command == "setting":
        _begin_owner_setting(message.chat.id, action)
        return
    _handle_owner_action(message.chat.id, action, lambda text: bot.reply_to(message, text))


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and (_get_input_state(OWNER_ID) or {}).get("kind") == "owner_setting")
def owner_setting_input(message):
    state = _get_input_state(OWNER_ID)
    if not state:
        bot.reply_to(message, _hosted_message(OWNER_ID, "prompt_expired"))
        return
    field = state["field"]
    customer_language = state.get("customer_language")
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        _pop_input_state(OWNER_ID)
        bot.reply_to(message, _hosted_message(OWNER_ID, "owner_canceled"))
        _show_owner_group(message.chat.id, "owner_money" if field == "refpercent" else "owner_setup")
        return
    try:
        if field in {"markup", "refpercent"}:
            value = float(raw)
            if value < 0 or (field == "refpercent" and value > 100):
                raise ValueError
        else:
            value = raw
        if field in {"support", "welcome"} and customer_language in LANGUAGES:
            settings = get_settings(OWNER_ID)
            key = f"{field}_texts"
            localized = dict(settings.get(key, {}))
            if value:
                localized[customer_language] = value
            else:
                localized.pop(customer_language, None)
            update_settings(OWNER_ID, {key: localized})
        else:
            key = {"markup": "markup_percent", "card": "card_number",
                   "support": "support_text", "welcome": "welcome_text", "refpercent": "referral_margin_percent"}[field]
            update_settings(OWNER_ID, {key: value})
        if field == "markup":
            mark_setup_step(OWNER_ID, "pricing")
        elif field in {"support", "welcome"}:
            mark_setup_step(OWNER_ID, "messages")
        _pop_input_state(OWNER_ID)
        bot.reply_to(message, _hosted_message(OWNER_ID, "setting_updated"))
        _show_owner_group(message.chat.id, "owner_money" if field == "refpercent" else "owner_setup")
    except ValueError:
        bot.reply_to(message, f"{_hosted_message(OWNER_ID, 'invalid_value')}\n\n{_hosted_message(OWNER_ID, f'prompt_{field}')}\n\n/cancel")


def _owner_plan_ids():
    result = set()
    for plan_id, plan in _load_plans().items():
        plan_key = str(plan_id).strip()
        if (
            not isinstance(plan, dict)
            or not plan_key.isdigit()
            or len(plan_key) > 12
            or plan.get("target", "both") == "customer"
        ):
            continue
        try:
            price = float(plan["price"])
            days = int(plan.get("days", 30))
            gigabytes = int(plan.get("gb", plan_key))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0 and 0 < days <= 3650 and gigabytes > 0:
            result.add(plan_key)
    return result


def _owner_plans_markup(settings=None):
    settings = settings or get_settings(OWNER_ID)
    all_ids = _owner_plan_ids()
    enabled = (
        {str(item) for item in settings.get("enabled_plan_ids", [])}
        if settings.get("plan_selection_configured") else set(all_ids)
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    recommended = str(settings.get("recommended_plan_id") or "")
    for plan_id in sorted(all_ids, key=int):
        markup.row(
            types.InlineKeyboardButton(
                f"{'✅' if plan_id in enabled else '❌'} {plan_id} GB",
                callback_data=f"hb:plantoggle:{plan_id}",
            ),
            types.InlineKeyboardButton(
                f"{'⭐' if plan_id == recommended else '☆'} {_hosted_message(OWNER_ID, 'recommend_plan')}",
                callback_data=f"hb:planrecommend:{plan_id}",
            ),
        )
    markup.add(types.InlineKeyboardButton(
        _hosted_message(OWNER_ID, "plans_done"), callback_data="hb:plansdone"
    ))
    return markup


def _handle_owner_action(chat_id, action, feedback):
    settings = get_settings(OWNER_ID)
    if action == "stats":
        _send_owner_stats(chat_id)
        return
    if action == "generate":
        if not _reseller(active_only=True):
            feedback(_hosted_message(OWNER_ID, "generation_suspended"))
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
            pricing = _reseller_plan_pricing(plan)
            markup.add(types.InlineKeyboardButton(
                _hosted_message(
                    OWNER_ID,
                    "owner_plan_button",
                    plan_gb=plan_id,
                    days=plan.get("days", 30),
                    price=f"{pricing['wholesale_price']:.2f}",
                    discount=f"{pricing['discount_percent']:.0f}",
                ),
                callback_data=f"hb:ogen:{plan_id}",
            ))
        bot.send_message(
            chat_id,
            _hosted_message(OWNER_ID, "owner_select_wholesale_plan"),
            reply_markup=markup,
        )
        return
    if action == "customers":
        reseller = get_reseller_data(OWNER_ID) or {}
        configs = [item for item in reseller.get("configs", []) if isinstance(item, dict) and not item.get("removed_from_vpn")]
        lines = [_hosted_message(OWNER_ID, "owner_customers_header"), ""]
        for item in configs[-30:]:
            label = (
                item.get("customer_name") or item.get("customer_telegram_username")
                or item.get("customer_telegram_id") or _hosted_message(OWNER_ID, "owner_manual_customer")
            )
            lines.append(f"• {item.get('username', '?')} · {label} · {item.get('plan_gb', item.get('gb', '?'))} GB")
        bot.send_message(
            chat_id,
            "\n".join(lines) if configs else _hosted_message(OWNER_ID, "owner_no_customers"),
        )
        return
    if action == "debt":
        reseller = get_reseller_data(OWNER_ID) or {}
        total_paid = get_reseller_total_paid(reseller)
        limit = get_reseller_trust_limit(total_paid)
        _, _, available = can_reseller_add_debt(reseller, 0)
        bot.send_message(
            chat_id,
            f"{build_reseller_level_compact(_language(OWNER_ID), reseller)}\n"
            + _hosted_message(
                OWNER_ID,
                "owner_debt_summary",
                debt=f"{float(reseller.get('debt', 0)):.2f}",
                limit=f"{limit:.2f}",
                available=f"{available:.2f}",
            ),
            parse_mode="Markdown",
        )
        return
    if action == "payment_methods":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "set_card"), callback_data="hb:payment:card"
        ))
        if settings.get("card_number"):
            markup.add(types.InlineKeyboardButton(
                _hosted_message(OWNER_ID, "remove_card"), callback_data="hb:payment:remove"
            ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "toggle_crypto"), callback_data="hb:payment:crypto"
        ))
        bot.send_message(
            chat_id,
            _hosted_message(
                OWNER_ID,
                "payment_summary",
                card_status=(settings.get("card_number") or _hosted_message(OWNER_ID, "disabled")),
                crypto_status=_hosted_message(
                    OWNER_ID, "enabled" if settings.get("crypto_enabled") else "disabled"
                ),
            ),
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return
    if action == "crypto":
        target = not settings.get("crypto_enabled")
        if target:
            if not os.getenv("CRYPTO_MERCHANT_ID") or not os.getenv("CRYPTO_API_KEY"):
                feedback(_hosted_message(OWNER_ID, "crypto_gateway_missing"))
                return
            unsupported = [
                pid
                for pid, plan in _sellable_plans().items()
                if not _hosted_plan_quote(plan, settings)["crypto_supported"]
            ]
            if unsupported:
                feedback(_hosted_message(OWNER_ID, "crypto_markup_low"))
                return
        update_settings(OWNER_ID, {"crypto_enabled": target})
        feedback(_hosted_message(
            OWNER_ID,
            "crypto_changed",
            state=_hosted_message(OWNER_ID, "enabled" if target else "disabled"),
        ))
        return
    if action == "plans":
        bot.send_message(
            chat_id,
            _hosted_message(OWNER_ID, "plans_select"),
            reply_markup=_owner_plans_markup(settings),
        )
        return
    if action == "messages":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "welcome"), callback_data="hb:messages:welcome"
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "support"), callback_data="hb:messages:support"
        ))
        markup.add(types.InlineKeyboardButton(
            _hosted_message(OWNER_ID, "messages_keep"), callback_data="hb:messages:done"
        ))
        bot.send_message(
            chat_id,
            _hosted_message(
                OWNER_ID,
                "messages_summary",
                welcome=settings.get("welcome_text") or _hosted_message(OWNER_ID, "welcome_default"),
                support=settings.get("support_text") or _hosted_message(OWNER_ID, "support_default"),
            ),
            reply_markup=markup,
        )
        return
    if action == "earnings":
        ledger = get_ledger(OWNER_ID)
        reseller = get_reseller_data(OWNER_ID) or {}
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(_hosted_message(OWNER_ID, "apply_earnings"), callback_data="hb:earn:settle"),
            types.InlineKeyboardButton(_hosted_message(OWNER_ID, "request_withdrawal"), callback_data="hb:earn:withdraw"),
        )
        bot.send_message(
            chat_id,
            _hosted_message(
                OWNER_ID,
                "owner_earnings_summary",
                available=f"{ledger['earnings_available']:.2f}",
                reserved=f"{ledger['earnings_reserved']:.2f}",
                debt=f"{float(reseller.get('debt', 0)):.2f}",
                liability=f"{ledger['referral_liability']:.2f}",
            ),
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return
    if action == "referrals":
        pending = [item for item in _referral_data().get("pending_withdrawals", []) if item.get("status") == "pending"]
        feedback(_hosted_message(OWNER_ID, "pending_referrals", count=len(pending)))
        for request in pending:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(_hosted_message(OWNER_ID, "mark_paid"), callback_data=f"hb:refresolve:paid:{request['id']}"),
                types.InlineKeyboardButton(_hosted_message(OWNER_ID, "rejected"), callback_data=f"hb:refresolve:rejected:{request['id']}"),
            )
            bot.send_message(
                chat_id,
                _hosted_message(
                    OWNER_ID,
                    "referral_request_detail",
                    user_id=request["user_id"],
                    amount=f"{request['amount']:.2f}",
                    wallet=request["wallet"],
                ),
                parse_mode="Markdown",
                reply_markup=markup,
            )


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:setup:"))
def owner_setup_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    if call.data == "hb:setup:keeppricing":
        _pop_input_state(OWNER_ID)
        mark_setup_step(OWNER_ID, "pricing")
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "setting_updated"))
        _show_owner_group(call.message.chat.id, "owner_setup")
        return
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "invalid_setting"), show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:payment:"))
def owner_payment_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    action = call.data.split(":")[2]
    if action == "card":
        _begin_owner_setting(call.message.chat.id, "card")
        bot.answer_callback_query(call.id)
        return
    if action == "remove":
        update_settings(OWNER_ID, {"card_number": ""})
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "card_removed"), show_alert=True)
    elif action == "crypto":
        _handle_owner_action(
            call.message.chat.id,
            "crypto",
            lambda text: bot.answer_callback_query(call.id, text, show_alert=True),
        )
    else:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "invalid_setting"), show_alert=True)
        return
    _handle_owner_action(call.message.chat.id, "payment_methods", lambda _text: None)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:messages:"))
def owner_messages_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    parts = call.data.split(":")
    action = parts[2]
    if action in {"welcome", "support"} and len(parts) == 3:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(*[
            types.InlineKeyboardButton(
                name,
                callback_data=f"hb:messages:{action}:{code}",
            )
            for code, name in LANGUAGES.items()
        ])
        bot.send_message(
            call.message.chat.id,
            _hosted_message(OWNER_ID, "owner_message_language"),
            reply_markup=markup,
        )
        bot.answer_callback_query(call.id)
        return
    if action in {"welcome", "support"} and len(parts) == 4 and parts[3] in LANGUAGES:
        _begin_owner_setting(call.message.chat.id, action, customer_language=parts[3])
        bot.answer_callback_query(call.id)
        return
    if action == "done":
        mark_setup_step(OWNER_ID, "messages")
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "messages_confirmed"), show_alert=True)
        _show_owner_group(call.message.chat.id, "owner_setup")
        return
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "invalid_setting"), show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:owner:"))
def owner_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    callback_answered = False

    def feedback(text):
        nonlocal callback_answered
        bot.answer_callback_query(call.id, text, show_alert=True)
        callback_answered = True

    _handle_owner_action(call.message.chat.id, call.data.split(":")[2], feedback)
    if not callback_answered:
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:ogen:"))
def owner_generate_plan(call):
    if call.from_user.id != OWNER_ID or not _reseller(active_only=True):
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "generation_suspended"), show_alert=True)
        return
    plan_id = call.data.split(":")[2]
    if plan_id not in _sellable_plans():
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "plan_unavailable"), show_alert=True)
        return
    _set_input_state(OWNER_ID, {"kind": "owner_generate", "plan_id": plan_id})
    bot.send_message(call.message.chat.id, _hosted_message(OWNER_ID, "owner_generate_label"))
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and (_get_input_state(OWNER_ID) or {}).get("kind") == "owner_generate")
def owner_generate_input(message):
    state = _get_input_state(OWNER_ID)
    if not state:
        bot.reply_to(message, _hosted_message(OWNER_ID, "prompt_expired"))
        return
    if (message.text or "").strip().lower() == "/cancel":
        _pop_input_state(OWNER_ID)
        bot.reply_to(message, _hosted_message(OWNER_ID, "owner_canceled"))
        _show_owner_group(message.chat.id, "owner_customers")
        return
    _pop_input_state(OWNER_ID)
    plan_id = state["plan_id"]
    plan = _sellable_plans().get(plan_id)
    label = (message.text or "customer").strip()[:64] or "customer"
    reseller = get_reseller_data(OWNER_ID) or {}
    pricing = _reseller_plan_pricing(plan, reseller) if plan else None
    _, _, available = can_reseller_add_debt(reseller, 0)
    reservation_id = f"manual-{uuid.uuid4()}"
    if not plan or not reserve_credit(
        OWNER_ID,
        reservation_id,
        pricing["wholesale_price"],
        available,
    ):
        bot.reply_to(message, _hosted_message(OWNER_ID, "insufficient_credit"))
        return
    username, result, client = _create_user({"gb": plan_id, "days": plan.get("days", 30),
                                             "unlimited": plan.get("unlimited", False)}, label)
    if result is None:
        release_credit(OWNER_ID, reservation_id)
        bot.reply_to(message, _hosted_message(OWNER_ID, "vpn_creation_failed"))
        return
    config = {"username": username, "customer_name": label, "server_id": getattr(client, "server_id", None),
              "reseller_id": str(OWNER_ID), "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
              "plan_gb": plan_id, "days": plan.get("days", 30),
              "price": pricing["wholesale_price"], "list_price": pricing["list_price"],
              "reseller_level": pricing["reseller_level"],
              "discount_percent": pricing["discount_percent"],
              "retail_order_id": reservation_id}
    if not consume_credit(OWNER_ID, reservation_id, config):
        client.delete_user(username)
        bot.reply_to(message, _hosted_message(OWNER_ID, "accounting_failed"))
        return
    _deliver_config(message.chat.id, username, client, include_downloads=False)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:plantoggle:"))
def plan_toggle(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    plan_id = call.data.split(":")[2]
    settings = get_settings(OWNER_ID)
    all_ids = _owner_plan_ids()
    if plan_id not in all_ids:
        bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "plan_unavailable"), show_alert=True)
        return
    enabled = set(settings.get("enabled_plan_ids", [])) if settings.get("plan_selection_configured") else set(all_ids)
    enabled.symmetric_difference_update({plan_id})
    update_settings(OWNER_ID, {"enabled_plan_ids": sorted(enabled), "plan_selection_configured": True})
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "action_updated"))
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_owner_plans_markup(),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:planrecommend:"))
def plan_recommend(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(
            call.id,
            _hosted_message(call.from_user.id, "owner_only"),
            show_alert=True,
        )
        return
    plan_id = call.data.split(":")[2]
    if plan_id not in _owner_plan_ids():
        bot.answer_callback_query(
            call.id,
            _hosted_message(OWNER_ID, "plan_unavailable"),
            show_alert=True,
        )
        return
    update_settings(OWNER_ID, {"recommended_plan_id": plan_id})
    bot.answer_callback_query(
        call.id,
        _hosted_message(OWNER_ID, "recommended_plan_updated"),
        show_alert=True,
    )
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_owner_plans_markup(),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "hb:plansdone")
def owner_plans_done(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    settings = get_settings(OWNER_ID)
    if not settings.get("plan_selection_configured"):
        update_settings(
            OWNER_ID,
            {"enabled_plan_ids": sorted(_owner_plan_ids(), key=int), "plan_selection_configured": True},
        )
    mark_setup_step(OWNER_ID, "plans")
    bot.answer_callback_query(call.id, _hosted_message(OWNER_ID, "setting_updated"), show_alert=True)
    _show_owner_group(call.message.chat.id, "owner_setup")


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:earn:"))
def earnings_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    action = call.data.split(":")[2]
    if action == "settle":
        success, result = transfer_earnings_to_debt(OWNER_ID)
        if success:
            present_pending_reseller_level(
                bot,
                OWNER_ID,
                _language(OWNER_ID),
                allow_introduction=False,
            )
        bot.answer_callback_query(
            call.id,
            _hosted_message(OWNER_ID, "action_updated") if success else str(result),
            show_alert=True,
        )
        return
    _set_input_state(OWNER_ID, {"kind": "earnings_destination"})
    bot.send_message(call.message.chat.id, _hosted_message(OWNER_ID, "withdraw_destination"))
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda m: m.from_user.id == OWNER_ID and (_get_input_state(OWNER_ID) or {}).get("kind") == "earnings_destination")
def earnings_destination(message):
    if (message.text or "").strip().lower() == "/cancel":
        _pop_input_state(OWNER_ID)
        bot.reply_to(message, _hosted_message(OWNER_ID, "owner_canceled"))
        _show_owner_group(message.chat.id, "owner_money")
        return
    _pop_input_state(OWNER_ID)
    success, result = request_earnings_withdrawal(OWNER_ID, (message.text or "").strip())
    bot.reply_to(
        message,
        _hosted_message(OWNER_ID, "withdraw_requested", amount=f"{result['amount']:.2f}")
        if success else str(result),
        parse_mode="Markdown",
    )


def _sync_hosted_renewal_event(event):
    status = event.get("status")
    if status == "waiting":
        status = "reserved"
    fields = {}
    if status == "attention":
        lookup_result = event.get("lookup_result") or (event.get("result") or {}).get("lookup_result") or {}
        fields = {
            "renewal_attention_reason": event.get("reason"),
            "renewal_last_error": event.get("reason"),
            "renewal_api_error": lookup_result.get("error"),
            "renewal_api_http_status": lookup_result.get("http_status"),
        }
    elif status == "applied":
        result = event.get("result") or {}
        fields = {
            "before_state": result.get("before_state"),
            "after_state": result.get("after_state"),
        }
    return sync_reseller_renewal_reservation(
        OWNER_ID,
        event.get("payment_id"),
        status,
        fields=fields,
    )


def _hosted_renewal_review_markup(payment_id, reason):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if reason == "external_renewal":
        markup.add(
            types.InlineKeyboardButton("Keep for next expiry", callback_data=f"hb:rr:wait:{payment_id}"),
            types.InlineKeyboardButton("Apply now", callback_data=f"hb:rr:apply:{payment_id}"),
        )
    elif reason == "server_unavailable":
        markup.add(
            types.InlineKeyboardButton("Retry now", callback_data=f"hb:rr:retry:{payment_id}"),
        )
    else:
        markup.add(
            types.InlineKeyboardButton("Retry now", callback_data=f"hb:rr:retry:{payment_id}"),
            types.InlineKeyboardButton("Apply now", callback_data=f"hb:rr:apply:{payment_id}"),
        )
    return markup


def _handle_hosted_renewal_event(event):
    from utils.renewal import mark_payment_renewal_alerted
    from utils.reseller import mark_reseller_renewal_alerted

    if not event:
        return
    _sync_hosted_renewal_event(event)
    record = event.get("record") or {}
    customer_id = record.get("user_id")
    if event.get("status") == "applied":
        result = event.get("result") or {}
        _deliver_config_safely(
            int(customer_id),
            result.get("username") or record.get("renew_username"),
            event.get("api_client") or result.get("api_client"),
            renewed=True,
        )
        return
    buyer_alert_due = event.get("buyer_alert_due", event.get("alert_due", False))
    operator_alert_due = event.get("operator_alert_due", event.get("alert_due", False))
    if event.get("status") != "attention" or (not buyer_alert_due and not operator_alert_due):
        return
    reason = event.get("reason") or "renewal_reset_failed"
    username = record.get("renew_username") or record.get("username") or "unknown"
    customer_reason = _message(int(customer_id), reason) if customer_id is not None else reason
    if customer_reason == reason:
        customer_reason = reason
    if buyer_alert_due:
        try:
            message_key = "renewal_reserved_server_unavailable" if reason == "server_unavailable" else "renewal_reserved_attention"
            message = _hosted_message(
                int(customer_id),
                message_key,
                username=username,
                reason=customer_reason,
            )
            bot.send_message(int(customer_id), message, parse_mode="Markdown")
        except Exception:
            pass
        mark_payment_renewal_alerted(
            event["payment_id"],
            payments_file=tenant_file(OWNER_ID, "payments.json"),
            audience="buyer",
        )
        mark_reseller_renewal_alerted(OWNER_ID, event["payment_id"], audience="buyer")
    if operator_alert_due:
        try:
            owner_reason = _message(OWNER_ID, reason)
            if owner_reason == reason:
                owner_reason = reason
            server_id = record.get("server_id") or record.get("renewal_server_id") or "unknown"
            bot.send_message(
                OWNER_ID,
                f"Reserved renewal needs attention.\nCustomer: `{customer_id}`\nConfig: `{username}`\nServer: `{server_id}`\nReason: {owner_reason}",
                reply_markup=_hosted_renewal_review_markup(event["payment_id"], reason),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        mark_payment_renewal_alerted(
            event["payment_id"],
            payments_file=tenant_file(OWNER_ID, "payments.json"),
            audience="operator",
        )
        mark_reseller_renewal_alerted(OWNER_ID, event["payment_id"], audience="operator")


def _process_hosted_reserved_renewals(now=None):
    from utils.renewal import list_payment_renewal_ids, process_payment_renewal_reservation

    payments_file = tenant_file(OWNER_ID, "payments.json")
    events = []
    for payment_id in list_payment_renewal_ids(payments_file=payments_file):
        try:
            event = process_payment_renewal_reservation(
                payment_id,
                payments_file=payments_file,
                now=now,
            )
            if event:
                events.append(event)
                _handle_hosted_renewal_event(event)
        except Exception as error:
            print(
                f"Hosted reserved renewal failed for reseller {OWNER_ID}, payment {payment_id}: {type(error).__name__}",
                flush=True,
            )

    # Recover a crash after payment fulfillment but before reseller-history sync.
    for payment_id, record in _tenant_payments().items():
        if not isinstance(record, dict) or record.get("renewal_mode") != "reserved":
            continue
        if record.get("renewal_status") == "applied":
            sync_reseller_renewal_reservation(
                OWNER_ID,
                payment_id,
                "applied",
                fields={
                    "before_state": record.get("renewal_before_state"),
                    "after_state": record.get("renewal_after_state"),
                },
            )
    return events


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:rr:"))
def hosted_renewal_review(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "owner_only"), show_alert=True)
        return
    parts = call.data.split(":", 3)
    if len(parts) != 4 or parts[2] not in {"wait", "retry", "apply"}:
        bot.answer_callback_query(call.id, "Invalid renewal review action.", show_alert=True)
        return
    action, payment_id = parts[2], parts[3]
    try:
        from utils.renewal import (
            capture_user_state,
            lookup_renewal_user,
            process_payment_renewal_reservation,
            refresh_payment_renewal_baseline,
        )
        from utils.reseller import refresh_reseller_renewal_baseline

        payments_file = tenant_file(OWNER_ID, "payments.json")
        record = _tenant_payments().get(payment_id, {})
        if action == "wait":
            client, live, _lookup_result = lookup_renewal_user(
                MultiServerAPI(),
                record.get("renew_username"),
                server_id=record.get("server_id"),
            )
            success = bool(live) and refresh_payment_renewal_baseline(
                payment_id,
                live,
                payments_file=payments_file,
            )
            if success:
                refresh_reseller_renewal_baseline(
                    OWNER_ID,
                    payment_id,
                    capture_user_state(live),
                )
        else:
            event = process_payment_renewal_reservation(
                payment_id,
                payments_file=payments_file,
                force=True,
                force_apply=action == "apply",
            )
            success = bool(event)
            if event:
                _handle_hosted_renewal_event(event)
        bot.answer_callback_query(
            call.id,
            "Renewal reservation updated." if success else "Renewal reservation could not be updated.",
            show_alert=True,
        )
    except Exception:
        bot.answer_callback_query(call.id, "Renewal review failed.", show_alert=True)


def _crypto_monitor():
    while True:
        try:
            _recover_stale_payment_claims()
            _recover_saved_receipts()
            _reconcile_credit_reservations()
            _reconcile_invite_discount_reservations()
            _process_hosted_reserved_renewals()
            for payment_id, current in _tenant_payments().items():
                if not isinstance(current, dict):
                    continue
                status = current.get("status")
                created = _parse_time(current.get("created_at")) or datetime.min
                age = datetime.now() - created
                if status in {"waiting_receipt", "pending_approval", "pending"}:
                    if (
                        REMINDERS_ENABLED
                        and age >= timedelta(minutes=30)
                        and not current.get("abandoned_reminder_at")
                        and status in {"waiting_receipt", "pending"}
                    ):
                        markup = types.InlineKeyboardMarkup(row_width=1)
                        if status == "pending" and current.get("payment_url"):
                            markup.add(types.InlineKeyboardButton(
                                get_button_text(_language(current["user_id"]), "payment_link"),
                                url=current["payment_url"],
                            ))
                            markup.add(types.InlineKeyboardButton(
                                get_button_text(_language(current["user_id"]), "check_status"),
                                callback_data=f"hb:check:{payment_id}",
                            ))
                        markup.add(types.InlineKeyboardButton(
                            _button(current["user_id"], "support"),
                            callback_data="hb:support",
                        ))
                        try:
                            bot.send_message(
                                current["user_id"],
                                _hosted_message(
                                    current["user_id"],
                                    "checkout_reminder",
                                    gb=current.get("plan_gb", ""),
                                ),
                                reply_markup=markup,
                            )
                            _save_payment(payment_id, {"abandoned_reminder_at": _now()})
                        except Exception:
                            pass
                    if age >= timedelta(hours=24) and status in {"waiting_receipt", "pending_approval"}:
                        claimed = _claim_payment(
                            payment_id,
                            {"waiting_receipt", "pending_approval"},
                        )
                        if claimed:
                            release_credit(OWNER_ID, payment_id, kind="credit_expired")
                            _release_invite_discount(current.get("user_id"), payment_id)
                            _save_payment(payment_id, {"status": "expired"})
                            try:
                                bot.send_message(
                                    current["user_id"],
                                    _hosted_message(current["user_id"], "pending_order_expired"),
                                )
                            except Exception:
                                pass
                        continue
                    if status == "pending_approval":
                        _notify_owner_of_receipt(payment_id)
                        continue
                    if status == "waiting_receipt":
                        continue
                if current.get("status") not in {"pending", "paid_provision_failed"} or not current.get("gateway_payment_id"):
                    continue
                record = _claim_payment(payment_id, {"pending", "paid_provision_failed"})
                if not record:
                    continue
                retry_status = record.get("processing_from_status")
                if retry_status not in {"pending", "paid_provision_failed"}:
                    retry_status = "pending"
                try:
                    response = CryptoPayment().check_payment_status(record["gateway_payment_id"])
                except Exception as error:
                    _save_payment(
                        payment_id,
                        {"status": retry_status, "last_error": f"Gateway status failed: {type(error).__name__}"},
                    )
                    continue
                result = response.get("result", {}) if isinstance(response, dict) else {}
                status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
                if str(status).lower() != "paid":
                    normalized_status = str(status or "").lower()
                    if normalized_status in {"expired", "failed", "canceled", "cancelled"}:
                        _release_invite_discount(record.get("user_id"), payment_id)
                        _save_payment(payment_id, {"status": "expired"})
                        try:
                            bot.send_message(
                                record["user_id"],
                                _hosted_message(record["user_id"], "pending_order_expired"),
                            )
                        except Exception:
                            pass
                    else:
                        _save_payment(payment_id, {"status": retry_status})
                    continue
                success, detail = _provision_claimed_payment(
                    payment_id, record, funded=True, retry_status="paid_provision_failed"
                )
                if not success:
                    bot.send_message(OWNER_ID, f"⚠️ Paid order `{payment_id}` needs retry: {detail}", parse_mode="Markdown")
        except Exception as error:
            print(f"Hosted crypto monitor failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
        time.sleep(300)


def _customer_notification_monitor():
    while True:
        try:
            reseller = get_reseller_data(OWNER_ID) or {}
            with locked_json(tenant_file(OWNER_ID, "notifications.json"), {}) as sent:
                for config in reseller.get("configs", []):
                    if not isinstance(config, dict) or not config.get("customer_telegram_id") or config.get("removed_from_vpn"):
                        continue
                    username = config.get("username")
                    client, live = MultiServerAPI().find_user(username, preferred_server_id=config.get("server_id"))
                    if not client or not live:
                        continue
                    user_id = int(config["customer_telegram_id"])
                    customer_configs = _find_customer_configs(user_id)
                    customer_index = next(
                        (
                            index for index, candidate in enumerate(customer_configs)
                            if candidate.get("username") == config.get("username")
                            and candidate.get("server_id") == config.get("server_id")
                        ),
                        None,
                    )
                    service_cycle = _hosted_service_cycle(config)
                    account = inspect_account(
                        live,
                        cycle=service_cycle,
                        source="hosted_notification",
                    )
                    if (
                        account.panel_state == PanelState.UNKNOWN
                        or account.entitlement_state == EntitlementState.UNKNOWN
                    ):
                        continue
                    expiration = account.entitlement_days_remaining
                    cycle_marker = service_cycle.fingerprint
                    is_expired = (
                        account.panel_state == PanelState.BLOCKED
                        or account.entitlement_state == EntitlementState.EXPIRED
                    )
                    if is_expired:
                        from utils.renewal import find_reseller_reservation

                        if find_reseller_reservation(config):
                            continue
                    if is_expired and sent.get(f"expired:{username}") != cycle_marker:
                        markup = types.InlineKeyboardMarkup()
                        if customer_index is not None:
                            markup.add(types.InlineKeyboardButton(
                                _hosted_message(user_id, "renew_action"),
                                callback_data=f"hb:renewcfg:{customer_index}",
                            ))
                        bot.send_message(
                            user_id,
                            _hosted_message(user_id, "expired_alert", username=username),
                            parse_mode="Markdown",
                            reply_markup=markup,
                        )
                        sent[f"expired:{username}"] = cycle_marker
                    if is_expired:
                        continue
                    maximum = float(live.get("max_download_bytes", 0) or 0)
                    used = float(live.get("upload_bytes", 0) or 0) + float(live.get("download_bytes", 0) or 0)
                    progress = []
                    if maximum > 0:
                        progress.append((int((used / maximum) * 100), "traffic"))
                    plan_days = service_cycle.duration_days
                    if plan_days > 0 and expiration is not None:
                        progress.append((int((1 - min(expiration, plan_days) / plan_days) * 100), "time"))
                    percent, basis = max(progress, default=(0, "traffic"), key=lambda item: item[0])
                    percent = max(0, min(100, percent))
                    threshold = 90 if percent >= 90 else 80 if percent >= 80 else None
                    alert_key = f"allowance:{username}:{cycle_marker}"
                    if threshold and int(sent.get(alert_key, 0) or 0) < threshold:
                        markup = types.InlineKeyboardMarkup()
                        if customer_index is not None:
                            markup.add(types.InlineKeyboardButton(
                                _hosted_message(user_id, "reserve_renewal_action"),
                                callback_data=f"hb:renewcfg:{customer_index}",
                            ))
                        bot.send_message(
                            user_id,
                            _hosted_message(
                                user_id,
                                "usage_alert",
                                username=username,
                                percent=percent,
                                basis=_hosted_message(user_id, f"usage_basis_{basis}"),
                            ),
                            parse_mode="Markdown",
                            reply_markup=markup,
                        )
                        sent[alert_key] = threshold
                        _record_growth(
                            "renewal_prompted",
                            user_id,
                            plan=config.get("plan_gb") or config.get("gb"),
                            deduplication_key=f"hosted-renewal-alert:{OWNER_ID}:{username}:{cycle_marker}:{threshold}",
                        )
        except Exception as error:
            print(f"Hosted notification monitor failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
        time.sleep(7200)


def _owner_stats_monitor():
    while True:
        try:
            _run_due_owner_stats()
        except Exception as error:
            print(
                f"Hosted owner stats monitor failed for reseller {OWNER_ID}: {type(error).__name__}",
                flush=True,
            )
        time.sleep(OWNER_STATS_MONITOR_INTERVAL_SECONDS)


def run():
    try:
        bot.get_me()
    except Exception as error:
        set_bot_runtime_status(OWNER_ID, "error", f"Telegram authentication failed: {type(error).__name__}")
        raise SystemExit(2)
    set_bot_runtime_status(OWNER_ID, "active")
    _recover_stale_payment_claims()
    _recover_saved_receipts()
    _reconcile_credit_reservations()
    _reconcile_invite_discount_reservations()
    threading.Thread(target=_crypto_monitor, daemon=True, name="hosted-crypto").start()
    threading.Thread(target=_customer_notification_monitor, daemon=True, name="hosted-notifications").start()
    threading.Thread(target=_owner_stats_monitor, daemon=True, name="hosted-owner-stats").start()
    retry = 3
    while True:
        try:
            bot.polling(none_stop=False, timeout=25, long_polling_timeout=25, skip_pending=False)
            retry = 3
        except Exception as error:
            set_bot_runtime_status(OWNER_ID, "error", f"Telegram polling failed: {type(error).__name__}")
            print(f"Hosted bot polling failed for reseller {OWNER_ID}: {type(error).__name__}", flush=True)
            time.sleep(retry)
            retry = min(60, retry * 2)
            set_bot_runtime_status(OWNER_ID, "active")


if __name__ == "__main__":
    run()
