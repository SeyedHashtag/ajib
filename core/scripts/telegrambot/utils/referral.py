import json
import os
import threading
import random
import string
import uuid
import math
from copy import deepcopy
from contextlib import contextmanager
from utils.time_utils import format_utc_timestamp

REFERRALS_FILE = '/etc/ajib/core/scripts/telegrambot/referrals.json'
referral_lock = threading.RLock()


def _atomic_helpers():
    if not (__package__ or "").startswith("utils"):
        return None
    try:
        from utils.atomic_store import locked_json, read_json
        return locked_json, read_json
    except ImportError:
        return None

# Configuration
REFERRAL_REWARD_PERCENTAGE = 20  # 20% reward
REFERRAL_MIN_PAYOUT_BALANCE = 2.0
REFERRAL_BUYER_DISCOUNT_PERCENTAGE = 5.0
REFERRAL_COMBINED_DISCOUNT_CAP_PERCENTAGE = 10.0


def _env_flag(name, default=True):
    value = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _env_percentage(name, default, maximum=100.0):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value):
        return float(default)
    return max(0.0, min(float(maximum), value))


def referral_buyer_discount_enabled():
    """Emergency kill switch for the two-sided first-purchase incentive."""
    try:
        from utils.growth_features import BUYER_DISCOUNTS, is_growth_feature_enabled

        if not is_growth_feature_enabled(BUYER_DISCOUNTS):
            return False
    except ImportError:
        # Keep the legacy-specific switch usable during rolling upgrades.
        pass
    except ValueError:
        return False
    return _env_flag("AJIB_REFERRAL_BUYER_DISCOUNT_ENABLED", True)


def referral_buyer_discount_percent():
    return _env_percentage(
        "AJIB_REFERRAL_BUYER_DISCOUNT_PERCENT",
        REFERRAL_BUYER_DISCOUNT_PERCENTAGE,
        REFERRAL_COMBINED_DISCOUNT_CAP_PERCENTAGE,
    )


def combined_discount_cap_percent():
    return _env_percentage(
        "AJIB_COMBINED_DISCOUNT_CAP_PERCENT",
        REFERRAL_COMBINED_DISCOUNT_CAP_PERCENTAGE,
        100.0,
    )

def _default_referrals_data():
    return {
        "referrals": {},  # user_id -> referrer_id
        "stats": {},      # user_id -> { "count": 0, "total_earnings": 0, "available_balance": 0 }
        "codes": {},      # code -> user_id
        "user_codes": {},  # user_id -> code
        "wallets": {},    # user_id -> wallet_address
        "referral_details": {},  # invited user_id -> invite metadata
        "rewarded_orders": {},  # payment/order ID -> immutable reward record
        "discount_reservations": {},  # order ID -> reserved first-purchase benefit
        "discount_redemptions": {},  # invitee user ID -> completed discounted order
        "manual_rewards": {},  # immutable non-order rewards such as reseller recruitment
        "recruitment_milestones": {},  # referred reseller ID -> milestone/claim state
        "account_credits": {},  # compatibility mirror for user credit balances and history
        "pending_withdrawals": [],  # referral withdrawal requests awaiting admin payout
        "payouts": []     # paid referral payout audit records
    }

def _ensure_referrals_shape(data):
    defaults = _default_referrals_data()
    for key, value in defaults.items():
        data.setdefault(key, value.copy() if isinstance(value, dict) else list(value))
    if not isinstance(data.get("payouts"), list):
        data["payouts"] = []
    if not isinstance(data.get("pending_withdrawals"), list):
        data["pending_withdrawals"] = []
    return data

def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)

def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)

def load_referrals():
    with referral_lock:
        try:
            helpers = _atomic_helpers()
            if helpers:
                data = helpers[1](REFERRALS_FILE, _default_referrals_data())
            elif os.path.exists(REFERRALS_FILE):
                with open(REFERRALS_FILE, "r") as handle:
                    data = json.load(handle)
            else:
                data = _default_referrals_data()
            if isinstance(data, dict):
                return _ensure_referrals_shape(data)
        except Exception:
            pass
        return _default_referrals_data()

