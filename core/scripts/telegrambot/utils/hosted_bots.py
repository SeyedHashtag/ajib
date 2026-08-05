import hashlib
import os
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .atomic_store import locked_json, read_json
from . import database, reseller as reseller_store


BOT_DIR = os.getenv("AJIB_BOT_DIR", "/etc/ajib/core/scripts/telegrambot")
HOSTED_ROOT = os.path.join(BOT_DIR, "hosted_bots")
REGISTRY_FILE = os.path.join(BOT_DIR, "hosted_bots.json")
SECRETS_FILE = os.path.join(BOT_DIR, "hosted_bot_tokens.json")


def _positive_int_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MAX_ACTIVE_BOTS = _positive_int_env("AJIB_MAX_HOSTED_BOTS", 50)
CRYPTO_DISCOUNT_PERCENT = Decimal("5")
INVITED_BUYER_DISCOUNT_PERCENT = Decimal("5")
MAX_CUSTOMER_DISCOUNT_PERCENT = Decimal("10")
MINIMUM_PAYOUT = Decimal("2.00")
SETUP_VERSION = 1
SETUP_REQUIRED_STEPS = ("pricing", "payments", "plans")
SETUP_CONFIRMABLE_STEPS = {"pricing", "plans", "messages"}
LEGACY_WELCOME_TEXT = "Welcome!"
LEGACY_SUPPORT_TEXT = "Contact the reseller for support."
SUPPORTED_STOREFRONT_LANGUAGES = ("en", "fa", "ru", "tk")
PRIVATE_PROJECT_IDENTIFIER = "".join(chr(code) for code in (97, 106, 105, 98))


def _contains_private_identifier(value):
    return PRIVATE_PROJECT_IDENTIFIER in str(value or "").casefold()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _money(value):
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid monetary amount") from error
    if not amount.is_finite():
        raise ValueError("Invalid monetary amount")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _reseller_key(reseller_id):
    key = str(reseller_id).strip()
    if not key.isdigit() or len(key) > 20:
        raise ValueError("Invalid reseller ID")
    return key


def _tenant_dir(reseller_id):
    return os.path.join(HOSTED_ROOT, _reseller_key(reseller_id))


def tenant_file(reseller_id, name):
    root = os.path.abspath(_tenant_dir(reseller_id))
    candidate = os.path.abspath(os.path.join(root, str(name)))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError("Invalid hosted-bot state path")
    os.makedirs(root, mode=0o700, exist_ok=True)
    for directory in (os.path.abspath(HOSTED_ROOT), root):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return candidate


def default_settings():
    return {
        "markup_percent": 20.0,
        "enabled_plan_ids": [],
        "plan_selection_configured": False,
        "card_number": "",
        "welcome_text": "",
        "support_text": "",
        "welcome_texts": {},
        "support_texts": {},
        "recommended_plan_id": "",
        "referral_margin_percent": 20.0,
        "crypto_enabled": False,
        "setup_version": 0,
        "setup_completed_steps": [],
    }


