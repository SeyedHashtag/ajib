"""Explicit operator recovery commands; imports never register Telegram handlers."""
import json
import os
from pathlib import Path
import sys

import click


def _services(*, needs_panel=True):
    import web_operator
    web_operator.root_required()
    source = Path(__file__).resolve().parent / 'scripts/telegrambot'
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    os.environ['AJIB_BOT_ROLE'] = 'api'
    from dotenv import load_dotenv
    path = web_operator.CONFIG / 'runtime.env' if (web_operator.CONFIG / 'deployment.json').exists() else source / '.env'
    load_dotenv(path, override=False)
    if os.getenv('AJIB_SQLITE_ACTIVE') != '1':
        raise ValueError('Operation recovery requires migrated SQLite storage')
    from utils import operation_recovery
    if not needs_panel:
        return operation_recovery, None
    from utils.api_client import MultiServerAPI
    return operation_recovery, MultiServerAPI()


@click.group('operations')
def operations_group():
    """Inspect durable account operations and reconcile proven outcomes."""


@operations_group.command('list')
@click.option('--status')
@click.option('--scope')
@click.option('--origin-id')
def list_command(status, scope, origin_id):
    try:
        recovery, _ = _services(needs_panel=False)
        click.echo(json.dumps(recovery.list_operations(status=status, scope=scope, origin_id=origin_id), indent=2))
    except Exception as error:
        raise click.ClickException('Operation listing failed: ' + type(error).__name__) from error


@operations_group.command('inspect')
@click.argument('operation_id')
@click.option('--json', 'as_json', is_flag=True, help='Print structured evidence (also the default).')
def inspect_command(operation_id, as_json):
    try:
        recovery, panels = _services()
        click.echo(json.dumps(recovery.inspect(operation_id, panels), indent=2))
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    except Exception as error:
        raise click.ClickException('Inspection failed: ' + type(error).__name__) from error


@operations_group.command('reconcile')
@click.argument('operation_id')
@click.option('--dry-run', is_flag=True)
@click.option('--evidence', help='Digest returned by a fresh inspection.')
@click.option('--reason', default='operator_reconciliation', help='Reason recorded in the audit trail.')
@click.option('--yes', is_flag=True)
def reconcile_command(operation_id, dry_run, evidence, reason, yes):
    try:
        if not dry_run and (not yes or not evidence):
            raise ValueError('Inspect first, then reconcile with --evidence <digest> --yes.')
        if not 3 <= len(reason) <= 500:
            raise ValueError('Reason must contain 3–500 characters.')
        recovery, panels = _services()
        if dry_run:
            report = recovery.inspect(operation_id, panels)
        else:
            import web_operator
            with web_operator.maintenance():
                if any((web_operator.CONFIG / name).exists() for name in ('upgrade.json', 'config-sync.json', 'settings.json')):
                    raise ValueError('Finish coordinated maintenance before reconciling operations.')
                report = recovery.reconcile(operation_id, panels, evidence, reason=reason)
        click.echo(json.dumps(report, indent=2))
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    except Exception as error:
        raise click.ClickException('Reconciliation failed; reservations retained: ' + type(error).__name__) from error
