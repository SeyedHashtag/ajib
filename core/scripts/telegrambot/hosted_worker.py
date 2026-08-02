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
from utils import database
from utils.atomic_store import locked_json, read_json
from utils.currency_format import format_toman_amount, format_usd_amount
from utils.exchange_rate import get_exchange_rate
from utils.hosted_bots import (
    add_referral_liability, calculate_quote, consume_credit,
    consume_renewal_credit, credit_crypto_sale, get_ledger, get_settings, get_setup_status, get_token,
    mark_setup_step,
    release_credit, release_stale_credit_reservations, request_earnings_withdrawal, reserve_credit,
    set_bot_runtime_status, settle_referral_liability, tenant_file, transfer_earnings_to_debt,
    update_settings,
)
from utils.hosted_translations import HOSTED_TRANSLATIONS, hosted_text
from utils.hosted_stats import build_hosted_stats
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

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=4)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


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


def _hosted_plan_quote(plan, settings, referral_margin_percent=0, referred=False):
    pricing = _reseller_plan_pricing(plan)
    quote = calculate_quote(
        pricing["wholesale_price"],
        settings["markup_percent"],
        referral_margin_percent,
        referred,
        retail_base=pricing["list_price"],
    )
    return {**quote, **pricing}


def _languages():
    return read_json(tenant_file(OWNER_ID, "languages.json"), {})


def _language(user_id):
    return _languages().get(str(user_id), "en")


def _set_language(user_id, value):
    with locked_json(tenant_file(OWNER_ID, "languages.json"), {}) as languages:
        languages[str(user_id)] = value


def _button(user_id, key, fallback):
    return BUTTON_TRANSLATIONS.get(_language(user_id), BUTTON_TRANSLATIONS["en"]).get(key, fallback)


def _message(user_id, key):
    return get_message_text(_language(user_id), key)


def _hosted_message(recipient_id, key, **values):
    return hosted_text(_language(recipient_id), key, **values)


def _all_button_values(key, fallback):
    return {items.get(key, fallback) for items in BUTTON_TRANSLATIONS.values()}


def _main_markup(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(_button(user_id, "my_configs", "📱 My Configs"), _button(user_id, "purchase_plan", "💳 Purchase Plan"))
    markup.row(_button(user_id, "downloads", "⬇️ Downloads"), _button(user_id, "test_config", "🎁 Test Config"))
    markup.row(_button(user_id, "referral", "💰 Earn Crypto"), _button(user_id, "support", "📞 Support"))
    markup.row(_button(user_id, "language", "🌐 Language/زبان"))
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


def _resolve_hosted_renewal_checkout(user_id, plan_id, renewal):
    from utils.renewal import find_reseller_renewal_offer

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
    client, live = MultiServerAPI().find_user(
        config.get("username"),
        preferred_server_id=config.get("server_id"),
    )
    offer = find_reseller_renewal_offer(
        OWNER_ID,
        config_index,
        client,
        live,
        _sellable_plans(),
        reseller_data=reseller,
        allow_reservation=True,
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
        "pending_withdrawals": [], "payouts": [],
    })


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
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        key = str(user_id)
        referrer = data.setdefault("codes", {}).get(code)
        if not referrer or referrer == key or key in data.setdefault("referrals", {}):
            return False
        data["referrals"][key] = referrer
        stats = data.setdefault("stats", {}).setdefault(referrer, {"count": 0, "total_earnings": 0.0, "available_balance": 0.0})
        stats["count"] = int(stats.get("count", 0)) + 1
        return True


def _credit_referral(order_id, customer_id, reward):
    if float(reward or 0) <= 0:
        return 0.0
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
    transaction = (
        database.write_transaction(operation="hosted_sale_referral_accounting")
        if os.getenv("AJIB_SQLITE_ACTIVE") == "1"
        else nullcontext()
    )
    with transaction:
        if funded:
            credit_crypto_sale(
                OWNER_ID,
                payment_id,
                margin,
                reward,
                metadata,
            )
        else:
            add_referral_liability(
                OWNER_ID,
                payment_id,
                reward,
                metadata,
            )
        return _credit_referral(payment_id, customer_id, reward)


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


