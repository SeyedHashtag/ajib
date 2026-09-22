import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def backup(tmp_path, monkeypatch):
    admins = {1, 2}
    sent = []
    bot = SimpleNamespace(message_handler=lambda **kw: lambda f: f,
                          send_message=lambda *a, **kw: None, send_chat_action=lambda *a: None)
    bot.send_document = lambda user, stream, **kw: sent.append((user, stream.read(), kw))
    command = ModuleType('utils.command')
    for key, value in dict(AJIB_PYTHON='python', ADMIN_USER_IDS=[1, 2], BACKUP_DIRECTORY=str(tmp_path),
                           CLI_PATH='synthetic.py', bot=bot, is_admin=lambda user: user in admins,
                           run_cli_command=lambda args: '').items():
        setattr(command, key, value)
    common = ModuleType('utils.common')
    common.admin_action_text = lambda key: key
    monkeypatch.setitem(sys.modules, 'utils.command', command)
    monkeypatch.setitem(sys.modules, 'utils.common', common)
    path = Path(__file__).resolve().parents[1] / 'core/scripts/telegrambot/utils/backup.py'
    spec = importlib.util.spec_from_file_location('operator_backup_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / 'internal-backup.zip'
    archive.write_bytes(b'complete private archive')
    module._run_backup_command = lambda: ((str(archive), archive.name), None)
    return module, bot, admins, sent


def test_private_admin_backup_is_intact_with_neutral_filename(backup):
    module, bot, admins, sent = backup
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1, type='private'))
    module.backup_bot(message)
    assert sent[0][1] == b'complete private archive'
    assert sent[0][2]['visible_file_name'] == 'service-backup.zip'
    assert sent[0][2]['_operator_document']


@pytest.mark.parametrize('user,chat,kind', [(1, -1, 'group'), (3, 3, 'private'), (1, 2, 'private')])
def test_backup_rejects_non_private_or_non_admin_delivery(backup, user, chat, kind):
    module, bot, admins, sent = backup
    module._run_backup_command = lambda: pytest.fail('unauthorized backup generation')
    module.backup_bot(SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=chat, type=kind)))
    assert not sent


def test_scheduled_delivery_continues_after_one_recipient_fails(backup):
    module, bot, admins, sent = backup
    original = bot.send_document
    def send(user, stream, **kw):
        if user == 1:
            raise RuntimeError('synthetic delivery failure')
        return original(user, stream, **kw)
    bot.send_document = send
    module.run_backup_and_send_to_admins()
    assert [item[0] for item in sent] == [2]
    assert module.BACKUP_LOCK.acquire(blocking=False)
    module.BACKUP_LOCK.release()


def test_revoked_admin_receives_no_file_after_backup_finishes(backup):
    module, bot, admins, sent = backup
    original = module._run_backup_command
    def generate():
        admins.clear()
        return original()
    module._run_backup_command = generate
    module.run_backup_and_send(1)
    assert not sent
