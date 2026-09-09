"""Shared reseller/customer wording, independent of either Telegram runtime."""

from datetime import timedelta
from utils.reseller_journey import journey_text, recovery_text

TEXT = {
    'en': {
        'single': 'Single user', 'many': 'Unlimited users', 'unknown': 'User limit unavailable',
        'single_note': '👤 Single-user configuration: for one user only.',
        'many_note': '👥 Unlimited-user configuration (traffic and validity limits still apply).',
        'recent': 'Paid in the last 90 days: ${amount}',
        'settlement_window': '{days} days ({hours} hours) to settle',
        'settlement_next': 'Next debt cycle at level {level}: {window}.',
        'settlement_current': 'Current debt cycle, locked at level {level}: {window}.',
        'settlement_rules': 'All deadlines run from the original debt start. The level and deadlines are locked for each debt cycle; level changes apply to the next cycle. Daily reminders still begin after 24 hours.',
        'level_down': 'Your level is now {level}/{count} because older payments left the 90-day window. Current discount: {discount}%. Base credit limit: ${limit}.',
        'credit': 'Full credit', 'half_credit': 'Half credit', 'prepaid_only': 'Prepaid only',
        'selling': 'Selling available', 'paused': 'Selling suspended', 'banned': 'Access banned by an administrator',
        'summary': '💳 Credit status\nPrepaid available: ${available} · Reserved: ${reserved}\nDebt: ${debt}\nBase credit limit: ${base} · Effective limit: ${effective}\nRemaining borrowing capacity: ${remaining}\nPayment standing: {mode}\n{selling}',
        'reminder': 'Debt reminder',
        'help_button': 'How credit works',
        'help': 'Wholesale balance is prepaid, non-withdrawable money for wholesale orders. Credit is permission to buy now and owe the wholesale cost. Funding a balance alone does not raise your level. Levels use paid wholesale activity in the rolling last 90 days and can fall.\n\n{recovery_rules}\n\nDebt reminders begin after 24 hours. Selling pauses after {suspend} hours; unpaid services are blocked after {hold} hours; the final deletion warning is after {warning} hours; unpaid services are removed after {remove} hours. New charges and partial payments do not reset the clock. Full settlement restores eligible selling and policy-held services, but does not erase payment penalties or recreate deleted configs.',
        'deadline': '{stage}: {date} ({hours} hours remaining)',
        'suspend': 'Selling suspension', 'hold': 'Unpaid-service block', 'warning': 'Final deletion warning', 'remove': 'Unpaid-service removal',
        'due': 'Full settlement required: ${amount}',
        'block_button': '⛔ Temporary block', 'unblock_button': '▶ Unblock early',
        'block_prompt': 'Choose the access-block duration. Subscription time keeps running.',
        'block_status': 'Temporary block until {date} · {hours} hours remaining\nSubscription time keeps running. State: {state}',
        'hours': '{hours} hours', 'custom': 'Custom hours', 'custom_prompt': 'Enter a whole number of hours from 1 to 720. /cancel cancels.',
        'invalid': 'Enter a whole number from 1 to 720.', 'cancel': 'Cancel',
        'pending': 'Awaiting panel confirmation', 'blocked': 'Blocked', 'complete': 'Temporary block removed',
        'failed': 'Account unavailable or identity unverified. Resolve the account identity and try again. Any saved request remains pending.',
        'denied': 'This account selection is unavailable or you do not own it.',
        'other_block': 'Another admin, debt, or pre-existing block still applies.',
        'remove_recommendation': 'Remove recommendation',
    },
    'fa': {
        'single': 'تک‌کاربر', 'many': 'کاربران نامحدود', 'unknown': 'محدودیت کاربر نامشخص است',
        'single_note': '👤 کانفیگ تک‌کاربر: فقط برای استفاده یک کاربر.',
        'many_note': '👥 تعداد کاربران نامحدود است؛ محدودیت حجم و اعتبار زمانی همچنان برقرار است.',
        'recent': 'پرداخت در ۹۰ روز اخیر: ${amount}',
        'settlement_window': '{days} روز ({hours} ساعت) مهلت تسویه',
        'settlement_next': 'دوره بدهی بعدی در سطح {level}: {window}.',
        'settlement_current': 'دوره بدهی فعلی با سطح ثابت {level}: {window}.',
        'settlement_rules': 'همه مهلت‌ها از زمان شروع اولیه بدهی محاسبه می‌شوند. سطح و مهلت‌ها برای هر دوره بدهی ثابت می‌مانند؛ تغییر سطح در دوره بعدی اعمال می‌شود. یادآوری روزانه همچنان پس از ۲۴ ساعت آغاز می‌شود.',
        'level_down': 'با خروج پرداخت‌های قدیمی از بازه ۹۰ روزه، سطح شما اکنون {level}/{count} است. تخفیف فعلی: {discount}٪. سقف پایه اعتبار: ${limit}.',
        'credit': 'اعتبار کامل', 'half_credit': 'نصف اعتبار', 'prepaid_only': 'فقط پیش‌پرداخت',
        'selling': 'فروش فعال است', 'paused': 'فروش تعلیق شده است', 'banned': 'دسترسی توسط مدیر مسدود شده است',
        'summary': '💳 وضعیت اعتبار\nموجودی پیش‌پرداخت: ${available} · رزروشده: ${reserved}\nبدهی: ${debt}\nسقف پایه اعتبار: ${base} · سقف مؤثر: ${effective}\nاعتبار باقی‌مانده برای خرید: ${remaining}\nوضعیت پرداخت: {mode}\n{selling}',
        'reminder': 'یادآوری بدهی',
        'help_button': 'اعتبار چگونه کار می‌کند؟',
        'help': 'موجودی عمده\u200cفروشی پیش\u200cپرداختی غیرقابل\u200cبرداشت برای سفارش\u200cهای عمده است. اعتبار اجازه خرید اکنون و پرداخت هزینه عمده در آینده است. شارژ موجودی به\u200cتنهایی سطح را افزایش نمی\u200cدهد. سطح بر اساس پرداخت\u200cهای عمده در ۹۰ روز اخیر است و ممکن است کاهش یابد.\n\n{recovery_rules}\n\nیادآوری بدهی از ۲۴ ساعت آغاز می\u200cشود. فروش پس از {suspend} ساعت متوقف، سرویس\u200cهای پرداخت\u200cنشده پس از {hold} ساعت مسدود، هشدار نهایی حذف پس از {warning} ساعت ارسال و سرویس\u200cهای پرداخت\u200cنشده پس از {remove} ساعت حذف می\u200cشوند. خرید جدید و پرداخت جزئی زمان را از نو شروع نمی\u200cکنند. تسویه کامل فروش واجد شرایط و سرویس\u200cهای مسدودشده بابت بدهی را بازمی\u200cگرداند؛ جریمه اعتباری پاک نمی\u200cشود و کانفیگ حذف\u200cشده بازسازی نمی\u200cشود.',
        'deadline': '{stage}: {date} ({hours} ساعت باقی‌مانده)',
        'suspend': 'توقف فروش', 'hold': 'مسدودسازی سرویس پرداخت‌نشده', 'warning': 'هشدار نهایی حذف', 'remove': 'حذف سرویس پرداخت‌نشده',
        'due': 'مبلغ لازم برای تسویه کامل: ${amount}',
        'block_button': '⛔ مسدودسازی موقت', 'unblock_button': '▶ رفع مسدودی زودتر',
        'block_prompt': 'مدت مسدودی دسترسی را انتخاب کنید. اعتبار زمانی اشتراک همچنان مصرف می‌شود.',
        'block_status': 'مسدودی موقت تا {date} · {hours} ساعت باقی‌مانده\nاعتبار زمانی همچنان مصرف می‌شود. وضعیت: {state}',
        'hours': '{hours} ساعت', 'custom': 'ساعت دلخواه', 'custom_prompt': 'تعداد ساعت را به‌صورت عدد صحیح از ۱ تا ۷۲۰ وارد کنید. /cancel برای لغو.',
        'invalid': 'عدد صحیح از ۱ تا ۷۲۰ وارد کنید.', 'cancel': 'لغو',
        'pending': 'در انتظار تأیید سرور', 'blocked': 'مسدود', 'complete': 'مسدودی موقت برداشته شد',
        'failed': 'حساب در دسترس نیست یا هویت تأیید نشد. هویت حساب را بررسی و دوباره تلاش کنید. درخواست ذخیره‌شده در انتظار می‌ماند.',
        'denied': 'این انتخاب در دسترس نیست یا مالک حساب نیستید.',
        'other_block': 'مسدودی دیگر از طرف مدیر، بدهی یا مسدودی قبلی همچنان برقرار است.',
        'remove_recommendation': 'حذف پیشنهاد',
    },
    'ru': {
        'single': 'Один пользователь', 'many': 'Неограниченное число пользователей', 'unknown': 'Лимит пользователей неизвестен',
        'single_note': '👤 Конфигурация только для одного пользователя.',
        'many_note': '👥 Число пользователей не ограничено; лимиты трафика и срока действуют.',
        'recent': 'Оплачено за последние 90 дней: ${amount}',
        'settlement_window': '{days} дн. ({hours} ч.) на погашение',
        'settlement_next': 'Следующий долговой цикл на уровне {level}: {window}.',
        'settlement_current': 'Текущий долговой цикл, закреплённый уровень {level}: {window}.',
        'settlement_rules': 'Все сроки отсчитываются от первоначального возникновения долга. Уровень и сроки фиксируются на весь долговой цикл; изменение уровня применяется к следующему циклу. Ежедневные напоминания по-прежнему начинаются через 24 часа.',
        'level_down': 'Старые платежи вышли из окна 90 дней. Ваш уровень: {level}/{count}. Скидка: {discount}%. Базовый кредит: ${limit}.',
        'credit': 'Полный кредит', 'half_credit': 'Половина кредита', 'prepaid_only': 'Только предоплата',
        'selling': 'Продажи доступны', 'paused': 'Продажи приостановлены', 'banned': 'Доступ заблокирован администратором',
        'summary': '💳 Состояние кредита\nПредоплата: ${available} · Зарезервировано: ${reserved}\nДолг: ${debt}\nБазовый лимит: ${base} · Действующий лимит: ${effective}\nОстаток для покупок в долг: ${remaining}\nПлатёжный статус: {mode}\n{selling}',
        'reminder': 'Напоминание о долге',
        'help_button': 'Как работает кредит',
        'help': 'Оптовый баланс — предоплата для оптовых заказов без возможности вывода. Кредит позволяет купить сейчас и оплатить оптовую стоимость позже. Само пополнение не повышает уровень. Уровень зависит от оплаченной оптовой активности за последние 90 дней и может снижаться.\n\n{recovery_rules}\n\nНапоминания начинаются через 24 часа. Продажи приостанавливаются через {suspend} ч.; неоплаченные услуги блокируются через {hold} ч.; последнее предупреждение об удалении — через {warning} ч.; удаление — через {remove} ч. Новые покупки и частичная оплата не перезапускают срок. Полное погашение восстанавливает допустимые продажи и услуги, заблокированные из-за долга, но не отменяет кредитные ограничения и не воссоздаёт удалённые конфигурации.',
        'deadline': '{stage}: {date} (осталось {hours} ч.)',
        'suspend': 'Приостановка продаж', 'hold': 'Блокировка неоплаченных услуг', 'warning': 'Последнее предупреждение', 'remove': 'Удаление неоплаченных услуг',
        'due': 'Для полного погашения: ${amount}',
        'block_button': '⛔ Временная блокировка', 'unblock_button': '▶ Снять блокировку',
        'block_prompt': 'Выберите срок блокировки доступа. Срок подписки продолжает идти.',
        'block_status': 'Блокировка до {date} · осталось {hours} ч.\nСрок подписки продолжает идти. Состояние: {state}',
        'hours': '{hours} ч.', 'custom': 'Другой срок', 'custom_prompt': 'Введите целое число часов от 1 до 720. /cancel — отмена.',
        'invalid': 'Введите целое число от 1 до 720.', 'cancel': 'Отмена',
        'pending': 'Ожидание подтверждения панели', 'blocked': 'Заблокировано', 'complete': 'Временная блокировка снята',
        'failed': 'Аккаунт недоступен или его идентичность не подтверждена. Уточните аккаунт и повторите попытку. Сохранённый запрос остаётся в ожидании.',
        'denied': 'Аккаунт недоступен или не принадлежит вам.',
        'other_block': 'Другая блокировка администратора, из-за долга или прежняя блокировка остаётся.',
        'remove_recommendation': 'Убрать рекомендацию',
    },
    'tk': {
        'single': 'Bir ulanyjy', 'many': 'Çäksiz ulanyjy', 'unknown': 'Ulanyjy çägi näbelli',
        'single_note': '👤 Bu sazlama diňe bir ulanyjy üçin.',
        'many_note': '👥 Ulanyjy sany çäksiz; trafik we möhlet çäkleri güýjünde galýar.',
        'recent': 'Soňky 90 günde tölenen: ${amount}',
        'settlement_window': 'üzmek üçin {days} gün ({hours} sagat)',
        'settlement_next': '{level}-nji derejede indiki bergi döwri: {window}.',
        'settlement_current': 'Häzirki bergi döwri, berkidilen dereje {level}: {window}.',
        'settlement_rules': 'Ähli möhletler berginiň ilkinji başlan wagtyndan hasaplanýar. Dereje we möhletler her bergi döwri üçin üýtgemeýär; dereje üýtgemegi indiki döwre degişli bolýar. Gündelik ýatlatmalar öňküsi ýaly 24 sagatdan başlanýar.',
        'level_down': 'Köne tölegler 90 günlük aralykdan çykdy. Derejäňiz: {level}/{count}. Arzanladyş: {discount}%. Esasy karz çägi: ${limit}.',
        'credit': 'Doly karz', 'half_credit': 'Ýarym karz', 'prepaid_only': 'Diňe öňünden töleg',
        'selling': 'Satuw elýeterli', 'paused': 'Satuw wagtlaýyn togtadyldy', 'banned': 'Administrator girişi gadagan etdi',
        'summary': '💳 Karz ýagdaýy\nÖňünden töleg: ${available} · Ätiýaçda: ${reserved}\nBergi: ${debt}\nEsasy çäk: ${base} · Häzirki çäk: ${effective}\nGalan karz mümkinçiligi: ${remaining}\nTöleg ýagdaýy: {mode}\n{selling}',
        'reminder': 'Bergi ýatlatmasy',
        'help_button': 'Karz nähili işleýär',
        'help': 'Lomaý balans — diňe lomaý sargytlar üçin öňünden tölenen, çykaryp bolmaýan pul. Karz häzir satyn alyp, lomaý bahany soň tölemäge mümkinçilik berýär. Diňe balans doldurmak derejäni ýokarlandyrmaýar. Dereje soňky 90 gündäki tölenen lomaý işjeňlige bagly we peselip biler.\n\n{recovery_rules}\n\nÝatlatmalar 24 sagatdan başlanýar. Satuw {suspend} sagatdan togtaýar; tölenmedik hyzmatlar {hold} sagatdan bloklanýar; soňky pozmak duýduryşy {warning} sagatdan; pozmak {remove} sagatdan bolýar. Täze sargyt we bölekleýin töleg wagty täzeden başlatmaýar. Doly töleg degişli satuwy we bergi sebäpli bloklanan hyzmatlary dikeldýär, emma karz çäklendirmesini aýyrmaýar we pozulan sazlamalary döretmeýär.',
        'deadline': '{stage}: {date} ({hours} sagat galdy)',
        'suspend': 'Satuwy togtatmak', 'hold': 'Tölenmedik hyzmaty bloklamak', 'warning': 'Soňky pozmak duýduryşy', 'remove': 'Tölenmedik hyzmaty pozmak',
        'due': 'Doly üzmek üçin: ${amount}',
        'block_button': '⛔ Wagtlaýyn bloklamak', 'unblock_button': '▶ Ir açmak',
        'block_prompt': 'Giriş blokunyň möhletini saýlaň. Abunanyň wagty dowam edýär.',
        'block_status': '{date} çenli bloklanan · {hours} sagat galdy\nAbunanyň wagty dowam edýär. Ýagdaý: {state}',
        'hours': '{hours} sagat', 'custom': 'Başga sagat', 'custom_prompt': '1-den 720-ä çenli bitin sagat sanyny giriziň. /cancel — ýatyrmak.',
        'invalid': '1-den 720-ä çenli bitin san giriziň.', 'cancel': 'Ýatyrmak',
        'pending': 'Panel tassyklamasyna garaşýar', 'blocked': 'Bloklanan', 'complete': 'Wagtlaýyn blok aýryldy',
        'failed': 'Hasap elýeterli däl ýa-da şahsyýeti tassyklanmady. Hasaby anyklap, täzeden synanyşyň. Ýatda saklanan isleg garaşýar.',
        'denied': 'Hasap elýeterli däl ýa-da size degişli däl.',
        'other_block': 'Administratoryň, berginiň ýa-da öňki başga bloky güýjünde galýar.',
        'remove_recommendation': 'Maslahaty aýyrmak',
    },
}


