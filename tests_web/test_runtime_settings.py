"""Synthetic settings restoration and interruption tests; no external services."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import runtime_settings as settings


@pytest.fixture
def installed(tmp_path, monkeypatch):
    web, upgrade = settings.web, settings.upgrade
    config, checkout, source = (tmp_path / name for name in ('config', 'checkout', 'source'))
    config.mkdir()
    bot = checkout / 'core/scripts/telegrambot'
    bot.mkdir(parents=True)
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(web, 'SOURCE', source)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'maintenance', contextlib.nullcontext)
    actual_write = web.write
    monkeypatch.setattr(web, 'write', lambda p, t, mode=0o644, **kw: actual_write(p, t, mode))
    plan = {'checkout': str(checkout), 'database': str(tmp_path / 'live.db'), 'domain': 'example.test', 'gid': 1}
    (config / 'deployment.json').write_text(json.dumps(plan))
    normalized = settings.operator.normalize_config({'schema_version': 1,
        'telegram': {'token': '123:synthetic', 'admin_ids': [1]},
        'servers': [{'id': 's1', 'name': 'Synthetic', 'url': 'https://panel.example.test',
                     'token': 'synthetic-panel', 'panel': '3x-ui', 'default_inbound_ids': [1]}]})
    env = ''.join(k + '=' + str(v) + '\n' for k, v in settings.operator._env_updates(normalized).items())
    env += "# keep this comment\nCRYPTO_API_KEY='synthetic-payment-key'\nCUSTOM_OPTION=keep\n"
    (bot / '.env').write_text(env)
    values = settings._config()._environment(env)
    (config / 'runtime.env').write_text(settings._config()._encode({**values, 'AJIB_WEB_WRITES_ENABLED': '0'}))
    with sqlite3.connect(plan['database']) as db:
        db.execute('CREATE TABLE payments (id TEXT)')
        db.execute("INSERT INTO payments VALUES ('synthetic-payment')")
    policy = {'access': 'admin', 'accept_writes': 0, 'process_existing': 0}
    active = {'ajib-telegram-bot', *web.UNITS}
    calls = []
    def run(*args, **kwargs):
        assert args[:2] in [('systemctl', 'stop'), ('systemctl', 'start')]
        assert set(args[2:]) <= {'ajib-telegram-bot', *web.UNITS}
        calls.append(args)
        (active.difference_update if args[1] == 'stop' else active.update)(args[2:])
    monkeypatch.setattr(web, 'run', run)
    monkeypatch.setattr(upgrade, '_active', lambda u: u in active)
    monkeypatch.setattr(upgrade, '_hosted_processes', lambda: set())
    monkeypatch.setattr(upgrade, '_check_baseline', lambda p: {})
    monkeypatch.setattr(upgrade, '_policy', lambda p: dict(policy))
    monkeypatch.setattr(upgrade, '_save_policy', lambda p, value: policy.update(value))
    monkeypatch.setattr(upgrade, '_drain', lambda p: None)
    monkeypatch.setattr(upgrade, '_healthy', lambda *a: None)
    monkeypatch.setattr(settings.operator, 'active_transfer_for_server', lambda s: None)
    panel = SimpleNamespace(get_inbound_options=lambda: [{'id': 1, 'enable': True}, {'id': 2, 'enable': True}])
    monkeypatch.setattr(settings, '_panel', lambda s: panel)
    return SimpleNamespace(config=config, bot=bot, plan=plan, active=active, policy=policy, calls=calls, panel=panel)


def inbound_request():
    current = settings.inbound_options('s1')
    return settings.prepare({'kind': 'inbounds', 'server_id': 's1', 'inbound_ids': [1, 2], 'expected': current['expected']})


def test_preview_is_read_only_and_application_preserves_credentials_and_gates(installed):
    before = (installed.bot / '.env').read_text()
    request = inbound_request()
    assert not installed.calls and (installed.bot / '.env').read_text() == before
    assert settings.apply(request)['status'] == 'applied'
    values = settings._config()._environment((installed.bot / '.env').read_text())
    assert values['CRYPTO_API_KEY'] == 'synthetic-payment-key'
    assert values['CUSTOM_OPTION'] == 'keep'
    assert '# keep this comment' in (installed.bot / '.env').read_text()
    normalized = settings.operator.load_config(installed.bot / '.env')
    assert normalized['servers'][0]['default_inbound_ids'] == [1, 2]
    web_values = settings._config()._environment((installed.config / 'runtime.env').read_text())
    assert json.loads(web_values['SERVERS_JSON'])[0]['default_inbound_ids'] == [1, 2]
    assert not installed.policy['accept_writes']
    assert not (installed.config / 'settings.json').exists()


def test_restores_only_matching_merchant_without_rotating_api_key(installed):
    source = installed.config / 'old.env'
    source.write_text('CRYPTO_API_KEY=synthetic-payment-key\nCRYPTO_MERCHANT_ID=00000000-0000-0000-0000-000000000001\n')
    source.chmod(0o600)
    request = settings.prepare({'kind': 'crypto', 'source': str(source)})
    assert 'synthetic-payment-key' not in json.dumps(request)
    settings.apply(request)
    for path in (installed.bot / '.env', installed.config / 'runtime.env'):
        values = settings._config()._environment(path.read_text())
        assert values['CRYPTO_MERCHANT_ID'] == '00000000-0000-0000-0000-000000000001'
        assert values['CRYPTO_API_KEY'] == 'synthetic-payment-key'


@pytest.mark.parametrize('phase', ['prepared', 'stopping', 'installing', 'starting', 'complete'])
def test_interrupted_phase_recovers_without_restoring_database(installed, monkeypatch, phase):
    request = inbound_request()
    persist = settings._persist
    interrupted = False
    def crash(journal, actual):
        nonlocal interrupted
        persist(journal, actual)
        if actual == phase and not interrupted:
            interrupted = True
            raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(settings, '_persist', crash)
    with pytest.raises((ValueError, RuntimeError)):
        settings.apply(request)
    with sqlite3.connect(installed.plan['database']) as db:
        db.execute("INSERT INTO payments VALUES ('newer-payment')")
    assert settings.apply(recover=True)['status'] == 'applied'
    with sqlite3.connect(installed.plan['database']) as db:
        assert db.execute('SELECT COUNT(*) FROM payments').fetchone()[0] == 2
    assert installed.active == {'ajib-telegram-bot', *settings.web.UNITS}


def test_stale_configuration_and_revoked_admin_are_rejected_before_stop(installed):
    request = inbound_request()
    with (installed.bot / '.env').open('a') as stream:
        stream.write('OTHER=new\n')
    with pytest.raises(ValueError, match='changed'):
        settings.apply(request)
    with pytest.raises(ValueError, match='revoked'):
        current = settings.inbound_options('s1')
        settings.prepare({'kind': 'inbounds', 'server_id': 's1', 'inbound_ids': [1, 2], 'expected': current['expected']}, actor=2)
    assert not installed.calls


def test_invalid_inbound_active_transfer_and_mismatched_secret_are_rejected(installed, monkeypatch):
    installed.panel.get_inbound_options = lambda: [{'id': 1}]
    with pytest.raises(ValueError, match='unavailable'):
        inbound_request()
    monkeypatch.setattr(settings.operator, 'active_transfer_for_server', lambda s: {'job_id': 'test'})
    with pytest.raises(ValueError, match='transfer'):
        inbound_request()
    source = installed.config / 'wrong.env'
    source.write_text('CRYPTO_API_KEY=another-key\nCRYPTO_MERCHANT_ID=00000000-0000-0000-0000-000000000001\n')
    source.chmod(0o600)
    with pytest.raises(ValueError, match='unique historical'):
        settings.prepare({'kind': 'crypto', 'source': str(source)})
    assert not installed.calls


def test_notification_failure_does_not_pause_or_reapply_settings(installed, monkeypatch):
    request = inbound_request()
    monkeypatch.setattr(settings, '_notify', lambda j: (_ for _ in ()).throw(RuntimeError('offline')))
    result = settings.apply(request)
    assert result['status'] == 'applied' and result['notification'] == 'pending'
    assert not (installed.config / 'settings.json').exists()
    assert installed.active == {'ajib-telegram-bot', *settings.web.UNITS}


def test_health_failure_keeps_financial_processing_paused_until_recovery(installed, monkeypatch):
    installed.policy.update(accept_writes=1, process_existing=1)
    request = inbound_request()
    monkeypatch.setattr(settings.upgrade, '_healthy', lambda *a: (_ for _ in ()).throw(RuntimeError('unhealthy')))
    with pytest.raises(ValueError, match='recovery'):
        settings.apply(request)
    assert not installed.active
    assert not installed.policy['accept_writes'] and not installed.policy['process_existing']
    monkeypatch.setattr(settings.upgrade, '_healthy', lambda *a: None)
    settings.apply(recover=True)
    assert installed.policy['accept_writes'] == installed.policy['process_existing'] == 1


def test_partially_written_environment_recovers_and_preserves_stopped_services(installed, monkeypatch):
    installed.active.remove('ajib-web-api')
    request = inbound_request()
    write = settings.web.write
    failed = False
    def interrupted(path, *args, **kwargs):
        nonlocal failed
        if Path(path) == installed.config / 'runtime.env' and not failed:
            failed = True
            raise RuntimeError('partial installation')
        return write(path, *args, **kwargs)
    monkeypatch.setattr(settings.web, 'write', interrupted)
    with pytest.raises(ValueError):
        settings.apply(request)
    settings.apply(recover=True)
    assert 'ajib-web-api' not in installed.active
    assert settings.operator.load_config(installed.bot / '.env')['servers'][0]['default_inbound_ids'] == [1, 2]


def test_ordinary_sync_cannot_remove_or_replace_crypto_credentials():
    current = {'CRYPTO_MERCHANT_ID': 'synthetic-merchant', 'CRYPTO_API_KEY': 'synthetic-key'}
    for desired in ({'CRYPTO_API_KEY': 'synthetic-key'}, {**current, 'CRYPTO_API_KEY': 'replacement'}):
        with pytest.raises(ValueError, match='Credential changes'):
            settings._config().reject_secret_rotation(current, desired)