def save_referrals(data):
    with referral_lock:
        helpers = _atomic_helpers()
        if helpers:
            with helpers[0](REFERRALS_FILE, _default_referrals_data()) as stored:
                if not isinstance(stored, dict):
                    raise ValueError("Referral database must contain a JSON object.")
                stored.clear()
                stored.update(_ensure_referrals_shape(data))
        else:
            os.makedirs(os.path.dirname(REFERRALS_FILE), exist_ok=True)
            with open(REFERRALS_FILE, "w") as handle:
                json.dump(data, handle, indent=4)


@contextmanager
def _edit_referrals():
    with referral_lock:
        helpers = _atomic_helpers()
        if helpers:
            with helpers[0](REFERRALS_FILE, _default_referrals_data()) as data:
                if not isinstance(data, dict):
                    raise ValueError("Referral database must contain a JSON object.")
                yield _ensure_referrals_shape(data)
            return
        data = load_referrals()
        original = deepcopy(data)
        yield data
        if data != original:
            save_referrals(data)


def generate_unique_code():
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(8))

def get_or_create_referral_code(user_id):
    user_id_str = str(user_id)
    with _edit_referrals() as data:
        if user_id_str in data["user_codes"]:
            return data["user_codes"][user_id_str]

        while True:
            code = generate_unique_code()
            if code not in data["codes"]:
                break

        data["codes"][code] = user_id_str
        data["user_codes"][user_id_str] = code
        data["stats"][user_id_str] = data["stats"].get(user_id_str, {
            "count": 0,
            "total_earnings": 0,
            "available_balance": 0
        })
        return code

def process_referral(
    new_user_id,
    code,
    telegram_username=None,
    first_name=None,
    last_name=None,
    campaign_type="customer",
):
    new_user_id_str = str(new_user_id)
    with _edit_referrals() as data:
        if new_user_id_str in data["referrals"]:
            return False, "User already referred"
        if code not in data["codes"]:
            return False, "Invalid referral code"

        referrer_id = data["codes"][code]
        if referrer_id == new_user_id_str:
            return False, "Cannot refer yourself"

        data["referrals"][new_user_id_str] = referrer_id
        data.setdefault("referral_details", {})[new_user_id_str] = {
            "telegram_user_id": new_user_id,
            "telegram_username": telegram_username,
            "first_name": first_name,
            "last_name": last_name,
            "referral_code": code,
            "referrer_id": referrer_id,
            "invited_at": format_utc_timestamp(),
            "campaign_type": (
                "reseller" if str(campaign_type).strip().lower() == "reseller" else "customer"
            ),
        }
        if referrer_id not in data["stats"]:
            data["stats"][referrer_id] = {
                "count": 0, "total_earnings": 0, "available_balance": 0
            }
        data["stats"][referrer_id]["count"] += 1
        return True, referrer_id

def add_referral_reward(user_id, purchase_amount, order_id=None):
    """
    Add reward to the referrer of user_id based on purchase_amount.
    """
    user_id_str = str(user_id)
    with _edit_referrals() as data:
        if user_id_str not in data["referrals"]:
            return False

        referrer_id = data["referrals"][user_id_str]
        order_key = str(order_id or "").strip()
        rewarded_orders = data.setdefault("rewarded_orders", {})
        if order_key and order_key in rewarded_orders:
            return False
        reward_amount = round(
            float(purchase_amount) * (REFERRAL_REWARD_PERCENTAGE / 100),
            2,
        )
        if referrer_id not in data["stats"]:
            data["stats"][referrer_id] = {
                "count": 0, "total_earnings": 0, "available_balance": 0
            }
        data["stats"][referrer_id]["total_earnings"] = round(
            _safe_float(data["stats"][referrer_id].get("total_earnings"))
            + reward_amount,
            2,
        )
        data["stats"][referrer_id]["available_balance"] = round(
            _safe_float(data["stats"][referrer_id].get("available_balance"))
            + reward_amount,
            2,
        )
        if order_key:
            rewarded_orders[order_key] = {
                "referrer_id": referrer_id,
                "invitee_user_id": user_id_str,
                "amount": reward_amount,
                "rewarded_at": format_utc_timestamp(),
            }
        return True, referrer_id, reward_amount


