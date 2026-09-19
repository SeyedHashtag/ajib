"""Account claims spanning destination creation, reference moves and source removal."""
import json

from . import account_operations as operations, database, identity_references
from .account_rename import signature


def process_item(job, item, panels, *, path=None):
    from . import bulk_transfer as transfer
    ident = f"migration:{job['job_id']}:{item['ordinal']}"
    resources = [(job['source_server_id'], item['username']), (job['destination_server_id'], item['username'])]
    with operations.serialize_many(resources):
        prior = operations.existing(ident)
        if prior:
            request = json.loads(prior['request_json'])
        else:
            if item['stage'] != 'pending':
                raise operations.AccountBusy('Legacy migration requires verified provenance')
            source = panels.get_client(job['source_server_id'])
            result = source.get_user_result(item['username']) if source else {'status': 'unavailable'}
            if result.get('status') != 'found':
                return transfer._process_item_unclaimed(job, item, panels, path=path)
            operations.assert_no_pending_obligations(job['source_server_id'], item['username'])
            if identity_references.references(job['destination_server_id'], item['username']):
                raise operations.AccountBusy('Destination has existing ownership history')
            request = {'job_id': job['job_id'], 'ordinal': item['ordinal'], 'mode': job['mode'],
                       'destination_server': job['destination_server_id'], 'source_snapshot': json.loads(json.dumps(result['data'], default=str)),
                       'generation': signature(result['data']),
                       'references': identity_references.fingerprint(job['source_server_id'], item['username'])}
        def dispatch():
            current = item
            for _ in range(8):
                transfer._process_item_unclaimed(job, current, panels, path=path)
                current = dict(database.get_connection(path).execute('SELECT * FROM bulk_transfer_items WHERE job_id=? AND ordinal=?',
                               (job['job_id'], item['ordinal'])).fetchone())
                if database.get_connection(path).execute("SELECT 1 FROM account_operation_steps WHERE operation_id=? AND step_id='domain_ready' AND phase='verified'", (ident,)).fetchone():
                    return {'success': True}
                if current['stage'] in {'failed', 'manual_review', 'skipped'}:
                    raise operations.AccountBusy('Migration requires investigation')
            raise operations.AccountBusy('Migration did not reach verified completion')
        operations.execute(ident, job['source_server_id'], item['username'], 'migration', request, dispatch,
                           origin={'type': 'migration', 'scope': 'main', 'id': ident, 'job_id': job['job_id'],
                                   'ordinal': item['ordinal'], 'actor': str(job['requested_by'])}, resources=resources)
        with database.transaction(operation='migration_complete') as db:
            db.execute("UPDATE bulk_transfer_items SET stage='completed',error_code=NULL,completed_at=?,updated_at=? WHERE job_id=? AND ordinal=?",
                       (transfer.format_utc_timestamp(), transfer.format_utc_timestamp(), job['job_id'], item['ordinal']))
            transfer._release_item_notifications(db, job, item, transfer.format_utc_timestamp())
            operations.complete(ident)
        return True


def verify_destination(job, item, destination_data):
    from . import bulk_transfer as transfer
    ident = operations.active_workflow()
    if not ident:
        raise operations.AccountBusy('Migration claim is required')
    request = json.loads(operations.existing(ident)['request_json'])
    matched, _ = transfer._interrupted_destination_match(job, request['source_snapshot'], destination_data,
                                                        item.get('destination_panel_type') or 'blitz', item['username'])
    if not matched:
        raise operations.AccountBusy('Copied account identity or entitlement could not be verified')
    operations.assert_no_pending_obligations(job['source_server_id'], item['username'])
    operations.assert_no_pending_obligations(job['destination_server_id'], item['username'])
    assert_references(ident, request, operations.existing(ident))
    return request