def _settle_hosted_reserved_renewal(payment_id, record, funded):
    from utils.renewal import mark_payment_renewal_reserved

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
        record.get("referral_reward", 0),
        common,
        funded=funded,
        margin=record.get("margin", 0),
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
    if funded:
        present_pending_reseller_level(
            bot,
            OWNER_ID,
            _language(OWNER_ID),
            allow_introduction=False,
        )
    reserved_text = _hosted_message(customer_id, "renewal_reserved_success")
    if reserved_text == "renewal_reserved_success":
        reserved_text = "Your renewal is paid and reserved. It will apply automatically when this config expires."
    bot.send_message(customer_id, reserved_text)
    return True, username


def _provision_payment(payment_id, record, funded):
    customer_id = int(record["user_id"])
    username = record.get("renew_username")
    client = None
    renewed = bool(username)
    if renewed and record.get("renewal_mode") == "reserved":
        return _settle_hosted_reserved_renewal(payment_id, record, funded)
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
            record.get("referral_reward", 0),
            metadata,
            funded=funded,
            margin=record.get("margin", 0),
        )
        _save_payment(payment_id, {"status": "completed", "username": username,
                                   "server_id": existing_config.get("server_id")})
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
        client, live = MultiServerAPI().find_user(username, preferred_server_id=record.get("server_id"))
        if not client or not live or client.reset_user(username) is None:
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
        record.get("referral_reward", 0),
        common,
        funded=funded,
        margin=record.get("margin", 0),
    )
    _save_payment(payment_id, {"status": "completed", "username": username, "server_id": server_id})
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