def get_referral_attribution(user_id):
    """Return immutable referral attribution without exposing mutable internals."""
    data = load_referrals()
    invitee_id = str(user_id)
    referrer_id = data.get("referrals", {}).get(invitee_id)
    if not referrer_id:
        return None
    detail = data.get("referral_details", {}).get(invitee_id, {})
    return {
        "invitee_user_id": invitee_id,
        "referrer_user_id": str(referrer_id),
        "referral_code": detail.get("referral_code"),
        "campaign_type": detail.get("campaign_type", "customer"),
        "invited_at": detail.get("invited_at"),
    }


def _completed_main_purchase_exists(user_id, payments=None):
    if payments is None:
        try:
            from utils.payment_records import get_user_payments

            payments = get_user_payments(int(user_id) if str(user_id).isdigit() else user_id)
        except Exception:
            payments = {}
    terminal_paid_statuses = {
        "completed",
        "paid",
        "approved",
        "renewal_reserved",
    }
    for record in (payments or {}).values():
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "").strip().lower() in terminal_paid_statuses:
            if record.get("type") != "settlement" and record.get("plan_gb") != "Settlement":
                return True
    return False


def get_invitee_discount_eligibility(user_id, payments=None):
    """Describe a referred user's one-time discount without reserving it."""
    invitee_id = str(user_id)
    data = load_referrals()
    if not referral_buyer_discount_enabled():
        return {"eligible": False, "reason": "disabled", "percent": 0.0}
    if invitee_id not in data.get("referrals", {}):
        return {"eligible": False, "reason": "not_referred", "percent": 0.0}
    if invitee_id in data.get("discount_redemptions", {}):
        return {"eligible": False, "reason": "already_redeemed", "percent": 0.0}
    if _completed_main_purchase_exists(user_id, payments=payments):
        return {"eligible": False, "reason": "existing_customer", "percent": 0.0}
    return {
        "eligible": True,
        "reason": None,
        "percent": referral_buyer_discount_percent(),
        "referrer_id": str(data["referrals"][invitee_id]),
    }


def reserve_invitee_discount(user_id, order_id, payments=None):
    """Atomically reserve the first-order benefit for exactly one live checkout."""
    invitee_id = str(user_id)
    order_key = str(order_id or "").strip()
    if not order_key:
        return None
    if not referral_buyer_discount_enabled():
        return None
    # Check historical payments before taking the referral-store lock. The
    # redemption/reservation checks are repeated atomically below.
    if _completed_main_purchase_exists(user_id, payments=payments):
        return None
    with _edit_referrals() as data:
        if invitee_id not in data.get("referrals", {}):
            return None
        if invitee_id in data.setdefault("discount_redemptions", {}):
            return None
        reservations = data.setdefault("discount_reservations", {})
        existing = reservations.get(order_key)
        if isinstance(existing, dict):
            return dict(existing) if existing.get("invitee_user_id") == invitee_id else None
        if any(
            isinstance(item, dict)
            and item.get("invitee_user_id") == invitee_id
            and item.get("status", "reserved") == "reserved"
            for item in reservations.values()
        ):
            return None
        reservation = {
            "order_id": order_key,
            "invitee_user_id": invitee_id,
            "referrer_id": str(data["referrals"][invitee_id]),
            "percent": referral_buyer_discount_percent(),
            "status": "reserved",
            "reserved_at": format_utc_timestamp(),
        }
        reservations[order_key] = reservation
        return dict(reservation)


