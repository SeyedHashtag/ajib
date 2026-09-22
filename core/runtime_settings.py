"""Private, journaled settings changes shared by Telegram and the operator CLI."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid

import click
import ajib_operator as operator
import web_operator as web
import web_upgrade as upgrade


def _config():
    import web_config
    return web_config


def _env_path(plan):
    return Path(plan['checkout']) / 'core/scripts/telegrambot/.env'


def _private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Settings input must be a private regular file.')
    return path


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _patch_environment(before, current, desired):
    """Preserve unrelated lines and the operator's unquoted JSON configuration."""
    changed = {k: v for k, v in desired.items() if current.get(k) != v}
    output = []
    for line in before.splitlines(keepends=True):
        key = line.split('=', 1)[0].strip() if '=' in line else None
        if key not in changed:
            output.append(line if line.endswith('\n') else line + '\n')
    raw_keys = {'API_TOKEN', 'ADMIN_USER_IDS', 'URL', 'TOKEN', 'SERVERS_JSON', 'AJIB_CONFIG_FINGERPRINT'}
    for key, value in changed.items():
        output.append(key + '=' + (str(value) if key in raw_keys else json.dumps(str(value))) + '\n')
    return ''.join(output)


def _panel(server):
    sys.path.insert(0, str(Path(__file__).parent / 'scripts/telegrambot'))
    from utils.api_client import ThreeXUIAPIClient
    return ThreeXUIAPIClient(server)


def inbound_options(server_id):
    plan = web.load()
    config = operator.load_config(_env_path(plan))
    server = next((s for s in config['servers'] if s['id'] == server_id), None)
    if not server or server['panel'] != '3x-ui':
        raise ValueError('Select a configured 3x-ui server.')
    options = _panel(server).get_inbound_options()
    if options is None:
        raise ValueError('The panel is unavailable; defaults were not changed.')
    return {'server_id': server_id, 'expected': operator.config_fingerprint(config),
            'selected': server['default_inbound_ids'],
            'options': [{'id': o['id'], 'protocol': o.get('protocol', ''),
                         'enabled': o.get('enable', True)} for o in options]}


def _authorize(values, actor):
    if actor is not None and str(actor) not in {str(v) for v in json.loads(values.get('ADMIN_USER_IDS') or '[]')}:
        raise ValueError('Administrator permission was revoked.')


def _inbound_change(plan, values, change):
    config = operator.load_config(_env_path(plan))
    if operator.config_fingerprint(config) != change['expected']:
        raise ValueError('Configuration changed. Reopen the inbound selection.')
    server = next((s for s in config['servers'] if s['id'] == change['server_id']), None)
    if not server or server['panel'] != '3x-ui' or operator.active_transfer_for_server(server['id']):
        raise ValueError('Server is unavailable or used by an active transfer.')
    selected = sorted(set(change['inbound_ids']))
    if not selected or any(type(v) is not int or v <= 0 for v in selected):
        raise ValueError('Select at least one valid inbound.')
    options = _panel(server).get_inbound_options()
    available = {o['id'] for o in (options or []) if o.get('enable', True)}
    if not set(selected) <= available:
        raise ValueError('Selected inbounds are unavailable. Reopen the selection.')
    server['default_inbound_ids'] = selected
    values.update(operator._env_updates(config))
    return values


def _crypto_change(values, source):
    """Recover only a missing merchant ID paired with the installed API key."""
    from dotenv import dotenv_values
    if values.get('CRYPTO_MERCHANT_ID') or not values.get('CRYPTO_API_KEY'):
        raise ValueError('Restoration requires a missing merchant ID and an existing API key.')
    path = _private(source)
    candidates = []
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for item in archive.getmembers():
                if item.isfile() and item.size < 1048576 and Path(item.name).name in {'.env', 'runtime.env'}:
                    candidates.append(archive.extractfile(item).read().decode())
    else:
        candidates.append(path.read_text())
    merchants = set()
    for text in candidates:
        old = dotenv_values(stream=io.StringIO(text), interpolate=False)
        if old.get('CRYPTO_API_KEY') == values['CRYPTO_API_KEY'] and old.get('CRYPTO_MERCHANT_ID'):
            try:
                uuid.UUID(old['CRYPTO_MERCHANT_ID'])
            except ValueError:
                continue
            merchants.add(old['CRYPTO_MERCHANT_ID'])
    if len(merchants) != 1:
        raise ValueError('No unique historical merchant matches the installed API key.')
    return {**values, 'CRYPTO_MERCHANT_ID': merchants.pop()}


