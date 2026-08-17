"""Auditable main-account purchase credit with atomic reservation semantics."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from utils import database
from utils.time_utils import format_utc_timestamp


def _now():
    return format_utc_timestamp()


def _user_key(user_id):
    value = str(user_id or "").strip()
    if not value or len(value) > 64 or "\x00" in value:
        raise ValueError("Invalid account-credit user ID")
    return value


def _money_cents(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid account-credit amount") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid account-credit amount")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(cents):
    return float((Decimal(int(cents or 0)) / Decimal(100)).quantize(Decimal("0.01")))


def _dump(value):
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _ensure_account(connection, user_id):
    connection.execute(
        """
        INSERT INTO account_credit_accounts(user_id, payload_json)
        VALUES (?, '{}') ON CONFLICT(user_id) DO NOTHING
        """,
        (user_id,),
    )


def _account_from_connection(connection, user_id):
    row = connection.execute(
        """
        SELECT available_cents, reserved_cents, payload_json
        FROM account_credit_accounts WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return {
            "user_id": user_id,
            "available": 0.0,
            "reserved": 0.0,
            "metadata": {},
        }
    try:
        metadata = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "user_id": user_id,
        "available": _money(row["available_cents"]),
        "reserved": _money(row["reserved_cents"]),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def credit_account_in_transaction(
    connection,
    user_id,
    amount,
    transaction_id,
    *,
    source=None,
    metadata=None,
):
    """Credit an account using an existing SQLite write transaction."""
    user_key = _user_key(user_id)
    amount_cents = _money_cents(amount)
    transaction_key = str(transaction_id or "").strip()
    if amount_cents <= 0 or not transaction_key or len(transaction_key) > 200:
        raise ValueError("A positive amount and transaction ID are required")
    payload = {"source": source, **dict(metadata or {})}
    _ensure_account(connection, user_key)
    existing = connection.execute(
        """
        SELECT amount_cents, kind FROM account_credit_transactions
        WHERE user_id=? AND transaction_id=?
        """,
        (user_key, transaction_key),
    ).fetchone()
    if existing is not None:
        if int(existing["amount_cents"]) != amount_cents or existing["kind"] != "credit":
            raise ValueError("Account-credit transaction ID was reused with different data")
        return _account_from_connection(connection, user_key)
    connection.execute(
        """
        UPDATE account_credit_accounts
        SET available_cents=available_cents + ? WHERE user_id=?
        """,
        (amount_cents, user_key),
    )
    connection.execute(
        """
        INSERT INTO account_credit_transactions(
            user_id, transaction_id, kind, amount_cents, created_at, payload_json
        ) VALUES (?, ?, 'credit', ?, ?, ?)
        """,
        (user_key, transaction_key, amount_cents, _now(), _dump(payload)),
    )
    return _account_from_connection(connection, user_key)


def get_account_credit(user_id, *, path=None):
    user_key = _user_key(user_id)
    with database.read_transaction(path, operation="account_credit_read") as connection:
        return _account_from_connection(connection, user_key)


def credit_account(user_id, amount, transaction_id, *, source=None, metadata=None, path=None):
    """Credit a balance once; repeated transaction IDs return the existing state."""
    with database.write_transaction(path, operation="account_credit_credit") as connection:
        return credit_account_in_transaction(
            connection,
            user_id,
            amount,
            transaction_id,
            source=source,
            metadata=metadata,
        )


