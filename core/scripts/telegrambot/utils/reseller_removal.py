"""Banned-reseller removal with durable accounting intent and retained claims."""
import copy
import hashlib
import json

from . import account_operations as operations, database, state_store
from .account_rename import signature


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def current(owner, db=None):
    db = db or database.get_connection()
    row = db.execute('SELECT payload_json FROM resellers WHERE reseller_id=?', (str(owner),)).fetchone()
    return json.loads(row[0]) if row else None


def finalize(operation_id):
    from . import reseller as rules, web_store
    operation = operations.existing(operation_id)
    request = json.loads(operation['request_json'])
    owner = request['reseller_id']
    with database.transaction(operation='banned_removal_finalization') as db:
        if operations.details(operation_id)['phase'] == 'completed':
            return json.loads(db.execute("SELECT result_json FROM account_operation_steps WHERE operation_id=? AND step_id='accounting'", (operation_id,)).fetchone()[0])
        raw = current(owner, db)
        if _digest(raw) != request['reseller_digest']:
            raise operations.AccountBusy('Reseller ownership or debt changed after dispatch; financial review required')
        record = rules._ensure_reseller_defaults(copy.deepcopy(raw))
        had_total_paid = 'total_paid' in raw
        deleted, missing = [], []
        for account in request['accounts']:
            step = db.execute('SELECT phase,result_json FROM account_operation_steps WHERE operation_id=? AND step_id=?',
                              (operation_id, account['step_id'])).fetchone()
            if not step or step['phase'] != 'verified':
                raise operations.AccountBusy('A removal outcome is not verified')
            outcome = json.loads(step['result_json'])
            candidate = account['candidate']
            (missing if outcome.get('already_missing') else deleted).append(candidate)
            status = rules.REMOVAL_STATUS_ALREADY_MISSING if outcome.get('already_missing') else rules.REMOVAL_STATUS_DELETED_FROM_VPN
            if request.get('mode') != 'debt':
                record['configs'][candidate['config_index']] = rules._mark_config_removed(record['configs'][candidate['config_index']], status)
        removed_value = sum(rules._safe_float(account['candidate'].get('price', 0.0)) for account in request['accounts'])
        previous_debt = rules._safe_float(record.get('debt', 0.0))
        target_debt = max(0.0, previous_debt - removed_value)
        rules._ensure_debt_charge_ledger(record)
        if request.get('mode') == 'debt':
            removed_value = 0.0
            charges = {str(charge['id']): charge for charge in record['debt_charges']}
            for account in request['accounts']:
                candidate = account['candidate']
                for calculation in account['calculations']:
                    charge = charges[calculation['charge_id']]
                    amount = calculation['writeoff']
                    before = rules._charge_outstanding(charge)
                    if before < amount:
                        raise operations.AccountBusy('Debt proration no longer matches the recorded charge')
                    if amount > 0:
                        charge['outstanding_amount'] = round(max(0.0, before - amount), 2)
                        charge.setdefault('writeoffs', []).append({'kind': 'unused_service_proration', 'amount': amount,
                            'created_at': rules._now_str(), **calculation})
                        removed_value += amount
                config = record['configs'][candidate['config_index']]
                config.update(removed_from_vpn=True, removal_reason=rules.REMOVAL_REASON_RESELLER_DEBT_DEFAULT,
                              removal_note='Removed after reseller debt default deadline', removed_at=rules._now_str(),
                              removed_cleanup_status=rules.REMOVAL_STATUS_DELETED_FROM_VPN,
                              debt_proration=account['calculations'], debt_removal_api_result={'verified_missing': True})
            target_debt = round(max(0.0, previous_debt - removed_value), 2)
            if removed_value > 0:
                rules._record_debt_allocation(record, removed_value, [], 'unused_service_proration',
                                              reference_id=f"proration:{record.get('debt_cycle_id')}")
            record['debt_services_removed_at'] = rules._now_str()
            record['debt_service_remove_due'] = False
            record.setdefault('debt_service_actions', []).append({'action': 'remove', 'timestamp': rules._now_str(),
                'changed': len(request['accounts']), 'failed': 0, 'manual_review': [], 'writeoff': round(removed_value, 2)})
            record['debt_service_actions'] = record['debt_service_actions'][-50:]
        else:
            rules._allocate_debt_fifo(record, max(0.0, previous_debt - target_debt), kind='banned_cleanup_writeoff')
        record['debt'] = target_debt
        if request.get('mode') == 'debt' and rules._is_debt_fully_settled(target_debt):
            record['debt_since'] = None
            record = rules._restore_suspended_if_debt_fully_settled(record)
            record = rules._mark_policy_restore_due_if_needed(record)
            record = rules._finish_debt_cycle(record)
        if not had_total_paid:
            record.pop('total_paid', None)
            record.pop('trust_limit', None)
        record = rules._ensure_reseller_defaults(record)
        state_store._save_reseller_record(db, owner, record)
        result = {'success': True, 'deleted': deleted, 'already_missing': missing, 'failed': [],
                  'removed_count': len(request['accounts']), 'tagged_count': len(request['accounts']),
                  'removed_value': removed_value, 'remaining_debt': rules._safe_float(record.get('debt', 0.0)),
                  'remaining_configs': len(record.get('configs', [])), 'last_payment_at': record.get('last_payment_at')}
        if request.get('mode') == 'debt':
            result.update(changed=deleted, completed=len(request['accounts']), stage_completed=True,
                          writeoff=round(removed_value, 2), manual_review=[])
        web_store.initialize()
        language_row = db.execute("SELECT value_json FROM kv_state WHERE namespace='user_languages' AND scope='main' AND state_key=?", (owner,)).fetchone()
        language = json.loads(language_row[0]) if language_row else 'en'
        messages = {'en': 'Account cleanup has completed. Open your account history for the current status.',
                    'fa': 'پاک‌سازی حساب‌ها انجام شد. برای وضعیت فعلی، تاریخچه حساب‌ها را باز کنید.',
                    'ru': 'Очистка аккаунтов завершена. Откройте историю аккаунтов.',
                    'tk': 'Hasaplary arassalamak tamamlandy. Hasaplaryň taryhyny açyň.'}
        web_store.enqueue(db, 'banned-removal:' + operation_id, 'main', owner,
                          messages.get(language, messages['en']))
        db.execute("INSERT INTO account_operation_steps VALUES (?, 'accounting', 'verified', ?, ?, strftime('%s','now'))",
                   (operation_id, json.dumps({'reseller_digest': request['reseller_digest']}), json.dumps(result)))
        operations.event(db, operation_id, 'banned_removal_accounting_committed', actor=json.loads(operations.details(operation_id)['origin_json'])['actor'])
        operations.complete(operation_id)
        return result