def release_invitee_discount(order_id, user_id=None):
    order_key = str(order_id or "").strip()
    if not order_key:
        return False
    with _edit_referrals() as data:
        reservation = data.setdefault("discount_reservations", {}).get(order_key)
        if not isinstance(reservation, dict):
            return False
        if user_id is not None and reservation.get("invitee_user_id") != str(user_id):
            return False
        data["discount_reservations"].pop(order_key, None)
        return True


def redeem_invitee_discount(user_id, order_id):
    """Consume a reserved benefit after payment completion, idempotently."""
    invitee_id = str(user_id)
    order_key = str(order_id or "").strip()
    if not order_key:
        return False
    with _edit_referrals() as data:
        redemptions = data.setdefault("discount_redemptions", {})
        existing = redemptions.get(invitee_id)
        if isinstance(existing, dict):
            return existing.get("order_id") == order_key
        reservation = data.setdefault("discount_reservations", {}).get(order_key)
        if not isinstance(reservation, dict) or reservation.get("invitee_user_id") != invitee_id:
            return False
        redemption = {
            **reservation,
            "status": "redeemed",
            "redeemed_at": format_utc_timestamp(),
        }
        redemptions[invitee_id] = redemption
        data["discount_reservations"].pop(order_key, None)
        return True


def stacked_discount_percent(invitee_percent=0, payment_discount_percent=0):
    """Combine independently earned discounts while enforcing the global cap."""
    try:
        invitee = max(0.0, float(invitee_percent or 0))
        payment = max(0.0, float(payment_discount_percent or 0))
    except (TypeError, ValueError):
        return 0.0
    return min(combined_discount_cap_percent(), invitee + payment)


def credit_manual_referral_reward(user_id, amount, reward_id, metadata=None):
    """Credit an idempotent non-order reward to the withdrawable referral balance."""
    user_key = str(user_id)
    reward_key = str(reward_id or "").strip()
    amount_value = _safe_float(amount)
    if not reward_key or amount_value <= 0:
        return False
    with _edit_referrals() as data:
        rewards = data.setdefault("manual_rewards", {})
        if reward_key in rewards:
            return True
        stats = data.setdefault("stats", {}).setdefault(
            user_key,
            {"count": 0, "total_earnings": 0, "available_balance": 0},
        )
        stats["total_earnings"] = round(_safe_float(stats.get("total_earnings")) + amount_value, 2)
        stats["available_balance"] = round(_safe_float(stats.get("available_balance")) + amount_value, 2)
        rewards[reward_key] = {
            "user_id": user_key,
            "amount": round(amount_value, 2),
            "credited_at": format_utc_timestamp(),
            "metadata": dict(metadata or {}),
        }
        return True


def get_referral_progress(user_id):
    data = load_referrals()
    user_key = str(user_id)
    stats = data.get("stats", {}).get(user_key, {})
    converted_invitees = {
        str(reward.get("invitee_user_id"))
        for reward in data.get("rewarded_orders", {}).values()
        if isinstance(reward, dict)
        and str(reward.get("referrer_id")) == user_key
        and reward.get("invitee_user_id") is not None
    }
    return {
        "invited": _safe_int(stats.get("count", 0)),
        "first_purchases": len(converted_invitees),
        "total_earnings": _safe_float(stats.get("total_earnings", 0)),
        "available_balance": _safe_float(stats.get("available_balance", 0)),
    }

def get_referral_stats(user_id):
    data = load_referrals()
    user_id_str = str(user_id)
    return data["stats"].get(user_id_str, {"count": 0, "total_earnings": 0, "available_balance": 0})

def get_referrer(user_id):
    data = load_referrals()
    return data["referrals"].get(str(user_id))

def set_wallet_address(user_id, address):
    user_id_str = str(user_id)
    with _edit_referrals() as data:
        data.setdefault("wallets", {})[user_id_str] = address
    return True

