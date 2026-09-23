"""Operator-only, evidence-bound completion of an uncertain 3x-ui renewal.

The two panel snapshots are read-only evidence. This module never sends a panel
mutation; the original reserved-payment finalizer owns all accounting changes.
"""
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import account_operations, database, operation_recovery

GIB = 1024 ** 3
DAY_MS = 86_400_000


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _database(path):
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError('Evidence must be a regular SQLite file')
    source = candidate.resolve(strict=True)
    if not source.is_file():
        raise ValueError('Evidence must be a regular SQLite file')
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        connection.close()
        raise ValueError('Evidence database failed integrity check')
    return connection


def _one(connection, query, arguments):
    rows = connection.execute(query, arguments).fetchmany(2)
    if len(rows) != 1:
        raise ValueError('Evidence identity is missing or ambiguous')
    return dict(rows[0])


def _epoch_ms(value):
    return round(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)


def _panel_state(connection, username):
    client = _one(connection, 'SELECT * FROM clients WHERE email=?', (username,))
    traffic = _one(connection, 'SELECT * FROM client_traffics WHERE email=?', (username,))
    inbound = sorted(row[0] for row in connection.execute(
        'SELECT inbound_id FROM client_inbounds WHERE client_id=?', (client['id'],)))
    if not inbound:
        raise ValueError('Panel inbound ownership is missing')
    return client, traffic, inbound


def _evidence(operation_id, panels, before, after, payment_before):
    rows = operation_recovery._read(operation_id)
    if len(rows) != 1:
        raise ValueError('Operation is missing or ambiguous')
    row = rows[0]
    origin = json.loads(row['origin_json'] or '{}')
    request = json.loads(row['request_json'] or '{}')
    if (row['kind'], row['status'], row['phase'], origin.get('type'), origin.get('scope')) != (
            'renewal', 'uncertain', 'uncertain', 'payment', 'main'):
        raise ValueError('Only an uncertain main-store payment renewal is supported')
    if not row['operation_id'].startswith('main-payment:') or row['operation_id'] != 'main-payment:' + str(origin.get('id')):
        raise ValueError('Operation and payment identities disagree')
    if json.loads(row['resources_json'] or 'null') != [[row['server_id'], row['username']]]:
        raise ValueError('Account ownership scope is incomplete')
    current = operation_recovery._payment(origin)
    if not current or current.get('status') != 'completed' or current.get('renewal_status') != 'reserved':
        raise ValueError('The paid reservation is no longer pending')
    if current.get('renewal_mode') != 'reserved' or current.get('renewal_claim_id'):
        raise ValueError('A competing renewal claim exists')
    if (str(current.get('user_id')) != str(origin.get('user_id'))
            or current.get('fulfillment_owner', 'bot') != origin.get('owner')
            or current.get('renewal_username') != row['username']
            or str(current.get('renewal_server_id') or current.get('renewal_recorded_server_id')) != row['server_id']):
        raise ValueError('Payment ownership changed')
    claim = database.get_connection().execute(
        'SELECT operation_id FROM account_operation_claims WHERE server_id=? AND username_key=?',
        (row['server_id'], row['username'].casefold())).fetchone()
    if not claim or claim[0] != operation_id:
        raise ValueError('Original account claim is missing')
    if database.get_connection().execute('SELECT 1 FROM account_operation_steps WHERE operation_id=? LIMIT 1',
                                         (operation_id,)).fetchone():
        raise ValueError('This recovery requires separate child-step evidence')
    with closing(_database(payment_before)) as prior:
        old = json.loads(_one(prior, 'SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                              ('main', origin['id']))['payload_json'])
    if account_operations.payment_terms(old) != origin.get('terms_digest'):
        raise ValueError('Retained payment terms do not match dispatch')
    restored = {**current, 'renewal_baseline': old.get('renewal_baseline')}
    if account_operations.payment_terms(restored) != origin['terms_digest']:
        raise ValueError('Financial terms beyond the baseline changed')
    target = request.get('target') or {}
    days = int(target['days'])
    expected_bytes = int(target['plan_gb']) * GIB
    if days <= 0 or expected_bytes <= 0 or not isinstance(request.get('before'), dict):
        raise ValueError('Saved renewal intent is incomplete')
    with closing(_database(before)) as first, closing(_database(after)) as second:
        prior_client, prior_traffic, prior_inbound = _panel_state(first, row['username'])
        renewed_client, renewed_traffic, renewed_inbound = _panel_state(second, row['username'])
    identity = ('id', 'email', 'sub_id', 'uuid', 'password', 'auth', 'secret', 'created_at')
    if any(prior_client[key] != renewed_client[key] for key in identity) or prior_inbound != renewed_inbound:
        raise ValueError('Panel account identity or inbounds changed')
    if (int(prior_client['expiry_time']) - int(request['before']['expiration_days']) * DAY_MS
            != _epoch_ms(request['before']['account_creation_date'])):
        raise ValueError('Before snapshot does not match dispatch generation')
    cycle_ms = int(renewed_client['expiry_time']) - days * DAY_MS
    updated_ms = int(renewed_client['updated_at'])
    dispatch_ms = int(row['created_at']) * 1000
    if not (dispatch_ms <= cycle_ms <= dispatch_ms + 10_000
            and dispatch_ms <= updated_ms <= dispatch_ms + 10_000
            and abs(cycle_ms - updated_ms) <= 1_000):
        raise ValueError('Panel cycle does not align with original dispatch')
    if (int(renewed_client['total_gb']) != expected_bytes
            or int(renewed_traffic['total']) != expected_bytes
            or int(renewed_traffic['expiry_time']) != int(renewed_client['expiry_time'])
            or int(renewed_client['enable']) != 1
            or int(renewed_traffic['up']) + int(renewed_traffic['down']) >=
               int(prior_traffic['up']) + int(prior_traffic['down'])):
        raise ValueError('The panel does not prove the intended reset')
    client, user, lookup = panels.resolve_unique_user(row['username'], preferred_server_id=row['server_id'],
                                                      allow_exact_on_partial=False, force_refresh=True)
    if (lookup.get('status') != 'found' or not lookup.get('uniqueness_verified')
            or not client or str(client.server_id) != row['server_id'] or not isinstance(user, dict)):
        raise ValueError('Current panel identity cannot be verified')
    if (user.get('username', '').casefold() != row['username'].casefold()
            or abs(_epoch_ms(user.get('account_creation_date')) - cycle_ms) > 1
            or int(user.get('expiration_days') or 0) != days
            or int(user.get('max_download_bytes') or 0) != expected_bytes
            or bool(user.get('unlimited_ip')) != bool(target.get('unlimited'))):
        raise ValueError('Current panel cycle changed')
    from .renewal import capture_user_state
    observed = dict(user, upload_bytes=renewed_traffic['up'], download_bytes=renewed_traffic['down'])
    after_state = capture_user_state(observed, now=datetime.fromtimestamp(updated_ms / 1000, timezone.utc))
    after_state['captured_at'] = datetime.fromtimestamp(updated_ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')
    hashes = {name: _file_digest(path) for name, path in (
        ('panel_before', before), ('panel_after', after), ('payment_before', payment_before))}
    # Traffic continues to accrue while the operator reviews evidence. Bind the
    # digest to the live entitlement instead of volatile usage counters; the
    # panel is fetched again under the account lock immediately before commit.
    live_entitlement = {key: user.get(key) for key in (
        'username', 'account_creation_date', 'expiration_days',
        'max_download_bytes', 'unlimited_ip')}
    live_entitlement['server_id'] = str(client.server_id)
    bound = {'operation': {key: row[key] for key in ('operation_id', 'revision', 'request_json', 'result_json', 'origin_json')},
             'payment': operation_recovery._digest(current), 'original_baseline': old.get('renewal_baseline'),
             'files': hashes, 'panel': live_entitlement, 'after_state': after_state}
    digest = hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode()).hexdigest()
    report = {'operation_id': operation_id, 'classification': 'panel_verified', 'action': 'complete_accounting',
              'reason': 'paired_panel_backups_verify_original_renewal', 'evidence_digest': digest,
              'backup_sha256': hashes, 'operation_revision': row['revision'],
              'panel_cycle_started_at': after_state['account_creation_date']}
    return report, old['renewal_baseline'], after_state, current, row


def inspect(operation_id, panels, before, after, payment_before):
    return _evidence(operation_id, panels, before, after, payment_before)[0]


def reconcile(operation_id, panels, before, after, payment_before, evidence_digest):
    from . import renewal, reserved_completion, reseller
    rows = operation_recovery._read(operation_id)
    if len(rows) != 1:
        raise ValueError('Operation unavailable')
    row = rows[0]
    with account_operations.serialize(row['server_id'], row['username']):
        report, baseline, after_state, current, saved = _evidence(
            operation_id, panels, before, after, payment_before)
        if not hmac.compare_digest(str(evidence_digest), report['evidence_digest']):
            raise ValueError('Evidence changed; inspect again')
        origin = json.loads(saved['origin_json'])
        with reseller.reseller_lock, database.transaction(operation='verified_renewal_backup_reconciliation') as db:
            detail = account_operations.details(operation_id)
            if not detail or detail['revision'] != saved['revision'] or detail['phase'] != 'uncertain':
                raise ValueError('Operation changed during inspection')
            payment = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                                 ('main', origin['id'])).fetchone()
            if not payment or operation_recovery._digest(json.loads(payment[0])) != operation_recovery._digest(current):
                raise ValueError('Payment changed during inspection')
            from .atomic_store import locked_json
            with locked_json(renewal.PAYMENTS_FILE, {}) as payments:
                record = payments[str(origin['id'])]
                record['renewal_baseline'] = baseline
                if account_operations.payment_terms(record) != origin['terms_digest']:
                    raise ValueError('Recovered terms changed')
                claim_id = 'verified-backup:' + report['evidence_digest'][:32]
                record['renewal_status'] = 'processing'
                record['renewal_claim_id'] = claim_id
                record['renewal_processing_from'] = 'reserved'
                record['renewal_claimed_at'] = datetime.now(timezone.utc).isoformat()
            result = json.loads(account_operations.existing(operation_id)['result_json'] or '{}')
            result.update(success=True, username=saved['username'], server_id=saved['server_id'],
                          before_state=json.loads(saved['request_json'])['before'], after_state=after_state)
            db.execute('UPDATE account_operations SET status=?,result_json=? WHERE operation_id=?',
                       ('succeeded', json.dumps(result), operation_id))
            account_operations.transition(db, operation_id, 'panel_verified', actor='cli',
                                          reason='paired_panel_backups_verify_original_renewal',
                                          evidence_digest=report['evidence_digest'])
            if not reserved_completion.payment(origin['id'], claim_id, payments_file=renewal.PAYMENTS_FILE):
                raise ValueError('Original reserved-renewal finalizer did not complete')
        return {**report, 'applied': True}
