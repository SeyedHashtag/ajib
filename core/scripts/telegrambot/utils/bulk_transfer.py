"""Persistent, resumable mass copy and server-migration orchestration."""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import timedelta, timezone

from . import database
from .api_client import (
    BLITZ_PANEL,
    GIB,
    THREE_X_UI_PANEL,
    BulkUserTransferSpec,
    MultiServerAPI,
    UserCopySpec,
    UserRef,
    _destination_transfer_password,
)
from .account_state import panel_deadline
from .time_utils import format_utc_timestamp, utc_now
from .translations import DEFAULT_LANGUAGE, get_message_text


LOGGER = logging.getLogger("ajib.bulk_transfer")
ACTIVE_JOB_STATUSES = ("queued", "running", "cancel_requested")
TERMINAL_ITEM_STAGES = ("completed", "skipped", "failed", "manual_review")
MAX_SOURCE_DELETE_ATTEMPTS = 3
MAX_NOTIFICATION_ATTEMPTS = 5
PROGRESS_INTERVAL_SECONDS = 2.0
ALLOWED_HYSTERIA_PROTOCOLS = {"hysteria", "hysteria2", "hy2"}

_worker_lock = threading.RLock()
_worker_thread = None
_progress_callback = None


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(raw, default=None):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if default is None else default


def _row_dict(row):
    return dict(row) if row is not None else None


def _panel_type(client) -> str:
    return str(getattr(client, "panel_type", BLITZ_PANEL) or BLITZ_PANEL)


def _server_config(multi_api, server_id):
    target = str(server_id or "")
    return next((item for item in multi_api.servers if str(item.get("id")) == target), None)


def _exact_client(multi_api, server_id):
    return multi_api.get_client(server_id) if _server_config(multi_api, server_id) else None


def _public_server(server, panel=None):
    return {
        key: server.get(key)
        for key in (
            "id", "name", "enabled", "weight", "default_inbound_ids",
            "default_limit_ip",
        )
        if key in server
    } | {"panel": panel or server.get("panel") or BLITZ_PANEL}


def _iter_users(users):
    if isinstance(users, dict):
        for key, value in users.items():
            if isinstance(value, dict):
                yield str(value.get("username") or key), value
    elif isinstance(users, list):
        for value in users:
            if isinstance(value, dict) and value.get("username"):
                yield str(value["username"]), value


def _nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def validate_transfer_spec(spec: BulkUserTransferSpec) -> BulkUserTransferSpec:
    if not isinstance(spec, BulkUserTransferSpec):
        raise ValueError("bulk_spec_invalid")
    mode = str(spec.mode or "").strip().lower()
    policy = str(spec.notification_policy or "").strip().lower()
    source = str(spec.source_server_id or "").strip()
    destination = str(spec.destination_server_id or "").strip()
    if mode not in {"copy", "migrate"}:
        raise ValueError("mode_invalid")
    if not source or not destination or source == destination:
        raise ValueError("destination_invalid")
    if policy not in {"send", "disabled", "deferred"}:
        raise ValueError("notification_policy_invalid")
    if mode == "copy" and policy != "disabled":
        raise ValueError("copy_notifications_not_allowed")
    inbound_ids = []
    for raw in spec.inbound_ids:
        value = _nonnegative_int(raw)
        if value is None or value in inbound_ids:
            continue
        inbound_ids.append(value)
    return BulkUserTransferSpec(
        mode=mode,
        source_server_id=source,
        destination_server_id=destination,
        inbound_ids=tuple(inbound_ids),
        requesting_admin=str(spec.requesting_admin or ""),
        notification_policy=policy,
    )


def compatible_destinations(source_server_id, multi_api=None):
    multi_api = multi_api or MultiServerAPI()
    source = _server_config(multi_api, source_server_id)
    if source is None:
        return []
    source_client = _exact_client(multi_api, source_server_id)
    source_panel = _panel_type(source_client)
    result = []
    for server in multi_api.servers:
        if str(server.get("id")) == str(source_server_id):
            continue
        client = _exact_client(multi_api, server.get("id"))
        destination_panel = _panel_type(client)
        if source_panel == THREE_X_UI_PANEL and destination_panel != BLITZ_PANEL:
            continue
        if source_panel not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
            continue
        if destination_panel not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
            continue
        result.append(_public_server(server, destination_panel))
    return result


def _source_rejection(user, source_panel, destination_panel, source_options):
    password = user.get("password")
    if source_panel == THREE_X_UI_PANEL:
        metadata = user.get("credential_metadata")
        fields = metadata.get("fields_present") if isinstance(metadata, dict) else []
        selected = metadata.get("selected_field") if isinstance(metadata, dict) else None
        if "auth" not in (fields or []) or selected != "auth" or not str(password or "").strip():
            return "source_auth_missing"
        if source_options is None:
            return "source_inbounds_unavailable"
        option_map = {int(item["id"]): item for item in source_options if _nonnegative_int(item.get("id")) is not None}
        attached = {
            value for value in (_nonnegative_int(item) for item in (user.get("inbound_ids") or []))
            if value is not None
        }
        if not any(
            inbound_id in option_map
            and str(option_map[inbound_id].get("protocol") or "").lower() in ALLOWED_HYSTERIA_PROTOCOLS
            for inbound_id in attached
        ):
            return "source_not_hysteria2"
    elif not isinstance(password, str) or not password:
        return "source_password_missing"

    required = (
        _nonnegative_int(user.get("max_download_bytes")),
        _nonnegative_int(user.get("upload_bytes")),
        _nonnegative_int(user.get("download_bytes")),
        _nonnegative_int(user.get("expiration_days")),
    )
    access_key = "unlimited_ip" if source_panel == THREE_X_UI_PANEL else "unlimited_user"
    if any(value is None for value in required):
        return "source_state_malformed"
    if not isinstance(user.get("blocked"), bool) or not isinstance(user.get(access_key), bool):
        return "source_state_malformed"
    delayed = user.get("delayed_start")
    if not isinstance(delayed, bool):
        delayed = user.get("timer_started") is False or str(user.get("status") or "").lower() == "on hold"
    unlimited_duration = required[3] == 0
    if not unlimited_duration and not delayed and not (
        user.get("expiry") or user.get("expiry_time") or user.get("expiration_date")
    ):
        # panel_deadline performs the definitive live check; this catches only
        # obviously malformed records without making preflight panel-specific.
        if not user.get("account_creation_date"):
            return "source_state_malformed"
    if destination_panel == BLITZ_PANEL:
        total, upload, download, _days = required
        if total <= 0:
            return "blitz_unlimited_not_representable"
        if total - upload - download <= 0:
            return "blitz_allowance_exhausted"
    return None


