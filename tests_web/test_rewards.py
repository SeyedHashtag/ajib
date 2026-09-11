from concurrent.futures import ThreadPoolExecutor


def test_withdrawal_retries_preserve_one_shared_reservation(client, login):
    from utils import referral, database
    login(client)
    assert referral.credit_manual_referral_reward(123, 5, 'synthetic-reward')
    response = client.put('/api/v1/referrals/wallet', json={'address': 'synthetic-wallet-address'},
                          headers={'Idempotency-Key': 'wallet-key-123456'})
    assert response.status_code == 200, response.text
    headers = {'Idempotency-Key': 'withdrawal-key-12345'}
    first = client.post('/api/v1/referrals/withdrawals', headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()['amount'] == 5
    assert client.post('/api/v1/referrals/withdrawals', headers=headers).json() == first.json()
    assert client.post('/api/v1/referrals/withdrawals', headers={'Idempotency-Key': 'another-withdrawal-key'}).status_code == 409
    summary = client.get('/api/v1/referrals').json()
    assert summary['available_balance_cents'] == 0
    assert len(summary['withdrawals']) == 1
    assert summary['withdrawals'][0]['wallet'] == 'synthetic-wallet-address'
    # The existing bot sees the same pending liability.
    assert referral.get_pending_withdrawal_requests()[0]['id'] == first.json()['id']
    assert database.get_connection().execute('SELECT COUNT(*) FROM web_outbox').fetchone()[0] == 1
    login(client, 999)
    assert client.get('/api/v1/referrals').json()['withdrawals'] == []


def test_referral_code_attribution_and_payload_conflict(client, login):
    from utils import referral
    login(client)
    first = client.post('/api/v1/referrals/code', headers={'Idempotency-Key': 'referral-code-key-001'})
    assert first.status_code == 200, first.text
    code = first.json()['code']
    login(client, 999)
    response = client.post('/api/v1/referrals/attribution', json={'code': code},
                           headers={'Idempotency-Key': 'attribute-referral-key'})
    assert response.status_code == 200, response.text
    assert str(referral.get_referral_attribution(999)['referrer_user_id']) == '123'
    assert client.post('/api/v1/referrals/attribution', json={'code': 'different'},
                       headers={'Idempotency-Key': 'attribute-referral-key'}).status_code == 409


def test_simultaneous_bot_web_withdrawals_reserve_once(storage):
    from utils import referral, database
    from utils.web_rewards import perform
    from utils.web_services import ServiceError
    referral.credit_manual_referral_reward(123, 5, 'parallel-reward')
    referral.set_wallet_address(123, 'synthetic-wallet-address')
    def withdraw(interface):
        try:
            if interface == 'bot':
                return referral.process_withdrawal_request(123)[0]
            try:
                perform(123, 'main', 'withdrawal', 'parallel-withdrawal-key', {})
                return True
            except ServiceError:
                return False
        finally:
            for connection in database._connection_map().values():
                connection.close()
            database._connection_map().clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(withdraw, ['bot', 'web'])) == 1
    assert len(referral.get_pending_withdrawal_requests()) == 1


def test_reward_actions_require_write_gate_and_csrf(client, login):
    login(client)
    client.headers.pop('X-CSRF-Token')
    assert client.post('/api/v1/referrals/code', headers={'Idempotency-Key': 'referral-csrf-key'}).status_code == 403