def prepare(change, *, actor=None):
    web.root_required()
    with web.maintenance():
        upgrade._no_pending()
        plan = web.load()
        upgrade._check_baseline(plan)
        before = _env_path(plan).read_text()
        values = _config()._environment(before)
        _authorize(values, actor)
        if change['kind'] == 'inbounds':
            _inbound_change(plan, dict(values), change)
        elif change['kind'] == 'crypto':
            _crypto_change(dict(values), change['source'])
        else:
            raise ValueError('Unsupported settings change.')
        return {'change': change, 'actor': actor, 'input_digest': _digest(before)}


def _persist(journal, phase):
    journal['phase'] = phase
    web.write(web.CONFIG / 'settings.json', json.dumps(journal), 0o600)


def _notify(journal):
    if journal.get('actor') is None or journal.get('notified'):
        return
    values = _config()._environment(_env_path(journal['plan']).read_text())
    _authorize(values, journal['actor'])
    import requests
    response = requests.post('https://api.telegram.org/bot' + values['API_TOKEN'] + '/sendMessage',
        json={'chat_id': journal['actor'], 'text': 'Inbound defaults updated. Existing accounts are unchanged.'}, timeout=15)
    if response.status_code != 200 or not response.json().get('ok'):
        raise ValueError('Settings applied; administrator notification requires retry.')
    journal['notified'] = True
    web.write(Path(journal['root']) / 'result.json', json.dumps(journal), 0o600)


def apply(request=None, *, recover=False):
    web.root_required()
    with web.maintenance():
        path = web.CONFIG / 'settings.json'
        if recover:
            if not path.exists():
                raise ValueError('No settings recovery is pending.')
            journal = json.loads(path.read_text())
            plan = web.load()
            if journal['plan'] != plan:
                raise ValueError('Deployment configuration changed; inspect settings recovery privately.')
        else:
            upgrade._no_pending()
            plan = web.load()
            upgrade._check_baseline(plan)
            before = _env_path(plan).read_text()
            if _digest(before) != request['input_digest']:
                raise ValueError('Configuration changed before application; reopen the selection.')
            values = _config()._environment(before)
            _authorize(values, request.get('actor'))
            change = request['change']
            desired = (_inbound_change(plan, dict(values), change) if change['kind'] == 'inbounds'
                       else _crypto_change(dict(values), change['source']))
            current_web = _config()._environment((web.CONFIG / 'runtime.env').read_text())
            expected_web = {**values, **{k: v for k, v in current_web.items() if k.startswith('AJIB_WEB_')}}
            _config().reject_secret_rotation(current_web, expected_web)
            merged = {k: v for k, v in desired.items() if not k.startswith('AJIB_WEB_')}
            merged.update({k: v for k, v in current_web.items() if k.startswith('AJIB_WEB_')})
            merged.update(AJIB_DB_PATH=plan['database'], AJIB_DB_SHARED_GROUP='ajib-state',
                          AJIB_SQLITE_ACTIVE='1', AJIB_BOT_DIR=str(web.SOURCE / 'core/scripts/telegrambot'))
            for key in ('AJIB_ENV_FILE', 'AJIB_BOT_ROLE'):
                merged.pop(key, None)
            root = web.CONFIG / 'history' / ('settings-' + uuid.uuid4().hex)
            root.mkdir(mode=0o700, parents=True)
            for name, content in [('bot-before.env', before), ('web-before.env', (web.CONFIG / 'runtime.env').read_text()),
                                  ('bot-after.env', _patch_environment(before, values, desired)), ('web-after.env', _config()._encode(merged))]:
                web.write(root / name, content, 0o600)
            journal = {'root': str(root), 'plan': plan, 'actor': request.get('actor'),
                       'active': [u for u in (*web.UNITS, upgrade.BOT_UNIT) if upgrade._active(u)],
                       'hosted_ids': sorted(upgrade._hosted_processes()), 'policy': upgrade._policy(plan),
                       'edge_running': True, 'input_digest': request['input_digest']}
            _persist(journal, 'prepared')
        if any(u not in (*web.UNITS, upgrade.BOT_UNIT) for u in journal['active']):
            raise ValueError('Unexpected service in settings journal.')
        try:
            if journal['phase'] != 'complete':
                upgrade._save_policy(plan, {**journal['policy'], 'accept_writes': 0})
                upgrade._drain(plan)
                upgrade._save_policy(plan, {**journal['policy'], 'accept_writes': 0, 'process_existing': 0})
                _persist(journal, 'stopping')
                web.run('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT)
                root = Path(journal['root'])
                for target, prefix, mode in [(_env_path(plan), 'bot', 0o600), (web.CONFIG / 'runtime.env', 'web', 0o640)]:
                    current = target.read_text()
                    if current not in {(root / (prefix + '-before.env')).read_text(), (root / (prefix + '-after.env')).read_text()}:
                        raise ValueError('Configuration changed during maintenance; forward repair is required.')
                _persist(journal, 'installing')
                if not (root / 'state.db').exists():
                    web.snapshot_database(plan['database'], root / 'state.db')
                with operator.config_lock(_env_path(plan)):
                    web.write(_env_path(plan), (root / 'bot-after.env').read_text(), 0o600)
                    web.write(web.CONFIG / 'runtime.env', (root / 'web-after.env').read_text(), 0o640, gid=plan['gid'])
                _persist(journal, 'starting')
                started = int(time.time())
                if journal['active']:
                    web.run('systemctl', 'start', *journal['active'])
                upgrade._healthy(journal, started)
                upgrade._save_policy(plan, journal['policy'])
                _persist(journal, 'complete')
                web.write(root / 'result.json', json.dumps(journal), 0o600)
            path.unlink()
        except BaseException:
            upgrade._save_policy(plan, {**journal['policy'], 'accept_writes': 0, 'process_existing': 0})
            web.run('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT)
            _persist(journal, 'recovery_required')
            raise ValueError('Settings require recovery: ajib settings recover --yes.') from None
    # Delivery cannot roll back committed settings or repeat their application.
    try:
        _notify(journal)
    except Exception:
        return {'status': 'applied', 'notification': 'pending', 'result': str(Path(journal['root']) / 'result.json')}
    return {'status': 'applied', 'notification': 'complete'}


def queue_inbounds(server_id, inbound_ids, expected, actor):
    request = prepare({'kind': 'inbounds', 'server_id': server_id, 'inbound_ids': inbound_ids, 'expected': expected}, actor=actor)
    root = web.CONFIG / 'history' / ('request-' + uuid.uuid4().hex)
    root.mkdir(mode=0o700, parents=True)
    path = root / 'request.json'
    web.write(path, json.dumps(request), 0o600)
    # A separate systemd cgroup survives the bot supervisor's coordinated stop.
    subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=ajib-settings-' + root.name,
                    sys.executable, str(Path(__file__).resolve()), 'apply', str(path)], check=True, capture_output=True)