def experience_text(language, key, **values):
    return TEXT.get(language, TEXT['en'])[key].format(**values)


def access_limit_text(language, record=None, live=None, *, short=False, plan=False):
    source = record or {}
    renewals = [item for item in source.get('renewals', []) if isinstance(item, dict)
                and item.get('unlimited') is not None
                and (item.get('renewal_mode') != 'reserved' or item.get('renewal_status') == 'applied')]
    if renewals:
        source = renewals[-1]
    value = source.get('unlimited', (source.get('renewal_plan_snapshot') or {}).get('unlimited', False if plan else None))
    # A panel's default IP limit is not a promise of unlimited-user service.
    # Prefer the purchased access type; live fields recover legacy records.
    if value is None:
        for key in ('unlimited_ip', 'unlimited_user'):
            if isinstance((live or {}).get(key), bool):
                value = live[key]
                break
    if value is None:
        return experience_text(language, 'unknown')
    unlimited = value is True or str(value).lower() in {'true', '1'}
    return experience_text(language, ('many' if unlimited else 'single') + ('' if short else '_note'))


def settlement_window_text(language, hours):
    return experience_text(language, 'settlement_window', days=f'{hours / 24:g}', hours=f'{hours:g}')


def build_settlement_terms(language, record, *, now=None):
    from utils import reseller as store
    summary = store.get_reseller_level_summary(record, now=now)
    text = experience_text(language, 'settlement_next', level=summary['level'],
        window=settlement_window_text(language, summary['settlement_hours']))
    if not store._is_debt_fully_settled((record or {}).get('debt', 0)):
        deadlines = store.get_reseller_debt_deadlines(record, now=now)
        text += '\n' + experience_text(language, 'settlement_current', level=deadlines['level'],
            window=settlement_window_text(language, deadlines['suspend_hours']))
    return text


