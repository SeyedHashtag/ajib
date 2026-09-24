"""No-funded cancellation is an operator action, never a panel retry."""
import json
from pathlib import Path
import sys
import time
from contextlib import nullcontext

import pytest
from click.testing import CliRunner


class Gateway:
    merchant_id = 'synthetic-merchant'

    def __init__(self, **changes):
        self.changes = changes
        self.calls = 0

    def check_payment_status(self, payment_id=None, *, order_id=None):
        assert payment_id == 'synthetic-invoice' and order_id is None
        self.calls += 1
        return {'state': 0, 'result': {'uuid': 'synthetic-invoice', 'order_id': 'web_cancelled',
                           'currency': 'USD', 'amount': '10.00',
                           'payment_amount_usd': '0', 'payment_amount': '0',
                           'status': 'cancel', 'payment_status': 'cancel',
                           'is_final': True, **self.changes}}


@pytest.fixture
def unpaid(storage):
    from utils import database
    from utils.web_orders import save_payment
    with database.transaction() as db:
        db.execute("INSERT INTO web_release_control VALUES (1,'pilot','[]',0,1,?,NULL,?)",
                   ('a' * 40, int(time.time())))
        save_payment(db, 'main', 'web_cancelled', {
            'user_id': 123, 'status': 'uncertain', 'price': '10.00',
            'currency': 'USD', 'payment_method': 'Crypto', 'gateway_order_id': 'web_cancelled',
            'gateway_payment_id': 'synthetic-invoice', 'gateway_merchant_id': 'synthetic-merchant',
            'fulfillment_owner': 'web', 'web_attention_reason': 'gateway_cancel'})
        db.execute('INSERT INTO web_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                   ('web_cancelled', 'main', '123', 'key', 'hash', 'purchase',
                    'uncertain', '{}', 1, 1))
    return 'web_cancelled'


def test_inspect_is_read_only_and_apply_closes_original_once(unpaid):
    from utils import database
    from utils import unpaid_crypto_recovery as recovery
    gateway = Gateway()
    before = database.get_connection().execute('SELECT COUNT(*) FROM payment_events').fetchone()[0]
    report = recovery.inspect(unpaid, gateway)
    assert report['provider_received_usd'] == '0'
    assert report['panel_request_needed'] is False
    assert database.get_connection().execute(
        'SELECT status FROM web_operations WHERE id=?', (unpaid,)).fetchone()[0] == 'uncertain'
    assert recovery.apply(unpaid, report['evidence_digest'], gateway)['status'] == 'cancelled'
    assert recovery.apply(unpaid, report['evidence_digest'], gateway)['status'] == 'already_reconciled'
    assert gateway.calls == 2  # one inspection and one fresh check before the first commit
    row = database.get_connection().execute(
        'SELECT status,payload_json FROM payments WHERE payment_id=?', (unpaid,)).fetchone()
    assert row['status'] == 'cancelled'
    assert json.loads(row['payload_json'])['gateway_cancel_evidence_digest'] == report['evidence_digest']
    assert database.get_connection().execute(
        'SELECT status FROM web_operations WHERE id=?', (unpaid,)).fetchone()[0] == 'cancelled'
    assert database.get_connection().execute('SELECT COUNT(*) FROM payment_events').fetchone()[0] == before + 1
    assert database.get_connection().execute(
        "SELECT COUNT(*) FROM web_audit WHERE action='gateway.cancelled_unfunded'").fetchone()[0] == 1


@pytest.mark.parametrize('change', [
    {'status': 'paid'}, {'payment_amount_usd': '0.01'}, {'payment_amount_usd': None},
    {'is_final': False},
    {'payment_amount': '0.01'}, {'payment_status': 'paid'},
    {'uuid': 'other'}, {'order_id': 'other'}, {'currency': 'EUR'}, {'amount': '9.99'},
])
def test_changed_or_incomplete_provider_evidence_keeps_reservation(unpaid, change):
    from utils import unpaid_crypto_recovery as recovery
    from utils import database
    with pytest.raises(ValueError):
        recovery.inspect(unpaid, Gateway(**change))
    assert database.get_connection().execute(
        'SELECT status FROM web_operations WHERE id=?', (unpaid,)).fetchone()[0] == 'uncertain'


def test_stale_evidence_and_open_writes_cannot_apply(unpaid):
    from utils import database
    from utils import unpaid_crypto_recovery as recovery
    from utils.web_orders import save_payment
    digest = recovery.inspect(unpaid, Gateway())['evidence_digest']
    with database.transaction() as db:
        save_payment(db, 'main', unpaid, {'operator_note': 'changed'})
    with pytest.raises(ValueError, match='Evidence changed'):
        recovery.apply(unpaid, digest, Gateway())
    digest = recovery.inspect(unpaid, Gateway())['evidence_digest']
    with database.transaction() as db:
        db.execute('UPDATE web_release_control SET accept_writes=1')
    with pytest.raises(ValueError, match='Pause new customer writes'):
        recovery.apply(unpaid, digest, Gateway())


def test_account_claim_and_failed_accounting_keep_original_order(unpaid, monkeypatch):
    from utils import database
    from utils import unpaid_crypto_recovery as recovery
    from utils import purchase_incentives
    with database.transaction() as db:
        db.execute('INSERT INTO account_operations VALUES (?,?,?,?,?,?,?,?,?)',
                   ('main-payment:' + unpaid, 'server', 'user', 'create', 'uncertain', '{}', None, 1, 1))
    with pytest.raises(ValueError, match='account-operation history'):
        recovery.inspect(unpaid, Gateway())
    with database.transaction() as db:
        db.execute('DELETE FROM account_operations')
    digest = recovery.inspect(unpaid, Gateway())['evidence_digest']
    monkeypatch.setattr(purchase_incentives, 'release_main_checkout',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic')))
    with pytest.raises(RuntimeError):
        recovery.apply(unpaid, digest, Gateway())
    assert database.get_connection().execute(
        'SELECT status FROM web_operations WHERE id=?', (unpaid,)).fetchone()[0] == 'uncertain'


def test_prior_fulfillment_history_blocks_unpaid_closure(unpaid):
    from utils import database
    from utils import unpaid_crypto_recovery as recovery
    with database.transaction() as db:
        db.execute('INSERT INTO payment_events VALUES (?,?,?,?,?,?,?)',
                   ('main', unpaid, 10, 'approved', 'pending', 'synthetic-time', '{}'))
    with pytest.raises(ValueError, match='fulfillment history'):
        recovery.inspect(unpaid, Gateway())


def test_operator_cli_requires_fresh_digest_and_confirmation(unpaid, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
    import operations_cli
    import web_operator
    import web_upgrade
    from utils import payments
    monkeypatch.setattr(operations_cli, '_services', lambda **kwargs: (None, None))
    monkeypatch.setattr(payments, 'CryptoPayment', Gateway)
    monkeypatch.setattr(web_operator, 'maintenance', nullcontext)
    monkeypatch.setattr(web_upgrade, '_no_pending', lambda: None)
    runner = CliRunner()
    command = ['reconcile-unpaid-crypto', unpaid]
    assert runner.invoke(operations_cli.operations_group, command + ['--yes']).exit_code != 0
    preview = runner.invoke(operations_cli.operations_group, command + ['--dry-run'])
    assert preview.exit_code == 0, preview.output
    digest = json.loads(preview.output)['evidence_digest']
    applied = runner.invoke(operations_cli.operations_group,
                            command + ['--evidence', digest, '--yes'])
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.output)['status'] == 'cancelled'