def assert_references(operation_id, request, operation):
    if not request.get('job_id'):
        return
    moved = database.get_connection().execute("SELECT result_json FROM account_operation_steps WHERE operation_id=? AND step_id='move_references' AND phase='verified'", (operation_id,)).fetchone()
    expected = json.loads(moved[0]).get('destination_references') if moved else request['references']
    server = request['destination_server'] if moved else operation['server_id']
    if identity_references.fingerprint(server, operation['username']) != expected:
        raise operations.AccountBusy('Migration ownership or obligations changed')


def move_references(job, item):
    from . import bulk_transfer as transfer
    ident = operations.active_workflow()
    if not ident:
        raise operations.AccountBusy('Migration claim is required')
    request = json.loads(operations.existing(ident)['request_json'])
    with database.transaction(operation='migration_references') as db:
        refs = identity_references.references(job['source_server_id'], item['username'], db)
        recipients = set()
        for ref in refs:
            record, keys = ref['record'], ref['keys']
            if ref['table'] == 'payments' and record.get('user_id'):
                recipients.add((keys['scope'], str(record['user_id'])))
            elif ref['table'] == 'resellers':
                for config in record.get('configs', []):
                    if config.get('username') == item['username'] and config.get('server_id') == job['source_server_id'] and config.get('customer_telegram_id'):
                        recipients.add(('hosted:' + keys['reseller_id'], str(config['customer_telegram_id'])))
            elif ref['table'] == 'kv_state' and keys['namespace'] == 'test_configs':
                recipients.add((record.get('trial_scope') or 'main', str(record.get('telegram_id') or keys['state_key'])))
        identity_references.move(ident, job['source_server_id'], item['username'], job['destination_server_id'], item['username'], request['references'])
        now = transfer.format_utc_timestamp()
        for scope, recipient in recipients:
            transfer._insert_recipient(db, job, item, scope, recipient, now)
        db.execute("INSERT OR IGNORE INTO account_operation_steps VALUES (?,'move_references','verified',?,?,strftime('%s','now'))",
                   (ident, json.dumps({'before': request['references']}), json.dumps({'success': True,
                    'destination_references': identity_references.fingerprint(job['destination_server_id'], item['username'], db)})))
        db.execute("UPDATE bulk_transfer_items SET stage='records_updated',records_updated=?,recipient_count=?,updated_at=? WHERE job_id=? AND ordinal=?",
                   (len(refs), len(recipients), now, job['job_id'], item['ordinal']))
        return {'records_updated': len(refs), 'recipients': len(recipients)}


def inspect(operation_id, panels):
    from . import bulk_transfer as transfer
    operation = operations.existing(operation_id)
    request = json.loads(operation['request_json'])
    source = panels.get_client(operation['server_id'])
    destination = panels.get_client(request['destination_server'])
    src = source.get_user_result(operation['username']) if source else {'status': 'unavailable'}
    dst = destination.get_user_result(operation['username']) if destination else {'status': 'unavailable'}
    job = transfer.get_job(request['job_id']) if request.get('job_id') else {
        'mode': 'copy', 'destination_server_id': request['destination_server'], 'inbound_ids': request.get('inbound_ids', [])}
    report = {'source': src.get('status'), 'destination': dst.get('status'), 'action': 'none', 'reason': 'migration_outcome_unproven'}
    if not job:
        return report
    matched = dst.get('status') == 'found' and transfer._interrupted_destination_match(
        job, request['source_snapshot'], dst['data'], destination.panel_type, operation['username'])[0]
    report['destination_verified'] = bool(matched)
    try:
        source_matches = src.get('status') == 'found' and signature(src['data']) == request['generation']
    except operations.AccountBusy:
        source_matches = False
    report['source_generation_matches'] = source_matches
    if operation['kind'] == 'copy':
        if matched and source_matches:
            report.update(action='complete_accounting', reason='copied_identity_verified')
        return report
    db = database.get_connection()
    moved = db.execute("SELECT result_json FROM account_operation_steps WHERE operation_id=? AND step_id='move_references' AND phase='verified'", (operation_id,)).fetchone()
    references_match = (identity_references.fingerprint(request['destination_server'], operation['username']) == json.loads(moved[0]).get('destination_references')) if moved else (
        identity_references.fingerprint(operation['server_id'], operation['username']) == request['references'])
    report['references_match'] = references_match
    if not references_match:
        report['reason'] = 'migration_references_changed'
        return report
    if matched and ((job['mode'] == 'copy' and source_matches) or (moved and src.get('status') == 'missing')):
        report.update(action='complete_accounting', reason='migration_effects_verified')
    elif matched and source_matches:
        deletion = db.execute("SELECT phase FROM account_operation_steps WHERE operation_id=? AND step_id='delete_source'", (operation_id,)).fetchone()
        if deletion is None and job.get('status') not in {'cancelled', 'cancel_requested'}:
            report.update(action='resume_migration', reason='remaining_source_delete_never_dispatched')
    elif (operations.details(operation_id)['phase'] in {'prepared', 'ready'} and source_matches
          and dst.get('status') == 'missing' and job.get('status') not in {'cancelled', 'cancel_requested'}):
        report.update(action='resume_migration', reason='migration_never_dispatched')
    return report