@click.group('settings')
def settings_group():
    """Coordinate private runtime settings across bot and website."""


@settings_group.command('inbounds')
@click.argument('server_id')
@click.option('--inbound-id', multiple=True, required=True, type=click.IntRange(min=1))
@click.option('--yes', is_flag=True)
def inbounds_command(server_id, inbound_id, yes):
    options = inbound_options(server_id)
    request = prepare({'kind': 'inbounds', 'server_id': server_id,
                       'inbound_ids': list(inbound_id), 'expected': options['expected']})
    click.echo(json.dumps(apply(request) if yes else {'status': 'preview', 'inbound_ids': list(inbound_id), 'existing_accounts_changed': False}))


@settings_group.command('restore-crypto')
@click.option('--source', required=True, type=click.Path(exists=True))
@click.option('--yes', is_flag=True)
def crypto_command(source, yes):
    request = prepare({'kind': 'crypto', 'source': str(Path(source).resolve())})
    click.echo(json.dumps(apply(request) if yes else {'status': 'preview', 'merchant_recoverable': True, 'api_key_changed': False}))


@settings_group.command('recover')
@click.option('--yes', is_flag=True, required=True)
def recover_command(yes):
    click.echo(json.dumps(apply(recover=True)))


@settings_group.command('apply', hidden=True)
@click.argument('request_file', type=click.Path(exists=True))
def apply_command(request_file):
    request_file = _private(request_file)
    result = apply(json.loads(request_file.read_text()))
    web.write(request_file.parent / 'outcome.json', json.dumps(result), 0o600)
    click.echo(json.dumps(result))


@settings_group.command('retry-notification')
@click.argument('result_file', type=click.Path(exists=True))
@click.option('--yes', is_flag=True, required=True)
def retry_notification_command(result_file, yes):
    web.root_required()
    journal = json.loads(_private(result_file).read_text())
    if journal.get('phase') != 'complete':
        raise click.ClickException('Settings are not complete; use settings recovery.')
    _notify(journal)
    click.echo('Administrator notification complete.')


if __name__ == '__main__':
    settings_group()