def _finite_setting(value, name, minimum, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {name}") from error
    if not Decimal(str(result)).is_finite() or result < minimum or result > maximum:
        raise ValueError(f"Invalid {name}")
    return result


def _validate_setting(key, value):
    if key == "markup_percent":
        return _finite_setting(value, "markup percentage", 0, 1000)
    if key == "referral_margin_percent":
        return _finite_setting(value, "referral percentage", 0, 100)
    if key in {"crypto_enabled", "plan_selection_configured"}:
        if not isinstance(value, bool):
            raise ValueError(f"Invalid {key}")
        return value
    if key == "setup_version":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SETUP_VERSION:
            raise ValueError("Invalid setup version")
        return value
    if key == "setup_completed_steps":
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("Invalid setup progress")
        normalized = []
        for item in value:
            step = str(item).strip()
            if step not in SETUP_CONFIRMABLE_STEPS:
                raise ValueError("Invalid setup progress")
            if step not in normalized:
                normalized.append(step)
        return normalized
    if key == "enabled_plan_ids":
        if not isinstance(value, (list, tuple, set)):
            raise ValueError("Invalid plan selection")
        normalized = []
        for item in value:
            plan_id = str(item).strip()
            if not plan_id.isdigit() or len(plan_id) > 12:
                raise ValueError("Invalid plan selection")
            if plan_id not in normalized:
                normalized.append(plan_id)
            if len(normalized) > 200:
                raise ValueError("Too many selected plans")
        return normalized
    if key in {"welcome_texts", "support_texts"}:
        if not isinstance(value, dict):
            raise ValueError(f"Invalid {key}")
        normalized = {}
        for language, message in value.items():
            language_code = str(language).strip().lower()
            if language_code not in SUPPORTED_STOREFRONT_LANGUAGES:
                raise ValueError(f"Invalid {key}")
            text = str(message or "").strip()
            if "\x00" in text or len(text) > 2000 or _contains_private_identifier(text):
                raise ValueError(f"Invalid {key}")
            if text:
                normalized[language_code] = text
        return normalized
    if key == "recommended_plan_id":
        plan_id = str(value or "").strip()
        if plan_id and (not plan_id.isdigit() or len(plan_id) > 12):
            raise ValueError("Invalid recommended plan")
        return plan_id
    if key in {"card_number", "welcome_text", "support_text"}:
        limits = {"card_number": 64, "welcome_text": 2000, "support_text": 2000}
        text = str(value or "").strip()
        if (
            "\x00" in text
            or len(text) > limits[key]
            or (key != "card_number" and _contains_private_identifier(text))
        ):
            raise ValueError(f"Invalid {key}")
        if key == "card_number" and any(character not in "0123456789 -" for character in text):
            raise ValueError("Invalid card number")
        return text
    raise ValueError("Unknown hosted-bot setting")


def _normalized_settings(stored):
    settings = default_settings()
    if not isinstance(stored, dict):
        return settings
    for key, default in default_settings().items():
        if key not in stored:
            continue
        try:
            settings[key] = _validate_setting(key, stored[key])
        except ValueError:
            settings[key] = default
    if stored.get("updated_at"):
        settings["updated_at"] = str(stored["updated_at"])
    if settings["setup_version"] == 0:
        if settings["welcome_text"] == LEGACY_WELCOME_TEXT:
            settings["welcome_text"] = ""
        if settings["support_text"] == LEGACY_SUPPORT_TEXT:
            settings["support_text"] = ""
    return settings


def localized_storefront_text(settings, field, language, default=""):
    """Resolve localized storefront copy while retaining legacy single-text fallback."""
    if field not in {"welcome", "support"}:
        raise ValueError("Invalid localized storefront field")
    current = settings if isinstance(settings, dict) else {}
    language_code = str(language or "en").split("-", 1)[0].lower()
    messages = current.get(f"{field}_texts")
    if isinstance(messages, dict):
        value = messages.get(language_code) or messages.get("en")
        if str(value or "").strip():
            return str(value).strip()
    legacy = str(current.get(f"{field}_text") or "").strip()
    return legacy or str(default or "")


def get_settings(reseller_id):
    stored = read_json(tenant_file(reseller_id, "settings.json"), {})
    return _normalized_settings(stored)


def update_settings(reseller_id, updates):
    allowed = set(default_settings())
    validated = {
        key: _validate_setting(key, value)
        for key, value in dict(updates or {}).items()
        if key in allowed
    }
    with locked_json(tenant_file(reseller_id, "settings.json"), default_settings()) as settings:
        normalized = _normalized_settings(settings)
        normalized.update(validated)
        settings.clear()
        settings.update(normalized)
        settings["updated_at"] = _now()
        return dict(settings)


def get_setup_status(reseller_id, settings=None, crypto_available=True, plans_available=True):
    """Return advisory owner-setup progress without mutating legacy state."""
    current = dict(settings or get_settings(reseller_id))
    version = int(current.get("setup_version", 0) or 0)
    if version == 0:
        completed = {"pricing", "plans"}
        if (
            current.get("welcome_text")
            or current.get("support_text")
            or current.get("welcome_texts")
            or current.get("support_texts")
        ):
            completed.add("messages")
    else:
        completed = {
            step for step in current.get("setup_completed_steps", [])
            if step in SETUP_CONFIRMABLE_STEPS
        }
    payment_ready = bool(
        current.get("card_number")
        or (current.get("crypto_enabled") and crypto_available)
    )
    steps = {
        "pricing": "pricing" in completed,
        "payments": payment_ready,
        "plans": "plans" in completed and bool(plans_available),
    }
    completed_required = sum(1 for step in SETUP_REQUIRED_STEPS if steps[step])
    next_step = next((step for step in SETUP_REQUIRED_STEPS if not steps[step]), None)
    return {
        "version": version,
        "steps": steps,
        "messages_complete": "messages" in completed,
        "completed": completed_required,
        "total": len(SETUP_REQUIRED_STEPS),
        "ready": completed_required == len(SETUP_REQUIRED_STEPS),
        "next_step": next_step,
    }


def mark_setup_step(reseller_id, step, completed=True):
    """Confirm an owner-reviewed setup step, lazily upgrading legacy progress."""
    if step not in SETUP_CONFIRMABLE_STEPS:
        raise ValueError("Invalid setup step")
    current = get_settings(reseller_id)
    if int(current.get("setup_version", 0) or 0) == 0:
        confirmed = {"pricing", "plans"}
        if (
            current.get("welcome_text")
            or current.get("support_text")
            or current.get("welcome_texts")
            or current.get("support_texts")
        ):
            confirmed.add("messages")
    else:
        confirmed = set(current.get("setup_completed_steps", []))
    if completed:
        confirmed.add(step)
    else:
        confirmed.discard(step)
    return update_settings(
        reseller_id,
        {
            "setup_version": SETUP_VERSION,
            "setup_completed_steps": sorted(confirmed),
        },
    )


def calculate_quote(
    wholesale,
    markup_percent,
    referral_margin_percent=0,
    referred=False,
    retail_base=None,
    buyer_discount_percent=0,
):
    wholesale_amount = _money(wholesale)
    if wholesale_amount < 0:
        raise ValueError("Wholesale price cannot be negative")
    retail_base_amount = _money(wholesale if retail_base is None else retail_base)
    if retail_base_amount < 0:
        raise ValueError("Retail base price cannot be negative")
    markup = Decimal(str(_finite_setting(markup_percent or 0, "markup percentage", 0, 1000)))
    retail = _money(retail_base_amount * (Decimal("1") + markup / Decimal("100")))
    buyer_discount = Decimal(str(_finite_setting(
        buyer_discount_percent or 0,
        "buyer discount percentage",
        0,
        float(MAX_CUSTOMER_DISCOUNT_PERCENT),
    )))
    card_discount = min(MAX_CUSTOMER_DISCOUNT_PERCENT, buyer_discount)
    crypto_discount = min(
        MAX_CUSTOMER_DISCOUNT_PERCENT,
        buyer_discount + CRYPTO_DISCOUNT_PERCENT,
    )
    crypto_component_discount = max(Decimal("0"), crypto_discount - card_discount)
    card_collected = _money(retail * (Decimal("1") - card_discount / Decimal("100")))
    collected = _money(retail * (Decimal("1") - crypto_discount / Decimal("100")))
    buyer_discount_amount = _money(retail - card_collected)
    crypto_discount_amount = _money(retail - collected)
    crypto_component_discount_amount = _money(
        max(Decimal("0"), crypto_discount_amount - buyer_discount_amount)
    )
    margin = _money(collected - wholesale_amount)
    card_margin = _money(card_collected - wholesale_amount)
    referral_rate = (
        Decimal(str(_finite_setting(referral_margin_percent or 0, "referral percentage", 0, 100)))
        if referred else Decimal("0")
    )
    referral_reward = _money(max(Decimal("0"), margin) * referral_rate / Decimal("100"))
    card_referral_reward = _money(max(Decimal("0"), card_margin) * referral_rate / Decimal("100"))
    return {
        "wholesale": float(wholesale_amount),
        "retail_base": float(retail_base_amount),
        "retail": float(retail),
        "original_price": float(retail),
        "card_collected": float(card_collected),
        "crypto_collected": float(collected),
        "crypto_margin": float(margin),
        "card_margin": float(card_margin),
        "referral_reward": float(referral_reward),
        "crypto_referral_reward": float(referral_reward),
        "card_referral_reward": float(card_referral_reward),
        "crypto_supported": collected >= wholesale_amount,
        "card_supported": card_collected >= wholesale_amount,
        "buyer_discount_percent": float(buyer_discount),
        "card_discount_percent": float(card_discount),
        "crypto_discount_percent": float(crypto_discount),
        "crypto_component_discount_percent": float(crypto_component_discount),
        "buyer_discount_amount": float(buyer_discount_amount),
        "crypto_component_discount_amount": float(crypto_component_discount_amount),
        "card_discount_amount": float(buyer_discount_amount),
        "crypto_discount_amount": float(crypto_discount_amount),
        # Kept for backward compatibility with records and owner pricing views.
        "discount_percent": float(CRYPTO_DISCOUNT_PERCENT),
    }


def _token_fingerprint(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def list_bots():
    data = read_json(REGISTRY_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(value, dict) and str(key).isdigit() and len(str(key)) <= 20
    }


def get_bot(reseller_id):
    return list_bots().get(_reseller_key(reseller_id))


def get_token(reseller_id):
    data = read_json(SECRETS_FILE, {})
    if not isinstance(data, dict):
        return None
    token = data.get(_reseller_key(reseller_id))
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token if ":" in token and len(token) <= 256 else None


def register_bot(reseller_id, token, bot_info, main_bot_id=None):
    reseller_key = _reseller_key(reseller_id)
    clean_token = str(token or "").strip()
    if ":" not in clean_token or len(clean_token) > 256:
        return False, "Telegram bot token is invalid."
    mapping = bot_info if isinstance(bot_info, dict) else {}
    bot_id = str(getattr(bot_info, "id", "") or mapping.get("id") or "")
    username = str(getattr(bot_info, "username", "") or mapping.get("username") or "")
    if not bot_id or not username:
        return False, "Telegram did not return a usable bot identity."
    if main_bot_id is not None and bot_id == str(main_bot_id):
        return False, "The primary service bot token cannot be used as a reseller bot."

    fingerprint = _token_fingerprint(clean_token)
    with locked_json(REGISTRY_FILE, {}) as registry:
        for owner_id, record in registry.items():
            if owner_id == reseller_key:
                continue
            if str(record.get("bot_id")) == bot_id or record.get("token_fingerprint") == fingerprint:
                return False, "This Telegram bot is already connected to another reseller."
        active_others = sum(
            1 for owner_id, record in registry.items()
            if (owner_id != reseller_key and isinstance(record, dict)
                and record.get("enabled", True) and record.get("status") not in {"disabled", "disconnected", "blocked"})
        )
        if active_others >= MAX_ACTIVE_BOTS:
            return False, f"This installation already has {MAX_ACTIVE_BOTS} hosted bots."
        previous = registry.get(reseller_key, {})
        # Commit the secret before publishing its fingerprint. Readers continue
        # using the old registry until the atomic registry replacement below.
        with locked_json(SECRETS_FILE, {}) as secrets:
            secrets[reseller_key] = clean_token
        registry[reseller_key] = {
            "reseller_id": reseller_key,
            "bot_id": bot_id,
            "username": username.lstrip("@"),
            "status": "starting",
            "enabled": True,
            "token_fingerprint": fingerprint,
            "created_at": previous.get("created_at") or _now(),
            "updated_at": _now(),
            "last_error": None,
        }
    if previous:
        update_settings(reseller_key, {})
    else:
        update_settings(
            reseller_key,
            {"setup_version": SETUP_VERSION, "setup_completed_steps": []},
        )
    return True, get_bot(reseller_key)


def disconnect_bot(reseller_id):
    key = _reseller_key(reseller_id)
    found = False
    outer = database.transaction() if os.getenv("AJIB_SQLITE_ACTIVE") == "1" else nullcontext()
    with outer:
        with locked_json(REGISTRY_FILE, {}) as registry:
            if key in registry:
                found = True
                registry[key]["enabled"] = False
                registry[key]["status"] = "disconnected"
                registry[key]["updated_at"] = _now()
        with locked_json(SECRETS_FILE, {}) as secrets:
            secrets.pop(key, None)
    return found


def set_bot_runtime_status(reseller_id, status, error=None):
    key = _reseller_key(reseller_id)
    with locked_json(REGISTRY_FILE, {}) as registry:
        if key not in registry:
            return False
        registry[key]["status"] = str(status)
        registry[key]["last_error"] = str(error)[:500] if error else None
        registry[key]["updated_at"] = _now()
        if status == "active":
            registry[key].setdefault("started_at", _now())
        return True


def set_bot_enabled(reseller_id, enabled):
    key = _reseller_key(reseller_id)
    with locked_json(REGISTRY_FILE, {}) as registry:
        if key not in registry:
            return False
        registry[key]["enabled"] = bool(enabled)
        registry[key]["status"] = "starting" if enabled else "disabled"
        registry[key]["updated_at"] = _now()
        return True


def _default_ledger():
    return {
        "earnings_available": 0.0,
        "earnings_reserved": 0.0,
        "credit_reservations": {},
        "withdrawals": [],
        "transactions": [],
        "referral_liability": 0.0,
    }


def get_ledger(reseller_id):
    ledger = _default_ledger()
    stored = read_json(tenant_file(reseller_id, "ledger.json"), {})
    if isinstance(stored, dict):
        ledger.update(stored)
    return ledger


def _append_transaction(ledger, kind, amount, metadata=None, transaction_id=None):
    transaction_id = transaction_id or str(uuid.uuid4())
    if any(item.get("id") == transaction_id for item in ledger.get("transactions", [])):
        return False
    ledger.setdefault("transactions", []).append({
        "id": transaction_id,
        "type": kind,
        "amount": float(_money(amount)),
        "metadata": dict(metadata or {}),
        "created_at": _now(),
    })
    return True


def credit_crypto_sale(reseller_id, order_id, margin, referral_reward=0, metadata=None):
    margin_value = _money(margin)
    referral_value = _money(referral_reward)
    if margin_value < 0 or referral_value < 0 or referral_value > margin_value:
        return False
    path = tenant_file(reseller_id, "ledger.json")
    with locked_json(path, _default_ledger()) as ledger:
        if not _append_transaction(ledger, "crypto_sale", margin_value, metadata, transaction_id=f"sale:{order_id}"):
            return False
        ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) + margin_value)
        if referral_value > 0:
            ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) + referral_value)
            _append_transaction(ledger, "referral_liability", referral_value, metadata, transaction_id=f"referral:{order_id}")
        return True


