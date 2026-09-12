"""Coordinated, recoverable VPS code upgrades. Never restores the live database.

The durable journal and prior releases live outside the replaced checkout.
Only declared schema/fulfillment-compatible releases can be rolled back.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

import web_operator as web

REPOSITORY = 'https://github.com/SeyedHashtag/ajib.git'
RELEASE_API = 'https://api.github.com/repos/SeyedHashtag/ajib/releases/latest'
RAW = 'https://raw.githubusercontent.com/SeyedHashtag/ajib/'
CONTRACT = 'core/web/release-contract.json'
BOT_UNIT = 'ajib-telegram-bot'
RELEASES = Path('/opt/ajib-web/releases')
ROOT_FILES = {'ajib.sh', 'menu.sh', 'upgrade.sh', 'install.sh', 'VERSION', 'changelog',
              'requirements.txt', 'requirements-web.txt'}
MUTABLE = ('.env', '.env.previous', 'plans.json', 'support_info.json', 'hosted_bots', 'logs', 'broadcast_logs')


def _json(path):
    return json.loads(Path(path).read_text())


def _exists(path):
    return Path(path).exists() or Path(path).is_symlink()


def _rename(source, target):
    if _exists(target):
        raise ValueError('Refusing to overwrite an upgrade recovery path.')
    os.rename(source, target)
    for parent in {Path(source).parent, Path(target).parent}:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _code_file(relative):
    parts = relative.parts
    if any(part.startswith('.') or part in {'__pycache__', 'node_modules', 'logs', 'hosted_bots', 'broadcast_logs'} for part in parts):
        return False
    name = relative.as_posix()
    return (name in ROOT_FILES or
            (parts[0] == 'core' and (relative.suffix in {'.py', '.sh'} or name == CONTRACT)) or
            (parts[0] == 'web' and (parts[1:2] in [('src',), ('dist',), ('public',)] or len(parts) == 2)
             and relative.suffix in {'.ts', '.tsx', '.js', '.css', '.html', '.json', '.svg', '.png', '.woff2'}))


def hashes(root):
    """Hash installed code, including untracked code; exclude all private state."""
    root = Path(root)
    result = {}
    for folder in ('core', 'web'):
        for directory, dirs, files in os.walk(root / folder, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith('.') and name not in
                       {'__pycache__', 'node_modules', 'logs', 'hosted_bots', 'broadcast_logs'}]
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise ValueError('Managed source directories must not be symlinks.')
            for name in files:
                path = Path(directory) / name
                if _code_file(path.relative_to(root)):
                    if path.is_symlink():
                        raise ValueError('Managed source files must not be symlinks.')
                    result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ROOT_FILES:
        path = root / name
        if path.is_file():
            if path.is_symlink():
                raise ValueError('Managed source files must not be symlinks.')
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def validate_contract(contract, previous=None):
    expected = {'format': 1, 'bot_schema': 6, 'web_schema': 1,
                'fulfillment_contract': 1, 'unit_contract': 1, 'live_release_policy': True}
    if any(contract.get(key) != value for key, value in expected.items()):
        raise ValueError('Release does not declare the supported shared-state and recovery contract.')
    if previous and any(contract.get(key) != previous.get(key) for key in expected):
        raise ValueError('Release requires a separately reviewed compatibility migration.')


def _read_remote(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ajib-upgrade'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def resolve_target(channel='stable', version=None):
    if channel not in {'stable', 'main'}:
        raise ValueError('Unknown release channel.')
    if version is None and channel == 'stable':
        version = _read_remote(RELEASE_API).get('tag_name', '')
    if version is not None and not re.fullmatch(r'v?\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?', version):
        raise ValueError('Invalid release tag.')
    ref = 'refs/tags/' + version if version else 'refs/heads/main'
    rows = web.run('git', 'ls-remote', REPOSITORY, ref, ref + '^{}', capture=True).stdout.splitlines()
    entries = dict(line.split()[::-1] for line in rows)
    commit = entries.get(ref + '^{}') or entries.get(ref)
    if not commit or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('The requested release could not be resolved to a commit.')
    contract = _read_remote(RAW + commit + '/' + CONTRACT)
    validate_contract(contract)
    required = {'pytest (3.10)', 'pytest (3.11)', 'pytest (3.12)', 'shell', 'web'}
    runs = _read_remote('https://api.github.com/repos/SeyedHashtag/ajib/commits/' + commit + '/check-runs?filter=latest&per_page=100')
    passed = {run.get('name') for run in runs.get('check_runs', []) if run.get('head_sha') == commit
              and run.get('conclusion') == 'success' and run.get('app', {}).get('slug') == 'github-actions'}
    if not required <= passed:
        raise ValueError('Required CI checks have not passed for the exact target commit: ' + ', '.join(sorted(required - passed)))
    return {'commit': commit, 'label': version or 'main', 'contract': contract, 'ci_passed': sorted(required)}


def status():
    journal = web.CONFIG / 'upgrade.json'
    if not journal.exists():
        return {'status': 'idle', 'baseline_installed': (web.CONFIG / 'managed-release.json').exists()}
    value = _json(journal)
    return {key: value[key] for key in ('id', 'phase', 'commit', 'started_at')}


def _schema(path):
    with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True) as db:
        return (db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0],
                db.execute('SELECT MAX(version) FROM web_schema').fetchone()[0])


def _check_baseline(plan):
    baseline = _json(web.CONFIG / 'managed-release.json')
    validate_contract(baseline['contract'])
    if baseline['bot'] != hashes(plan['checkout']) or baseline['web'] != hashes(web.SOURCE):
        raise ValueError('Installed code differs from the managed baseline; review local changes before upgrading.')
    if _schema(plan['database']) != (6, 1):
        raise ValueError('Unexpected live database schema; no services were changed.')
    return baseline


def adopt_baseline():
    """Explicitly record reviewed, compatible installed code; never done implicitly."""
    web.root_required()
    with web.maintenance():
        _no_pending()
        plan = web.load()
        contract = _json(Path(plan['checkout']) / CONTRACT)
        validate_contract(contract)
        if _json(web.SOURCE / CONTRACT) != contract:
            raise ValueError('Bot and website must have the same release contract before adoption.')
        if _schema(plan['database']) != (6, 1):
            raise ValueError('Unsupported baseline database schema.')
        # Existing manifests cannot be silently re-baselined over local modifications.
        if (web.CONFIG / 'managed-release.json').exists():
            raise ValueError('A managed baseline already exists.')
        value = {'contract': contract, 'bot': hashes(plan['checkout']), 'web': hashes(web.SOURCE),
                 'adopted_at': int(time.time()), 'commit': 'adopted'}
        web.write(web.CONFIG / 'managed-release.json', json.dumps(value), 0o600)
        return {'status': 'adopted', 'bot_files': len(value['bot']), 'web_files': len(value['web'])}


def _no_pending():
    if any((web.CONFIG / name).exists() for name in ('upgrade.json', 'config-sync.json')):
        raise ValueError('Recover pending website maintenance before starting another operation.')


def preview(channel='stable', version=None):
    _no_pending()
    plan = web.load()
    baseline = _check_baseline(plan)
    target = resolve_target(channel, version)
    validate_contract(target['contract'], baseline['contract'])
    if _policy(plan)['accept_writes'] and not target['contract'].get('customer_release_ready'):
        raise ValueError('The target is not cleared for existing customer writes.')
    return {**target, 'services': [*web.UNITS, BOT_UNIT], 'preserve_live_database': True}


def _active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], capture_output=True).returncode == 0


def _policy(plan):
    from dotenv import dotenv_values
    env = dotenv_values(web.CONFIG / 'runtime.env')
    with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM web_release_control WHERE id=1').fetchone()
    return dict(row) if row else {'id': 1, 'access': 'public' if env.get('AJIB_WEB_PUBLIC_PORTAL') == '1' else 'pilot',
        'pilot_users_json': json.dumps([x for x in env.get('AJIB_WEB_PILOT_USERS', '').split(',') if x]),
        'accept_writes': int(env.get('AJIB_WEB_WRITES_ENABLED') == '1'),
        'process_existing': int(env.get('AJIB_WEB_WRITES_ENABLED') == '1'),
        'revision': '', 'pilot_started_at': None, 'updated_at': int(time.time())}


def _save_policy(plan, policy):
    with sqlite3.connect(plan['database'], timeout=15) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''INSERT OR REPLACE INTO web_release_control
            (id,access,pilot_users_json,accept_writes,process_existing,revision,pilot_started_at,updated_at)
            VALUES (1,?,?,?,?,?,?,?)''', (policy['access'], policy['pilot_users_json'], policy['accept_writes'],
            policy['process_existing'], policy['revision'], policy['pilot_started_at'], int(time.time())))


def _migrate(source, python, database, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(Path(source) / 'core/scripts/telegrambot'),
           'AJIB_DB_PATH': str(database), 'AJIB_BOT_DIR': str(state_dir), 'AJIB_SQLITE_ACTIVE': '1',
           'AJIB_BOT_ROLE': 'api', 'PYTHONDONTWRITEBYTECODE': '1'}
    if Path(database).resolve() == Path(web.load()['database']).resolve():
        env['AJIB_DB_SHARED_GROUP'] = 'ajib-state'
    web.run(python, '-c', 'from utils import web_store; web_store.initialize()', env=env, cwd=source)


def _copy_code(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in hashes(source):
        src, dst = Path(source) / name, destination / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o755 if dst.suffix == '.sh' else 0o644)
    for path in [destination, *destination.rglob('*')]:
        if path.is_dir():
            path.chmod(0o755)


def _prepare(plan, target, root, candidate_bot, candidate_web):
    # No secrets or live state are copied until both builds pass.
    if shutil.disk_usage(Path(plan['checkout']).parent).free < 3 * 1024**3:
        raise ValueError('At least 3 GiB free space is required to retain builds and rollback data.')
    available = re.search(r'MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text())
    if not available or int(available[1]) < 768 * 1024:
        raise ValueError('At least 768 MiB available memory is required before preparing an upgrade.')
    web.run('git', 'clone', '--no-checkout', REPOSITORY, candidate_bot)
    web.run('git', '-C', candidate_bot, 'checkout', '--detach', target['commit'])
    if _json(candidate_bot / CONTRACT) != target['contract']:
        raise ValueError('Downloaded release contract differs from the reviewed target.')
    for kind, requirements in (('bot', 'requirements.txt'), ('web', 'requirements-web.txt')):
        venv = root / (kind + '-venv')
        web.run(sys.executable, '-m', 'venv', venv)
        web.run(venv / 'bin/pip', 'install', '--disable-pip-version-check', '-r', candidate_bot / requirements)
    if shutil.which('npm'):
        web.run('npm', 'ci', cwd=candidate_bot / 'web')
        web.run('npm', 'run', 'build', cwd=candidate_bot / 'web')
    else:
        web.run('docker', 'run', '--rm', '--memory', '1g', '-v', f'{candidate_bot}/web:/app', '-w', '/app',
                'node:22-bookworm-slim', 'sh', '-c', 'npm ci && npm run build')
    _copy_code(candidate_bot, candidate_web)
    (candidate_bot / 'ajib_venv').symlink_to(root / 'bot-venv', target_is_directory=True)
    web.snapshot_database(plan['database'], root / 'private/rehearsal.db')
    _migrate(candidate_bot, root / 'web-venv/bin/python', root / 'private/rehearsal.db', root / 'private/rehearsal')
    if _schema(root / 'private/rehearsal.db') != (6, 1):
        raise ValueError('Candidate migration is incompatible with the rollback release.')


def _persist(journal, phase):
    journal['phase'] = phase
    web.write(web.CONFIG / 'upgrade.json', json.dumps(journal), 0o600)


def _drain(plan, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
            busy = db.execute("SELECT COUNT(*) FROM web_operations WHERE status IN ('processing','creating')").fetchone()[0]
            busy += db.execute("SELECT COUNT(*) FROM web_trials WHERE status='processing'").fetchone()[0]
        if not busy:
            return
        time.sleep(1)
    # Recovery must reconcile these operations; they are never marked retryable here.


def _edge(journal):
    inspection = subprocess.run(['docker', 'inspect', 'ajib-web-edge'], capture_output=True, text=True)
    if inspection.returncode == 0:
        web.run('docker', 'rm', '-f', 'ajib-web-edge')
    args = web.edge_args(journal['plan'], journal['plan']['uid'], journal['plan']['gid'])
    args[args.index(web.EDGE_IMAGE)] = journal['edge_image']
    web.run(*args)
    if not journal['edge_running']:
        web.run('docker', 'stop', 'ajib-web-edge')


def _healthy(journal, started):
    plan, active = journal['plan'], journal['active']
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        good = all(_active(unit) for unit in active)
        if good and BOT_UNIT in active:
            # Read readiness through the release's own normalizer.
            check = subprocess.run([str(Path(plan['checkout']) / 'ajib_venv/bin/python'), '-c',
                "import sys; sys.path.insert(0,'core'); import ajib_operator as o; "
                "raise SystemExit(0 if o._ready_for_fingerprint(o.config_fingerprint(o.load_config())) else 1)"],
                cwd=plan['checkout'], capture_output=True)
            good = check.returncode == 0
            if good:
                with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
                    active_ids = {row[0] for row in db.execute("SELECT reseller_id FROM hosted_bots WHERE status='active'")}
                    expected = set(journal['hosted_ids'])
                    good = expected <= active_ids and expected <= _hosted_processes()
        if good and 'ajib-web-worker' in active:
            with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
                row = db.execute("SELECT heartbeat_at,last_error FROM web_worker_health WHERE role='worker'").fetchone()
                good = bool(row and row[0] > started and row[1] is None)
        if good and 'ajib-web-api' in active:
            command = ['curl', '-fsS', '--max-time', '3', '--unix-socket', '/run/ajib-web/api.sock', 'http://localhost/api/v1/health']
            good = subprocess.run(command, capture_output=True).returncode == 0
            if good and journal['edge_running']:
                good = subprocess.run(['curl', '-fsS', '--max-time', '5', 'https://' + plan['domain'] + '/api/v1/health'], capture_output=True).returncode == 0
        if good:
            return
        time.sleep(1)
    raise ValueError('Upgraded services did not pass readiness checks.')


def _switch(entries, reverse=False):
    for item in reversed(entries) if reverse else entries:
        live, staged, old = (Path(item[key]) for key in ('live', 'staged', 'old'))
        if reverse:
            if _exists(old):
                if _exists(live):
                    _rename(live, staged)
                _rename(old, live)
        else:
            _rename(live, old)
            _rename(staged, live)


def _restore(journal):
    plan = journal['plan']
    _persist(journal, 'rolling_back')
    web.run('systemctl', 'stop', *web.UNITS, BOT_UNIT)
    _save_policy(plan, {**journal['policy'], 'accept_writes': 0, 'process_existing': 0})
    _switch(journal['switches'], reverse=True)
    original = Path(journal['root']) / 'private/runtime.env'
    if original.exists():
        web.write(web.CONFIG / 'runtime.env', original.read_text(), 0o640, gid=plan['gid'])
    baseline = Path(journal['root']) / 'private/managed-release.json'
    if baseline.exists():
        web.write(web.CONFIG / 'managed-release.json', baseline.read_text(), 0o600)
    _edge(journal)
    started = int(time.time())
    try:
        if journal['active']:
            web.run('systemctl', 'start', *journal['active'])
        _healthy(journal, started)
    except BaseException:
        # The bot also performs financial work; a paused web gate alone cannot
        # make an unverified rollback safe.
        web.run('systemctl', 'stop', *web.UNITS, BOT_UNIT)
        _persist(journal, 'recovery_required')
        raise
    _save_policy(plan, journal['policy'])
    _persist(journal, 'rolled_back')


def _finish(journal):
    web.write(Path(journal['root']) / 'private/result.json', json.dumps(journal), 0o600)
    (web.CONFIG / 'upgrade.json').unlink()


def recover():
    web.root_required()
    with web.maintenance():
        journal = _json(web.CONFIG / 'upgrade.json')
        if journal['plan']['checkout'] != web.load()['checkout'] or journal['plan']['database'] != web.load()['database']:
            raise ValueError('Deployment paths changed; recovery requires manual review.')
        if journal['phase'] not in {'complete', 'rolled_back'}:
            _restore(journal)
        _finish(journal)
        return {'status': journal['phase'], 'retained_release': journal['root']}


def upgrade(channel='stable', version=None):
    web.root_required()
    with web.maintenance():
        target = preview(channel, version)
        plan = web.load()
        import web_config
        from dotenv import dotenv_values
        current_environment = dict(dotenv_values(web.CONFIG / 'runtime.env', interpolate=False))
        desired_environment = web_config._environment(web_config._inputs(plan)['.env'])
        web_config.reject_secret_rotation(current_environment, {**desired_environment,
            **{key: value for key, value in current_environment.items() if key.startswith('AJIB_WEB_')}})
        ident = uuid.uuid4().hex
        RELEASES.mkdir(parents=True, exist_ok=True, mode=0o755)
        RELEASES.chmod(0o755)
        root = RELEASES / ident
        root.mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
        (root / 'private').mkdir(mode=0o700)
        bot = Path(plan['checkout'])
        staged_bot = bot.parent / ('.ajib-candidate-' + ident)
        staged_web = web.SOURCE.parent / ('.app-candidate-' + ident)
        _prepare(plan, target, root, staged_bot, staged_web)
        _check_baseline(plan)  # Fail before downtime if code changed during the build.
        inspection = _json_from_output(web.run('docker', 'inspect', 'ajib-web-edge', capture=True).stdout)[0]
        journal = {'id': ident, 'commit': target['commit'], 'started_at': int(time.time()), 'root': str(root),
                   'plan': plan, 'policy': _policy(plan), 'active': [unit for unit in (*web.UNITS, BOT_UNIT) if _active(unit)],
                   'edge_image': inspection['Image'], 'edge_running': inspection['State']['Running']}
        with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
            journal['hosted_ids'] = [row[0] for row in db.execute("SELECT reseller_id FROM hosted_bots WHERE status='active'")]
        staged_venv = web.VENV.with_name('.venv-candidate-' + ident)
        staged_venv.symlink_to(root / 'web-venv', target_is_directory=True)
        journal['switches'] = [{'live': str(live), 'staged': str(staged), 'old': str(live.with_name('.' + live.name + '-previous-' + ident))}
                               for live, staged in ((bot, staged_bot), (web.SOURCE, staged_web), (web.VENV, staged_venv))]
        web.write(root / 'private/runtime.env', (web.CONFIG / 'runtime.env').read_text(), 0o600)
        web.write(root / 'private/managed-release.json', (web.CONFIG / 'managed-release.json').read_text(), 0o600)
        _persist(journal, 'pausing')
        try:
            _save_policy(plan, {**journal['policy'], 'accept_writes': 0})
            web.run('systemctl', 'stop', 'ajib-web-api')
            _drain(plan)
            _persist(journal, 'stopping')
            web.run('systemctl', 'stop', 'ajib-web-worker', BOT_UNIT)
            _save_policy(plan, {**journal['policy'], 'accept_writes': 0, 'process_existing': 0})
            web.snapshot_database(plan['database'], root / 'private/ajib.db')
            web.backup_configuration()
            for name in MUTABLE:
                src, dst = bot / 'core/scripts/telegrambot' / name, staged_bot / 'core/scripts/telegrambot' / name
                if src.is_symlink():
                    raise ValueError('Mutable bot data cannot be a symlink during upgrade.')
                if src.is_dir():
                    if any(path.is_symlink() for path in src.rglob('*')):
                        raise ValueError('Mutable bot data contains a symlink; manual review is required.')
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                elif src.is_file():
                    shutil.copy2(src, dst)
            (staged_bot / 'core/scripts/telegrambot').chmod(0o700)
            # Synchronize only transport-independent settings and public catalogs.
            import web_config
            inputs = web_config._inputs(plan)
            from dotenv import dotenv_values
            current = dict(dotenv_values(web.CONFIG / 'runtime.env', interpolate=False))
            secret = dict(dotenv_values(staged_bot / 'core/scripts/telegrambot/.env', interpolate=False))
            if secret.get('API_TOKEN') != current.get('API_TOKEN'):
                raise ValueError('Secret rotation must be completed before a code upgrade.')
            web_config.reject_secret_rotation(current, {**secret,
                **{key: value for key, value in current.items() if key.startswith('AJIB_WEB_')}})
            secret.update({key: value for key, value in current.items() if key.startswith('AJIB_WEB_')})
            secret.update(AJIB_DB_PATH=plan['database'], AJIB_DB_SHARED_GROUP='ajib-state', AJIB_SQLITE_ACTIVE='1',
                          AJIB_BOT_DIR=str(web.SOURCE / 'core/scripts/telegrambot'))
            for key in ('AJIB_ENV_FILE', 'AJIB_BOT_ROLE'):
                secret.pop(key, None)
            for name in ('plans.json', 'support_info.json'):
                if inputs[name] is not None:
                    web.write(staged_web / 'core/scripts/telegrambot' / name, inputs[name])
            _persist(journal, 'switching')
            _switch(journal['switches'])
            web.write(web.CONFIG / 'runtime.env', web_config._encode(secret), 0o640, gid=plan['gid'])
            _migrate(bot, web.VENV / 'bin/python', Path(plan['database']), root / 'private/migration')
            _persist(journal, 'starting')
            _edge(journal)
            started = int(time.time())
            if journal['active']:
                web.run('systemctl', 'start', *journal['active'])
            _healthy(journal, started)
            _save_policy(plan, {**journal['policy'], 'revision': target['commit'], 'pilot_started_at': None})
            baseline = {'contract': target['contract'], 'commit': target['commit'], 'bot': hashes(bot), 'web': hashes(web.SOURCE)}
            web.write(web.CONFIG / 'managed-release.json', json.dumps(baseline), 0o600)
            _persist(journal, 'complete')
            _finish(journal)
            return {'status': 'complete', 'commit': target['commit'], 'retained_release': str(root)}
        except BaseException:
            try:
                _restore(journal)
                _finish(journal)
            except BaseException:
                # Never report success or reopen admission after an incomplete rollback.
                raise ValueError('Upgrade recovery is incomplete. Run ajib web upgrade-status and '
                                 'ajib web recover-upgrade --yes; keep financial processing paused.') from None
            raise ValueError('Upgrade failed; previous compatible code and service states were restored. '
                             'The live database was preserved.') from None


def _json_from_output(value):
    return json.loads(value)


def _hosted_processes():
    """Verify hosted identities in this unit's cgroup, without logging environment."""
    group = web.run('systemctl', 'show', BOT_UNIT, '--property=ControlGroup', '--value', capture=True).stdout.strip()
    base = Path('/sys/fs/cgroup')
    path = (base / group.lstrip('/') / 'cgroup.procs').resolve()
    if not group or not path.is_relative_to(base):
        return set()
    result = set()
    try:
        pids = path.read_text().split()
        for pid in pids:
            try:
                values = dict(item.split(b'=', 1) for item in Path('/proc', pid, 'environ').read_bytes().split(b'\0') if b'=' in item)
                ident = values.get(b'AJIB_HOSTED_RESELLER_ID', b'').decode()
                if values.get(b'AJIB_BOT_ROLE') == b'hosted' and ident:
                    result.add(ident)
            except (OSError, UnicodeError):
                continue
    except OSError:
        pass
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=['stable', 'main'], default='stable')
    parser.add_argument('--version')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args()
    try:
        if args.dry_run:
            print(json.dumps(preview(args.channel, args.version), indent=2))
        elif not args.yes:
            raise ValueError('Preview with --dry-run, then apply with --yes.')
        else:
            print(json.dumps(upgrade(args.channel, args.version), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('Upgrade: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
