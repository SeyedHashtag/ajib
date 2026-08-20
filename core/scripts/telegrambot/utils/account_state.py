"""Shared, fail-closed account and entitlement state calculations.

The VPN panel reports ``expiration_days`` as a configured duration.  It is not
a countdown.  This module is the single place where panel and locally issued
service-cycle timestamps are turned into deadlines and remaining time.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

try:
    from .time_utils import format_utc_timestamp, parse_utc_timestamp, utc_now
except ImportError:  # Standalone diagnostics/tests.
    from time_utils import format_utc_timestamp, parse_utc_timestamp, utc_now


DEFAULT_TIMEZONE = "UTC"
SUCCESS_STATUSES = frozenset({"completed", "paid", "succeeded", "approved"})
CONNECTED_STATUSES = frozenset({"online", "offline"})
HOLD_STATUS = "on hold"


class PanelState(str, Enum):
    HOLD = "hold"
    CONNECTED = "connected"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class EntitlementState(str, Enum):
    CURRENT = "current"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class DeadlineSource(str, Enum):
    ISSUANCE = "issuance"
    PANEL = "panel"
    NONE = "none"


@dataclass(frozen=True)
class ServiceCycle:
    issued_at: datetime
    duration_days: int
    deadline: datetime
    record_id: str
    fingerprint: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("issued_at", "deadline"):
            result[key] = format_utc_timestamp(result[key])
        return result


@dataclass(frozen=True)
class AccountState:
    state: str
    panel_state: PanelState
    entitlement_state: EntitlementState
    normalized_status: str | None
    blocked: bool | None
    timer_started: bool
    configured_days: int | None
    panel_started_at: datetime | None
    panel_deadline: datetime | None
    panel_days_remaining: int | None
    entitlement_issued_at: datetime | None
    entitlement_deadline: datetime | None
    entitlement_days_remaining: int | None
    cycle_fingerprint: str | None
    service_deadline: datetime | None
    service_days_remaining: int | None
    service_duration_days: int | None
    deadline_source: DeadlineSource
    service_marker: str | None
    observed_at: datetime
    source: str
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["panel_state"] = self.panel_state.value
        result["entitlement_state"] = self.entitlement_state.value
        result["deadline_source"] = self.deadline_source.value
        for key in (
            "panel_started_at",
            "panel_deadline",
            "entitlement_issued_at",
            "entitlement_deadline",
            "service_deadline",
            "observed_at",
        ):
            value = result.get(key)
            result[key] = format_utc_timestamp(value) if value is not None else None
        return result


def bot_timezone(name: str | None = None):
    """Return UTC; retained as a compatibility name for older callers."""
    return timezone.utc


def parse_timestamp(value: Any, *, default_timezone=None) -> datetime | None:
    """Parse an API/bot timestamp and return an aware UTC datetime."""
    return parse_utc_timestamp(value, legacy_naive_timezone=default_timezone or timezone.utc)


def normalize_panel_status(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(
        str(value).strip().lower().replace("_", " ").replace("-", " ").split()
    )
    return normalized or None


def is_hold_status(value: Any) -> bool:
    return normalize_panel_status(value) == HOLD_STATUS


def safe_int(value: Any, default=None):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def remaining_full_days(deadline: datetime | None, now: datetime | None = None) -> int | None:
    if deadline is None:
        return None
    current = parse_timestamp(now or utc_now())
    if current is None:
        return None
    seconds = (deadline - current).total_seconds()
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 86400))


def elapsed_full_days(started_at: datetime | None, now: datetime | None = None) -> int | None:
    if started_at is None:
        return None
    current = parse_timestamp(now or utc_now())
    if current is None or current < started_at:
        return 0
    return int((current - started_at).total_seconds() // 86400)


def panel_deadline(user_data: Mapping[str, Any] | None) -> datetime | None:
    if not isinstance(user_data, Mapping):
        return None
    duration = safe_int(user_data.get("expiration_days"))
    # Both supported panels use zero as the canonical no-expiry value.  It
    # must take precedence over stale or adapter-derived date fields.
    if duration == 0:
        return None
    for field in ("account_expiration_date", "absolute_expiry", "expiration_at"):
        explicit = parse_timestamp(user_data.get(field))
        if explicit is not None:
            return explicit
    started_at = parse_timestamp(user_data.get("account_creation_date"))
    if duration is None or duration <= 0 or started_at is None:
        return None
    return started_at + timedelta(days=duration)


def panel_days_remaining(user_data: Mapping[str, Any] | None, now: datetime | None = None) -> int | None:
    return remaining_full_days(panel_deadline(user_data), now=now)


def verified_panel_expired(
    user_data: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> bool:
    """Match only the legacy panel-expiration rules with valid live values."""
    if not isinstance(user_data, Mapping) or strict_bool(user_data.get("blocked")) is not True:
        return False

    current = parse_timestamp(now or utc_now())
    deadline = panel_deadline(user_data)
    if current is not None and deadline is not None and current >= deadline:
        return True

    limit = safe_int(user_data.get("max_download_bytes"))
    uploaded = safe_int(user_data.get("upload_bytes"))
    downloaded = safe_int(user_data.get("download_bytes"))
    if (
        limit is not None
        and limit > 0
        and uploaded is not None
        and downloaded is not None
        and uploaded >= 0
        and downloaded >= 0
        and uploaded + downloaded >= limit
    ):
        return True
    return False


def _record_username(record: Mapping[str, Any]) -> str:
    return str(
        record.get("renewal_username")
        or record.get("username")
        or record.get("provisioned_username")
        or ""
    ).strip()


def _record_server(record: Mapping[str, Any]) -> str:
    return str(record.get("renewal_server_id") or record.get("server_id") or "primary").strip()


def _record_duration(record: Mapping[str, Any]) -> int | None:
    snapshot = record.get("renewal_plan_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    duration = safe_int(record.get("days"))
    if duration is None:
        duration = safe_int(snapshot.get("days"))
    return duration if duration is not None and duration > 0 else None


def _is_successful_cycle(record: Mapping[str, Any]) -> bool:
    mode = str(record.get("renewal_mode") or "").strip().lower()
    renewal_status = str(record.get("renewal_status") or "").strip().lower()
    if mode == "reserved":
        return renewal_status == "applied"
    if renewal_status and renewal_status not in {"applied", "completed", "succeeded"}:
        return False
    return str(record.get("status") or "completed").strip().lower() in SUCCESS_STATUSES


def _cycle_timestamp(record: Mapping[str, Any]) -> datetime | None:
    for field in (
        "renewal_applied_at",
        "updated_at",
        "created_at",
    ):
        parsed = parse_timestamp(record.get(field))
        if parsed is not None:
            return parsed
    # Historical records predate the shared cycle model. Accept their
    # successful completion timestamp only after all current fields above.
    for field in ("completed_at", "timestamp"):
        parsed = parse_timestamp(record.get(field))
        if parsed is not None:
            return parsed
    return None


def _iter_cycle_records(records: Any) -> Iterable[tuple[str, Mapping[str, Any]]]:
    if isinstance(records, Mapping):
        is_record = any(
            key in records
            for key in ("username", "renewal_username", "days", "renewal_plan_snapshot")
        )
        if is_record:
            yield str(records.get("payment_id") or records.get("reservation_id") or "record"), records
            renewals = records.get("renewals")
            if isinstance(renewals, list):
                for index, renewal in enumerate(renewals):
                    if not isinstance(renewal, Mapping):
                        continue
                    inherited = dict(renewal)
                    inherited.setdefault("username", records.get("username"))
                    inherited.setdefault("server_id", records.get("server_id"))
                    yield str(renewal.get("reservation_id") or f"renewal:{index}"), inherited
            return
        for record_id, record in records.items():
            if isinstance(record, Mapping):
                yield str(record_id), record
        return
    if isinstance(records, (list, tuple)):
        for index, record in enumerate(records):
            if isinstance(record, Mapping):
                yield str(record.get("payment_id") or record.get("reservation_id") or index), record


def resolve_service_cycle(
    records: Any,
    *,
    username: str,
    server_id: str | None,
    source: str,
) -> ServiceCycle | None:
    """Resolve the newest unambiguous, successfully applied issuance cycle."""
    expected_username = str(username or "").strip().lower()
    expected_server = str(server_id or "primary").strip().lower()
    candidates = []
    for record_id, record in _iter_cycle_records(records):
        if not _is_successful_cycle(record):
            continue
        if _record_username(record).lower() != expected_username:
            continue
        if _record_server(record).lower() != expected_server:
            continue
        duration = _record_duration(record)
        issued_at = _cycle_timestamp(record)
        if duration is None or issued_at is None:
            continue
        candidates.append((issued_at, str(record_id), duration, record))

    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
    latest_time = candidates[0][0]
    latest = [candidate for candidate in candidates if candidate[0] == latest_time]
    signatures = {(candidate[1], candidate[2]) for candidate in latest}
    if len(signatures) != 1:
        return None

    issued_at, record_id, duration, _record = latest[0]
    fingerprint_source = json.dumps(
        {
            "record_id": record_id,
            "username": expected_username,
            "server_id": expected_server,
            "issued_at": format_utc_timestamp(issued_at),
            "duration_days": duration,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24]
    return ServiceCycle(
        issued_at=issued_at,
        duration_days=duration,
        deadline=issued_at + timedelta(days=duration),
        record_id=record_id,
        fingerprint=fingerprint,
        source=source,
    )


def inspect_account(
    user_data: Mapping[str, Any] | None,
    *,
    cycle: ServiceCycle | None = None,
    now: datetime | None = None,
    observed_at: datetime | None = None,
    source: str = "live",
    available: bool = True,
    stale: bool = False,
) -> AccountState:
    current = parse_timestamp(now or utc_now()) or utc_now()
    observation = parse_timestamp(observed_at or current) or current
    data = user_data if isinstance(user_data, Mapping) else None
    status = normalize_panel_status(data.get("status")) if data is not None else None
    blocked = strict_bool(data.get("blocked")) if data is not None else None
    started_at = parse_timestamp(data.get("account_creation_date")) if data is not None else None
    duration = safe_int(data.get("expiration_days")) if data is not None else None
    unlimited_duration = duration == 0
    deadline = panel_deadline(data)
    explicit_timer_started = strict_bool(data.get("timer_started")) if data is not None else None
    timer_started = explicit_timer_started if explicit_timer_started is not None else started_at is not None
    if unlimited_duration:
        timer_started = True
    if started_at is None and timer_started and deadline is not None and duration is not None and duration >= 0:
        started_at = deadline - timedelta(days=duration)

    valid_duration = duration is not None and duration >= 0
    if not available or data is None or blocked is None:
        panel_state_value = PanelState.UNKNOWN
    elif blocked:
        panel_state_value = PanelState.BLOCKED
    elif unlimited_duration and status in CONNECTED_STATUSES | {HOLD_STATUS}:
        panel_state_value = PanelState.CONNECTED
    elif status == HOLD_STATUS and not timer_started and valid_duration:
        panel_state_value = PanelState.HOLD
    elif status in CONNECTED_STATUSES and timer_started and (valid_duration or deadline is not None):
        panel_state_value = PanelState.CONNECTED
    else:
        panel_state_value = PanelState.UNKNOWN

    service_deadline = None
    service_days = None
    service_duration = None
    deadline_source = DeadlineSource.NONE
    service_marker = None

    if not available or stale or panel_state_value == PanelState.UNKNOWN:
        entitlement = EntitlementState.UNKNOWN
    elif panel_state_value == PanelState.HOLD:
        # Before first use, the panel timer has not started.  Only the local
        # issuance cycle can bound how long the unused allocation is valid.
        if cycle is None:
            entitlement = EntitlementState.UNKNOWN
        else:
            entitlement = (
                EntitlementState.EXPIRED
                if current >= cycle.deadline
                else EntitlementState.CURRENT
            )
            service_deadline = cycle.deadline
            service_days = remaining_full_days(cycle.deadline, now=current)
            service_duration = cycle.duration_days
            deadline_source = DeadlineSource.ISSUANCE
            service_marker = cycle.fingerprint
    elif panel_state_value == PanelState.CONNECTED:
        # Once the account starts, the live panel is authoritative.  A local
        # issuance timestamp may describe an older, recycled username.
        entitlement = EntitlementState.CURRENT
        service_deadline = deadline
        service_days = remaining_full_days(deadline, now=current)
        service_duration = duration
        deadline_source = DeadlineSource.PANEL
    elif verified_panel_expired(data, now=current):
        entitlement = EntitlementState.EXPIRED
        service_deadline = deadline
        service_days = 0
        service_duration = duration
        deadline_source = DeadlineSource.PANEL
    else:
        # A manual/administrative block is not proof of service expiration.
        entitlement = EntitlementState.UNKNOWN
        service_deadline = deadline
        service_days = remaining_full_days(deadline, now=current)
        service_duration = duration
        deadline_source = DeadlineSource.PANEL if deadline is not None else DeadlineSource.NONE

    if deadline_source == DeadlineSource.PANEL:
        marker_source = json.dumps(
            {
                "source": deadline_source.value,
                "started_at": format_utc_timestamp(started_at) if started_at else None,
                "deadline": format_utc_timestamp(deadline) if deadline else None,
                "duration_days": duration,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        service_marker = hashlib.sha256(marker_source.encode("utf-8")).hexdigest()[:24]

    if panel_state_value == PanelState.UNKNOWN:
        normalized_state = "unknown"
    elif entitlement == EntitlementState.EXPIRED:
        normalized_state = "expired"
    else:
        normalized_state = panel_state_value.value

    return AccountState(
        state=normalized_state,
        panel_state=panel_state_value,
        entitlement_state=entitlement,
        normalized_status=status,
        blocked=blocked,
        timer_started=timer_started,
        configured_days=duration,
        panel_started_at=started_at,
        panel_deadline=deadline,
        panel_days_remaining=remaining_full_days(deadline, now=current),
        entitlement_issued_at=cycle.issued_at if cycle else None,
        entitlement_deadline=cycle.deadline if cycle else None,
        entitlement_days_remaining=(
            remaining_full_days(cycle.deadline, now=current) if cycle else None
        ),
        cycle_fingerprint=cycle.fingerprint if cycle else None,
        service_deadline=service_deadline,
        service_days_remaining=service_days,
        service_duration_days=service_duration,
        deadline_source=deadline_source,
        service_marker=service_marker,
        observed_at=observation,
        source=source,
        stale=bool(stale or not available),
    )


def is_business_expired(cycle: ServiceCycle | None, now: datetime | None = None) -> bool:
    if cycle is None:
        return False
    current = parse_timestamp(now or utc_now())
    return current is not None and current >= cycle.deadline
