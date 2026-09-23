from contextlib import nullcontext
from pathlib import Path

import pytest

from test_renewal_backup_recovery import evidence


@pytest.fixture
def maintenance(evidence, storage, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'core'))
    from core import renewal_backup_maintenance as runtime
    import web_operator as web
    import web_upgrade as upgrade
    from utils import renewal_backup_recovery
    config = tmp_path / 'operator-config'
    config.mkdir()
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setattr(runtime, 'ROOT', tmp_path / 'private-releases')
    monkeypatch.setattr(web, 'maintenance', nullcontext)
    monkeypatch.setattr(web, 'root_required', lambda: None)
    monkeypatch.setattr(web, 'load', lambda: {'database': str(storage / 'ajib.db')})
    monkeypatch.setattr(upgrade, '_no_pending', lambda: None)
    monkeypatch.setattr(upgrade, '_check_baseline', lambda plan: {'commit': 'synthetic-commit',
                                                                  'contract': {'customer_release_ready': False}})
    monkeypatch.setattr(upgrade, '_policy', lambda plan: {'access': 'admin', 'accept_writes': 0,
                                                          'process_existing': 0, 'pilot_users_json': '[]'})
    active = {unit: True for unit in runtime.UNITS}
    monkeypatch.setattr(upgrade, '_active', lambda unit: active[unit])
    monkeypatch.setattr(upgrade, '_hosted_processes', lambda: {'synthetic-hosted'})
    import web_release_cli
    monkeypatch.setattr(web_release_cli, 'diagnostics', lambda plan: {'worker_healthy': True})
    def systemctl(*args, **kwargs):
        assert args[0] == 'systemctl'
        for unit in args[2:]: active[unit] = args[1] == 'start'
    monkeypatch.setattr(web, 'run', systemctl)
    report = renewal_backup_recovery.inspect(evidence.ident, evidence.panels, *evidence.files)
    return runtime, report, active


def test_journaled_repair_restores_prior_services_and_keeps_backup(maintenance, evidence):
    from utils import account_operations
    runtime, report, active = maintenance
    result = runtime.apply(evidence.ident, evidence.panels, *evidence.files, report['evidence_digest'])
    assert result['outcome'] == 'committed'
    assert all(active.values())
    assert account_operations.details(evidence.ident)['phase'] == 'completed'
    assert not (runtime.web.CONFIG / runtime.JOURNAL).exists()
    assert list(runtime.ROOT.glob('*/state-before.db'))
    assert runtime.recover()['status'] == 'idle'


def test_interrupted_before_commit_restores_services_and_retains_reservation(maintenance, evidence, monkeypatch):
    from utils import account_operations, renewal_backup_recovery
    runtime, report, active = maintenance
    def fail(*args): raise RuntimeError('synthetic process failure')
    monkeypatch.setattr(renewal_backup_recovery, 'reconcile', fail)
    with pytest.raises(RuntimeError):
        runtime.apply(evidence.ident, evidence.panels, *evidence.files, report['evidence_digest'])
    assert all(active.values())
    assert account_operations.details(evidence.ident)['phase'] == 'uncertain'
    assert not (runtime.web.CONFIG / runtime.JOURNAL).exists()


def test_interrupted_after_commit_detects_completion_without_repeating(maintenance, evidence, monkeypatch):
    from utils import account_operations, database
    runtime, report, active = maintenance
    original = runtime._phase
    def fail_after_commit(value, phase):
        original(value, phase)
        if phase == 'accounting_committed': raise RuntimeError('synthetic process failure')
    monkeypatch.setattr(runtime, '_phase', fail_after_commit)
    with pytest.raises(RuntimeError):
        runtime.apply(evidence.ident, evidence.panels, *evidence.files, report['evidence_digest'])
    assert all(active.values())
    assert account_operations.details(evidence.ident)['phase'] == 'completed'
    assert database.get_connection().execute("SELECT COUNT(*) FROM web_outbox WHERE id LIKE 'renewal-applied:%'").fetchone()[0] == 1
    assert not (runtime.web.CONFIG / runtime.JOURNAL).exists()
