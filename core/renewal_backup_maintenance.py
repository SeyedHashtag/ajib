"""Journaled, operator-only maintenance for evidence-backed renewal completion."""
import json
from pathlib import Path
import shutil
import time
import uuid

import web_operator as web
import web_upgrade as upgrade

JOURNAL = 'renewal-evidence.json'
ROOT = Path('/opt/ajib-backups/renewal-reconciliation')
UNITS = (*web.UNITS, upgrade.BOT_UNIT)


def _journal(value):
    web.write(web.CONFIG / JOURNAL, json.dumps(value, sort_keys=True), 0o600)


def _phase(value, phase):
    value['phase'] = phase
    _journal(value)


def _checks(plan):
    upgrade._no_pending()
    if (web.CONFIG / JOURNAL).exists():
        raise ValueError('Recover the previous renewal reconciliation first')
    baseline = upgrade._check_baseline(plan)
    policy = upgrade._policy(plan)
    if (policy['access'] != 'admin' or policy['accept_writes'] or policy['process_existing']
            or json.loads(policy['pilot_users_json']) or baseline['contract'].get('customer_release_ready')):
        raise ValueError('Customer release gates must remain closed')
    return policy


def _status(operation_id):
    from utils import account_operations, database
    detail = account_operations.details(operation_id)
    row = account_operations.existing(operation_id)
    if not detail or not row:
        raise ValueError('Operation is missing during recovery')
    origin = json.loads(detail['origin_json'])
    payment = database.get_connection().execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                                                ('main', origin['id'])).fetchone()
    record = json.loads(payment[0]) if payment else {}
    if detail['phase'] == 'completed' and row['status'] == 'succeeded' and record.get('renewal_status') == 'applied':
        return 'committed'
    if detail['phase'] == 'uncertain' and row['status'] == 'uncertain' and record.get('renewal_status') == 'reserved':
        return 'unchanged'
    raise ValueError('Financial state is incomplete; keep processing paused for forward repair')


def _restore_units(value):
    active = value['active']
    if active:
        web.run('systemctl', 'start', *active)
    for unit in UNITS:
        if upgrade._active(unit) != (unit in active):
            raise ValueError('Application service state did not recover')
    deadline = time.monotonic() + 30
    while True:
        from web_release_cli import diagnostics
        worker_ok = (web.UNITS[1] not in active or diagnostics(web.load())['worker_healthy'])
        hosted_ok = (upgrade.BOT_UNIT not in active or
                     set(value.get('hosted_ids', [])).issubset(upgrade._hosted_processes()))
        if worker_ok and hosted_ok:
            return
        if time.monotonic() >= deadline:
            raise ValueError('Worker or hosted processes did not recover')
        time.sleep(0.5)


def _finish(value):
    outcome = _status(value['operation_id'])
    _restore_units(value)
    value['phase'] = 'complete' if outcome == 'committed' else 'not_applied'
    web.write(Path(value['root']) / 'result.json', json.dumps({'outcome': outcome, 'commit': value['commit'],
              'operation_id': value['operation_id'], 'backup': value.get('backup')}), 0o600)
    _journal(value)
    (web.CONFIG / JOURNAL).unlink()
    return {'outcome': outcome, 'backup': value.get('backup'), 'commit': value['commit']}


def apply(operation_id, panels, before, after, payment_before, evidence_digest):
    from utils import renewal_backup_recovery
    web.root_required()
    with web.maintenance():
        plan = web.load()
        _checks(plan)
        report = renewal_backup_recovery.inspect(operation_id, panels, before, after, payment_before)
        if report['evidence_digest'] != evidence_digest:
            raise ValueError('Evidence changed; inspect again')
        ident = uuid.uuid4().hex
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        ROOT.chmod(0o700)
        root = ROOT / ident
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        retained = {}
        for name, source in (('panel-before.db', before), ('panel-after.db', after),
                             ('payment-before.db', payment_before)):
            destination = root / name
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            retained[name] = str(destination)
        value = {'operation_id': operation_id, 'commit': upgrade._check_baseline(plan)['commit'],
                 'evidence_digest': evidence_digest, 'root': str(root), 'active': [unit for unit in UNITS if upgrade._active(unit)],
                 'hosted_ids': sorted(upgrade._hosted_processes()),
                 'phase': 'prepared', 'created_at': int(time.time())}
        _journal(value)
        try:
            _phase(value, 'stopping')
            web.run('systemctl', 'stop', *UNITS)
            _phase(value, 'backing_up')
            backup = root / 'state-before.db'
            web.snapshot_database(plan['database'], backup)
            value['backup'] = str(backup)
            _phase(value, 'backed_up')
            # Fresh panel and obligation reads occur under the account lock.
            outcome = renewal_backup_recovery.reconcile(operation_id, panels, retained['panel-before.db'],
                         retained['panel-after.db'], retained['payment-before.db'], evidence_digest)
            _phase(value, 'accounting_committed')
            result = _finish(value)
            return {**outcome, **result}
        except BaseException:
            # A crash between SQLite COMMIT and the journal update is possible.
            # The database, never a stale backup, determines recovery direction.
            try:
                _finish(value)
            except BaseException:
                pass
            raise


def recover():
    web.root_required()
    with web.maintenance():
        path = web.CONFIG / JOURNAL
        if not path.exists():
            return {'status': 'idle'}
        value = json.loads(path.read_text())
        if not Path(value['root']).resolve().is_relative_to(ROOT.resolve()) or not Path(value['root']).is_dir():
            raise ValueError('Recovery journal path is invalid')
        return _finish(value)
