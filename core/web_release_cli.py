"""Root-only admission and release evidence controls for the main storefront."""
import json
from pathlib import Path
import sqlite3
import time
import re
from decimal import Decimal, InvalidOperation

import click
import web_operator as web
import web_upgrade as upgrade

AUTOMATED_CHECKS = {'python', 'shell', 'api', 'frontend', 'browser', 'concurrency', 'upgrade_recovery', 'backup_restore'}
LIVE_CHECKS = {'live_card', 'live_crypto', 'cross_interface', 'immediate_renewal', 'reserved_renewal',
               'trials', 'referrals_credits_withdrawals', 'pilot_observation'}
ALL_CHECKS = AUTOMATED_CHECKS | LIVE_CHECKS


def dotenv_values(*args, **kwargs):
    # Keep optional deployment dependencies out of bot-only CLI imports.
    from dotenv import dotenv_values as read_values
    return read_values(*args, **kwargs)


def _baseline(plan):
    value = upgrade._check_baseline(plan)
    if not value['contract'].get('customer_release_ready'):
        raise ValueError('This release is not yet cleared for Stage 4 customer writes.')
    if value['commit'] == 'adopted':
        raise ValueError('Install a tested immutable release before opening the customer pilot.')
    return value


def diagnostics(plan):
    with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
        now = int(time.time())
        worker = db.execute("SELECT heartbeat_at,last_error FROM web_worker_health WHERE role='worker'").fetchone()
        pending = db.execute("SELECT COUNT(*),MIN(created_at) FROM web_operations WHERE status NOT IN ('completed','cancelled','rejected')").fetchone()
        uncertain = db.execute("""SELECT
            (SELECT COUNT(*) FROM web_operations WHERE status='uncertain') +
            (SELECT COUNT(*) FROM web_trials WHERE status='uncertain') +
            (SELECT COUNT(*) FROM account_operations WHERE status='uncertain' OR (status='executing' AND updated_at<?))""",
            (now - 600,)).fetchone()[0]
        notifications = db.execute("SELECT COUNT(*),MIN(next_attempt_at) FROM web_outbox WHERE status!='sent'").fetchone()
        return {'worker_healthy': bool(worker and worker[0] >= now-30 and worker[1] is None),
                'pending_operations': pending[0], 'oldest_pending_age_seconds': now-pending[1] if pending[1] else 0,
                'uncertain_operations': uncertain, 'notification_backlog': notifications[0],
                'oldest_notification_delay_seconds': max(0, now-notifications[1]) if notifications[1] else 0}


def state():
    plan = web.load()
    value = upgrade._policy(plan)
    with sqlite3.connect(Path(plan['database']).as_uri() + '?mode=ro', uri=True) as db:
        checks = {row[0]: bool(row[1]) for row in db.execute('SELECT name,passed FROM web_release_checks WHERE revision=?', (value['revision'],))}
    return {'policy': value, 'checks': checks, 'missing_checks': sorted(name for name in ALL_CHECKS if not checks.get(name)),
            'health': diagnostics(plan)}


def _audit(db, action, revision, details):
    db.execute('INSERT INTO web_audit(actor,scope,action,resource,occurred_at,details_json) VALUES (?,?,?,?,?,?)',
               ('cli', 'main', action, revision, int(time.time()), json.dumps(details)))


