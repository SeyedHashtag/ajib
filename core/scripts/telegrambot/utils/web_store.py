"""Additive web state in the same SQLite file as bot transactions.

The extension has its own migration marker: schema-6 bot workers can continue
to read their tables, and SQLite backups include all extension tables.
"""
import hashlib
import json
import secrets
import time
from . import database

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS web_schema(version INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS web_worker_health(
        role TEXT PRIMARY KEY, heartbeat_at INTEGER NOT NULL,
        last_success_at INTEGER, last_error TEXT, writes_enabled INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS web_trials(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, key TEXT NOT NULL,
        status TEXT NOT NULL, language TEXT NOT NULL, username TEXT,server_id TEXT,
        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,reason TEXT,
        UNIQUE(user_id,key))""",
    """CREATE TABLE IF NOT EXISTS web_actions(
        scope TEXT NOT NULL,user_id TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,
        request_hash TEXT NOT NULL,result_json TEXT NOT NULL,created_at INTEGER NOT NULL,
        PRIMARY KEY(scope,user_id,kind,key))""",
    """CREATE TABLE IF NOT EXISTS web_challenges(
        id TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, scope TEXT NOT NULL,
        user_id TEXT, expires_at INTEGER NOT NULL, confirmed_at INTEGER,
        consumed_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS web_sessions(
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, scope TEXT NOT NULL,
        csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL, revoked_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS web_replays(
        digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS web_rate_limits(
        bucket TEXT PRIMARY KEY, started_at INTEGER NOT NULL, hits INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS web_storefronts(
        scope TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS web_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
        scope TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
        occurred_at INTEGER NOT NULL, details_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS web_operations(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL,
        key TEXT NOT NULL, request_hash TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        UNIQUE(scope, user_id, key))""",
    """CREATE TABLE IF NOT EXISTS web_outbox(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, recipient TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
        lease_until INTEGER, lease_token TEXT, last_error TEXT)""",
    """CREATE INDEX IF NOT EXISTS web_outbox_due
        ON web_outbox(status,next_attempt_at,lease_until)""",
    """CREATE TABLE IF NOT EXISTS web_receipts(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, payment_id TEXT NOT NULL,
        user_id TEXT NOT NULL, content_type TEXT NOT NULL, contents BLOB NOT NULL,
        created_at INTEGER NOT NULL)""",
)


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def initialize():
    with database.transaction(operation="web_schema") as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        version = connection.execute("SELECT MAX(version) FROM web_schema").fetchone()[0]
        if version and version > 1:
            raise RuntimeError("Web schema is newer than this application")
        connection.execute("INSERT OR IGNORE INTO web_schema VALUES (1)")


def audit(connection, actor, scope, action, resource, details=None):
    connection.execute("""INSERT INTO web_audit
        (actor,scope,action,resource,occurred_at,details_json) VALUES (?,?,?,?,?,?)""",
        (str(actor), scope, action, str(resource), int(time.time()),
         json.dumps(details or {}, ensure_ascii=False)))


def enqueue(connection, event_id, scope, recipient, text):
    connection.execute("""INSERT OR IGNORE INTO web_outbox
        (id,scope,recipient,text,next_attempt_at) VALUES (?,?,?,?,?)""",
        (event_id, scope, str(recipient), text, int(time.time())))


def rate_limit(bucket, maximum=10, window=60):
    now = int(time.time())
    with database.transaction(operation="web_rate_limit") as connection:
        connection.execute("DELETE FROM web_rate_limits WHERE started_at < ?", (now - 3600,))
        row = connection.execute("SELECT * FROM web_rate_limits WHERE bucket=?", (bucket,)).fetchone()
        if not row or row["started_at"] <= now - window:
            connection.execute("INSERT OR REPLACE INTO web_rate_limits VALUES (?,?,1)", (bucket, now))
            return True
        if row["hits"] >= maximum:
            return False
        connection.execute("UPDATE web_rate_limits SET hits=hits+1 WHERE bucket=?", (bucket,))
        return True


def claim_notification():
    now = int(time.time())
    with database.transaction(operation="web_outbox_claim") as connection:
        row = connection.execute("""SELECT * FROM web_outbox
            WHERE (status='pending' AND next_attempt_at<=?)
               OR (status='sending' AND lease_until<=?)
            ORDER BY next_attempt_at LIMIT 1""", (now, now)).fetchone()
        if not row:
            return None
        token = secrets.token_hex(16)
        connection.execute("""UPDATE web_outbox SET status='sending',
            attempts=attempts+1,lease_until=?,lease_token=? WHERE id=?""", (now + 120, token, row["id"]))
        return {**dict(row), "lease_token": token}


def finish_notification(item, error=None):
    with database.transaction(operation="web_outbox_finish") as connection:
        connection.execute("""UPDATE web_outbox SET status=?,last_error=?,
            next_attempt_at=?,lease_until=NULL,lease_token=NULL
            WHERE id=? AND lease_token=? AND status='sending'""",
            ("pending" if error else "sent", error,
             int(time.time()) + min(3600, 2 ** min(item["attempts"] + 2, 12)),
             item["id"], item["lease_token"]))
