"""Atomic operational reference moves; original identities remain immutable."""
import copy
import hashlib
import json
import re
import time

from . import account_operations as operations, database, state_store

HISTORY_FIELDS = {'updates', 'history', 'identity_history', 'before_state', 'after_state',
                  'renewal_before_state', 'renewal_after_state', 'last_state'}
NAME_FIELDS = {'username': 'server_id', 'renewal_username': 'renewal_server_id',
               'renew_username': 'server_id', 'provisioned_username': 'provisioned_server_id',
               'replacement_from_username': 'replacement_from_server_id'}
NAMESPACES = {'test_configs', 'expired_cleanup', 'traffic_alerts'}


def _move(value, old_server, old_name, new_server, new_name, inherited=None, inherited_name=None):
    if isinstance(value, list):
        return any([_move(item, old_server, old_name, new_server, new_name, inherited, inherited_name) for item in value])
    if not isinstance(value, dict):
        return False
    changed = False
    original_server = value.get('server_id') or inherited or 'primary'
    original_name = next((value[key] for key in NAME_FIELDS if value.get(key)), inherited_name)
    for name_key, server_key in NAME_FIELDS.items():
        server = value.get(server_key) or original_server
        if str(value.get(name_key, '')).casefold() == old_name.casefold() and str(server) == old_server:
            value[name_key], value[server_key] = new_name, new_server
            changed = True
    if original_name and str(original_name).casefold() == old_name.casefold():
        for key in ('server_id', 'renewal_server_id', 'renewal_recorded_server_id', 'provisioned_server_id'):
            if str(value.get(key)) == old_server:
                value[key] = new_server
                changed = True
    for key, child in value.items():
        if key not in HISTORY_FIELDS:
            changed = _move(child, old_server, old_name, new_server, new_name, original_server, original_name) or changed
    return changed


def references(server, username, db=None):
    db = db or database.get_connection()
    found = []
    tables = [('payments', ('scope', 'payment_id'), 'payload_json'),
                                ('resellers', ('reseller_id',), 'payload_json'),
              ('kv_state', ('namespace', 'scope', 'state_key'), 'value_json')]
    existing_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, keys, column in [('web_operations', ('id',), 'payload_json'),
                              ('web_actions', ('scope', 'user_id', 'kind', 'key'), 'result_json')]:
        if table in existing_tables:
            tables.append((table, keys, column))
    for table, keys, column in tables:
        for row in db.execute(f'SELECT * FROM {table}').fetchall():
            if table == 'kv_state' and row['namespace'] not in NAMESPACES:
                continue
            record = json.loads(row[column])
            if _move(copy.deepcopy(record), str(server), str(username), str(server), str(username)):
                found.append({'table': table, 'keys': {key: row[key] for key in keys}, 'record': record})
    for table, keys in [('web_trials', ('id',))]:
        if table not in existing_tables:
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        # Bulk notification schema versions differ; only typed account routes
        # can be moved. Never rewrite free-form queued text or configuration.
        if not {'username', 'server_id', *keys} <= columns:
            continue
        for row in db.execute(f'SELECT * FROM {table} WHERE server_id=? AND lower(username)=?', (server, username.casefold())):
            found.append({'table': table, 'keys': {key: row[key] for key in keys}, 'record': dict(row)})
    for row in db.execute('''SELECT n.*,COALESCE(n.account_server_id,j.destination_server_id) AS server_id
        FROM bulk_transfer_notifications n JOIN bulk_transfer_jobs j ON n.job_id=j.job_id
        WHERE COALESCE(n.account_server_id,j.destination_server_id)=? AND lower(n.username)=?
        AND n.status IN ('held','pending','sending')''', (server, username.casefold())):
        found.append({'table': 'bulk_transfer_notifications', 'keys': {'notification_id': row['notification_id']}, 'record': dict(row)})
    return found


def fingerprint(server, username, db=None):
    return hashlib.sha256(json.dumps(references(server, username, db), sort_keys=True).encode()).hexdigest()


def destination_unused(server, username):
    scopes = [row[0] for row in database.get_connection().execute('SELECT DISTINCT scope FROM payments')]
    if str(username).casefold() in {name.casefold() for name in state_store.query_recorded_usernames(scopes=scopes)}:
        raise operations.AccountBusy('Destination has existing account history')
    if database.get_connection().execute('SELECT 1 FROM account_identity_owners WHERE username_key=?',
                                         (str(username).casefold(),)).fetchone():
        raise operations.AccountBusy('Destination has prior identity ownership')


