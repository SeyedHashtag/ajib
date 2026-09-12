import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import web_operator as web
import web_upgrade as upgrade

CONTRACT = {'format': 1, 'bot_schema': 6, 'web_schema': 1,
            'fulfillment_contract': 1, 'unit_contract': 1, 'live_release_policy': True,
            'customer_release_ready': False}


def code(root, version):
    (root / 'core/web').mkdir(parents=True)
    (root / 'core/web/release-contract.json').write_text(json.dumps(CONTRACT))
    (root / 'core/main.py').write_text('VERSION = ' + repr(version))
    (root / 'VERSION').write_text(version)
    return root


@pytest.fixture
def installation(tmp_path, monkeypatch):
    config = tmp_path / 'config'
    config.mkdir()
    bot = code(tmp_path / 'bot', 'old')
    source = code(tmp_path / 'source', 'old')
    venv = tmp_path / 'venv'
    venv.mkdir()
    (venv / 'identity').write_text('old')
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(web, 'SOURCE', source)
    monkeypatch.setattr(web, 'VENV', venv)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'maintenance', contextlib.nullcontext)
    original_write = web.write
    monkeypatch.setattr(web, 'write', lambda path, text, mode=0o644, **kw: original_write(path, text, mode))
    database = tmp_path / 'live.db'
    with sqlite3.connect(database) as db:
        db.executescript('''CREATE TABLE schema_migrations(version INTEGER); INSERT INTO schema_migrations VALUES(6);
            CREATE TABLE web_schema(version INTEGER); INSERT INTO web_schema VALUES(1);
            CREATE TABLE payments(id INTEGER); INSERT INTO payments VALUES(1);
            CREATE TABLE web_release_control(id INTEGER PRIMARY KEY,access TEXT,pilot_users_json TEXT,
                accept_writes INTEGER,process_existing INTEGER,revision TEXT,pilot_started_at INTEGER,updated_at INTEGER);
            INSERT INTO web_release_control VALUES(1,'pilot','["42"]',1,1,'old',100,100);''')
    plan = {'checkout': str(bot), 'database': str(database), 'domain': 'example.com', 'gid': 1000}
    (config / 'deployment.json').write_text(json.dumps(plan))
    (config / 'runtime.env').write_text('API_TOKEN=synthetic\nAJIB_WEB_WRITES_ENABLED=0\n')
    upgrade.adopt_baseline()
    return tmp_path, plan


def test_code_hashes_ignore_state_but_detect_new_code(installation):
    root, plan = installation
    original = upgrade.hashes(plan['checkout'])
    bot = Path(plan['checkout'])
    (bot / 'core/.env').write_text('secret')
    (bot / 'core/tokens.json').write_text('{"private":true}')
    assert upgrade.hashes(bot) == original
    (bot / 'core/new.py').write_text('changed = True')
    with pytest.raises(ValueError, match='differs'):
        upgrade._check_baseline(plan)


def test_preflight_rejects_incompatible_release_before_service_calls(installation, monkeypatch):
    root, plan = installation
    monkeypatch.setattr(upgrade, 'resolve_target', lambda *args: {'commit': 'a'*40, 'contract': {**CONTRACT, 'bot_schema': 7}})
    monkeypatch.setattr(web, 'run', lambda *args, **kw: pytest.fail('must not change services'))
    with pytest.raises(ValueError, match='contract'):
        upgrade.preview()


@pytest.mark.skipif(os.name != 'posix', reason='Linux durable directory switches')
@pytest.mark.parametrize('interruption', range(7))
def test_every_partial_directory_switch_rolls_back_without_database_loss(installation, monkeypatch, interruption):
    root, plan = installation
    staged = [code(root / 'new-bot', 'new'), code(root / 'new-web', 'new'), root / 'new-venv']
    staged[2].mkdir()
    (staged[2] / 'identity').write_text('new')
    switches = [{'live': str(live), 'staged': str(new), 'old': str(root / ('old-' + str(index)))}
                for index, (live, new) in enumerate(zip([Path(plan['checkout']), web.SOURCE, web.VENV], staged))]
    real_rename = upgrade._rename
    calls = 0
    def fail_after(source, target):
        nonlocal calls
        if calls == interruption:
            raise KeyboardInterrupt()
        calls += 1
        real_rename(source, target)
    monkeypatch.setattr(upgrade, '_rename', fail_after)
    try:
        upgrade._switch(switches)
    except KeyboardInterrupt:
        pass
    # Simulates state committed after an earlier backup; rollback cannot erase it.
    with sqlite3.connect(plan['database']) as db:
        db.execute('INSERT INTO payments VALUES(2)')
    monkeypatch.setattr(upgrade, '_rename', real_rename)
    upgrade._switch(switches, reverse=True)
    upgrade._switch(switches, reverse=True)  # Recovery is safe to repeat.
    assert (Path(plan['checkout']) / 'VERSION').read_text() == 'old'
    assert (web.SOURCE / 'VERSION').read_text() == 'old'
    assert (web.VENV / 'identity').read_text() == 'old'
    with sqlite3.connect(plan['database']) as db:
        assert db.execute('SELECT id FROM payments').fetchall() == [(1,), (2,)]


