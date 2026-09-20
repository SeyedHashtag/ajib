import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import web_bootstrap as bootstrap
import web_operator as web
import web_upgrade as upgrade


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    config, bot, source, venv = [tmp_path / p for p in ('config', 'bot', 'source', 'venv')]
    for p in (config, bot, source, venv):
        p.mkdir()
    (config / 'edge').mkdir()
    (config / 'edge/nginx.conf').write_text('original route')
    data = bot / 'core/scripts/telegrambot'
    data.mkdir(parents=True)
    (data / '.env').write_text('API_TOKEN=synthetic\n')
    (data / 'uploads').mkdir()
    (data / 'uploads/receipt.jpg').write_bytes(b'private receipt')
    (data / 'card_checkout_reminders.json').write_text('{"pending":1}')
    (source / 'core').mkdir()
    (bot / 'core/old.py').write_text('old=True')
    (source / 'core/old.py').write_text('old=True')
    live = tmp_path / 'live.db'
    with sqlite3.connect(live) as db:
        db.executescript('''CREATE TABLE schema_migrations(version INTEGER); INSERT INTO schema_migrations VALUES(6);
            CREATE TABLE web_schema(version INTEGER); INSERT INTO web_schema VALUES(1);
            CREATE TABLE payments(payload_json TEXT); INSERT INTO payments VALUES('{"user_id":42,"status":"pending_approval"}');
            CREATE TABLE web_sessions(revoked_at INTEGER); INSERT INTO web_sessions VALUES(NULL);
            CREATE TABLE web_challenges(id TEXT); INSERT INTO web_challenges VALUES('old');''')
    plan = {'checkout': str(bot), 'database': str(live), 'domain': 'example.com', 'gid': 1}
    (config / 'deployment.json').write_text(json.dumps(plan))
    (config / 'runtime.env').write_text('API_TOKEN=synthetic\nAJIB_WEB_WRITES_ENABLED=0\nAJIB_WEB_PUBLIC_PORTAL=0\n'
        'AJIB_WEB_PILOT_USERS=\nAJIB_WEB_ORIGIN=https://example.com\nAJIB_DB_PATH=' + str(live) + '\n')
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(web, 'SOURCE', source)
    monkeypatch.setattr(web, 'VENV', venv)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'maintenance', contextlib.nullcontext)
    real_write = web.write
    monkeypatch.setattr(web, 'write', lambda p, s, mode=0o644, **kw: real_write(p, s, mode))
    return tmp_path, plan


def test_review_rejects_drift_and_existing_obligations(legacy, monkeypatch):
    root, plan = legacy
    review = bootstrap.inventory(plan)
    monkeypatch.setattr(upgrade, 'resolve_target', lambda _: {'commit': 'a'*40, 'contract': {'customer_release_ready': False}})
    assert bootstrap.preview(review, 'a'*40)['commit'] == 'a'*40
    (Path(plan['checkout']) / 'core/old.py').write_text('changed=True')
    with pytest.raises(ValueError, match='changed'):
        bootstrap.preview(review, 'a'*40)
    with sqlite3.connect(plan['database']) as db:
        db.execute('CREATE TABLE web_operations(id TEXT)')
        db.execute("INSERT INTO web_operations VALUES('existing')")
    with pytest.raises(ValueError, match='obligations'):
        bootstrap.inventory(plan)


def test_legacy_inflight_payment_keeps_runtimes_stopped(legacy, monkeypatch):
    root, plan = legacy
    (root / 'release/private').mkdir(parents=True)
    with sqlite3.connect(plan['database']) as db:
        db.execute("UPDATE payments SET payload_json='{\"status\":\"processing\"}'")
    journal = {'kind': 'bootstrap', 'id': 'test', 'commit': 'a'*40, 'phase': 'bootstrap_prepared',
               'plan': plan, 'root': str(root / 'release'), 'active': list(web.UNITS), 'switches': []}
    commands = []
    monkeypatch.setattr(web, 'run', lambda *a, **kw: commands.append(a))
    monkeypatch.setattr(bootstrap, '_runtime', lambda _: pytest.fail('must not copy or switch code'))
    with pytest.raises(ValueError, match='forward recovery'):
        bootstrap.resume(journal)
    assert all(c == ('systemctl', 'stop', *web.UNITS, upgrade.BOT_UNIT) for c in commands)
    assert journal['phase'] == 'bootstrap_forward_recovery_required'
    with sqlite3.connect(plan['database']) as db:
        assert json.loads(db.execute('SELECT payload_json FROM payments').fetchone()[0])['status'] == 'processing'


@pytest.mark.skipif(os.name != 'posix', reason='Linux retained environment symlink')
def test_install_records_distinct_live_staged_and_retained_paths(legacy, monkeypatch):
    from types import SimpleNamespace
    root, plan = legacy
    review = bootstrap.inventory(plan)
    monkeypatch.setattr(bootstrap, 'preview', lambda *a: {'commit': 'a'*40, 'contract': {}})
    monkeypatch.setattr(upgrade, 'RELEASES', root / 'releases')
    monkeypatch.setattr(upgrade, '_prepare', lambda *a: None)
    monkeypatch.setattr(upgrade, '_hosted_processes', lambda: set())
    monkeypatch.setattr(upgrade, '_active', lambda _: True)
    monkeypatch.setattr(web, 'run', lambda *a, **kw: SimpleNamespace(stdout='[{"Image":"image","State":{"Running":true}}]'))
    monkeypatch.setattr(web, 'backup_configuration', lambda: 'private-backup')
    monkeypatch.setattr(bootstrap, 'resume', lambda j: j)
    journal = bootstrap.install(review, 'a'*40)
    paths = [item[key] for item in journal['switches'] for key in ('live', 'staged', 'old')]
    assert len(set(paths)) == 9


