"""Click commands for the optional website; bot-only installations stay optional."""
import json
import os
from pathlib import Path
import subprocess

import click

import web_operator as web


@click.group('web')
def web_group():
    """Set up and operate the website and Telegram Mini App."""


@web_group.command('setup')
@click.option('--domain', help='Public hostname, e.g. utility.example.com.')
@click.option('--proxy', type=click.Choice(['auto', 'traefik', 'standalone']), default='auto')
@click.option('--email', help="Optional Let's Encrypt expiry contact for standalone HTTPS.")
@click.option('--dry-run', is_flag=True, help='Inspect and print the deployment without changing services.')
@click.option('--yes', is_flag=True, help='Apply the displayed setup, including an ajib bot restart.')
def setup(domain, proxy, email, dry_run, yes):
    """Install public pages and an administrator-only read-only portal."""
    if not domain:
        domain = click.prompt('Website domain')
    try:
        plan = web.deployment_plan(domain, os.getenv('AJIB_INSTALL_DIR', '/etc/ajib'), proxy)
        click.echo(json.dumps(plan, indent=2))
        if dry_run:
            return
        if not yes:
            if not click.get_text_stream('stdin').isatty():
                raise click.ClickException('Non-interactive web setup requires --yes; preview with --dry-run.')
            click.confirm('Install this website and coordinate the ajib bot restart?', abort=True)
        click.echo(json.dumps(web.setup(plan, email=email), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


@web_group.command('status')
def status():
    """Show website URL, proxy mode, database location, and service state."""
    try:
        click.echo(json.dumps(web.status(), indent=2))
    except FileNotFoundError:
        click.echo('Website is not configured. Run ajib web setup.')


@web_group.command('doctor')
def doctor():
    """Check private API, public HTTPS, and proxy configuration."""
    try:
        plan = web.load()
        click.echo(json.dumps(web.status(), indent=2))
        web.run('curl', '--fail', '--silent', '--show-error', '--max-time', '15',
                '--unix-socket', '/run/ajib-web/api.sock', 'http://localhost/api/v1/health')
        web.run('curl', '--fail', '--silent', '--show-error', '--max-time', '20',
                'https://' + plan['domain'] + '/api/v1/health')
        web.run('docker', 'exec', 'ajib-web-edge', 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-t')
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


@web_group.command('logs')
@click.option('--lines', default=100, type=click.IntRange(1, 1000))
def logs(lines):
    """Read API and worker logs, without exposing environment files."""
    web.run('journalctl', '-u', 'ajib-web-api', '-u', 'ajib-web-worker', '-n', str(lines), '--no-pager')


@web_group.command('stop')
def stop():
    """Stop only ajib website processes; leave bot and unrelated services running."""
    web.root_required()
    try:
        with web.maintenance():
            web.run('docker', 'stop', 'ajib-web-edge')
            web.run('systemctl', 'stop', *web.UNITS)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


@web_group.command('restart')
def restart():
    """Restart only the website API, worker and edge."""
    web.root_required()
    try:
        @web.maintained
        def perform():
            web.run('systemctl', 'restart', *web.UNITS)
            web.run('docker', 'restart', 'ajib-web-edge')
        perform()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


@web_group.command('renew-certificate')
def renew_certificate():
    """Run the standalone renewal check; Traefik handles its own certificates."""
    click.echo(web.renew_certificate())


@web_group.command('sync-config')
@click.option('--dry-run', is_flag=True, help='Show changed setting names and catalogs without revealing values.')
@click.option('--recover', is_flag=True, help='Retry an interrupted sync using current bot settings.')
@click.option('--yes', is_flag=True, help='Restart previously running web services after synchronization.')
def sync_config(dry_run, recover, yes):
    """Refresh the read-only pilot from bot settings, preserving website access gates."""
    import web_config
    try:
        if dry_run:
            click.echo(json.dumps(web_config.preview(), indent=2))
            return
        if not yes:
            raise click.ClickException('Preview with --dry-run, then apply with --yes.')
        click.echo(json.dumps(web_config.sync(recover=recover), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise click.ClickException(str(exc)) from exc


def apply_database_environment():
    """CLI backups/restore must follow the same database as all service runtimes."""
    if (web.CONFIG / 'deployment.json').is_file():
        database = web.load()['database']
        supplied = os.environ.get('AJIB_DB_PATH')
        if supplied and Path(supplied).resolve() != Path(database).resolve():
            raise click.ClickException('AJIB_DB_PATH conflicts with the installed website database.')
        os.environ['AJIB_DB_PATH'] = database
        os.environ['AJIB_DB_SHARED_GROUP'] = 'ajib-state'
