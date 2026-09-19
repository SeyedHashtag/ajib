"""Durable account mutation claims and process-shared serialization.

An expired process is not evidence that its panel request failed. Uncertain
claims are retained until explicit reconciliation proves the external outcome.
"""
from contextlib import contextmanager, ExitStack
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
    key = hashlib.sha256(json.dumps([str(server_id), str(username).casefold()]).encode()).hexdigest()
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
        target = Path(database.database_path()).parent / ('.account-' + key + '.lock')
        if target.is_symlink():
            raise AccountBusy('Account lock path is invalid')
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o660)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                if os.fstat(descriptor).st_uid == os.geteuid() or os.geteuid() == 0:
                    os.fchmod(descriptor, 0o660)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
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


def active_workflow():
    return getattr(_local, 'operation_id', None)


def details(operation_id):
    row = database.get_connection().execute('SELECT * FROM account_operation_details WHERE operation_id=?', (operation_id,)).fetchone()
    return dict(row) if row else None


@contextmanager
def serialize_many(resources):
    with ExitStack() as stack:
        for server, username in sorted({(str(server), str(username).casefold()) for server, username in resources}):
            stack.enter_context(serialize(server, username))
        yield


def _origin(operation_id):
    if operation_id.startswith('main-payment:'):
        return {'scope': 'main', 'type': 'payment', 'id': operation_id.removeprefix('main-payment:')}
    if operation_id.startswith('hosted-payment:'):
        _, owner, ident = operation_id.split(':', 2)
        return {'scope': 'hosted:' + owner, 'type': 'payment', 'id': ident}
    return {'scope': 'main', 'type': 'manual', 'id': operation_id}


def payment_terms(record):
    """Bind dispatch to the authorized quote, excluding evolving progress fields."""
    fields = ('user_id', 'type', 'plan_gb', 'days', 'unlimited', 'price', 'currency',
              'payment_method', 'account_credit_reservation_id', 'incentive_reservation_id',
              'account_credit_reserved', 'collected_amount', 'invite_discount_percent', 'referral_reward_base',
              'converted_amount', 'converted_currency', 'exchange_rate',
              'renewal_mode', 'renewal_base_record_id', 'renewal_username',
              'renewal_recorded_server_id', 'receipt_checker_user_id', 'routed_to_checker',
              'renew_username', 'retail_price', 'wholesale_price', 'wholesale_prepaid', 'funding',
              'referral_reward', 'reward_calculation_base', 'margin', 'original_price',
              'invite_discount_amount', 'crypto_discount_amount', 'crypto_discount_percent',
              'total_discount_amount', 'total_discount_percent', 'crypto_collected',
              'list_price', 'reseller_level', 'discount_percent', 'renewal_source_plan_snapshot',
              'renewal_plan_snapshot', 'renewal_baseline')
    return hashlib.sha256(json.dumps({key: record.get(key) for key in fields},
                                    sort_keys=True, default=str).encode()).hexdigest()


def funding_terms(record):
    fields = ('reseller_id', 'operation_id', 'total_cents', 'prepaid_cents', 'debt_cents', 'metadata')
    return hashlib.sha256(json.dumps({key: record.get(key) for key in fields},
                                    sort_keys=True, default=str).encode()).hexdigest()


def event(db, operation_id, phase, *, actor='runtime', reason='', evidence_digest=None):
    db.execute('INSERT INTO account_operation_events(operation_id,actor,phase,reason,evidence_digest,occurred_at) VALUES (?,?,?,?,?,?)',
               (operation_id, str(actor), phase, reason, evidence_digest, int(time.time())))


def transition(db, operation_id, phase, *, actor='runtime', reason='', evidence_digest=None):
    db.execute('UPDATE account_operation_details SET phase=?,revision=revision+1,updated_at=? WHERE operation_id=?',
               (phase, int(time.time()), operation_id))
    event(db, operation_id, phase, actor=actor, reason=reason, evidence_digest=evidence_digest)


