"""Canonical UTC timestamp helpers used by persistent bot state.

Persisted instants use RFC 3339 UTC strings with an explicit ``Z`` suffix.
Legacy offset-free values are accepted only when the caller supplies (or
intentionally accepts) the timezone in which those values were written.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
CANONICAL_TIMESPEC = "microseconds"
LEGACY_DEFAULT_TIMEZONE = "Asia/Tehran"


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def legacy_timezone(name: str | None = None) -> tzinfo:
    """Return the timezone used only to interpret legacy local timestamps."""
    timezone_name = (
        name
        or os.getenv("AJIB_LEGACY_TIMEZONE")
        or os.getenv("AJIB_TIMEZONE")
        or LEGACY_DEFAULT_TIMEZONE
    )
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(LEGACY_DEFAULT_TIMEZONE)


def _coerce_timezone(value: str | tzinfo | None) -> tzinfo:
    if value is None:
        return UTC
    if isinstance(value, str):
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return UTC
    return value


def parse_utc_timestamp(
    value: Any,
    *,
    legacy_naive_timezone: str | tzinfo | None = UTC,
) -> datetime | None:
    """Parse a timestamp and return an aware UTC datetime.

    Explicit offsets and ``Z`` are honored. Offset-free legacy values are
    interpreted in ``legacy_naive_timezone``; application state should use
    UTC while known historical local fields can opt into ``legacy_timezone``.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif value is None:
        return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith(("Z", "z")):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_coerce_timezone(legacy_naive_timezone))
    return parsed.astimezone(UTC)


def format_utc_timestamp(value: Any = None, *, timespec: str = CANONICAL_TIMESPEC) -> str:
    """Format an instant as canonical RFC 3339 UTC with a ``Z`` suffix."""
    parsed = parse_utc_timestamp(utc_now() if value is None else value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def utc_date(value: Any = None) -> date:
    """Return the UTC calendar date for an instant."""
    parsed = parse_utc_timestamp(utc_now() if value is None else value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return parsed.date()


def format_utc_display(value: Any = None, *, include_microseconds: bool = False) -> str:
    """Format an instant for user-facing output with an explicit UTC label."""
    parsed = parse_utc_timestamp(utc_now() if value is None else value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    pattern = "%Y-%m-%d %H:%M:%S.%f UTC" if include_microseconds else "%Y-%m-%d %H:%M:%S UTC"
    return parsed.strftime(pattern)


def format_utc_filename(value: Any = None) -> str:
    """Return a filesystem-safe UTC timestamp token."""
    parsed = parse_utc_timestamp(utc_now() if value is None else value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    return parsed.strftime("%Y%m%dT%H%M%SZ")
