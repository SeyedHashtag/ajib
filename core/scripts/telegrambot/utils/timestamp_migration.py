"""Targeted schema-v3 repair for corroborated legacy renewal timestamps."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .time_utils import format_utc_timestamp, legacy_timezone, parse_utc_timestamp


LOGGER = logging.getLogger("ajib.timestamp_migration")
MIGRATION_METADATA_KEY = "utc_timestamp_migration_v3"
MAX_CORROBORATION_SECONDS = 10 * 60


def _naive_datetime(value):
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or ":" not in raw or raw.endswith(("Z", "z")):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else None


def _legacy_local_as_utc(value):
    parsed = _naive_datetime(value)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=legacy_timezone()).astimezone(timezone.utc)


def _independent_utc_anchors(record):
    anchors = []
    for field in ("reviewed_at", "incentives_finalized_at"):
        value = record.get(field)
        parsed = parse_utc_timestamp(value, legacy_naive_timezone=timezone.utc)
        if parsed is not None:
            anchors.append((field, parsed))
    return anchors


def _corroborated_local(value, anchors):
    converted = _legacy_local_as_utc(value)
    if converted is None:
        return None
    if any(
        abs((converted - anchor).total_seconds()) <= MAX_CORROBORATION_SECONDS
        for _field, anchor in anchors
    ):
        return converted
    return None


def _dump(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _repair_payment(connection, row, record, anchors, completed_utc):
    old_completed = record["completed_at"]
    new_completed = format_utc_timestamp(completed_utc)
    changed_fields = 0

    record["completed_at"] = new_completed
    changed_fields += 1
    converted_values = {old_completed: new_completed}

    for field in ("renewal_reserved_at",):
        old_value = record.get(field)
        converted = (
            completed_utc
            if old_value == old_completed
            else _corroborated_local(old_value, anchors)
        )
        if converted is None:
            continue
        new_value = format_utc_timestamp(converted)
        record[field] = new_value
        converted_values[old_value] = new_value
        changed_fields += 1

    updates = record.get("updates")
    if isinstance(updates, list):
        for event in updates:
            if not isinstance(event, dict) or event.get("status") != "completed":
                continue
            old_value = event.get("timestamp")
            converted = (
                completed_utc
                if old_value == old_completed
                else _corroborated_local(old_value, anchors)
            )
            if converted is None:
                continue
            new_value = format_utc_timestamp(converted)
            event["timestamp"] = new_value
            converted_values[old_value] = new_value
            changed_fields += 1

    for field in ("created_at", "updated_at"):
        old_value = record.get(field)
        if old_value in converted_values:
            record[field] = converted_values[old_value]
            changed_fields += 1

    typed_created_at = row["created_at"]
    typed_updated_at = row["updated_at"]
    new_typed_created_at = converted_values.get(typed_created_at, typed_created_at)
    new_typed_updated_at = converted_values.get(typed_updated_at, typed_updated_at)
    connection.execute(
        """
        UPDATE payments
        SET created_at=?, updated_at=?, payload_json=?
        WHERE scope=? AND payment_id=?
        """,
        (
            new_typed_created_at,
            new_typed_updated_at,
            _dump(record),
            row["scope"],
            row["payment_id"],
        ),
    )

    event_rows = connection.execute(
        """
        SELECT sequence, status, occurred_at, payload_json
        FROM payment_events
        WHERE scope=? AND payment_id=?
        ORDER BY sequence
        """,
        (row["scope"], row["payment_id"]),
    ).fetchall()
    for event_row in event_rows:
        if event_row["status"] != "completed":
            continue
        old_value = event_row["occurred_at"]
        converted = (
            completed_utc
            if old_value == old_completed
            else _corroborated_local(old_value, anchors)
        )
        if converted is None:
            continue
        new_value = format_utc_timestamp(converted)
        try:
            event_payload = json.loads(event_row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            event_payload = None
        serialized_event = event_row["payload_json"]
        if isinstance(event_payload, dict):
            payload_timestamp = event_payload.get("timestamp")
            if payload_timestamp == old_value or payload_timestamp == old_completed:
                event_payload["timestamp"] = new_value
            serialized_event = _dump(event_payload)
        connection.execute(
            """
            UPDATE payment_events
            SET occurred_at=?, payload_json=?
            WHERE scope=? AND payment_id=? AND sequence=?
            """,
            (
                new_value,
                serialized_event,
                row["scope"],
                row["payment_id"],
                event_row["sequence"],
            ),
        )
        changed_fields += 1
    return changed_fields


def migrate_v3_utc_timestamps(connection):
    """Repair only reserved renewals whose legacy-local time is corroborated."""
    existing = connection.execute(
        "SELECT value FROM state_metadata WHERE key=?",
        (MIGRATION_METADATA_KEY,),
    ).fetchone()
    if existing is not None:
        try:
            return json.loads(existing["value"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    rows = connection.execute(
        """
        SELECT scope, payment_id, created_at, updated_at, payload_json
        FROM payments
        ORDER BY scope, payment_id
        """
    ).fetchall()
    summary = {
        "migration": MIGRATION_METADATA_KEY,
        "migrated_at": format_utc_timestamp(),
        "legacy_timezone": str(legacy_timezone()),
        "scanned_count": len(rows),
        "candidate_count": 0,
        "changed_count": 0,
        "changed_field_count": 0,
        "skipped_count": 0,
        "affected_identifiers": [],
        "ambiguous_identifiers": [],
    }

    for row in rows:
        try:
            record = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "renewal" or record.get("renewal_mode") != "reserved":
            continue
        if _naive_datetime(record.get("completed_at")) is None:
            continue

        summary["candidate_count"] += 1
        identifier = f"{row['scope']}:{row['payment_id']}"
        anchors = _independent_utc_anchors(record)
        completed_utc = _corroborated_local(record.get("completed_at"), anchors)
        if completed_utc is None:
            summary["skipped_count"] += 1
            summary["ambiguous_identifiers"].append(identifier)
            LOGGER.warning(
                "Ambiguous legacy renewal timestamp left unchanged",
                extra={
                    "event": "utc_timestamp_migration_ambiguous",
                    "payment_scope": row["scope"],
                    "payment_id": row["payment_id"],
                },
            )
            continue

        summary["changed_field_count"] += _repair_payment(
            connection, row, record, anchors, completed_utc
        )
        summary["changed_count"] += 1
        summary["affected_identifiers"].append(identifier)

    serialized = _dump(summary)
    connection.execute(
        """
        INSERT INTO state_metadata(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (MIGRATION_METADATA_KEY, serialized, summary["migrated_at"]),
    )
    LOGGER.info(
        "UTC timestamp migration completed scanned=%d candidates=%d changed=%d skipped=%d identifiers=%s",
        summary["scanned_count"],
        summary["candidate_count"],
        summary["changed_count"],
        summary["skipped_count"],
        ",".join(summary["affected_identifiers"]),
        extra={"event": "utc_timestamp_migration_completed"},
    )
    return summary
