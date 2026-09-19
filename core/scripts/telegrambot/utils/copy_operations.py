"""Durable administrator copy using the same panel steps as migration."""
import json

from . import account_operations as operations, database
from .account_rename import signature


def copy(operation_id, panels, spec, actor):
    source = panels.get_client(spec.source.server_id)
    destination = panels.get_client(spec.destination_server_id)
    if source is None or destination is None:
        return {'ok': False, 'error': 'server_unavailable'}
    resources = [(source.server_id, spec.source.username), (destination.server_id, spec.source.username)]
    with operations.serialize_many(resources):
        previous = operations.existing(operation_id)
        if previous:
            request = json.loads(previous['request_json'])
            if request['destination_server'] != spec.destination_server_id or request['inbound_ids'] != list(spec.inbound_ids):
                raise operations.AccountBusy('Copy destination changed')
        else:
            found = source.get_user_result(spec.source.username)
            if found.get('status') != 'found':
                return {'ok': False, 'error': 'source_unavailable'}
            request = {'source_snapshot': json.loads(json.dumps(found['data'], default=str)), 'generation': signature(found['data']),
                       'destination_server': spec.destination_server_id, 'inbound_ids': list(spec.inbound_ids), 'mode': 'copy'}
        captured = {}
        def action():
            captured.update(panels.copy_user(spec))
            return {'success': bool(captured.get('ok'))}
        result = operations.execute(operation_id, source.server_id, spec.source.username, 'copy', request, action,
                                   origin={'type': 'copy', 'scope': 'main', 'id': operation_id, 'actor': str(actor)}, resources=resources)
        if not result.get('success'):
            return captured or {'ok': False, 'error': 'copy_requires_reconciliation'}
        with database.transaction(operation='admin_copy_complete') as db:
            operations.event(db, operation_id, 'copy_completed', actor=str(actor))
            operations.complete(operation_id)
        if captured:
            return captured
        uri = destination.get_user_uri(spec.source.username) or {}
        return {'ok': True, 'username': spec.source.username, 'destination_server_id': destination.server_id,
                'destination_server_name': destination.server_name, 'panel_type': destination.panel_type, **uri}