def build_credit_help(language, record=None):
    from utils import reseller as store
    deadlines = store.get_reseller_debt_deadlines(record)
    return experience_text(language, 'help', recovery_rules=journey_text(language, 'rules'),
        suspend=f"{deadlines['suspend_hours']:g}", hold=f"{deadlines['hold_hours']:g}",
        warning=f"{deadlines['warning_hours']:g}", remove=f"{deadlines['removal_hours']:g}") + '\n\n' + (
            build_settlement_terms(language, record) + '\n' + experience_text(language, 'settlement_rules'))


def build_credit_summary(language, record, reseller_id, *, balance=None, now=None, deadlines=True, policy=None):
    from utils import reseller as store
    from utils.time_utils import utc_now, parse_utc_timestamp, format_utc_display
    from utils.reseller_wholesale_credit import get_wholesale_balance
    from utils.currency_format import format_usd_amount
    current = parse_utc_timestamp(now) if now is not None else utc_now()
    policy = store.get_reseller_credit_policy(record, now=now) if policy is None else policy
    balance = get_wholesale_balance(reseller_id) if balance is None else balance
    debt = float(record.get('debt', 0) or 0)
    cycle_deadlines = store.get_reseller_debt_deadlines(record, now=current)
    has_collectible_debt = not store._is_debt_fully_settled(debt)
    started = parse_utc_timestamp(record.get('debt_since'))
    overdue = bool(has_collectible_debt and started and
                   (current - started).total_seconds() >= cycle_deadlines['suspend_hours'] * 3600)
    status = record.get('status', 'approved')
    selling = 'banned' if status == 'banned' else 'paused' if status != 'approved' or overdue else 'selling'
    values = {key: format_usd_amount(value) for key, value in {
        'available': balance.get('available', 0), 'reserved': balance.get('reserved', 0),
        'debt': debt, 'base': policy['base_limit'], 'effective': policy['effective_limit'],
        'remaining': max(0, policy['effective_limit'] - debt),
    }.items()}
    from utils.reseller_funding import borrowing_reserved
    reserved_credit = borrowing_reserved(reseller_id)
    values['remaining'] = format_usd_amount(max(0, policy['effective_limit'] - debt - reserved_credit))
    text = experience_text(language, 'summary', **values, mode=experience_text(language, policy['mode']), selling=experience_text(language, selling))
    text += '\n' + journey_text(language, 'reserved', amount=format_usd_amount(reserved_credit))
    next_stage = None
    if has_collectible_debt and started:
        for stage, hours in [('suspend', cycle_deadlines['suspend_hours']), ('hold', cycle_deadlines['hold_hours']),
                             ('warning', cycle_deadlines['warning_hours']), ('remove', cycle_deadlines['removal_hours'])]:
            due = started + timedelta(hours=hours)
            if due > current:
                next_stage = stage
                text += '\n' + experience_text(language, 'deadline', stage=experience_text(language, stage),
                    date=format_utc_display(due), hours=f'{(due-current).total_seconds()/3600:.1f}')
                break
    text += '\n' + recovery_text(language, record, policy)
    if selling != 'selling':
        text += '\n' + journey_text(language, 'settle' if overdue or record.get('suspended_reason') in {'debt', 'unban_grace'} else 'contact')
    elif policy['mode'] != 'credit':
        text += '\n' + journey_text(language, 'next')
    text += '\n' + build_settlement_terms(language, record, now=current)
    if debt > 0.005:
        text += '\n' + experience_text(language, 'due', amount=format_usd_amount(debt))
    if deadlines and has_collectible_debt and started:
        for stage, hours in [('reminder', 24), ('suspend', cycle_deadlines['suspend_hours']), ('hold', cycle_deadlines['hold_hours']),
                             ('warning', cycle_deadlines['warning_hours']), ('remove', cycle_deadlines['removal_hours'])]:
            due = started + timedelta(hours=hours)
            if stage == next_stage:
                continue
            if due <= current:
                text += '\n' + journey_text(language, 'overdue', stage=experience_text(language, stage), date=format_utc_display(due))
            else:
                text += '\n' + experience_text(language, 'deadline', stage=experience_text(language, stage),
                    date=format_utc_display(due), hours=f'{(due-current).total_seconds()/3600:.1f}')
    return text