def preflight_transfer(spec: BulkUserTransferSpec, multi_api=None) -> dict:
    """Fetch a confirmation-time snapshot and classify every source user."""
    spec = validate_transfer_spec(spec)
    multi_api = multi_api or MultiServerAPI()
    source_client = _exact_client(multi_api, spec.source_server_id)
    destination_client = _exact_client(multi_api, spec.destination_server_id)
    if source_client is None:
        return {"ok": False, "error": "source_server_missing"}
    if destination_client is None:
        return {"ok": False, "error": "destination_server_missing"}
    source_panel = _panel_type(source_client)
    destination_panel = _panel_type(destination_client)
    if source_panel == THREE_X_UI_PANEL and destination_panel != BLITZ_PANEL:
        return {"ok": False, "error": "destination_panel_not_supported"}
    if source_panel not in {BLITZ_PANEL, THREE_X_UI_PANEL} or destination_panel not in {BLITZ_PANEL, THREE_X_UI_PANEL}:
        return {"ok": False, "error": "destination_panel_not_supported"}

    source_users = source_client.get_users()
    if source_users is None:
        return {"ok": False, "error": "source_unavailable"}
    destination_users = destination_client.get_users()
    if destination_users is None:
        return {"ok": False, "error": "destination_unavailable"}

    if destination_panel == THREE_X_UI_PANEL:
        if not spec.inbound_ids:
            return {"ok": False, "error": "inbounds_required"}
        options = destination_client.get_inbound_options()
        if options is None:
            return {"ok": False, "error": "inbounds_unavailable"}
        option_map = {int(item["id"]): item for item in options if _nonnegative_int(item.get("id")) is not None}
        if any(
            inbound_id not in option_map
            or str(option_map[inbound_id].get("protocol") or "").lower() not in ALLOWED_HYSTERIA_PROTOCOLS
            for inbound_id in spec.inbound_ids
        ):
            return {"ok": False, "error": "inbounds_not_hysteria2"}

    source_options = source_client.get_inbound_options() if source_panel == THREE_X_UI_PANEL else []
    destination_names = {name.casefold() for name, _user in _iter_users(destination_users)}
    items = []
    reasons = Counter()
    for username, user in sorted(_iter_users(source_users), key=lambda item: item[0].casefold()):
        reason = "destination_exists" if username.casefold() in destination_names else _source_rejection(
            user, source_panel, destination_panel, source_options
        )
        if reason:
            reasons[reason] += 1
        items.append({
            "username": username,
            "source_panel_type": source_panel,
            "eligible": reason is None,
            "reason": reason,
        })
    return {
        "ok": True,
        "spec": spec,
        "snapshot_at": format_utc_timestamp(),
        "source_panel_type": source_panel,
        "destination_panel_type": destination_panel,
        "items": items,
        "total": len(items),
        "eligible": sum(1 for item in items if item["eligible"]),
        "collisions": reasons.get("destination_exists", 0),
        "rejections": dict(sorted(reasons.items())),
    }


def create_transfer_job(spec, preflight, *, status_chat_id=None, status_message_id=None, path=None):
    spec = validate_transfer_spec(spec)
    if not isinstance(preflight, dict) or not preflight.get("ok"):
        return {"ok": False, "error": "preflight_required"}
    items = list(preflight.get("items") or [])
    if not any(item.get("eligible") for item in items):
        return {"ok": False, "error": "no_eligible_users"}
    job_id = uuid.uuid4().hex
    now = format_utc_timestamp()
    try:
        with database.write_transaction(path, operation="create_bulk_transfer") as connection:
            connection.execute(
                """
                INSERT INTO bulk_transfer_jobs(
                    job_id, mode, source_server_id, destination_server_id,
                    inbound_ids_json, requested_by, notification_policy, status,
                    snapshot_at, total_users, eligible_users, status_chat_id,
                    status_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, spec.mode, spec.source_server_id, spec.destination_server_id,
                    _dump(list(spec.inbound_ids)), spec.requesting_admin,
                    spec.notification_policy, preflight.get("snapshot_at") or now,
                    len(items), sum(1 for item in items if item.get("eligible")),
                    str(status_chat_id) if status_chat_id is not None else None,
                    int(status_message_id) if status_message_id is not None else None,
                    now, now,
                ),
            )
            for ordinal, item in enumerate(items):
                eligible = bool(item.get("eligible"))
                connection.execute(
                    """
                    INSERT INTO bulk_transfer_items(
                        job_id, ordinal, username, source_panel_type, stage,
                        error_code, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, ordinal, str(item.get("username") or ""),
                        item.get("source_panel_type"), "pending" if eligible else "skipped",
                        None if eligible else str(item.get("reason") or "preflight_rejected"),
                        now, now, None if eligible else now,
                    ),
                )
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "active_job_exists"}
    return {"ok": True, "job_id": job_id}


def _decode_job(row):
    result = _row_dict(row)
    if result is not None:
        result["inbound_ids"] = tuple(_load(result.pop("inbound_ids_json", "[]"), []))
    return result


