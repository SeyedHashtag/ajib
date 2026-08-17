"""Stable lifecycle timestamps for payment reporting."""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from .time_utils import parse_utc_timestamp
except ImportError:  # Standalone diagnostics/tests.
    from time_utils import parse_utc_timestamp


PAID_STATUSES = frozenset({"completed", "paid", "success", "succeeded"})
FAILED_STATUSES = frozenset({"rejected", "failed", "canceled", "cancelled", "error"})
EXPIRED_STATUSES = frozenset({"expired"})
OPEN_STATUSES = frozenset({
    "creating",
    "waiting_receipt",
    "pending_approval",
    "pending",
    "processing",
    "waiting",
    "unpaid",
})


def parse_payment_timestamp(value) -> datetime | None:
    """Return an aware UTC datetime for a persisted payment timestamp."""
    return parse_utc_timestamp(value, legacy_naive_timezone=timezone.utc)


def _event_timestamps(record: dict, statuses) -> list[datetime]:
    timestamps = []
    updates = record.get("updates", [])
    if not isinstance(updates, list):
        return timestamps
    for event in updates:
        if not isinstance(event, dict):
            continue
        event_status = str(event.get("status") or "").strip().lower()
        if event_status not in statuses:
            continue
        parsed = parse_payment_timestamp(event.get("timestamp") or event.get("occurred_at"))
        if parsed is not None:
            timestamps.append(parsed)
    return timestamps


def _first_valid_timestamp(*values) -> datetime | None:
    for value in values:
        parsed = parse_payment_timestamp(value)
        if parsed is not None:
            return parsed
    return None


def payment_lifecycle_timestamp(record: dict) -> datetime | None:
    """Resolve the immutable lifecycle event used to date a payment report row."""
    if not isinstance(record, dict):
        return None

    status = str(record.get("status") or "").strip().lower()
    if status in PAID_STATUSES:
        completed_at = parse_payment_timestamp(record.get("completed_at"))
        if completed_at is not None:
            return completed_at
        paid_events = _event_timestamps(record, PAID_STATUSES)
        if paid_events:
            return min(paid_events)
        return _first_valid_timestamp(record.get("updated_at"), record.get("created_at"))

    if status in FAILED_STATUSES:
        failed_events = _event_timestamps(record, FAILED_STATUSES)
        if failed_events:
            return max(failed_events)
        return _first_valid_timestamp(record.get("updated_at"), record.get("created_at"))

    if status in EXPIRED_STATUSES:
        expired_events = _event_timestamps(record, EXPIRED_STATUSES)
        if expired_events:
            return max(expired_events)
        return _first_valid_timestamp(record.get("updated_at"), record.get("created_at"))

    if status in OPEN_STATUSES:
        return _first_valid_timestamp(record.get("created_at"), record.get("updated_at"))

    return _first_valid_timestamp(record.get("updated_at"), record.get("created_at"))
