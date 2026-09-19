"""Public content policy; deployment identifiers stay in private infrastructure."""
import html
import base64
import os
import unicodedata
from functools import wraps
from urllib.parse import unquote

PRIVATE_NAMES = ('ajib', 'عجیب')
TITLES = {'en': 'Your connections', 'fa': 'اتصال‌های شما', 'ru': 'Ваши подключения', 'tk': 'Siziň birikmeleriňiz'}
UNAVAILABLE = 'This content is temporarily unavailable. Please contact support.'
UNAVAILABLE_TEXTS = {'en': UNAVAILABLE, 'fa': 'این محتوا موقتاً در دسترس نیست. با پشتیبانی تماس بگیرید.',
                     'ru': 'Этот материал временно недоступен. Обратитесь в поддержку.',
                     'tk': 'Bu maglumat wagtlaýyn elýeterli däl. Goldaw bilen habarlaşyň.'}


class PublicContentUnavailable(ValueError):
    def __init__(self):
        super().__init__(UNAVAILABLE)


def public_error(language='en'):
    return UNAVAILABLE_TEXTS.get(language, UNAVAILABLE)


def contains_private(value):
    if callable(getattr(value, 'to_dict', None)):
        value = value.to_dict()
    if isinstance(value, dict):
        return any(contains_private(key) or contains_private(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_private(item) for item in value)
    if not isinstance(value, str):
        return False
    text = unicodedata.normalize('NFKC', html.unescape(unquote(value))).casefold()
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    if any(name in text for name in PRIVATE_NAMES):
        return True
    if value.startswith('vmess://'):
        try:
            payload = value[8:].split('#', 1)[0]
            decoded = base64.b64decode(payload + '=' * (-len(payload) % 4)).decode('utf-8')
            return contains_private(decoded)
        except (ValueError, UnicodeError):
            pass
    return False


def require_public(value):
    if contains_private(value):
        raise PublicContentUnavailable()
    return value


def make_qr(data, *, encoder=None, **kwargs):
    """Check the actual payload before encoding it into an opaque image."""
    import qrcode
    return (encoder or qrcode.make)(require_public(data), **kwargs)


def check_attachment(value):
    """Validate text exports without changing the stream or its functional data."""
    if isinstance(value, (list, tuple)):
        for item in value:
            check_attachment(item)
    elif isinstance(value, dict):
        for item in value.values():
            check_attachment(item)
    elif callable(getattr(value, 'to_dict', None)):
        media = getattr(value, 'media', None)
        if media is not value:
            check_attachment(media)
    filename = getattr(value, 'name', None)
    if isinstance(filename, str):
        basename = os.path.basename(filename.replace('\\', '/'))
        require_public(basename)
        # Logs and state archives belong to the restricted operator interface.
        if basename.lower().endswith(('.log', '.zip', '.db', '.sqlite', '.gz')):
            raise PublicContentUnavailable()
    if callable(getattr(value, 'read', None)):
        if not callable(getattr(value, 'seekable', None)) or not value.seekable():
            raise PublicContentUnavailable()
        offset = value.tell()
        try:
            data = value.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise PublicContentUnavailable()
            if isinstance(data, bytes):
                # Images are verified at generation/upload, textual exports here.
                try:
                    data = data.decode('utf-8')
                except UnicodeError:
                    data = None
            require_public(data)
        finally:
            value.seek(offset)


def install_telegram_guard(bot):
    if getattr(bot, '_public_content_guard', False):
        return bot
    for name in ('send_message', 'reply_to', 'edit_message_text', 'edit_message_caption',
                 'edit_message_reply_markup', 'answer_callback_query', 'send_photo',
                 'send_document', 'send_media_group', 'send_video', 'send_audio'):
        original = getattr(bot, name, None)
        if not callable(original):
            continue
        @wraps(original)
        def guarded(*args, __original=original, __name=name, **kwargs):
            # reply_to's first argument is an INCOMING message, not a payload.
            # Do not reject a neutral reply just because the user typed a private name.
            outgoing = args[1:] if __name == 'reply_to' else args
            for value in (*outgoing, *(v for k, v in kwargs.items() if k != 'message')):
                require_public(value)
                check_attachment(value)
            return __original(*args, **kwargs)
        setattr(bot, name, guarded)
    bot._public_content_guard = True
    return bot