def change(mode, users=(), halt_worker=False):
    web.root_required()
    with web.maintenance():
        # Emergency pause remains available during failed maintenance.
        if mode != 'pause':
            upgrade._no_pending()
        plan = web.load()
        current = upgrade._policy(plan)
        if mode == 'pause':
            desired = {**current, 'accept_writes': 0,
                       'process_existing': 0 if halt_worker else current['process_existing'], 'pilot_started_at': None}
        elif mode == 'admin':
            desired = {**current, 'access': 'admin', 'accept_writes': 0, 'pilot_started_at': None}
        else:
            baseline = _baseline(plan)
            revision = baseline['commit']
            admins = {str(value) for value in json.loads(dotenv_values(web.CONFIG / 'runtime.env').get('ADMIN_USER_IDS', '[]'))}
            if mode == 'pilot':
                users = sorted(set(str(user).strip() for user in users))
                if len(users) < 2 or any(not user.isdigit() or not 0 < int(user) < 2**53 for user in users) or set(users) & admins:
                    raise ValueError('Provide at least two distinct non-admin numeric Telegram IDs.')
                desired = {**current, 'access': 'pilot', 'pilot_users_json': json.dumps(users),
                           'accept_writes': 1, 'process_existing': 1, 'revision': revision,
                           'pilot_started_at': int(time.time())}
                with sqlite3.connect(plan['database']) as db:
                    passed = {row[0] for row in db.execute('SELECT name FROM web_release_checks WHERE revision=? AND passed=1', (revision,))}
                if not AUTOMATED_CHECKS <= passed:
                    raise ValueError('Required automated release checks have not passed for this revision.')
            elif mode == 'public':
                report = state()
                if (current['revision'] != revision or current['access'] != 'pilot' or
                        not current['accept_writes'] or not current['process_existing'] or len(json.loads(current['pilot_users_json'])) < 2):
                    raise ValueError('Run a named-customer pilot on the installed revision before promotion.')
                if not current['pilot_started_at'] or int(time.time())-current['pilot_started_at'] < 86400:
                    raise ValueError('The customer pilot must run for at least 24 hours.')
                if report['missing_checks']:
                    raise ValueError('Release evidence is incomplete: ' + ', '.join(report['missing_checks']))
                health = report['health']
                if not health['worker_healthy'] or health['uncertain_operations'] or health['oldest_notification_delay_seconds'] > 300:
                    raise ValueError('Worker, fulfillment, or notification health blocks promotion.')
                desired = {**current, 'access': 'public', 'accept_writes': 1, 'process_existing': 1}
            else:
                raise ValueError('Unknown release mode.')
        upgrade._save_policy(plan, desired)
        with sqlite3.connect(plan['database']) as db:
            _audit(db, 'release.' + mode, desired['revision'], {'access': desired['access'],
                'accept_writes': bool(desired['accept_writes']), 'process_existing': bool(desired['process_existing'])})
        return {'access': desired['access'], 'accept_writes': bool(desired['accept_writes']),
                'process_existing': bool(desired['process_existing']), 'revision': desired['revision']}