def transfer_account_credit(
    source_user_id,
    destination_user_id,
    amount,
    transaction_id,
    *,
    source=None,
    metadata=None,
    path=None,
):
    """Atomically move available credit between two ledger accounts."""
    requested_cents = _money_cents(amount)
    if requested_cents <= 0:
        raise ValueError("A positive transfer amount is required")
    transfer_id = str(transaction_id or "").strip()
    if not transfer_id:
        raise ValueError("A transfer transaction ID is required")
    reservation_id = f"transfer:{transfer_id}"
    source_key = _user_key(source_user_id)
    destination_key = _user_key(destination_user_id)
    with database.write_transaction(path, operation="account_credit_transfer") as connection:
        existing = connection.execute(
            """
            SELECT amount_cents, payload_json FROM account_credit_transactions
            WHERE user_id=? AND transaction_id=? AND kind='credit'
            """,
            (destination_key, f"transfer-in:{transfer_id}"),
        ).fetchone()
        if existing is not None:
            if int(existing["amount_cents"]) != requested_cents:
                raise ValueError("Account-credit transfer ID was reused with a different amount")
            try:
                existing_payload = json.loads(existing["payload_json"] or "{}")
            except (TypeError, ValueError):
                existing_payload = {}
            if str(existing_payload.get("source_user_id") or "") != source_key:
                raise ValueError("Account-credit transfer ID was reused with a different source")
            return _account_from_connection(connection, destination_key)
        outgoing = connection.execute(
            """
            SELECT amount_cents, payload_json FROM account_credit_transactions
            WHERE user_id=? AND transaction_id=? AND kind='consume'
            """,
            (source_key, f"consume:{reservation_id}"),
        ).fetchone()
        if outgoing is not None:
            try:
                outgoing_payload = json.loads(outgoing["payload_json"] or "{}")
            except (TypeError, ValueError):
                outgoing_payload = {}
            if (
                -int(outgoing["amount_cents"]) != requested_cents
                or str(outgoing_payload.get("destination_user_id") or "") != destination_key
            ):
                raise ValueError("Account-credit transfer ID was reused with different data")
            return credit_account(
                destination_key,
                amount,
                f"transfer-in:{transfer_id}",
                source=source or "account_credit_transfer",
                metadata={"source_user_id": source_key, **dict(metadata or {})},
                path=path,
            )
        reserved = reserve_account_credit(
            source_key,
            reservation_id,
            amount,
            order_id=transfer_id,
            metadata={"destination_user_id": str(destination_user_id)},
            path=path,
        )
        if _money_cents(reserved) != requested_cents:
            raise ValueError("Insufficient account credit")
        consumed = consume_account_credit(
            source_key,
            reservation_id,
            order_id=transfer_id,
            metadata={"destination_user_id": str(destination_user_id)},
            path=path,
        )
        if _money_cents(consumed) != requested_cents:
            raise RuntimeError("Account-credit transfer could not be consumed")
        return credit_account(
            destination_user_id,
            amount,
            f"transfer-in:{transfer_id}",
            source=source or "account_credit_transfer",
            metadata={
                "source_user_id": str(source_user_id),
                **dict(metadata or {}),
            },
            path=path,
        )


