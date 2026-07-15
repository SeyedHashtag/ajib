import telebot
import subprocess
import json
import os
import shlex
from dotenv import load_dotenv
from utils.bot_logging import configure_logging, get_telegram_worker_count, instrument_bot
from utils.telegram_safe import install_safe_telegram_methods

TELEGRAM_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
load_dotenv(TELEGRAM_ENV_PATH)
configure_logging()

API_TOKEN = os.getenv('API_TOKEN')
ADMIN_USER_IDS = json.loads(os.getenv('ADMIN_USER_IDS'))
CLI_PATH = '/etc/ajib/core/cli.py'
AJIB_PYTHON = os.getenv('AJIB_PYTHON', '/etc/ajib/ajib_venv/bin/python')
BACKUP_DIRECTORY = '/opt/ajib-backups'
bot = telebot.TeleBot(API_TOKEN, threaded=True, num_threads=get_telegram_worker_count())
install_safe_telegram_methods(bot)
instrument_bot(bot)

def run_cli_command(command):
    args = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f'Error: {e.stdout or str(e)}'.strip()
    except OSError as e:
        executable = args[0] if args else 'command'
        return f'Error: Unable to run {executable}: {e}'

def is_admin(user_id):
    return user_id in ADMIN_USER_IDS
