"""Transport-independent legacy customer identity matching."""
import re


def user_config_patterns(user_id):
    # Never interpolate an unchecked identifier into a regular expression.
    key = str(user_id)
    if not key.isdigit():
        raise ValueError("A numeric Telegram user ID is required")
    return (
        (re.compile(rf"^s{key}[a-z]*$", re.IGNORECASE),
         re.compile(rf"^{key}t"), re.compile(rf"^sell{key}t")),
        (re.compile(rf"^t{key}[a-z]*$", re.IGNORECASE), re.compile(rf"^test{key}t")),
    )


def username_belongs_to_user(username, user_id):
    paid, test = user_config_patterns(user_id)
    return any(pattern.match(str(username or "")) for pattern in paid + test)