def test_recovery_failure_keeps_admission_and_worker_paused(installation, monkeypatch):
    root, plan = installation
    journal = {'id': 'test', 'phase': 'starting', 'commit': 'a'*40, 'started_at': 1,
               'plan': plan, 'policy': upgrade._policy(plan), 'root': str(root), 'switches': [],
               'active': list(web.UNITS), 'edge_running': True}
    (root / 'private').mkdir()
    original_manifest = (web.CONFIG / 'managed-release.json').read_text()
    (root / 'private/managed-release.json').write_text(original_manifest)
    (web.CONFIG / 'managed-release.json').write_text('{"wrong":"new"}')
    commands = []
    monkeypatch.setattr(web, 'run', lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(upgrade, '_edge', lambda *args: None)
    def fail_health(*args):
        raise ValueError('unhealthy')
    monkeypatch.setattr(upgrade, '_healthy', fail_health)
    with pytest.raises(ValueError, match='unhealthy'):
        upgrade._restore(journal)
    current = upgrade._policy(plan)
    assert current['accept_writes'] == current['process_existing'] == 0
    assert (web.CONFIG / 'upgrade.json').exists()
    assert (web.CONFIG / 'managed-release.json').read_text() == original_manifest
    assert all(set(command[2:]) <= {*web.UNITS, upgrade.BOT_UNIT} for command in commands)
    assert commands[-1] == ('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT)
    assert journal['phase'] == 'recovery_required'


def test_saved_policy_can_pause_admission_without_stopping_fulfillment(installation):
    root, plan = installation
    old = upgrade._policy(plan)
    upgrade._save_policy(plan, {**old, 'accept_writes': 0})
    assert upgrade._policy(plan)['process_existing'] == 1
    assert upgrade._policy(plan)['accept_writes'] == 0


@pytest.mark.skipif(os.name != 'posix', reason='Linux staged releases and symlinks')
@pytest.mark.parametrize('failure', [None, 'build', 'migrate', 'health', 'complete'])
def test_coordinated_upgrade_lifecycle_preserves_live_state(installation, monkeypatch, failure):
    from types import SimpleNamespace
    root, plan = installation
    bot_data = Path(plan['checkout']) / 'core/scripts/telegrambot'
    bot_data.mkdir(parents=True)
    (bot_data / '.env').write_text('API_TOKEN=synthetic\nADMIN_USER_IDS=[1]\n')
    (bot_data / 'plans.json').write_text('{"100":{"price":2}}')
    with sqlite3.connect(plan['database']) as db:
        db.execute('CREATE TABLE hosted_bots(reseller_id TEXT,status TEXT)')
    monkeypatch.setattr(upgrade, 'RELEASES', root / 'releases')
    monkeypatch.setattr(upgrade, 'resolve_target', lambda *args: {'commit': 'a'*40, 'contract': {**CONTRACT, 'customer_release_ready': True}})
    active = {*web.UNITS, upgrade.BOT_UNIT}
    events = []
    def run(*args, **kwargs):
        events.append(args)
        if args[:2] == ('docker', 'inspect'):
            return SimpleNamespace(stdout=json.dumps([{'Image': 'immutable-image', 'State': {'Running': True}}]))
        assert args[0] == 'systemctl'
        assert set(args[2:]) <= {*web.UNITS, upgrade.BOT_UNIT}
        if args[1] == 'stop':
            active.difference_update(args[2:])
        elif args[1] == 'start':
            active.update(args[2:])
    monkeypatch.setattr(web, 'run', run)
    monkeypatch.setattr(upgrade, '_active', lambda unit: unit in active)
    monkeypatch.setattr(web, 'backup_configuration', lambda: None)
    def prepare(plan, target, release_root, candidate_bot, candidate_web):
        assert events == []
        if failure == 'build':
            raise ValueError('build failed')
        code(candidate_bot, 'new')
        code(candidate_web, 'new')
        (candidate_bot / 'core/scripts/telegrambot').mkdir(parents=True)
        (candidate_web / 'core/scripts/telegrambot').mkdir(parents=True)
        (release_root / 'web-venv').mkdir()
        (release_root / 'bot-venv').mkdir()
        (candidate_bot / 'ajib_venv').symlink_to(release_root / 'bot-venv')
    monkeypatch.setattr(upgrade, '_prepare', prepare)
    monkeypatch.setattr(upgrade, '_drain', lambda *args: None)
    def migrate(*args):
        assert not active
        if failure == 'migrate':
            raise ValueError('migration failed')
    monkeypatch.setattr(upgrade, '_migrate', migrate)
    monkeypatch.setattr(upgrade, '_edge', lambda journal: None)
    def healthy(journal, started):
        if journal['phase'] == 'starting':
            with sqlite3.connect(plan['database']) as db:
                db.execute('INSERT INTO payments VALUES (99)')
            if failure == 'health':
                raise ValueError('health failed')
    monkeypatch.setattr(upgrade, '_healthy', healthy)
    original_persist = upgrade._persist
    def persist(journal, phase):
        if phase == failure:
            raise ValueError('journal failure after baseline update')
        original_persist(journal, phase)
    monkeypatch.setattr(upgrade, '_persist', persist)
    if failure:
        with pytest.raises(ValueError):
            upgrade.upgrade()
    else:
        assert upgrade.upgrade()['status'] == 'complete'
    assert active == {*web.UNITS, upgrade.BOT_UNIT}
    expected = 'new' if failure is None else 'old'
    assert (Path(plan['checkout']) / 'VERSION').read_text() == expected
    assert (web.SOURCE / 'VERSION').read_text() == expected
    with sqlite3.connect(plan['database']) as db:
        values = db.execute('SELECT id FROM payments').fetchall()
        assert (1,) in values
        if failure in {None, 'health', 'complete'}:
            assert (99,) in values
    assert not (web.CONFIG / 'upgrade.json').exists()
    upgrade._check_baseline(plan)
    if failure == 'build':
        assert events == []
