import os

from dotenv import load_dotenv


TELEGRAM_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))


def get_exchange_rate():
    """Return the live USD-to-Toman rate used by the primary Telegram bot."""
    load_dotenv(TELEGRAM_ENV_PATH, override=True)
    try:
        return float(os.getenv("EXCHANGE_RATE", "1"))
    except (TypeError, ValueError):
        return 1.0
