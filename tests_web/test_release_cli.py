import contextlib
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import web_release_cli as release
import web_operator as web
import web_upgrade as upgrade


@pytest.fixture
def managed(storage, monkeypatch):
    from utils import database
    config = storage / 'config'
    config.mkdir()
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'maintenance', contextlib.nullcontext)
    monkeypatch.setattr(upgrade, '_check_baseline', lambda plan: {'commit': 'a'*40, 'contract': {'customer_release_ready': True}})
    (config / 'deployment.json').write_text(json.dumps({'database': database.database_path(), 'checkout': str(storage)}))
    (config / 'runtime.env').write_text('ADMIN_USER_IDS=[1]\nAJIB_WEB_WRITES_ENABLED=0\n')
    with database.transaction() as db:
        db.execute("INSERT INTO web_worker_health VALUES ('worker',?,?,NULL,0)", (int(time.time()), int(time.time())))
        for name in release.AUTOMATED_CHECKS:
            db.execute('INSERT INTO web_release_checks VALUES (?,?,1,?,?)', ('a'*40, name, '{}', int(time.time())))
    return storage


def test_pilot_checks_names_and_enables_only_explicit_users(managed):
    from utils import database
    with pytest.raises(ValueError, match='non-admin'):
        release.change('pilot', ['1', '2'])
    with pytest.raises(ValueError, match='two distinct'):
        release.change('pilot', ['2'])
    release.change('pilot', ['2', '3'])
    current = upgrade._policy(web.load())
    assert json.loads(current['pilot_users_json']) == ['2', '3']
    assert current['accept_writes'] == current['process_existing'] == 1
    assert database.get_connection().execute("SELECT COUNT(*) FROM web_audit WHERE action='release.pilot'").fetchone()[0] == 1


def test_pause_immediately_separates_admission_from_existing_work(managed):
    release.change('pilot', ['2', '3'])
    result = release.change('pause')
    assert result['accept_writes'] is False and result['process_existing'] is True
    assert upgrade._policy(web.load())['pilot_started_at'] is None
    assert release.change('pause', halt_worker=True)['process_existing'] is False


def test_public_promotion_requires_time_evidence_and_health(managed):
    from utils import database
    release.change('pilot', ['2', '3'])
    with pytest.raises(ValueError, match='24 hours'):
        release.change('public')
    with database.transaction() as db:
        db.execute('UPDATE web_release_control SET pilot_started_at=?', (int(time.time())-86401,))
    with pytest.raises(ValueError, match='evidence'):
        release.change('public')
    with database.transaction() as db:
        for name in release.LIVE_CHECKS:
            db.execute('INSERT INTO web_release_checks VALUES (?,?,1,?,?)', ('a'*40, name, '{}', int(time.time())))
        db.execute("UPDATE web_worker_health SET last_error='failed'")
    with pytest.raises(ValueError, match='health'):
        release.change('public')
    with database.transaction() as db:
        db.execute('UPDATE web_worker_health SET last_error=NULL')
    assert release.change('public')['access'] == 'public'


def test_ready_contract_and_exact_revision_are_required(managed, monkeypatch):
    monkeypatch.setattr(upgrade, '_check_baseline', lambda plan: {'commit': 'a'*40, 'contract': {'customer_release_ready': False}})
    with pytest.raises(ValueError, match='not yet cleared'):
        release.change('pilot', ['2', '3'])
    monkeypatch.setattr(upgrade, '_check_baseline', lambda plan: {'commit': 'b'*40, 'contract': {'customer_release_ready': True}})
    with pytest.raises(ValueError, match='automated'):
        release.change('pilot', ['2', '3'])


def test_live_check_cannot_record_unpaid_or_other_revision_payment(managed):
    from utils import database
    from utils.web_orders import save_payment
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'user_id': 2, 'status': 'pending', 'payment_method': 'Crypto',
                                         'web_revision': 'a'*40})
    evidence = {'note': 'Operator verified this designated pilot payment.', 'payment_ids': ['pilot']}
    with pytest.raises(ValueError, match='completed'):
        release.record_check('live_crypto', True, evidence)
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'status': 'completed', 'web_revision': 'b'*40})
    with pytest.raises(ValueError, match='revision'):
        release.record_check('live_crypto', True, evidence)


def test_live_evidence_requires_selected_customer_real_payment_and_correct_renewal_mode(managed):
    from utils import database
    from utils.web_orders import save_payment
    release.change('pilot', ['2', '3'])
    evidence = {'note': 'Synthetic acceptance evidence for the named pilot.', 'payment_ids': ['pilot']}
    record = {'user_id': 99, 'status': 'completed', 'payment_method': 'Crypto', 'web_revision': 'a'*40,
              'price': '10', 'gateway_verified_payment_amount_usd': '10',
              'gateway_payment_id': 'synthetic-invoice', 'gateway_merchant_id': 'synthetic-merchant'}
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', record)
    with pytest.raises(ValueError, match='selected customer'):
        release.record_check('live_crypto', True, evidence)
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'user_id': 2, 'price': 0})
    with pytest.raises(ValueError, match='real positive'):
        release.record_check('live_crypto', True, evidence)
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'price': '10', 'gateway_verified_payment_amount_usd': '9.99'})
    with pytest.raises(ValueError, match='verified provider'):
        release.record_check('live_crypto', True, evidence)
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'gateway_verified_payment_amount_usd': '10'})
    assert release.record_check('live_crypto', True, evidence)['passed']
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'type': 'renewal', 'renewal_mode': 'reserved', 'renewal_status': 'reserved'})
    with pytest.raises(ValueError, match='immediate'):
        release.record_check('immediate_renewal', True, evidence)
    with pytest.raises(ValueError, match='actually be applied'):
        release.record_check('reserved_renewal', True, evidence)
    with database.transaction() as db:
        save_payment(db, 'main', 'pilot', {'renewal_status': 'applied'})
    assert release.record_check('reserved_renewal', True, evidence)['passed']


def test_unresolved_bot_account_mutation_blocks_promotion(managed):
    from utils import database
    release.change('pilot', ['2', '3'])
    with database.transaction() as db:
        db.execute('UPDATE web_release_control SET pilot_started_at=?', (int(time.time())-90000,))
        for name in release.LIVE_CHECKS:
            db.execute('INSERT INTO web_release_checks VALUES (?,?,1,?,?)', ('a'*40, name, '{}', int(time.time())))
        db.execute("INSERT INTO account_operations VALUES ('bot-payment','server','account','renewal','uncertain','{}',NULL,1,1)")
    with pytest.raises(ValueError, match='health blocks'):
        release.change('public')
