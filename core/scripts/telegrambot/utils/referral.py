import json
import os
import threading
import random
import string
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime

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

def _default_referrals_data():
    return {
        "referrals": {},  # user_id -> referrer_id
        "stats": {},      # user_id -> { "count": 0, "total_earnings": 0, "available_balance": 0 }
        "codes": {},      # code -> user_id
        "user_codes": {},  # user_id -> code
        "wallets": {},    # user_id -> wallet_address
        "referral_details": {},  # invited user_id -> invite metadata
        "rewarded_orders": {},  # payment/order ID -> immutable reward record
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

def process_referral(new_user_id, code, telegram_username=None, first_name=None, last_name=None):
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
            "invited_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
        reward_amount = float(purchase_amount) * (REFERRAL_REWARD_PERCENTAGE / 100)
        if referrer_id not in data["stats"]:
            data["stats"][referrer_id] = {
                "count": 0, "total_earnings": 0, "available_balance": 0
            }
        data["stats"][referrer_id]["total_earnings"] += reward_amount
        data["stats"][referrer_id]["available_balance"] += reward_amount
        if order_key:
            rewarded_orders[order_key] = {
                "referrer_id": referrer_id,
                "amount": reward_amount,
                "rewarded_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
        return True, referrer_id, reward_amount

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
            "paid_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
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

            paid_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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

        requested_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
