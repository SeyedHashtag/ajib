"""Sales statistics for reseller-owned hosted storefronts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    from utils.payment_lifecycle import parse_payment_timestamp, payment_lifecycle_timestamp
    from utils.time_utils import utc_now
except ImportError:  # Support direct module loading in maintenance tools and tests.
    from payment_lifecycle import parse_payment_timestamp, payment_lifecycle_timestamp
    from time_utils import utc_now


OPEN_STATUSES = {
    "creating",
    "waiting_receipt",
    "pending_approval",
    "pending",
    "processing",
}
ATTENTION_STATUSES = {"paid_provision_failed"}
FAILED_STATUSES = {"failed", "canceled", "cancelled", "rejected", "error"}
EXPIRED_STATUSES = {"expired"}
COMPLETED_STATUSES = {"completed"}
PAYMENT_METHODS = ("card", "crypto", "other")


def _coerce_date(value):
    parsed = parse_payment_timestamp(value)
    if parsed is None:
        raise ValueError("A valid report end date is required")
    return parsed.date()


def _money(value):
    try:
        amount = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not amount.is_finite():
        return Decimal("0")
    return amount


def _empty_bucket(bucket_date=None):
    return {
        "date": bucket_date.isoformat() if bucket_date else None,
        "started": 0,
        "completed": 0,
        "open": 0,
        "attention": 0,
        "failed": 0,
        "expired": 0,
        "unique_buyers": 0,
        "new_configs": 0,
        "renewals": 0,
        "manual_configs": 0,
        "revenue": Decimal("0"),
        "gross_profit": Decimal("0"),
        "referral_payouts": Decimal("0"),
        "net_profit": Decimal("0"),
        "methods": {
            method: {"completed": 0, "revenue": Decimal("0")}
            for method in PAYMENT_METHODS
        },
        "_buyers": set(),
    }


def _bucket_for(buckets, timestamp):
    parsed = parse_payment_timestamp(timestamp)
    return buckets.get(parsed.date()) if parsed is not None else None


def _payment_financials(payment):
    method = str(payment.get("payment_method") or "other").strip().lower()
    if method not in {"card", "crypto"}:
        method = "other"
    if method == "crypto":
        revenue = _money(
            payment.get("crypto_collected", payment.get("retail_price", payment.get("price", 0)))
        )
    else:
        revenue = _money(payment.get("retail_price", payment.get("price", 0)))
    gross_profit = revenue - _money(payment.get("wholesale_price", 0))
    referral_payout = _money(payment.get("referral_reward", 0))
    return method, revenue, gross_profit, referral_payout, gross_profit - referral_payout


def _record_completed(bucket, payment):
    if bucket is None:
        return
    method, revenue, gross_profit, referral_payout, net_profit = _payment_financials(payment)
    bucket["completed"] += 1
    buyer_id = str(payment.get("user_id") or "").strip()
    if buyer_id:
        bucket["_buyers"].add(buyer_id)
    if payment.get("renew_username"):
        bucket["renewals"] += 1
    else:
        bucket["new_configs"] += 1
    bucket["revenue"] += revenue
    bucket["gross_profit"] += gross_profit
    bucket["referral_payouts"] += referral_payout
    bucket["net_profit"] += net_profit
    bucket["methods"][method]["completed"] += 1
    bucket["methods"][method]["revenue"] += revenue


def _is_manual_config(config, origin_bot_id):
    order_id = str(config.get("retail_order_id") or "")
    if not order_id.startswith("manual-"):
        return False
    if not origin_bot_id:
        return True
    return str(config.get("origin_bot_id") or "") == str(origin_bot_id)


def _finalize_bucket(bucket):
    result = dict(bucket)
    buyers = result.pop("_buyers", set())
    result["unique_buyers"] = len(buyers)
    for key in ("revenue", "gross_profit", "referral_payouts", "net_profit"):
        result[key] = float(result[key].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    methods = {}
    for method, values in result["methods"].items():
        methods[method] = {
            "completed": values["completed"],
            "revenue": float(values["revenue"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }
    result["methods"] = methods
    return result


def _rollup(buckets):
    result = _empty_bucket()
    for bucket in buckets:
        for key in (
            "started",
            "completed",
            "open",
            "attention",
            "failed",
            "expired",
            "new_configs",
            "renewals",
            "manual_configs",
        ):
            result[key] += bucket[key]
        result["_buyers"].update(bucket["_buyers"])
        for key in ("revenue", "gross_profit", "referral_payouts", "net_profit"):
            result[key] += bucket[key]
        for method in PAYMENT_METHODS:
            result["methods"][method]["completed"] += bucket["methods"][method]["completed"]
            result["methods"][method]["revenue"] += bucket["methods"][method]["revenue"]
    return _finalize_bucket(result)


def build_hosted_stats(payments, reseller_configs, end_date=None, origin_bot_id=None):
    """Build seven daily buckets and a trailing 30-calendar-day storefront summary."""
    report_end = _coerce_date(end_date or utc_now())
    last30_start = report_end - timedelta(days=29)
    seven_day_start = report_end - timedelta(days=6)
    buckets = {
        last30_start + timedelta(days=offset): _empty_bucket(last30_start + timedelta(days=offset))
        for offset in range(30)
    }

    for payment in (payments or {}).values():
        if not isinstance(payment, dict):
            continue
        status = str(payment.get("status") or "").strip().lower()
        started_bucket = _bucket_for(buckets, payment.get("created_at"))
        if started_bucket is not None:
            started_bucket["started"] += 1

        lifecycle_timestamp = payment_lifecycle_timestamp(payment)
        if status in COMPLETED_STATUSES:
            _record_completed(_bucket_for(buckets, lifecycle_timestamp), payment)
        elif status in OPEN_STATUSES:
            open_bucket = _bucket_for(buckets, lifecycle_timestamp)
            if open_bucket is not None:
                open_bucket["open"] += 1
        elif status in ATTENTION_STATUSES:
            attention_bucket = _bucket_for(buckets, lifecycle_timestamp)
            if attention_bucket is not None:
                attention_bucket["attention"] += 1
        elif status in FAILED_STATUSES:
            failed_bucket = _bucket_for(buckets, lifecycle_timestamp)
            if failed_bucket is not None:
                failed_bucket["failed"] += 1
        elif status in EXPIRED_STATUSES:
            expired_bucket = _bucket_for(buckets, lifecycle_timestamp)
            if expired_bucket is not None:
                expired_bucket["expired"] += 1

    for config in reseller_configs or ():
        if not isinstance(config, dict) or not _is_manual_config(config, origin_bot_id):
            continue
        bucket = _bucket_for(buckets, config.get("timestamp"))
        if bucket is not None:
            bucket["manual_configs"] += 1

    ordered = [buckets[last30_start + timedelta(days=offset)] for offset in range(30)]
    daily = [
        _finalize_bucket(buckets[seven_day_start + timedelta(days=offset)])
        for offset in range(7)
    ]
    return {
        "start_date": seven_day_start.isoformat(),
        "end_date": report_end.isoformat(),
        "last30_start_date": last30_start.isoformat(),
        "last30_end_date": report_end.isoformat(),
        "days": daily,
        "last30": _rollup(ordered),
    }
