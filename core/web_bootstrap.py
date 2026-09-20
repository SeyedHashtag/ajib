"""Reviewed one-time transition from a read-only legacy website to contract 2.

The legacy trees are retained, but never advertised as a compatible rollback.
After switching, interrupted installation only recovers forward to the reviewed
candidate. No recovery path restores the live database or starts legacy code.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import time
import uuid

import web_operator as web
import web_upgrade as upgrade
import web_config


def inventory(plan):
    """Read-only evidence to review before preparing an unmanaged installation."""
    if (web.CONFIG / 'managed-release.json').exists():
        raise ValueError('A managed baseline exists; use the coordinated upgrade.')
    upgrade._no_pending()
    env = web_config._environment((web.CONFIG / 'runtime.env').read_text())
    if (env.get('AJIB_WEB_WRITES_ENABLED') != '0' or env.get('AJIB_WEB_PUBLIC_PORTAL') != '0'
            or env.get('AJIB_WEB_PILOT_USERS', '').strip()):
        raise ValueError('Bootstrap requires administrator-only access with writes and pilot users disabled.')
    if upgrade._schema(plan['database']) != (6, 1):
        raise ValueError('Unsupported legacy schema; requires separate migration review.')
    with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('web_operations', 'web_trials', 'web_outbox', 'account_operations'):
            if table in tables and db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
                raise ValueError('Existing web/account obligations require separate baseline review.')
        if 'web_release_control' in tables and db.execute('SELECT COUNT(*) FROM web_release_control').fetchone()[0]:
            raise ValueError('Existing release policy requires separate baseline review.')
        for row in db.execute('SELECT payload_json FROM payments'):
            if json.loads(row[0]).get('fulfillment_owner') == 'web':
                raise ValueError('Web-owned payments cannot use the legacy bootstrap.')
    inputs = web_config._inputs(plan)
    web_config._desired(plan, inputs)  # Includes rejection of credential rotation.
    files = [web.CONFIG / 'runtime.env', web.CONFIG / 'deployment.json', web.CONFIG / 'edge/nginx.conf']
    files += list(Path('/etc/systemd/system').glob('ajib*.service'))
    files += list(Path('/etc/systemd/system/ajib-telegram-bot.service.d').glob('*.conf'))
    return {'format': 1, 'plan': plan, 'bot': upgrade.hashes(plan['checkout']),
            'web': upgrade.hashes(web.SOURCE), 'inputs': web_config._digest(inputs),
            'configuration': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}}


def preview(review, commit):
    plan = web.load()
    if inventory(plan) != review:
        raise ValueError('Installed files or configuration changed since the bootstrap review.')
    target = upgrade.resolve_target('main')
    if target['commit'] != commit:
        raise ValueError('The reviewed commit is not the current CI-verified main target.')
    if target['contract'].get('customer_release_ready') is not False:
        raise ValueError('Bootstrap installs only a gated candidate, never a customer release.')
    return target


def forward_switch(entries):
    """Resume each previously journaled rename, including either crash window."""
    for item in entries:
        live, staged, old = (Path(item[k]) for k in ('live', 'staged', 'old'))
        exists = tuple(upgrade._exists(p) for p in (live, staged, old))
        if exists == (True, True, False):
            upgrade._rename(live, old)
            upgrade._rename(staged, live)
        elif exists == (False, True, True):
            upgrade._rename(staged, live)
        elif exists != (True, False, True):
            raise ValueError('Unexpected bootstrap switch state; preserve files for forward repair.')


def _runtime(journal):
    root, plan = Path(journal['root']), journal['plan']
    original = Path(plan['checkout']) / 'core/scripts/telegrambot'
    staged = Path(journal['switches'][0]['staged']) / 'core/scripts/telegrambot'
    names = set(upgrade.MUTABLE) | {p.name for p in original.glob('*.json')}
    for name in sorted(names):
        src, dst = original / name, staged / name
        if src.is_symlink() or (src.is_dir() and any(p.is_symlink() for p in src.rglob('*'))):
            raise ValueError('Runtime symlinks require review before bootstrap.')
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, dst)
    # Include private uploads and JSON reminder/disclosure state, not only the
    # database and configuration archive. Writers are stopped throughout.
    backup = root / 'private/runtime.tar.gz'
    temporary = backup.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        temporary.chmod(0o600)
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name in sorted(names):
                if (original / name).exists():
                    archive.add(original / name, arcname=name)
        stream.flush()
        os.fsync(stream.fileno())
    with tarfile.open(temporary) as archive:
        for member in archive.getmembers():
            if member.isfile():
                saved = archive.extractfile(member).read()
                if hashlib.sha256(saved).digest() != hashlib.sha256((original / member.name).read_bytes()).digest():
                    raise ValueError('Runtime backup verification failed.')
    os.replace(temporary, backup)
    staged.chmod(0o700)
    inputs = web_config._inputs(plan)
    desired, _ = web_config._desired(plan, inputs)
    for name in web_config.CATALOGS:
        if inputs[name] is not None:
            web.write(Path(journal['switches'][1]['staged']) / 'core/scripts/telegrambot' / name, inputs[name])
    web.write(root / 'private/candidate.env', desired, 0o600)
    for script in Path(journal['switches'][0]['staged']).rglob('*.sh'):
        if not script.is_symlink():
            script.chmod(0o755)


def _snapshot(journal):
    path = Path(journal['root']) / 'private/ajib.db'
    if not path.exists():
        web.snapshot_database(journal['plan']['database'], path)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Bootstrap backup failed integrity; do not replace the live database.')


def resume(journal):
    """Caller holds the maintenance lock. Any failure leaves financial runtimes stopped."""
    plan, root = journal['plan'], Path(journal['root'])
    if plan != web.load():
        raise ValueError('Deployment metadata changed; forward recovery requires review.')
    if journal['phase'] == 'complete':
        upgrade._finish(journal)
        return {'status': 'complete', 'commit': journal['commit'], 'retained_release': str(root)}
    try:
        # Legacy admission is already off; stop all application writers before
        # copying mutable runtime files or initializing new control tables.
        upgrade._persist(journal, 'bootstrap_stopping')
        web.run('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT)
        _snapshot(journal)
        if not journal.get('runtime_prepared'):
            with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
                if any(json.loads(row[0]).get('status') in {'processing', 'approved', 'uncertain', 'paid_provision_failed'}
                       for row in db.execute('SELECT payload_json FROM payments')):
                    raise ValueError('Legacy in-flight fulfillment requires review before bootstrap.')
            _runtime(journal)
            journal['runtime_prepared'] = True
            journal['candidate_hashes'] = {
                'bot': upgrade.hashes(journal['switches'][0]['staged']),
                'web': upgrade.hashes(journal['switches'][1]['staged'])}
            upgrade._persist(journal, 'bootstrap_switching')
        forward_switch(journal['switches'])
        if (upgrade.hashes(plan['checkout']) != journal['candidate_hashes']['bot'] or
                upgrade.hashes(web.SOURCE) != journal['candidate_hashes']['web']):
            raise ValueError('Candidate code drift blocks forward recovery.')
        web.write(web.CONFIG / 'runtime.env', (root / 'private/candidate.env').read_text(), 0o640, gid=plan['gid'])
        upgrade._migrate(plan['checkout'], web.VENV / 'bin/python', Path(plan['database']), root / 'private/migration')
        policy = {'access': 'admin', 'pilot_users_json': '[]', 'accept_writes': 0, 'process_existing': 0,
                  'revision': journal['commit'], 'pilot_started_at': None}
        upgrade._save_policy(plan, policy)
        with sqlite3.connect(plan['database']) as db:
            db.execute('UPDATE web_sessions SET revoked_at=? WHERE revoked_at IS NULL', (int(time.time()),))
            db.execute('DELETE FROM web_challenges')
        upgrade._persist(journal, 'bootstrap_starting')
        upgrade._edge(journal)
        started = int(time.time())
        if journal['active']:
            web.run('systemctl', 'start', *journal['active'])
        upgrade._healthy(journal, started)
        baseline = {'contract': journal['contract'], 'commit': journal['commit'], **journal['candidate_hashes']}
        web.write(web.CONFIG / 'managed-release.json', json.dumps(baseline), 0o600)
        upgrade._persist(journal, 'complete')
        upgrade._finish(journal)
        return {'status': 'complete', 'commit': journal['commit'], 'retained_release': str(root)}
    except BaseException:
        web.run('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT)
        upgrade._persist(journal, 'bootstrap_forward_recovery_required')
        raise ValueError('Bootstrap requires forward recovery using the retained candidate coordinator. '
                         'Financial runtimes remain stopped; legacy code is not a compatible rollback.') from None


def install(review, commit):
    web.root_required()
    with web.maintenance():
        target = preview(review, commit)
        plan = web.load()
        ident = 'bootstrap-' + uuid.uuid4().hex
        os.umask(0o022)
        upgrade.RELEASES.mkdir(parents=True, exist_ok=True, mode=0o755)
        upgrade.RELEASES.chmod(0o755)
        root = upgrade.RELEASES / ident
        root.mkdir(parents=True, mode=0o755)
        (root / 'private').mkdir(mode=0o700)
        bot = Path(plan['checkout'])
        staged_bot = bot.with_name('.ajib-' + ident)
        staged_web = web.SOURCE.with_name('.app-' + ident)
        upgrade._prepare(plan, target, root, staged_bot, staged_web)
        if inventory(plan) != review:
            raise ValueError('Installed files changed during preparation; no services stopped.')
        staged_venv = web.VENV.with_name('.venv-' + ident)
        staged_venv.symlink_to(root / 'web-venv', target_is_directory=True)
        edge = json.loads(web.run('docker', 'inspect', 'ajib-web-edge', capture=True).stdout)[0]
        journal = {'kind': 'bootstrap', 'id': ident, 'commit': commit, 'contract': target['contract'],
                   'started_at': int(time.time()), 'root': str(root), 'plan': plan,
                   'active': [u for u in (*web.UNITS, upgrade.BOT_UNIT) if upgrade._active(u)],
                   'edge_image': edge['Image'], 'edge_running': edge['State']['Running'],
                   'hosted_ids': sorted(upgrade._hosted_processes()),
                   'switches': [{'live': str(live), 'staged': str(staged), 'old': str(live.with_name('.' + live.name + '-previous-' + ident))}
                                for live, staged in [(bot, staged_bot), (web.SOURCE, staged_web), (web.VENV, staged_venv)]]}
        web.write(root / 'private/review.json', json.dumps(review), 0o600)
        web.write(root / 'private/runtime.env', (web.CONFIG / 'runtime.env').read_text(), 0o600)
        journal['configuration_backup'] = web.backup_configuration()
        upgrade._persist(journal, 'bootstrap_prepared')
        return resume(journal)
