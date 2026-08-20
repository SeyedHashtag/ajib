"""SQLite connection, schema, transaction, integrity, and backup helpers."""

from __future__ import annotations

import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from pathlib import Path

from .time_utils import format_utc_timestamp
from .timestamp_migration import migrate_v3_utc_timestamps
from .renewal_migration import migrate_v4_renewal_timezone_rechecks


DEFAULT_BOT_DIR = "/etc/ajib/core/scripts/telegrambot"
DATABASE_NAME = "ajib.db"
SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000

_local = threading.local()
_schema_lock = threading.RLock()
_initialized_paths = set()


def bot_dir() -> str:
    return os.path.abspath(os.getenv("AJIB_BOT_DIR", DEFAULT_BOT_DIR))


def database_path(path: str | os.PathLike[str] | None = None) -> str:
    if path is not None:
        return os.path.abspath(os.fspath(path))
    configured = os.getenv("AJIB_DB_PATH")
    if configured:
        return os.path.abspath(configured)
    return os.path.join(bot_dir(), DATABASE_NAME)


def _connection_map() -> dict[str, sqlite3.Connection]:
    connections = getattr(_local, "connections", None)
    if connections is None:
        connections = {}
        _local.connections = connections
    return connections


def _transaction_depths() -> dict[str, int]:
    depths = getattr(_local, "transaction_depths", None)
    if depths is None:
        depths = {}
        _local.transaction_depths = depths
    return depths


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS state_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        scope TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        user_id TEXT,
        status TEXT,
        kind TEXT,
        payment_method TEXT,
        amount_cents INTEGER,
        currency TEXT,
        created_at TEXT,
        updated_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, payment_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS payments_user_idx ON payments(scope, user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS payments_status_idx ON payments(scope, status, updated_at)",
    """
    CREATE TABLE IF NOT EXISTS payment_events (
        scope TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        status TEXT,
        previous_status TEXT,
        occurred_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, payment_id, sequence),
        FOREIGN KEY (scope, payment_id)
            REFERENCES payments(scope, payment_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS payment_events_status_idx
    ON payment_events(scope, status, occurred_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS payment_events_time_idx
    ON payment_events(scope, occurred_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS resellers (
        reseller_id TEXT PRIMARY KEY,
        status TEXT,
        debt_cents INTEGER NOT NULL DEFAULT 0,
        total_paid_cents INTEGER NOT NULL DEFAULT 0,
        debt_since TEXT,
        telegram_username TEXT,
        payload_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS resellers_status_idx ON resellers(status)",
    """
    CREATE TABLE IF NOT EXISTS reseller_configs (
        reseller_id TEXT NOT NULL,
        config_index INTEGER NOT NULL,
        username TEXT,
        server_id TEXT,
        retail_order_id TEXT,
        price_cents INTEGER,
        created_at TEXT,
        cleanup_status TEXT,
        removed INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (reseller_id, config_index),
        FOREIGN KEY (reseller_id) REFERENCES resellers(reseller_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS reseller_config_order_idx
    ON reseller_configs(reseller_id, retail_order_id)
    WHERE retail_order_id IS NOT NULL AND retail_order_id != ''
    """,
    "CREATE INDEX IF NOT EXISTS reseller_config_username_idx ON reseller_configs(username, server_id)",
    """
    CREATE TABLE IF NOT EXISTS reseller_renewals (
        reseller_id TEXT NOT NULL,
        config_index INTEGER NOT NULL,
        renewal_index INTEGER NOT NULL,
        retail_order_id TEXT,
        price_cents INTEGER,
        created_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (reseller_id, config_index, renewal_index),
        FOREIGN KEY (reseller_id, config_index)
            REFERENCES reseller_configs(reseller_id, config_index) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS reseller_renewal_order_idx
    ON reseller_renewals(reseller_id, retail_order_id)
    WHERE retail_order_id IS NOT NULL AND retail_order_id != ''
    """,
    """
    CREATE TABLE IF NOT EXISTS hosted_bots (
        reseller_id TEXT PRIMARY KEY,
        bot_id TEXT,
        username TEXT,
        token TEXT,
        token_fingerprint TEXT,
        status TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT,
        updated_at TEXT,
        started_at TEXT,
        last_error TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS hosted_bot_id_idx
    ON hosted_bots(bot_id) WHERE bot_id IS NOT NULL AND bot_id != ''
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS hosted_bot_fingerprint_idx
    ON hosted_bots(token_fingerprint)
    WHERE token_fingerprint IS NOT NULL AND token_fingerprint != ''
    """,
    "CREATE INDEX IF NOT EXISTS hosted_bot_status_idx ON hosted_bots(status, enabled)",
    """
    CREATE TABLE IF NOT EXISTS hosted_settings (
        reseller_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_accounts (
        reseller_id TEXT PRIMARY KEY,
        earnings_available_cents INTEGER NOT NULL DEFAULT 0,
        earnings_reserved_cents INTEGER NOT NULL DEFAULT 0,
        referral_liability_cents INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_transactions (
        reseller_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        kind TEXT,
        amount_cents INTEGER NOT NULL,
        metadata_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        PRIMARY KEY (reseller_id, transaction_id),
        FOREIGN KEY (reseller_id) REFERENCES ledger_accounts(reseller_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS credit_reservations (
        reseller_id TEXT NOT NULL,
        reservation_id TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        created_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (reseller_id, reservation_id),
        FOREIGN KEY (reseller_id) REFERENCES ledger_accounts(reseller_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS withdrawals (
        reseller_id TEXT NOT NULL,
        withdrawal_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        status TEXT,
        amount_cents INTEGER NOT NULL,
        destination TEXT,
        requested_at TEXT,
        resolved_at TEXT,
        admin_id TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (reseller_id, withdrawal_id),
        FOREIGN KEY (reseller_id) REFERENCES ledger_accounts(reseller_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS withdrawals_status_idx ON withdrawals(status, requested_at)",
    """
    CREATE TABLE IF NOT EXISTS referral_scopes (
        scope TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS referral_accounts (
        scope TEXT NOT NULL,
        user_id TEXT NOT NULL,
        code TEXT,
        invited_count INTEGER NOT NULL DEFAULT 0,
        total_earnings_cents INTEGER NOT NULL DEFAULT 0,
        available_balance_cents INTEGER NOT NULL DEFAULT 0,
        wallet TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, user_id),
        FOREIGN KEY (scope) REFERENCES referral_scopes(scope) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS referral_code_idx
    ON referral_accounts(scope, code) WHERE code IS NOT NULL AND code != ''
    """,
    """
    CREATE TABLE IF NOT EXISTS referral_links (
        scope TEXT NOT NULL,
        invitee_user_id TEXT NOT NULL,
        referrer_user_id TEXT NOT NULL,
        code TEXT,
        invited_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, invitee_user_id),
        FOREIGN KEY (scope) REFERENCES referral_scopes(scope) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS referral_rewards (
        scope TEXT NOT NULL,
        order_id TEXT NOT NULL,
        referrer_user_id TEXT,
        amount_cents INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, order_id),
        FOREIGN KEY (scope) REFERENCES referral_scopes(scope) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS referral_withdrawals (
        scope TEXT NOT NULL,
        withdrawal_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        user_id TEXT,
        status TEXT,
        amount_cents INTEGER NOT NULL,
        requested_at TEXT,
        resolved_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, withdrawal_id),
        FOREIGN KEY (scope) REFERENCES referral_scopes(scope) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS referral_payouts (
        scope TEXT NOT NULL,
        payout_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        user_id TEXT,
        amount_cents INTEGER NOT NULL,
        paid_at TEXT,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (scope, payout_id),
        FOREIGN KEY (scope) REFERENCES referral_scopes(scope) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checker_settlements (
        settlement_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        checker_user_id TEXT,
        admin_user_id TEXT,
        amount_toman INTEGER NOT NULL,
        created_at TEXT,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv_state (
        namespace TEXT NOT NULL,
        scope TEXT NOT NULL,
        state_key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')),
        PRIMARY KEY (namespace, scope, state_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        user_id TEXT,
        surface TEXT NOT NULL,
        hosted_tenant_id TEXT NOT NULL DEFAULT '',
        language TEXT,
        plan_id TEXT,
        payment_method TEXT,
        referral_campaign TEXT,
        occurred_at TEXT NOT NULL,
        deduplication_key TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000Z', 'now')),
        UNIQUE(event_type, surface, hosted_tenant_id, deduplication_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS growth_events_time_idx
    ON growth_events(occurred_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS growth_events_scope_time_idx
    ON growth_events(surface, hosted_tenant_id, occurred_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS growth_events_type_time_idx
    ON growth_events(event_type, occurred_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS growth_events_user_time_idx
    ON growth_events(user_id, occurred_at)
    WHERE user_id IS NOT NULL AND user_id != ''
    """,
    """
    CREATE TABLE IF NOT EXISTS account_credit_accounts (
        user_id TEXT PRIMARY KEY,
        available_cents INTEGER NOT NULL DEFAULT 0,
        reserved_cents INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_credit_transactions (
        user_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        kind TEXT,
        amount_cents INTEGER NOT NULL,
        order_id TEXT,
        created_at TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (user_id, transaction_id),
        FOREIGN KEY (user_id)
            REFERENCES account_credit_accounts(user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS account_credit_transactions_order_idx
    ON account_credit_transactions(order_id)
    WHERE order_id IS NOT NULL AND order_id != ''
    """,
    """
    CREATE TABLE IF NOT EXISTS account_credit_reservations (
        user_id TEXT NOT NULL,
        reservation_id TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        created_at TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (user_id, reservation_id),
        FOREIGN KEY (user_id)
            REFERENCES account_credit_accounts(user_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recruitment_milestones (
        reseller_id TEXT PRIMARY KEY,
        referrer_id TEXT,
        sales_count INTEGER NOT NULL DEFAULT 0,
        settled_cents INTEGER NOT NULL DEFAULT 0,
        status TEXT,
        reward_cents INTEGER NOT NULL DEFAULT 0,
        qualified_at TEXT,
        claimed_at TEXT,
        choice TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS recruitment_milestones_status_idx
    ON recruitment_milestones(status, qualified_at)
    """,
)


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")


def _ensure_permissions(path: str) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _open_connection(path: str) -> sqlite3.Connection:
    _ensure_permissions(path)
    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    _configure_connection(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    _ensure_permissions(path)
    return connection


def _ensure_schema(connection: sqlite3.Connection, path: str) -> None:
    with _schema_lock:
        if path in _initialized_paths:
            return
        connection.execute("BEGIN EXCLUSIVE")
        try:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            ledger_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(ledger_transactions)"
                )
            }
            if "payload_json" not in ledger_columns:
                connection.execute(
                    "ALTER TABLE ledger_transactions "
                    "ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
                )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            current = int(row["version"])
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {current} is newer than supported schema {SCHEMA_VERSION}."
                )
            if current < SCHEMA_VERSION:
                if current < 3:
                    migrate_v3_utc_timestamps(connection)
                if current < 4:
                    migrate_v4_renewal_timezone_rechecks(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, format_utc_timestamp()),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        _initialized_paths.add(path)
        _ensure_permissions(path)


def get_connection(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    resolved = database_path(path)
    connections = _connection_map()
    connection = connections.get(resolved)
    if connection is None:
        connection = _open_connection(resolved)
        connections[resolved] = connection
    _ensure_schema(connection, resolved)
    return connection


@contextmanager
def transaction(
    path: str | os.PathLike[str] | None = None,
    *,
    immediate: bool = True,
    operation: str | None = None,
):
    """Yield a connection inside a re-entrant transaction.

    Nested domain operations use savepoints on the same thread-local
    connection, allowing existing accounting call graphs to commit atomically.
    """

    resolved = database_path(path)
    connection = get_connection(resolved)
    depths = _transaction_depths()
    depth = depths.get(resolved, 0)
    savepoint = f"ajib_sp_{depth}"
    try:
        if depth == 0:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        else:
            connection.execute(f"SAVEPOINT {savepoint}")
    except sqlite3.OperationalError as error:
        logging.getLogger("ajib.database").error(
            "SQLite transaction could not start operation=%s path=%s depth=%s error=%s",
            operation or "unspecified",
            resolved,
            depth,
            error,
        )
        raise
    depths[resolved] = depth + 1
    try:
        yield connection
        if depth == 0:
            connection.execute("COMMIT")
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as error:
        if depth == 0:
            connection.execute("ROLLBACK")
        else:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        if isinstance(error, sqlite3.OperationalError):
            logging.getLogger("ajib.database").error(
                "SQLite transaction failed operation=%s path=%s depth=%s error=%s",
                operation or "unspecified",
                resolved,
                depth,
                error,
            )
        raise
    finally:
        if depth == 0:
            depths.pop(resolved, None)
        else:
            depths[resolved] = depth


@contextmanager
def read_transaction(path: str | os.PathLike[str] | None = None, *, operation=None):
    with transaction(path, immediate=False, operation=operation) as connection:
        yield connection


@contextmanager
def write_transaction(path: str | os.PathLike[str] | None = None, *, operation=None):
    with transaction(path, immediate=True, operation=operation) as connection:
        yield connection


def close_connections() -> None:
    for connection in _connection_map().values():
        try:
            connection.close()
        except sqlite3.Error:
            pass
    _local.connections = {}
    _local.transaction_depths = {}


def reset_connection(path: str | os.PathLike[str] | None = None) -> None:
    resolved = database_path(path)
    connection = _connection_map().pop(resolved, None)
    if connection is not None:
        connection.close()
    _transaction_depths().pop(resolved, None)
    with _schema_lock:
        _initialized_paths.discard(resolved)


def integrity_check(path: str | os.PathLike[str] | None = None, *, quick: bool = False) -> str:
    connection = get_connection(path)
    pragma = "quick_check" if quick else "integrity_check"
    rows = connection.execute(f"PRAGMA {pragma}").fetchall()
    result = "\n".join(str(row[0]) for row in rows)
    if result.lower() != "ok":
        raise RuntimeError(f"SQLite {pragma} failed: {result}")
    return result


def backup_database(
    destination: str | os.PathLike[str],
    source: str | os.PathLike[str] | None = None,
) -> str:
    source_connection = get_connection(source)
    destination_path = os.path.abspath(os.fspath(destination))
    parent = os.path.dirname(destination_path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(destination_path):
        os.remove(destination_path)
    target = sqlite3.connect(destination_path, isolation_level=None)
    try:
        source_connection.backup(target)
        rows = target.execute("PRAGMA quick_check").fetchall()
        result = "\n".join(str(row[0]) for row in rows)
        if result.lower() != "ok":
            raise RuntimeError(f"SQLite backup quick_check failed: {result}")
    finally:
        target.close()
    os.chmod(destination_path, 0o600)
    return destination_path


def schema_version(path: str | os.PathLike[str] | None = None) -> int:
    row = get_connection(path).execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"])


def user_table_row_count(path: str | os.PathLike[str] | None = None) -> int:
    connection = get_connection(path)
    tables = (
        "payments",
        "payment_events",
        "resellers",
        "reseller_configs",
        "reseller_renewals",
        "hosted_bots",
        "hosted_settings",
        "ledger_accounts",
        "ledger_transactions",
        "credit_reservations",
        "withdrawals",
        "referral_scopes",
        "referral_accounts",
        "referral_links",
        "referral_rewards",
        "referral_withdrawals",
        "referral_payouts",
        "checker_settlements",
        "kv_state",
        "growth_events",
        "account_credit_accounts",
        "account_credit_transactions",
        "account_credit_reservations",
        "recruitment_milestones",
    )
    application_metadata = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM state_metadata
            WHERE key NOT IN ('utc_timestamp_migration_v3', 'renewal_timezone_recheck_v4')
            """
        ).fetchone()[0]
    )
    return application_metadata + sum(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    )


def replace_database_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None = None,
) -> str:
    """Atomically install an already validated database snapshot."""

    destination_path = database_path(destination)
    source_path = os.path.abspath(os.fspath(source))
    reset_connection(destination_path)
    for suffix in ("-wal", "-shm"):
        sidecar = f"{destination_path}{suffix}"
        if os.path.exists(sidecar):
            os.remove(sidecar)
    Path(destination_path).parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_path, destination_path)
    _ensure_permissions(destination_path)
    integrity_check(destination_path)
    return destination_path
