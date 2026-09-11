"""Catalog loading shared by Telegram and HTTP without importing handlers."""
import json
from pathlib import Path

DEFAULT_PLANS = {
    "40": {"price": 1.20, "days": 30},
    "60": {"price": 1.50, "days": 30},
    "100": {"price": 2.00, "days": 30},
}


def load_catalog(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {key: dict(value) for key, value in DEFAULT_PLANS.items()}
