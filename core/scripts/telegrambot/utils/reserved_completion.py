"""Atomic completion of verified scheduled renewals and their notifications."""
import json
import hashlib

from . import account_operations as operations, database, state_store, web_store


def reseller_obligation(owner, reservation_id):
    row = database.get_connection().execute('SELECT payload_json FROM resellers WHERE reseller_id=?', (str(owner),)).fetchone()
    data = json.loads(row[0]) if row else {}
    matches = [(config, reservation) for config in data.get('configs', [])
               for reservation in config.get('renewals', []) if str(reservation.get('reservation_id')) == str(reservation_id)]
    if len(matches) != 1:
        return None
    config, reservation = matches[0]
    return {'config': config, 'reservation': reservation}


def reseller_terms(obligation):
    if not obligation:
        return None
    config, record = obligation['config'], obligation['reservation']
    fields = ('reservation_id', 'retail_order_id', 'price', 'debt_charge_id', 'funding', 'funded_at_checkout',
              'gb', 'plan_gb', 'days', 'unlimited', 'renewal_source_plan_snapshot', 'renewal_plan_snapshot', 'renewal_baseline')
    binding = {key: record.get(key) for key in fields}
    binding['owner'] = {key: config.get(key) for key in ('username', 'server_id', 'customer_telegram_id', 'customer_id')}
    return hashlib.sha256(json.dumps(binding, sort_keys=True, default=str).encode()).hexdigest()


def reseller(owner, reservation_id, claim_id, *, fields=None, now=None):
    from . import reseller as store, renewal
    ident = f'reseller-reservation:{owner}:{reservation_id}'
    with store.reseller_lock, database.transaction(operation='reseller_reserved_complete') as db:
        obligation = reseller_obligation(owner, reservation_id)
        operation, detail = operations.existing(ident), operations.details(ident)
        if not obligation or not operation or not detail or operation['status'] != 'succeeded':
            raise operations.AccountBusy('Reserved reseller outcome requires verified provenance')
        origin = json.loads(detail['origin_json'])
        if origin.get('type') != 'reseller_reservation' or origin.get('terms_digest') != reseller_terms(obligation):
            raise operations.AccountBusy('Reserved reseller ownership or terms changed')
        if detail['phase'] == 'completed':
            return obligation['reservation'].get('renewal_status') == 'applied'
        result = json.loads(operation['result_json'])
        fields = {**(fields or {}), 'before_state': result.get('before_state'), 'after_state': result.get('after_state'),
                  'renewal_server_id': operation['server_id']}
        if not store._finish_reseller_renewal_reservation(owner, reservation_id, claim_id, 'applied', fields=fields, now=now):
            raise operations.AccountBusy('Reserved reseller processing ownership changed')
        renewal.mark_cleanup_state_renewed(operation['username'], operation['server_id'])
        _notify(db, ident, 'main', owner)
        operations.event(db, ident, 'reseller_reserved_completed', actor='renewal_finalizer')
        operations.complete(ident)
        return True


def _notify(db, ident, scope, user):
    if user is None:
        raise operations.AccountBusy('Renewal notification recipient is missing')
    namespace = 'user_languages' if scope == 'main' else 'hosted_languages'
    row = db.execute('SELECT value_json FROM kv_state WHERE namespace=? AND scope=? AND state_key=?',
                     (namespace, scope, str(user))).fetchone()
    language = json.loads(row[0]) if row else 'en'
    texts = {
        'en': 'Your reserved renewal is now active. Open My connections for details.',
        'fa': 'تمدید رزروشده شما فعال شد. جزئیات را در اتصال‌های من ببینید.',
        'ru': 'Запланированное продление активировано. Подробности — в разделе «Мои подключения».',
        'tk': 'Ätiýaçdaky uzaltmaňyz işjeňleşdi. Maglumat üçin birikmeleriňizi açyň.',
    }
    web_store.enqueue(db, 'renewal-applied:' + ident, scope, user, texts.get(language, texts['en']))


def payment(payment_id, claim_id, *, payments_file, fields=None, now=None):
    from . import reseller, renewal
    descriptor = state_store.describe_path(payments_file)
    if not descriptor or descriptor.kind != 'payments':
        raise operations.AccountBusy('Renewal payment scope is unknown')
    scope = descriptor.scope
    ident = ('main-payment:' if scope == 'main' else 'hosted-payment:' + scope.removeprefix('hosted:') + ':') + str(payment_id)
    with reseller.reseller_lock, database.transaction(operation='reserved_payment_complete') as db:
        row = db.execute('SELECT payload_json FROM payments WHERE scope=? AND payment_id=?', (scope, str(payment_id))).fetchone()
        operation, detail = operations.existing(ident), operations.details(ident)
        record = json.loads(row[0]) if row else None
        if not record or not operation or not detail or operation['status'] != 'succeeded' or operation['kind'] != 'renewal':
            raise operations.AccountBusy('Reserved renewal requires verified panel provenance')
        if operations.payment_terms(record) != json.loads(detail['origin_json']).get('terms_digest'):
            raise operations.AccountBusy('Reserved renewal terms changed after dispatch')
        result = json.loads(operation['result_json'])
        fields = {**(fields or {}), 'renewal_before_state': result.get('before_state'),
                  'renewal_after_state': result.get('after_state'), 'username': operation['username'],
                  'server_id': operation['server_id'], 'renewal_server_id': operation['server_id']}
        if detail['phase'] == 'completed':
            return record.get('renewal_status') == 'applied'
        if not renewal._finish_payment_renewal(payment_id, claim_id, 'applied', payments_file=payments_file, fields=fields, now=now):
            raise operations.AccountBusy('Reserved renewal processing ownership changed')
        if scope != 'main':
            if not reseller.sync_reseller_renewal_reservation(scope.removeprefix('hosted:'), payment_id, 'applied', fields={
                    'before_state': result.get('before_state'), 'after_state': result.get('after_state'),
                    'renewal_server_id': operation['server_id']}):
                raise operations.AccountBusy('Hosted reserved renewal history is missing')
        renewal.mark_cleanup_state_renewed(operation['username'], operation['server_id'])
        _notify(db, ident, scope, record['user_id'])
        operations.event(db, ident, 'reserved_renewal_completed', actor='renewal_finalizer')
        operations.complete(ident)
        return True