def add_referral_liability(reseller_id, order_id, amount, metadata=None):
    amount_value = _money(amount)
    if amount_value <= 0:
        return False
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        if not _append_transaction(ledger, "referral_liability", amount_value, metadata, transaction_id=f"referral:{order_id}"):
            return False
        ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) + amount_value)
        return True


def settle_referral_liability(reseller_id, withdrawal_id, amount):
    requested = _money(amount)
    if requested <= 0:
        return False
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        transaction_id = f"referral-paid:{withdrawal_id}"
        if any(item.get("id") == transaction_id for item in ledger.get("transactions", [])):
            return False
        value = min(requested, _money(ledger.get("referral_liability", 0)))
        if value <= 0:
            return False
        ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) - value)
        _append_transaction(ledger, "referral_paid", -value, {"withdrawal_id": withdrawal_id}, transaction_id)
        return True


def reserve_credit(reseller_id, reservation_id, amount, available_credit):
    reservation_key = str(reservation_id or "").strip()
    amount_value = _money(amount)
    available_value = _money(available_credit)
    if not reservation_key or len(reservation_key) > 128 or amount_value <= 0 or available_value < 0:
        return False
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservations = ledger.setdefault("credit_reservations", {})
        if reservation_key in reservations:
            return True
        reserved = sum(_money(item.get("amount", 0)) for item in reservations.values())
        if reserved + amount_value > available_value:
            return False
        reservations[reservation_key] = {"amount": float(amount_value), "created_at": _now()}
        _append_transaction(ledger, "credit_reserved", amount_value, {"reservation_id": reservation_key})
        return True


