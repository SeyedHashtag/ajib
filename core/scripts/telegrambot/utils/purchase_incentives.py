"""Main-store checkout incentives with idempotent reservation and completion."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from utils.account_credit import (
    consume_account_credit,
    release_account_credit,
    reserve_account_credit,
)
from utils.referral import (
    add_referral_reward,
    combined_discount_cap_percent,
    get_referral_attribution,
    redeem_invitee_discount,
    release_invitee_discount,
    reserve_invitee_discount,
    stacked_discount_percent,
)


MONEY = Decimal("0.01")


def _money(value):
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid checkout amount") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid checkout amount")
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


def _percent(value):
    try:
        result = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid checkout discount") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Invalid checkout discount")
    return result


def reserve_main_checkout(
    user_id,
    reservation_id,
    original_price,
    *,
    payment_method,
    payment_discount_percent=0,
    payments=None,
    allow_invite_discount=True,
    allow_account_credit=True,
    path=None,
):
    """Reserve one quote and return the immutable fields stored on its payment.

    Discounts are calculated against the original retail amount, then purchase
    credit is applied to the discounted total. ``collected_amount`` therefore
    means the external amount used as the referral-reward base.
    """
    reservation_key = str(reservation_id or "").strip()
    if not reservation_key:
        raise ValueError("A checkout reservation ID is required")
    original = _money(original_price)
    requested_method_discount = _percent(payment_discount_percent)
    invite_reservation = None
    if allow_invite_discount:
        invite_reservation = reserve_invitee_discount(
            user_id,
            reservation_key,
            payments=payments,
        )
    requested_invite_percent = _percent(
        (invite_reservation or {}).get("percent", 0)
    )
    total_percent = _percent(
        stacked_discount_percent(requested_invite_percent, requested_method_discount)
    )
    # Persist the components actually applied after the cap, not their nominal
    # inputs. Invite value is allocated first and the payment-method incentive
    # uses only the remaining headroom.
    invite_percent = min(requested_invite_percent, total_percent)
    method_discount = max(Decimal("0"), total_percent - invite_percent)
    discount_amount = (
        original * total_percent / Decimal("100")
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    invite_discount_amount = (
        original * invite_percent / Decimal("100")
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    payment_discount_amount = max(
        Decimal("0"),
        discount_amount - invite_discount_amount,
    ).quantize(MONEY, rounding=ROUND_HALF_UP)
    discounted_total = max(Decimal("0"), original - discount_amount)

    credit_reserved = Decimal("0")
    try:
        if allow_account_credit and discounted_total > 0:
            credit_reserved = _money(
                reserve_account_credit(
                    user_id,
                    reservation_key,
                    discounted_total,
                    order_id=reservation_key,
                    metadata={
                        "kind": "main_checkout",
                        "payment_method": str(payment_method),
                    },
                    path=path,
                )
            )
    except Exception:
        if invite_reservation:
            release_invitee_discount(reservation_key, user_id=user_id)
        raise

    collected = max(Decimal("0"), discounted_total - credit_reserved).quantize(
        MONEY,
        rounding=ROUND_HALF_UP,
    )
    attribution = get_referral_attribution(user_id)
    return {
        "original_price": float(original),
        "invite_discount_percent": float(invite_percent),
        "invite_discount_amount": float(invite_discount_amount),
        "payment_discount_percent": float(method_discount),
        "payment_discount_amount": float(payment_discount_amount),
        "crypto_discount_percent": (
            float(method_discount)
            if str(payment_method).strip().lower() == "crypto"
            else 0.0
        ),
        "crypto_discount_amount": (
            float(payment_discount_amount)
            if str(payment_method).strip().lower() == "crypto"
            else 0.0
        ),
        "discount_percent": float(total_percent),
        "total_discount_percent": float(total_percent),
        "discount_cap_percent": float(combined_discount_cap_percent()),
        "discount_amount": float(discount_amount),
        "discounted_total": float(discounted_total),
        "account_credit_reserved": float(credit_reserved),
        "account_credit_reservation_id": reservation_key,
        "collected_amount": float(collected),
        "referral_reward_base": float(collected),
        "price": float(collected),
        "referrer_id": (attribution or {}).get("referrer_user_id"),
        "referral_code": (attribution or {}).get("referral_code"),
        "referral_campaign": (
            (attribution or {}).get("campaign_type") if attribution else None
        ),
    }


def release_main_checkout(user_id, reservation_id, *, path=None):
    """Release all unconsumed checkout benefits; safe to call repeatedly."""
    reservation_key = str(reservation_id or "").strip()
    if not reservation_key:
        return {"invite_released": False, "credit_released": False}
    return {
        "invite_released": release_invitee_discount(
            reservation_key,
            user_id=user_id,
        ),
        "credit_released": release_account_credit(
            user_id,
            reservation_key,
            path=path,
        ),
    }


def finalize_main_checkout(
    payment_id,
    payment_record,
    *,
    reward_referrer=True,
    path=None,
):
    """Consume reserved benefits and award the post-discount referral amount."""
    record = dict(payment_record or {})
    user_id = record.get("user_id")
    if user_id is None:
        raise ValueError("Completed checkout has no user ID")
    payment_key = str(payment_id or record.get("payment_id") or "").strip()
    if not payment_key:
        raise ValueError("Completed checkout has no payment ID")
    reservation_id = str(
        record.get("account_credit_reservation_id")
        or record.get("incentive_reservation_id")
        or payment_key
    )
    credit_consumed = consume_account_credit(
        user_id,
        reservation_id,
        order_id=payment_key,
        metadata={"payment_id": payment_key},
        path=path,
    )
    invite_redeemed = False
    if float(record.get("invite_discount_percent", 0) or 0) > 0:
        invite_redeemed = redeem_invitee_discount(user_id, reservation_id)

    reward_result = False
    reward_base = float(
        record.get(
            "referral_reward_base",
            record.get("collected_amount", record.get("price", 0)),
        )
        or 0
    )
    if reward_referrer:
        reward_result = add_referral_reward(user_id, max(0.0, reward_base), payment_key)

    growth_recorded = False
    try:
        from utils.growth_events import (
            EVENT_CHECKOUT_COMPLETED,
            EVENT_REFERRAL_CONVERTED,
            record_growth_event,
        )

        common = {
            "user_id": user_id,
            "language": record.get("language"),
            "plan_id": record.get("plan_gb"),
            "payment_method": record.get("payment_method"),
            "referral_campaign": record.get("referral_campaign"),
        }
        growth_recorded = record_growth_event(
            EVENT_CHECKOUT_COMPLETED,
            deduplication_key=f"payment:{payment_key}",
            metadata={"renewal": record.get("type") == "renewal"},
            **common,
        ).created
        if record.get("referrer_id") or get_referral_attribution(user_id):
            record_growth_event(
                EVENT_REFERRAL_CONVERTED,
                deduplication_key=f"referral-payment:{payment_key}",
                **common,
            )
    except Exception:
        # Purchase completion must never depend on analytics availability.
        growth_recorded = False

    referrer_id = None
    reward_amount = 0.0
    if isinstance(reward_result, tuple) and len(reward_result) >= 3:
        referrer_id = reward_result[1]
        reward_amount = float(reward_result[2] or 0)
    return {
        "credit_consumed": credit_consumed,
        "invite_redeemed": invite_redeemed,
        "reward_created": bool(reward_result),
        "referrer_id": referrer_id,
        "reward_amount": reward_amount,
        "reward_base": reward_base,
        "growth_recorded": growth_recorded,
    }