def reserve_account_credit(
    user_id,
    reservation_id,
    requested_amount,
    *,
    order_id=None,
    metadata=None,
    path=None,
):
    """Reserve up to the available balance for a direct AJIB checkout."""
    user_key = _user_key(user_id)
    reservation_key = str(reservation_id or "").strip()
    requested_cents = _money_cents(requested_amount)
    if not reservation_key or len(reservation_key) > 200:
        raise ValueError("A reservation ID is required")
    with database.write_transaction(path, operation="account_credit_reserve") as connection:
        _ensure_account(connection, user_key)
        existing = connection.execute(
            """
            SELECT amount_cents, payload_json FROM account_credit_reservations
            WHERE user_id=? AND reservation_id=?
            """,
            (user_key, reservation_key),
        ).fetchone()
        if existing is not None:
            return _money(existing["amount_cents"])
        row = connection.execute(
            "SELECT available_cents FROM account_credit_accounts WHERE user_id=?",
            (user_key,),
        ).fetchone()
        reserved_cents = min(requested_cents, int(row["available_cents"] or 0))
        if reserved_cents <= 0:
            return 0.0
        payload = {"order_id": str(order_id or reservation_key), **dict(metadata or {})}
        connection.execute(
            """
            UPDATE account_credit_accounts
            SET available_cents=available_cents - ?, reserved_cents=reserved_cents + ?
            WHERE user_id=?
            """,
            (reserved_cents, reserved_cents, user_key),
        )
        connection.execute(
            """
            INSERT INTO account_credit_reservations(
                user_id, reservation_id, amount_cents, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (user_key, reservation_key, reserved_cents, _now(), _dump(payload)),
        )
        return _money(reserved_cents)


def release_account_credit(user_id, reservation_id, *, path=None):
    user_key = _user_key(user_id)
    reservation_key = str(reservation_id or "").strip()
    with database.write_transaction(path, operation="account_credit_release") as connection:
        row = connection.execute(
            """
            SELECT amount_cents FROM account_credit_reservations
            WHERE user_id=? AND reservation_id=?
            """,
            (user_key, reservation_key),
        ).fetchone()
        if row is None:
            return False
        amount_cents = int(row["amount_cents"])
        connection.execute(
            """
            UPDATE account_credit_accounts
            SET available_cents=available_cents + ?,
                reserved_cents=MAX(0, reserved_cents - ?)
            WHERE user_id=?
            """,
            (amount_cents, amount_cents, user_key),
        )
        connection.execute(
            "DELETE FROM account_credit_reservations WHERE user_id=? AND reservation_id=?",
            (user_key, reservation_key),
        )
        return True


def consume_account_credit(user_id, reservation_id, *, order_id=None, metadata=None, path=None):
    """Consume a reservation exactly once after fulfillment succeeds."""
    user_key = _user_key(user_id)
    reservation_key = str(reservation_id or "").strip()
    transaction_key = f"consume:{reservation_key}"
    with database.write_transaction(path, operation="account_credit_consume") as connection:
        existing = connection.execute(
            """
            SELECT amount_cents FROM account_credit_transactions
            WHERE user_id=? AND transaction_id=? AND kind='consume'
            """,
            (user_key, transaction_key),
        ).fetchone()
        if existing is not None:
            return _money(-int(existing["amount_cents"]))
        row = connection.execute(
            """
            SELECT amount_cents, payload_json FROM account_credit_reservations
            WHERE user_id=? AND reservation_id=?
            """,
            (user_key, reservation_key),
        ).fetchone()
        if row is None:
            return 0.0
        amount_cents = int(row["amount_cents"])
        connection.execute(
            """
            UPDATE account_credit_accounts
            SET reserved_cents=MAX(0, reserved_cents - ?) WHERE user_id=?
            """,
            (amount_cents, user_key),
        )
        connection.execute(
            "DELETE FROM account_credit_reservations WHERE user_id=? AND reservation_id=?",
            (user_key, reservation_key),
        )
        payload = {"reservation_id": reservation_key, **dict(metadata or {})}
        connection.execute(
            """
            INSERT INTO account_credit_transactions(
                user_id, transaction_id, kind, amount_cents, order_id,
                created_at, payload_json
            ) VALUES (?, ?, 'consume', ?, ?, ?, ?)
            """,
            (
                user_key,
                transaction_key,
                -amount_cents,
                str(order_id or reservation_key),
                _now(),
                _dump(payload),
            ),
        )
        return _money(amount_cents)


def list_account_credit_transactions(user_id, *, path=None):
    user_key = _user_key(user_id)
    with database.read_transaction(path, operation="account_credit_history") as connection:
        return [
            {
                "transaction_id": row["transaction_id"],
                "kind": row["kind"],
                "amount": _money(row["amount_cents"]),
                "order_id": row["order_id"],
                "created_at": row["created_at"],
            }
            for row in connection.execute(
                """
                SELECT transaction_id, kind, amount_cents, order_id, created_at
                FROM account_credit_transactions
                WHERE user_id=? ORDER BY rowid
                """,
                (user_key,),
            )
        ]
