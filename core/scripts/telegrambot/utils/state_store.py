"""Compatibility repositories for state historically stored as JSON files.

Runtime callers keep their established dict/list APIs while state is persisted
as scoped rows in SQLite. Legacy filesystem access is intentionally handled by
the migration module, not by these repositories.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from . import database
from .time_utils import format_utc_timestamp


STATIC_JSON_FILES = {"plans.json", "support_info.json"}
TOP_LEVEL_STATE = {
    "payments.json": ("payments", "main"),
    "resellers.json": ("resellers", "main"),
    "hosted_bots.json": ("hosted_registry", "main"),
    "hosted_bot_tokens.json": ("hosted_secrets", "main"),
    "referrals.json": ("referrals", "main"),
    "checker_settlements.json": ("checker_settlements", "main"),
    "user_languages.json": ("kv_dict", "user_languages"),
    "test_configs.json": ("kv_dict", "test_configs"),
    "test_settings.json": ("kv_dict", "test_settings"),
    "waiting_test_users.json": ("kv_dict", "test_waiting"),
    "traffic_alerts.json": ("kv_dict", "traffic_alerts"),
    "expired_user_cleanup.json": ("kv_dict", "expired_cleanup"),
    "expired_cleanup_schedule.json": ("kv_dict", "expired_cleanup_schedule"),
    "broadcast_failed_users.json": ("kv_list", "broadcast_failed_users"),
}
HOSTED_STATE = {
    "payments.json": "payments",
    "settings.json": "hosted_settings",
    "ledger.json": "ledger",
    "referrals.json": "referrals",
    "languages.json": "kv_dict",
    "renewal_tokens.json": "kv_dict",
    "notifications.json": "kv_dict",
}
HOSTED_NAMESPACES = {
    "languages.json": "hosted_languages",
    "renewal_tokens.json": "hosted_renewal_tokens",
    "notifications.json": "hosted_notifications",
}


@dataclass(frozen=True)
class StateDescriptor:
    kind: str
    scope: str
    namespace: str = ""


def _is_within(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        return False


def describe_path(
    path: str | os.PathLike[str],
    *,
    legacy_root: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> StateDescriptor | None:
    if not force and os.getenv("AJIB_SQLITE_ACTIVE") != "1":
        return None
    candidate = os.path.abspath(os.fspath(path))
    active_root = os.path.abspath(os.fspath(legacy_root)) if legacy_root else database.bot_dir()
    roots = [active_root]
    # Tests and administrative tools can point the DB elsewhere while existing
    # runtime constants still carry the installed /etc path.
    if os.getenv("AJIB_DB_PATH"):
        roots.append(os.path.abspath(database.DEFAULT_BOT_DIR))
    matched_root = next((root for root in roots if _is_within(candidate, root)), None)
    if matched_root is None and not force:
        return None
    root = matched_root or active_root
    relative = Path(os.path.relpath(candidate, root))
    if ".." in relative.parts or relative.is_absolute():
        return None
    if len(relative.parts) == 1:
        name = relative.name
        if name in STATIC_JSON_FILES:
            return None
        state = TOP_LEVEL_STATE.get(name)
        if state is None:
            return None
        kind, value = state
        if kind.startswith("kv_"):
            return StateDescriptor(kind, "main", value)
        return StateDescriptor(kind, value)
    if (
        len(relative.parts) >= 3
        and relative.parts[0] == "hosted_bots"
        and relative.parts[1].isdigit()
        and len(relative.parts) == 3
    ):
        reseller_id = relative.parts[1]
        name = relative.parts[2]
        kind = HOSTED_STATE.get(name)
        if kind is None:
            return None
        scope = f"hosted:{reseller_id}"
        if kind == "kv_dict":
            return StateDescriptor(kind, scope, HOSTED_NAMESPACES[name])
        return StateDescriptor(kind, scope)
    return None


def is_managed_path(path: str | os.PathLike[str]) -> bool:
    return describe_path(path) is not None


def _dump(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _load(raw: str):
    return json.loads(raw)


def _copy_default(default):
    return deepcopy({} if default is None else default)


def _money_cents(value, default=0) -> int:
    try:
        amount = Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Invalid monetary amount: {value!r}") from error
    if not amount.is_finite():
        raise ValueError(f"Invalid monetary amount: {value!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money_float(cents) -> float:
    return float((Decimal(int(cents or 0)) / Decimal(100)).quantize(Decimal("0.01")))


def _optional_cents(value):
    if value is None or value == "":
        return None
    try:
        return _money_cents(value)
    except ValueError:
        return None


def _hosted_reseller_id(scope: str) -> str:
    if not scope.startswith("hosted:"):
        raise ValueError(f"Invalid hosted state scope: {scope}")
    reseller_id = scope.split(":", 1)[1]
    if not reseller_id.isdigit():
        raise ValueError(f"Invalid hosted reseller ID: {reseller_id}")
    return reseller_id


def _normalize_receipt_path(record: dict) -> dict:
    value = record.get("receipt_path")
    if not isinstance(value, str) or not value:
        return record
    root = os.path.abspath(database.bot_dir())
    candidate = (
        os.path.abspath(value)
        if os.path.isabs(value)
        else os.path.abspath(os.path.join(root, value))
    )
    if not _is_within(candidate, root):
        raise ValueError(f"Receipt path escapes the bot state directory: {value}")
    record["receipt_path"] = os.path.relpath(candidate, root)
    return record


def _resolve_receipt_path(record: dict) -> dict:
    value = record.get("receipt_path")
    if isinstance(value, str) and value and not os.path.isabs(value):
        candidate = os.path.abspath(os.path.join(database.bot_dir(), value))
        if _is_within(candidate, database.bot_dir()):
            record["receipt_path"] = candidate
    return record


def _load_payments(connection, scope):
    result = {}
    rows = connection.execute(
        """
        SELECT payment_id, status, amount_cents, payload_json
        FROM payments WHERE scope=? ORDER BY rowid
        """,
        (scope,),
    ).fetchall()
    for row in rows:
        payload = _load(row["payload_json"])
        if isinstance(payload, dict):
            if row["status"] is not None:
                payload["status"] = row["status"]
            if row["amount_cents"] is not None:
                payload["price"] = _money_float(row["amount_cents"])
            result[row["payment_id"]] = _resolve_receipt_path(payload)
    return result


def _save_payments(connection, scope, data):
    if not isinstance(data, dict):
        raise ValueError("Payment database must contain a JSON object.")
    keys = {str(key) for key in data}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        connection.execute(
            f"DELETE FROM payments WHERE scope=? AND payment_id NOT IN ({placeholders})",
            (scope, *sorted(keys)),
        )
    else:
        connection.execute("DELETE FROM payments WHERE scope=?", (scope,))
    for payment_id, raw_record in data.items():
        if not isinstance(raw_record, dict):
            raise ValueError(f"Payment record {payment_id!r} must contain a JSON object.")
        record = _normalize_receipt_path(deepcopy(raw_record))
        payment_key = str(payment_id)
        connection.execute(
            """
            INSERT INTO payments(
                scope, payment_id, user_id, status, kind, payment_method,
                amount_cents, currency, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, payment_id) DO UPDATE SET
                user_id=excluded.user_id,
                status=excluded.status,
                kind=excluded.kind,
                payment_method=excluded.payment_method,
                amount_cents=excluded.amount_cents,
                currency=excluded.currency,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                scope,
                payment_key,
                str(record.get("user_id")) if record.get("user_id") is not None else None,
                str(record.get("status")) if record.get("status") is not None else None,
                str(record.get("type")) if record.get("type") is not None else None,
                str(record.get("payment_method")) if record.get("payment_method") is not None else None,
                _optional_cents(record.get("price")),
                str(record.get("currency") or "USD"),
                record.get("created_at"),
                record.get("updated_at"),
                _dump(record),
            ),
        )
        connection.execute(
            "DELETE FROM payment_events WHERE scope=? AND payment_id=?",
            (scope, payment_key),
        )
        updates = record.get("updates", [])
        if not isinstance(updates, list):
            updates = []
        for sequence, event in enumerate(updates):
            if not isinstance(event, dict):
                continue
            connection.execute(
                """
                INSERT INTO payment_events(
                    scope, payment_id, sequence, status, previous_status,
                    occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    payment_key,
                    sequence,
                    event.get("status"),
                    event.get("previous_status"),
                    event.get("timestamp"),
                    _dump(event),
                ),
            )


def _load_resellers(connection):
    result = {}
    for row in connection.execute(
        """
        SELECT reseller_id, debt_cents, total_paid_cents, payload_json
        FROM resellers ORDER BY reseller_id
        """
    ):
        record = _load(row["payload_json"])
        if not isinstance(record, dict):
            continue
        record["debt"] = _money_float(row["debt_cents"])
        record["total_paid"] = _money_float(row["total_paid_cents"])
        configs = record.get("configs", [])
        if isinstance(configs, list):
            for config_row in connection.execute(
                """
                SELECT config_index, price_cents FROM reseller_configs
                WHERE reseller_id=? ORDER BY config_index
                """,
                (row["reseller_id"],),
            ):
                index = int(config_row["config_index"])
                if (
                    index < len(configs)
                    and isinstance(configs[index], dict)
                    and config_row["price_cents"] is not None
                ):
                    configs[index]["price"] = _money_float(config_row["price_cents"])
            for renewal_row in connection.execute(
                """
                SELECT config_index, renewal_index, price_cents
                FROM reseller_renewals
                WHERE reseller_id=?
                ORDER BY config_index, renewal_index
                """,
                (row["reseller_id"],),
            ):
                config_index = int(renewal_row["config_index"])
                renewal_index = int(renewal_row["renewal_index"])
                if config_index >= len(configs) or not isinstance(configs[config_index], dict):
                    continue
                renewals = configs[config_index].get("renewals", [])
                if (
                    isinstance(renewals, list)
                    and renewal_index < len(renewals)
                    and isinstance(renewals[renewal_index], dict)
                    and renewal_row["price_cents"] is not None
                ):
                    renewals[renewal_index]["price"] = _money_float(
                        renewal_row["price_cents"]
                    )
        result[row["reseller_id"]] = record
    return result


def _save_resellers(connection, data):
    if not isinstance(data, dict):
        raise ValueError("Reseller database must contain a JSON object.")
    keys = {str(key) for key in data}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        connection.execute(
            f"DELETE FROM resellers WHERE reseller_id NOT IN ({placeholders})",
            tuple(sorted(keys)),
        )
    else:
        connection.execute("DELETE FROM resellers")
    for reseller_id, raw_record in data.items():
        if not isinstance(raw_record, dict):
            raise ValueError(f"Reseller record {reseller_id!r} must contain a JSON object.")
        key = str(reseller_id)
        record = deepcopy(raw_record)
        connection.execute(
            """
            INSERT INTO resellers(
                reseller_id, status, debt_cents, total_paid_cents, debt_since,
                telegram_username, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reseller_id) DO UPDATE SET
                status=excluded.status,
                debt_cents=excluded.debt_cents,
                total_paid_cents=excluded.total_paid_cents,
                debt_since=excluded.debt_since,
                telegram_username=excluded.telegram_username,
                payload_json=excluded.payload_json
            """,
            (
                key,
                record.get("status"),
                _money_cents(record.get("debt", 0)),
                _money_cents(record.get("total_paid", 0)),
                record.get("debt_since"),
                record.get("telegram_username"),
                _dump(record),
            ),
        )
        connection.execute("DELETE FROM reseller_configs WHERE reseller_id=?", (key,))
        configs = record.get("configs", [])
        if not isinstance(configs, list):
            raise ValueError(f"Reseller {key!r} configs must contain a JSON list.")
        seen_orders = set()
        for config_index, raw_config in enumerate(configs):
            if not isinstance(raw_config, dict):
                raise ValueError(f"Reseller {key!r} config {config_index} must be an object.")
            config = deepcopy(raw_config)
            order_id = str(config.get("retail_order_id") or "")
            if order_id:
                if order_id in seen_orders:
                    raise ValueError(f"Duplicate reseller order ID {order_id!r} for {key}.")
                seen_orders.add(order_id)
            connection.execute(
                """
                INSERT INTO reseller_configs(
                    reseller_id, config_index, username, server_id,
                    retail_order_id, price_cents, created_at, cleanup_status,
                    removed, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    config_index,
                    config.get("username"),
                    str(config.get("server_id")) if config.get("server_id") is not None else None,
                    order_id or None,
                    _optional_cents(config.get("price")),
                    config.get("timestamp"),
                    config.get("cleanup_status"),
                    int(bool(config.get("removed_from_vpn"))),
                    _dump(config),
                ),
            )
            renewals = config.get("renewals", [])
            if renewals is None:
                renewals = []
            if not isinstance(renewals, list):
                raise ValueError(
                    f"Reseller {key!r} config {config_index} renewals must be a list."
                )
            for renewal_index, raw_renewal in enumerate(renewals):
                if not isinstance(raw_renewal, dict):
                    raise ValueError(
                        f"Reseller {key!r} renewal {config_index}:{renewal_index} must be an object."
                    )
                renewal = deepcopy(raw_renewal)
                renewal_order = str(renewal.get("retail_order_id") or "")
                if renewal_order:
                    if renewal_order in seen_orders:
                        raise ValueError(
                            f"Duplicate reseller order ID {renewal_order!r} for {key}."
                        )
                    seen_orders.add(renewal_order)
                connection.execute(
                    """
                    INSERT INTO reseller_renewals(
                        reseller_id, config_index, renewal_index,
                        retail_order_id, price_cents, created_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        config_index,
                        renewal_index,
                        renewal_order or None,
                        _optional_cents(renewal.get("price")),
                        renewal.get("timestamp"),
                        _dump(renewal),
                    ),
                )


def _load_hosted_registry(connection):
    result = {}
    rows = connection.execute(
        """
        SELECT reseller_id, payload_json
        FROM hosted_bots
        WHERE bot_id IS NOT NULL OR payload_json != '{}'
        ORDER BY reseller_id
        """
    )
    for row in rows:
        payload = _load(row["payload_json"])
        if isinstance(payload, dict) and payload:
            result[row["reseller_id"]] = payload
    return result


def _save_hosted_registry(connection, data):
    if not isinstance(data, dict):
        raise ValueError("Hosted bot registry must contain a JSON object.")
    keys = {str(key) for key in data}
    if keys:
        placeholders = ",".join("?" for _ in keys)
        connection.execute(
            f"DELETE FROM hosted_bots WHERE reseller_id NOT IN ({placeholders})",
            tuple(sorted(keys)),
        )
    else:
        connection.execute("DELETE FROM hosted_bots")
    for reseller_id, raw_record in data.items():
        if not isinstance(raw_record, dict):
            raise ValueError(f"Hosted bot record {reseller_id!r} must be an object.")
        key = str(reseller_id)
        record = deepcopy(raw_record)
        connection.execute(
            """
            INSERT INTO hosted_bots(
                reseller_id, bot_id, username, token_fingerprint, status,
                enabled, created_at, updated_at, started_at, last_error,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(reseller_id) DO UPDATE SET
                bot_id=excluded.bot_id,
                username=excluded.username,
                token_fingerprint=excluded.token_fingerprint,
                status=excluded.status,
                enabled=excluded.enabled,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                started_at=excluded.started_at,
                last_error=excluded.last_error,
                payload_json=excluded.payload_json
            """,
            (
                key,
                str(record.get("bot_id")) if record.get("bot_id") is not None else None,
                record.get("username"),
                record.get("token_fingerprint"),
                record.get("status"),
                int(bool(record.get("enabled", True))),
                record.get("created_at"),
                record.get("updated_at"),
                record.get("started_at"),
                record.get("last_error"),
                _dump(record),
            ),
        )


def _load_hosted_secrets(connection):
    return {
        row["reseller_id"]: row["token"]
        for row in connection.execute(
            "SELECT reseller_id, token FROM hosted_bots WHERE token IS NOT NULL"
        )
    }


def _save_hosted_secrets(connection, data):
    if not isinstance(data, dict):
        raise ValueError("Hosted bot token store must contain a JSON object.")
    connection.execute("UPDATE hosted_bots SET token=NULL")
    for reseller_id, token in data.items():
        key = str(reseller_id)
        if not isinstance(token, str):
            raise ValueError(f"Hosted bot token for {key!r} must be a string.")
        connection.execute(
            """
            INSERT INTO hosted_bots(reseller_id, token, payload_json)
            VALUES (?, ?, '{}')
            ON CONFLICT(reseller_id) DO UPDATE SET token=excluded.token
            """,
            (key, token),
        )


def _load_hosted_settings(connection, scope):
    reseller_id = _hosted_reseller_id(scope)
    row = connection.execute(
        "SELECT payload_json FROM hosted_settings WHERE reseller_id=?",
        (reseller_id,),
    ).fetchone()
    return _load(row["payload_json"]) if row else {}


def _save_hosted_settings(connection, scope, data):
    if not isinstance(data, dict):
        raise ValueError("Hosted bot settings must contain a JSON object.")
    reseller_id = _hosted_reseller_id(scope)
    connection.execute(
        """
        INSERT INTO hosted_settings(reseller_id, payload_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(reseller_id) DO UPDATE SET
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        (reseller_id, _dump(data), data.get("updated_at")),
    )


def _default_ledger():
    return {
        "earnings_available": 0.0,
        "earnings_reserved": 0.0,
        "credit_reservations": {},
        "withdrawals": [],
        "transactions": [],
        "referral_liability": 0.0,
    }


def _load_ledger(connection, scope):
    reseller_id = _hosted_reseller_id(scope)
    row = connection.execute(
        "SELECT * FROM ledger_accounts WHERE reseller_id=?",
        (reseller_id,),
    ).fetchone()
    if row is None:
        return _default_ledger()
    payload = _load(row["payload_json"])
    ledger = payload if isinstance(payload, dict) else {}
    ledger.update(
        {
            "earnings_available": _money_float(row["earnings_available_cents"]),
            "earnings_reserved": _money_float(row["earnings_reserved_cents"]),
            "referral_liability": _money_float(row["referral_liability_cents"]),
        }
    )
    ledger["transactions"] = []
    for item in connection.execute(
        """
        SELECT * FROM ledger_transactions
        WHERE reseller_id=? ORDER BY sequence
        """,
        (reseller_id,),
    ):
        transaction_payload = _load(item["payload_json"])
        transaction = (
            transaction_payload if isinstance(transaction_payload, dict) else {}
        )
        transaction.update(
            {
                "id": item["transaction_id"],
                "type": item["kind"],
                "amount": _money_float(item["amount_cents"]),
                "metadata": _load(item["metadata_json"]),
                "created_at": item["created_at"],
            }
        )
        ledger["transactions"].append(transaction)
    ledger["credit_reservations"] = {
        item["reservation_id"]: {
            **_load(item["payload_json"]),
            "amount": _money_float(item["amount_cents"]),
            "created_at": item["created_at"],
        }
        for item in connection.execute(
            "SELECT * FROM credit_reservations WHERE reseller_id=? ORDER BY rowid",
            (reseller_id,),
        )
    }
    ledger["withdrawals"] = [
        {
            **_load(item["payload_json"]),
            "id": item["withdrawal_id"],
            "status": item["status"],
            "amount": _money_float(item["amount_cents"]),
            "destination": item["destination"],
            "requested_at": item["requested_at"],
            "resolved_at": item["resolved_at"],
            "admin_id": item["admin_id"],
        }
        for item in connection.execute(
            "SELECT * FROM withdrawals WHERE reseller_id=? ORDER BY sequence",
            (reseller_id,),
        )
    ]
    return ledger


def _save_ledger(connection, scope, data):
    if not isinstance(data, dict):
        raise ValueError("Hosted ledger must contain a JSON object.")
    reseller_id = _hosted_reseller_id(scope)
    known = {
        "earnings_available",
        "earnings_reserved",
        "credit_reservations",
        "withdrawals",
        "transactions",
        "referral_liability",
    }
    extras = {key: value for key, value in data.items() if key not in known}
    connection.execute(
        """
        INSERT INTO ledger_accounts(
            reseller_id, earnings_available_cents, earnings_reserved_cents,
            referral_liability_cents, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(reseller_id) DO UPDATE SET
            earnings_available_cents=excluded.earnings_available_cents,
            earnings_reserved_cents=excluded.earnings_reserved_cents,
            referral_liability_cents=excluded.referral_liability_cents,
            payload_json=excluded.payload_json
        """,
        (
            reseller_id,
            _money_cents(data.get("earnings_available", 0)),
            _money_cents(data.get("earnings_reserved", 0)),
            _money_cents(data.get("referral_liability", 0)),
            _dump(extras),
        ),
    )
    connection.execute("DELETE FROM ledger_transactions WHERE reseller_id=?", (reseller_id,))
    transactions = data.get("transactions", [])
    if not isinstance(transactions, list):
        raise ValueError("Ledger transactions must contain a JSON list.")
    seen_transactions = set()
    for sequence, raw_item in enumerate(transactions):
        if not isinstance(raw_item, dict):
            raise ValueError("Ledger transaction must contain a JSON object.")
        item = deepcopy(raw_item)
        transaction_id = str(item.get("id") or "")
        if not transaction_id or transaction_id in seen_transactions:
            raise ValueError(f"Invalid or duplicate ledger transaction ID {transaction_id!r}.")
        seen_transactions.add(transaction_id)
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {"legacy_value": metadata}
        connection.execute(
            """
            INSERT INTO ledger_transactions(
                reseller_id, transaction_id, sequence, kind, amount_cents,
                metadata_json, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reseller_id,
                transaction_id,
                sequence,
                item.get("type"),
                _money_cents(item.get("amount", 0)),
                _dump(metadata),
                _dump(item),
                item.get("created_at"),
            ),
        )
    connection.execute("DELETE FROM credit_reservations WHERE reseller_id=?", (reseller_id,))
    reservations = data.get("credit_reservations", {})
    if not isinstance(reservations, dict):
        raise ValueError("Credit reservations must contain a JSON object.")
    for reservation_id, raw_item in reservations.items():
        if not isinstance(raw_item, dict):
            raise ValueError("Credit reservation must contain a JSON object.")
        item = deepcopy(raw_item)
        connection.execute(
            """
            INSERT INTO credit_reservations(
                reseller_id, reservation_id, amount_cents, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                reseller_id,
                str(reservation_id),
                _money_cents(item.get("amount", 0)),
                item.get("created_at"),
                _dump(item),
            ),
        )
    connection.execute("DELETE FROM withdrawals WHERE reseller_id=?", (reseller_id,))
    withdrawals = data.get("withdrawals", [])
    if not isinstance(withdrawals, list):
        raise ValueError("Ledger withdrawals must contain a JSON list.")
    seen_withdrawals = set()
    for sequence, raw_item in enumerate(withdrawals):
        if not isinstance(raw_item, dict):
            raise ValueError("Ledger withdrawal must contain a JSON object.")
        item = deepcopy(raw_item)
        withdrawal_id = str(item.get("id") or "")
        if not withdrawal_id or withdrawal_id in seen_withdrawals:
            raise ValueError(f"Invalid or duplicate withdrawal ID {withdrawal_id!r}.")
        seen_withdrawals.add(withdrawal_id)
        connection.execute(
            """
            INSERT INTO withdrawals(
                reseller_id, withdrawal_id, sequence, status, amount_cents,
                destination, requested_at, resolved_at, admin_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reseller_id,
                withdrawal_id,
                sequence,
                item.get("status"),
                _money_cents(item.get("amount", 0)),
                item.get("destination"),
                item.get("requested_at"),
                item.get("resolved_at"),
                str(item.get("admin_id")) if item.get("admin_id") is not None else None,
                _dump(item),
            ),
        )


def _load_referrals(connection, scope):
    row = connection.execute(
        "SELECT payload_json FROM referral_scopes WHERE scope=?",
        (scope,),
    ).fetchone()
    if row is None:
        return {}
    data = _load(row["payload_json"])
    if not isinstance(data, dict):
        return {}
    stats = data.get("stats", {})
    if isinstance(stats, dict):
        for account in connection.execute(
            """
            SELECT user_id, invited_count, total_earnings_cents,
                   available_balance_cents
            FROM referral_accounts WHERE scope=?
            """,
            (scope,),
        ):
            current = stats.get(account["user_id"])
            if not isinstance(current, dict):
                continue
            current["count"] = int(account["invited_count"])
            current["total_earnings"] = _money_float(
                account["total_earnings_cents"]
            )
            current["available_balance"] = _money_float(
                account["available_balance_cents"]
            )
    return data


def _save_referrals(connection, scope, data):
    if not isinstance(data, dict):
        raise ValueError("Referral database must contain a JSON object.")
    connection.execute(
        """
        INSERT INTO referral_scopes(scope, payload_json) VALUES (?, ?)
        ON CONFLICT(scope) DO UPDATE SET payload_json=excluded.payload_json
        """,
        (scope, _dump(data)),
    )
    for table in (
        "referral_accounts",
        "referral_links",
        "referral_rewards",
        "referral_withdrawals",
        "referral_payouts",
    ):
        connection.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
    stats = data.get("stats", {})
    codes = data.get("user_codes", {})
    wallets = data.get("wallets", {})
    code_to_user = data.get("codes", {})
    if not all(isinstance(value, dict) for value in (stats, codes, wallets, code_to_user)):
        raise ValueError("Referral stats, codes, and wallets must contain JSON objects.")
    account_ids = set(stats) | set(codes) | set(wallets) | {
        str(value) for value in code_to_user.values()
    }
    seen_codes = set()
    for user_id in sorted(account_ids, key=str):
        account = stats.get(user_id, {})
        if not isinstance(account, dict):
            raise ValueError(f"Referral stats for {user_id!r} must be an object.")
        code = codes.get(str(user_id), codes.get(user_id))
        if code is None:
            matches = [
                raw_code
                for raw_code, owner in code_to_user.items()
                if str(owner) == str(user_id)
            ]
            if len(matches) > 1:
                raise ValueError(f"Multiple referral codes map to user {user_id!r}.")
            code = matches[0] if matches else None
        if code is not None:
            code = str(code)
            if code in seen_codes:
                raise ValueError(f"Duplicate referral code {code!r} in {scope}.")
            seen_codes.add(code)
        connection.execute(
            """
            INSERT INTO referral_accounts(
                scope, user_id, code, invited_count, total_earnings_cents,
                available_balance_cents, wallet, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                str(user_id),
                code,
                int(account.get("count", 0) or 0),
                _money_cents(account.get("total_earnings", 0)),
                _money_cents(account.get("available_balance", 0)),
                wallets.get(str(user_id), wallets.get(user_id)),
                _dump(account),
            ),
        )
    links = data.get("referrals", {})
    details = data.get("referral_details", {})
    if not isinstance(links, dict) or not isinstance(details, dict):
        raise ValueError("Referral links and details must contain JSON objects.")
    for code, user_id in code_to_user.items():
        expected = codes.get(str(user_id), codes.get(user_id))
        if expected is not None and str(expected) != str(code):
            raise ValueError(f"Conflicting referral code mapping for {code!r}.")
    for invitee, referrer in links.items():
        detail = details.get(str(invitee), details.get(invitee, {}))
        if not isinstance(detail, dict):
            detail = {}
        connection.execute(
            """
            INSERT INTO referral_links(
                scope, invitee_user_id, referrer_user_id, code,
                invited_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                str(invitee),
                str(referrer),
                detail.get("referral_code"),
                detail.get("invited_at"),
                _dump(detail),
            ),
        )
    rewarded = data.get("rewarded_orders", {})
    if not isinstance(rewarded, dict):
        raise ValueError("Rewarded orders must contain a JSON object.")
    for order_id, raw_reward in rewarded.items():
        if isinstance(raw_reward, dict):
            amount = raw_reward.get("amount", 0)
            referrer = raw_reward.get("referrer_id")
            payload = raw_reward
        else:
            amount = raw_reward
            referrer = None
            payload = {"amount": raw_reward}
        connection.execute(
            """
            INSERT INTO referral_rewards(
                scope, order_id, referrer_user_id, amount_cents, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (scope, str(order_id), str(referrer) if referrer is not None else None, _money_cents(amount), _dump(payload)),
        )
    for table, source_name, id_name, time_name in (
        ("referral_withdrawals", "pending_withdrawals", "withdrawal_id", "requested_at"),
        ("referral_payouts", "payouts", "payout_id", "paid_at"),
    ):
        records = data.get(source_name, [])
        if not isinstance(records, list):
            raise ValueError(f"Referral {source_name} must contain a JSON list.")
        seen = set()
        for sequence, raw_item in enumerate(records):
            if not isinstance(raw_item, dict):
                raise ValueError(f"Referral {source_name} record must be an object.")
            item = deepcopy(raw_item)
            record_id = str(item.get("id") or f"legacy:{sequence}")
            if record_id in seen:
                raise ValueError(f"Duplicate referral record ID {record_id!r}.")
            seen.add(record_id)
            if table == "referral_withdrawals":
                connection.execute(
                    """
                    INSERT INTO referral_withdrawals(
                        scope, withdrawal_id, sequence, user_id, status,
                        amount_cents, requested_at, resolved_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope,
                        record_id,
                        sequence,
                        str(item.get("user_id")) if item.get("user_id") is not None else None,
                        item.get("status"),
                        _money_cents(item.get("amount", 0)),
                        item.get(time_name),
                        item.get("resolved_at") or item.get("paid_at"),
                        _dump(item),
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO referral_payouts(
                        scope, payout_id, sequence, user_id, amount_cents,
                        paid_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope,
                        record_id,
                        sequence,
                        str(item.get("user_id")) if item.get("user_id") is not None else None,
                        _money_cents(item.get("amount", 0)),
                        item.get(time_name),
                        _dump(item),
                    ),
                )


def _load_checker_settlements(connection):
    settlements = []
    for row in connection.execute(
        """
        SELECT amount_toman, payload_json
        FROM checker_settlements ORDER BY sequence
        """
    ):
        payload = _load(row["payload_json"])
        if isinstance(payload, dict):
            payload["amount_toman"] = int(row["amount_toman"])
            settlements.append(payload)
    return settlements


def _save_checker_settlements(connection, data):
    if not isinstance(data, list):
        raise ValueError("Checker settlements must contain a JSON list.")
    connection.execute("DELETE FROM checker_settlements")
    seen = set()
    for sequence, raw_item in enumerate(data):
        if not isinstance(raw_item, dict):
            raise ValueError("Checker settlement must contain a JSON object.")
        item = deepcopy(raw_item)
        settlement_id = str(item.get("id") or f"legacy:{sequence}")
        if settlement_id in seen:
            raise ValueError(f"Duplicate checker settlement ID {settlement_id!r}.")
        seen.add(settlement_id)
        amount = item.get("amount_toman", 0)
        try:
            amount_toman = int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"Invalid Toman settlement amount: {amount!r}") from error
        connection.execute(
            """
            INSERT INTO checker_settlements(
                settlement_id, sequence, checker_user_id, admin_user_id,
                amount_toman, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settlement_id,
                sequence,
                str(item.get("checker_user_id")) if item.get("checker_user_id") is not None else None,
                str(item.get("admin_user_id")) if item.get("admin_user_id") is not None else None,
                amount_toman,
                item.get("created_at"),
                _dump(item),
            ),
        )


def _load_kv(connection, descriptor):
    if descriptor.kind == "kv_list":
        row = connection.execute(
            """
            SELECT value_json FROM kv_state
            WHERE namespace=? AND scope=? AND state_key='__document__'
            """,
            (descriptor.namespace, descriptor.scope),
        ).fetchone()
        return _load(row["value_json"]) if row else []
    return {
        row["state_key"]: _load(row["value_json"])
        for row in connection.execute(
            """
            SELECT state_key, value_json FROM kv_state
            WHERE namespace=? AND scope=? ORDER BY rowid
            """,
            (descriptor.namespace, descriptor.scope),
        )
    }


def _save_kv(connection, descriptor, data):
    connection.execute(
        "DELETE FROM kv_state WHERE namespace=? AND scope=?",
        (descriptor.namespace, descriptor.scope),
    )
    if descriptor.kind == "kv_list":
        if not isinstance(data, list):
            raise ValueError(f"{descriptor.namespace} must contain a JSON list.")
        connection.execute(
            """
            INSERT INTO kv_state(namespace, scope, state_key, value_json, updated_at)
            VALUES (?, ?, '__document__', ?, ?)
            """,
            (
                descriptor.namespace,
                descriptor.scope,
                _dump(data),
                format_utc_timestamp(),
            ),
        )
        return
    if not isinstance(data, dict):
        raise ValueError(f"{descriptor.namespace} must contain a JSON object.")
    for key, value in data.items():
        connection.execute(
            """
            INSERT INTO kv_state(namespace, scope, state_key, value_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                descriptor.namespace,
                descriptor.scope,
                str(key),
                _dump(value),
                format_utc_timestamp(),
            ),
        )


def load_descriptor(connection, descriptor: StateDescriptor, default=None):
    if descriptor.kind == "payments":
        value = _load_payments(connection, descriptor.scope)
    elif descriptor.kind == "resellers":
        value = _load_resellers(connection)
    elif descriptor.kind == "hosted_registry":
        value = _load_hosted_registry(connection)
    elif descriptor.kind == "hosted_secrets":
        value = _load_hosted_secrets(connection)
    elif descriptor.kind == "hosted_settings":
        value = _load_hosted_settings(connection, descriptor.scope)
    elif descriptor.kind == "ledger":
        value = _load_ledger(connection, descriptor.scope)
    elif descriptor.kind == "referrals":
        value = _load_referrals(connection, descriptor.scope)
    elif descriptor.kind == "checker_settlements":
        value = _load_checker_settlements(connection)
    elif descriptor.kind.startswith("kv_"):
        value = _load_kv(connection, descriptor)
    else:
        raise ValueError(f"Unknown state kind: {descriptor.kind}")
    if value in ({}, []) and default is not None:
        return _copy_default(default)
    return value


def save_descriptor(connection, descriptor: StateDescriptor, data) -> None:
    # Validate serializability before deleting or replacing any existing rows.
    _dump(data)
    if descriptor.kind == "payments":
        _save_payments(connection, descriptor.scope, data)
    elif descriptor.kind == "resellers":
        _save_resellers(connection, data)
    elif descriptor.kind == "hosted_registry":
        _save_hosted_registry(connection, data)
    elif descriptor.kind == "hosted_secrets":
        _save_hosted_secrets(connection, data)
    elif descriptor.kind == "hosted_settings":
        _save_hosted_settings(connection, descriptor.scope, data)
    elif descriptor.kind == "ledger":
        _save_ledger(connection, descriptor.scope, data)
    elif descriptor.kind == "referrals":
        _save_referrals(connection, descriptor.scope, data)
    elif descriptor.kind == "checker_settlements":
        _save_checker_settlements(connection, data)
    elif descriptor.kind.startswith("kv_"):
        _save_kv(connection, descriptor, data)
    else:
        raise ValueError(f"Unknown state kind: {descriptor.kind}")


def read_state(path, default=None):
    descriptor = describe_path(path)
    if descriptor is None:
        raise ValueError(f"Path is not managed SQLite state: {path}")
    try:
        connection = database.get_connection()
        return load_descriptor(connection, descriptor, default)
    except sqlite3.OperationalError as error:
        import logging

        logging.getLogger("ajib.database").error(
            "SQLite read failed operation=%s:%s error=%s",
            descriptor.kind,
            descriptor.scope,
            error,
        )
        raise


def write_state(path, data) -> None:
    descriptor = describe_path(path)
    if descriptor is None:
        raise ValueError(f"Path is not managed SQLite state: {path}")
    with database.write_transaction(
        operation=f"{descriptor.kind}:{descriptor.scope}"
    ) as connection:
        save_descriptor(connection, descriptor, data)


def claim_payment_for_processing(path, payment_id, allowed_statuses, timestamp):
    """Atomically claim one payment with a conditional SQL update."""

    descriptor = describe_path(path)
    if descriptor is None or descriptor.kind != "payments":
        raise ValueError(f"Path is not managed payment state: {path}")
    allowed = tuple(sorted({str(status) for status in allowed_statuses}))
    if not allowed:
        return False
    payment_key = str(payment_id)
    with database.write_transaction(
        operation=f"payment_claim:{descriptor.scope}"
    ) as connection:
        row = connection.execute(
            """
            SELECT status, payload_json FROM payments
            WHERE scope=? AND payment_id=?
            """,
            (descriptor.scope, payment_key),
        ).fetchone()
        if row is None or str(row["status"] or "") not in allowed:
            return False
        previous_status = str(row["status"] or "")
        payload = _load(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError(f"Payment record {payment_key!r} must be an object.")
        update = {
            "status": "processing",
            "timestamp": timestamp,
            "previous_status": previous_status,
        }
        payload["status"] = "processing"
        payload["updated_at"] = timestamp
        updates = payload.setdefault("updates", [])
        if not isinstance(updates, list):
            updates = []
            payload["updates"] = updates
        updates.append(update)
        changed = connection.execute(
            """
            UPDATE payments
            SET status='processing', updated_at=?, payload_json=?
            WHERE scope=? AND payment_id=? AND status=?
            """,
            (
                timestamp,
                _dump(payload),
                descriptor.scope,
                payment_key,
                previous_status,
            ),
        ).rowcount
        if changed != 1:
            return False
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), -1) + 1
            FROM payment_events WHERE scope=? AND payment_id=?
            """,
            (descriptor.scope, payment_key),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO payment_events(
                scope, payment_id, sequence, status, previous_status,
                occurred_at, payload_json
            ) VALUES (?, ?, ?, 'processing', ?, ?, ?)
            """,
            (
                descriptor.scope,
                payment_key,
                int(sequence),
                previous_status,
                timestamp,
                _dump(update),
            ),
        )
        return True


def state_exists(path) -> bool:
    descriptor = describe_path(path)
    if descriptor is None:
        return os.path.exists(path)
    connection = database.get_connection()
    if descriptor.kind == "payments":
        query, args = "SELECT 1 FROM payments WHERE scope=? LIMIT 1", (descriptor.scope,)
    elif descriptor.kind == "resellers":
        query, args = "SELECT 1 FROM resellers LIMIT 1", ()
    elif descriptor.kind == "hosted_registry":
        query, args = "SELECT 1 FROM hosted_bots WHERE payload_json != '{}' LIMIT 1", ()
    elif descriptor.kind == "hosted_secrets":
        query, args = "SELECT 1 FROM hosted_bots WHERE token IS NOT NULL LIMIT 1", ()
    elif descriptor.kind == "hosted_settings":
        query, args = "SELECT 1 FROM hosted_settings WHERE reseller_id=? LIMIT 1", (_hosted_reseller_id(descriptor.scope),)
    elif descriptor.kind == "ledger":
        query, args = "SELECT 1 FROM ledger_accounts WHERE reseller_id=? LIMIT 1", (_hosted_reseller_id(descriptor.scope),)
    elif descriptor.kind == "referrals":
        query, args = "SELECT 1 FROM referral_scopes WHERE scope=? LIMIT 1", (descriptor.scope,)
    elif descriptor.kind == "checker_settlements":
        query, args = "SELECT 1 FROM checker_settlements LIMIT 1", ()
    else:
        query = "SELECT 1 FROM kv_state WHERE namespace=? AND scope=? LIMIT 1"
        args = (descriptor.namespace, descriptor.scope)
    return connection.execute(query, args).fetchone() is not None


def delete_state(path) -> None:
    descriptor = describe_path(path)
    if descriptor is None:
        if os.path.exists(path):
            os.remove(path)
        return
    empty = [] if descriptor.kind in {"kv_list", "checker_settlements"} else {}
    write_state(path, empty)


def query_recorded_usernames(scopes=("main",)):
    usernames = set()
    connection = database.get_connection()
    normalized_scopes = tuple(str(scope) for scope in scopes)
    if normalized_scopes:
        placeholders = ",".join("?" for _ in normalized_scopes)
        for row in connection.execute(
            f"SELECT payload_json FROM payments WHERE scope IN ({placeholders})",
            normalized_scopes,
        ):
            usernames.update(_extract_usernames(_load(row["payload_json"])))
    for row in connection.execute("SELECT payload_json FROM resellers"):
        usernames.update(_extract_usernames(_load(row["payload_json"])))
    for namespace in ("test_configs", "expired_cleanup"):
        for row in connection.execute(
            "SELECT value_json FROM kv_state WHERE namespace=?",
            (namespace,),
        ):
            usernames.update(_extract_usernames(_load(row["value_json"])))
    return usernames


def _extract_usernames(value):
    fields = {"username", "renewal_username", "renew_username", "provisioned_username"}
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields and isinstance(item, str) and item.strip():
                found.add(item.strip())
            found.update(_extract_usernames(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_extract_usernames(item))
    return found