def finish(operation_id):
    from . import bulk_transfer as transfer
    operation = operations.existing(operation_id)
    request = json.loads(operation['request_json'])
    with database.transaction(operation='migration_reconciled_completion') as db:
        assert_references(operation_id, request, operation)
        # The caller has just verified the entire intended destination state.
        db.execute("UPDATE account_operation_steps SET phase='verified',result_json=COALESCE(result_json,'{\"success\":true}') WHERE operation_id=? AND phase!='verified'", (operation_id,))
        if request.get('job_id'):
            job = transfer.get_job(request['job_id'])
            item = dict(db.execute('SELECT * FROM bulk_transfer_items WHERE job_id=? AND ordinal=?',
                                   (request['job_id'], request['ordinal'])).fetchone())
            db.execute("UPDATE bulk_transfer_items SET stage='completed',error_code=NULL,completed_at=?,updated_at=? WHERE job_id=? AND ordinal=?",
                       (transfer.format_utc_timestamp(), transfer.format_utc_timestamp(), request['job_id'], request['ordinal']))
            transfer._release_item_notifications(db, job, item, transfer.format_utc_timestamp())
        operations.complete(operation_id)


def resume(operation_id, panels, expected_revision, *, actor='cli', reason='resume_original_migration_owner', evidence_digest=None):
    from . import bulk_transfer as transfer
    row = operations.existing(operation_id)
    request = json.loads(row['request_json'])
    with database.transaction(operation='migration_resume_original_owner') as db:
        if operations.details(operation_id)['revision'] != expected_revision:
            raise operations.AccountBusy('Migration revision changed')
        assert_references(operation_id, request, row)
        job = transfer.get_job(request['job_id'])
        item = dict(db.execute('SELECT * FROM bulk_transfer_items WHERE job_id=? AND ordinal=?', (request['job_id'], request['ordinal'])).fetchone())
        has_steps = db.execute("SELECT 1 FROM account_operation_steps WHERE operation_id=? AND phase!='prepared'", (operation_id,)).fetchone()
        moved = db.execute("SELECT 1 FROM account_operation_steps WHERE operation_id=? AND step_id='move_references'", (operation_id,)).fetchone()
        stage = 'records_updated' if moved else 'copied' if has_steps else 'pending'
        if has_steps:
            db.execute("UPDATE account_operation_steps SET phase='verified',result_json=COALESCE(result_json,'{\"success\":true}') WHERE operation_id=? AND step_id!='delete_source' AND phase!='verified'", (operation_id,))
        db.execute('UPDATE bulk_transfer_items SET stage=?,error_code=NULL,completed_at=NULL WHERE job_id=? AND ordinal=?', (stage, request['job_id'], request['ordinal']))
        operations.transition(db, operation_id, 'ready', actor=actor, reason=reason, evidence_digest=evidence_digest)
        item['stage'] = stage
    return process_item(job, item, panels)
