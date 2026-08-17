"""Idempotent growth-event recording and privacy-safe funnel summaries."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import database
from .time_utils import format_utc_timestamp, parse_utc_timestamp


SURFACE_MAIN = "main"
SURFACE_HOSTED = "hosted"

EVENT_ONBOARDING_VIEWED = "onboarding_viewed"
EVENT_TRIAL_STARTED = "trial_started"
EVENT_TRIAL_ACTIVATED = "trial_activated"
EVENT_PLAN_VIEWED = "plan_viewed"
EVENT_PLAN_SELECTED = "plan_selected"
EVENT_CHECKOUT_STARTED = "checkout_started"
EVENT_CHECKOUT_COMPLETED = "checkout_completed"
EVENT_RENEWAL_PROMPTED = "renewal_prompted"
EVENT_RENEWAL_COMPLETED = "renewal_completed"
EVENT_REFERRAL_ATTRIBUTED = "referral_attributed"
EVENT_REFERRAL_CONVERTED = "referral_converted"
EVENT_RESELLER_APPLIED = "reseller_applied"
EVENT_RESELLER_APPROVED = "reseller_approved"
EVENT_HOSTED_READY = "hosted_ready"
EVENT_HOSTED_FIRST_SALE = "hosted_first_sale"

# Semantic alias for callers that describe fulfillment rather than checkout.
EVENT_PURCHASE_COMPLETED = EVENT_CHECKOUT_COMPLETED

EVENT_TYPES = (
    EVENT_ONBOARDING_VIEWED,
    EVENT_TRIAL_STARTED,
    EVENT_TRIAL_ACTIVATED,
    EVENT_PLAN_VIEWED,
    EVENT_PLAN_SELECTED,
    EVENT_CHECKOUT_STARTED,
    EVENT_CHECKOUT_COMPLETED,
    EVENT_RENEWAL_PROMPTED,
    EVENT_RENEWAL_COMPLETED,
    EVENT_REFERRAL_ATTRIBUTED,
    EVENT_REFERRAL_CONVERTED,
    EVENT_RESELLER_APPLIED,
    EVENT_RESELLER_APPROVED,
    EVENT_HOSTED_READY,
    EVENT_HOSTED_FIRST_SALE,
)

DEFAULT_FUNNELS = {
    "customer_journey": (
        EVENT_ONBOARDING_VIEWED,
        EVENT_TRIAL_ACTIVATED,
        EVENT_PLAN_SELECTED,
        EVENT_CHECKOUT_STARTED,
        EVENT_CHECKOUT_COMPLETED,
    ),
    "trial_to_paid": (EVENT_TRIAL_ACTIVATED, EVENT_CHECKOUT_COMPLETED),
    "checkout": (EVENT_CHECKOUT_STARTED, EVENT_CHECKOUT_COMPLETED),
    "renewal": (EVENT_RENEWAL_PROMPTED, EVENT_RENEWAL_COMPLETED),
    "referral": (EVENT_REFERRAL_ATTRIBUTED, EVENT_REFERRAL_CONVERTED),
    "reseller_application": (EVENT_RESELLER_APPLIED, EVENT_RESELLER_APPROVED),
    "reseller_activation": (
        EVENT_RESELLER_APPROVED,
        EVENT_HOSTED_READY,
        EVENT_HOSTED_FIRST_SALE,
    ),
}

_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class GrowthEvent:
    event_id: int
    event_type: str
    user_id: str | None
    surface: str
    hosted_tenant_id: str | None
    language: str | None
    plan_id: str | None
    payment_method: str | None
    referral_campaign: str | None
    occurred_at: str
    deduplication_key: str
    metadata: dict[str, Any]
    recorded_at: str


@dataclass(frozen=True)
class GrowthEventResult:
    event: GrowthEvent
    created: bool


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _required_text(value: Any, field: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _slug(value: Any, field: str) -> str:
    normalized = _required_text(value, field).lower()
    if not _SLUG.fullmatch(normalized):
        raise ValueError(
            f"{field} must start with a letter and contain only lowercase "
            "letters, numbers, or underscores."
        )
    return normalized


def _timestamp(value: datetime | date | str | None, *, required: bool) -> str | None:
    if value is None:
        if not required:
            return None
        return format_utc_timestamp()
    if not isinstance(value, (datetime, date, str)):
        raise TypeError(f"Unsupported timestamp value: {value!r}")
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        if not required and isinstance(value, str) and not value.strip():
            return None
        raise ValueError(f"Invalid ISO timestamp: {value!r}")
    return format_utc_timestamp(parsed)


def _metadata_json(metadata: Mapping[str, Any] | None) -> str:
    if metadata is None:
        payload = {}
    elif isinstance(metadata, Mapping):
        payload = dict(metadata)
    else:
        raise TypeError("metadata must be a mapping.")
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be JSON serializable and finite.") from error


def _from_row(row) -> GrowthEvent:
    return GrowthEvent(
        event_id=int(row["event_id"]),
        event_type=str(row["event_type"]),
        user_id=_optional_text(row["user_id"]),
        surface=str(row["surface"]),
        hosted_tenant_id=_optional_text(row["hosted_tenant_id"]),
        language=_optional_text(row["language"]),
        plan_id=_optional_text(row["plan_id"]),
        payment_method=_optional_text(row["payment_method"]),
        referral_campaign=_optional_text(row["referral_campaign"]),
        occurred_at=str(row["occurred_at"]),
        deduplication_key=str(row["deduplication_key"]),
        metadata=json.loads(row["metadata_json"]),
        recorded_at=str(row["recorded_at"]),
    )


def record_growth_event(
    event_type: str,
    *,
    deduplication_key: str,
    user_id: str | int | None = None,
    surface: str = SURFACE_MAIN,
    hosted_tenant_id: str | int | None = None,
    language: str | None = None,
    plan_id: str | int | None = None,
    payment_method: str | None = None,
    referral_campaign: str | None = None,
    occurred_at: datetime | date | str | None = None,
    metadata: Mapping[str, Any] | None = None,
    path: str | None = None,
) -> GrowthEventResult:
    """Persist one immutable event, returning the prior row on a retry.

    The idempotency identity is ``event_type + surface + tenant + key``. A
    repeated call never overwrites the original attribution or timestamp.
    """

    normalized_type = _slug(event_type, "event_type")
    normalized_surface = _slug(surface, "surface")
    tenant = _optional_text(hosted_tenant_id) or ""
    key = _required_text(deduplication_key, "deduplication_key")
    values = (
        normalized_type,
        _optional_text(user_id),
        normalized_surface,
        tenant,
        _optional_text(language),
        _optional_text(plan_id),
        _optional_text(payment_method),
        _optional_text(referral_campaign),
        _timestamp(occurred_at, required=True),
        key,
        _metadata_json(metadata),
        format_utc_timestamp(),
    )

    with database.write_transaction(path, operation="record_growth_event") as connection:
        cursor = connection.execute(
            """
            INSERT INTO growth_events(
                event_type, user_id, surface, hosted_tenant_id, language,
                plan_id, payment_method, referral_campaign, occurred_at,
                deduplication_key, metadata_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_type, surface, hosted_tenant_id, deduplication_key)
            DO NOTHING
            """,
            values,
        )
        row = connection.execute(
            """
            SELECT * FROM growth_events
            WHERE event_type=? AND surface=? AND hosted_tenant_id=?
              AND deduplication_key=?
            """,
            (normalized_type, normalized_surface, tenant, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("Growth event insert did not produce a stored row.")
        return GrowthEventResult(event=_from_row(row), created=cursor.rowcount == 1)


def get_growth_event(
    event_type: str,
    *,
    deduplication_key: str,
    surface: str = SURFACE_MAIN,
    hosted_tenant_id: str | int | None = None,
    path: str | None = None,
) -> GrowthEvent | None:
    """Look up one event by its idempotency identity."""

    tenant = _optional_text(hosted_tenant_id) or ""
    with database.read_transaction(path, operation="get_growth_event") as connection:
        row = connection.execute(
            """
            SELECT * FROM growth_events
            WHERE event_type=? AND surface=? AND hosted_tenant_id=?
              AND deduplication_key=?
            """,
            (
                _slug(event_type, "event_type"),
                _slug(surface, "surface"),
                tenant,
                _required_text(deduplication_key, "deduplication_key"),
            ),
        ).fetchone()
    return _from_row(row) if row is not None else None


def _summary_filters(
    *,
    start_at: datetime | date | str | None,
    end_at: datetime | date | str | None,
    surface: str | None,
    hosted_tenant_id: str | int | None,
) -> tuple[list[str], list[str], str | None, str | None, str | None, str | None]:
    start = _timestamp(start_at, required=False)
    end = _timestamp(end_at, required=False)
    if start is not None and end is not None and start >= end:
        raise ValueError("start_at must be earlier than end_at.")
    normalized_surface = _slug(surface, "surface") if surface is not None else None
    tenant = _optional_text(hosted_tenant_id)
    clauses: list[str] = []
    parameters: list[str] = []
    if start is not None:
        clauses.append("occurred_at >= ?")
        parameters.append(start)
    if end is not None:
        clauses.append("occurred_at < ?")
        parameters.append(end)
    if normalized_surface is not None:
        clauses.append("surface = ?")
        parameters.append(normalized_surface)
    if tenant is not None:
        clauses.append("hosted_tenant_id = ?")
        parameters.append(tenant)
    return clauses, parameters, start, end, normalized_surface, tenant


def _normalize_funnels(
    funnels: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    source = DEFAULT_FUNNELS if funnels is None else funnels
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_stages in source.items():
        name = _slug(raw_name, "funnel name")
        if isinstance(raw_stages, (str, bytes)):
            raise TypeError(f"Funnel {name!r} stages must be a sequence.")
        stages = tuple(_slug(stage, "event_type") for stage in raw_stages)
        if len(stages) < 2:
            raise ValueError(f"Funnel {name!r} must contain at least two stages.")
        normalized[name] = stages
    return normalized


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def growth_funnel_summary(
    *,
    start_at: datetime | date | str | None = None,
    end_at: datetime | date | str | None = None,
    surface: str | None = None,
    hosted_tenant_id: str | int | None = None,
    funnels: Mapping[str, Sequence[str]] | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Return aggregate counts and ordered, distinct-user funnels.

    The period is half-open: ``start_at`` is included and ``end_at`` is not.
    Funnel users must encounter stages in order inside that period. The return
    value contains counts only; customer identifiers are never exposed.
    """

    normalized_funnels = _normalize_funnels(funnels)
    clauses, parameters, start, end, normalized_surface, tenant = _summary_filters(
        start_at=start_at,
        end_at=end_at,
        surface=surface,
        hosted_tenant_id=hosted_tenant_id,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    selected_types = sorted(
        {stage for stages in normalized_funnels.values() for stage in stages}
    )

    with database.read_transaction(path, operation="growth_funnel_summary") as connection:
        count_rows = connection.execute(
            f"""
            SELECT event_type, COUNT(*) AS event_count,
                   COUNT(DISTINCT CASE
                       WHEN user_id IS NOT NULL AND user_id != '' THEN user_id
                   END) AS unique_users
            FROM growth_events
            {where}
            GROUP BY event_type
            """,
            parameters,
        ).fetchall()
        total_row = connection.execute(
            f"""
            SELECT COUNT(*) AS event_count,
                   COUNT(DISTINCT CASE
                       WHEN user_id IS NOT NULL AND user_id != '' THEN user_id
                   END) AS unique_users
            FROM growth_events
            {where}
            """,
            parameters,
        ).fetchone()

        sequence_rows = []
        if selected_types:
            type_placeholders = ",".join("?" for _ in selected_types)
            sequence_clauses = list(clauses)
            sequence_clauses.extend(
                [
                    "user_id IS NOT NULL",
                    "user_id != ''",
                    f"event_type IN ({type_placeholders})",
                ]
            )
            sequence_where = f"WHERE {' AND '.join(sequence_clauses)}"
            sequence_rows = connection.execute(
                f"""
                SELECT event_id, event_type, user_id, occurred_at
                FROM growth_events
                {sequence_where}
                ORDER BY user_id, occurred_at, event_id
                """,
                [*parameters, *selected_types],
            ).fetchall()

    raw_counts = {
        str(row["event_type"]): {
            "events": int(row["event_count"]),
            "unique_users": int(row["unique_users"]),
        }
        for row in count_rows
    }
    event_types = [*EVENT_TYPES]
    event_types.extend(sorted(set(raw_counts) - set(event_types)))
    event_counts = {
        event_type: raw_counts.get(event_type, {"events": 0, "unique_users": 0})
        for event_type in event_types
    }

    events_by_user: dict[str, list[str]] = {}
    for row in sequence_rows:
        events_by_user.setdefault(str(row["user_id"]), []).append(str(row["event_type"]))

    funnel_summaries: dict[str, Any] = {}
    for name, stages in normalized_funnels.items():
        reached = [0] * len(stages)
        for user_events in events_by_user.values():
            next_stage = 0
            for event_type in user_events:
                if event_type != stages[next_stage]:
                    continue
                reached[next_stage] += 1
                next_stage += 1
                if next_stage == len(stages):
                    break
        first_count = reached[0]
        stage_summaries = []
        for index, (event_type, users) in enumerate(zip(stages, reached)):
            previous = reached[index - 1] if index else 0
            raw = event_counts[event_type]
            stage_summaries.append(
                {
                    "event_type": event_type,
                    "events": raw["events"],
                    "unique_users": raw["unique_users"],
                    "funnel_users": users,
                    "conversion_from_previous_percent": (
                        None if index == 0 else _percent(users, previous)
                    ),
                    "conversion_from_first_percent": _percent(users, first_count),
                }
            )
        funnel_summaries[name] = {
            "started_users": first_count,
            "completed_users": reached[-1],
            "conversion_percent": _percent(reached[-1], first_count),
            "stages": stage_summaries,
        }

    return {
        "start_at": start,
        "end_at": end,
        "surface": normalized_surface,
        "hosted_tenant_id": tenant,
        "total_events": int(total_row["event_count"]),
        "total_unique_users": int(total_row["unique_users"]),
        "event_counts": event_counts,
        "funnels": funnel_summaries,
    }


def main_admin_funnel_summary(
    *,
    start_at: datetime | date | str | None = None,
    end_at: datetime | date | str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Aggregate the main storefront for an administrator-facing summary."""

    return growth_funnel_summary(
        start_at=start_at,
        end_at=end_at,
        surface=SURFACE_MAIN,
        path=path,
    )


def hosted_owner_funnel_summary(
    hosted_tenant_id: str | int,
    *,
    start_at: datetime | date | str | None = None,
    end_at: datetime | date | str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Aggregate exactly one hosted tenant for its authenticated owner."""

    tenant = _required_text(hosted_tenant_id, "hosted_tenant_id")
    return growth_funnel_summary(
        start_at=start_at,
        end_at=end_at,
        surface=SURFACE_HOSTED,
        hosted_tenant_id=tenant,
        path=path,
    )