@pytest.mark.skipif(os.name != 'posix', reason='Linux directory switches')
@pytest.mark.parametrize('interruption', range(7))
def test_each_rename_interruption_recovers_forward_once(tmp_path, monkeypatch, interruption):
    entries = []
    for number in range(3):
        live, staged, old = [tmp_path / f'{kind}-{number}' for kind in ('live', 'staged', 'old')]
        live.mkdir(); staged.mkdir()
        (live / 'value').write_text('legacy')
        (staged / 'value').write_text('candidate')
        entries.append(dict(live=str(live), staged=str(staged), old=str(old)))
    original, calls = upgrade._rename, 0
    def crash(src, dst):
        nonlocal calls
        if calls == interruption:
            raise KeyboardInterrupt()
        calls += 1
        original(src, dst)
    monkeypatch.setattr(upgrade, '_rename', crash)
    try:
        bootstrap.forward_switch(entries)
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(upgrade, '_rename', original)
    bootstrap.forward_switch(entries)
    bootstrap.forward_switch(entries)
    for item in entries:
        assert (Path(item['live']) / 'value').read_text() == 'candidate'
        assert (Path(item['old']) / 'value').read_text() == 'legacy'


@pytest.mark.skipif(os.name != 'posix', reason='Linux bootstrap lifecycle')
@pytest.mark.parametrize('failure', ['runtime', 'migration', 'health', 'complete', None])
def test_bootstrap_recovery_preserves_payments_and_never_starts_legacy(legacy, monkeypatch, failure):
    root, plan = legacy
    release = root / 'release'
    (release / 'private').mkdir(parents=True)
    switches = []
    for number, live in enumerate((Path(plan['checkout']), web.SOURCE, web.VENV)):
        staged, old = root / f'staged-{number}', root / f'old-{number}'
        (staged / 'core/scripts/telegrambot').mkdir(parents=True)
        (staged / 'core/new.py').write_text('candidate=True')
        switches.append(dict(live=str(live), staged=str(staged), old=str(old)))
    journal = {'kind': 'bootstrap', 'id': 'test', 'commit': 'a'*40, 'contract': {}, 'phase': 'bootstrap_prepared',
               'plan': plan, 'root': str(release), 'switches': switches, 'active': [upgrade.BOT_UNIT, *web.UNITS]}
    active = set(journal['active'])
    def run(*args, **kwargs):
        assert args[0] == 'systemctl' and set(args[2:]) <= set(journal['active'])
        if args[1] == 'stop': active.difference_update(args[2:])
        else:
            assert (Path(plan['checkout']) / 'core/new.py').exists()
            active.update(args[2:])
    monkeypatch.setattr(web, 'run', run)
    monkeypatch.setattr(upgrade, '_edge', lambda _: None)
    original_runtime = bootstrap._runtime
    def runtime(j):
        if failure == 'runtime': raise ValueError('copy failed')
        original_runtime(j)
    monkeypatch.setattr(bootstrap, '_runtime', runtime)
    def migrate(*args):
        assert not active
        if failure == 'migration': raise ValueError('migration failed')
        with sqlite3.connect(plan['database']) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS web_release_control(id INTEGER PRIMARY KEY,access TEXT,
                pilot_users_json TEXT,accept_writes INTEGER,process_existing INTEGER,revision TEXT,
                pilot_started_at INTEGER,updated_at INTEGER)''')
    monkeypatch.setattr(upgrade, '_migrate', migrate)
    def health(*args):
        with sqlite3.connect(plan['database']) as db:
            db.execute("INSERT INTO payments VALUES('{\"status\":\"completed\",\"id\":\"after-backup\"}')")
        if failure == 'health': raise ValueError('health failed')
    monkeypatch.setattr(upgrade, '_healthy', health)
    original_persist = upgrade._persist
    def persist(j, phase):
        if failure == 'complete' and phase == 'complete': raise ValueError('interrupted completion')
        original_persist(j, phase)
    monkeypatch.setattr(upgrade, '_persist', persist)
    if failure:
        with pytest.raises(ValueError, match='forward recovery'):
            bootstrap.resume(journal)
        assert not active
        assert (web.CONFIG / 'upgrade.json').exists()
        failure = None
        journal = json.loads((web.CONFIG / 'upgrade.json').read_text())
    assert bootstrap.resume(journal)['status'] == 'complete'
    assert active == set(journal['active'])
    assert upgrade._policy(plan)['accept_writes'] == 0
    assert upgrade._policy(plan)['access'] == 'admin'
    assert (Path(plan['checkout']) / 'core/scripts/telegrambot/uploads/receipt.jpg').read_bytes() == b'private receipt'
    with sqlite3.connect(plan['database']) as db:
        assert db.execute('SELECT COUNT(*) FROM payments').fetchone()[0] >= 2
        assert db.execute('SELECT revoked_at FROM web_sessions').fetchone()[0]
        assert not db.execute('SELECT * FROM web_challenges').fetchall()
    assert not (web.CONFIG / 'upgrade.json').exists()