def release_credit(reseller_id, reservation_id, kind="credit_released"):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservation = ledger.setdefault("credit_reservations", {}).pop(str(reservation_id), None)
        if not reservation:
            return False
        _append_transaction(ledger, kind, reservation.get("amount", 0), {"reservation_id": reservation_id})
        return True


def release_stale_credit_reservations(reseller_id, active_reservation_ids, max_age_seconds=86400, now=None):
    """Release old reservations that no live checkout still owns."""
    active = {str(item) for item in (active_reservation_ids or ())}
    current_time = now or datetime.now()
    released = []
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservations = ledger.setdefault("credit_reservations", {})
        for reservation_id, reservation in list(reservations.items()):
            if reservation_id in active:
                continue
            try:
                created_at = datetime.strptime(reservation.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
            except (AttributeError, TypeError, ValueError):
                created_at = current_time - timedelta(seconds=max_age_seconds + 1)
            if (current_time - created_at).total_seconds() < max_age_seconds:
                continue
            reservations.pop(reservation_id, None)
            _append_transaction(
                ledger,
                "credit_stale_released",
                reservation.get("amount", 0),
                {"reservation_id": reservation_id},
                transaction_id=f"stale-release:{reservation_id}",
            )
            released.append(reservation_id)
    return released


def consume_credit(reseller_id, reservation_id, config_data):
    ledger_path = tenant_file(reseller_id, "ledger.json")
    with locked_json(ledger_path, _default_ledger()) as ledger:
        reservation = ledger.setdefault("credit_reservations", {}).get(str(reservation_id))
        if not reservation:
            return False
        amount = reservation.get("amount", 0)
        if not reseller_store.add_reseller_debt(reseller_id, amount, dict(config_data or {})):
            return False
        ledger["credit_reservations"].pop(str(reservation_id), None)
        _append_transaction(ledger, "credit_consumed", amount, {"reservation_id": reservation_id})
        return True


def consume_renewal_credit(reseller_id, reservation_id, username, renewal_data, server_id=None):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservation = ledger.setdefault("credit_reservations", {}).get(str(reservation_id))
        if not reservation:
            return False
        amount = reservation.get("amount", 0)
        if not reseller_store.add_reseller_renewal_debt(reseller_id, username, amount, dict(renewal_data or {}), server_id=server_id):
            return False
        ledger["credit_reservations"].pop(str(reservation_id), None)
        _append_transaction(ledger, "renewal_credit_consumed", amount, {"reservation_id": reservation_id})
        return True


def transfer_earnings_to_debt(reseller_id):
    path = tenant_file(reseller_id, "ledger.json")
    with locked_json(path, _default_ledger()) as ledger:
        current = reseller_store.get_reseller_data(reseller_id) or {}
        debt = _money(current.get("debt", 0))
        if debt <= 0:
            return False, "There is no debt to settle."
        amount = min(debt, _money(ledger.get("earnings_available", 0)))
        if amount <= 0:
            return False, "There are no available earnings."
        transfer_id = f"earnings-{uuid.uuid4().hex}"
        try:
            success, remaining = reseller_store.apply_reseller_payment(
                reseller_id,
                float(amount),
                payment_id=transfer_id,
                allocation_kind="earnings_transfer",
            )
        except TypeError:
            success, remaining = reseller_store.apply_reseller_payment(reseller_id, float(amount))
        if not success:
            return False, "Debt settlement failed."
        ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) - amount)
        _append_transaction(ledger, "earnings_to_debt", -amount, {
            "remaining_debt": remaining,
            "debt_allocation_id": transfer_id,
        })
        return True, {"amount": float(amount), "remaining_debt": remaining}


