"""Live release admission policy. Shared SQLite state, no transport imports."""
import json
import time
from . import database, web_store


def policy(settings, connection=None):
    db = connection or database.get_connection()
    row = db.execute('SELECT * FROM web_release_control WHERE id=1').fetchone()
    if row is None:
        return {'access': 'public' if settings.public_portal else 'pilot',
                'pilot_users': settings.pilot_users, 'accept_writes': settings.writes_enabled,
                'process_existing': settings.writes_enabled, 'revision': '', 'pilot_started_at': None}
    return {**dict(row), 'pilot_users': frozenset(json.loads(row['pilot_users_json'])),
            'accept_writes': bool(row['accept_writes']), 'process_existing': bool(row['process_existing'])}


def save(connection, values, actor='cli'):
    access = values['access']
    users = sorted(set(str(user) for user in values['pilot_users']))
    if access not in {'admin', 'pilot', 'public'} or any(not user.isdigit() or int(user) <= 0 for user in users):
        raise ValueError('Invalid release access policy')
    connection.execute('''INSERT INTO web_release_control
        (id,access,pilot_users_json,accept_writes,process_existing,revision,pilot_started_at,updated_at)
        VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        access=excluded.access,pilot_users_json=excluded.pilot_users_json,
        accept_writes=excluded.accept_writes,process_existing=excluded.process_existing,
        revision=excluded.revision,pilot_started_at=excluded.pilot_started_at,updated_at=excluded.updated_at''',
        (access, json.dumps(users), int(values['accept_writes']), int(values['process_existing']),
         values['revision'], values.get('pilot_started_at'), int(time.time())))
    web_store.audit(connection, actor, 'main', 'release.policy', values['revision'],
                    {'access': access, 'accept_writes': bool(values['accept_writes']),
                     'process_existing': bool(values['process_existing'])})


def permits(identity, current):
    return ('admin' in identity['roles'] or (current['access'] == 'public' and identity['scope'] == 'main')
            or (current['access'] == 'pilot' and identity['user_id'] in current['pilot_users']))
