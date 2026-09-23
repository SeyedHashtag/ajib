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


def test_ready_manifest_alone_keeps_customer_access_closed(managed):
    contract = json.loads((Path(__file__).resolve().parents[1] / 'core/web/release-contract.json').read_text())
    assert contract['customer_release_ready'] is True
    policy = release.state()['policy']
    assert not policy['accept_writes'] and not policy['process_existing']
    assert json.loads(policy['pilot_users_json']) == []
    with pytest.raises(ValueError, match='named-customer pilot'):
        release.change('public')


def test_undeliverable_notifications_are_visible_without_retry_backlog(managed):
    from utils import database, web_store
    with database.transaction() as db:
        web_store.enqueue(db, 'old-event', 'main', 123, 'Your service is ready.')
    item = web_store.claim_notification()
    web_store.finish_notification(item, 'telegram_forbidden', terminal=True)
    health = release.diagnostics(web.load())
    assert health['notification_backlog'] == 0
    assert health['undeliverable_notifications'] == 1


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


def test_payment_waiver_is_documented_scoped_and_never_claims_a_pass(managed):
    note = ('The operator declined a real payment in the named pilot; automated provider '
            'and receipt-flow checks are retained separately from this live-payment waiver.')
    evidence = {'note': note, 'waiver': True, 'artifact_sha256': 'f'*64}
    with pytest.raises(ValueError, match='active named pilot'):
        release.record_check('live_crypto', False, evidence)
    release.change('pilot', ['2', '3'])
    with pytest.raises(ValueError, match='Only an unpassed'):
        release.record_check('live_crypto', True, evidence)
    with pytest.raises(ValueError, match='Only an unpassed'):
        release.record_check('reserved_renewal', False, evidence)
    with pytest.raises(ValueError, match='Only an unpassed'):
        release.record_check('live_card', False, {**evidence, 'payment_ids': ['not-a-payment']})
    with pytest.raises(ValueError, match='Only an unpassed'):
        release.record_check('live_card', False, {'note': note, 'waiver': True})
    assert release.record_check('live_card', False, evidence) == {
        'revision': 'a'*40, 'name': 'live_card', 'passed': False, 'waived': True}
    assert release.record_check('live_crypto', False, evidence)['waived']
    report = release.state()
    assert report['checks']['live_card'] is False and report['checks']['live_crypto'] is False
    assert report['waived_checks'] == ['live_card', 'live_crypto']
    assert not {'live_card', 'live_crypto'} & set(report['missing_checks'])
    assert 'reserved_renewal' in report['missing_checks']
    release.record_check('live_crypto', False, {'note': 'Waiver withdrawn after operator review.'})
    report = release.state()
    assert report['waived_checks'] == ['live_card']
    assert 'live_crypto' in report['missing_checks']


def test_public_promotion_accepts_documented_payment_waivers_but_no_other_missing_checks(managed):
    from utils import database
    release.change('pilot', ['2', '3'])
    note = ('The operator explicitly accepted release without a live transfer; '
            'this check remains untested and is recorded as a waiver, not a pass.')
    for name in release.WAIVABLE_CHECKS:
        release.record_check(name, False, {'note': note, 'waiver': True, 'artifact_sha256': 'f'*64})
    with database.transaction() as db:
        db.execute('UPDATE web_release_control SET pilot_started_at=?', (int(time.time())-86401,))
        for name in release.LIVE_CHECKS - release.WAIVABLE_CHECKS:
            db.execute('INSERT INTO web_release_checks VALUES (?,?,1,?,?)',
                       ('a'*40, name, '{}', int(time.time())))
    assert release.state()['missing_checks'] == []
    assert release.change('public')['access'] == 'public'


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