def record_check(name, passed, evidence):
    web.root_required()
    if name not in ALL_CHECKS:
        raise ValueError('Unknown acceptance check.')
    if (not isinstance(evidence, dict) or not isinstance(evidence.get('note'), str)
            or not 10 <= len(evidence['note']) <= 2000 or set(evidence) - {'note', 'payment_ids', 'artifact_sha256'}):
        raise ValueError('Evidence requires a 10–2000 character note; optional fields are payment_ids and artifact_sha256.')
    if 'artifact_sha256' in evidence and not re.fullmatch(r'[a-f0-9]{64}', str(evidence['artifact_sha256'])):
        raise ValueError('artifact_sha256 must be a lowercase SHA-256 digest.')
    with web.maintenance():
        upgrade._no_pending()
        plan = web.load()
        baseline = upgrade._check_baseline(plan)
        revision = baseline['commit']
        if revision == 'adopted':
            raise ValueError('Evidence must reference an immutable installed release.')
        with sqlite3.connect(plan['database']) as db:
            if passed and name in {'live_card', 'live_crypto', 'immediate_renewal', 'reserved_renewal'}:
                ids = evidence.get('payment_ids')
                if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
                    raise ValueError('This live check requires verified payment IDs.')
                for key in ids:
                    row = db.execute("SELECT payload_json FROM payments WHERE scope='main' AND payment_id=?", (key,)).fetchone()
                    record = json.loads(row[0]) if row else {}
                    if record.get('status') != 'completed' or record.get('web_revision') != revision:
                        raise ValueError('Payment must be completed on this deployed revision.')
                    policy = upgrade._policy(plan)
                    if (policy['access'] != 'pilot' or policy['revision'] != revision
                            or str(record.get('user_id')) not in json.loads(policy['pilot_users_json'])):
                        raise ValueError('Payment must belong to an explicitly selected customer in this pilot.')
                    if name == 'live_card' and record.get('payment_method') != 'Card to Card':
                        raise ValueError('Expected a card payment.')
                    if name == 'live_crypto' and record.get('payment_method') != 'Crypto':
                        raise ValueError('Expected a crypto payment.')
                    if name in {'live_card', 'live_crypto'}:
                        try:
                            amount = Decimal(str(record.get('price')))
                            valid_amount = amount.is_finite() and amount > 0
                        except InvalidOperation:
                            valid_amount = False
                        if not valid_amount:
                            raise ValueError('A real positive payment is required; a credit-only order is insufficient.')
                    if name == 'live_card' and record.get('reviewed_action') != 'approve':
                        raise ValueError('The card transfer must have an approved review record.')
                    if name == 'live_crypto':
                        try:
                            received = Decimal(str(record.get('gateway_verified_payment_amount_usd')))
                            verified = received.is_finite() and received >= amount
                        except InvalidOperation:
                            verified = False
                        if not verified or not record.get('gateway_payment_id') or not record.get('gateway_merchant_id'):
                            raise ValueError('The crypto invoice must have verified provider payment evidence.')
                    if name in {'immediate_renewal', 'reserved_renewal'} and record.get('type') != 'renewal':
                        raise ValueError('Expected a renewal payment.')
                    if name == 'reserved_renewal' and record.get('renewal_status') != 'applied':
                        raise ValueError('The reserved renewal must actually be applied.')
                    if name == 'reserved_renewal' and record.get('renewal_mode') != 'reserved':
                        raise ValueError('Expected a reserved renewal.')
                    if name == 'immediate_renewal' and record.get('renewal_mode') == 'reserved':
                        raise ValueError('Expected an immediate renewal.')
            db.execute('INSERT OR REPLACE INTO web_release_checks VALUES (?,?,?,?,?)',
                       (revision, name, int(passed), json.dumps(evidence), int(time.time())))
            _audit(db, 'release.check', revision, {'name': name, 'passed': bool(passed)})
        return {'revision': revision, 'name': name, 'passed': bool(passed)}


@click.group('release')
def release_group():
    """Manage main-store access and release evidence without restarting the API."""


@release_group.command('status')
def release_status():
    click.echo(json.dumps(state(), indent=2))


@release_group.command('set')
@click.argument('mode', type=click.Choice(['admin', 'pilot', 'public', 'pause']))
@click.option('--users', default='', help='Comma-separated non-admin Telegram IDs for pilot mode.')
@click.option('--halt-worker', is_flag=True, help='With pause, also pause existing web financial work.')
@click.option('--yes', is_flag=True)
def set_release(mode, users, halt_worker, yes):
    if not yes:
        raise click.ClickException('Inspect release status and apply the chosen mode with --yes.')
    if (halt_worker and mode != 'pause') or (users and mode != 'pilot'):
        raise click.ClickException('--users applies only to pilot; --halt-worker applies only to pause.')
    try:
        click.echo(json.dumps(change(mode, users.split(',') if users else (), halt_worker), indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc


@release_group.command('record-check')
@click.option('--name', required=True, type=click.Choice(sorted(ALL_CHECKS)))
@click.option('--passed/--failed', required=True)
@click.option('--evidence-file', required=True, type=click.Path(exists=True, dir_okay=False))
def check_command(name, passed, evidence_file):
    try:
        click.echo(json.dumps(record_check(name, passed, json.loads(Path(evidence_file).read_text())), indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc
