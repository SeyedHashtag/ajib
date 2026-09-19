"""Read-only evidence gathering and explicitly authorized reconciliation.

Panel inspection never performs a mutation. A timeout, age, unchanged account,
or an apparently renewed account cannot authorize another external request.
"""
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from contextlib import nullcontext

from . import account_operations as operations, database


def _read(operation_id=None, *, status=None, scope=None, origin_id=None):
    with sqlite3.connect(Path(database.database_path()).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        query = '''SELECT o.*,d.phase,d.revision,d.origin_json,d.resources_json FROM account_operations o
            LEFT JOIN account_operation_details d ON d.operation_id=o.operation_id WHERE 1=1'''
        parameters = []
        for clause, value in [('o.operation_id=?', operation_id), ('COALESCE(d.phase,o.status)=?', status),
                              ("json_extract(d.origin_json,'$.scope')=?", scope),
                              ("json_extract(d.origin_json,'$.id')=?", origin_id)]:
            if value is not None:
                query += ' AND ' + clause
                parameters.append(value)
        return [dict(row) for row in db.execute(query + ' ORDER BY o.created_at,o.operation_id LIMIT 500', parameters)]


def list_operations(**filters):
    fields = ('operation_id', 'server_id', 'username', 'kind', 'status', 'phase', 'revision', 'created_at', 'updated_at')
    return [{**{key: row[key] for key in fields}, 'origin': json.loads(row['origin_json'] or '{}')}
            for row in _read(**filters)]


def _payment(origin):
    if origin.get('type') == 'reseller_reservation':
        from .reserved_completion import reseller_obligation
        return reseller_obligation(origin['reseller_id'], origin['id'])
    if origin.get('type') == 'trial':
        from .atomic_store import read_json
        from .trial_operations import CONFIGS
        return read_json(CONFIGS, {}).get(origin['user_id'])
    if origin.get('type') == 'funding':
        with sqlite3.connect(Path(database.database_path()).as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT payload_json FROM reseller_order_funding WHERE reseller_id=? AND operation_id=?',
                             (origin.get('reseller_id'), origin.get('id'))).fetchone()
            return json.loads(row[0]) if row else None
    if origin.get('type') != 'payment':
        return None
    with sqlite3.connect(Path(database.database_path()).as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                         (origin.get('scope'), origin.get('id'))).fetchone()
        return json.loads(row[0]) if row else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _snapshot(user):
    if not isinstance(user, dict):
        return None
    fields = ('username', 'account_creation_date', 'expiration_days', 'max_download_bytes',
              'upload_bytes', 'download_bytes', 'blocked', 'unlimited_ip', 'status')
    result = {key: user.get(key) for key in fields}
    result['note_digest'] = hashlib.sha256(str(user.get('note') or '').encode()).hexdigest()
    return result


def _created_matches(row, request, origin, user):
    marker = request.get('marker')
    if not marker and row['operation_id'].startswith('main-payment:web_'):
        marker = 'web-order:' + origin.get('id', '')
    if not marker or marker not in str(user.get('note') or ''):
        return False
    try:
        return (str(user.get('username', '')).casefold() == row['username'].casefold()
                and int(user['max_download_bytes']) == int(request['plan_gb']) * 1024**3
                and int(user['expiration_days']) == int(request['days'])
                and isinstance(user.get('unlimited_ip'), bool)
                and user['unlimited_ip'] == bool(request.get('unlimited')))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def inspect(operation_id, panels):
    rows = _read(operation_id)
    if not rows:
        raise ValueError('Operation not found')
    row = rows[0]
    origin, request = json.loads(row['origin_json'] or '{}'), json.loads(row['request_json'])
    payment = _payment(origin)
    report = {key: row[key] for key in ('operation_id', 'server_id', 'username', 'kind', 'phase', 'revision')}
    report.update(origin=origin, classification='uncertain', action='none', reason='insufficient_evidence', observed=None,
                  obligation_digest=_digest(payment))
    if not row['phase']:
        report['reason'] = 'legacy_provenance_missing'
    elif row['phase'] == 'completed':
        report.update(classification='completed', reason='already_completed')
    elif origin.get('type') == 'payment' and (not payment
            or str(payment.get('user_id')) != origin.get('user_id')
            or payment.get('fulfillment_owner', 'bot' if origin.get('scope') == 'main' else 'hosted') != origin.get('owner')
            or operations.payment_terms(payment) != origin.get('terms_digest')
            or payment.get('status') in {'cancelled', 'canceled', 'rejected'}):
        report['reason'] = 'obligation_changed'
    elif origin.get('type') == 'trial' and (not payment
            or payment.get('account_operation_id') != operation_id
            or payment.get('trial_scope') != origin.get('scope')):
        report['reason'] = 'trial_ownership_changed'
    elif origin.get('type') == 'funding' and (not payment or payment.get('status') not in {'reserved', 'completed'}
            or operations.funding_terms(payment) != origin.get('terms_digest')):
        report['reason'] = 'funding_obligation_changed'
    elif origin.get('type') == 'reseller_reservation' and not _reseller_reservation_matches(origin, payment):
        report['reason'] = 'reseller_reservation_changed'
    elif origin.get('type') == 'rename':
        from .account_rename import evidence
        client = panels.get_client(row['server_id'])
        if client is not None:
            observed = evidence(client, row['username'], request)
            report['observed'] = observed
            if observed['success'] and observed['reference_digest'] == request['references']:
                report.update(classification='panel_verified', action='complete_accounting', reason='rename_identity_verified')
            elif observed['reference_digest'] != request['references']:
                report['reason'] = 'identity_references_changed'
    elif origin.get('type') in {'banned_cleanup', 'debt_removal'}:
        from .reseller_removal import inspect as inspect_removal
        observed = inspect_removal(operation_id, panels)
        report['observed'] = observed
        if observed['success']:
            report.update(classification='panel_verified', action='complete_accounting', reason='reseller_removal_verified')
        elif observed['reseller_digest'] != request['reseller_digest']:
            report['reason'] = 'reseller_debt_or_ownership_changed'
    elif origin.get('type') in {'migration', 'copy'}:
        from .migration_operations import inspect as inspect_migration
        observed = inspect_migration(operation_id, panels)
        report.update(observed=observed, action=observed['action'], reason=observed['reason'])
        if observed['action'] != 'none':
            report['classification'] = 'panel_verified'
    else:
        try:
            client, user, lookup = panels.resolve_unique_user(row['username'], preferred_server_id=row['server_id'],
                allow_exact_on_partial=False, force_refresh=True)
        except Exception:
            client, user, lookup = None, None, {'status': 'unavailable'}
        report['lookup'] = {'status': lookup.get('status'), 'uniqueness_verified': bool(lookup.get('uniqueness_verified'))}
        report['observed'] = _snapshot(user)
        exact = (lookup.get('status') == 'found' and lookup.get('uniqueness_verified')
                 and client and str(client.server_id) == row['server_id'])
        if row['phase'] in {'prepared', 'ready'}:
            # This is durable local dispatch evidence, not an inference from absence.
            report.update(classification='not_dispatched', action='return_to_owner', reason='dispatch_never_started')
        elif row['phase'] == 'panel_verified' and row['status'] == 'succeeded' and exact and (
                row['kind'] != 'renewal' or _renewal_generation_matches(row, user)):
            report.update(classification='panel_verified', action='complete_accounting', reason='durable_panel_success')
        elif row['kind'] == 'create' and exact and _created_matches(row, request, origin, user):
            report.update(classification='panel_verified', action='complete_accounting', reason='creation_identity_and_marker_match')
        elif row['kind'] == 'delete' and lookup.get('status') == 'missing' and lookup.get('uniqueness_verified'):
            report.update(classification='panel_verified', action='complete_accounting', reason='deletion_verified_absent')
        elif row['kind'] == 'renewal':
            report['reason'] = 'renewal_dispatch_outcome_unproven'
        # Never route one transport's payment into another transport's fulfillment.
        supported = (origin.get('type') == 'payment' and origin.get('scope') == 'main'
                     and row['kind'] in {'create', 'renewal'})
        if origin.get('type') == 'payment' and str(origin.get('scope', '')).startswith('hosted:'):
            from .hosted_settlement import intent
            saved = intent(origin['scope'].removeprefix('hosted:'), origin['id'])
            supported = bool(saved and row['kind'] in {'create', 'renewal'} and
                             (saved['record'].get('renewal_mode') == 'reserved' or
                              saved['terms_digest'] == origin.get('terms_digest')))
        supported = supported or (origin.get('type') == 'trial' and row['kind'] == 'create')
        supported = supported or (origin.get('type') == 'reseller_reservation' and row['kind'] == 'renewal')
        supported = supported or (origin.get('type') == 'admin' and row['kind'] in {'create', 'update', 'reset', 'delete'})
        if origin.get('type') == 'cleanup':
            from .identity_references import fingerprint
            supported = row['kind'] == 'delete' and fingerprint(row['server_id'], row['username']) == request.get('references')
        if origin.get('type') == 'funding':
            fulfillment = (payment or {}).get('fulfillment') or {}
            data = fulfillment.get('data') or {}
            supported = (row['kind'] in {'create', 'renewal'} and
                         (data.get('username') or fulfillment.get('username')) == row['username'] and
                         str(data.get('server_id') or fulfillment.get('server_id')) == row['server_id'])
        if report['action'] == 'return_to_owner':
            supported = supported and origin.get('owner') == 'web' and origin.get('type') == 'payment'
        if report['action'] != 'none' and not supported:
            report.update(action='none', reason='origin_completion_adapter_required')
    report['steps'] = [dict(step) for step in database.get_connection().execute(
        'SELECT step_id,phase,updated_at FROM account_operation_steps WHERE operation_id=? ORDER BY step_id', (operation_id,))]
    bound = {'report': report, 'request': row['request_json'], 'result': row['result_json'],
             'payment': payment, 'resources': row['resources_json']}
    report['evidence_digest'] = hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode()).hexdigest()
    return report


