"""CLI-only synchronization of the installed website's bot configuration.

No database restoration, Telegram calls, or changes to the bot/proxy service.
Interrupted work leaves a private journal; recovery retries from current bot data.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from dotenv import dotenv_values
import web_operator as web

CATALOGS = ('plans.json', 'support_info.json')


def _live_policy(plan):
    with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
        installed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_release_control'").fetchone()
    if installed:
        import web_upgrade
        return web_upgrade._policy(plan)
    return None


def reject_secret_rotation(current, desired):
    keys = set(current) | set(desired)
    sensitive = {key for key in keys if re.search(r'(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|MERCHANT_ID)$', key)}
    if any(current.get(key) != desired.get(key) for key in sensitive):
        raise ValueError('Credential changes require coordinated CLI secret rotation; sync-config cannot rotate secrets.')
    old_servers = {str(item.get('id')): item for item in json.loads(current.get('SERVERS_JSON') or '[]')}
    new_servers = {str(item.get('id')): item for item in json.loads(desired.get('SERVERS_JSON') or '[]')}
    for key in old_servers.keys() & new_servers.keys():
        for secret in ('token', 'TOKEN', 'password', 'username'):
            if old_servers[key].get(secret) != new_servers[key].get(secret):
                raise ValueError('Existing panel credentials require coordinated CLI secret rotation.')


def _read(path):
    if path.is_symlink():
        raise ValueError('Configuration synchronization does not follow file symlinks.')
    return path.read_text(encoding='utf-8') if path.exists() else None


def _environment(text):
    return dict(dotenv_values(stream=io.StringIO(text), interpolate=False))


def _encode(values):
    return ''.join(key + '=' + json.dumps(str(value), ensure_ascii=False) + '\n'
                   for key, value in sorted(values.items()) if value is not None)


def _inputs(plan):
    bot = Path(plan['checkout']) / 'core/scripts/telegrambot'
    values = {name: _read(bot / name) for name in ('.env', *CATALOGS)}
    if not values['.env']:
        raise ValueError('The installed bot environment is missing.')
    for name in CATALOGS:
        if values[name] is not None:
            try:
                value = json.loads(values[name])
                if not isinstance(value, dict):
                    raise ValueError()
            except ValueError:
                raise ValueError(f'{name} must contain a valid JSON object.') from None
    return values


def _digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _desired(plan, inputs):
    current = _environment(_read(web.CONFIG / 'runtime.env') or '')
    bot = _environment(inputs['.env'])
    if not bot.get('API_TOKEN') or bot.get('API_TOKEN') != current.get('API_TOKEN'):
        raise ValueError('Bot token changes require coordinated CLI secret rotation; sync-config cannot rotate identity.')
    reject_secret_rotation(current, {**bot, **{key: value for key, value in current.items() if key.startswith('AJIB_WEB_')}})
    if current.get('AJIB_WEB_WRITES_ENABLED') != '0' and _live_policy(plan) is None:
        raise ValueError('Install coordinated release controls before synchronizing an installation with writes enabled.')
    if current.get('AJIB_WEB_ORIGIN') != 'https://' + plan['domain']:
        raise ValueError('Website origin differs from the deployment manifest.')
    if current.get('AJIB_DB_PATH') != plan['database']:
        raise ValueError('Website database differs from the deployment manifest.')
    # Drop removed adapter settings, preserve every website-specific setting,
    # and force the installed shared-state paths. Bot .env cannot open the gates.
    merged = {key: value for key, value in bot.items() if not key.startswith('AJIB_WEB_')}
    merged.update({key: value for key, value in current.items() if key.startswith('AJIB_WEB_')})
    merged.update(AJIB_DB_PATH=plan['database'], AJIB_DB_SHARED_GROUP='ajib-state',
                  AJIB_SQLITE_ACTIVE='1', AJIB_BOT_DIR=str(web.SOURCE / 'core/scripts/telegrambot'))
    for key in ('AJIB_ENV_FILE', 'AJIB_BOT_ROLE'):
        merged.pop(key, None)  # Owned by the systemd unit, never by the bot .env.
    keys = sorted(key for key in set(merged) | set(current) if merged.get(key) != current.get(key))
    return _encode(merged), keys


def _targets():
    return {'runtime.env': web.CONFIG / 'runtime.env',
            **{name: web.SOURCE / 'core/scripts/telegrambot' / name for name in CATALOGS}}


def preview():
    """Return only names/status, never values or hashes of secrets."""
    plan = web.load()
    inputs = _inputs(plan)
    _text, keys = _desired(plan, inputs)
    policy = _live_policy(plan)
    return {'environment_keys': keys,
            'catalogs': [name for name in CATALOGS if _read(_targets()[name]) != inputs[name]],
            'restart_services': list(web.UNITS), 'writes_enabled': bool(policy and policy['accept_writes']),
            'pause_and_drain': policy is not None,
            'recovery_pending': (web.CONFIG / 'config-sync.json').exists()}


def _active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit], capture_output=True).returncode == 0


def _bot_ready(plan):
    import ajib_operator as operator
    try:
        config = operator.load_config(Path(plan['checkout']) / 'core/scripts/telegrambot/.env')
        ready = config and operator._ready_for_fingerprint(operator.config_fingerprint(config))
    except (ValueError, operator.OperatorError):
        ready = False
    if not _active('ajib-telegram-bot') or not ready:
        raise ValueError('The bot must be running and ready with its saved configuration before synchronization.')


def _health(active, plan, started):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        healthy = all(_active(unit) for unit in active)
        if healthy and 'ajib-web-api' in active:
            check = subprocess.run(['curl', '-fsS', '--max-time', '2', '--unix-socket',
                                    '/run/ajib-web/api.sock', 'http://localhost/api/v1/health'], capture_output=True)
            healthy = check.returncode == 0
        if healthy and 'ajib-web-worker' in active:
            try:
                with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True, timeout=1) as db:
                    row = db.execute("SELECT heartbeat_at,last_error,writes_enabled FROM web_worker_health WHERE role='worker'").fetchone()
                    healthy = bool(row and row[0] > started and row[1] is None and row[2] == 0)
            except sqlite3.Error:
                healthy = False
        if healthy:
            return
        time.sleep(1)
    raise ValueError('Website readiness did not recover after synchronization.')


def _install(contents, gid):
    for name, target in _targets().items():
        if contents[name] is None:
            target.unlink(missing_ok=True)
        else:
            web.write(target, contents[name], 0o640 if name == 'runtime.env' else 0o644,
                      gid=gid if name == 'runtime.env' else None)


def sync(*, recover=False):
    web.root_required()
    with web.maintenance():
        if (web.CONFIG / 'upgrade.json').exists():
            raise ValueError('Recover the interrupted upgrade first: ajib web recover-upgrade --yes.')
        journal_path = web.CONFIG / 'config-sync.json'
        pending = journal_path.exists()
        if pending and not recover:
            raise ValueError('An interrupted sync requires ajib web sync-config --recover --yes.')
        if recover and not pending:
            raise ValueError('There is no interrupted configuration sync to recover.')
        plan = web.load()
        inputs = _inputs(plan)
        environment, keys = _desired(plan, inputs)
        _bot_ready(plan)
        changes = [name for name in CATALOGS if _read(_targets()[name]) != inputs[name]]
        if not pending and not keys and not changes:
            return {'status': 'unchanged', 'restarted_services': []}
        if pending:
            journal = json.loads(journal_path.read_text())
            if journal['checkout'] != plan['checkout'] or journal['database'] != plan['database']:
                raise ValueError('Deployment paths changed since the interrupted sync; manual review is required.')
        else:
            ident = 'config-sync-' + uuid.uuid4().hex
            backup = web.CONFIG / 'history' / ident
            backup.mkdir(parents=True, mode=0o700)
            journal = {'id': ident, 'checkout': plan['checkout'], 'database': plan['database'],
                       'active': [unit for unit in web.UNITS if _active(unit)],
                       'phase': 'prepared', 'backup': str(backup), 'policy': _live_policy(plan)}
            for name, target in _targets().items():
                old = _read(target)
                if old is not None:
                    web.write(backup / name, old, 0o600)
        active = journal['active']
        if any(unit not in web.UNITS for unit in active):
            raise ValueError('Unexpected service in the sync journal.')
        journal.update(phase='stopping', input_digest=_digest(inputs))
        web.write(journal_path, json.dumps(journal), 0o600)
        try:
            if journal.get('policy') is not None:
                import web_upgrade
                original_policy = journal['policy']
                web_upgrade._save_policy(plan, {**original_policy, 'accept_writes': 0,
                    'process_existing': 0 if pending else original_policy['process_existing']})
                web.run('systemctl', 'stop', 'ajib-web-api')
                web_upgrade._drain(plan)
                web_upgrade._save_policy(plan, {**original_policy, 'accept_writes': 0, 'process_existing': 0})
            # Stop both in recovery too: an interrupted start may have left one running.
            web.run('systemctl', 'stop', *web.UNITS)
            if _inputs(plan) != inputs:
                raise ValueError('Bot configuration changed during synchronization; retry recovery.')
            _bot_ready(plan)
            journal['phase'] = 'installing'
            web.write(journal_path, json.dumps(journal), 0o600)
            _install({'runtime.env': environment, **{name: inputs[name] for name in CATALOGS}}, plan['gid'])
            if _inputs(plan) != inputs:
                raise ValueError('Bot configuration changed during synchronization; retry recovery.')
            journal['phase'] = 'starting'
            web.write(journal_path, json.dumps(journal), 0o600)
            started = int(time.time())
            if active:
                web.run('systemctl', 'start', *active)
                _health(active, plan, started)
            _bot_ready(plan)
            if _inputs(plan) != inputs:
                raise ValueError('Bot configuration changed during synchronization; retry recovery.')
            if journal.get('policy') is not None:
                web_upgrade._save_policy(plan, journal['policy'])
            journal['phase'] = 'complete'
            web.write(Path(journal['backup']) / 'result.json', json.dumps(journal), 0o600)
            journal_path.unlink()
            return {'status': 'synchronized', 'restarted_services': active, 'backup': journal['backup']}
        except BaseException:
            # Do not resume a stale permission/configuration snapshot. Keep the new
            # settings and journal for a retry against the now-current bot settings.
            try:
                try:
                    if journal.get('policy') is not None:
                        web_upgrade._save_policy(plan, {**journal['policy'], 'accept_writes': 0, 'process_existing': 0})
                finally:
                    web.run('systemctl', 'stop', *web.UNITS)
            except Exception:
                journal['phase'] = 'stop_failed'
            else:
                journal['phase'] = 'recovery_required'
            web.write(journal_path, json.dumps(journal), 0o600)
            raise ValueError('Sync did not complete. Check ajib web status/logs; retry with '
                             'ajib web sync-config --recover --yes. The bot was not restarted.') from None
