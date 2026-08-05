"""Private, period-over-period growth funnel reports for operators and owners."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from utils.growth_events import (
    hosted_owner_funnel_summary,
    main_admin_funnel_summary,
)


FUNNEL_LABELS = {
    "trial_to_paid": "Trial → paid",
    "checkout": "Checkout completion",
    "renewal": "Renewal",
    "referral": "Referred first purchase",
    "reseller_application": "Reseller approval",
    "reseller_activation": "Approved reseller → first sale",
    "customer_journey": "Customer journey",
}


def _utc(value=None):
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _periods(end_at=None, days=30):
    end = _utc(end_at)
    current_start = end - timedelta(days=days)
    baseline_start = current_start - timedelta(days=days)
    return baseline_start, current_start, end


def _relative_change(current, baseline):
    if current is None or baseline is None or baseline == 0:
        return None
    return round(((current - baseline) / baseline) * 100, 2)


def _compare(current, baseline):
    names = list(FUNNEL_LABELS)
    result = {}
    for name in names:
        current_funnel = current.get("funnels", {}).get(name, {})
        baseline_funnel = baseline.get("funnels", {}).get(name, {})
        current_rate = current_funnel.get("conversion_percent")
        baseline_rate = baseline_funnel.get("conversion_percent")
        result[name] = {
            "started": int(current_funnel.get("started_users", 0) or 0),
            "completed": int(current_funnel.get("completed_users", 0) or 0),
            "conversion_percent": current_rate,
            "baseline_started": int(baseline_funnel.get("started_users", 0) or 0),
            "baseline_completed": int(baseline_funnel.get("completed_users", 0) or 0),
            "baseline_conversion_percent": baseline_rate,
            "relative_change_percent": _relative_change(current_rate, baseline_rate),
        }
    return result


def main_growth_comparison(*, end_at=None, days=30, path=None):
    baseline_start, current_start, end = _periods(end_at, days)
    baseline = main_admin_funnel_summary(
        start_at=baseline_start,
        end_at=current_start,
        path=path,
    )
    current = main_admin_funnel_summary(
        start_at=current_start,
        end_at=end,
        path=path,
    )
    return {
        "surface": "main",
        "days": days,
        "baseline_start": baseline_start,
        "current_start": current_start,
        "end_at": end,
        "current_total_events": current["total_events"],
        "baseline_total_events": baseline["total_events"],
        "funnels": _compare(current, baseline),
    }


def hosted_growth_comparison(hosted_tenant_id, *, end_at=None, days=30, path=None):
    baseline_start, current_start, end = _periods(end_at, days)
    baseline = hosted_owner_funnel_summary(
        hosted_tenant_id,
        start_at=baseline_start,
        end_at=current_start,
        path=path,
    )
    current = hosted_owner_funnel_summary(
        hosted_tenant_id,
        start_at=current_start,
        end_at=end,
        path=path,
    )
    return {
        "surface": "hosted",
        "hosted_tenant_id": str(hosted_tenant_id),
        "days": days,
        "baseline_start": baseline_start,
        "current_start": current_start,
        "end_at": end,
        "current_total_events": current["total_events"],
        "baseline_total_events": baseline["total_events"],
        "funnels": _compare(current, baseline),
    }


def _rate(value):
    return "n/a" if value is None else f"{float(value):.1f}%"


def format_growth_comparison(report, *, funnel_names=None, title="Growth funnel"):
    names = tuple(funnel_names or FUNNEL_LABELS)
    current_start = report["current_start"].date().isoformat()
    end_date = report["end_at"].date().isoformat()
    lines = [
        f"📈 *{title}*",
        f"Current {report['days']} days: `{current_start}` to `{end_date}`",
        "Previous equal period is the available baseline.",
        "",
    ]
    for name in names:
        item = report.get("funnels", {}).get(name)
        if not item:
            continue
        delta = item.get("relative_change_percent")
        delta_text = "n/a" if delta is None else f"{delta:+.1f}% relative"
        lines.extend([
            f"*{FUNNEL_LABELS.get(name, name.replace('_', ' ').title())}*",
            (
                f"{item['completed']}/{item['started']} · "
                f"{_rate(item['conversion_percent'])} "
                f"(baseline {_rate(item['baseline_conversion_percent'])}; {delta_text})"
            ),
        ])
    lines.extend([
        "",
        "Only aggregate counts are shown; customer identities are not included.",
    ])
    return "\n".join(lines)
