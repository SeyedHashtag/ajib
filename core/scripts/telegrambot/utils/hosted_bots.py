import hashlib
import os
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .atomic_store import locked_json, read_json
from . import reseller as reseller_store


BOT_DIR = os.getenv("AJIB_BOT_DIR", "/etc/ajib/core/scripts/telegrambot")
HOSTED_ROOT = os.path.join(BOT_DIR, "hosted_bots")
REGISTRY_FILE = os.path.join(BOT_DIR, "hosted_bots.json")
SECRETS_FILE = os.path.join(BOT_DIR, "hosted_bot_tokens.json")
MAX_ACTIVE_BOTS = 50
CRYPTO_DISCOUNT_PERCENT = Decimal("5")
MINIMUM_PAYOUT = Decimal("2.00")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _tenant_dir(reseller_id):
    return os.path.join(HOSTED_ROOT, str(reseller_id))


def tenant_file(reseller_id, name):
    return os.path.join(_tenant_dir(reseller_id), name)


def default_settings():
    return {
        "markup_percent": 20.0,
        "enabled_plan_ids": [],
        "plan_selection_configured": False,
        "exchange_rate": 1.0,
        "card_number": "",
        "welcome_text": "Welcome!",
        "support_text": "Contact the reseller for support.",
        "referral_margin_percent": 20.0,
        "crypto_enabled": False,
    }


def get_settings(reseller_id):
    settings = default_settings()
    stored = read_json(tenant_file(reseller_id, "settings.json"), {})
    if isinstance(stored, dict):
        settings.update(stored)
    return settings


def update_settings(reseller_id, updates):
    allowed = set(default_settings())
    with locked_json(tenant_file(reseller_id, "settings.json"), default_settings()) as settings:
        settings.update({key: value for key, value in updates.items() if key in allowed})
        settings["updated_at"] = _now()
        return dict(settings)


def calculate_quote(wholesale, markup_percent, referral_margin_percent=0, referred=False):
    wholesale_amount = _money(wholesale)
    markup = Decimal(str(markup_percent or 0))
    retail = _money(wholesale_amount * (Decimal("1") + markup / Decimal("100")))
    collected = _money(retail * (Decimal("1") - CRYPTO_DISCOUNT_PERCENT / Decimal("100")))
    margin = _money(collected - wholesale_amount)
    card_margin = _money(retail - wholesale_amount)
    referral_rate = Decimal(str(referral_margin_percent or 0)) if referred else Decimal("0")
    referral_reward = _money(max(Decimal("0"), margin) * referral_rate / Decimal("100"))
    card_referral_reward = _money(max(Decimal("0"), card_margin) * referral_rate / Decimal("100"))
    return {
        "wholesale": float(wholesale_amount),
        "retail": float(retail),
        "crypto_collected": float(collected),
        "crypto_margin": float(margin),
        "card_margin": float(card_margin),
        "referral_reward": float(referral_reward),
        "crypto_referral_reward": float(referral_reward),
        "card_referral_reward": float(card_referral_reward),
        "crypto_supported": collected >= wholesale_amount,
        "discount_percent": float(CRYPTO_DISCOUNT_PERCENT),
    }


def _token_fingerprint(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def list_bots():
    data = read_json(REGISTRY_FILE, {})
    return data if isinstance(data, dict) else {}


def get_bot(reseller_id):
    return list_bots().get(str(reseller_id))


def get_token(reseller_id):
    data = read_json(SECRETS_FILE, {})
    return data.get(str(reseller_id)) if isinstance(data, dict) else None


def register_bot(reseller_id, token, bot_info, main_bot_id=None):
    reseller_key = str(reseller_id)
    mapping = bot_info if isinstance(bot_info, dict) else {}
    bot_id = str(getattr(bot_info, "id", "") or mapping.get("id") or "")
    username = str(getattr(bot_info, "username", "") or mapping.get("username") or "")
    if not bot_id or not username:
        return False, "Telegram did not return a usable bot identity."
    if main_bot_id is not None and bot_id == str(main_bot_id):
        return False, "The main ajib bot token cannot be used as a reseller bot."

    fingerprint = _token_fingerprint(token)
    with locked_json(REGISTRY_FILE, {}) as registry:
        for owner_id, record in registry.items():
            if owner_id == reseller_key:
                continue
            if str(record.get("bot_id")) == bot_id or record.get("token_fingerprint") == fingerprint:
                return False, "This Telegram bot is already connected to another reseller."
        active_others = sum(
            1 for owner_id, record in registry.items()
            if owner_id != reseller_key and record.get("status") in {"active", "starting", "error"}
        )
        if active_others >= MAX_ACTIVE_BOTS:
            return False, f"This installation already has {MAX_ACTIVE_BOTS} hosted bots."
        previous = registry.get(reseller_key, {})
        # Commit the secret before publishing its fingerprint. Readers continue
        # using the old registry until the atomic registry replacement below.
        with locked_json(SECRETS_FILE, {}) as secrets:
            secrets[reseller_key] = str(token).strip()
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
    update_settings(reseller_key, {})
    return True, get_bot(reseller_key)


def disconnect_bot(reseller_id):
    key = str(reseller_id)
    found = False
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
    key = str(reseller_id)
    with locked_json(REGISTRY_FILE, {}) as registry:
        if key not in registry:
            return False
        registry[key]["status"] = str(status)
        registry[key]["last_error"] = str(error)[:500] if error else None
        registry[key]["updated_at"] = _now()
        return True


def set_bot_enabled(reseller_id, enabled):
    key = str(reseller_id)
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
    path = tenant_file(reseller_id, "ledger.json")
    with locked_json(path, _default_ledger()) as ledger:
        if not _append_transaction(ledger, "crypto_sale", margin, metadata, transaction_id=f"sale:{order_id}"):
            return False
        ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) + _money(margin))
        if _money(referral_reward) > 0:
            ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) + _money(referral_reward))
            _append_transaction(ledger, "referral_liability", referral_reward, metadata, transaction_id=f"referral:{order_id}")
        return True


