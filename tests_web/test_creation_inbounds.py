import json
from types import SimpleNamespace

import pytest


def panel():
    calls = []
    client = SimpleNamespace(server_id='server', panel_type='3x-ui', default_inbound_ids=[1, 2],
        add_user=lambda *a, **kw: calls.append(kw) or {'ok': True})
    panels = SimpleNamespace(get_client=lambda s: client,
        prepare_new_user_creation=lambda **kw: {'client': client, 'existing_usernames': []},
        record_created_user=lambda *a: None)
    return client, panels, calls


def test_creation_persists_inbounds_and_retains_selection_after_default_change(storage):
    from utils import account_mutations, account_operations
    client, panels, calls = panel()
    plan = {'gb': 10, 'days': 30}
    account_mutations.create('create-1', panels, lambda names: 'synthetic', plan,
                             origin={'type': 'admin', 'actor': '1'})
    stored = account_operations.existing('create-1')
    assert json.loads(stored['request_json'])['inbound_ids'] == [1, 2]
    client.default_inbound_ids = [3]
    account_mutations.create('create-1', panels, lambda names: pytest.fail('must not allocate again'), plan)
    assert len(calls) == 1 and calls[0]['inbound_ids'] == [1, 2]


def test_legacy_creation_without_inbounds_cannot_dispatch(storage, monkeypatch):
    from utils import account_mutations, account_operations
    client, panels, calls = panel()
    monkeypatch.setattr(account_operations, 'existing', lambda ident: {'server_id': 'server', 'username': 'synthetic',
        'request_json': json.dumps({'plan_gb': '10', 'days': 30, 'unlimited': False, 'note': 'legacy'})})
    with pytest.raises(account_operations.AccountBusy, match='inbound'):
        account_mutations.create('legacy', panels, lambda names: 'synthetic', {'gb': 10, 'days': 30})
    assert not calls


def test_recovery_requires_all_recorded_inbounds():
    from utils.operation_recovery import _created_matches
    row = {'username': 'synthetic', 'operation_id': 'create-1'}
    request = {'marker': 'private-marker', 'plan_gb': 10, 'days': 30, 'unlimited': False, 'inbound_ids': [1, 2]}
    user = {'username': 'synthetic', 'note': 'private-marker', 'max_download_bytes': 10*1024**3,
            'expiration_days': 30, 'unlimited_ip': False, 'inbound_ids': [1]}
    assert not _created_matches(row, request, {}, user)
    user['inbound_ids'] = [1, 2]
    assert _created_matches(row, request, {}, user)
    request.pop('inbound_ids')
    assert not _created_matches(row, request, {}, user)
