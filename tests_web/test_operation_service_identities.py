"""Linux bot/API/worker process boundaries sharing a dedicated SQLite directory."""
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() != 0,
                    reason='Requires Linux root to launch isolated synthetic service UIDs')
def test_distinct_service_identities_share_claims_without_holding_sqlite_writer():
    import grp
    shared_group = grp.getgrnam('www-data')
    with tempfile.TemporaryDirectory(prefix='ajib-operation-uids-') as directory:
        root = Path(directory)
        os.chmod(root, 0o755)
        # The Windows checkout may not be traversable by Linux service users.
        # Copy only public Python source, never configuration or snapshots.
        source = root / 'source'
        (source / 'utils').mkdir(parents=True)
        for path in (Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot/utils').glob('*.py'):
            target = source / 'utils' / path.name
            target.write_bytes(path.read_bytes())
            target.chmod(0o644)
        source.chmod(0o755)
        (source / 'utils').chmod(0o755)
        state = root / 'state'
        state.mkdir()
        os.chown(state, 0, shared_group.gr_gid)
        os.chmod(state, 0o2770)
        env = {**os.environ, 'AJIB_SQLITE_ACTIVE': '1', 'AJIB_DB_PATH': str(state / 'ajib.db'),
               'AJIB_BOT_DIR': str(root / 'bot'), 'AJIB_DB_SHARED_GROUP': shared_group.gr_name,
               'AJIB_BOT_ROLE': 'api',
               'PYTHONPATH': str(source)}
        def launch(script, uid, **kwargs):
            return subprocess.Popen([sys.executable, '-c', script], env=env, user=uid,
                group=shared_group.gr_gid, extra_groups=[], umask=0o007,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        bot = launch('''from utils import account_operations as a, database
import sys
def panel():
    assert not database.get_connection().in_transaction
    print('dispatched', flush=True)
    sys.stdin.readline()
    return {'success': True}
a.execute('bot-order', 's1', 'Alice', 'renewal', {}, panel)
''', 65534, stdin=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(bot.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=20), 'Synthetic bot did not reach dispatch'
            line = bot.stdout.readline().strip()
            assert line == 'dispatched', bot.communicate(timeout=10)
            def contender(uid):
                child = launch('''from utils import account_operations as a
try:
    a.execute('competing', 's1', 'alice', 'renewal', {}, lambda: {'success': True})
except a.AccountBusy:
    pass
else:
    raise SystemExit('Cross-service ownership was bypassed')
import os
ident = 'independent-' + str(os.geteuid())
a.execute(ident, 's1', ident, 'update', {}, lambda: {'success': True})
a.complete(ident)
''', uid)
                stdout, stderr = child.communicate(timeout=30)
                assert child.returncode == 0, (stdout, stderr)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(contender, [1, 33]))
            bot.kill()
            bot.communicate(timeout=10)
            contender(33)  # Process death releases flock, but not the durable claim.
            with sqlite3.connect(state / 'ajib.db') as db:
                assert db.execute("SELECT phase FROM account_operation_details WHERE operation_id='bot-order'").fetchone()[0] == 'dispatched'
                assert db.execute("SELECT COUNT(*) FROM account_operation_claims WHERE operation_id='bot-order'").fetchone()[0] == 1
        finally:
            if bot.poll() is None:
                bot.kill()
                bot.communicate(timeout=10)
