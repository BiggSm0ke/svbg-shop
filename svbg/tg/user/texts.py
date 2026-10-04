"""Short strings the user path builds in code: button labels with amounts, status lines, toasts, errors.

Screen bodies are content (:mod:`svbg.tg.user.seeds`); these are the pieces that carry live values (a price
on a button, «осталось 3 дн.»). The bot speaks Russian only.
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
    "fmt_bytes",
    "fmt_date",
    "fmt_datetime",
    "fmt_left",
    "fmt_left_short",
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
    "trial_unavailable": "Пробный период недоступен",
    "not_member_yet": "Подписка на канал пока не видна. Подпишитесь и нажмите ещё раз.",
    "channel_check_failed": "Не получилось проверить подписку. Попробуйте через минуту.",
    "captcha_wrong": "Не то. Попробуйте ещё раз",
    "captcha_cooldown": "Слишком много ошибок подряд. Подождите минуту и попробуйте снова",
    "captcha_wait": "Подождите ещё {seconds} сек.",
    "captcha_done": "Проверка уже пройдена",
    "no_subscription": "Сначала оформите подписку",
    "receipt_saved": "📎 Чек получен. Проверим перевод и зачислим деньги, обычно это недолго.",
    "receipt_no_payment": "Не нашли открытый перевод. Создайте счёт через «💰 Баланс».",
    "receipt_already": "Этот перевод уже проверен.",
    # units
    "unlimited": "безлимит",
    "left_days": "{n} дн.",
    "left_hours": "{n} ч",
    "left_minutes": "{n} мин",
    "left_days_hours": "{d} дн. {h} ч",
    "size_gb": "{n} ГБ",
    "size_mb": "{n} МБ",
    # the «Подписка» section and its home button
    "sub_btn_expired": "закончилась",
    "sub_btn_paused": "на паузе",
    "sub_state_active": "🟢 активна",
    "sub_state_trial": "🎁 пробный период",
    "sub_state_expired": "🔴 закончилась",
    "sub_state_frozen": "⏸ на паузе",
    "sub_state_pending": "⏳ подключается",
    "sub_state_missing": "⚠️ временно недоступна",
    "sub_used_of": "{used} из {limit}",
    "sub_no_limit": "{used}, без лимита",
    "sub_devices_upto": "до {limit}",
    "sub_servers": "\nСерверы: {list}",
    "sub_servers_more": "{list} и ещё {n}",
    "sub_trial_line": "\n\n🎁 Можно попробовать бесплатно: {days} дн.",
    # «👤 Профиль»
    "profile_sub": (
        "Тариф: {plan}\nСтатус: {status}\nОсталось: {left}\nДействует до: {until}\n"
        "Устройства: {devices}\nТрафик: {traffic}{servers}"
    ),
    "profile_no_sub": "Подписки пока нет. Нажмите «Купить подписку», чтобы выбрать тариф и срок.",
    "profile_no_name": "без имени",
}

#: The one table, under its language code (older helpers and tests read ``_TEXTS["ru"]``).
_TEXTS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType({"ru": MappingProxyType(dict(_RU))})

#: Payment method kinds as the user sees them (provider names stay hidden, 04 §8): (icon, name).
METHOD_ICONS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "sbp": ("📱", "СБП"),
        "card": ("💳", "Карта"),
        "intl_card": ("🌍", "Зарубежная карта"),
        "crypto": ("🪙", "Крипта"),
        "stars": ("⭐", "Stars"),
        "wallet": ("👛", "Кошелёк"),
        "manual": ("🏦", "Перевод"),
    }
)


def t(_lang: str | None, key: str, **values: Any) -> str:
    """String ``key`` with ``{name}`` values substituted (no format specs). The bot speaks Russian only: the
    first argument (an old language code) is ignored."""
    text = _RU[key]
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def method_label(kind: str | None, _lang: str | None = None) -> tuple[str, str]:
    """``(icon, name)`` of a method kind; unknown kinds are shown as «Оплата»."""
    return METHOD_ICONS.get(kind or "", ("💳", "Оплата"))


def money(amount_minor: int, currency: str, _lang: str | None = None) -> str:
    try:
        return format_money(int(amount_minor), currency, nbsp=True)
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


def fmt_left_short(seconds: float, lang: str = "ru") -> str:
    """Time left on the «Подписка» button: «12 дн.» from 3 days up (days rounded up, like ``days_left``),
    «2 дн. 5 ч» below 3 days, «5 ч» / «40 мин» below a day (rounded down: never more than there is)."""
    s = max(0, int(seconds))
    if s >= 3 * 86_400:
        return t(lang, "left_days", n=-(-s // 86_400))
    days, rest = divmod(s, 86_400)
    hours = rest // 3_600
    if days:
        return t(lang, "left_days_hours", d=days, h=hours) if hours else t(lang, "left_days", n=days)
    if hours:
        return t(lang, "left_hours", n=hours)
    return t(lang, "left_minutes", n=max(1, rest // 60))


def fmt_bytes(value: int | None, lang: str = "ru") -> str:
    """«1,5 ГБ» / «300 МБ» (binary units, one decimal for gigabytes)."""
    n = max(0, int(value or 0))
    if n >= 1024**3:
        gb = f"{n / 1024**3:.1f}".rstrip("0").rstrip(".")
        return t(lang, "size_gb", n=gb.replace(".", ","))
    return t(lang, "size_mb", n=round(n / 1024**2))


def plural_days(days: int, lang: str = "ru") -> str:
    """A period as people say it: «30 дн.» → «1 мес.», 360/365 → «1 год», 90 → «3 мес.»."""
    if days in (360, 365):
        return t(lang, "period_year")
    if days >= 30 and days % 30 == 0:
        return t(lang, "period_months", n=days // 30)
    return t(lang, "period_days", n=days)