def complete(operation_id):
    """Call only from the transaction committing the originating obligation."""
    if not enabled():
        return False
    with database.transaction(operation='account_operation_complete') as db:
        row = existing(operation_id)
        if not row:
            return False
        if row['status'] != 'succeeded':
            raise AccountBusy('Panel success has not been verified')
        metadata = details(operation_id)
        if not metadata:
            raise AccountBusy('Legacy operation requires reviewed provenance')
        if db.execute("SELECT 1 FROM account_operation_steps WHERE operation_id=? AND phase!='verified'",
                      (operation_id,)).fetchone():
            raise AccountBusy('A workflow step still requires verification')
        if metadata['phase'] != 'completed':
            transition(db, operation_id, 'completed')
            db.execute('DELETE FROM account_operation_claims WHERE operation_id=?', (operation_id,))
        return True


def step(operation_id, step_id, intent, action):
    """One durable panel dispatch within a claimed multi-step workflow.

    A dispatched step never replays, including after parent process termination.
    The caller must hold the parent's resource locks throughout the workflow.
    """
    metadata = details(operation_id)
    if not metadata or metadata['phase'] == 'completed':
        raise AccountBusy('A live workflow claim is required')
    payload = json.dumps(intent, sort_keys=True, separators=(',', ':'))
    with serialize_many(json.loads(metadata['resources_json'])):
        with database.transaction(operation='account_step_prepare') as db:
            row = db.execute('SELECT * FROM account_operation_steps WHERE operation_id=? AND step_id=?',
                             (operation_id, step_id)).fetchone()
            if row:
                if row['intent_json'] != payload:
                    raise AccountBusy('Workflow step intent changed')
                if row['phase'] == 'verified':
                    return json.loads(row['result_json'])
                if row['phase'] != 'prepared':
                    raise AccountBusy('Workflow step requires reconciliation')
            else:
                db.execute('INSERT INTO account_operation_steps VALUES (?,?,?, ?,NULL,?)',
                           (operation_id, step_id, 'prepared', payload, int(time.time())))
        with database.transaction(operation='account_step_dispatch') as db:
            db.execute("UPDATE account_operation_steps SET phase='dispatched',updated_at=? WHERE operation_id=? AND step_id=?",
                       (int(time.time()), operation_id, step_id))
            event(db, operation_id, 'step_dispatched', reason=step_id)
        result = action()
        if not isinstance(result, dict) or not result.get('success'):
            raise AccountBusy('Workflow step outcome requires verification')
        with database.transaction(operation='account_step_verified') as db:
            db.execute("UPDATE account_operation_steps SET phase='verified',result_json=?,updated_at=? WHERE operation_id=? AND step_id=?",
                       (json.dumps(result), int(time.time()), operation_id, step_id))
            event(db, operation_id, 'step_verified', reason=step_id)
        return result


def plan_steps(operation_id, intents):
    """Persist every planned child intent before the first external dispatch."""
    if not details(operation_id) or details(operation_id)['phase'] == 'completed':
        raise AccountBusy('A live workflow claim is required')
    with database.transaction(operation='account_steps_plan') as db:
        for name, intent in intents.items():
            payload = json.dumps(intent, sort_keys=True, separators=(',', ':'))
            previous = db.execute('SELECT intent_json FROM account_operation_steps WHERE operation_id=? AND step_id=?',
                                  (operation_id, name)).fetchone()
            if previous and previous[0] != payload:
                raise AccountBusy('Workflow plan changed')
            db.execute("INSERT OR IGNORE INTO account_operation_steps VALUES (?,?,'prepared',?,NULL,?)",
                       (operation_id, name, payload, int(time.time())))


