"""Public progress and permitted actions; never expose recovery internals."""


def receipt_eligible(record):
    return (record.get('status') == 'waiting_receipt'
            and record.get('payment_method') in {'Card to Card', 'card'}
            and record.get('fulfillment_owner') in {'web', 'bot'}
            and bool(record.get('card_number')) and record.get('price') is not None
            and record.get('converted_amount') is not None
            and record.get('receipt_type', 'regular') == 'regular')


def progress(record, *, writes=True):
    status, renewal = record.get('status'), record.get('renewal_status')
    code = {'creating': 'preparing_payment', 'waiting_receipt': 'awaiting_receipt',
            'pending': 'awaiting_payment', 'pending_approval': 'awaiting_review',
            'approved': 'preparing_service', 'processing': 'preparing_service',
            'paid': 'preparing_service', 'paid_provision_failed': 'needs_attention',
            'uncertain': 'needs_attention', 'completed': 'completed',
            'success': 'completed', 'succeeded': 'completed',
            'cancelled': 'cancelled', 'rejected': 'rejected', 'expired': 'expired'}.get(status, 'needs_attention')
    if record.get('renewal_mode') == 'reserved' and status in {'completed', 'paid', 'succeeded'}:
        code = {'reserved': 'renewal_reserved', 'processing': 'renewal_activating',
                'applied': 'completed', 'attention': 'needs_attention'}.get(renewal, 'needs_attention')
    actions = []
    if writes and receipt_eligible(record):
        actions.extend(['upload_receipt', 'cancel'])
    if writes and status == 'pending' and str(record.get('payment_url', '')).startswith('https://'):
        actions.append('pay')
    if code == 'needs_attention' or (status == 'waiting_receipt' and not receipt_eligible(record)):
        actions.append('contact_support')
    return {'code': code, 'actions': actions,
            'poll': code not in {'completed', 'cancelled', 'rejected', 'expired'}}


def safe_progress(payment_id, record, scope='main', *, writes=True):
    result = progress(record, writes=writes)
    if set(result['actions']) & {'pay', 'upload_receipt', 'cancel'}:
        from .account_operations import assert_obligation_releasable, AccountBusy
        try:
            assert_obligation_releasable(scope, payment_id)
        except AccountBusy:
            return {'code': 'needs_attention', 'actions': ['contact_support'], 'poll': True}
    return result
