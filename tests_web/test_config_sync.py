"""Synthetic filesystem/service tests; no real tokens, systemd or external calls."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import web_config as sync
import web_operator as web
from web_cli import web_group


@pytest.fixture
def installed(tmp_path, monkeypatch):
    config, source, checkout = (tmp_path / name for name in ('config', 'source', 'checkout'))
    config.mkdir()
    for root in (source, checkout):
        (root / 'core/scripts/telegrambot').mkdir(parents=True)
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(web, 'SOURCE', source)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'maintenance', contextlib.nullcontext)
    monkeypatch.setattr(sync, '_bot_ready', lambda plan: None)
    monkeypatch.setattr(sync, '_health', lambda *args: None)
    actual_write = web.write
    # The installed runtime must be owned by root:ajib-state on the real VPS.
    # This fixture also runs under non-root Windows; mode tests run on Linux.
    monkeypatch.setattr(web, 'write', lambda path, text, mode=0o644, **kw: actual_write(path, text, mode))
    plan = {'checkout': str(checkout), 'domain': 'example.com',
            'database': str(tmp_path / 'live.db'), 'gid': 1000}
    (config / 'deployment.json').write_text(json.dumps(plan))
    with sqlite3.connect(plan['database']) as db:
        db.execute('CREATE TABLE payments (id INTEGER)')
        db.execute('INSERT INTO payments VALUES (1)')
    bot = checkout / 'core/scripts/telegrambot'
    (bot / '.env').write_text('API_TOKEN=synthetic\nADMIN_USER_IDS=[1]\nURL=new-adapter\nAJIB_WEB_PUBLIC_PORTAL=1\n')
    (bot / 'plans.json').write_text('{"100":{"price":2}}')
    current = {'API_TOKEN': 'synthetic', 'ADMIN_USER_IDS': '[1]', 'URL': 'old-adapter',
               'REMOVED_SETTING': 'old', 'AJIB_WEB_ORIGIN': 'https://example.com',
               'AJIB_WEB_WRITES_ENABLED': '0', 'AJIB_WEB_PUBLIC_PORTAL': '0',
               'AJIB_WEB_PILOT_USERS': '42', 'AJIB_WEB_BOT_USERNAME': 'SyntheticBot',
               'AJIB_DB_PATH': plan['database'], 'AJIB_DB_SHARED_GROUP': 'ajib-state',
               'AJIB_SQLITE_ACTIVE': '1', 'AJIB_BOT_DIR': str(source / 'core/scripts/telegrambot')}
    (config / 'runtime.env').write_text(sync._encode(current))
    (source / 'core/scripts/telegrambot/plans.json').write_text('{"100":{"price":1}}')
    (source / 'core/scripts/telegrambot/support_info.json').write_text('{"text":"old"}')
    active = set(web.UNITS)
    commands = []
    def run(*args, **kwargs):
        commands.append(args)
        assert args[0] == 'systemctl' and args[1] in ('stop', 'start')
        assert set(args[2:]) <= set(web.UNITS)
        if args[1] == 'stop':
            active.difference_update(args[2:])
        else:
            active.update(args[2:])
    monkeypatch.setattr(web, 'run', run)
    monkeypatch.setattr(sync, '_active', lambda unit: unit in active)
    return config, bot, plan, active, commands


def test_preview_redacts_settings_and_does_not_mutate(installed):
    config, bot, plan, active, commands = installed
    result = CliRunner().invoke(web_group, ['sync-config', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert 'URL' in result.output and 'REMOVED_SETTING' in result.output
    assert 'old-adapter' not in result.output and 'new-adapter' not in result.output
    assert 'synthetic' not in result.output
    assert commands == []
    assert not (config / 'config-sync.json').exists()


def test_sync_keeps_gates_drops_removed_settings_and_leaves_database(installed):
    config, bot, plan, active, commands = installed
    result = sync.sync()
    settings = sync._environment((config / 'runtime.env').read_text())
    assert settings['URL'] == 'new-adapter'
    assert 'REMOVED_SETTING' not in settings
    assert settings['AJIB_WEB_PUBLIC_PORTAL'] == '0'
    assert settings['AJIB_WEB_WRITES_ENABLED'] == '0'
    assert settings['AJIB_WEB_PILOT_USERS'] == '42'
    assert settings['AJIB_DB_PATH'] == plan['database']
    assert sync._targets()['plans.json'].read_text() == (bot / 'plans.json').read_text()
    assert not sync._targets()['support_info.json'].exists()
    assert active == set(web.UNITS)
    assert not (config / 'config-sync.json').exists()
    backup = Path(result['backup'])
    assert 'old-adapter' in (backup / 'runtime.env').read_text()
    if os.name == 'posix':
        assert (backup / 'runtime.env').stat().st_mode & 0o777 == 0o600
        assert (config / 'runtime.env').stat().st_mode & 0o777 == 0o640
    with sqlite3.connect(plan['database']) as db:
        assert db.execute('SELECT id FROM payments').fetchall() == [(1,)]
    commands.clear()
    assert sync.sync()['status'] == 'unchanged'
    assert commands == []


@pytest.mark.parametrize('previous', [set(), {'ajib-web-api'}, {'ajib-web-worker'}])
def test_preserves_previously_stopped_services(installed, previous):
    config, bot, plan, active, commands = installed
    active.clear()
    active.update(previous)
    sync.sync()
    assert active == previous


@pytest.mark.parametrize('point', ['install', 'start', 'health'])
def test_interruption_recovers_without_losing_new_payments(installed, monkeypatch, point):
    config, bot, plan, active, commands = installed
    original_install, original_run = sync._install, web.run
    def fail(*args):
        raise KeyboardInterrupt()
    if point == 'install':
        def partial(contents, gid):
            web.write(config / 'runtime.env', contents['runtime.env'], 0o640)
            fail()
        monkeypatch.setattr(sync, '_install', partial)
    elif point == 'start':
        def partial_start(*args):
            original_run(*args)
            if args[1] == 'start':
                fail()
        monkeypatch.setattr(web, 'run', partial_start)
    else:
        monkeypatch.setattr(sync, '_health', fail)
    with pytest.raises(ValueError, match='--recover'):
        sync.sync()
    assert active == set()
    assert (config / 'config-sync.json').exists()
    with pytest.raises(ValueError, match='--recover'):
        sync.sync()
    # Bot continues taking payments while web maintenance is paused.
    with sqlite3.connect(plan['database']) as db:
        db.execute('INSERT INTO payments VALUES (2)')
    (bot / 'plans.json').write_text('{"200":{"price":3}}')
    monkeypatch.setattr(sync, '_install', original_install)
    monkeypatch.setattr(web, 'run', original_run)
    monkeypatch.setattr(sync, '_health', lambda *args: None)
    assert sync.sync(recover=True)['status'] == 'synchronized'
    assert active == set(web.UNITS)
    assert sync._targets()['plans.json'].read_text() == (bot / 'plans.json').read_text()
    with sqlite3.connect(plan['database']) as db:
        assert db.execute('SELECT id FROM payments').fetchall() == [(1,), (2,)]


def test_changed_bot_configuration_during_stop_requires_recovery(installed, monkeypatch):
    config, bot, plan, active, commands = installed
    original = web.run
    def change(*args):
        original(*args)
        (bot / 'plans.json').write_text('{"300":{"price":4}}')
    monkeypatch.setattr(web, 'run', change)
    with pytest.raises(ValueError, match='--recover'):
        sync.sync()
    assert active == set()
    assert 'old-adapter' in (config / 'runtime.env').read_text()


@pytest.mark.parametrize('problem', ['token', 'writes', 'json', 'unready', 'database'])
def test_invalid_preflight_does_not_stop_services(installed, monkeypatch, problem):
    config, bot, plan, active, commands = installed
    if problem == 'token':
        (bot / '.env').write_text('API_TOKEN=different\nADMIN_USER_IDS=[1]\n')
    elif problem in ('writes', 'database'):
        text = (config / 'runtime.env').read_text()
        settings = sync._environment(text)
        settings['AJIB_WEB_WRITES_ENABLED' if problem == 'writes' else 'AJIB_DB_PATH'] = '1'
        (config / 'runtime.env').write_text(sync._encode(settings))
    elif problem == 'json':
        (bot / 'plans.json').write_text('{invalid')
    else:
        def unready(plan):
            raise ValueError('not ready')
        monkeypatch.setattr(sync, '_bot_ready', unready)
    with pytest.raises(ValueError):
        sync.sync()
    assert commands == []
    assert active == set(web.UNITS)


def test_restart_cannot_bypass_pending_recovery(installed):
    config, bot, plan, active, commands = installed
    (config / 'config-sync.json').write_text('{}')
    result = CliRunner().invoke(web_group, ['restart'])
    assert result.exit_code != 0 and '--recover' in result.output
    assert commands == []


@pytest.mark.parametrize('failure', [False, True])
def test_live_sync_pauses_drains_and_recovers_original_access_policy(installed, monkeypatch, failure):
    import web_upgrade
    config, bot, plan, active, commands = installed
    with sqlite3.connect(plan['database']) as db:
        db.execute('''CREATE TABLE web_release_control (id INTEGER PRIMARY KEY,access TEXT,pilot_users_json TEXT,
            accept_writes INTEGER,process_existing INTEGER,revision TEXT,pilot_started_at INTEGER,updated_at INTEGER)''')
        db.execute("INSERT INTO web_release_control VALUES (1,'pilot','[2,3]',1,1,'revision',123,123)")
    original = web_upgrade._policy(plan)
    drains = []
    def drain(plan):
        policy = web_upgrade._policy(plan)
        assert policy['accept_writes'] == 0
        assert 'ajib-web-api' not in active
        drains.append(policy['process_existing'])
    monkeypatch.setattr(web_upgrade, '_drain', drain)
    def health(*args):
        policy = web_upgrade._policy(plan)
        assert policy['accept_writes'] == policy['process_existing'] == 0
        if failure:
            raise ValueError('synthetic health failure')
    monkeypatch.setattr(sync, '_health', health)
    assert sync.preview()['pause_and_drain']
    if failure:
        with pytest.raises(ValueError, match='--recover'):
            sync.sync()
        assert active == set()
        policy = web_upgrade._policy(plan)
        assert policy['accept_writes'] == policy['process_existing'] == 0
        with sqlite3.connect(plan['database']) as db:
            db.execute('INSERT INTO payments VALUES(2)')
        failure = False
        sync.sync(recover=True)
        assert drains == [1, 0]
    else:
        sync.sync()
        assert drains == [1]
    current = web_upgrade._policy(plan)
    for key in ('access', 'pilot_users_json', 'accept_writes', 'process_existing', 'revision', 'pilot_started_at'):
        assert current[key] == original[key]
    assert active == set(web.UNITS)
    assert not (config / 'config-sync.json').exists()


@pytest.mark.parametrize('key', ['TOKEN', 'CRYPTO_API_KEY', 'CRYPTO_MERCHANT_ID', 'PANEL_PASSWORD'])
def test_sync_rejects_credential_changes_before_stopping(installed, key):
    config, bot, plan, active, commands = installed
    settings = sync._environment((config / 'runtime.env').read_text())
    settings[key] = 'synthetic-old'
    (config / 'runtime.env').write_text(sync._encode(settings))
    with pytest.raises(ValueError, match='secret rotation'):
        sync.sync()
    assert not commands


def test_sync_rejects_rotated_token_inside_server_catalog(installed):
    config, bot, plan, active, commands = installed
    settings = sync._environment((config / 'runtime.env').read_text())
    settings['SERVERS_JSON'] = json.dumps([{'id': 'server', 'token': 'synthetic-old'}])
    (config / 'runtime.env').write_text(sync._encode(settings))
    with (bot / '.env').open('a') as stream:
        stream.write('SERVERS_JSON=' + json.dumps([{'id': 'server', 'token': 'synthetic-new'}]) + '\n')
    with pytest.raises(ValueError, match='secret rotation'):
        sync.sync()
    assert not commands


def test_real_readiness_validation_rejects_stale_configuration_metadata(tmp_path, monkeypatch):
    import ajib_operator as operator
    bot = tmp_path / 'core/scripts/telegrambot'
    bot.mkdir(parents=True)
    env = bot / '.env'
    config = {'telegram': {'token': '123456:synthetic', 'admin_ids': [1]},
              'servers': [{'id': 'test', 'url': 'https://example.com', 'token': 'synthetic'}]}
    fingerprint = operator.save_config(config, path=env)
    ready = tmp_path / 'ready.json'
    monkeypatch.setenv('AJIB_READY_FILE', str(ready))
    monkeypatch.setattr(sync, '_active', lambda unit: True)
    ready.write_text(json.dumps({'pid': os.getpid(), 'config_fingerprint': 'stale'}))
    with pytest.raises(ValueError, match='saved configuration'):
        sync._bot_ready({'checkout': str(tmp_path)})
    ready.write_text(json.dumps({'pid': os.getpid(), 'config_fingerprint': fingerprint}))
    sync._bot_ready({'checkout': str(tmp_path)})


@pytest.mark.skipif(os.name != 'posix', reason='Linux maintenance lock')
def test_maintenance_lock_excludes_another_command(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'CONFIG', tmp_path)
    with web.maintenance():
        with pytest.raises(ValueError, match='Another'):
            with web.maintenance():
                pytest.fail('second command entered')