def get_job(job_id, *, path=None, include_items=False):
    connection = database.get_connection(path)
    row = connection.execute("SELECT * FROM bulk_transfer_jobs WHERE job_id=?", (str(job_id),)).fetchone()
    job = _decode_job(row)
    if job is None:
        return None
    if include_items:
        job["items"] = [dict(item) for item in connection.execute(
            "SELECT * FROM bulk_transfer_items WHERE job_id=? ORDER BY ordinal", (job["job_id"],)
        )]
    return job


def get_active_job(*, path=None):
    row = database.get_connection(path).execute(
        """SELECT * FROM bulk_transfer_jobs
           WHERE status IN ('queued','running','cancel_requested')
           ORDER BY created_at LIMIT 1"""
    ).fetchone()
    return _decode_job(row)


def get_latest_job(*, path=None):
    row = database.get_connection(path).execute(
        "SELECT * FROM bulk_transfer_jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _decode_job(row)


def job_counts(job_id, *, path=None):
    connection = database.get_connection(path)
    rows = connection.execute(
        "SELECT stage, COUNT(*) AS count FROM bulk_transfer_items WHERE job_id=? GROUP BY stage",
        (str(job_id),),
    ).fetchall()
    counts = {str(row["stage"]): int(row["count"]) for row in rows}
    counts["processed"] = sum(counts.get(stage, 0) for stage in TERMINAL_ITEM_STAGES)
    counts["remaining"] = sum(
        count for stage, count in counts.items()
        if stage not in TERMINAL_ITEM_STAGES and stage not in {"processed", "remaining"}
    )
    counts["unmatched"] = int(connection.execute(
        """SELECT COUNT(*) FROM bulk_transfer_items i
           JOIN bulk_transfer_jobs j ON j.job_id=i.job_id
           WHERE i.job_id=? AND j.mode='migrate' AND i.stage='completed'
           AND i.recipient_count=0""",
        (str(job_id),),
    ).fetchone()[0])
    return counts


def notification_counts(job_id, *, path=None):
    rows = database.get_connection(path).execute(
        "SELECT status, COUNT(*) AS count FROM bulk_transfer_notifications WHERE job_id=? GROUP BY status",
        (str(job_id),),
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def request_cancel(job_id, admin_id, *, path=None):
    with database.write_transaction(path, operation="cancel_bulk_transfer") as connection:
        result = connection.execute(
            """UPDATE bulk_transfer_jobs SET status='cancel_requested', updated_at=?
               WHERE job_id=? AND requested_by=? AND status IN ('queued','running')""",
            (format_utc_timestamp(), str(job_id), str(admin_id)),
        )
    return result.rowcount == 1


def resume_job(job_id, admin_id, *, path=None):
    now = format_utc_timestamp()
    try:
        with database.write_transaction(path, operation="resume_bulk_transfer") as connection:
            result = connection.execute(
                """UPDATE bulk_transfer_jobs SET status='queued', completed_at=NULL,
                   last_error=NULL, updated_at=?
                   WHERE job_id=? AND requested_by=? AND status IN ('cancelled','failed')
                   AND EXISTS (
                       SELECT 1 FROM bulk_transfer_items i WHERE i.job_id=bulk_transfer_jobs.job_id
                       AND i.stage NOT IN ('completed','skipped','failed','manual_review')
                   )""",
                (now, str(job_id), str(admin_id)),
            )
    except sqlite3.IntegrityError:
        return False
    if result.rowcount:
        start_transfer_worker(path=path)
    return result.rowcount == 1


def _sanitize_copy_result(result):
    allowed = {
        "source_server_id", "source_panel_type", "destination_server_id",
        "destination_server_name", "panel_type", "inbound_ids",
        "direct_link", "blitz_quota_gib", "expiry_rounded",
        "expiry_extension_seconds",
    }
    return {key: result.get(key) for key in allowed if key in result}


def _exact_identity(record, username, source_server_id):
    if not isinstance(record, dict):
        return False
    record_username = record.get("renewal_username") or record.get("username")
    record_server = record.get("renewal_server_id") or record.get("server_id") or "primary"
    return (
        str(record_username or "").casefold() == str(username).casefold()
        and str(record_server).casefold() == str(source_server_id).casefold()
    )


def _rehome_payment_record(record, username, source_server_id, destination_server_id):
    if not _exact_identity(record, username, source_server_id):
        return False
    changed = False
    if str(record.get("username") or "").casefold() == str(username).casefold():
        own_server = record.get("server_id") or "primary"
        if str(own_server).casefold() == str(source_server_id).casefold():
            record["server_id"] = destination_server_id
            changed = True
    if str(record.get("renewal_username") or "").casefold() == str(username).casefold():
        renewal_server = record.get("renewal_server_id") or record.get("server_id") or "primary"
        if str(renewal_server).casefold() == str(source_server_id).casefold():
            record["renewal_server_id"] = destination_server_id
            changed = True
    return changed


def _active_cleanup_record(record):
    return str(record.get("cleanup_status") or "").lower() not in {
        "deleted", "already_missing", "renewed"
    } and not record.get("cleanup_deleted_at")


def _insert_recipient(connection, job, item, route_scope, recipient_id, now):
    if job["notification_policy"] == "disabled" or recipient_id in (None, ""):
        return 0
    connection.execute(
        """
        INSERT OR IGNORE INTO bulk_transfer_notifications(
            job_id, item_ordinal, username, route_scope, recipient_id,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'held', ?, ?)
        """,
        (
            job["job_id"], item["ordinal"], item["username"],
            str(route_scope), str(recipient_id), now, now,
        ),
    )
    return int(connection.execute(
        """SELECT COUNT(*) FROM bulk_transfer_notifications
           WHERE job_id=? AND item_ordinal=? AND route_scope=? AND recipient_id=?""",
        (job["job_id"], item["ordinal"], str(route_scope), str(recipient_id)),
    ).fetchone()[0] > 0)


def _rehome_records_and_hold_recipients(job, item, *, path=None):
    """Move exact operational references and journal recipients atomically."""
    now = format_utc_timestamp()
    changed = 0
    recipient_keys = set()
    with database.write_transaction(path, operation="rehome_bulk_user_records") as connection:
        for row in connection.execute("SELECT scope, payment_id, payload_json FROM payments").fetchall():
            record = _load(row["payload_json"], {})
            if not _rehome_payment_record(
                record, item["username"], job["source_server_id"], job["destination_server_id"]
            ):
                continue
            connection.execute(
                "UPDATE payments SET payload_json=?, updated_at=? WHERE scope=? AND payment_id=?",
                (_dump(record), now, row["scope"], row["payment_id"]),
            )
            changed += 1
            route = row["scope"] if str(row["scope"]).startswith("hosted:") else "main"
            recipient = record.get("user_id")
            if recipient not in (None, ""):
                recipient_keys.add((route, str(recipient)))

        reseller_payloads = {
            str(row["reseller_id"]): _load(row["payload_json"], {})
            for row in connection.execute(
                "SELECT reseller_id, payload_json FROM resellers"
            ).fetchall()
        }
        changed_resellers = set()
        for row in connection.execute(
            "SELECT reseller_id, config_index, username, server_id, payload_json FROM reseller_configs"
        ).fetchall():
            if str(row["username"] or "").casefold() != str(item["username"]).casefold():
                continue
            if str(row["server_id"] or "primary").casefold() != str(job["source_server_id"]).casefold():
                continue
            record = _load(row["payload_json"], {})
            record["server_id"] = job["destination_server_id"]
            parent = reseller_payloads.get(str(row["reseller_id"]))
            configs = parent.get("configs") if isinstance(parent, dict) else None
            if (
                isinstance(configs, list)
                and int(row["config_index"]) < len(configs)
                and isinstance(configs[int(row["config_index"])], dict)
            ):
                configs[int(row["config_index"])]["server_id"] = job["destination_server_id"]
                changed_resellers.add(str(row["reseller_id"]))
            connection.execute(
                """UPDATE reseller_configs SET server_id=?, payload_json=?
                   WHERE reseller_id=? AND config_index=?""",
                (job["destination_server_id"], _dump(record), row["reseller_id"], row["config_index"]),
            )
            changed += 1
            customer_id = record.get("customer_telegram_id")
            if customer_id not in (None, ""):
                recipient_keys.add((f"hosted:{row['reseller_id']}", str(customer_id)))

        for row in connection.execute(
            "SELECT reseller_id, config_index, renewal_index, payload_json FROM reseller_renewals"
        ).fetchall():
            record = _load(row["payload_json"], {})
            if _rehome_payment_record(
                record, item["username"], job["source_server_id"], job["destination_server_id"]
            ):
                connection.execute(
                    """UPDATE reseller_renewals SET payload_json=?
                       WHERE reseller_id=? AND config_index=? AND renewal_index=?""",
                    (_dump(record), row["reseller_id"], row["config_index"], row["renewal_index"]),
                )
                parent = reseller_payloads.get(str(row["reseller_id"]))
                configs = parent.get("configs") if isinstance(parent, dict) else None
                config_index = int(row["config_index"])
                renewal_index = int(row["renewal_index"])
                if (
                    isinstance(configs, list)
                    and config_index < len(configs)
                    and isinstance(configs[config_index], dict)
                    and isinstance(configs[config_index].get("renewals"), list)
                    and renewal_index < len(configs[config_index]["renewals"])
                    and isinstance(configs[config_index]["renewals"][renewal_index], dict)
                ):
                    configs[config_index]["renewals"][renewal_index] = record
                    changed_resellers.add(str(row["reseller_id"]))
                changed += 1

        for reseller_id in changed_resellers:
            connection.execute(
                "UPDATE resellers SET payload_json=? WHERE reseller_id=?",
                (_dump(reseller_payloads[reseller_id]), reseller_id),
            )

        kv_namespaces = {"test_configs", "expired_cleanup", "traffic_alerts"}
        kv_rows = connection.execute(
            "SELECT namespace, scope, state_key, value_json FROM kv_state"
        ).fetchall()
        for row in kv_rows:
            if row["namespace"] not in kv_namespaces:
                continue
            record = _load(row["value_json"], {})
            if not isinstance(record, dict):
                continue
            if row["namespace"] == "expired_cleanup" and not _active_cleanup_record(record):
                continue
            if not _exact_identity(record, item["username"], job["source_server_id"]):
                continue
            if str(record.get("server_id") or "primary").casefold() == str(job["source_server_id"]).casefold():
                record["server_id"] = job["destination_server_id"]
            if str(record.get("renewal_server_id") or "").casefold() == str(job["source_server_id"]).casefold():
                record["renewal_server_id"] = job["destination_server_id"]
            new_key = row["state_key"]
            old_exact_key = f"{job['source_server_id']}:{item['username']}"
            if str(new_key).casefold() == old_exact_key.casefold():
                new_key = f"{job['destination_server_id']}:{item['username']}"
            if new_key != row["state_key"]:
                connection.execute(
                    "DELETE FROM kv_state WHERE namespace=? AND scope=? AND state_key=?",
                    (row["namespace"], row["scope"], row["state_key"]),
                )
                connection.execute(
                    """INSERT INTO kv_state(namespace, scope, state_key, value_json, updated_at)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, scope, state_key)
                       DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                    (row["namespace"], row["scope"], new_key, _dump(record), now),
                )
            else:
                connection.execute(
                    """UPDATE kv_state SET value_json=?, updated_at=?
                       WHERE namespace=? AND scope=? AND state_key=?""",
                    (_dump(record), now, row["namespace"], row["scope"], row["state_key"]),
                )
            changed += 1
            if row["namespace"] == "test_configs":
                recipient = record.get("telegram_id") or row["state_key"]
                if recipient not in (None, ""):
                    recipient_keys.add(("main", str(recipient)))

        for route, recipient in sorted(recipient_keys):
            _insert_recipient(connection, job, item, route, recipient, now)
        metadata = _load(item.get("result_json"), {})
        if not recipient_keys:
            metadata["unmatched_panel_account"] = True
        connection.execute(
            """UPDATE bulk_transfer_items SET stage='records_updated', records_updated=?,
               recipient_count=?, result_json=?, updated_at=?
               WHERE job_id=? AND ordinal=?""",
            (
                changed, len(recipient_keys), _dump(metadata), now,
                job["job_id"], item["ordinal"],
            ),
        )
    return {"records_updated": changed, "recipients": len(recipient_keys)}


def _set_item(job_id, ordinal, *, path=None, **fields):
    if not fields:
        return
    fields["updated_at"] = format_utc_timestamp()
    columns = ", ".join(f"{key}=?" for key in fields)
    with database.write_transaction(path, operation="update_bulk_item") as connection:
        connection.execute(
            f"UPDATE bulk_transfer_items SET {columns} WHERE job_id=? AND ordinal=?",
            (*fields.values(), str(job_id), int(ordinal)),
        )


def _rollback_destination(multi_api, job, item):
    destination = _exact_client(multi_api, job["destination_server_id"])
    if destination is None:
        return False
    destination.delete_user(item["username"])
    lookup = destination.get_user_result(item["username"])
    return lookup.get("status") == "missing"


def _canonical_note(value):
    value = str(value or "").strip()
    return value or None


def _ceil_utc_day(value):
    current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight if current == midnight else midnight + timedelta(days=1)


def _interrupted_destination_match(
    job,
    source_user,
    destination_user,
    destination_panel,
    username,
):
    """Strictly compare live states before adopting an interrupted creation."""
    source_panel = str(source_user.get("panel_type") or BLITZ_PANEL)
    source_access = source_user.get(
        "unlimited_ip" if source_panel == THREE_X_UI_PANEL else "unlimited_user"
    )
    destination_access = destination_user.get(
        "unlimited_ip" if destination_panel == THREE_X_UI_PANEL else "unlimited_user"
    )
    total = _nonnegative_int(source_user.get("max_download_bytes"))
    upload = _nonnegative_int(source_user.get("upload_bytes"))
    download = _nonnegative_int(source_user.get("download_bytes"))
    days = _nonnegative_int(source_user.get("expiration_days"))
    if None in (total, upload, download, days):
        return False, {}
    if destination_panel == THREE_X_UI_PANEL:
        expected_total = total
        expected_upload = upload
        expected_download = download
    else:
        remaining = total - upload - download
        if remaining <= 0:
            return False, {}
        expected_total = int(math.ceil(remaining / GIB)) * GIB
        expected_upload = 0
        expected_download = 0
    destination_upload = destination_user.get("upload_bytes")
    destination_download = destination_user.get("download_bytes")
    if destination_panel == BLITZ_PANEL:
        destination_upload = 0 if destination_upload is None else destination_upload
        destination_download = 0 if destination_download is None else destination_download
    expected_password = _destination_transfer_password(
        username,
        source_user.get("password"),
        source_panel,
        destination_panel,
    )
    matched = (
        destination_user.get("password") == expected_password
        and _nonnegative_int(destination_user.get("max_download_bytes")) == expected_total
        and _nonnegative_int(destination_upload) == expected_upload
        and _nonnegative_int(destination_download) == expected_download
        and destination_user.get("blocked") is source_user.get("blocked")
        and destination_access is source_access
        and _nonnegative_int(destination_user.get("expiration_days")) == days
        and _canonical_note(destination_user.get("note")) == _canonical_note(source_user.get("note"))
    )
    source_delayed = source_user.get("delayed_start") is True
    destination_delayed = destination_user.get("delayed_start") is True
    expiry_extension = 0.0
    if days == 0:
        matched = (
            matched
            and destination_delayed is False
            and panel_deadline(source_user) is None
            and panel_deadline(destination_user) is None
        )
    elif source_delayed:
        matched = matched and destination_delayed
    else:
        source_expiry = panel_deadline(source_user)
        destination_expiry = panel_deadline(destination_user)
        expected_expiry = (
            _ceil_utc_day(source_expiry)
            if source_expiry is not None and destination_panel == BLITZ_PANEL
            else source_expiry
        )
        if expected_expiry is None or destination_expiry is None:
            matched = False
        else:
            matched = matched and abs((destination_expiry - expected_expiry).total_seconds()) <= 2
            expiry_extension = max(0.0, (expected_expiry - source_expiry).total_seconds())
    if destination_panel == THREE_X_UI_PANEL:
        selected = {int(value) for value in (job.get("inbound_ids") or ())}
        attached = {
            value for value in (_nonnegative_int(raw) for raw in (destination_user.get("inbound_ids") or []))
            if value is not None
        }
        matched = matched and selected.issubset(attached)
    return matched, {
        "source_panel_type": source_panel,
        "panel_type": destination_panel,
        "destination_server_id": job["destination_server_id"],
        "expiry_rounded": expiry_extension > 0,
        "expiry_extension_seconds": expiry_extension,
        "recovered_after_restart": True,
    }


def _release_item_notifications(connection, job, item, now):
    if job["notification_policy"] == "send":
        connection.execute(
            """UPDATE bulk_transfer_notifications SET status='pending', updated_at=?
               WHERE job_id=? AND item_ordinal=? AND status='held'""",
            (now, job["job_id"], item["ordinal"]),
        )


def _complete_item(job, item, *, path=None):
    now = format_utc_timestamp()
    with database.write_transaction(path, operation="complete_bulk_item") as connection:
        _release_item_notifications(connection, job, item, now)
        connection.execute(
            """UPDATE bulk_transfer_items SET stage='completed', error_code=NULL,
               completed_at=?, updated_at=? WHERE job_id=? AND ordinal=?""",
            (now, now, job["job_id"], item["ordinal"]),
        )


def _copy_error_stage(error):
    if error in {
        "destination_exists", "destination_panel_not_supported", "source_panel_not_supported",
        "source_auth_missing", "source_not_hysteria2", "source_password_missing",
        "blitz_unlimited_not_representable", "blitz_allowance_exhausted",
        "source_state_malformed", "inbounds_required", "inbounds_not_hysteria2",
    }:
        return "skipped"
    if error == "destination_create_outcome_unknown":
        return "manual_review"
    return "failed"


def _recover_item(job, item, multi_api, *, path=None):
    source = _exact_client(multi_api, job["source_server_id"])
    destination = _exact_client(multi_api, job["destination_server_id"])
    if source is None or destination is None:
        return False
    source_result = source.get_user_result(item["username"])
    destination_result = destination.get_user_result(item["username"])
    if item["stage"] == "copying":
        if destination_result.get("status") == "missing" and source_result.get("status") == "found":
            _set_item(job["job_id"], item["ordinal"], path=path, stage="pending", error_code=None)
            return True
        if destination_result.get("status") == "found" and source_result.get("status") == "found":
            matched, metadata = _interrupted_destination_match(
                job, source_result.get("data") or {}, destination_result.get("data") or {},
                _panel_type(destination), item["username"],
            )
            if matched:
                _set_item(
                    job["job_id"], item["ordinal"], path=path, stage="copied",
                    error_code=None, result_json=_dump(metadata),
                )
            else:
                _set_item(
                    job["job_id"], item["ordinal"], path=path, stage="manual_review",
                    error_code="interrupted_copy_ambiguous", completed_at=format_utc_timestamp(),
                )
            return True
        if destination_result.get("status") == "found":
            _set_item(
                job["job_id"], item["ordinal"], path=path, stage="manual_review",
                error_code="interrupted_copy_ambiguous", completed_at=format_utc_timestamp(),
            )
            return True
        return False
    if item["stage"] == "copied":
        if destination_result.get("status") != "found":
            _set_item(
                job["job_id"], item["ordinal"], path=path, stage="failed",
                error_code="destination_missing_after_copy", completed_at=format_utc_timestamp(),
            )
            return True
        if job["mode"] == "copy":
            _complete_item(job, item, path=path)
            return True
        try:
            _rehome_records_and_hold_recipients(job, item, path=path)
        except Exception:
            LOGGER.exception("Bulk transfer record update failed job=%s ordinal=%s", job["job_id"], item["ordinal"])
            if _rollback_destination(multi_api, job, item):
                _set_item(
                    job["job_id"], item["ordinal"], path=path, stage="failed",
                    error_code="record_update_failed", completed_at=format_utc_timestamp(),
                )
            else:
                _set_item(
                    job["job_id"], item["ordinal"], path=path, stage="manual_review",
                    error_code="record_update_rollback_failed", completed_at=format_utc_timestamp(),
                )
            return True
        return True
    if item["stage"] in {"records_updated", "source_delete_pending"}:
        if destination_result.get("status") != "found":
            _set_item(
                job["job_id"], item["ordinal"], path=path, stage="manual_review",
                error_code="destination_missing_after_records_updated", completed_at=format_utc_timestamp(),
            )
            return True
        if source_result.get("status") == "missing":
            _complete_item(job, item, path=path)
            return True
        if source_result.get("status") != "found":
            return False
        attempts = int(item.get("delete_attempts") or 0) + 1
        _set_item(
            job["job_id"], item["ordinal"], path=path,
            stage="source_delete_pending", delete_attempts=attempts,
        )
        source.delete_user(item["username"])
        verification = source.get_user_result(item["username"])
        if verification.get("status") == "missing":
            _complete_item(job, item, path=path)
        elif attempts >= MAX_SOURCE_DELETE_ATTEMPTS:
            _set_item(
                job["job_id"], item["ordinal"], path=path, stage="manual_review",
                error_code="source_delete_failed", completed_at=format_utc_timestamp(),
            )
        return True
    return False


def _process_item(job, item, multi_api, *, path=None):
    if item["stage"] != "pending":
        return _recover_item(job, item, multi_api, path=path)
    source = _exact_client(multi_api, job["source_server_id"])
    destination = _exact_client(multi_api, job["destination_server_id"])
    if source is None or destination is None:
        _set_item(
            job["job_id"], item["ordinal"], path=path, stage="failed",
            error_code="server_not_configured", completed_at=format_utc_timestamp(),
        )
        return True
    source_lookup = source.get_user_result(item["username"])
    if source_lookup.get("status") != "found":
        stage = "skipped" if source_lookup.get("status") == "missing" else "failed"
        _set_item(
            job["job_id"], item["ordinal"], path=path, stage=stage,
            error_code=f"source_{source_lookup.get('status', 'unavailable')}",
            completed_at=format_utc_timestamp(),
        )
        return True
    destination_lookup = destination.get_user_result(item["username"])
    if destination_lookup.get("status") == "found":
        _set_item(
            job["job_id"], item["ordinal"], path=path, stage="skipped",
            error_code="destination_exists", completed_at=format_utc_timestamp(),
        )
        return True
    if destination_lookup.get("status") != "missing":
        _set_item(
            job["job_id"], item["ordinal"], path=path, stage="failed",
            error_code="destination_unavailable", completed_at=format_utc_timestamp(),
        )
        return True

    attempts = int(item.get("copy_attempts") or 0) + 1
    _set_item(
        job["job_id"], item["ordinal"], path=path,
        stage="copying", copy_attempts=attempts, error_code=None,
    )
    result = multi_api.copy_user(UserCopySpec(
        source=UserRef(
            server_id=job["source_server_id"], username=item["username"],
            panel_type=item.get("source_panel_type") or _panel_type(source),
        ),
        destination_server_id=job["destination_server_id"],
        inbound_ids=tuple(job.get("inbound_ids") or ()),
    ))
    if not result.get("ok"):
        error = str(result.get("error") or "copy_failed")
        stage = "manual_review" if result.get("rollback_failed") else _copy_error_stage(error)
        _set_item(
            job["job_id"], item["ordinal"], path=path, stage=stage,
            error_code=error, result_json=_dump({"rollback_failed": bool(result.get("rollback_failed"))}),
            completed_at=format_utc_timestamp(),
        )
        return True
    _set_item(
        job["job_id"], item["ordinal"], path=path, stage="copied",
        result_json=_dump(_sanitize_copy_result(result)), error_code=None,
    )
    refreshed = dict(item)
    refreshed["stage"] = "copied"
    refreshed["result_json"] = _dump(_sanitize_copy_result(result))
    return _recover_item(job, refreshed, multi_api, path=path)


def _finish_job(job_id, *, path=None):
    counts = job_counts(job_id, path=path)
    remaining = counts.get("remaining", 0)
    if remaining:
        return False
    status = "completed_with_review" if counts.get("manual_review", 0) else "completed"
    now = format_utc_timestamp()
    with database.write_transaction(path, operation="complete_bulk_transfer") as connection:
        connection.execute(
            """UPDATE bulk_transfer_jobs SET status=?, completed_at=?, updated_at=?
               WHERE job_id=? AND status='running'""",
            (status, now, now, str(job_id)),
        )
    return True


def run_transfer_job(job_id, *, multi_api=None, path=None, progress_callback=None):
    job = get_job(job_id, path=path)
    if job is None or job["status"] not in ACTIVE_JOB_STATUSES:
        return False
    now = format_utc_timestamp()
    with database.write_transaction(path, operation="claim_bulk_transfer") as connection:
        connection.execute(
            """UPDATE bulk_transfer_jobs SET status='running',
               started_at=COALESCE(started_at, ?), updated_at=?
               WHERE job_id=? AND status IN ('queued','running')""",
            (now, now, str(job_id)),
        )
    multi_api = multi_api or MultiServerAPI()
    last_progress = 0.0
    callback = progress_callback or _progress_callback
    while True:
        job = get_job(job_id, path=path)
        if job is None:
            return False
        if job["status"] == "cancel_requested":
            with database.write_transaction(path, operation="stop_bulk_transfer") as connection:
                connection.execute(
                    "UPDATE bulk_transfer_jobs SET status='cancelled', completed_at=?, updated_at=? WHERE job_id=?",
                    (format_utc_timestamp(), format_utc_timestamp(), str(job_id)),
                )
            if callable(callback):
                callback(job_id)
            return True
        item_row = database.get_connection(path).execute(
            """SELECT * FROM bulk_transfer_items WHERE job_id=?
               AND stage NOT IN ('completed','skipped','failed','manual_review')
               ORDER BY ordinal LIMIT 1""",
            (str(job_id),),
        ).fetchone()
        if item_row is None:
            _finish_job(job_id, path=path)
            if callable(callback):
                callback(job_id)
            return True
        item = dict(item_row)
        try:
            progressed = _process_item(job, item, multi_api, path=path)
        except Exception as error:
            LOGGER.exception("Bulk transfer item crashed job=%s ordinal=%s", job_id, item["ordinal"])
            _set_item(
                job_id, item["ordinal"], path=path, stage="failed",
                error_code=f"internal_{type(error).__name__}", completed_at=format_utc_timestamp(),
            )
            progressed = True
        if callable(callback) and (time.monotonic() - last_progress >= PROGRESS_INTERVAL_SECONDS):
            callback(job_id)
            last_progress = time.monotonic()
        if not progressed:
            # A panel is unavailable.  Keep the item resumable and stop this
            # run instead of spinning or changing ownership assumptions.
            with database.write_transaction(path, operation="pause_bulk_transfer") as connection:
                connection.execute(
                    """UPDATE bulk_transfer_jobs SET status='failed', last_error=?,
                       completed_at=?, updated_at=? WHERE job_id=?""",
                    ("panel_unavailable_during_recovery", format_utc_timestamp(), format_utc_timestamp(), job_id),
                )
            if callable(callback):
                callback(job_id)
            return False


def _worker_main(path):
    global _worker_thread
    try:
        while True:
            active = get_active_job(path=path)
            if active is None:
                return
            run_transfer_job(active["job_id"], path=path)
    finally:
        with _worker_lock:
            _worker_thread = None


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def start_transfer_worker(*, path=None):
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return _worker_thread
        if get_active_job(path=path) is None:
            return None
        _worker_thread = threading.Thread(
            target=_worker_main, args=(path,), daemon=True, name="ajib-bulk-transfer"
        )
        _worker_thread.start()
        return _worker_thread


def decide_deferred_notifications(job_id, admin_id, decision, *, path=None):
    if decision not in {"send", "discard"}:
        return False
    now = format_utc_timestamp()
    with database.write_transaction(path, operation="decide_bulk_notifications") as connection:
        job = connection.execute(
            "SELECT * FROM bulk_transfer_jobs WHERE job_id=? AND requested_by=?",
            (str(job_id), str(admin_id)),
        ).fetchone()
        if (
            job is None
            or job["notification_policy"] != "deferred"
            or job["status"] in ACTIVE_JOB_STATUSES
            or connection.execute(
                """SELECT 1 FROM bulk_transfer_items WHERE job_id=?
                   AND stage NOT IN ('completed','skipped','failed','manual_review') LIMIT 1""",
                (str(job_id),),
            ).fetchone() is not None
        ):
            return False
        target_status = "pending" if decision == "send" else "discarded"
        connection.execute(
            """UPDATE bulk_transfer_notifications SET status=?, updated_at=?
               WHERE job_id=? AND status='held'""",
            (target_status, now, str(job_id)),
        )
        connection.execute(
            "UPDATE bulk_transfer_jobs SET notification_decided_at=?, updated_at=? WHERE job_id=?",
            (now, now, str(job_id)),
        )
    return True


def deferred_recipient_preview(job_id, *, path=None, limit=20):
    connection = database.get_connection(path)
    rows = connection.execute(
        """SELECT route_scope, recipient_id, COUNT(*) AS accounts
           FROM bulk_transfer_notifications WHERE job_id=? AND status='held'
           GROUP BY route_scope, recipient_id ORDER BY route_scope, recipient_id LIMIT ?""",
        (str(job_id), int(limit)),
    ).fetchall()
    total = int(connection.execute(
        """SELECT COUNT(*) FROM (SELECT 1 FROM bulk_transfer_notifications
           WHERE job_id=? AND status='held' GROUP BY route_scope, recipient_id)""",
        (str(job_id),),
    ).fetchone()[0])
    return {"total": total, "recipients": [dict(row) for row in rows]}


def _claim_notification(route_scope, *, path=None):
    now = format_utc_timestamp()
    with database.write_transaction(path, operation="claim_bulk_notification") as connection:
        row = connection.execute(
            """SELECT n.*, j.destination_server_id, i.result_json
               FROM bulk_transfer_notifications n
               JOIN bulk_transfer_jobs j ON j.job_id=n.job_id
               JOIN bulk_transfer_items i
                 ON i.job_id=n.job_id AND i.ordinal=n.item_ordinal
               WHERE n.route_scope=? AND n.status='pending'
               AND (n.next_attempt_at IS NULL OR n.next_attempt_at<=?)
               ORDER BY n.notification_id LIMIT 1""",
            (str(route_scope), now),
        ).fetchone()
        if row is None:
            return None
        updated = connection.execute(
            """UPDATE bulk_transfer_notifications SET status='sending', updated_at=?
               WHERE notification_id=? AND status='pending'""",
            (now, row["notification_id"]),
        )
        return dict(row) if updated.rowcount else None


def _delivery_language(language_resolver, recipient_id):
    if language_resolver is None:
        return DEFAULT_LANGUAGE
    try:
        language = language_resolver(recipient_id)
    except Exception:
        return DEFAULT_LANGUAGE
    if not isinstance(language, str) or not language.strip():
        return DEFAULT_LANGUAGE
    return language.strip().lower()


def _notification_text(notification, uri_data, language_code=DEFAULT_LANGUAGE):
    return get_message_text(language_code, "migration_connection_updated").format(
        username=notification["username"],
        link=uri_data["normal_sub"],
    )


def _finish_notification(notification, success, error=None, *, path=None):
    attempts = int(notification.get("attempt_count") or 0) + 1
    now = utc_now()
    if success:
        status = "sent"
        next_attempt = None
        sent_at = format_utc_timestamp(now)
    elif attempts >= MAX_NOTIFICATION_ATTEMPTS:
        status = "permanent_failed"
        next_attempt = None
        sent_at = None
    else:
        status = "pending"
        next_attempt = format_utc_timestamp(now + timedelta(seconds=min(3600, 30 * (2 ** (attempts - 1)))))
        sent_at = None
    with database.write_transaction(path, operation="finish_bulk_notification") as connection:
        connection.execute(
            """UPDATE bulk_transfer_notifications SET status=?, attempt_count=?,
               next_attempt_at=?, last_error=?, sent_at=?, updated_at=?
               WHERE notification_id=?""",
            (
                status, attempts, next_attempt, str(error or "")[:300] or None,
                sent_at, format_utc_timestamp(now), notification["notification_id"],
            ),
        )


def deliver_notifications(
    route_scope,
    sender,
    *,
    path=None,
    max_items=20,
    multi_api=None,
    language_resolver=None,
):
    """Deliver due notices for one bot scope.

    ``sender`` receives ``(recipient_id, text)`` and may raise on Telegram
    failures. ``language_resolver`` receives the recipient ID at delivery time;
    missing or failed lookups fall back to English. Destination links are always
    fetched immediately before send.
    """
    delivered = 0
    multi_api = multi_api or MultiServerAPI()
    for _ in range(max(0, int(max_items))):
        notification = _claim_notification(route_scope, path=path)
        if notification is None:
            break
        destination = _exact_client(multi_api, notification["destination_server_id"])
        if destination is None:
            _finish_notification(notification, False, "destination_not_configured", path=path)
            continue
        uri_data = destination.get_user_uri(notification["username"])
        if not isinstance(uri_data, dict) or not uri_data.get("normal_sub"):
            _finish_notification(notification, False, "destination_uri_unavailable", path=path)
            continue
        try:
            recipient_id = int(notification["recipient_id"])
            language = _delivery_language(language_resolver, recipient_id)
            sender(recipient_id, _notification_text(notification, uri_data, language))
        except Exception as error:
            _finish_notification(notification, False, type(error).__name__, path=path)
        else:
            _finish_notification(notification, True, path=path)
            delivered += 1
    return delivered


def recover_stale_notification_claims(*, path=None, older_than_seconds=300):
    cutoff = format_utc_timestamp(utc_now() - timedelta(seconds=max(1, int(older_than_seconds))))
    with database.write_transaction(path, operation="recover_bulk_notifications") as connection:
        result = connection.execute(
            """UPDATE bulk_transfer_notifications SET status='pending', updated_at=?
               WHERE status='sending' AND updated_at<?""",
            (format_utc_timestamp(), cutoff),
        )
    return result.rowcount


def export_job_csv(job_id, *, path=None):
    job = get_job(job_id, path=path)
    if job is None:
        return None
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow((
        "ordinal", "username", "stage", "error_code", "source_panel",
        "destination_panel", "records_updated", "recipient_count",
        "expiry_extension_seconds",
    ))
    for row in database.get_connection(path).execute(
        "SELECT * FROM bulk_transfer_items WHERE job_id=? ORDER BY ordinal", (str(job_id),)
    ):
        metadata = _load(row["result_json"], {})
        writer.writerow((
            row["ordinal"], row["username"], row["stage"], row["error_code"] or "",
            row["source_panel_type"] or "", metadata.get("panel_type") or "",
            row["records_updated"], row["recipient_count"],
            metadata.get("expiry_extension_seconds") or 0,
        ))
    return output.getvalue().encode("utf-8-sig")
