"""A review must not change the provenance of an unresolved panel operation."""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux account-operation locks')


@pytest.mark.parametrize('scope', ['main', 'hosted:7'])
@pytest.mark.parametrize('phase', ['prepared', 'dispatched', 'uncertain', 'panel_verified'])
def test_payment_wait_retains_original_baseline_and_claim(storage, scope, phase):
    from utils import account_operations as operations, database, renewal, state_store
    record = {'user_id': 123, 'status': 'completed', 'renewal_status': 'attention',
        'renewal_username': 'synthetic', 'renewal_server_id': 's1', 'renewal_baseline': {'cycle': 'original'}}
    db = database.get_connection()
    state_store._save_payment_record(db, scope, 'p1', record)
    ident = 'main-payment:p1' if scope == 'main' else 'hosted-payment:7:p1'
    operations.execute(ident, 's1', 'synthetic', 'renewal', {}, lambda: {'success': False})
    with database.transaction() as connection:
        connection.execute('UPDATE account_operation_details SET phase=? WHERE operation_id=?', (phase, ident))
    before = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?', (scope, 'p1')).fetchone()[0]
    path = storage / 'payments.json' if scope == 'main' else storage / 'hosted_bots/7/payments.json'
    with pytest.raises(operations.AccountBusy):
        renewal.refresh_payment_renewal_baseline('p1', {'account_creation_date': 'changed'}, payments_file=str(path))
    assert db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?', (scope, 'p1')).fetchone()[0] == before
    assert operations.details(ident)['phase'] == phase
    assert db.execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 1


def test_unclaimed_payment_wait_preserves_existing_behavior(storage):
    from utils import database, renewal, state_store
    db = database.get_connection()
    state_store._save_payment_record(db, 'main', 'p1', {'user_id': 123, 'status': 'completed',
        'renewal_status': 'attention', 'renewal_username': 'synthetic', 'renewal_server_id': 's1'})
    assert renewal.refresh_payment_renewal_baseline('p1', {'account_creation_date': 'fresh'}, payments_file=str(storage / 'payments.json'))
    record = json.loads(db.execute("SELECT payload_json FROM payments WHERE payment_id='p1'").fetchone()[0])
    assert record['renewal_status'] == 'reserved'
    assert record['renewal_baseline']['account_creation_date'] == 'fresh'


def test_reseller_wait_cannot_replace_claimed_account_baseline(storage, monkeypatch):
    from utils import account_operations as operations, database, reseller, state_store
    monkeypatch.setattr(reseller, 'RESELLERS_FILE', str(storage / 'resellers.json'))
    db = database.get_connection()
    state_store._save_reseller_record(db, '7', {'status': 'approved', 'configs': [
        {'username': 'synthetic', 'server_id': 's1', 'renewals': [{'reservation_id': 'r1',
            'renewal_status': 'attention', 'renewal_baseline': {'cycle': 'original'}}]}]})
    operations.execute('synthetic-owner', 's1', 'synthetic', 'renewal', {}, lambda: {'success': False})
    before = db.execute("SELECT payload_json FROM resellers WHERE reseller_id='7'").fetchone()[0]
    with pytest.raises(operations.AccountBusy):
        reseller.refresh_reseller_renewal_baseline('7', 'r1', {'cycle': 'changed'})
    assert db.execute("SELECT payload_json FROM resellers WHERE reseller_id='7'").fetchone()[0] == before
    assert operations.details('synthetic-owner')['phase'] == 'uncertain'
