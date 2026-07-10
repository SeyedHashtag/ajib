import os
import threading
from utils.command import ADMIN_USER_IDS, BACKUP_DIRECTORY, CLI_PATH, bot, is_admin, run_cli_command

BACKUP_LOCK = threading.Lock()

def _get_latest_backup_file():
    try:
        files = [f for f in os.listdir(BACKUP_DIRECTORY) if f.endswith('.zip')]
        files.sort(key=lambda x: os.path.getctime(os.path.join(BACKUP_DIRECTORY, x)), reverse=True)
        latest_backup_file = files[0] if files else None
    except Exception as e:
        return None, f"Failed to locate the backup file: {str(e)}"

    if not latest_backup_file:
        return None, "No backup file found after the backup process."

    return os.path.join(BACKUP_DIRECTORY, latest_backup_file), latest_backup_file

def _run_backup_command():
    backup_command = f"python3 {CLI_PATH} backup-ajib"
    result = run_cli_command(backup_command)
    if "Error" in result:
        return None, result

    reported_path = result.splitlines()[-1].strip() if result else ""
    if reported_path and os.path.isfile(reported_path):
        return (reported_path, os.path.basename(reported_path)), None

    backup_file_path, latest_backup_file_or_error = _get_latest_backup_file()
    if not backup_file_path:
        return None, latest_backup_file_or_error

    return (backup_file_path, latest_backup_file_or_error), None

def _send_backup_file(chat_id, backup_file_path, latest_backup_file, caption_prefix="Backup completed"):
    with open(backup_file_path, 'rb') as f:
        bot.send_document(chat_id, f, caption=f"{caption_prefix}: {latest_backup_file}")

def run_backup_and_send(chat_id, start_message="Starting backup. This may take a few moments...", caption_prefix="Backup completed"):
    bot.send_message(chat_id, start_message)
    bot.send_chat_action(chat_id, 'typing')

    with BACKUP_LOCK:
        result, error = _run_backup_command()

    if error:
        bot.send_message(chat_id, f"Backup failed: {error}")
        return

    backup_file_path, latest_backup_file = result
    _send_backup_file(chat_id, backup_file_path, latest_backup_file, caption_prefix=caption_prefix)

def run_backup_and_send_to_admins():
    with BACKUP_LOCK:
        result, error = _run_backup_command()

    if error:
        for admin_id in ADMIN_USER_IDS:
            bot.send_message(admin_id, f"Automated backup failed: {error}")
        return

    backup_file_path, latest_backup_file = result
    for admin_id in ADMIN_USER_IDS:
        bot.send_message(admin_id, "Automated backup completed.")
        _send_backup_file(admin_id, backup_file_path, latest_backup_file, caption_prefix="Automated backup completed")


@bot.message_handler(func=lambda message: is_admin(message.from_user.id) and message.text == '💾 Backup Bot')
def backup_bot(message):
    run_backup_and_send(message.chat.id)
