"""Schema-v4 recovery marker for reserved renewals awaiting false external review."""

from __future__ import annotations

import json

from .time_utils import format_utc_timestamp


MIGRATION_METADATA_KEY = "renewal_timezone_recheck_v4"
RECHECK_MARKER = "v4_timezone_normalization"


def _dump(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _load_payload(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _eligible(record, *, payment=False):
    if not isinstance(record, dict):
        return False
    if payment and record.get("status") != "completed":
        return False
    return (
        record.get("renewal_mode") == "reserved"
        and record.get("renewal_status") == "attention"
        and record.get("renewal_attention_reason") == "external_renewal"
    )


def migrate_v4_renewal_timezone_rechecks(connection):
    """Queue a safe read-only reinspection; never alter the purchased renewal."""
    existing = connection.execute(
        "SELECT value FROM state_metadata WHERE key=?",
        (MIGRATION_METADATA_KEY,),
    ).fetchone()
    if existing is not None:
        try:
            return json.loads(existing["value"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    migrated_at = format_utc_timestamp()
    summary = {
        "migration": MIGRATION_METADATA_KEY,
        "migrated_at": migrated_at,
        "payment_rechecks": 0,
        "reseller_rechecks": 0,
        "affected_identifiers": [],
    }

    payment_rows = connection.execute(
        "SELECT scope, payment_id, payload_json FROM payments ORDER BY scope, payment_id"
    ).fetchall()
    for row in payment_rows:
        record = _load_payload(row["payload_json"])
        if not _eligible(record, payment=True):
            continue
        record["renewal_recheck_pending"] = RECHECK_MARKER
        record["renewal_recheck_requested_at"] = migrated_at
        record.pop("renewal_next_attempt_at", None)
        record["updated_at"] = migrated_at
        connection.execute(
            "UPDATE payments SET updated_at=?, payload_json=? WHERE scope=? AND payment_id=?",
            (migrated_at, _dump(record), row["scope"], row["payment_id"]),
        )
        summary["payment_rechecks"] += 1
        summary["affected_identifiers"].append(f"payment:{row['scope']}:{row['payment_id']}")

    renewal_rows = connection.execute(
        """
        SELECT reseller_id, config_index, renewal_index, payload_json
        FROM reseller_renewals
        ORDER BY reseller_id, config_index, renewal_index
        """
    ).fetchall()
    for row in renewal_rows:
        record = _load_payload(row["payload_json"])
        if not _eligible(record):
            continue
        record["renewal_recheck_pending"] = RECHECK_MARKER
        record["renewal_recheck_requested_at"] = migrated_at
        record.pop("renewal_next_attempt_at", None)
        connection.execute(
            """
            UPDATE reseller_renewals SET payload_json=?
            WHERE reseller_id=? AND config_index=? AND renewal_index=?
            """,
            (_dump(record), row["reseller_id"], row["config_index"], row["renewal_index"]),
        )
        summary["reseller_rechecks"] += 1
        summary["affected_identifiers"].append(
            f"reseller:{row['reseller_id']}:{row['config_index']}:{row['renewal_index']}"
        )

    connection.execute(
        """
        INSERT INTO state_metadata(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (MIGRATION_METADATA_KEY, _dump(summary), migrated_at),
    )
    return summary
