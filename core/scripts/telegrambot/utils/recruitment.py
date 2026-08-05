"""Productive-reseller recruitment milestones and one-time reward claims."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from utils import database
from utils.account_credit import credit_account, credit_account_in_transaction
from utils.growth_features import RECRUITMENT_REWARDS, is_growth_feature_enabled
from utils.referral import credit_manual_referral_reward, get_referral_attribution


DEFAULT_REQUIRED_SALES = 5
DEFAULT_REQUIRED_SETTLED = 100.0
DEFAULT_REWARD = 5.0
CLAIM_CHOICES = {"cash", "credit"}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def required_sales():
    return DEFAULT_REQUIRED_SALES


def required_settled_amount():
    return DEFAULT_REQUIRED_SETTLED


def recruitment_reward_amount():
    return DEFAULT_REWARD


def _cents(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid recruitment money amount") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid recruitment money amount")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(cents):
    return float((Decimal(int(cents or 0)) / Decimal(100)).quantize(Decimal("0.01")))


def _dump(value):
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def productive_sales_count(reseller_data):
    count = 0
    for config in (reseller_data or {}).get("configs", []) or []:
        if not isinstance(config, dict) or config.get("removed"):
            continue
        username = str(config.get("username") or "").strip().lower()
        plan = str(config.get("plan_gb") or config.get("gb") or "").strip().lower()
        if not username or username.startswith(("test", "ht")) or plan in {"test", "settlement"}:
            continue
        count += 1
    return count


def _row_result(row, *, newly_qualified=False):
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "reseller_id": row["reseller_id"],
        "referrer_id": row["referrer_id"],
        "sales_count": int(row["sales_count"] or 0),
        "settled_amount": _money(row["settled_cents"]),
        "status": row["status"],
        "reward_amount": _money(row["reward_cents"]),
        "qualified_at": row["qualified_at"],
        "claimed_at": row["claimed_at"],
        "choice": row["choice"],
        "metadata": payload if isinstance(payload, dict) else {},
        "newly_qualified": newly_qualified,
    }


def evaluate_recruitment_milestone(
    reseller_id,
    reseller_data=None,
    *,
    attribution=None,
    path=None,
):
    """Update progress and qualify a referred productive reseller once."""
    try:
        enabled = is_growth_feature_enabled(RECRUITMENT_REWARDS)
    except ValueError:
        enabled = False
    if not enabled:
        return None
    reseller_key = str(reseller_id)
    if reseller_data is None:
        from utils.reseller import get_reseller_data

        reseller_data = get_reseller_data(reseller_id) or {}
    attribution = attribution if attribution is not None else get_referral_attribution(reseller_id)
    if not attribution:
        return None
    referrer_id = str(attribution.get("referrer_user_id") or "")
    if not referrer_id or referrer_id == reseller_key:
        return None
    sales_count = productive_sales_count(reseller_data)
    settled_cents = _cents((reseller_data or {}).get("total_paid", 0))
    reward_cents = _cents(recruitment_reward_amount())
    qualifies = (
        sales_count >= required_sales()
        and settled_cents >= _cents(required_settled_amount())
    )
    qualified_at = _now() if qualifies else None
    metadata = {
        "campaign_type": attribution.get("campaign_type", "customer"),
        "referral_code": attribution.get("referral_code"),
        "required_sales": required_sales(),
        "required_settled_amount": required_settled_amount(),
    }
    with database.write_transaction(path, operation="recruitment_milestone_evaluate") as connection:
        existing = connection.execute(
            "SELECT * FROM recruitment_milestones WHERE reseller_id=?",
            (reseller_key,),
        ).fetchone()
        previous_status = existing["status"] if existing is not None else None
        status = previous_status if previous_status in {"qualified", "claimed"} else (
            "qualified" if qualifies else "tracking"
        )
        if existing is not None and existing["qualified_at"]:
            qualified_at = existing["qualified_at"]
        connection.execute(
            """
            INSERT INTO recruitment_milestones(
                reseller_id, referrer_id, sales_count, settled_cents, status,
                reward_cents, qualified_at, claimed_at, choice, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(reseller_id) DO UPDATE SET
                referrer_id=recruitment_milestones.referrer_id,
                sales_count=excluded.sales_count,
                settled_cents=excluded.settled_cents,
                status=CASE
                    WHEN recruitment_milestones.status='claimed' THEN 'claimed'
                    WHEN recruitment_milestones.status='qualified' THEN 'qualified'
                    ELSE excluded.status
                END,
                reward_cents=excluded.reward_cents,
                qualified_at=COALESCE(recruitment_milestones.qualified_at, excluded.qualified_at),
                payload_json=recruitment_milestones.payload_json
            """,
            (
                reseller_key,
                referrer_id,
                sales_count,
                settled_cents,
                status,
                reward_cents,
                qualified_at,
                _dump(metadata),
            ),
        )
        row = connection.execute(
            "SELECT * FROM recruitment_milestones WHERE reseller_id=?",
            (reseller_key,),
        ).fetchone()
        return _row_result(
            row,
            newly_qualified=(previous_status not in {"qualified", "claimed"} and row["status"] == "qualified"),
        )


def claimable_recruitment_rewards(referrer_id, *, path=None):
    referrer_key = str(referrer_id)
    with database.read_transaction(path, operation="recruitment_claimable") as connection:
        return [
            _row_result(row)
            for row in connection.execute(
                """
                SELECT * FROM recruitment_milestones
                WHERE referrer_id=? AND status='qualified'
                ORDER BY qualified_at, reseller_id
                """,
                (referrer_key,),
            )
        ]


def recruitment_progress_for_referrer(referrer_id, *, path=None):
    referrer_key = str(referrer_id)
    with database.read_transaction(path, operation="recruitment_progress") as connection:
        return [
            _row_result(row)
            for row in connection.execute(
                """
                SELECT * FROM recruitment_milestones
                WHERE referrer_id=? ORDER BY rowid
                """,
                (referrer_key,),
            )
        ]


def claim_recruitment_reward(
    referrer_id,
    reseller_id,
    choice,
    *,
    path=None,
    cash_creditor=None,
    purchase_creditor=None,
):
    """Claim a qualified reward to cash balance or main-account credit once."""
    referrer_key = str(referrer_id)
    reseller_key = str(reseller_id)
    choice_key = str(choice or "").strip().lower()
    if choice_key not in CLAIM_CHOICES:
        raise ValueError("Invalid recruitment reward choice")
    reward_id = f"recruitment:{reseller_key}"
    cash_creditor = cash_creditor or credit_manual_referral_reward
    purchase_creditor = purchase_creditor or credit_account
    with database.write_transaction(path, operation="recruitment_claim") as connection:
        row = connection.execute(
            "SELECT * FROM recruitment_milestones WHERE reseller_id=?",
            (reseller_key,),
        ).fetchone()
        if row is None or str(row["referrer_id"]) != referrer_key:
            return None
        if row["status"] == "claimed":
            if row["choice"] != choice_key:
                return None
            return _row_result(row)
        if row["status"] != "qualified":
            return None
        reward = _money(row["reward_cents"])
        metadata = {"reseller_id": reseller_key, "kind": "productive_reseller"}
        if choice_key == "cash":
            if not cash_creditor(referrer_key, reward, reward_id, metadata):
                raise RuntimeError("Recruitment cash reward could not be credited")
        elif purchase_creditor is credit_account:
            credit_account_in_transaction(
                connection,
                referrer_key,
                reward,
                reward_id,
                source="recruitment",
                metadata=metadata,
            )
        else:
            try:
                purchase_creditor(
                    referrer_key,
                    reward,
                    reward_id,
                    source="recruitment",
                    metadata=metadata,
                    path=path,
                )
            except TypeError:
                purchase_creditor(referrer_key, reward, reward_id, metadata)
        claimed_at = _now()
        connection.execute(
            """
            UPDATE recruitment_milestones
            SET status='claimed', claimed_at=?, choice=?
            WHERE reseller_id=? AND status='qualified'
            """,
            (claimed_at, choice_key, reseller_key),
        )
        claimed = connection.execute(
            "SELECT * FROM recruitment_milestones WHERE reseller_id=?",
            (reseller_key,),
        ).fetchone()
        return _row_result(claimed)


def evaluate_and_notify_recruitment_milestone(reseller_id, reseller_data=None, *, path=None):
    """Evaluate progress and notify the recruiter once qualification is reached."""
    result = evaluate_recruitment_milestone(
        reseller_id,
        reseller_data,
        path=path,
    )
    if not result or not result.get("newly_qualified"):
        return result
    try:
        from telebot import types
        from utils.command import bot
        from utils.language import get_user_language
        from utils.translations import get_message_text

        referrer_id = int(result["referrer_id"])
        language = get_user_language(referrer_id)
        message = get_message_text(language, "recruitment_reward_qualified").format(
            amount=f"{result['reward_amount']:.2f}",
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(
                get_message_text(language, "recruitment_claim_cash"),
                callback_data=f"recruit_reward:{result['reseller_id']}:cash",
            ),
            types.InlineKeyboardButton(
                get_message_text(language, "recruitment_claim_credit"),
                callback_data=f"recruit_reward:{result['reseller_id']}:credit",
            ),
        )
        bot.send_message(referrer_id, message, reply_markup=markup)
    except Exception:
        # Qualification is safely persisted; the same claim is also exposed in
        # Invite & Earn, so Telegram delivery failure cannot lose the reward.
        pass
    return result