def get_wallet_address(user_id):
    data = load_referrals()
    user_id_str = str(user_id)
    return data.get("wallets", {}).get(user_id_str)

def get_eligible_referral_users(min_balance=REFERRAL_MIN_PAYOUT_BALANCE):
    data = load_referrals()
    wallets = data.get("wallets", {})
    eligible_users = []

    for user_id, stats in data.get("stats", {}).items():
        if not isinstance(stats, dict):
            continue
        available_balance = _safe_float(stats.get("available_balance", 0))
        if available_balance < float(min_balance):
            continue
        wallet = wallets.get(str(user_id))
        eligible_users.append({
            "user_id": str(user_id),
            "available_balance": available_balance,
            "total_earnings": _safe_float(stats.get("total_earnings", 0)),
            "invited_count": _safe_int(stats.get("count", 0)),
            "wallet": wallet,
            "has_wallet": bool(wallet),
        })

    eligible_users.sort(key=lambda item: (-item["available_balance"], str(item["user_id"])))
    return eligible_users

def mark_referral_payout_paid(user_id, admin_user_id):
    with _edit_referrals() as data:
        user_id_str = str(user_id)
        stats = data.get("stats", {}).get(user_id_str)

        if not isinstance(stats, dict):
            return False, "No stats found"

        available_balance = _safe_float(stats.get("available_balance", 0))
        if available_balance < REFERRAL_MIN_PAYOUT_BALANCE:
            return False, "Insufficient balance (Minimum $2.00)"

        wallet = data.get("wallets", {}).get(user_id_str)
        if not wallet:
            return False, "Wallet address not set"

        payout = {
            "id": str(uuid.uuid4()),
            "user_id": user_id_str,
            "admin_user_id": str(admin_user_id),
            "amount": available_balance,
            "wallet": wallet,
            "paid_at": format_utc_timestamp(),
            "available_balance_before": available_balance,
            "available_balance_after": 0,
            "total_earnings_snapshot": _safe_float(stats.get("total_earnings", 0)),
            "invited_count_snapshot": _safe_int(stats.get("count", 0)),
        }

        stats["available_balance"] = 0
        data.setdefault("payouts", []).append(payout)
        return True, payout

def _is_pending_withdrawal_request(request_data):
    return isinstance(request_data, dict) and request_data.get("status") == "pending"

def get_pending_withdrawal_requests():
    data = load_referrals()
    pending_requests = [
        dict(request_data)
        for request_data in data.get("pending_withdrawals", [])
        if _is_pending_withdrawal_request(request_data)
    ]
    pending_requests.sort(key=lambda item: (item.get("requested_at") or "", str(item.get("user_id") or "")))
    return pending_requests

def mark_withdrawal_request_paid(request_id, admin_user_id):
    with _edit_referrals() as data:
        request_id_str = str(request_id)

        for withdrawal_request in data.get("pending_withdrawals", []):
            if str(withdrawal_request.get("id")) != request_id_str:
                continue

            if withdrawal_request.get("status") != "pending":
                return False, f"Withdrawal request already {withdrawal_request.get('status', 'processed')}"

            paid_at = format_utc_timestamp()
            withdrawal_request["status"] = "paid"
            withdrawal_request["paid_at"] = paid_at
            withdrawal_request["admin_user_id"] = str(admin_user_id)

            payout = {
                "id": str(uuid.uuid4()),
                "withdrawal_request_id": request_id_str,
                "user_id": str(withdrawal_request.get("user_id")),
                "admin_user_id": str(admin_user_id),
                "amount": _safe_float(withdrawal_request.get("amount", 0)),
                "wallet": withdrawal_request.get("wallet"),
                "paid_at": paid_at,
                "available_balance_before": _safe_float(withdrawal_request.get("available_balance_before", 0)),
                "available_balance_after": _safe_float(withdrawal_request.get("available_balance_after", 0)),
                "total_earnings_snapshot": _safe_float(withdrawal_request.get("total_earnings", 0)),
                "invited_count_snapshot": _safe_int(withdrawal_request.get("invited_count", 0)),
            }

            data.setdefault("payouts", []).append(payout)
            return True, payout

        return False, "Withdrawal request not found"

