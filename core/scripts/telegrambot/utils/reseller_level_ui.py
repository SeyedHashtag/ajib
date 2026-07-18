"""Formatting and reliable delivery for reseller level presentations."""

from utils.currency_format import format_usd_amount
from utils.reseller import (
    RESELLER_LEVEL_COUNT,
    RESELLER_TRUST_PAID_STEP,
    claim_reseller_level_presentation,
    complete_reseller_level_presentation,
    get_reseller_level_summary,
    release_reseller_level_presentation,
)
from utils.translations import get_message_text


def _progress_bar(summary):
    filled = min(10, max(0, int(summary.get("progress_segments", 0))))
    return ("█" * filled) + ("░" * (10 - filled))


def build_reseller_level_compact(language, reseller_data):
    summary = get_reseller_level_summary(reseller_data)
    key = (
        "reseller_level_compact_max"
        if summary["is_max_level"]
        else "reseller_level_compact"
    )
    return get_message_text(language, key).format(
        icon=summary["icon"],
        level=summary["level"],
        level_count=summary["level_count"],
        discount_percent=summary["discount_percent"],
        next_level=summary["next_level"],
        amount_to_next=format_usd_amount(summary["amount_to_next"]),
    )


def build_reseller_level_profile(
    language,
    reseller_data,
    user_id,
    joined_date,
    total_configs,
    total_value,
    current_debt,
):
    summary = get_reseller_level_summary(reseller_data)
    if summary["is_max_level"]:
        progress_line = get_message_text(
            language,
            "reseller_level_progress_max",
        ).format(progress_bar=_progress_bar(summary))
    else:
        progress_line = get_message_text(
            language,
            "reseller_level_progress_next",
        ).format(
            progress_bar=_progress_bar(summary),
            progress_amount=format_usd_amount(summary["progress_amount"]),
            level_step=format_usd_amount(RESELLER_TRUST_PAID_STEP),
            amount_to_next=format_usd_amount(summary["amount_to_next"]),
            next_level=summary["next_level"],
        )
    return get_message_text(language, "reseller_level_profile").format(
        icon=summary["icon"],
        level=summary["level"],
        level_count=summary["level_count"],
        discount_percent=summary["discount_percent"],
        trust_limit=format_usd_amount(summary["trust_limit"]),
        progress_line=progress_line,
        user_id=user_id,
        joined_date=joined_date,
        total_configs=total_configs,
        total_value=format_usd_amount(total_value),
        total_paid=format_usd_amount(summary["total_paid"]),
        current_debt=format_usd_amount(current_debt),
    )


def build_reseller_level_roadmap(language, reseller_data):
    current = get_reseller_level_summary(reseller_data)["level"]
    lines = [get_message_text(language, "reseller_level_roadmap_title")]
    for level in range(1, RESELLER_LEVEL_COUNT + 1):
        threshold = (level - 1) * RESELLER_TRUST_PAID_STEP
        level_summary = get_reseller_level_summary({"total_paid": threshold})
        lines.append(
            get_message_text(language, "reseller_level_roadmap_row").format(
                marker="➤" if level == current else "•",
                icon=level_summary["icon"],
                level=level,
                discount_percent=level_summary["discount_percent"],
                trust_limit=format_usd_amount(level_summary["trust_limit"]),
                threshold=format_usd_amount(threshold),
            )
        )
    return "\n".join(lines)


def build_reseller_level_presentation(language, claim):
    summary = claim["summary"]
    key = (
        "reseller_level_introduction"
        if claim.get("kind") == "introduction"
        else "reseller_level_up"
    )
    message = get_message_text(language, key).format(
        icon=summary["icon"],
        from_level=claim.get("from_level", 0),
        level=summary["level"],
        level_count=summary["level_count"],
        discount_percent=summary["discount_percent"],
        trust_limit=format_usd_amount(summary["trust_limit"]),
    )
    reward_key = (
        "reseller_level_reward_max"
        if summary["is_max_level"]
        else "reseller_level_next_reward"
    )
    next_summary = (
        summary
        if summary["is_max_level"]
        else get_reseller_level_summary({
            "total_paid": summary["next_threshold"],
        })
    )
    return message + "\n\n" + get_message_text(language, reward_key).format(
        next_level=summary["next_level"],
        next_threshold=format_usd_amount(summary["next_threshold"] or 0),
        next_discount_percent=next_summary["discount_percent"],
        next_trust_limit=format_usd_amount(next_summary["trust_limit"]),
    )


def present_pending_reseller_level(
    bot,
    reseller_id,
    language,
    allow_introduction=True,
):
    claim = claim_reseller_level_presentation(reseller_id)
    if not claim:
        return False
    if claim.get("kind") == "introduction" and not allow_introduction:
        release_reseller_level_presentation(reseller_id, claim["id"])
        return False
    try:
        bot.send_message(
            reseller_id,
            build_reseller_level_presentation(language, claim),
            parse_mode="Markdown",
        )
    except Exception:
        release_reseller_level_presentation(reseller_id, claim["id"])
        return False
    complete_reseller_level_presentation(reseller_id, claim["id"])
    return True
