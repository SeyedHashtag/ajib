"""Verified rename with both identities claimed until ownership commits."""
import hashlib
import json
import re

from . import account_operations as operations, database, identity_references as references


def signature(user):
    if not isinstance(user, dict) or not (user.get('account_creation_date') or any(user.get(key) for key in ('password', 'uuid', 'id', 'auth'))):
        raise operations.AccountBusy('Account generation cannot be verified')
    fields = ('account_creation_date', 'expiration_days', 'max_download_bytes', 'unlimited_ip', 'unlimited_user',
              'password', 'uuid', 'id', 'auth', 'blocked')
    return hashlib.sha256(json.dumps({key: user.get(key) for key in fields}, sort_keys=True, default=str).encode()).hexdigest()


def evidence(client, username, request):
    source = client.get_user_result(username)
    destination = client.get_user_result(request['destination'])
    try:
        verified = (source.get('status') == 'missing' and destination.get('status') == 'found'
                    and str(destination['data'].get('username', '')).casefold() == request['destination'].casefold()
                    and signature(destination['data']) == request['signature'])
    except operations.AccountBusy:
        verified = False
    return {'success': verified, 'source': source.get('status'), 'destination': destination.get('status'),
            'reference_digest': references.fingerprint(str(client.server_id), username)}


def finalize(operation_id, client_server, username, request):
    with database.transaction(operation='account_rename_complete'):
        references.move(operation_id, str(client_server), username, str(client_server), request['destination'], request['references'])
        # Point to the portal without embedding a configuration that may expire.
        from . import web_store
        web_store.initialize()
        owners = database.get_connection().execute('SELECT scope,user_id FROM account_identity_owners WHERE server_id=? AND username_key=? AND retired=0',
                                                   (str(client_server), request['destination'].casefold())).fetchall()
        for owner in owners:
            if owner['user_id']:
                language_row = database.get_connection().execute('SELECT value_json FROM kv_state WHERE namespace=? AND scope=? AND state_key=?',
                    ('user_languages' if owner['scope'] == 'main' else 'hosted_languages', owner['scope'], owner['user_id'])).fetchone()
                language = json.loads(language_row[0]) if language_row else 'en'
                messages = {'en': 'Your connection has been updated. Open your connections to view it.',
                            'fa': 'اتصال شما به‌روزرسانی شد. برای مشاهده، اتصال‌های خود را باز کنید.',
                            'ru': 'Ваше подключение обновлено. Откройте список подключений.',
                            'tk': 'Birikmäňiz täzelendi. Birikmeleriňiziň sanawyny açyň.'}
                web_store.enqueue(database.get_connection(), f'rename:{operation_id}:{owner["scope"]}:{owner["user_id"]}',
                                  owner['scope'], owner['user_id'], messages.get(language, messages['en']))
        operations.complete(operation_id)


def rename(operation_id, client, username, destination, actor):
    if not callable(getattr(client, 'get_user_result', None)):
        raise operations.AccountBusy('Verified panel lookup is required for renaming')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', destination) or destination.casefold() == username.casefold():
        raise operations.AccountBusy('Choose a different valid destination name')
    resources = [(str(client.server_id), username), (str(client.server_id), destination)]
    with operations.serialize_many(resources):
        old = operations.existing(operation_id)
        if old:
            request = json.loads(old['request_json'])
            if request.get('destination') != destination:
                raise operations.AccountBusy('Rename destination changed')
        else:
            for server, name in resources:
                operations.assert_available(server, name)
            references.destination_unused(str(client.server_id), destination)
            source = client.get_user_result(username)
            target = client.get_user_result(destination)
            if source.get('status') != 'found' or target.get('status') != 'missing':
                raise operations.AccountBusy('Source or destination could not be verified')
            request = {'destination': destination, 'signature': signature(source['data']),
                       'references': references.fingerprint(str(client.server_id), username)}
        def dispatch():
            def panel_step():
                client.update_user(username, {'new_username': destination})
                return evidence(client, username, request)
            operations.step(operation_id, 'rename_panel', request, panel_step)
            return {'success': True}
        result = operations.execute(operation_id, client.server_id, username, 'rename', request, dispatch,
                                    origin={'type': 'rename', 'scope': 'main', 'id': operation_id, 'actor': str(actor)}, resources=resources)
        if result.get('success'):
            finalize(operation_id, client.server_id, username, request)
        return result
