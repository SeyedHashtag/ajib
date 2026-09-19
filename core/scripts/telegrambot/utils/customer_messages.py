"""Neutral localized customer payment notifications."""
TEXT = {
    'withdrawal_review': ('A reward withdrawal is awaiting review in this bot.',
        'درخواست برداشت پاداش در این ربات منتظر بررسی است.',
        'Заявка на вывод вознаграждения ожидает проверки в боте.',
        'Sylag çykarmak haýyşy botda barlaga garaşýar.'),
    'attention': ('Support is reviewing this payment. Do not pay again.',
        'پشتیبانی در حال بررسی این پرداخت است. دوباره پرداخت نکنید.',
        'Поддержка проверяет платёж. Не оплачивайте повторно.',
        'Goldaw tölegi barlaýar. Gaýtadan tölemäň.'),
    'review_saved': ('Review saved. Delivery will continue automatically.',
        'بررسی ذخیره شد. آماده‌سازی سرویس خودکار ادامه می‌یابد.',
        'Решение сохранено. Подготовка продолжится автоматически.',
        'Barlag ýatda saklandy. Hyzmat awtomatik dowam eder.'),
    'review_receipt': (
        'A receipt is awaiting review. Open pending confirmations in this bot.',
        'رسید در انتظار بررسی است. تأییدهای در انتظار را در این ربات باز کنید.',
        'Квитанция ожидает проверки. Откройте ожидающие подтверждения в боте.',
        'Töleg resminamasy garaşýar. Botdaky garaşýan tassyklamalary açyň.'),
    'review_approved': ('Your payment was approved. Service delivery will continue automatically.',
        'پرداخت شما تأیید شد. آماده‌سازی سرویس به‌صورت خودکار ادامه می‌یابد.',
        'Платёж подтверждён. Подготовка подключения продолжится автоматически.',
        'Tölegiňiz tassyklandy. Hyzmat awtomatik taýýarlanar.'),
    'review_rejected': ('Your receipt was rejected. Contact support for help.',
        'رسید شما رد شد. برای راهنمایی با پشتیبانی تماس بگیرید.',
        'Квитанция отклонена. Обратитесь в поддержку.',
        'Töleg resminamaňyz ret edildi. Goldaw gullugyna ýüz tutuň.'),
    'pending_payments': ('Pending payments', 'پرداخت‌های در انتظار', 'Ожидающие платежи', 'Garaşýan tölegler'),
    'continue': ('Continue', 'ادامه', 'Продолжить', 'Dowam etmek'),
}


def message(language, key):
    return TEXT[key][{'en': 0, 'fa': 1, 'ru': 2, 'tk': 3}.get(language, 0)]


def language_for(user_id, scope='main'):
    import json
    from . import database
    namespace = 'user_languages' if scope == 'main' else 'hosted_languages'
    row = database.get_connection().execute(
        'SELECT value_json FROM kv_state WHERE namespace=? AND scope=? AND state_key=?',
        (namespace, scope, str(user_id))).fetchone()
    language = json.loads(row[0]) if row else 'en'
    return language if language in {'en', 'fa', 'ru', 'tk'} else 'en'