def _get_invitee_payments(invitee_user_id):
    try:
        from utils.payment_records import load_payments
        payments = load_payments()
    except Exception:
        payments = {}

    invitee_payments = []
    for payment_id, payment_data in payments.items():
        if str(payment_data.get("user_id")) != str(invitee_user_id):
            continue

        invitee_payments.append({
            "payment_id": payment_id,
            "status": payment_data.get("status"),
            "price": payment_data.get("price"),
            "plan_gb": payment_data.get("plan_gb"),
            "days": payment_data.get("days"),
            "created_at": payment_data.get("created_at"),
            "updated_at": payment_data.get("updated_at")
        })

    return invitee_payments

def build_withdrawal_audit_payload(user_id, telegram_username, withdrawal_data):
    data = load_referrals()
    user_id_str = str(user_id)
    referral_details = data.get("referral_details", {})

    invitees = []
    for invitee_id, referrer_id in data.get("referrals", {}).items():
        if str(referrer_id) != user_id_str:
            continue

        details = referral_details.get(str(invitee_id), {})
        metadata_complete = bool(details.get("invited_at"))
        invitees.append({
            "telegram_user_id": int(invitee_id) if str(invitee_id).isdigit() else invitee_id,
            "telegram_username": details.get("telegram_username"),
            "first_name": details.get("first_name"),
            "last_name": details.get("last_name"),
            "referral_code": details.get("referral_code"),
            "invited_at": details.get("invited_at"),
            "metadata_complete": metadata_complete,
            "payments": _get_invitee_payments(invitee_id)
        })

    invitees.sort(key=lambda item: (item.get("invited_at") is None, item.get("invited_at") or "", str(item.get("telegram_user_id"))))

    return {
        "request": {
            "requested_at": withdrawal_data.get("requested_at"),
            "requester_user_id": user_id,
            "requester_username": telegram_username,
            "amount": withdrawal_data.get("amount"),
            "wallet": withdrawal_data.get("wallet"),
            "available_balance_after": withdrawal_data.get("available_balance_after"),
            "total_earnings": withdrawal_data.get("total_earnings"),
            "invited_count": withdrawal_data.get("invited_count")
        },
        "invitees": invitees
    }

def process_withdrawal_request(user_id, telegram_username=None):
    with _edit_referrals() as data:
        user_id_str = str(user_id)

        if any(
            _is_pending_withdrawal_request(request_data) and str(request_data.get("user_id")) == user_id_str
            for request_data in data.get("pending_withdrawals", [])
        ):
            return False, "Withdrawal request already pending"

        stats = data["stats"].get(user_id_str)
        if not stats:
            return False, "No stats found"

        available_balance = _safe_float(stats.get("available_balance", 0))
        if available_balance < REFERRAL_MIN_PAYOUT_BALANCE:
            return False, "Insufficient balance (Minimum $2.00)"

        wallet = data.get("wallets", {}).get(user_id_str)
        if not wallet:
            return False, "Wallet address not set"

        requested_at = format_utc_timestamp()
        withdrawal_request = {
            "id": str(uuid.uuid4()),
            "status": "pending",
            "user_id": user_id_str,
            "telegram_username": telegram_username,
            "amount": available_balance,
            "wallet": wallet,
            "requested_at": requested_at,
            "available_balance_before": available_balance,
            "available_balance_after": 0,
            "total_earnings": _safe_float(stats.get("total_earnings", 0)),
            "invited_count": _safe_int(stats.get("count", 0)),
        }

        stats["available_balance"] = 0
        data.setdefault("pending_withdrawals", []).append(withdrawal_request)
        return True, dict(withdrawal_request)
