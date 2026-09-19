"""Durable removal ownership and metadata completion, without Telegram imports."""
import hashlib
import json

from . import account_operations as operations, database, identity_references, state_store
from .account_rename import signature
from .time_utils import format_utc_timestamp


def validate_reference(candidate, server):
    ref = candidate.get('_record_ref') or []
    if not ref:
        return
    db = database.get_connection()
    if ref[0] == 'payment':
        row = db.execute("SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?", (ref[1],)).fetchone()
        record = json.loads(row[0]) if row else {}
    elif ref[0] == 'reseller':
        row = db.execute('SELECT payload_json FROM resellers WHERE reseller_id=?', (str(ref[1]),)).fetchone()
        record = json.loads(row[0])['configs'][int(ref[2])] if row else {}
    elif ref[0] in {'test', 'test_history'}:
        row = db.execute("SELECT value_json FROM kv_state WHERE namespace='test_configs' AND scope='main' AND state_key=?", (str(ref[1]),)).fetchone()
        record = json.loads(row[0]) if row else {}
        if ref[0] == 'test_history':
            record = record.get('historical_configs', [])[int(ref[2])]
    else:
        raise operations.AccountBusy('Cleanup reference provenance is unsupported')
    if (str(record.get('username') or record.get('renewal_username') or '').casefold() != candidate['username'].casefold()
            or str(record.get('server_id') or 'primary') != server):
        raise operations.AccountBusy('Cleanup reference belongs to another account')


def finalize(operation_id):
    row = operations.existing(operation_id)
    request = json.loads(row['request_json'])
    with database.transaction(operation='cleanup_obligation_complete') as db:
        if operations.details(operation_id)['phase'] == 'completed':
            return
        if identity_references.fingerprint(row['server_id'], row['username'], db) != request['references']:
            raise operations.AccountBusy('Cleanup ownership changed after dispatch')
        fields = {**request['metadata'], 'cleanup_status': 'deleted', 'cleanup_delete_result': 'deleted',
                  'cleanup_deleted_at': format_utc_timestamp(), 'cleanup_error': None}
        ref = request['reference']
        if ref:
            if ref[0] == 'payment':
                saved = db.execute("SELECT payload_json FROM payments WHERE scope=? AND payment_id=?", (request['payment_scope'], ref[1])).fetchone()
                if not saved:
                    raise operations.AccountBusy('Cleanup payment reference is unavailable')
                record = json.loads(saved[0])
                state_store._save_payment_record(db, request['payment_scope'], ref[1], {**record, **fields})
            elif ref[0] == 'reseller':
                saved = db.execute('SELECT payload_json FROM resellers WHERE reseller_id=?', (str(ref[1]),)).fetchone()
                record = json.loads(saved[0])
                record['configs'][int(ref[2])].update(fields)
                state_store._save_reseller_record(db, str(ref[1]), record)
            elif ref[0] in {'test', 'test_history'}:
                saved = db.execute("SELECT value_json FROM kv_state WHERE namespace='test_configs' AND scope='main' AND state_key=?", (str(ref[1]),)).fetchone()
                record = json.loads(saved[0])
                target = record if ref[0] == 'test' else record['historical_configs'][int(ref[2])]
                target.update(fields)
                db.execute("UPDATE kv_state SET value_json=? WHERE namespace='test_configs' AND scope='main' AND state_key=?", (json.dumps(record), str(ref[1])))
            else:
                raise operations.AccountBusy('Unsupported cleanup reference')
        key = row['server_id'] + ':' + row['username']
        saved = db.execute("SELECT value_json FROM kv_state WHERE namespace='expired_cleanup' AND scope='main' AND state_key=?", (key,)).fetchone()
        state = json.loads(saved[0]) if saved else {}
        state.update(request['candidate'])
        state.update(cleanup_status='deleted', delete_result='deleted', deleted_at=fields['cleanup_deleted_at'],
                     account_operation_id=operation_id, last_state=request['metadata'].get('cleanup_last_state'))
        db.execute("""INSERT INTO kv_state(namespace,scope,state_key,value_json) VALUES ('expired_cleanup','main',?,?)
            ON CONFLICT(namespace,scope,state_key) DO UPDATE SET value_json=excluded.value_json""", (key, json.dumps(state)))
        from . import web_store
        web_store.initialize()
        recipient = request['candidate'].get('telegram_user_id') or request['candidate'].get('reseller_id')
        if recipient:
            lang_row = db.execute("SELECT value_json FROM kv_state WHERE namespace='user_languages' AND scope='main' AND state_key=?", (str(recipient),)).fetchone()
            language = json.loads(lang_row[0]) if lang_row else 'en'
            messages = {'en': 'An expired connection has been removed. Open your connections for the current status.',
                        'fa': 'یک اتصال منقضی‌شده حذف شد. برای وضعیت فعلی، اتصال‌های خود را باز کنید.',
                        'ru': 'Подключение с истёкшим сроком удалено. Откройте список подключений.',
                        'tk': 'Möhleti geçen birikme aýryldy. Birikmeleriňiziň sanawyny açyň.'}
            web_store.enqueue(db, 'cleanup:' + operation_id, request['payment_scope'], recipient, messages.get(language, messages['en']))
        operations.event(db, operation_id, 'cleanup_metadata_committed')
        operations.complete(operation_id)


def remove(client, candidate, expected_user, metadata, eligible, *, actor='scheduler'):
    """Recheck live entitlement and current obligations before one removal attempt."""
    server, username = str(client.server_id), candidate['username']
    generation = signature(expected_user)
    ref = list(candidate.get('_record_ref') or [])
    ident = 'cleanup:' + hashlib.sha256(json.dumps([server, username, generation, ref], sort_keys=True).encode()).hexdigest()
    with operations.serialize(server, username):
        old = operations.existing(ident)
        if old:
            request = json.loads(old['request_json'])
        else:
            operations.assert_available(server, username)
            operations.assert_no_pending_obligations(server, username)
            validate_reference(candidate, server)
            current = client.get_user_result(username)
            if current.get('status') != 'found' or signature(current['data']) != generation or not eligible(current['data']):
                raise operations.AccountBusy('Cleanup eligibility or account generation changed')
            request = {'generation': generation, 'references': identity_references.fingerprint(server, username),
                       'reference': ref, 'metadata': metadata, 'payment_scope': 'main',
                       'candidate': {key: candidate.get(key) for key in ('username', 'server_id', 'source', 'telegram_user_id', 'reseller_id', 'cleanup_reason')}}
        def dispatch():
            operations.assert_no_pending_obligations(server, username)
            current = client.get_user_result(username)
            if (current.get('status') != 'found' or signature(current['data']) != request['generation']
                    or not eligible(current['data']) or identity_references.fingerprint(server, username) != request['references']):
                raise operations.AccountBusy('Cleanup eligibility changed before dispatch')
            def panel_step():
                client.delete_user(username)
                return {'success': client.get_user_result(username).get('status') == 'missing'}
            operations.step(ident, 'delete_panel', {'generation': generation, 'server': server, 'username': username}, panel_step)
            return {'success': True}
        result = operations.execute(ident, server, username, 'delete', request, dispatch,
                                    origin={'type': 'cleanup', 'scope': 'main', 'id': ident, 'actor': str(actor)})
        if result.get('success'):
            finalize(ident)
        return result
