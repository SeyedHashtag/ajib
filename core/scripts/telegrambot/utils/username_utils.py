import json
import os

try:
    from .time_utils import utc_now
except ImportError:  # Standalone diagnostics/tests.
    from time_utils import utc_now


BOT_STATE_DIR = os.getenv("AJIB_BOT_DIR", "/etc/ajib/core/scripts/telegrambot")
DEFAULT_RECORDED_USERNAME_PATHS = (
    os.path.join(BOT_STATE_DIR, "payments.json"),
    os.path.join(BOT_STATE_DIR, "test_configs.json"),
    os.path.join(BOT_STATE_DIR, "resellers.json"),
    os.path.join(BOT_STATE_DIR, "expired_user_cleanup.json"),
)
RECORDED_USERNAME_FIELDS = {
    "username",
    "renewal_username",
    "renew_username",
    "provisioned_username",
}


class RecordedUsernameLoadError(RuntimeError):
    """Raised when persisted username history cannot be read safely."""


def extract_recorded_usernames(records):
    """Collect VPN account usernames from nested persisted bot records."""
    usernames = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in RECORDED_USERNAME_FIELDS and isinstance(item, str):
                    username = item.strip()
                    if username:
                        usernames.add(username)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(records)
    return usernames


def load_recorded_usernames(record_paths=None, extra_paths=None, scopes=None):
    """Load every recorded VPN username, failing closed on damaged state."""
    extra_paths = list(extra_paths or ())
    if record_paths is None:
        try:
            from utils import state_store
        except ImportError:
            state_store = None
        if state_store is not None and os.getenv("AJIB_SQLITE_ACTIVE") == "1":
            query_scopes = list(scopes or ("main",))
            remaining_extra_paths = []
            for raw_path in extra_paths:
                descriptor = state_store.describe_path(raw_path)
                if descriptor is not None and descriptor.kind == "payments":
                    if descriptor.scope not in query_scopes:
                        query_scopes.append(descriptor.scope)
                else:
                    remaining_extra_paths.append(raw_path)
            try:
                usernames = set(state_store.query_recorded_usernames(query_scopes))
            except Exception as exc:
                raise RecordedUsernameLoadError(
                    f"Unable to read recorded username history from SQLite: {exc}"
                ) from exc
            if not remaining_extra_paths:
                return usernames
            paths = remaining_extra_paths
        else:
            usernames = set()
            paths = list(DEFAULT_RECORDED_USERNAME_PATHS)
            paths.extend(extra_paths)
    else:
        usernames = set()
        paths = list(record_paths)
        paths.extend(extra_paths)

    seen_paths = set()
    for raw_path in paths:
        path = os.fspath(raw_path)
        normalized_path = os.path.abspath(path)
        if normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)

        try:
            with open(path, "r", encoding="utf-8") as handle:
                records = json.load(handle)
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise RecordedUsernameLoadError(
                f"Unable to read recorded username history from {path}: {exc}"
            ) from exc

        if not isinstance(records, dict):
            raise RecordedUsernameLoadError(
                f"Recorded username history in {path} must contain a JSON object"
            )
        usernames.update(extract_recorded_usernames(records))

    return usernames


def format_username_timestamp():
    """Return username metadata timestamp in YYMMDDHHMMSS format."""
    return utc_now().strftime("%y%m%d%H%M%S")


def format_readable_timestamp():
    """Return a human-readable timestamp with an explicit UTC label."""
    return utc_now().strftime("%Y-%m-%d %H:%M UTC")


def extract_existing_usernames(users_payload):
    """Collect usernames from API responses (dict or list forms)."""
    usernames = set()
    if isinstance(users_payload, dict):
        for username in users_payload.keys():
            if isinstance(username, str) and username:
                usernames.add(username)
    elif isinstance(users_payload, list):
        for item in users_payload:
            if not isinstance(item, dict):
                continue
            username = item.get("username")
            if isinstance(username, str) and username:
                usernames.add(username)
    return usernames


def _alpha_suffix(index):
    """Convert 0-based index to suffix: 0->'', 1->a, 26->z, 27->aa ..."""
    if index <= 0:
        return ""
    chars = []
    value = index
    while value > 0:
        value -= 1
        chars.append(chr(ord("a") + (value % 26)))
        value //= 26
    return "".join(reversed(chars))


def allocate_username(prefix, telegram_id, existing_usernames):
    """Allocate first available username using alphabetical collision suffixes."""
    base = f"{prefix}{telegram_id}"
    existing_lower = {
        username.lower()
        for username in existing_usernames
        if isinstance(username, str) and username
    }

    index = 0
    while True:
        candidate = f"{base}{_alpha_suffix(index)}"
        if candidate.lower() not in existing_lower:
            return candidate
        index += 1


def build_user_note(
    username,
    traffic_limit,
    expiration_days,
    password="",
    creation_date="",
    unlimited=False,
    note_text="",
    timestamp=None,
):
    """Build note as a human-readable formatted string."""
    ts = timestamp or format_readable_timestamp()
    note_str = str(note_text or "N/A")
    return f"📅 {ts} | 📝 {note_str} | ✏️ "
