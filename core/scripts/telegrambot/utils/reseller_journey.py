"""Localized reseller funding and rehabilitation journey."""

TEXT = {
    'en': {
        'completed': 'Order completed: {username}.',
        'accounting_pending': 'The service is ready; payment accounting is being retried. Do not repeat the order.',
        'pending_orders': 'Orders awaiting completion: {count}',
        'recovery': '🔄 Credit recovery: {cycles}/2 cycles completed.\nCurrent cycle: ${progress}/$5.00 prepaid spent. Spend ${remaining} more prepaid to restore full credit.',
        'ready': '✅ Full credit available at your current level. Purchases use prepaid first, then available credit.',
        'late': 'Reason: late settlement.', 'default': 'Reason: missed payment / service hold.',
        'next': 'Next: top up if needed and complete prepaid wholesale spending. Each cycle is $5 at every level; full credit returns after $10.',
        'settle': 'Next: settle the outstanding debt to restore eligible selling. Credit recovery and account access are separate.',
        'contact': 'Next: contact an administrator about your access restriction.',
        'funding': 'Payment: ${prepaid} prepaid + ${debt} credit.',
        'after': 'After purchase: prepaid ${balance} · debt ${debt}.',
        'reserved': 'Borrowing reserved: ${amount}',
        'changed': 'Funding changed. Review the updated amounts and confirm the purchase again.',
        'rules': 'Purchases spend prepaid first and borrow only the remainder. Recovery uses two $5 prepaid spending cycles at every level ($10 total), not order counts. Mixed orders count only their prepaid portion. Top-ups and debt settlements do not count. Restrictions remain after the first cycle; the second restores full credit. A new late/default event resets recovery. Full credit does not erase debt or remove separate access restrictions.',
        'restore': 'Restore full credit',
        'reason_prompt': 'Enter the reason for restoring credit (1–500 characters). /cancel cancels.',
        'restore_preview': 'Restore full credit for {user_id}?\nReason: {reason}\nResulting credit limit: ${limit}\nDebt, settlement deadlines and independent access restrictions remain in effect.',
        'restored': 'Your full credit eligibility has been restored by an administrator.',
        'overdue': '{stage}: {date} (overdue)',
    },
    'fa': {
        'completed': 'سفارش تکمیل شد: {username}.',
        'accounting_pending': 'سرویس آماده است؛ ثبت پرداخت در حال تلاش مجدد است. سفارش را تکرار نکنید.',
        'pending_orders': 'سفارش\u200cهای در انتظار تکمیل: {count}',
        'recovery': '🔄 بازگشت اعتبار: {cycles} از ۲ دوره کامل شده.\nخرج پیش‌پرداخت در دوره فعلی: ${progress} از $5.00. برای بازگشت اعتبار کامل، ${remaining} دیگر پیش‌پرداخت خرج کنید.',
        'ready': '✅ اعتبار کامل سطح فعلی برقرار است. خرید ابتدا از پیش‌پرداخت و سپس از اعتبار باقی‌مانده پرداخت می‌شود.',
        'late': 'دلیل: تأخیر در تسویه.', 'default': 'دلیل: پرداخت‌نشدن بدهی و مسدودی سرویس.',
        'next': 'گام بعد: در صورت نیاز موجودی را شارژ و خرید عمده با پیش‌پرداخت انجام دهید. هر دوره در همه سطح‌ها $5 است؛ پس از $10 اعتبار کامل بازمی‌گردد.',
        'settle': 'گام بعد: بدهی را تسویه کنید تا فروش واجد شرایط فعال شود. بازگشت اعتبار و دسترسی حساب جدا هستند.',
        'contact': 'گام بعد: برای محدودیت دسترسی با مدیر تماس بگیرید.',
        'funding': 'پرداخت: ${prepaid} از پیش‌پرداخت + ${debt} از اعتبار.',
        'after': 'پس از خرید: موجودی پیش‌پرداخت ${balance} · بدهی ${debt}.',
        'reserved': 'اعتبار رزروشده برای خریدها: ${amount}',
        'changed': 'منابع پرداخت تغییر کرده است. مبلغ‌های جدید را بررسی و خرید را دوباره تأیید کنید.',
        'rules': 'خرید ابتدا از پیش‌پرداخت و فقط باقی‌مانده از اعتبار پرداخت می‌شود. بازگشت اعتبار در همه سطح‌ها به دو دوره خرج پیش‌پرداخت $5، یعنی مجموع $10، نیاز دارد؛ تعداد سفارش ملاک نیست. در خرید ترکیبی فقط بخش پیش‌پرداخت حساب می‌شود. شارژ موجودی و تسویه بدهی حساب نمی‌شوند. پس از دوره اول محدودیت باقی می‌ماند؛ دوره دوم اعتبار کامل را بازمی‌گرداند. تأخیر یا نکول جدید پیشرفت را صفر می‌کند. بازگشت اعتبار بدهی یا محدودیت مستقل دسترسی را حذف نمی‌کند.',
        'restore': 'بازگرداندن اعتبار کامل',
        'reason_prompt': 'دلیل بازگرداندن اعتبار را بنویسید (۱ تا ۵۰۰ نویسه). /cancel برای لغو.',
        'restore_preview': 'اعتبار کامل {user_id} بازگردانده شود؟\nدلیل: {reason}\nسقف اعتبار پس از تغییر: ${limit}\nبدهی، مهلت تسویه و محدودیت مستقل دسترسی برقرار می‌مانند.',
        'restored': 'مدیر امکان استفاده از اعتبار کامل را برای شما بازگرداند.',
        'overdue': '{stage}: {date} (مهلت گذشته)',
    },
    'ru': {
        'completed': 'Заказ завершён: {username}.',
        'accounting_pending': 'Услуга готова; учёт оплаты будет повторён. Не повторяйте заказ.',
        'pending_orders': 'Заказы, ожидающие завершения: {count}',
        'recovery': '🔄 Восстановление кредита: завершено {cycles}/2 циклов.\nПредоплата в текущем цикле: ${progress}/$5.00. Потратьте ещё ${remaining} предоплаты для полного кредита.',
        'ready': '✅ Доступен полный кредит вашего уровня. Сначала расходуется предоплата, затем доступный кредит.',
        'late': 'Причина: просроченное погашение.', 'default': 'Причина: неоплаченный долг / блокировка услуг.',
        'next': 'Далее: при необходимости пополните баланс и оплачивайте оптовые заказы предоплатой. Цикл — $5 на любом уровне; полный кредит вернётся после $10.',
        'settle': 'Далее: погасите долг для восстановления разрешённых продаж. Восстановление кредита и доступ к аккаунту учитываются отдельно.',
        'contact': 'Далее: обратитесь к администратору по поводу ограничения доступа.',
        'funding': 'Оплата: ${prepaid} предоплаты + ${debt} кредита.',
        'after': 'После покупки: предоплата ${balance} · долг ${debt}.',
        'reserved': 'Зарезервированный кредит: ${amount}',
        'changed': 'Источники оплаты изменились. Проверьте суммы и подтвердите покупку снова.',
        'rules': 'Сначала расходуется предоплата, остаток оплачивается кредитом. Восстановление требует двух циклов расходов предоплаты по $5 на любом уровне ($10 всего), независимо от числа заказов. При смешанной оплате учитывается только предоплата. Пополнения и погашения долга не учитываются. После первого цикла ограничения остаются; второй возвращает полный кредит. Новая просрочка или дефолт обнуляет прогресс. Восстановление кредита не списывает долг и не отменяет отдельные ограничения доступа.',
        'restore': 'Восстановить полный кредит',
        'reason_prompt': 'Укажите причину восстановления кредита (1–500 символов). /cancel — отмена.',
        'restore_preview': 'Восстановить полный кредит для {user_id}?\nПричина: {reason}\nНовый кредитный лимит: ${limit}\nДолг, сроки погашения и отдельные ограничения доступа сохраняются.',
        'restored': 'Администратор восстановил ваш полный кредит.',
        'overdue': '{stage}: {date} (просрочено)',
    },
    'tk': {
        'completed': 'Sargyt tamamlandy: {username}.',
        'accounting_pending': 'Hyzmat taýýar; töleg ýazgysy gaýtadan synanyşylýar. Sargydy gaýtalamaň.',
        'pending_orders': 'Tamamlanmagyna garaşylýan sargytlar: {count}',
        'recovery': '🔄 Karzy dikeltmek: {cycles}/2 döwür tamamlandy.\nHäzirki döwürde sarp edilen öňünden töleg: ${progress}/$5.00. Doly karzy dikeltmek üçin ýene ${remaining} sarp ediň.',
        'ready': '✅ Häzirki derejäňiziň doly karzy elýeterli. Ilki öňünden töleg, soň galan karz ulanylýar.',
        'late': 'Sebäp: bergi gijä galyp üzüldi.', 'default': 'Sebäp: tölenmedik bergi / hyzmatlaryň bloklanmagy.',
        'next': 'Indiki ädim: gerek bolsa balans dolduryň we öňünden töleg bilen lomaý sargyt ediň. Her derejede bir döwür $5; $10 sarp edilenden soň doly karz dikeldilýär.',
        'settle': 'Indiki ädim: degişli satuwy dikeltmek üçin bergini üzüň. Karzy dikeltmek we hasaba giriş aýratyn hasaplanýar.',
        'contact': 'Indiki ädim: giriş çäklendirmesi barada administrator bilen habarlaşyň.',
        'funding': 'Töleg: ${prepaid} öňünden töleg + ${debt} karz.',
        'after': 'Satyn alandan soň: öňünden töleg ${balance} · bergi ${debt}.',
        'reserved': 'Ätiýaçdaky karz: ${amount}',
        'changed': 'Töleg çeşmeleri üýtgedi. Täze möçberleri barlap, satyn almagy gaýtadan tassyklaň.',
        'rules': 'Ilki öňünden töleg, diňe galany karzdan alynýar. Karzy dikeltmek üçin her derejede $5-lik iki sarp ediş döwri ($10 jemi) gerek; sargyt sany hasaplanmaýar. Garyşyk tölegde diňe öňünden tölenen bölek hasaplanýar. Balans doldurmak we bergi üzmek hasaplanmaýar. Birinji döwürden soň çäklendirme galýar; ikinji döwür doly karzy dikeldýär. Täze gijikme ýa-da tölemezlik ösüşi nollaýar. Karzy dikeltmek bergini ýa-da aýratyn giriş çäklendirmesini aýyrmaýar.',
        'restore': 'Doly karzy dikeltmek',
        'reason_prompt': 'Karzy dikeltmegiň sebäbini ýazyň (1–500 nyşan). /cancel — ýatyrmak.',
        'restore_preview': '{user_id} üçin doly karzy dikeltmelimi?\nSebäp: {reason}\nTäze karz çägi: ${limit}\nBergi, töleg möhletleri we aýratyn giriş çäklendirmeleri saklanýar.',
        'restored': 'Administrator doly karz mümkinçiligiňizi dikeltdi.',
        'overdue': '{stage}: {date} (möhleti geçdi)',
    },
}


def journey_text(language, key, **values):
    return TEXT.get(language, TEXT['en'])[key].format(**values)


def recovery_text(language, record, policy=None):
    from utils.reseller import get_reseller_credit_policy
    from utils.reseller_credit import recovery_state
    policy = policy or get_reseller_credit_policy(record)
    state = policy.get('recovery') or recovery_state(record)
    if not state['penalty']:
        return journey_text(language, 'ready')
    spent = state['spent_cents']
    reason = state.get('reason')
    text = journey_text(language, reason) + '\n' if reason in {'late', 'default'} else ''
    return text + journey_text(language, 'recovery', cycles=spent // 500,
        progress=f'{spent % 500 / 100:.2f}', remaining=f'{(1000 - spent) / 100:.2f}')


def funding_text(language, funding, current_debt=None):
    text = journey_text(language, 'funding', prepaid=f"{funding['prepaid_cents'] / 100:.2f}",
                        debt=f"{funding['debt_cents'] / 100:.2f}")
    if current_debt is not None:
        text += '\n' + journey_text(language, 'after',
            balance=f"{(funding['available_prepaid_cents'] - funding['prepaid_cents']) / 100:.2f}",
            debt=f"{float(current_debt) + funding['debt_cents'] / 100:.2f}")
    return text
