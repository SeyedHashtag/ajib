"""Referral and recruitment actions backed by existing accounting services."""
import json
import os
import time
from . import database, web_store
from .web_services import ServiceError


def perform(user_id, scope, kind, key, payload):
    if scope != "main":
        raise ServiceError("Use this storefront's Telegram bot for reward actions", 409)
    request_hash = web_store.digest(json.dumps(payload, sort_keys=True))
    with database.transaction(operation="web_reward_action") as connection:
        existing = connection.execute("SELECT request_hash,result_json FROM web_actions WHERE scope=? AND user_id=? AND kind=? AND key=?",
            (scope, str(user_id), kind, key)).fetchone()
        if existing:
            if existing[0] != request_hash:
                raise ServiceError("This request key was already used for another action", 409)
            return json.loads(existing[1])
        from . import referral, recruitment
        if kind == "code":
            result = {"code": referral.get_or_create_referral_code(int(user_id))}
        elif kind == "wallet":
            address = str(payload.get("address", "")).strip()
            if not 10 <= len(address) <= 256 or any(ord(c) < 33 for c in address):
                raise ServiceError("Enter a valid wallet address")
            referral.set_wallet_address(int(user_id), address)
            result = {"saved": True}
        elif kind == "attribution":
            success, result = referral.process_referral(int(user_id), payload["code"])
            if not success:
                raise ServiceError(str(result), 409)
            result = {"registered": True}
        elif kind == "withdrawal":
            success, request = referral.process_withdrawal_request(int(user_id))
            if not success:
                raise ServiceError(str(request), 409)
            result = {"id": request["id"], "amount": request["amount"], "status": "pending"}
            for admin in json.loads(os.getenv("ADMIN_USER_IDS", "[]")):
                web_store.enqueue(connection, f"withdrawal:{request['id']}:{admin}", "main", admin,
                                  f"Referral withdrawal {request['id']} is awaiting review in the bot.")
        elif kind == "recruitment":
            result = recruitment.claim_recruitment_reward(user_id, payload["reseller_id"], payload["choice"])
            if not result:
                raise ServiceError("Reward is unavailable or has already been claimed", 409)
            result = {"claimed": True, "choice": payload["choice"]}
        else:
            raise ServiceError("Unknown reward action")
        connection.execute("INSERT INTO web_actions VALUES (?,?,?,?,?,?,?)",
            (scope, str(user_id), kind, key, request_hash, json.dumps(result), int(time.time())))
        web_store.audit(connection, user_id, scope, "referral." + kind, key)
        return result


def details(user_id, scope):
    connection = database.get_connection()
    withdrawals = []
    for row in connection.execute("SELECT withdrawal_id,payload_json FROM referral_withdrawals WHERE scope=? AND user_id=? ORDER BY requested_at DESC LIMIT 100", (scope, str(user_id))):
        data = json.loads(row[1])
        withdrawals.append({"id": row[0], **{k: data[k] for k in ("amount", "status", "requested_at", "paid_at", "wallet") if k in data}})
    rewards = []
    if scope == "main":
        from .recruitment import claimable_recruitment_rewards
        rewards = [{key: item[key] for key in ("reseller_id", "reward_amount") if key in item}
                   for item in claimable_recruitment_rewards(user_id)]
    from .referral import REFERRAL_MIN_PAYOUT_BALANCE
    return {"withdrawals": withdrawals, "recruitment": rewards,
            "minimum_withdrawal": REFERRAL_MIN_PAYOUT_BALANCE, "actions_available": scope == "main"}
