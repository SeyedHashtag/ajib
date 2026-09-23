import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def evidence(storage, tmp_path):
    from utils import account_operations, database
    start = 1_790_013_487_000
    before_start = 1_788_425_875_000
    days, total = 60, 100 * 1024**3
    prior = {'user_id': 123, 'status': 'completed', 'fulfillment_owner': 'bot',
             'renewal_mode': 'reserved', 'renewal_status': 'reserved',
             'renewal_username': 's123a', 'renewal_server_id': 's1',
             'renewal_baseline': {'account_creation_date': '2026-09-03T08:57:55Z'}}
    before = {'account_creation_date': '2026-09-03T08:57:55Z', 'expiration_days': days}
    operation_id = 'main-payment:synthetic-order'
    with database.transaction() as db:
        db.execute('INSERT INTO payments(scope,payment_id,user_id,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                   ('main', 'synthetic-order', '123', json.dumps(prior), start//1000, start//1000))
    prior_db = tmp_path / 'prior.db'
    with sqlite3.connect(prior_db) as copy:
        database.get_connection().backup(copy)
    current = {**prior, 'renewal_baseline': {'account_creation_date': '2026-09-21T17:58:08Z'}}
    origin = {'type': 'payment', 'scope': 'main', 'id': 'synthetic-order',
              'owner': 'bot', 'user_id': '123', 'terms_digest': account_operations.payment_terms(prior)}
    request = {'before': before, 'target': {'plan_gb': 100, 'days': 60, 'unlimited': True}}
    with database.transaction() as db:
        db.execute('UPDATE payments SET payload_json=? WHERE scope=? AND payment_id=?',
                   (json.dumps(current), 'main', 'synthetic-order'))
        db.execute('INSERT INTO account_operations VALUES (?,?,?,?,?,?,?,?,?)',
                   (operation_id, 's1', 's123a', 'renewal', 'uncertain', json.dumps(request),
                    json.dumps({'success': False, 'reason': 'renewal_reset_failed'}), start//1000, start//1000+1))
        db.execute('INSERT INTO account_operation_details VALUES (?,?,?,?,?,?)',
                   (operation_id, 'uncertain', 1, json.dumps(origin), json.dumps([['s1', 's123a']]), start//1000))
        db.execute('INSERT INTO account_operation_claims VALUES (?,?,?)', ('s1', 's123a', operation_id))
    panel_files = []
    for index, cycle in enumerate((before_start, start+1000)):
        path = tmp_path / f'panel-{index}.db'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE clients(id INTEGER,email TEXT,sub_id TEXT,uuid TEXT,password TEXT,
                              auth TEXT,secret TEXT,created_at INTEGER,total_gb INTEGER,expiry_time INTEGER,
                              enable INTEGER,updated_at INTEGER);
                              CREATE TABLE client_traffics(email TEXT,up INTEGER,down INTEGER,expiry_time INTEGER,total INTEGER);
                              CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER);''')
            db.execute('INSERT INTO clients VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (7, 's123a', 'sub', 'uuid', 'password', None, None, 1234,
                        total, cycle+days*86_400_000, 1, start-30_000 if index==0 else start+1088))
            db.execute('INSERT INTO client_traffics VALUES (?,?,?,?,?)',
                       ('s123a', 10*1024**3 if index==0 else 10_000_000,
                        90*1024**3 if index==0 else 20_000_000, cycle+days*86_400_000, total))
            db.execute('INSERT INTO client_inbounds VALUES (?,?)', (7, 1))
        panel_files.append(path)
    live = {'username': 's123a', 'account_creation_date': datetime.fromtimestamp((start+1000)/1000,timezone.utc).isoformat().replace('+00:00','Z'),
            'expiration_days': days, 'max_download_bytes': total, 'unlimited_ip': True,
            'upload_bytes': 20_000_000, 'download_bytes': 30_000_000, 'blocked': False, 'status': 'Online'}
    panel = SimpleNamespace(server_id='s1')
    class Panels:
        calls = 0
        def resolve_unique_user(self, username, **kwargs):
            self.calls += 1
            return panel, dict(live), {'status':'found','uniqueness_verified':True}
    return SimpleNamespace(ident=operation_id, panels=Panels(), files=(*panel_files, prior_db), live=live,
                           prior=prior, current=current)


def test_backup_evidence_completes_original_renewal_once(evidence):
    from utils import account_operations, database, renewal_backup_recovery
    item = evidence
    report = renewal_backup_recovery.inspect(item.ident, item.panels, *item.files)
    assert report['action'] == 'complete_accounting'
    assert renewal_backup_recovery.reconcile(item.ident, item.panels, *item.files,
                                             report['evidence_digest'])['applied']
    record = json.loads(database.get_connection().execute(
        'SELECT payload_json FROM payments WHERE scope=? AND payment_id=?', ('main', 'synthetic-order')).fetchone()[0])
    assert record['renewal_status'] == 'applied'
    assert record['renewal_baseline'] == item.prior['renewal_baseline']
    assert account_operations.details(item.ident)['phase'] == 'completed'
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 0
    assert database.get_connection().execute("SELECT COUNT(*) FROM web_outbox WHERE id LIKE 'renewal-applied:%'").fetchone()[0] == 1
    assert item.panels.calls == 2
    with pytest.raises(ValueError):
        renewal_backup_recovery.reconcile(item.ident, item.panels, *item.files, report['evidence_digest'])


@pytest.mark.parametrize('change', ['identity','expiry','traffic','terms','stale','tampered'])
def test_invalid_backup_or_obligation_keeps_claim(evidence, change):
    from utils import account_operations, database, renewal_backup_recovery
    item=evidence
    if change in {'identity','expiry','traffic'}:
        with sqlite3.connect(item.files[1]) as db:
            if change=='identity': db.execute("UPDATE clients SET uuid='changed'")
            if change=='expiry': db.execute('UPDATE clients SET expiry_time=expiry_time+86400000')
            if change=='traffic': db.execute('UPDATE client_traffics SET up=999999999999')
    elif change=='terms':
        with database.transaction() as db:
            row=db.execute('SELECT payload_json FROM payments WHERE payment_id=?',('synthetic-order',)).fetchone()
            record=json.loads(row[0]);record['price']=5
            db.execute('UPDATE payments SET payload_json=? WHERE payment_id=?',(json.dumps(record),'synthetic-order'))
    elif change=='stale': item.live['account_creation_date']='2026-09-22T00:00:00Z'
    else:
        report=renewal_backup_recovery.inspect(item.ident,item.panels,*item.files)
        with pytest.raises(ValueError,match='Evidence changed'):
            renewal_backup_recovery.reconcile(item.ident,item.panels,*item.files,report['evidence_digest']+'0')
    if change!='tampered':
        with pytest.raises(ValueError): renewal_backup_recovery.inspect(item.ident,item.panels,*item.files)
    assert account_operations.details(item.ident)['phase']=='uncertain'
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0]==1


def test_notification_failure_rolls_back_accounting(evidence, monkeypatch):
    from utils import account_operations, database, renewal_backup_recovery, web_store
    report = renewal_backup_recovery.inspect(evidence.ident, evidence.panels, *evidence.files)
    def fail(*args):
        raise RuntimeError('synthetic notification storage failure')
    monkeypatch.setattr(web_store, 'enqueue', fail)
    with pytest.raises(RuntimeError):
        renewal_backup_recovery.reconcile(evidence.ident, evidence.panels, *evidence.files,
                                          report['evidence_digest'])
    record = json.loads(database.get_connection().execute(
        'SELECT payload_json FROM payments WHERE payment_id=?', ('synthetic-order',)).fetchone()[0])
    assert record == evidence.current
    assert account_operations.details(evidence.ident)['phase'] == 'uncertain'
    assert database.get_connection().execute('SELECT COUNT(*) FROM account_operation_claims').fetchone()[0] == 1


def test_evidence_rejects_changed_snapshot_after_inspection(evidence):
    from utils import renewal_backup_recovery
    report = renewal_backup_recovery.inspect(evidence.ident, evidence.panels, *evidence.files)
    with sqlite3.connect(evidence.files[1]) as db:
        db.execute('UPDATE clients SET updated_at=updated_at+1')
    with pytest.raises(ValueError, match='Evidence changed'):
        renewal_backup_recovery.reconcile(evidence.ident, evidence.panels, *evidence.files,
                                          report['evidence_digest'])


def test_live_traffic_can_accrue_after_inspection(evidence):
    from utils import account_operations, renewal_backup_recovery
    report = renewal_backup_recovery.inspect(evidence.ident, evidence.panels, *evidence.files)
    evidence.live['upload_bytes'] += 1024 * 1024
    evidence.live['download_bytes'] += 1024 * 1024
    assert renewal_backup_recovery.inspect(evidence.ident, evidence.panels, *evidence.files)['evidence_digest'] == report['evidence_digest']
    assert renewal_backup_recovery.reconcile(evidence.ident, evidence.panels, *evidence.files,
                                             report['evidence_digest'])['applied']
    assert account_operations.details(evidence.ident)['phase'] == 'completed'