def _show_plans(chat_id, user_id, message_id=None):
    language = _language(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    settings = get_settings(OWNER_ID)
    for plan_id, plan in sorted(_sellable_plans().items(), key=lambda item: int(item[0])):
        quote = _hosted_plan_quote(plan, settings)
        account_type = get_message_text(
            language, "unlimited_users" if plan.get("unlimited", False) else "single_user"
        )
        markup.add(types.InlineKeyboardButton(
            f"{plan_id} GB · {plan.get('days', 30)} days · ${format_usd_amount(quote['retail'])}{account_type}",
            callback_data=f"hb:buy:{plan_id}",
        ))
    text = _message(user_id, "select_plan") if markup.keyboard else _message(user_id, "plan_not_found")
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


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
    quote = _hosted_plan_quote(
        plan,
        settings,
        settings["referral_margin_percent"],
        referred=str(user_id) in _referral_data().get("referrals", {}),
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    suffix = ""
    if renewal:
        token = _store_renewal_token(user_id, renewal)
        suffix = f":{token}"
    if settings.get("card_number"):
        markup.add(types.InlineKeyboardButton(get_button_text(language, "card_to_card"),
                                              callback_data=f"hb:pay:card:{plan_id}{suffix}"))
    if settings.get("crypto_enabled") and quote["crypto_supported"] and os.getenv("CRYPTO_MERCHANT_ID") and os.getenv("CRYPTO_API_KEY"):
        markup.add(types.InlineKeyboardButton(
            get_message_text(language, "crypto_discount_button").format(percent=5),
            callback_data=f"hb:pay:crypto:{plan_id}{suffix}",
        ))
    if not markup.keyboard:
        bot.send_message(chat_id, _message(user_id, "no_payment_methods"))
        return
    markup.add(types.InlineKeyboardButton(get_button_text(language, "back"), callback_data="hb:plans"))
    exchange_rate = get_exchange_rate()
    unlimited_text = get_button_text(language, "yes" if plan.get("unlimited", False) else "no")
    text = _message(user_id, "plan_details")
    text += _message(user_id, "data").format(plan_gb=plan_id)
    text += _message(user_id, "duration").format(days=plan.get("days", 30))
    text += _message(user_id, "unlimited").format(unlimited_text=unlimited_text)
    text += _message(user_id, "price").format(price=format_usd_amount(quote["retail"]))
    text += _message(user_id, "exchange_rate").format(exchange_rate=format_toman_amount(exchange_rate))
    text += _message(user_id, "toman_price").format(
        toman_price=format_toman_amount(quote["retail"] * exchange_rate)
    )
    text += _message(user_id, "purchase_connection_warning")
    text += _message(user_id, "select_payment_method")
    if message_id is not None:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


@bot.message_handler(commands=["start"])
def start(message):
    parts = (message.text or "").split(maxsplit=1)
    start_payload = parts[1].strip() if len(parts) == 2 else ""
    if start_payload == "owner_setup" and message.from_user.id == OWNER_ID:
        _show_owner_dashboard(message.chat.id, reply_to=message)
        return
    if start_payload and start_payload != "owner_setup":
        _register_referral(message.from_user.id, parts[1].strip())
    settings = get_settings(OWNER_ID)
    bot.reply_to(
        message,
        settings.get("welcome_text") or _hosted_message(message.from_user.id, "welcome_default"),
        reply_markup=_main_markup(message.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text in _all_button_values("purchase_plan", "💳 Purchase Plan"))
def plans(message):
    _show_plans(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "hb:plans")
def plans_back(call):
    bot.answer_callback_query(call.id)
    _show_plans(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:buy:"))
def buy(call):
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, call.data.split(":")[2],
                      message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:pay:"))
def payment_method(call):
    parts = call.data.split(":")
    if len(parts) not in {4, 5} or parts[2] not in {"card", "crypto"}:
        bot.answer_callback_query(call.id, "Invalid checkout action.", show_alert=True)
        return
    method, plan_id = parts[2], parts[3]
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
                "This renewal checkout expired. Open the config and start again.",
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
    quote = _hosted_plan_quote(
        plan,
        settings,
        settings["referral_margin_percent"],
        referred,
    )
    order_id = str(uuid.uuid4())
    record = {
        "id": order_id, "user_id": call.from_user.id, "telegram_username": call.from_user.username,
        "reseller_id": str(OWNER_ID), "origin_bot_id": os.getenv("AJIB_HOSTED_BOT_ID"),
        "plan_gb": plan_id, "days": plan.get("days", 30), "unlimited": plan.get("unlimited", False),
        "wholesale_price": quote["wholesale"], "retail_price": quote["retail"],
        "list_price": quote["list_price"], "reseller_level": quote["reseller_level"],
        "discount_percent": quote["discount_percent"],
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
        bot.answer_callback_query(call.id, _message(call.from_user.id, "no_payment_methods"), show_alert=True)
        return
    if method == "crypto" and (not settings.get("crypto_enabled") or not quote["crypto_supported"]):
        bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "crypto_disabled"), show_alert=True)
        return
    if method == "card":
        record["reservation_id"] = order_id
    started, existing_id = _start_checkout(order_id, record)
    if not started:
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
            _save_payment(order_id, {"status": "failed", "last_error": "Reseller credit is unavailable"})
            bot.answer_callback_query(call.id, _hosted_message(call.from_user.id, "credit_unavailable"),
                                      show_alert=True)
            return
        exchange_rate = get_exchange_rate()
        toman_price = quote["retail"] * exchange_rate
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
        bot.edit_message_text(
            _message(call.from_user.id, "card_to_card_payment").format(
                price=format_toman_amount(toman_price),
                exchange_rate=format_toman_amount(exchange_rate),
                card_number=settings["card_number"],
            ), call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup,
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
        bot.answer_callback_query(
            call.id, _message(call.from_user.id, "error_creating_payment").format(error=error_message),
            show_alert=True,
        )
        return
    gateway = response.get("result", {})
    gateway_id, url = gateway.get("uuid"), gateway.get("url")
    if not gateway_id or not url:
        _save_payment(order_id, {"status": "failed", "last_error": "Invalid gateway response"})
        bot.answer_callback_query(
            call.id, _message(call.from_user.id, "error_creating_payment").format(error="Invalid gateway response"),
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
    caption = _message(call.from_user.id, "payment_instructions").format(
        price=format_usd_amount(quote["crypto_collected"]), payment_url=url, payment_id=gateway_id
    )
    caption += "\n\n" + _message(call.from_user.id, "crypto_discount_summary").format(
        percent=5,
        original_price=format_usd_amount(quote["retail"]),
        discount_amount=format_usd_amount(quote["retail"] - quote["crypto_collected"]),
        discounted_price=format_usd_amount(quote["crypto_collected"]),
    )
    caption += _message(call.from_user.id, "purchase_connection_warning")
    image = io.BytesIO()
    qrcode.make(url).save(image, "PNG")
    image.seek(0)
    bot.answer_callback_query(call.id)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    bot.send_photo(call.message.chat.id, image, caption=caption, parse_mode="Markdown", reply_markup=markup)


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
            bot.reply_to(message, "Receipt upload failed. Please send the photo again.")
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
            call.id, _message(call.from_user.id, "payment_already_processed").format(status="completed"),
            show_alert=True,
        )
        return
    if current.get("status") not in {"pending", "paid_provision_failed", "processing"}:
        bot.answer_callback_query(
            call.id,
            _message(call.from_user.id, "payment_already_processed").format(status=current.get("status", "unknown")),
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
        bot.answer_callback_query(call.id, "Payment gateway reference is missing.", show_alert=True)
        return
    try:
        response = CryptoPayment().check_payment_status(record["gateway_payment_id"])
    except Exception as error:
        _save_payment(
            payment_id,
            {"status": retry_status, "last_error": f"Gateway status failed: {type(error).__name__}"},
        )
        bot.answer_callback_query(call.id, "Payment status is temporarily unavailable.", show_alert=True)
        return
    result = response.get("result", {}) if isinstance(response, dict) else {}
    status = result.get("status") or result.get("payment_status") or result.get("paymentStatus")
    if str(status).lower() != "paid":
        _save_payment(payment_id, {"status": retry_status})
        bot.answer_callback_query(
            call.id,
            (_message(call.from_user.id, "payment_pending") if not status else
             _message(call.from_user.id, "payment_status").format(status=status)),
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
    bot.answer_callback_query(call.id, _message(call.from_user.id, "payment_status").format(status="completed"),
                              show_alert=True)


@bot.message_handler(func=lambda m: m.text in _all_button_values("my_configs", "📱 My Configs"))
def my_configs(message):
    configs = _find_customer_configs(message.from_user.id)
    if not configs:
        bot.reply_to(message, "You have no configs in this bot.")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for index, config in enumerate(configs):
        markup.add(types.InlineKeyboardButton(config.get("username", f"Config {index + 1}"), callback_data=f"hb:cfg:{index}"))
    bot.reply_to(message, "Your configs:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:cfg:"))
def config_detail(call):
    configs = _find_customer_configs(call.from_user.id)
    try:
        config = configs[int(call.data.split(":")[2])]
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "Config not found", show_alert=True)
        return
    client, live = MultiServerAPI().find_user(config.get("username"), preferred_server_id=config.get("server_id"))
    if not client or not live:
        bot.answer_callback_query(call.id, "Config unavailable", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _deliver_config(call.message.chat.id, config["username"], client)
    from utils.renewal import find_reseller_renewal_offer, find_reseller_reservation

    existing = find_reseller_reservation(config) or _matching_reserved_checkout(
        _tenant_payments(),
        call.from_user.id,
        config.get("username"),
        config.get("server_id"),
    )
    if existing:
        status_text = _hosted_message(call.from_user.id, "renewal_reserved_status")
        if status_text == "renewal_reserved_status":
            status_text = "A renewal is already reserved for this config."
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
        if message == message_key:
            message = (
                "This config is eligible for renewal."
                if renewal_mode == "immediate"
                else "Reserve the next renewal now and it will apply automatically at expiry."
            )
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
        bot.answer_callback_query(call.id, "Invalid renewal action.", show_alert=True)
        return
    plan_id, token = parts[2], parts[3]
    renewal = _consume_renewal_token(token, call.from_user.id)
    if not renewal:
        bot.answer_callback_query(call.id, "This renewal action expired. Open the config again.", show_alert=True)
        return
    bot.answer_callback_query(call.id)
    _purchase_options(call.message.chat.id, call.from_user.id, plan_id, renewal)


@bot.message_handler(func=lambda m: m.text in _all_button_values("support", "📞 Support"))
def support(message):
    bot.reply_to(message, get_settings(OWNER_ID).get("support_text") or
                 _hosted_message(message.from_user.id, "support_default"))


@bot.message_handler(func=lambda m: m.text in _all_button_values("language", "🌐 Language/زبان"))
def language(message):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(name, callback_data=f"hb:lang:{code}") for code, name in LANGUAGES.items()])
    bot.reply_to(message, "Select language:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data.startswith("hb:lang:"))
def language_set(call):
    code = call.data.split(":")[2]
    if code in LANGUAGES:
        _set_language(call.from_user.id, code)
    bot.answer_callback_query(call.id, "Language updated", show_alert=True)
    bot.send_message(call.message.chat.id, "Menu updated.", reply_markup=_main_markup(call.from_user.id))


@bot.message_handler(func=lambda m: m.text in _all_button_values("downloads", "⬇️ Downloads"))
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


@bot.message_handler(func=lambda m: m.text in _all_button_values("test_config", "🎁 Test Config"))
def free_test(message):
    recovering_pending_test = False
    pending_username = None
    pending_server_id = None
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        key = str(message.from_user.id)
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
                bot.reply_to(message, "You have already used a free test on this infrastructure.")
                return
            recovering_pending_test = True
            pending_username = existing.get("username")
            pending_server_id = existing.get("server_id")
        tests[key] = {
            **(dict(existing) if recovering_pending_test else {}),
            "telegram_id": message.from_user.id,
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
                current = tests.get(str(message.from_user.id))
                if not isinstance(current, dict) or not current.get("creation_pending_at"):
                    raise RuntimeError("Hosted test creation claim is missing")
                current["username"] = allocated_username
                current["server_id"] = getattr(allocated_client, "server_id", None)

        username, result, client = _create_user(
            plan,
            "",
            customer_id=message.from_user.id,
            username_prefix="ht",
            on_username_allocated=persist_test_allocation,
            preferred_username=pending_username,
        )
    if result is None:
        with locked_json(GLOBAL_TEST_FILE, {}) as tests:
            current = tests.get(str(message.from_user.id))
            if not isinstance(current, dict) or not current.get("username"):
                tests.pop(str(message.from_user.id), None)
        bot.reply_to(message, "Test creation failed. Please try again later.")
        return
    with locked_json(GLOBAL_TEST_FILE, {}) as tests:
        tests[str(message.from_user.id)].update({"username": username, "server_id": getattr(client, "server_id", None),
                                                 "used_at": _now(), "creation_pending_at": None})
    _deliver_config_safely(message.chat.id, username, client)


@bot.message_handler(func=lambda m: m.text in _all_button_values("referral", "💰 Earn Crypto"))
def referral(message):
    data = _referral_data()
    code = _ensure_referral_code(message.from_user.id)
    stats = data.get("stats", {}).get(str(message.from_user.id), {})
    wallet = data.get("wallets", {}).get(str(message.from_user.id))
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("Set wallet", callback_data="hb:refwallet"),
               types.InlineKeyboardButton("Withdraw", callback_data="hb:refwithdraw"))
    bot.reply_to(message,
                 f"Invites: {stats.get('count', 0)}\nEarned: ${float(stats.get('total_earnings', 0)):.2f}\n"
                 f"Available: ${float(stats.get('available_balance', 0)):.2f}\nWallet: {wallet or 'not set'}\n"
                 f"Link: https://t.me/{BOT_USERNAME}?start={code}", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data in {"hb:refwallet", "hb:refwithdraw"})
def referral_action(call):
    if call.data == "hb:refwallet":
        _set_input_state(call.from_user.id, {"kind": "referral_wallet"})
        bot.send_message(call.message.chat.id, "Send your payout wallet/destination.")
        bot.answer_callback_query(call.id)
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        stats = data.get("stats", {}).get(str(call.from_user.id), {})
        amount = round(float(stats.get("available_balance", 0)), 2)
        wallet = data.get("wallets", {}).get(str(call.from_user.id))
        if amount < 2 or not wallet:
            bot.answer_callback_query(call.id, "Minimum $2 and a wallet are required.", show_alert=True)
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
    bot.answer_callback_query(call.id, "Withdrawal requested", show_alert=True)


@bot.message_handler(func=lambda m: (_get_input_state(m.from_user.id) or {}).get("kind") == "referral_wallet")
def referral_wallet_input(message):
    destination = (message.text or "").strip()
    if not destination or len(destination) > 500 or "\x00" in destination:
        _pop_input_state(message.from_user.id)
        bot.reply_to(message, "That payout destination is invalid.")
        return
    with locked_json(tenant_file(OWNER_ID, "referrals.json"), _referral_data()) as data:
        data.setdefault("wallets", {})[str(message.from_user.id)] = destination
    _pop_input_state(message.from_user.id)
    bot.reply_to(message, "Wallet saved.")


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
            if key == "back" and text in _all_button_values("back", "🔙 Back"):
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
        return _button(user_id, "back", "🔙 Back")
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


def _send_owner_stats(chat_id, end_date=None, scheduled=False):
    chunks = _owner_stats_chunks(end_date=end_date, scheduled=scheduled)
    for chunk in chunks:
        bot.send_message(chat_id, chunk, parse_mode="Markdown")
    return len(chunks)


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


def _begin_owner_setting(chat_id, field):
    _set_input_state(OWNER_ID, {"kind": "owner_setting", "field": field})
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
        settings = get_settings(OWNER_ID)
        bot.reply_to(message, settings.get("welcome_text") or _hosted_message(OWNER_ID, "welcome_default"),
                     reply_markup=_main_markup(OWNER_ID))
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
    markup = types.InlineKeyboardMarkup(row_width=1)
    for plan_id in sorted(all_ids, key=int):
        markup.add(types.InlineKeyboardButton(
            f"{'✅' if plan_id in enabled else '❌'} {plan_id} GB",
            callback_data=f"hb:plantoggle:{plan_id}",
        ))
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
    action = call.data.split(":")[2]
    if action in {"welcome", "support"}:
        _begin_owner_setting(call.message.chat.id, action)
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
        return True
    fields = {}
    if status == "attention":
        fields = {
            "renewal_attention_reason": event.get("reason"),
            "renewal_last_error": event.get("reason"),
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
    if event.get("status") != "attention" or not event.get("alert_due"):
        return
    reason = event.get("reason") or "renewal_reset_failed"
    username = record.get("renew_username") or record.get("username") or "unknown"
    customer_reason = _message(int(customer_id), reason) if customer_id is not None else reason
    if customer_reason == reason:
        customer_reason = reason
    try:
        message = _hosted_message(
            int(customer_id),
            "renewal_reserved_attention",
            username=username,
            reason=customer_reason,
        )
        if message == "renewal_reserved_attention":
            message = f"Your reserved renewal for `{username}` needs attention: {reason}"
        bot.send_message(int(customer_id), message, parse_mode="Markdown")
    except Exception:
        pass
    try:
        owner_reason = _message(OWNER_ID, reason)
        if owner_reason == reason:
            owner_reason = reason
        bot.send_message(
            OWNER_ID,
            f"Reserved renewal needs attention.\nCustomer: `{customer_id}`\nConfig: `{username}`\nReason: {owner_reason}",
            reply_markup=_hosted_renewal_review_markup(event["payment_id"], reason),
            parse_mode="Markdown",
        )
    except Exception:
        pass
    mark_payment_renewal_alerted(
        event["payment_id"],
        payments_file=tenant_file(OWNER_ID, "payments.json"),
    )
    mark_reseller_renewal_alerted(OWNER_ID, event["payment_id"])


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
            process_payment_renewal_reservation,
            refresh_payment_renewal_baseline,
        )
        from utils.reseller import refresh_reseller_renewal_baseline

        payments_file = tenant_file(OWNER_ID, "payments.json")
        record = _tenant_payments().get(payment_id, {})
        if action == "wait":
            client, live = MultiServerAPI().find_user(
                record.get("renew_username"),
                preferred_server_id=record.get("server_id"),
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
            _process_hosted_reserved_renewals()
            for payment_id, current in _tenant_payments().items():
                if not isinstance(current, dict):
                    continue
                if current.get("status") in {"waiting_receipt", "pending_approval"}:
                    created = _parse_time(current.get("created_at")) or datetime.min
                    if datetime.now() - created >= timedelta(hours=24):
                        claimed = _claim_payment(payment_id, {"waiting_receipt", "pending_approval"})
                        if claimed:
                            release_credit(OWNER_ID, payment_id, kind="credit_expired")
                            _save_payment(payment_id, {"status": "expired"})
                            try:
                                bot.send_message(
                                    current["user_id"],
                                    "Your pending card order expired. Start a new purchase if needed.",
                                )
                            except Exception:
                                pass
                    elif current.get("status") == "pending_approval":
                        _notify_owner_of_receipt(payment_id)
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
                    expiration = int(live.get("expiration_days", 0) or 0)
                    cycle = str(config.get("timestamp") or config.get("retail_order_id") or "initial")
                    if expiration <= 0:
                        from utils.renewal import find_reseller_reservation

                        if find_reseller_reservation(config):
                            continue
                    if expiration <= 0 and sent.get(f"expired:{username}") != cycle:
                        bot.send_message(user_id, f"Your config `{username}` has expired. Open My Configs to renew it.", parse_mode="Markdown")
                        sent[f"expired:{username}"] = cycle
                    maximum = float(live.get("max_download_bytes", 0) or 0)
                    used = float(live.get("upload_bytes", 0) or 0) + float(live.get("download_bytes", 0) or 0)
                    if maximum > 0:
                        percent = int((used / maximum) * 100)
                        threshold = 90 if percent >= 90 else 80 if percent >= 80 else None
                        alert_key = f"traffic:{username}:{cycle}"
                        if threshold and int(sent.get(alert_key, 0) or 0) < threshold:
                            bot.send_message(user_id, f"Config `{username}` has used {percent}% of its traffic quota.", parse_mode="Markdown")
                            sent[alert_key] = threshold
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
