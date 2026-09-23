import json
from contextlib import nullcontext
from types import SimpleNamespace
from click.testing import CliRunner
import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    import sys
    from core import operations_cli
    calls = []
    recovery = SimpleNamespace(
        list_operations=lambda **kw: calls.append(('list', kw)) or [],
        inspect=lambda *args: calls.append(('inspect', args)) or {'evidence_digest': 'digest'},
        reconcile=lambda *args, **kw: calls.append(('apply', args, kw)) or {'applied': True})
    monkeypatch.setattr(operations_cli, '_services', lambda **kw: (recovery, None))
    monkeypatch.setitem(sys.modules, 'web_operator', SimpleNamespace(CONFIG=tmp_path, maintenance=nullcontext))
    return CliRunner(), operations_cli.operations_group, calls, tmp_path


@pytest.mark.parametrize('arguments', [['reconcile', 'id'], ['reconcile', 'id', '--yes'],
                                    ['reconcile', 'id', '--evidence', 'digest']])
def test_apply_requires_evidence_and_explicit_confirmation(cli, arguments):
    runner, group, calls, _ = cli
    result = runner.invoke(group, arguments)
    assert result.exit_code == 1
    assert '--evidence' in result.output
    assert not calls


def test_dry_run_never_applies(cli):
    runner, group, calls, _ = cli
    result = runner.invoke(group, ['reconcile', 'id', '--dry-run', '--yes', '--evidence', 'digest'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['evidence_digest'] == 'digest'
    assert [call[0] for call in calls] == ['inspect']


@pytest.mark.parametrize('journal', ['upgrade.json', 'config-sync.json'])
def test_apply_is_blocked_during_coordinated_maintenance(cli, journal):
    runner, group, calls, path = cli
    (path / journal).write_text('{}')
    result = runner.invoke(group, ['reconcile', 'id', '--evidence', 'digest', '--yes'])
    assert result.exit_code == 1 and 'maintenance' in result.output
    assert not calls


def test_apply_passes_evidence_and_reason_to_recovery(cli):
    runner, group, calls, _ = cli
    result = runner.invoke(group, ['reconcile', 'id', '--evidence', 'digest', '--yes', '--reason', 'Verified panel identity'])
    assert result.exit_code == 0, result.output
    assert calls == [('apply', ('id', None, 'digest'), {'reason': 'Verified panel identity'})]


def test_cli_redacts_unexpected_error(cli, monkeypatch):
    from core import operations_cli
    runner, group, _, _ = cli
    def fail(**kw):
        raise RuntimeError('synthetic-sensitive-configuration')
    monkeypatch.setattr(operations_cli, '_services', fail)
    result = runner.invoke(group, ['inspect', 'id', '--json'])
    assert result.exit_code == 1
    assert 'RuntimeError' in result.output and 'synthetic-sensitive' not in result.output


def test_backup_reconciliation_requires_evidence_and_yes(cli, monkeypatch, tmp_path):
    from core import operations_cli
    import sys
    runner, group, calls, _ = cli
    paths = []
    for name in ('before.db', 'after.db', 'prior.db'):
        path = tmp_path / name
        path.write_bytes(b'synthetic')
        paths.extend(['--panel-before' if name == 'before.db' else '--panel-after' if name == 'after.db' else '--payment-before', str(path)])
    inspected = []
    recovery = SimpleNamespace(inspect=lambda *args: inspected.append(args) or {'evidence_digest': 'digest'})
    monkeypatch.setattr(operations_cli, '_backup_recovery', lambda: (recovery, None))
    applied = []
    monkeypatch.setitem(sys.modules, 'renewal_backup_maintenance',
                        SimpleNamespace(apply=lambda *args: applied.append(args) or {'applied': True}))
    result = runner.invoke(group, ['inspect-renewal-backup', 'id', *paths])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['evidence_digest'] == 'digest'
    result = runner.invoke(group, ['reconcile-renewal-backup', 'id', *paths, '--evidence', 'digest'])
    assert result.exit_code != 0 and not applied
    result = runner.invoke(group, ['reconcile-renewal-backup', 'id', *paths, '--evidence', 'digest', '--yes'])
    assert result.exit_code == 0, result.output
    assert len(applied) == 1
