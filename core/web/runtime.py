"""Explicit runtime initialization; importing the API must not start Telegram."""
import os
import sys
from pathlib import Path


def configure():
    source = Path(__file__).resolve().parents[1] / "scripts" / "telegrambot"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    os.environ.setdefault("AJIB_BOT_ROLE", "api")
    if os.environ["AJIB_BOT_ROLE"] not in {"api", "web-worker"}:
        raise RuntimeError("Run the web API in its own api/web-worker process")
    # Load secrets only at explicit application startup, never from the frontend.
    from dotenv import load_dotenv
    load_dotenv(os.getenv("AJIB_ENV_FILE") or Path(os.getenv("AJIB_BOT_DIR", str(source))) / ".env")
