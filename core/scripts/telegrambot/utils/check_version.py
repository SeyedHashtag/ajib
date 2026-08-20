import time
import re
from utils.command import AJIB_PYTHON, ADMIN_USER_IDS, CLI_PATH, bot, run_cli_command

def check_version():
    result = run_cli_command([AJIB_PYTHON, CLI_PATH, "version", "--check"])
    if result.startswith("Error:"):
        error_message = f"Error checking version: {result}"
        print(error_message)
        notify_admins(error_message)
        return

    bot_version = re.search(r'Bot Version: (\d+\.\d+\.\d+)', result)
    latest_version = re.search(r'Latest Version: (\d+\.\d+\.\d+)', result)

    if bot_version and latest_version and bot_version.group(1) != latest_version.group(1):
        notify_admins(f"🔔 New version available!\n\n{result}")

def notify_admins(message):
    for admin_id in ADMIN_USER_IDS:
        try:
            bot.send_message(admin_id, message)
        except Exception as e:
            print(f"Failed to notify admin {admin_id}: {str(e)}")

def version_monitoring():
    while True:
        check_version()
        time.sleep(86400)
