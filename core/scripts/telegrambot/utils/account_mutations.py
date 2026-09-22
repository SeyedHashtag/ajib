"""Account creation with one persisted allocation and one external attempt."""
import json

from . import account_operations as operations
from .username_utils import build_user_note


def create(operation_id, panels, allocator, plan, *, origin=None, note_text='',
           on_allocated=None, resources=()):
    previous = operations.existing(operation_id)
    if previous:
        client = panels.get_client(previous['server_id'])
        username = previous['username']
        request = json.loads(previous['request_json'])
        if (str(plan['gb']), int(plan['days']), bool(plan.get('unlimited'))) != (
                str(request['plan_gb']), int(request['days']), bool(request['unlimited'])):
            raise operations.AccountBusy('The plan differs from the persisted allocation')
    else:
        placement = panels.prepare_new_user_creation(force_refresh=True)
        client = placement.get('client')
        if client is None:
            return None, None, None
        username = allocator(set(placement.get('existing_usernames') or ()))
        marker = 'ajib-op:' + operation_id
        note = build_user_note(username, plan['gb'], plan['days'], unlimited=plan.get('unlimited', False),
                               note_text=(note_text + '; ' + marker).strip('; '))
        request = {'plan_gb': str(plan['gb']), 'days': int(plan['days']),
                   'unlimited': bool(plan.get('unlimited')), 'note': note, 'marker': marker}
        if getattr(client, 'panel_type', None) == '3x-ui':
            request['inbound_ids'] = list(client.default_inbound_ids)
    if client is None:
        raise operations.AccountBusy('The recorded server is unavailable; allocation is retained')
    if getattr(client, 'panel_type', None) == '3x-ui' and not request.get('inbound_ids'):
        raise operations.AccountBusy('The recorded inbound selection requires investigation')
    def action():
        if on_allocated:
            on_allocated(username, client)
        result = client.add_user(username, int(request['plan_gb']), request['days'],
                                 unlimited=request['unlimited'], note=request['note'],
                                 **({'inbound_ids': list(request['inbound_ids'])}
                                    if 'inbound_ids' in request else {}))
        return {'success': bool(result), 'username': username, 'server_id': client.server_id}
    result = operations.execute(operation_id, client.server_id, username, 'create', request, action,
                                origin=origin, resources=resources)
    if not result.get('success'):
        raise operations.AccountBusy('Account creation requires reconciliation; allocation and funds are retained')
    panels.record_created_user(client.server_id, username)
    return username, result, client