def execute(operation_id, server_id, username, kind, request, action, *, origin=None, resources=()):
    """Caller supplies validated intent. Failed/uncertain panel writes never replay."""
    resources = sorted({(str(server_id), str(username).casefold()),
                        *((str(server), str(name).casefold()) for server, name in resources)})
    with serialize_many(resources):
        payload = json.dumps(request, sort_keys=True, separators=(',', ':'))
        with database.transaction(operation='account_operation_claim') as db:
            row = db.execute('SELECT * FROM account_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if row:
                if (row['server_id'], row['username'], row['kind']) != (str(server_id), str(username), kind):
                    raise AccountBusy('Operation identity conflicts with a prior account mutation')
                if row['request_json'] != payload:
                    raise AccountBusy('Operation intent differs from the recorded request')
                metadata = details(operation_id)
                if not metadata or json.loads(metadata['resources_json']) != [list(item) for item in resources]:
                    raise AccountBusy('Operation resource ownership differs from the recorded claim')
                provenance = json.loads(metadata['origin_json'])
                if origin and any(provenance.get(key) != value for key, value in origin.items()):
                    raise AccountBusy('Operation origin differs from the recorded obligation')
                if provenance.get('type') == 'payment':
                    payment = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                                         (provenance['scope'], provenance['id'])).fetchone()
                    record = json.loads(payment[0]) if payment else {}
                    if payment_terms(record) != provenance.get('terms_digest'):
                        raise AccountBusy('Payment terms changed after preparation')
                if provenance.get('type') == 'funding' and provenance.get('terms_digest'):
                    funding = db.execute('SELECT payload_json FROM reseller_order_funding WHERE reseller_id=? AND operation_id=?',
                                         (provenance['reseller_id'], provenance['id'])).fetchone()
                    if not funding or funding_terms(json.loads(funding[0])) != provenance['terms_digest']:
                        raise AccountBusy('Funding terms changed after preparation')
                if provenance.get('type') == 'reseller_reservation':
                    from .reserved_completion import reseller_obligation, reseller_terms
                    if reseller_terms(reseller_obligation(provenance['reseller_id'], provenance['id'])) != provenance.get('terms_digest'):
                        raise AccountBusy('Reserved reseller terms changed after preparation')
                if row['status'] == 'succeeded':
                    return {**json.loads(row['result_json']), 'reconciled': True}
                if not metadata or metadata['phase'] != 'ready':
                    raise AccountBusy('The account operation requires reconciliation before any retry')
                for server, name in resources:
                    assert_available(server, name, operation_id=operation_id)
            else:
                try:
                    now = int(time.time())
                    for server, name in resources:
                        assert_available(server, name)
                        db.execute('INSERT INTO account_operation_claims VALUES (?,?,?)', (server, name, operation_id))
                    db.execute('INSERT INTO account_operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                               (operation_id, str(server_id), str(username), kind, 'executing', payload, now, now))
                    provenance = origin or _origin(operation_id)
                    if provenance.get('type') == 'reseller_reservation':
                        from .reserved_completion import reseller_obligation, reseller_terms
                        if reseller_terms(reseller_obligation(provenance['reseller_id'], provenance['id'])) != provenance.get('terms_digest'):
                            raise AccountBusy('Reserved reseller terms changed before dispatch')
                    if provenance.get('type') == 'funding':
                        funding = db.execute('SELECT payload_json FROM reseller_order_funding WHERE reseller_id=? AND operation_id=?',
                                             (provenance['reseller_id'], provenance['id'])).fetchone()
                        if funding:
                            provenance = {**provenance, 'terms_digest': funding_terms(json.loads(funding[0]))}
                    if provenance.get('type') == 'payment':
                        payment = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                                             (provenance['scope'], provenance['id'])).fetchone()
                        record = json.loads(payment[0]) if payment else {}
                        provenance = {**provenance, 'owner': record.get('fulfillment_owner',
                            'bot' if provenance['scope'] == 'main' else 'hosted'), 'user_id': str(record.get('user_id', '')),
                            'terms_digest': payment_terms(record),
                            'reservation_ids': [str(record[key]) for key in ('account_credit_reservation_id', 'incentive_reservation_id') if record.get(key)]}
                    db.execute('INSERT INTO account_operation_details VALUES (?,?,1,?,?,?)',
                               (operation_id, 'prepared', json.dumps(provenance), json.dumps(resources), now))
                    event(db, operation_id, 'prepared')
                except sqlite3.IntegrityError:
                    raise AccountBusy('Another account operation requires reconciliation') from None
        with database.transaction(operation='account_operation_dispatch') as db:
            transition(db, operation_id, 'dispatched')
        try:
            previous_context = active_workflow()
            _local.operation_id = operation_id
            try:
                result = action()
            finally:
                _local.operation_id = previous_context
            if not isinstance(result, dict):
                raise ValueError('Panel mutation returned an invalid result')
        except BaseException:
            with database.transaction(operation='account_operation_uncertain') as db:
                db.execute("UPDATE account_operations SET status='uncertain',updated_at=? WHERE operation_id=?",
                           (int(time.time()), operation_id))
                transition(db, operation_id, 'uncertain')
            raise
        saved = {key: result[key] for key in ('success', 'reason', 'username', 'server_id', 'before_state',
                                              'after_state', 'unlimited') if key in result}
        with database.transaction(operation='account_operation_finish') as db:
            db.execute('UPDATE account_operations SET status=?,result_json=?,updated_at=? WHERE operation_id=?',
                       ('succeeded' if result.get('success') else 'uncertain', json.dumps(saved), int(time.time()), operation_id))
            transition(db, operation_id, 'panel_verified' if result.get('success') else 'uncertain')
        return {**result, **({'uncertain': True} if not result.get('success') else {})}