def request_earnings_withdrawal(reseller_id, destination):
    clean_destination = str(destination or "").strip()
    if not clean_destination or len(clean_destination) > 500 or "\x00" in clean_destination:
        return False, "A payout destination is required."
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        debt = _money((reseller_store.get_reseller_data(reseller_id) or {}).get("debt", 0))
        if debt > 0:
            return False, "All reseller debt must be settled first."
        if any(item.get("status") == "pending" for item in ledger.get("withdrawals", [])):
            return False, "A withdrawal is already pending."
        amount = _money(ledger.get("earnings_available", 0))
        if amount < MINIMUM_PAYOUT:
            return False, f"Minimum withdrawal is ${MINIMUM_PAYOUT:.2f}."
        request = {
            "id": str(uuid.uuid4()), "status": "pending", "amount": float(amount),
            "destination": clean_destination, "requested_at": _now(),
        }
        ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) - amount)
        ledger["earnings_reserved"] = float(_money(ledger.get("earnings_reserved", 0)) + amount)
        ledger.setdefault("withdrawals", []).append(request)
        _append_transaction(ledger, "withdrawal_requested", -amount, {"withdrawal_id": request["id"]})
        return True, dict(request)


def resolve_earnings_withdrawal(reseller_id, withdrawal_id, action, admin_id):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        request = next((item for item in ledger.get("withdrawals", []) if item.get("id") == withdrawal_id), None)
        if not request or request.get("status") != "pending":
            return False, "Pending withdrawal not found."
        if action == "paid" and _money((reseller_store.get_reseller_data(reseller_id) or {}).get("debt", 0)) > 0:
            return False, "Reseller debt is no longer zero."
        if action not in {"paid", "rejected"}:
            return False, "Invalid withdrawal action."
        amount = _money(request.get("amount", 0))
        request["status"] = action
        request["resolved_at"] = _now()
        request["admin_id"] = str(admin_id)
        ledger["earnings_reserved"] = float(max(Decimal("0"), _money(ledger.get("earnings_reserved", 0)) - amount))
        if action == "rejected":
            ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) + amount)
        _append_transaction(ledger, f"withdrawal_{action}", amount, {"withdrawal_id": withdrawal_id, "admin_id": str(admin_id)})
        return True, dict(request)


def list_pending_earnings_withdrawals():
    pending = []
    for reseller_id in list_bots():
        for request in get_ledger(reseller_id).get("withdrawals", []):
            if request.get("status") == "pending":
                pending.append({"reseller_id": reseller_id, **request})
    return pending