def add_referral_liability(reseller_id, order_id, amount, metadata=None):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        if not _append_transaction(ledger, "referral_liability", amount, metadata, transaction_id=f"referral:{order_id}"):
            return False
        ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) + _money(amount))
        return True


def settle_referral_liability(reseller_id, withdrawal_id, amount):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        transaction_id = f"referral-paid:{withdrawal_id}"
        if any(item.get("id") == transaction_id for item in ledger.get("transactions", [])):
            return False
        value = min(_money(amount), _money(ledger.get("referral_liability", 0)))
        ledger["referral_liability"] = float(_money(ledger.get("referral_liability", 0)) - value)
        _append_transaction(ledger, "referral_paid", -value, {"withdrawal_id": withdrawal_id}, transaction_id)
        return True


def reserve_credit(reseller_id, reservation_id, amount, available_credit):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservations = ledger.setdefault("credit_reservations", {})
        if reservation_id in reservations:
            return True
        reserved = sum(_money(item.get("amount", 0)) for item in reservations.values())
        if reserved + _money(amount) > _money(available_credit):
            return False
        reservations[reservation_id] = {"amount": float(_money(amount)), "created_at": _now()}
        _append_transaction(ledger, "credit_reserved", amount, {"reservation_id": reservation_id})
        return True


def release_credit(reseller_id, reservation_id, kind="credit_released"):
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        reservation = ledger.setdefault("credit_reservations", {}).pop(str(reservation_id), None)
        if not reservation:
            return False
        _append_transaction(ledger, kind, reservation.get("amount", 0), {"reservation_id": reservation_id})
        return True


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
    current = reseller_store.get_reseller_data(reseller_id) or {}
    debt = _money(current.get("debt", 0))
    if debt <= 0:
        return False, "There is no debt to settle."
    path = tenant_file(reseller_id, "ledger.json")
    with locked_json(path, _default_ledger()) as ledger:
        amount = min(debt, _money(ledger.get("earnings_available", 0)))
        if amount <= 0:
            return False, "There are no available earnings."
        success, remaining = reseller_store.apply_reseller_payment(reseller_id, float(amount))
        if not success:
            return False, "Debt settlement failed."
        ledger["earnings_available"] = float(_money(ledger.get("earnings_available", 0)) - amount)
        _append_transaction(ledger, "earnings_to_debt", -amount, {"remaining_debt": remaining})
        return True, {"amount": float(amount), "remaining_debt": remaining}


def request_earnings_withdrawal(reseller_id, destination):
    debt = _money((reseller_store.get_reseller_data(reseller_id) or {}).get("debt", 0))
    if debt > 0:
        return False, "All reseller debt must be settled first."
    if not str(destination or "").strip():
        return False, "A payout destination is required."
    with locked_json(tenant_file(reseller_id, "ledger.json"), _default_ledger()) as ledger:
        if any(item.get("status") == "pending" for item in ledger.get("withdrawals", [])):
            return False, "A withdrawal is already pending."
        amount = _money(ledger.get("earnings_available", 0))
        if amount < MINIMUM_PAYOUT:
            return False, f"Minimum withdrawal is ${MINIMUM_PAYOUT:.2f}."
        request = {
            "id": str(uuid.uuid4()), "status": "pending", "amount": float(amount),
            "destination": str(destination).strip(), "requested_at": _now(),
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