def _renewal_generation_matches(row, user):
    after = json.loads(row['result_json'] or '{}').get('after_state') or {}
    fields = ('account_creation_date', 'expiration_days', 'max_download_bytes')
    return bool(after.get('account_creation_date')) and all(str(after.get(key)) == str(user.get(key)) for key in fields)


def reconcile(operation_id, panels, evidence_digest, *, actor='cli', reason='operator_reconciliation'):
    rows = _read(operation_id)
    if not rows or not rows[0]['resources_json']:
        raise ValueError('Operation has no verified resource ownership')
    with operations.serialize_many(json.loads(rows[0]['resources_json'])):
        report = inspect(operation_id, panels)
        if not hmac.compare_digest(str(evidence_digest), report['evidence_digest']):
            raise ValueError('Evidence changed; inspect this operation again')
        if report['classification'] == 'completed':
            return report
        if report['action'] == 'none':
            raise ValueError('Operation remains uncertain: ' + report['reason'])
        if report['action'] == 'resume_migration':
            # Original workflow may perform a proven-undispatched step. Do not
            # hold the SQLite writer transaction while it calls a panel.
            from .migration_operations import resume
            resume(operation_id, panels, report['revision'], actor=actor, reason=reason, evidence_digest=evidence_digest)
            return inspect(operation_id, panels)
        origin = report['origin']
        # Match the finalizers' lock order. Taking this after SQLite's writer
        # slot can deadlock a bot finalizer already holding the reseller lock.
        if (origin.get('type') in {'funding', 'reseller_reservation', 'banned_cleanup', 'debt_removal'}
                or (origin.get('type') == 'payment' and (origin.get('scope', '').startswith('hosted:')
                    or (_payment(origin) or {}).get('renewal_mode') == 'reserved'))):
            from .reseller import reseller_lock
            accounting_lock = reseller_lock
        else:
            accounting_lock = nullcontext()
        with accounting_lock, database.transaction(operation='account_operation_reconcile') as db:
            current = operations.details(operation_id)
            if not current or current['revision'] != report['revision']:
                raise ValueError('Operation revision changed')
            # Compare obligation again after acquiring SQLite's writer slot.
            if inspect_payment_changed(db, origin, report):
                raise ValueError('Payment obligation changed; inspect again')
            if report['action'] == 'return_to_owner':
                if current['phase'] not in {'prepared', 'ready'}:
                    raise ValueError('Dispatch evidence no longer permits retry')
                operations.transition(db, operation_id, 'ready', actor=actor, reason=reason, evidence_digest=evidence_digest)
                from .web_orders import save_payment
                save_payment(db, 'main', origin['id'], {'status': 'approved'})
                db.execute("UPDATE web_operations SET status='approved',updated_at=strftime('%s','now') WHERE id=? AND scope='main'", (origin['id'],))
            else:
                row = operations.existing(operation_id)
                result = json.loads(row['result_json'] or '{}')
                result.update(success=True, username=row['username'], server_id=row['server_id'])
                db.execute("UPDATE account_operations SET status='succeeded',result_json=? WHERE operation_id=?", (json.dumps(result), operation_id))
                operations.transition(db, operation_id, 'panel_verified', actor=actor, reason=reason, evidence_digest=evidence_digest)
                if origin['type'] in {'migration', 'copy'}:
                    from .migration_operations import finish
                    finish(operation_id)
                elif origin['type'] in {'banned_cleanup', 'debt_removal'}:
                    from .reseller_removal import finalize
                    for step in report['observed']['accounts']:
                        db.execute("UPDATE account_operation_steps SET phase='verified',result_json=? WHERE operation_id=? AND step_id=?",
                                   (json.dumps({'success': True}), operation_id, step['step_id']))
                    finalize(operation_id)
                elif origin['type'] == 'cleanup':
                    from .cleanup_operations import finalize
                    db.execute("UPDATE account_operation_steps SET phase='verified',result_json=? WHERE operation_id=? AND step_id='delete_panel'",
                               (json.dumps({'success': True}), operation_id))
                    finalize(operation_id)
                elif origin['type'] == 'rename':
                    from .account_rename import finalize
                    db.execute("UPDATE account_operation_steps SET phase='verified',result_json=? WHERE operation_id=? AND step_id='rename_panel'",
                               (json.dumps(report['observed']), operation_id))
                    finalize(operation_id, row['server_id'], row['username'], json.loads(row['request_json']))
                elif origin['type'] == 'trial':
                    from .trial_operations import complete
                    complete(operation_id, notify=True)
                elif origin['type'] == 'funding':
                    from .reseller_funding import finalize_funding
                    obligation = _payment(origin)
                    finalize_funding(origin['reseller_id'], origin['id'], **obligation['fulfillment'])
                elif origin['type'] == 'reseller_reservation':
                    from .reserved_completion import reseller as complete_reseller
                    obligation = _payment(origin)
                    complete_reseller(origin['reseller_id'], origin['id'], obligation['reservation'].get('renewal_claim_id'))
                elif origin['type'] == 'admin':
                    operations.complete(operation_id)
                elif origin['type'] == 'payment' and (_payment(origin) or {}).get('renewal_mode') == 'reserved':
                    from .reserved_completion import payment as complete_reserved
                    from .hosted_bots import tenant_file
                    from .renewal import PAYMENTS_FILE
                    obligation = _payment(origin)
                    path = PAYMENTS_FILE if origin['scope'] == 'main' else tenant_file(origin['scope'].removeprefix('hosted:'), 'payments.json')
                    complete_reserved(origin['id'], obligation.get('renewal_claim_id'), payments_file=path)
                elif origin['type'] == 'payment' and origin['scope'].startswith('hosted:'):
                    from .hosted_settlement import finalize
                    finalize(origin['scope'].removeprefix('hosted:'), origin['id'])
                else:
                    from .operation_completion import main_payment
                    fields = {'username': row['username'], 'server_id': row['server_id']}
                    if row['kind'] == 'renewal':
                        fields.update(renewal_after_state=result.get('after_state'), renewal_before_state=result.get('before_state'))
                    main_payment(origin['id'], fields, notify=True)
        return {**report, 'applied': True}


def inspect_payment_changed(db, origin, report):
    # No panel request is allowed inside the completion transaction.
    if origin.get('type') in {'trial', 'funding', 'reseller_reservation'}:
        return _digest(_payment(origin)) != report['obligation_digest']
    if origin.get('type') == 'admin':
        return False
    row = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                     (origin.get('scope'), origin.get('id'))).fetchone()
    return _digest(json.loads(row[0]) if row else None) != report['obligation_digest']


def _reseller_reservation_matches(origin, obligation):
    from .reserved_completion import reseller_terms
    return bool(obligation and origin.get('terms_digest') == reseller_terms(obligation))
