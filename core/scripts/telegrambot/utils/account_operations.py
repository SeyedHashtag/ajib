"""Durable account mutation claims and process-shared serialization.

An expired process is not evidence that its panel request failed. Uncertain
claims are retained until explicit reconciliation proves the external outcome.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from . import database

_local = threading.local()
_locks = {}
_registry_lock = threading.Lock()


class AccountBusy(ValueError):
    pass


def enabled():
    return os.getenv('AJIB_SQLITE_ACTIVE') == '1'


@contextmanager
def serialize(server_id, username):
    """Cross-process lock held across panel I/O, without a SQLite transaction."""
    if not enabled():
        yield
        return
    if not server_id or not username:
        raise AccountBusy('A verified server and account identity are required')
    key = hashlib.sha256(json.dumps([str(server_id), str(username)]).encode()).hexdigest()
    held = getattr(_local, 'held', set())
    if database.get_connection().in_transaction:
        raise AccountBusy('Account mutation cannot begin inside a database transaction')
    if key in held:
        yield
        return
    with _registry_lock:
        lock = _locks.setdefault(key, threading.RLock())
    # Fail rather than waiting while another thread might need a database lock.
    if not lock.acquire(blocking=False):
        raise AccountBusy('Another account operation is in progress')
    descriptor = None
    try:
        import fcntl
        target = Path(database.database_path()).parent / ('.account-' + key + '.lock')
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
        if os.fstat(descriptor).st_uid == os.geteuid() or os.geteuid() == 0:
            os.fchmod(descriptor, 0o660)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccountBusy('Another account operation is in progress') from None
        _local.held = held | {key}
        yield
    finally:
        _local.held = held
        if descriptor is not None:
            os.close(descriptor)
        lock.release()


def existing(operation_id):
    row = database.get_connection().execute('SELECT * FROM account_operations WHERE operation_id=?', (operation_id,)).fetchone()
    return dict(row) if row else None


def execute(operation_id, server_id, username, kind, request, action):
    """Caller supplies validated intent. Failed/uncertain panel writes never replay."""
    with serialize(server_id, username):
        payload = json.dumps(request, sort_keys=True, separators=(',', ':'))
        with database.transaction(operation='account_operation_claim') as db:
            row = db.execute('SELECT * FROM account_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if row:
                if (row['server_id'], row['username'], row['kind']) != (str(server_id), str(username), kind):
                    raise AccountBusy('Operation identity conflicts with a prior account mutation')
                if row['request_json'] != payload:
                    raise AccountBusy('Operation intent differs from the recorded request')
                if row['status'] == 'succeeded':
                    return {**json.loads(row['result_json']), 'reconciled': True}
                raise AccountBusy('The account operation requires reconciliation before any retry')
            try:
                now = int(time.time())
                db.execute('INSERT INTO account_operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                           (operation_id, str(server_id), str(username), kind, 'executing', payload, now, now))
            except sqlite3.IntegrityError:
                raise AccountBusy('Another account operation requires reconciliation') from None
        try:
            result = action()
        except BaseException:
            with database.transaction(operation='account_operation_uncertain') as db:
                db.execute("UPDATE account_operations SET status='uncertain',updated_at=? WHERE operation_id=?",
                           (int(time.time()), operation_id))
            raise
        saved = {key: result[key] for key in ('success', 'reason', 'username', 'server_id', 'before_state',
                                              'after_state', 'unlimited') if key in result}
        with database.transaction(operation='account_operation_finish') as db:
            db.execute('UPDATE account_operations SET status=?,result_json=?,updated_at=? WHERE operation_id=?',
                       ('succeeded' if result.get('success') else 'uncertain', json.dumps(saved), int(time.time()), operation_id))
        return {**result, **({'uncertain': True} if not result.get('success') else {})}


def assert_available(server_id, username):
    if enabled() and database.get_connection().execute("""SELECT 1 FROM account_operations
            WHERE server_id=? AND username=? AND status IN ('executing','uncertain') LIMIT 1""",
            (str(server_id), str(username))).fetchone():
        raise AccountBusy('The account has an unresolved mutation')
