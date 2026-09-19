import type {Language} from './api';

const text: Record<string, [string,string,string,string]> = {
  account_connected: ['Active', 'فعال', 'Активно', 'Işjeň'],
  account_hold: ['Waiting for first connection', 'در انتظار اولین اتصال', 'Ожидается первое подключение', 'Ilkinji birikmä garaşylýar'],
  account_expired: ['Expired', 'منقضی شده', 'Срок истёк', 'Möhleti gutardy'],
  account_blocked: ['Restricted', 'محدود شده', 'Ограничено', 'Çäklendirildi'],
  account_unknown: ['Verification needed', 'نیازمند بررسی', 'Требуется проверка', 'Barlag gerek'],
  preparing_payment: ['Preparing payment', 'آماده‌سازی پرداخت', 'Подготовка платежа', 'Töleg taýýarlanýar'],
  awaiting_receipt: ['Waiting for your receipt', 'در انتظار رسید شما', 'Ожидается квитанция', 'Töleg resminamaňyza garaşylýar'],
  awaiting_payment: ['Waiting for payment', 'در انتظار پرداخت', 'Ожидается оплата', 'Tölege garaşylýar'],
  awaiting_review: ['Receipt under review', 'رسید در حال بررسی', 'Квитанция на проверке', 'Resminama barlanýar'],
  preparing_service: ['Preparing your connection', 'آماده‌سازی اتصال شما', 'Подготовка подключения', 'Birikmäňiz taýýarlanýar'],
  needs_attention: ['Support is reviewing this payment. Do not pay again.', 'پشتیبانی در حال بررسی این پرداخت است. دوباره پرداخت نکنید.', 'Поддержка проверяет платёж. Не оплачивайте повторно.', 'Goldaw tölegi barlaýar. Gaýtadan tölemäň.'],
  completed: ['Completed', 'تکمیل شد', 'Завершено', 'Tamamlandy'],
  cancelled: ['Cancelled', 'لغو شد', 'Отменено', 'Ýatyryldy'],
  rejected: ['Receipt rejected', 'رسید رد شد', 'Квитанция отклонена', 'Resminama ret edildi'],
  expired: ['Payment expired', 'مهلت پرداخت تمام شد', 'Срок оплаты истёк', 'Töleg möhleti gutardy'],
  renewal_reserved: ['Renewal reserved; activation is automatic when eligible.', 'تمدید رزرو شد؛ در زمان مجاز خودکار فعال می‌شود.', 'Продление зарезервировано и активируется автоматически при наступлении срока.', 'Uzaltma saklandy; wagty gelende awtomatik işjeňleşer.'],
  renewal_activating: ['Activating your renewal', 'فعال‌سازی تمدید شما', 'Активация продления', 'Uzaltma işjeňleşdirilýär'],
  immediate: ['Renew now', 'تمدید فوری', 'Продлить сейчас', 'Häzir uzaltmak'],
  reserved: ['Reserve renewal', 'رزرو تمدید', 'Зарезервировать продление', 'Uzaltmany saklamak'],
  renewal_mode: ['Renewal option', 'نوع تمدید', 'Вариант продления', 'Uzaltma görnüşi'],
  already_reserved: ['A renewal is already pending for this account.', 'یک تمدید برای این حساب در انتظار است.', 'Для этого подключения уже ожидается продление.', 'Bu hasap üçin uzaltma eýýäm garaşýar.'],
  not_expired: ['This connection has not expired yet.', 'این اتصال هنوز منقضی نشده است.', 'Срок подключения ещё не истёк.', 'Bu birikmäniň möhleti entek gutarmady.'],
  protected_account: ['Contact support about this account before renewing.', 'پیش از تمدید این حساب با پشتیبانی تماس بگیرید.', 'Перед продлением обратитесь в поддержку.', 'Uzaltmazdan öň goldawa ýüz tutuň.'],
  history_unavailable: ['Account history needs verification.', 'سوابق حساب نیاز به بررسی دارد.', 'История подключения требует проверки.', 'Hasabyň taryhy barlanmaly.'],
  plan_unavailable: ['This plan is unavailable.', 'این طرح در دسترس نیست.', 'Тариф недоступен.', 'Bu meýilnama elýeterli däl.'],
  account_changed: ['The account changed. Refresh its renewal options.', 'حساب تغییر کرده است. گزینه‌های تمدید را تازه کنید.', 'Подключение изменилось. Обновите варианты продления.', 'Hasap üýtgedi. Uzaltma görnüşlerini täzeläň.'],
  renewal_unavailable: ['This renewal option is unavailable.', 'این نوع تمدید در دسترس نیست.', 'Этот вариант продления недоступен.', 'Bu uzaltma görnüşi elýeterli däl.'],
  store_unavailable: ['Continue through this storefront’s bot.', 'از ربات این فروشگاه ادامه دهید.', 'Продолжите через бот магазина.', 'Dükanyň botunda dowam ediň.'],
  writes_paused: ['Changes are temporarily paused.', 'تغییرات موقتاً متوقف شده‌اند.', 'Изменения временно приостановлены.', 'Üýtgetmeler wagtlaýyn togtadyldy.'],
  language_unavailable: ['Card transfer is available in Persian.', 'پرداخت کارت‌به‌کارت به زبان فارسی در دسترس است.', 'Перевод на карту доступен на персидском языке.', 'Kart tölegi pars dilinde elýeterlidir.'],
  method_unavailable: ['This payment method is temporarily unavailable.', 'این روش پرداخت موقتاً در دسترس نیست.', 'Этот способ оплаты временно недоступен.', 'Bu töleg usuly wagtlaýyn elýeterli däl.'],
  account_busy: ['Another operation is in progress for this connection.', 'عملیات دیگری برای این اتصال در جریان است.', 'Для подключения выполняется другая операция.', 'Bu birikme üçin başga amal dowam edýär.'],
  account_unavailable: ['Connection verification is temporarily unavailable.', 'بررسی اتصال موقتاً در دسترس نیست.', 'Проверка подключения временно недоступна.', 'Birikmäni barlamak wagtlaýyn elýeterli däl.'],
  refresh: ['Refresh status', 'تازه‌سازی وضعیت', 'Обновить статус', 'Ýagdaýy täzelemek'],
  telegram_resume: ['In Telegram, send /payments to continue the same payment.', 'در تلگرام برای ادامه همین پرداخت دستور /payments را بفرستید.', 'В Telegram отправьте /payments, чтобы продолжить этот же платёж.', 'Şol tölegi dowam etmek üçin Telegramda /payments iberiň.'],
};

export function customerText(lang: Language, key?: string | null): string {
  return (text[key || ''] || text.renewal_unavailable)[{en:0,fa:1,ru:2,tk:3}[lang]];
}
