"""Telegram authentication shared by the bot and HTTP transport; no TeleBot imports."""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl
from . import database, web_store


class AuthenticationError(ValueError):
    pass


def validate_init_data(raw, bot_token, *, now=None, max_age=300):
    now = int(time.time()) if now is None else now
    if not raw or len(raw) > 16384 or not bot_token:
        raise AuthenticationError("Invalid Telegram authentication")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        if len(pairs) != len(data):
            raise ValueError("Duplicate fields")
        supplied = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("Invalid signature")
        age = now - int(data["auth_date"])
        if age < -30 or age > max_age:
            raise ValueError("Expired authentication")
        user = json.loads(data["user"])
        if type(user["id"]) is not int or not 0 < user["id"] < 2**53:
            raise ValueError("Invalid user")
        return user, web_store.digest(expected)
    except (KeyError, ValueError, TypeError) as error:
        raise AuthenticationError("Invalid or expired Telegram authentication") from error


def create_challenge(scope):
    challenge, browser = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    with database.transaction(operation="web_login_start") as connection:
        connection.execute("DELETE FROM web_challenges WHERE expires_at<?", (int(time.time()) - 3600,))
        connection.execute("""INSERT INTO web_challenges
            (id,browser_hash,scope,expires_at) VALUES (?,?,?,?)""",
            (challenge, web_store.digest(browser), scope, int(time.time()) + 300))
    return challenge, browser


def confirm_challenge(challenge, user_id, scope):
    """Called only after an explicit confirmation in the authenticated bot chat."""
    now = int(time.time())
    with database.transaction(operation="web_login_confirm") as connection:
        result = connection.execute("""UPDATE web_challenges SET user_id=?,confirmed_at=?
            WHERE id=? AND scope=? AND expires_at>? AND confirmed_at IS NULL
            AND consumed_at IS NULL""", (str(user_id), now, challenge, scope, now))
        if result.rowcount:
            web_store.audit(connection, user_id, scope, "login.confirm", challenge)
        return result.rowcount == 1


def _new_session(connection, user_id, scope, lifetime=86400):
    token = secrets.token_urlsafe(32)
    csrf = web_store.digest("csrf:" + token)
    now = int(time.time())
    connection.execute("INSERT INTO web_sessions VALUES (?,?,?,?,?,?,NULL)",
                       (web_store.digest(token), str(user_id), scope,
                        web_store.digest(csrf), now, now + lifetime))
    web_store.audit(connection, user_id, scope, "login.session", "session")
    return token, csrf


def consume_challenge(challenge, browser):
    now = int(time.time())
    with database.transaction(operation="web_login_consume") as connection:
        row = connection.execute("SELECT * FROM web_challenges WHERE id=?", (challenge,)).fetchone()
        if (not row or not browser or row["expires_at"] <= now or row["consumed_at"]
                or not hmac.compare_digest(row["browser_hash"], web_store.digest(browser))):
            raise AuthenticationError("Invalid or expired login")
        if not row["confirmed_at"]:
            return None
        connection.execute("UPDATE web_challenges SET consumed_at=? WHERE id=?", (now, challenge))
        return _new_session(connection, row["user_id"], row["scope"])


def mini_app_session(raw, token, scope):
    user, replay = validate_init_data(raw, token)
    with database.transaction(operation="web_mini_login") as connection:
        now = int(time.time())
        connection.execute("DELETE FROM web_replays WHERE expires_at<?", (now,))
        inserted = connection.execute("INSERT OR IGNORE INTO web_replays VALUES (?,?)", (replay, now + 360))
        if inserted.rowcount != 1:
            raise AuthenticationError("Telegram authentication has already been used")
        return _new_session(connection, user["id"], scope)


def authenticate(token):
    if not token:
        raise AuthenticationError("Sign in with Telegram")
    row = database.get_connection().execute("""SELECT * FROM web_sessions
        WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?""",
        (web_store.digest(token), int(time.time()))).fetchone()
    if not row:
        raise AuthenticationError("Your session has expired")
    return dict(row)


def revoke(token):
    with database.transaction(operation="web_logout") as connection:
        connection.execute("UPDATE web_sessions SET revoked_at=? WHERE token_hash=?",
                           (int(time.time()), web_store.digest(token)))


def handle_login_start(bot, message, scope="main"):
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].startswith("web_"):
        return False
    # Existing installations need no web database until web login is requested.
    web_store.initialize()
    challenge = parts[1][4:]
    row = database.get_connection().execute("""SELECT id FROM web_challenges
        WHERE id=? AND scope=? AND expires_at>? AND consumed_at IS NULL""",
        (challenge, scope, int(time.time()))).fetchone()
    if not row or message.chat.id != message.from_user.id:
        bot.send_message(message.chat.id, "This website sign-in link has expired.")
        return True
    from telebot import types
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Confirm website sign-in", callback_data=f"webauth:{challenge}"))
    bot.send_message(message.chat.id,
                     "Confirm only if you just requested sign-in on the website. "
                     "Do not approve a sign-in link sent by someone else.", reply_markup=markup)
    return True


def register_login_callback(bot, scope="main"):
    @bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("webauth:"))
    def confirm(call):
        if call.message.chat.id != call.from_user.id:
            bot.answer_callback_query(call.id, "Use your private bot chat.", show_alert=True)
            return
        success = confirm_challenge(call.data.split(":", 1)[1], call.from_user.id, scope)
        bot.answer_callback_query(call.id, "Return to your browser." if success else "Sign-in expired.", show_alert=True)
        if success:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
