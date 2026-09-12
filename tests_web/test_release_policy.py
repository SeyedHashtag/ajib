def test_live_pause_applies_without_api_restart(client, login):
    login(client)
    from utils import database, web_release
    from core.web.settings import Settings
    with database.transaction() as db:
        current = web_release.policy(Settings(writes_enabled=True, public_portal=True), db)
        web_release.save(db, {**current, 'accept_writes': False})
    assert client.get('/api/v1/me').json()['writes_enabled'] is False
    assert client.put('/api/v1/me/language', json={'language': 'ru'}).status_code == 503
    assert web_release.policy(Settings())['process_existing'] is True


def test_pilot_removal_and_admin_only_apply_to_existing_sessions(client, login):
    login(client)
    from utils import database, web_release
    from core.web.settings import Settings
    with database.transaction() as db:
        current = web_release.policy(Settings(public_portal=True), db)
        web_release.save(db, {**current, 'access': 'pilot', 'pilot_users': ['123']})
    assert client.get('/api/v1/me').status_code == 200
    with database.transaction() as db:
        web_release.save(db, {**web_release.policy(Settings(), db), 'access': 'admin'})
    assert client.get('/api/v1/me').status_code == 403
