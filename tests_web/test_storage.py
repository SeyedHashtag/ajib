import os
import sqlite3
import pytest


def test_backup_contains_orders_receipts_and_authentication(storage, tmp_path):
    from utils import database, web_store
    from utils.web_auth import create_challenge
    from utils.web_trials import request
    request('123', 'main', 'backup-trial-key-123', 'en')
    challenge, _ = create_challenge('main')
    with database.transaction() as connection:
        connection.execute('INSERT INTO web_receipts VALUES (?,?,?,?,?,?,?)',
                           ('receipt', 'main', 'synthetic-order', '123', 'image/jpeg', b'synthetic-image', 1))
        web_store.enqueue(connection, 'event', 'main', '123', 'synthetic')
    target = tmp_path / 'snapshot' / 'backup.db'
    database.backup_database(target)
    # Exercise restoration by opening the snapshot independently, without replaying network work.
    restored = sqlite3.connect(target)
    try:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert restored.execute('SELECT contents FROM web_receipts').fetchone()[0] == b'synthetic-image'
        assert restored.execute('SELECT id FROM web_challenges').fetchone()[0] == challenge
        assert restored.execute('SELECT status FROM web_trials').fetchone()[0] == 'queued'
        assert restored.execute('SELECT status FROM web_outbox').fetchone()[0] == 'pending'
        assert restored.execute('SELECT COUNT(*) FROM web_audit').fetchone()[0] == 1
    finally:
        restored.close()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX deployment permissions')
def test_shared_database_permissions_preserve_private_backups(storage, monkeypatch, tmp_path):
    import grp
    import stat
    from utils import database
    shared = tmp_path / 'shared-state' / 'ajib.db'
    monkeypatch.setenv('AJIB_DB_PATH', str(shared))
    monkeypatch.setenv('AJIB_DB_SHARED_GROUP', grp.getgrgid(os.getgid()).gr_name)
    database.get_connection()
    assert stat.S_IMODE(shared.parent.stat().st_mode) == 0o2770
    assert stat.S_IMODE(shared.stat().st_mode) == 0o660
    assert shared.stat().st_gid == os.getgid()
    backup = tmp_path / 'private-backup' / 'ajib.db'
    database.backup_database(backup)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != 'posix', reason='Linux CLI recovery drill')
def test_cli_backup_restore_uses_configured_database(storage, tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from utils import database, web_trials
    root = Path(__file__).resolve().parents[1]
    install = tmp_path / 'installation'
    bot_dir = install / 'core/scripts/telegrambot'
    bot_dir.mkdir(parents=True)
    (bot_dir / '.env').write_text('API_TOKEN=123456:synthetic-test-token\n')
    web_trials.request('123', 'main', 'restore-trial-key-123', 'en')
    environment = {**os.environ, 'AJIB_INSTALL_DIR':str(install), 'AJIB_BACKUP_DIR':str(tmp_path/'backups'),
                   'AJIB_SKIP_SERVICE_RESTART':'1', 'AJIB_PYTHON_BIN':sys.executable}
    backup = subprocess.run(['bash',str(root/'core/scripts/ajib/backup.sh')],env=environment,
                            text=True,capture_output=True,check=True).stdout.strip()
    with database.transaction() as connection:
        connection.execute('DELETE FROM web_trials')
    for connection in database._connection_map().values(): connection.close()
    database._connection_map().clear()
    subprocess.run(['bash',str(root/'core/scripts/ajib/restore.sh'),backup],env=environment,
                   text=True,capture_output=True,check=True)
    assert not (bot_dir/'ajib.db').exists()
    assert database.get_connection().execute('SELECT user_id FROM web_trials').fetchone()[0] == '123'
