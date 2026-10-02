"""Short strings the user path builds in code: button labels with amounts, status lines, toasts, errors.

Screen bodies are content (:mod:`svbg.tg.user.seeds`); these are the pieces that carry live values (a price
on a button, «осталось 3 дн.»). Russian is the source, English the second language; an unknown key or
language falls back to Russian.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svbg.core.money import format_money

__all__ = [
    "METHOD_ICONS",
    "fmt_date",
    "fmt_datetime",
    "fmt_left",
    "method_label",
    "money",
    "plural_days",
    "t",
]

DEFAULT_TZ: Final = "Europe/Moscow"

_RU: Final[Mapping[str, str]] = {
    # home status lines
    "status_none": "Подписки пока нет.",
    "status_trial": "🎁 Пробный период до {until} (осталось {left}).",
    "status_active": "✅ Подписка «{plan}» активна до {until} (осталось {left}).",
    "status_expired": "⌛ Подписка закончилась {until}.",
    "status_frozen": "⏸ Подписка приостановлена. Напишите в поддержку.",
    "status_pending": "⏳ Подписка подключается, это займёт пару секунд.",
    "status_missing": "⚠️ Подписка временно недоступна. Мы уже разбираемся.",
    "friend": "друг",
    # buttons
    "btn_menu": "🏠 Меню",
    "btn_back": "◀️ Назад",
    "btn_support": "💬 Поддержка",
    "btn_period": "{period} — {price}",
    "btn_period_save": "{period} — {price} · {monthly}/мес, −{save}%",
    "btn_pay": "💳 Оплатить {price}",
    "btn_topup_method": "{icon} {method} — {amount}",
    "btn_other_amount": "✏️ Другая сумма",
    "btn_pay_url": "💳 Оплатить {amount}",
    "btn_pay_stars": "⭐ Оплатить {amount}",
    "btn_i_paid": "🔄 Я оплатил",
    "btn_other_method": "↩️ Другой способ",
    "btn_cancel_order": "✖️ Отменить покупку",
    "btn_connect": "🔗 Подключиться",
    "btn_open_page": "📲 Открыть страницу подключения",
    "btn_copy": "📋 Скопировать ссылку",
    "btn_qr": "🔳 QR-код",
    "btn_refresh": "🔄 Обновить",
    "btn_delete_device": "🗑 {n}",
    "btn_reset_devices": "🧹 Отвязать все",
    "btn_confirm_reset": "🧹 Да, отвязать все",
    "btn_confirm_reissue": "♻️ Да, перевыпустить",
    "btn_join": "📣 Перейти в канал",
    "btn_joined": "✅ Я подписался",
    "btn_renew": "🔄 Продлить",
    "btn_buy": "🛒 Купить подписку",
    "btn_devices": "📱 Устройства",
    "btn_not_me": "🗑 Это не я — удалить",
    "btn_plan": "{plan} — от {price}",
    "btn_preset": "{amount}",
    "btn_lang_ru": "🇷🇺 Русский",
    "btn_lang_en": "🇬🇧 English",
    # checkout
    "period_days": "{n} дн.",
    "period_months": "{n} мес.",
    "period_year": "1 год",
    "pay_line_enough": "Спишем с баланса {price}, останется {left}.",
    "pay_line_short": "Не хватает {missing}. Доплатите разницу, и покупка пройдёт сама.",
    "pay_line_methods": (
        "Не хватает {missing}. Выберите, чем доплатить: покупка пройдёт сразу после оплаты."
    ),
    "pay_line_free": "Ничего платить не нужно.",
    "until_approx": "≈ {date}",
    "surplus": "\n\nЕсли у способа оплаты минимальная сумма больше, разница останется на балансе.",
    "surplus_exact": "\n\n{method}: минимум {amount}, остаток {surplus} останется на балансе.",
    "after_purchase": "Как только оплата пройдёт, подписка оформится, а это сообщение обновится.",
    "after_topup": "Деньги зачислятся на баланс сразу после оплаты.",
    # connect
    "connect_ready": "Подписка действует до {until}.",
    "connect_pending": "⏳ Подписка подключается. Нажмите «Обновить» через пару секунд.",
    "connect_none": "У вас пока нет подписки.",
    "connect_frozen": "⏸ Подписка приостановлена, подключиться сейчас нельзя.",
    "qr_caption": "🔳 Наведите камеру приложения на QR-код, чтобы добавить подписку.",
    # devices
    "devices_empty": "Устройств пока нет. Подключитесь, и они появятся здесь.",
    "devices_loading": "⏳ Загружаю список…",
    "devices_fresh": "Список только что обновлён",
    "devices_unavailable": (
        "⚠️ Список не загрузился: панель временно недоступна. Нажмите «Обновить» чуть позже."
    ),
    "devices_note_delete": "\n\nНажмите 🗑 с номером, чтобы отвязать устройство.",
    "devices_unlimited": "без ограничений",
    "device_line": "{n}. {name}",
    "device_unknown": "Устройство",
    "device_deleting": "Устройство отвязывается…",
    "devices_resetting": "Устройства отвязываются…",
    "devices_gone": "Этого устройства уже нет в списке",
    # toasts / errors
    "creating_invoice": "⏳ Создаю счёт…",
    "checking": "⏳ Проверяю оплату…",
    "not_paid_yet": "Оплата ещё не поступила. Если уже оплатили, подождите минуту.",
    "paid_already": "✅ Оплата получена",
    "no_methods": "Способы оплаты сейчас недоступны. Напишите в поддержку.",
    "no_plans": "Тарифы скоро появятся. Загляните чуть позже.",
    "plan_gone": "Этот тариф больше не продаётся",
    "order_gone": "Заказ устарел. Выберите тариф заново.",
    "order_canceled": "Покупка отменена. Деньги остались на балансе.",
    "error_generic": "Не получилось. Попробуйте ещё раз.",
    "amount_prompt": "Сколько пополнить? Напишите сумму числом, например 300.",
    "amount_min": "Минимум {min}: именно столько не хватает на покупку.",
    "amount_bad": "Нужно число, например 300",
    "amount_range": "Сумма от {min} до {max}",
    "lang_set": "Язык изменён",
    "trial_unavailable": "Пробный период недоступен",
    "not_member_yet": "Подписка на канал пока не видна. Подпишитесь и нажмите ещё раз.",
    "channel_check_failed": "Не получилось проверить подписку. Попробуйте через минуту.",
    "no_subscription": "Сначала оформите подписку",
    "receipt_saved": "📎 Чек получен. Проверим перевод и зачислим деньги, обычно это недолго.",
    "receipt_no_payment": "Не нашли открытый перевод. Создайте счёт через «💰 Баланс».",
    "receipt_already": "Этот перевод уже проверен.",
    # units
    "unlimited": "безлимит",
    "left_days": "{n} дн.",
    "left_hours": "{n} ч",
    "left_minutes": "{n} мин",
}

_EN: Final[Mapping[str, str]] = {
    "status_none": "No subscription yet.",
    "status_trial": "🎁 Trial until {until} ({left} left).",
    "status_active": "✅ «{plan}» is active until {until} ({left} left).",
    "status_expired": "⌛ Your subscription ended on {until}.",
    "status_frozen": "⏸ Your subscription is on hold. Please contact support.",
    "status_pending": "⏳ Setting up your subscription. This takes a few seconds.",
    "status_missing": "⚠️ Your subscription is temporarily unavailable. We are on it.",
    "friend": "friend",
    "btn_menu": "🏠 Menu",
    "btn_back": "◀️ Back",
    "btn_support": "💬 Support",
    "btn_period_save": "{period} — {price} · {monthly}/mo, −{save}%",
    "btn_pay": "💳 Pay {price}",
    "btn_other_amount": "✏️ Other amount",
    "btn_pay_url": "💳 Pay {amount}",
    "btn_pay_stars": "⭐ Pay {amount}",
    "btn_i_paid": "🔄 I have paid",
    "btn_other_method": "↩️ Another method",
    "btn_cancel_order": "✖️ Cancel purchase",
    "btn_connect": "🔗 Connect",
    "btn_open_page": "📲 Open the connection page",
    "btn_copy": "📋 Copy the link",
    "btn_qr": "🔳 QR code",
    "btn_refresh": "🔄 Refresh",
    "btn_reset_devices": "🧹 Unlink all",
    "btn_confirm_reset": "🧹 Yes, unlink all",
    "btn_confirm_reissue": "♻️ Yes, issue a new link",
    "btn_join": "📣 Open the channel",
    "btn_joined": "✅ I have joined",
    "btn_renew": "🔄 Renew",
    "btn_buy": "🛒 Buy",
    "btn_devices": "📱 Devices",
    "btn_not_me": "🗑 Not me — remove",
    "btn_plan": "{plan} — from {price}",
    "btn_period": "{period} — {price}",
    "btn_topup_method": "{icon} {method} — {amount}",
    "btn_delete_device": "🗑 {n}",
    "btn_preset": "{amount}",
    "btn_lang_ru": "🇷🇺 Русский",
    "btn_lang_en": "🇬🇧 English",
    "period_days": "{n} days",
    "period_months": "{n} mo.",
    "period_year": "1 year",
    "pay_line_enough": "{price} will be taken from your balance, {left} will be left.",
    "pay_line_short": "You are {missing} short. Pay the difference and the purchase goes through on its own.",
    "pay_line_methods": (
        "You are {missing} short. Choose how to pay the difference: the purchase goes through once you pay."
    ),
    "pay_line_free": "Nothing to pay.",
    "until_approx": "≈ {date}",
    "surplus": "\n\nIf the method has a higher minimum, the extra stays on your balance.",
    "surplus_exact": "\n\n{method}: minimum {amount}, so {surplus} will stay on your balance.",
    "after_purchase": "Once the payment goes through, you get the subscription and this message updates.",
    "after_topup": "The money is added to your balance as soon as you pay.",
    "connect_ready": "Valid until {until}.",
    "connect_pending": "⏳ Setting up your subscription. Tap «Refresh» in a few seconds.",
    "connect_none": "You have no subscription yet.",
    "connect_frozen": "⏸ Your subscription is on hold, so you cannot connect right now.",
    "qr_caption": "🔳 Scan the QR code with the VPN app to add the subscription.",
    "devices_empty": "No devices yet. Connect and they will show up here.",
    "devices_loading": "⏳ Loading…",
    "devices_fresh": "The list has just been updated",
    "devices_unavailable": (
        "⚠️ Could not load the list: the panel is not responding. Tap «Refresh» a bit later."
    ),
    "devices_note_delete": "\n\nTap 🗑 with a number to unlink a device.",
    "devices_unlimited": "unlimited",
    "device_line": "{n}. {name}",
    "device_unknown": "Device",
    "device_deleting": "Unlinking the device…",
    "devices_resetting": "Unlinking devices…",
    "devices_gone": "This device is no longer in the list",
    "creating_invoice": "⏳ Creating the invoice…",
    "checking": "⏳ Checking the payment…",
    "not_paid_yet": "No payment yet. If you have paid, give it a minute.",
    "paid_already": "✅ Payment received",
    "no_methods": "Payment methods are unavailable right now. Please contact support.",
    "no_plans": "Plans are coming soon.",
    "plan_gone": "This plan is no longer on sale",
    "order_gone": "The order is outdated. Choose a plan again.",
    "order_canceled": "Purchase canceled. Your balance is untouched.",
    "error_generic": "Something went wrong. Please try again.",
    "amount_prompt": "How much to top up? Send the amount, e.g. 300.",
    "amount_min": "The minimum is {min}: that is how much you are short.",
    "amount_bad": "Send a number, e.g. 300",
    "amount_range": "The amount must be from {min} to {max}",
    "lang_set": "Language changed",
    "trial_unavailable": "The trial is unavailable",
    "not_member_yet": "You have not joined the channel yet. Join it and tap again.",
    "channel_check_failed": "Could not check whether you joined. Try again in a minute.",
    "no_subscription": "Get a subscription first",
    "receipt_saved": "📎 Got the receipt. We will check the transfer and credit the money shortly.",
    "receipt_no_payment": "No open transfer found. Create an invoice in «💰 Balance».",
    "receipt_already": "This transfer has already been checked.",
    "unlimited": "unlimited",
    "left_days": "{n} d",
    "left_hours": "{n} h",
    "left_minutes": "{n} min",
}

_TEXTS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"ru": MappingProxyType(dict(_RU)), "en": MappingProxyType(dict(_EN))}
)

#: Payment method kinds as the user sees them (provider names stay hidden, 04 §8): (icon, ru, en).
METHOD_ICONS: Final[Mapping[str, tuple[str, str, str]]] = MappingProxyType(
    {
        "sbp": ("📱", "СБП", "SBP"),
        "card": ("💳", "Карта", "Card"),
        "intl_card": ("🌍", "Зарубежная карта", "Foreign card"),
        "crypto": ("🪙", "Крипта", "Crypto"),
        "stars": ("⭐", "Stars", "Stars"),
        "wallet": ("👛", "Кошелёк", "Wallet"),
        "manual": ("🏦", "Перевод", "Transfer"),
    }
)


def t(lang: str | None, key: str, **values: Any) -> str:
    """String ``key`` in ``lang`` (Russian fallback) with ``{name}`` values substituted (no format specs)."""
    table = _TEXTS.get(lang or "ru") or _TEXTS["ru"]
    text = table.get(key) or _RU[key]
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def method_label(kind: str | None, lang: str) -> tuple[str, str]:
    """``(icon, name)`` of a method kind; unknown kinds are shown as «Оплата»."""
    icon, ru, en = METHOD_ICONS.get(kind or "", ("💳", "Оплата", "Payment"))
    return icon, en if lang == "en" else ru


def money(amount_minor: int, currency: str, lang: str = "ru") -> str:
    try:
        return format_money(int(amount_minor), currency, lang, nbsp=True)
    except (ValueError, KeyError, TypeError):
        return f"{amount_minor} {currency}"


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def fmt_date(value: datetime | None, tz: str | None = None) -> str:
    """``12.11.2026`` in the shop's time zone; «—» for none."""
    if value is None:
        return "—"
    return value.astimezone(_zone(tz)).strftime("%d.%m.%Y")


def fmt_datetime(value: datetime | None, tz: str | None = None) -> str:
    """``12.11.2026 18:30`` in the shop's time zone; «—» for none."""
    if value is None:
        return "—"
    return value.astimezone(_zone(tz)).strftime("%d.%m.%Y %H:%M")


def fmt_left(seconds: float, lang: str = "ru") -> str:
    """«3 дн.» / «5 ч» / «40 мин» — whole days rounded up above a day, hours rounded up below."""
    s = max(0.0, seconds)
    if s >= 86_400:
        return t(lang, "left_days", n=-(-int(s) // 86_400))
    if s >= 3_600:
        return t(lang, "left_hours", n=-(-int(s) // 3_600))
    return t(lang, "left_minutes", n=max(1, -(-int(s) // 60)))


def plural_days(days: int, lang: str = "ru") -> str:
    """A period as people say it: «30 дн.» → «1 мес.», 360/365 → «1 год», 90 → «3 мес.»."""
    if days in (360, 365):
        return t(lang, "period_year")
    if days >= 30 and days % 30 == 0:
        return t(lang, "period_months", n=days // 30)
    return t(lang, "period_days", n=days)