def move(operation_id, source_server, source_name, destination_server, destination_name, expected):
    """Caller verifies panel identity/entitlement while holding both claims first."""
    with database.transaction(operation='identity_reference_move') as db:
        prior = db.execute('SELECT * FROM account_identity_history WHERE operation_id=?', (operation_id,)).fetchone()
        if prior:
            if tuple(prior[key] for key in ('source_server', 'source_username', 'destination_server', 'destination_username')) != (
                    source_server, source_name, destination_server, destination_name):
                raise operations.AccountBusy('Identity move conflicts with immutable history')
            return
        if fingerprint(source_server, source_name, db) != expected:
            raise operations.AccountBusy('Account ownership or obligations changed after dispatch')
        records = references(source_server, source_name, db)
        owners = set()
        for ref in records:
            record, keys, table = copy.deepcopy(ref['record']), ref['keys'], ref['table']
            _move(record, source_server, source_name, destination_server, destination_name)
            if table == 'payments':
                state_store._save_payment_record(db, keys['scope'], keys['payment_id'], record)
                if record.get('user_id') is not None:
                    owners.add((keys['scope'], str(record['user_id'])))
            elif table == 'resellers':
                state_store._save_reseller_record(db, keys['reseller_id'], record)
                for config in record.get('configs', []):
                    if config.get('username') == destination_name and config.get('server_id') == destination_server:
                        customer = config.get('customer_telegram_id') or config.get('customer_id')
                        if customer:
                            owners.add(('hosted:' + keys['reseller_id'], str(customer)))
            elif table in {'web_operations', 'web_actions', 'web_trials', 'bulk_transfer_notifications'}:
                where = ' AND '.join(key + '=?' for key in keys)
                if table in {'web_trials', 'bulk_transfer_notifications'}:
                    server_column = 'account_server_id' if table == 'bulk_transfer_notifications' else 'server_id'
                    db.execute(f'UPDATE {table} SET username=?,{server_column}=? WHERE {where}',
                               (destination_name, destination_server, *keys.values()))
                else:
                    column = 'payload_json' if table == 'web_operations' else 'result_json'
                    db.execute(f'UPDATE {table} SET {column}=? WHERE {where}', (json.dumps(record), *keys.values()))
            else:
                new_key = keys['state_key']
                if new_key.casefold() == f'{source_server}:{source_name}'.casefold():
                    new_key = f'{destination_server}:{destination_name}'
                if new_key != keys['state_key'] and db.execute(
                        'SELECT 1 FROM kv_state WHERE namespace=? AND scope=? AND state_key=?',
                        (keys['namespace'], keys['scope'], new_key)).fetchone():
                    raise operations.AccountBusy('Destination already has lifecycle state')
                db.execute('UPDATE kv_state SET state_key=?,value_json=? WHERE namespace=? AND scope=? AND state_key=?',
                           (new_key, json.dumps(record), keys['namespace'], keys['scope'], keys['state_key']))
                if keys['namespace'] == 'test_configs':
                    owners.add((record.get('trial_scope', 'main'), str(record.get('telegram_id') or keys['state_key'])))
        # Preserve legacy identity only when no explicit hosted/customer record overrides it.
        match = re.match(r'^(?:s|t)(\d+)[a-z]*$|^(?:sell|test)?(\d+)t', source_name, re.I)
        if not owners and match:
            owners.add(('main', next(value for value in match.groups() if value)))
        owners = owners or {('main', '')}
        revision = int(time.time_ns())
        for server, name, retired in [(source_server, source_name, 1), (destination_server, destination_name, 0)]:
            for scope, user in owners:
                db.execute('''INSERT INTO account_identity_owners VALUES (?,?,?,?,?,?)
                    ON CONFLICT(server_id,username_key,scope,user_id) DO UPDATE SET retired=excluded.retired,revision=excluded.revision''',
                           (server, name.casefold(), scope, user, retired, revision))
        db.execute('INSERT INTO account_identity_history(operation_id,source_server,source_username,destination_server,destination_username,references_json,created_at) VALUES (?,?,?,?,?,?,?)',
                   (operation_id, source_server, source_name, destination_server, destination_name, json.dumps(records), int(time.time())))
        operations.event(db, operation_id, 'identity_references_committed')


def ownership(username, user_id, scope='main', server=None):
    """None permits legacy matching; False forbids matching a retired alias."""
    if not operations.enabled():
        return None
    rows = database.get_connection().execute('SELECT * FROM account_identity_owners WHERE username_key=?',
                                             (str(username).casefold(),)).fetchall()
    if not rows:
        return None
    return any(not row['retired'] and row['scope'] == scope and row['user_id'] == str(user_id)
               and (server is None or row['server_id'] == str(server)) for row in rows)


def revision(server, username):
    if not operations.enabled():
        return 0
    return database.get_connection().execute('SELECT COALESCE(MAX(revision),0) FROM account_identity_owners WHERE server_id=? AND username_key=?',
                                             (str(server), str(username).casefold())).fetchone()[0]


def is_trial(username, server):
    if operations.enabled():
        for row in database.get_connection().execute("SELECT value_json FROM kv_state WHERE namespace='test_configs'"):
            record = json.loads(row[0])
            if (str(record.get('username', '')).casefold() == str(username).casefold()
                    and str(record.get('server_id') or 'primary') == str(server)):
                return True
        if database.get_connection().execute('SELECT 1 FROM account_identity_owners WHERE username_key=? AND server_id=?',
                                              (str(username).casefold(), str(server))).fetchone():
            return False
    return str(username or '').lower().startswith('t')
