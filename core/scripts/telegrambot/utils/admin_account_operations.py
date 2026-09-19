"""Administrator panel changes serialized with customer financial operations."""
import json

from . import account_operations as operations, database


def generation(user):
    return {key: user.get(key) for key in ('username', 'account_creation_date',
            'expiration_days', 'max_download_bytes', 'unlimited_ip')}


def mutate(operation_id, client, username, *, kind='update', changes=None, actor='admin'):
    """Persist one explicit admin command; never repeat an uncertain command."""
    if not client:
        return None
    changes = changes or {}
    def call():
        if kind == 'delete':
            return client.delete_user(username)
        if kind == 'reset':
            return client.reset_user(username)
        return client.update_user(username, changes)
    if not operations.enabled():
        return call()
    try:
        if changes.get('new_username'):
            from .account_rename import rename
            return rename(operation_id, client, username, changes['new_username'], actor)
        resources = [(client.server_id, username)]
        if changes.get('new_username'):
            resources.append((client.server_id, changes['new_username']))
        with operations.serialize_many(resources):
            previous = operations.existing(operation_id)
            operations.assert_available(client.server_id, username, operation_id=operation_id)
            if previous:
                intent = json.loads(previous['request_json'])
                if intent.get('changes') != changes:
                    raise operations.AccountBusy('Admin command changed')
            else:
                user = client.get_user(username)
                if not user or str(user.get('username', '')).casefold() != str(username).casefold():
                    raise operations.AccountBusy('Admin account identity is unverified')
                intent = {'before': generation(user), 'changes': changes}
            result = operations.execute(operation_id, client.server_id, username, kind, intent,
                lambda: {'success': call() is not None},
                origin={'type': 'admin', 'scope': 'main', 'id': operation_id, 'actor': str(actor)},
                resources=resources)
            if not result.get('success'):
                return None
            with database.transaction(operation='admin_account_complete') as db:
                operations.event(db, operation_id, 'admin_effect_committed', actor=actor, reason=kind)
                operations.complete(operation_id)
            return result
    except operations.AccountBusy:
        return None


def from_telegram(event, client, username, *, kind='update', changes=None):
    if not operations.enabled():
        return mutate('', client, username, kind=kind, changes=changes)
    from .command import is_admin
    if not is_admin(event.from_user.id):
        return None
    message = getattr(event, 'message', event)
    actor = event.from_user.id
    action = ','.join(sorted(changes or {})) or kind
    ident = f'admin:{message.chat.id}:{message.message_id}:{action}'
    return mutate(ident, client, username, kind=kind, changes=changes, actor=actor)


def create_from_telegram(event, panels, client, username, gb, days, unlimited):
    if not operations.enabled():
        return client, client.add_user(username, gb, days, unlimited)
    from .command import is_admin
    from .account_mutations import create
    if not is_admin(event.from_user.id):
        return client, None
    ident = f'admin-create:{event.message.chat.id}:{event.message.message_id}'
    try:
        def allocate(existing):
            if username.casefold() in {str(name).casefold() for name in existing}:
                raise operations.AccountBusy('The account already exists')
            return username
        _, result, client = create(ident, panels, allocate,
            {'gb': gb, 'days': days, 'unlimited': unlimited},
            origin={'type': 'admin', 'scope': 'main', 'id': ident, 'actor': str(event.from_user.id)})
        if result:
            operations.complete(ident)
        return client, result
    except operations.AccountBusy:
        return client, None
