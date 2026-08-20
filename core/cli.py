#!/usr/bin/env python3

import typing
import click
import cli_api
import json


def pretty_print(data: typing.Any):
    if isinstance(data, dict):
        print(json.dumps(data, indent=4))
        return

    print(data)


@click.group()
def cli():
    pass

# region ajib


@cli.command('backup-ajib')
def backup_ajib():
    try:
        click.echo(cli_api.backup_ajib())
    except Exception as e:
        raise click.ClickException(str(e)) from e

@cli.command('restore-ajib')
@click.argument('backup_file_path', type=click.Path(exists=True, file_okay=True, dir_okay=False, readable=True))
def restore_ajib(backup_file_path):
    """Restore Telegram bot state from a backup ZIP file."""
    try:
        click.echo(cli_api.restore_ajib(backup_file_path))
    except Exception as e:
        raise click.ClickException(str(e)) from e

# endregion

@cli.command('server-info')
@click.option(
    '--section',
    type=click.Choice(['overview', 'business', 'customers', 'tech', 'traffic', 'alerts', 'full'], case_sensitive=False),
    default='full',
    show_default=True,
    help='Render a specific server info dashboard section.',
)
def server_info(section):
    try:
        res = cli_api.server_info(section=section)
        if res:
            pretty_print(res)
        else:
            click.echo('Server information not available.')
    except Exception as e:
        raise click.ClickException(str(e)) from e

@cli.command('telegram')
@click.option('--action', '-a', required=True, help='Action to perform: start or stop', type=click.Choice(['start', 'stop'], case_sensitive=False))
@click.option('--token', '-t', required=False, help='Token for running the telegram bot', type=str)
@click.option('--adminid', '-aid', required=False, help='Telegram admins ID for running the telegram bot', type=str)
@click.option('--api-url', '-u', required=False, help='API URL for the API client', type=str)
@click.option('--api-key', '-k', required=False, help='API key for the API client', type=str)
@click.option('--server', multiple=True, help='VPN server in id=url,token[,weight,enabled[,panel[,inbound_ids[,limit_ip]]]] format. Weight 0 pauses automatic placement. Inbound IDs use |. Can be repeated.', type=str)
def telegram(action: str, token: str, adminid: str, api_url: str, api_key: str, server):
    try:
        if action == 'start':
            if not token or not adminid or ((not api_url or not api_key) and not server):
                raise click.UsageError('Error: --token and --adminid are required. Provide --api-url/--api-key or at least one --server.')
            cli_api.start_telegram_bot(token, adminid, api_url, api_key, servers=server)
            click.echo('Telegram bot started successfully.')
        elif action == 'stop':
            cli_api.stop_telegram_bot()
            click.echo('Telegram bot stopped successfully.')
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cli.command('get-services-status')
def get_services_status():
    try:
        if services_status := cli_api.get_services_status():
            for service, status in services_status.items():
                click.echo(f"{service}: {'Active' if status else 'Inactive'}")
        else:
            click.echo('Error: Services status not available.')
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cli.command('show-version')
def show_version():
    """Display the currently installed bot version."""
    try:
        if version_info := cli_api.show_version():
             click.echo(version_info)
        else:
            click.echo("Error retrieving version")
    except Exception as e:
        raise click.ClickException(str(e)) from e


@cli.command('check-version')
def check_version():
    """Checks if the current version is up-to-date and displays changelog if not."""
    try:
        if version_info := cli_api.check_version():
            click.echo(version_info)
        else:
            click.echo("Error retrieving version")
    except Exception as e:
        raise click.ClickException(str(e)) from e

# endregion


if __name__ == '__main__':
    cli()