def inspect(operation_id, panels):
    operation = operations.existing(operation_id)
    request = json.loads(operation['request_json'])
    evidence = {'reseller_digest': _digest(current(request['reseller_id'])), 'accounts': []}
    for account in request['accounts']:
        client = panels.get_client(account['server_id'])
        result = client.get_user_result(account['username']) if client else {'status': 'unavailable'}
        evidence['accounts'].append({'step_id': account['step_id'], 'status': result.get('status')})
    evidence['success'] = (evidence['reseller_digest'] == request['reseller_digest']
                           and all(account['status'] == 'missing' for account in evidence['accounts']))
    return evidence


def cleanup_banned(owner, panels, *, actor='operator', _mode='banned'):
    from . import reseller as rules
    owner = str(owner)
    raw = current(owner)
    if not raw or (_mode == 'banned' and raw.get('status') != 'banned'):
        return False, {'reason': 'Cleanup is only available for banned resellers'}
    normalized = rules._ensure_reseller_defaults(copy.deepcopy(raw))
    if _mode == 'debt':
        if normalized.get('status') == 'banned' or rules._is_debt_fully_settled(normalized.get('debt', 0)):
            return False, {'reason': 'Debt removal is no longer eligible'}
        started = rules._parse_time(normalized.get('debt_since'))
        hours = rules.get_reseller_debt_deadlines(normalized)['removal_hours']
        if started is None or (rules.utc_now() - started).total_seconds() < hours * 3600:
            return False, {'reason': 'deadline_not_due'}
        rules._ensure_debt_charge_ledger(normalized)
        candidates, manual_review = rules.get_reseller_debt_service_candidates(normalized)
        if manual_review:
            return False, {'reason': 'Debt charge provenance requires review', 'manual_review': manual_review}
    else:
        candidates = rules.get_banned_reseller_cleanup_candidates(normalized)
    if not candidates:
        return True, {'deleted': [], 'already_missing': [], 'failed': [], 'removed_count': 0, 'tagged_count': 0,
                      'removed_value': 0.0, 'remaining_debt': normalized.get('debt', 0.0), 'remaining_configs': len(normalized.get('configs', []))}
    ident = ('debt-removal:' if _mode == 'debt' else 'banned-cleanup:') + owner + ':' + _digest(raw)
    previous = operations.existing(ident)
    if previous:
        request = json.loads(previous['request_json'])
    else:
        accounts = []
        for index, candidate in enumerate(candidates):
            operations.assert_no_pending_obligations(str(candidate.get('server_id') or 'primary'), candidate['username'])
            calculations = []
            if _mode == 'debt':
                config = normalized['configs'][candidate['config_index']]
                snapshot = config.get('debt_policy_hold_snapshot')
                sources = config.get('debt_policy_hold_sources') or rules._debt_service_charge_sources(config)
                charges = {str(charge.get('id')): charge for charge in normalized.get('debt_charges', [])}
                for charge_id in candidate.get('charge_ids', []):
                    calculation = rules._prorated_collectible(charges.get(str(charge_id)), sources.get(str(charge_id)), snapshot)
                    if calculation is None:
                        return False, {'reason': 'proration_unsafe', 'manual_review': [charge_id]}
                    calculations.append({'charge_id': str(charge_id), **calculation})
                if not calculations:
                    return False, {'reason': 'proration_unsafe'}
            client, user, lookup = panels.resolve_unique_user(candidate['username'], preferred_server_id=candidate.get('server_id'),
                                                              allow_exact_on_partial=False, force_refresh=True)
            if not lookup.get('uniqueness_verified') or lookup.get('status') not in {'found', 'missing'}:
                return False, {'reason': 'Account identity requires investigation'}
            server = str(getattr(client, 'server_id', None) or candidate.get('server_id') or 'primary')
            accounts.append({'step_id': 'delete:' + str(index), 'candidate': candidate, 'server_id': server,
                             'username': candidate['username'], 'generation': signature(user) if user else None,
                             'calculations': calculations})
        request = {'reseller_id': owner, 'reseller_digest': _digest(raw), 'accounts': accounts, 'mode': _mode}
    resources = [(account['server_id'], account['username']) for account in request['accounts']]
    resources.append(('reseller', owner))
    with operations.serialize_many(resources):
        def dispatch():
            if _digest(current(owner)) != request['reseller_digest']:
                raise operations.AccountBusy('Reseller eligibility changed before dispatch')
            for account in request['accounts']:
                def action(account=account):
                    operations.assert_no_pending_obligations(account['server_id'], account['username'])
                    client = panels.get_client(account['server_id'])
                    if client is None:
                        raise operations.AccountBusy('Recorded panel is unavailable')
                    live = client.get_user_result(account['username'])
                    if live.get('status') == 'missing':
                        return {'success': True, 'already_missing': True}
                    if live.get('status') != 'found' or signature(live['data']) != account['generation']:
                        raise operations.AccountBusy('Account generation changed')
                    if _digest(current(owner)) != request['reseller_digest']:
                        raise operations.AccountBusy('Reseller debt or ownership changed before dispatch')
                    client.delete_user(account['username'])
                    return {'success': client.get_user_result(account['username']).get('status') == 'missing'}
                operations.step(ident, account['step_id'], account, action)
            return {'success': True}
        kind = 'debt_removal' if _mode == 'debt' else 'banned_cleanup'
        operations.execute(ident, 'reseller', owner, kind, request, dispatch,
                           origin={'type': kind, 'scope': 'reseller:' + owner, 'id': ident,
                                   'reseller_id': owner, 'actor': str(actor)}, resources=resources)
        return True, finalize(ident)