def assert_available(server_id, username, *, operation_id=None):
    if enabled():
        db = database.get_connection()
        claim = db.execute('SELECT 1 FROM account_operation_claims WHERE server_id=? AND username_key=? AND operation_id!=?',
                           (str(server_id), str(username).casefold(), operation_id or '')).fetchone()
        legacy = db.execute("""SELECT 1 FROM account_operations o LEFT JOIN account_operation_details d
            ON d.operation_id=o.operation_id WHERE o.server_id=? AND lower(o.username)=?
            AND d.operation_id IS NULL AND o.status IN ('executing','uncertain','succeeded') LIMIT 1""",
            (str(server_id), str(username).casefold())).fetchone()
        if claim or legacy:
            raise AccountBusy('The account has an unresolved mutation')


def assert_no_pending_obligations(server_id, username):
    """Destructive workflows must preserve queued renewals as well as live claims."""
    if not enabled():
        return
    from .identity_references import references
    active = {'reserved', 'processing', 'attention', 'creating', 'pending', 'pending_approval',
              'approved', 'waiting_receipt', 'paid_provision_failed', 'uncertain'}
    def pending(value):
        if isinstance(value, list):
            return any(pending(item) for item in value)
        if not isinstance(value, dict):
            return False
        if value.get('renewal_status') in active or value.get('status') in active:
            return True
        return any(pending(item) for key, item in value.items() if key not in {'updates', 'history', 'debt_charges', 'transactions'})
    for ref in references(str(server_id), str(username)):
        if ref['table'] == 'payments' and pending(ref['record']):
            raise AccountBusy('An existing payment or renewal still owns this account')
        if ref['table'] == 'resellers':
            for config in ref['record'].get('configs', []):
                if (str(config.get('username', '')).casefold() == str(username).casefold()
                        and str(config.get('server_id') or 'primary') == str(server_id) and pending(config)):
                    raise AccountBusy('A reserved reseller obligation still owns this account')
        if ref['table'] == 'kv_state' and ref['keys']['namespace'] == 'test_configs':
            record = ref['record']
            if pending(record) or any(record.get(key) for key in
                    ('creation_pending_at', 'web_creation_pending', 'account_operation_id')):
                raise AccountBusy('Trial recovery still owns this account')


def assert_obligation_releasable(scope, origin_id):
    if not enabled():
        return
    row = database.get_connection().execute('''SELECT 1 FROM account_operation_details
        WHERE phase!='completed' AND json_extract(origin_json,'$.scope')=?
        AND (json_extract(origin_json,'$.id')=? OR EXISTS (
            SELECT 1 FROM json_each(json_extract(origin_json,'$.reservation_ids')) WHERE value=?)) LIMIT 1''',
        (str(scope), str(origin_id), str(origin_id))).fetchone()
    legacy_id = ('main-payment:' + str(origin_id) if scope == 'main' else
                 'hosted-payment:' + str(scope).removeprefix('hosted:') + ':' + str(origin_id))
    legacy = database.get_connection().execute('''SELECT 1 FROM account_operations o
        LEFT JOIN account_operation_details d ON d.operation_id=o.operation_id
        WHERE o.operation_id=? AND d.operation_id IS NULL''', (legacy_id,)).fetchone()
    if row or legacy:
        raise AccountBusy('An account operation still owns this financial reservation')
