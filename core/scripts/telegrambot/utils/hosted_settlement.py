"""Persisted hosted checkout economics and transport-independent finalization."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json

from . import account_operations as operations, database, state_store, web_store
from .atomic_store import locked_json, read_json
from .hosted_bots import tenant_file
from .time_utils import format_utc_timestamp


def payment(owner, payment_id):
    row = database.get_connection().execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?',
                                           ('hosted:' + str(owner), str(payment_id))).fetchone()
    return json.loads(row[0]) if row else None


def intent(owner, payment_id):
    row = database.get_connection().execute("SELECT value_json FROM kv_state WHERE namespace='hosted_settlement' AND scope=? AND state_key=?",
                                           ('hosted:' + str(owner), str(payment_id))).fetchone()
    return json.loads(row[0]) if row else None


def prepare(owner, payment_id, record, funded, *, bot_id=None, owner_snapshot=None):
    """Save the full authorized terms before an account dispatch can occur."""
    scope, payment_id = 'hosted:' + str(owner), str(payment_id)
    terms = operations.payment_terms(record)
    with database.transaction(operation='hosted_settlement_prepare') as db:
        current = payment(owner, payment_id)
        if not current or operations.payment_terms(current) != terms:
            raise operations.AccountBusy('Hosted payment changed before preparation')
        saved = intent(owner, payment_id)
        if saved:
            if saved['terms_digest'] != terms or saved['funded'] != bool(funded):
                raise operations.AccountBusy('Hosted settlement terms changed')
            return saved
        ident = f'hosted-payment:{owner}:{payment_id}'
        if operations.existing(ident) or current.get('provisioned_username') or current.get('status') == 'completed':
            raise operations.AccountBusy('Legacy fulfillment lacks persisted settlement provenance')
        referrals = state_store._load_referrals(db, scope) or {}
        saved = {'record': dict(record), 'funded': bool(funded), 'terms_digest': terms,
                 'financials': settlement_financials(record), 'bot_id': bot_id,
                 'referrer': (referrals.get('referrals') or {}).get(str(record['user_id'])),
                 'owner_snapshot': owner_snapshot, 'prepared_at': format_utc_timestamp()}
        db.execute("INSERT INTO kv_state(namespace,scope,state_key,value_json) VALUES ('hosted_settlement',?,?,?)",
                   (scope, payment_id, json.dumps(saved)))
        return saved


def language(owner, user):
    return read_json(tenant_file(owner, 'languages.json'), {}).get(str(user), 'en')


def _sale_and_referral(db, owner, payment_id, saved, common):
    from . import hosted_bots
    from .hosted_translations import hosted_text
    from .currency_format import format_usd_amount
    scope = 'hosted:' + str(owner)
    record, amounts = saved['record'], saved['financials']
    reward, margin = amounts['referral_reward'], amounts['margin']
    effective_funded = saved['funded'] or bool(record.get('wholesale_prepaid'))
    if effective_funded:
        accounted = hosted_bots.credit_crypto_sale(owner, payment_id, margin, reward, common)
        expected = {'sale:' + payment_id: margin}
    elif reward > 0:
        accounted = hosted_bots.add_referral_liability(owner, payment_id, reward, common)
        expected = {}
    else:
        accounted, expected = True, {}
    if reward > 0:
        expected['referral:' + payment_id] = reward
    if not accounted:
        actual = {str(item.get('id')): financial_amount(item.get('amount', 0), 'ledger amount')
                  for item in hosted_bots.get_ledger(owner).get('transactions', []) if isinstance(item, dict)}
        if any(actual.get(key) != financial_amount(amount, 'ledger amount') for key, amount in expected.items()):
            raise operations.AccountBusy('Hosted sale accounting could not be verified')
    data = state_store._load_referrals(db, scope) or {}
    user = str(record['user_id'])
    if (data.get('referrals') or {}).get(user) != saved['referrer']:
        raise operations.AccountBusy('Referral attribution changed after preparation')
    referrer = saved['referrer']
    if reward > 0:
        if not referrer:
            raise operations.AccountBusy('Referral reward has no recorded recipient')
        rewarded = data.setdefault('rewarded_orders', {})
        if payment_id in rewarded:
            if financial_amount(rewarded[payment_id], 'reward') != financial_amount(reward, 'reward'):
                raise operations.AccountBusy('Recorded referral reward differs')
        else:
            stats = data.setdefault('stats', {}).setdefault(str(referrer), {
                'count': 0, 'total_earnings': 0.0, 'available_balance': 0.0})
            for key in ('total_earnings', 'available_balance'):
                stats[key] = round(float(stats.get(key, 0)) + reward, 2)
            rewarded[payment_id] = reward
        text = hosted_text(language(owner, referrer), 'referral_reward_ready').format(amount=format_usd_amount(reward))
        web_store.enqueue(db, f'hosted-reward:{owner}:{payment_id}', scope, referrer, text)
    if float(record.get('invite_discount_percent', 0) or 0) > 0:
        redeemed = data.setdefault('buyer_discount_redeemed', {})
        reservation = data.setdefault('buyer_discount_reservations', {}).get(user)
        if user in redeemed:
            if str(redeemed[user].get('order_id')) != payment_id:
                raise operations.AccountBusy('Invite discount was redeemed by a different order')
        elif not isinstance(reservation, dict) or str(reservation.get('order_id')) != payment_id:
            raise operations.AccountBusy('Invite discount reservation is missing')
        else:
            redeemed[user] = {'order_id': payment_id, 'redeemed_at': format_utc_timestamp()}
            data['buyer_discount_reservations'].pop(user)
    state_store._save_referrals(db, scope, data)


def _common(owner, payment_id, saved, username, server, result):
    record = saved['record']
    common = {key: record.get(key) for key in ('retail_price', 'list_price', 'reseller_level',
              'discount_percent', 'plan_gb', 'days', 'unlimited')}
    common.update(username=username, server_id=server, customer_telegram_id=int(record['user_id']),
                  customer_telegram_username=record.get('telegram_username'), reseller_id=str(owner),
                  origin_bot_id=saved['bot_id'], retail_order_id=payment_id, price=record['wholesale_price'])
    if record.get('renew_username'):
        common.update(renewal_server_id=server,
                      renewal_recorded_server_id=record.get('renewal_recorded_server_id') or record.get('server_id') or server,
                      renewal_source_plan_snapshot=record.get('renewal_source_plan_snapshot') or {},
                      renewal_plan_snapshot=record.get('renewal_plan_snapshot') or {
                          'plan_gb': record['plan_gb'], 'days': record['days'], 'unlimited': record.get('unlimited', False),
                          'price': record['wholesale_price'], 'full_price': record.get('list_price'),
                          'reseller_level': record.get('reseller_level'), 'discount_percent': record.get('discount_percent')},
                      before_state=result.get('before_state'), after_state=result.get('after_state'))
    return common


def _wholesale(owner, payment_id, saved, common, *, reserved=False):
    from . import reseller, reseller_funding, reseller_wholesale_credit as prepaid, hosted_bots
    record = saved['record']
    username, server = common['username'], common.get('renewal_recorded_server_id') or common['server_id']
    renewed = bool(record.get('renew_username'))
    if record.get('funding'):
        # Reserved-renewal finalizers may return an empty result mapping.
        reseller_funding.finalize_funding(owner, payment_id, common,
            kind='reserved_renewal' if reserved else 'renewal' if renewed else 'config', username=username, server_id=server)
        accounted = True
    elif record.get('wholesale_prepaid'):
        if reserved:
            accounted, _ = prepaid.finalize_prepaid_reserved_renewal(owner, payment_id, username, record['wholesale_price'], common, server)
        else:
            accounted = (prepaid.finalize_prepaid_renewal(owner, payment_id, username, record['wholesale_price'], common, server)
                         if renewed else prepaid.finalize_prepaid_config(owner, payment_id, record['wholesale_price'], common))
    else:
        if not saved['funded']:
            reservation = hosted_bots.get_ledger(owner).get('credit_reservations', {}).get(payment_id)
            if not reservation or financial_amount(reservation.get('amount'), 'reservation') != financial_amount(record['wholesale_price'], 'wholesale'):
                raise operations.AccountBusy('Hosted credit reservation changed')
        if reserved:
            accounted, _ = reseller.reserve_reseller_renewal(owner, username, record['wholesale_price'], common,
                server_id=server, funded=saved['funded'], enforce_credit=False)
            if accounted and not saved['funded']:
                hosted_bots.release_credit(owner, payment_id, kind='renewal_credit_consumed')
        elif saved['funded']:
            accounted = (reseller.record_funded_reseller_renewal(owner, username, record['wholesale_price'], common, server)
                         if renewed else reseller.record_funded_reseller_config(owner, record['wholesale_price'], common))
        else:
            accounted = (hosted_bots.consume_renewal_credit(owner, payment_id, username, common, server)
                         if renewed else hosted_bots.consume_credit(owner, payment_id, common))
    if not accounted:
        raise operations.AccountBusy('Hosted wholesale accounting failed')


def reserve(owner, payment_id):
    """Charge and queue a future renewal atomically; no panel mutation yet."""
    from . import reseller
    from .renewal import mark_payment_renewal_reserved
    from .hosted_translations import hosted_text
    owner, payment_id = str(owner), str(payment_id)
    saved = intent(owner, payment_id)
    if not saved or saved['record'].get('renewal_mode') != 'reserved':
        raise operations.AccountBusy('Reserved settlement provenance is missing')
    record = saved['record']
    username, server = record.get('renew_username'), record.get('server_id')
    with operations.serialize(server, username), reseller.reseller_lock, database.transaction(operation='hosted_reserved_settlement') as db:
        operations.assert_available(server, username)
        current = payment(owner, payment_id)
        if not current or operations.payment_terms(current) != saved['terms_digest'] or current.get('status') in {'cancelled', 'canceled', 'rejected'}:
            raise operations.AccountBusy('Reserved settlement terms changed')
        if current.get('status') == 'completed':
            if current.get('renewal_status') not in {'reserved', 'processing', 'attention', 'applied'}:
                raise operations.AccountBusy('Reserved settlement records disagree')
            return current
        common = _common(owner, payment_id, saved, username, server, {})
        common.update(reservation_id=payment_id, gb=record['plan_gb'], renewal_source='hosted_customer',
                      renewal_mode='reserved', renewal_status='reserved', renewal_reserved_at=format_utc_timestamp(),
                      renewal_baseline=record.get('renewal_baseline') or {}, before_state=record.get('renewal_baseline') or {},
                      after_state=None, renewal_attempts=0)
        _wholesale(owner, payment_id, saved, common, reserved=True)
        _sale_and_referral(db, owner, payment_id, saved, common)
        fields = {'username': username, 'server_id': server, 'renewal_server_id': server, 'reservation_id': payment_id}
        if saved.get('owner_snapshot'):
            fields['owner_payment_followup'] = {'snapshot': {**saved['owner_snapshot'], 'username': username,
                                                             'completed_at': format_utc_timestamp()}}
        if not mark_payment_renewal_reserved(payment_id, payments_file=tenant_file(owner, 'payments.json'), fields=fields):
            raise operations.AccountBusy('Reserved payment could not be recorded')
        web_store.enqueue(db, f'hosted-reserved:{owner}:{payment_id}', 'hosted:' + owner, record['user_id'],
                          hosted_text(language(owner, record['user_id']), 'renewal_reserved_success'))
        return payment(owner, payment_id)


def finalize(owner, payment_id):
    """Complete a verified panel obligation. Never calls the panel or Telegram."""
    from . import reseller, reseller_funding, reseller_wholesale_credit as prepaid, hosted_bots
    owner, payment_id = str(owner), str(payment_id)
    ident, scope = f'hosted-payment:{owner}:{payment_id}', 'hosted:' + owner
    with reseller.reseller_lock, database.transaction(operation='hosted_settlement_finalize') as db:
        saved, current = intent(owner, payment_id), payment(owner, payment_id)
        operation, detail = operations.existing(ident), operations.details(ident)
        if not saved or not current or not operation or not detail or operation['status'] != 'succeeded':
            raise operations.AccountBusy('Hosted panel outcome or settlement provenance requires investigation')
        if (operations.payment_terms(current) != saved['terms_digest'] or
                json.loads(detail['origin_json']).get('terms_digest') != saved['terms_digest'] or
                current.get('status') in {'cancelled', 'canceled', 'rejected'}):
            raise operations.AccountBusy('Hosted payment terms changed after dispatch')
        if detail['phase'] == 'completed':
            if current.get('status') != 'completed':
                raise operations.AccountBusy('Hosted completion records disagree')
            return current
        record, result = saved['record'], json.loads(operation['result_json'])
        username, server = operation['username'], operation['server_id']
        common = _common(owner, payment_id, saved, username, server, result)
        renewed = bool(record.get('renew_username'))
        _wholesale(owner, payment_id, saved, common)
        _sale_and_referral(db, owner, payment_id, saved, common)
        now = format_utc_timestamp()
        current.update(status='completed', username=username, server_id=server, completed_at=current.get('completed_at') or now, updated_at=now)
        for key in ('processing_started_at', 'processing_from_status'):
            current.pop(key, None)
        if renewed:
            current.update(renewal_before_state=result.get('before_state'), renewal_after_state=result.get('after_state'))
            from .renewal import mark_cleanup_state_renewed
            mark_cleanup_state_renewed(username, server)
        if saved.get('owner_snapshot'):
            current.setdefault('owner_payment_followup', {'snapshot': {
                **saved['owner_snapshot'], 'username': username, 'completed_at': current['completed_at']}})
        state_store._save_payment_record(db, scope, payment_id, current)
        messages = {
            'en': 'Your service is ready. Open My connections to access it.',
            'fa': 'سرویس شما آماده است. برای دسترسی، اتصال‌های من را باز کنید.',
            'ru': 'Ваш сервис готов. Откройте «Мои подключения».',
            'tk': 'Hyzmatyňyz taýýar. Birikmeleriňizi açyň.',
        }
        web_store.enqueue(db, f'hosted-complete:{owner}:{payment_id}', scope, record['user_id'],
                          messages.get(language(owner, record['user_id']), messages['en']))
        operations.event(db, ident, 'hosted_accounting_completed', actor='hosted_settlement')
        operations.complete(ident)
        return current


def financial_amount(value, field):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Invalid hosted payment {field}") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Invalid hosted payment {field}")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def settlement_financials(record):
    """Validate immutable hosted checkout economics before side effects."""
    if not isinstance(record, dict):
        raise ValueError("Invalid hosted payment record")

    payment_method = str(record.get("payment_method") or "").strip().lower()
    if payment_method == "account_credit":
        raise ValueError("Main-account credit is not valid for hosted-store checkout")
    for field in (
        "account_credit_reserved",
        "account_credit_consumed",
        "account_credit_applied",
    ):
        if field in record and financial_amount(record.get(field, 0), field) > 0:
            raise ValueError("Main-account credit cannot reduce hosted-store proceeds")

    collected_value = record.get("collected_amount")
    if collected_value is None and payment_method == "crypto":
        collected_value = record.get("crypto_collected")
    if collected_value is None:
        collected_value = record.get("retail_price")
    collected = financial_amount(collected_value, "collected amount")
    wholesale = financial_amount(record.get("wholesale_price"), "wholesale price")
    margin = (collected - wholesale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if margin < 0:
        raise ValueError("Hosted payment route falls below wholesale cost")

    reward = financial_amount(record.get("referral_reward", 0), "referral reward")
    if reward > margin:
        raise ValueError("Hosted referral reward exceeds positive post-discount margin")

    if record.get("reward_calculation_base") is not None:
        reward_base = financial_amount(
            record.get("reward_calculation_base"),
            "reward calculation base",
        )
        if reward_base != margin:
            raise ValueError("Hosted referral reward base is not the post-discount margin")

    if record.get("margin") is not None:
        recorded_margin = financial_amount(record.get("margin"), "margin")
        if recorded_margin != margin:
            raise ValueError("Hosted payment margin does not match collected amount")

    component_fields = ("invite_discount_percent", "crypto_discount_percent")
    components = Decimal("0")
    for field in component_fields:
        if record.get(field) is not None:
            components += financial_amount(record.get(field), field)
    if components > Decimal("10.00"):
        raise ValueError("Hosted customer discount components exceed the 10% cap")
    if record.get("total_discount_percent") is not None:
        total_discount = financial_amount(
            record.get("total_discount_percent"),
            "total discount percent",
        )
        if total_discount > Decimal("10.00"):
            raise ValueError("Hosted customer discount exceeds the 10% cap")
        if any(record.get(field) is not None for field in component_fields) and total_discount != components:
            raise ValueError("Hosted customer discount components do not match the capped total")

    if record.get("original_price") is not None and record.get("total_discount_amount") is not None:
        original_price = financial_amount(record.get("original_price"), "original price")
        total_discount_amount = financial_amount(
            record.get("total_discount_amount"),
            "total discount amount",
        )
        if total_discount_amount > original_price or original_price - total_discount_amount != collected:
            raise ValueError("Hosted collected amount does not match the recorded discount")
        if (
            record.get("invite_discount_amount") is not None
            or record.get("crypto_discount_amount") is not None
        ):
            invite_amount = financial_amount(
                record.get("invite_discount_amount", 0),
                "invite discount amount",
            )
            crypto_amount = financial_amount(
                record.get("crypto_discount_amount", 0),
                "crypto discount amount",
            )
            if invite_amount + crypto_amount != total_discount_amount:
                raise ValueError("Hosted discount amounts do not match the collected total")

    return {
        "collected_amount": float(collected),
        "wholesale_price": float(wholesale),
        "margin": float(margin),
        "reward_calculation_base": float(margin),
        "referral_reward": float(reward),
    }


