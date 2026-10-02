"""User- and owner-facing texts of billing (one place; content screens may override the user ones).

Screen ids (system screens of the screen engine, 07 §2.4.1) are the ``SCREEN_*`` constants; the user path
seeds them with these texts as defaults. User notices exist in Russian and English (``lang`` — the user's
language, :func:`lang_of`); owner-facing texts («Требует внимания») are Russian.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svbg.billing.ports import Button, Notice
from svbg.core.money import format_money

__all__ = [
    "ATTENTION",
    "ERRORS",
    "ERRORS_EN",
    "LANGS",
    "REASONS_EN",
    "SCREEN_CONNECTING",
    "SCREEN_CREDITED",
    "SCREEN_HELD",
    "SCREEN_PAID",
    "SCREEN_REFUNDED",
    "connecting_notice",
    "credited_notice",
    "fmt_date",
    "held_notice",
    "lang_of",
    "localize_reason",
    "money",
    "paid_notice",
    "refunded_notice",
]

SCREEN_PAID: Final = "billing.paid"
SCREEN_CONNECTING: Final = "billing.connecting"
SCREEN_CREDITED: Final = "billing.credited"
SCREEN_REFUNDED: Final = "billing.refunded"
SCREEN_HELD: Final = "billing.held"

#: Refusals shown to the user by the checkout (``BillingError.text``).
ERRORS: Final[Mapping[str, str]] = {
    "not_found": "Заказ не найден. Откройте покупку заново.",
    "not_payable": "Этот заказ уже оплачен или отменён.",
    "plan_unavailable": "Этот тариф сейчас недоступен.",
    "topup_amount": "Неверная сумма пополнения.",
    "topup_currency": "Этот способ оплаты сейчас недоступен. Выберите другой.",
    "no_stars_rate": "Оплата звёздами сейчас недоступна.",
    "order_gone": "Эта покупка больше не ждёт оплаты. Откройте её заново, цена обновится.",
    "stale_price": "Цена этой покупки устарела. Откройте покупку заново, цена обновится.",
    "too_many_invoices": (
        "Слишком много счетов подряд. Оплатите уже созданный счёт или попробуйте через 10 минут."
    ),
}
ERRORS_EN: Final[Mapping[str, str]] = {
    "not_found": "Order not found. Please open the purchase again.",
    "not_payable": "This order is already paid or canceled.",
    "plan_unavailable": "This plan is not available right now.",
    "topup_amount": "Invalid top-up amount.",
    "topup_currency": "This payment method is not available right now. Please choose another one.",
    "no_stars_rate": "Paying with Stars is not available right now.",
    "order_gone": "This purchase is no longer waiting for payment. Open it again and the price will update.",
    "stale_price": "The price of this purchase is out of date. Open it again and the price will update.",
    "too_many_invoices": (
        "Too many invoices in a row. Pay the one you already have or try again in 10 minutes."
    ),
}

ATTENTION: Final[Mapping[str, str]] = {
    "held_title": "Покупка не завершена: подписка заморожена",
    "held_body": (
        "Заказ №{order}: пользователь {user} пополнил баланс на {amount}, но подписка заморожена, "
        "покупка «{title}» не завершена. Решите: «Зачесть в заморозку» или «Вернуть на кошелёк»."
    ),
    "fulfill_held_title": "Оплаченный заказ ждёт решения: подписка заморожена",
    "fulfill_held_body": (
        "Заказ №{order} «{title}» на {amount} оплачен, но подписка пользователя {user} заморожена. Деньги "
        "списаны. Решите: «Зачесть в заморозку» или «Вернуть на кошелёк»."
    ),
    "chargeback_title": "Возврат по пополнению больше остатка на балансе",
    "chargeback_body": (
        "Платёж {payment}: касса вернула {amount}, с баланса пользователя {user} списано {taken}, "
        "не хватило {missing}. Проверьте, не выдана ли уже подписка за эти деньги."
    ),
    "unpriced_title": "Оплата без заказа в чужой валюте",
    "unpriced_body": (
        "Платёж {payment} на {amount} пользователя {user} пришёл без заказа, и его нельзя пересчитать "
        "в валюту магазина. На баланс не зачислено, решите вручную."
    ),
    "fulfill_failed_title": "Не удалось оформить оплаченный заказ",
    "fulfill_failed_body": (
        "Заказ №{order}: {reason}. Деньги ({amount}) возвращены на баланс пользователя {user}."
    ),
    "held_expired_title": "Замороженный заказ отменён автоматически",
    "held_expired_body": (
        "Заказ №{order} «{title}» пользователя {user} ждал решения дольше {days} дн. и отменён; "
        "списанные деньги ({amount}) возвращены на баланс."
    ),
    "test_payment_title": "Тестовый платёж обычного пользователя не зачислен",
    "test_payment_body": (
        "Платёж {payment} на {amount} пользователя {user} пришёл через кассу в тестовом режиме. "
        "Тестовые деньги зачисляются только владельцу и администраторам, на баланс ничего не попало. "
        "Проверьте, не включён ли тестовый режим у рабочей кассы."
    ),
    "refund_amount_title": "Возврат без суммы: списано всё зачисленное",
    "refund_amount_body": (
        "Платёж {payment}: касса сообщила о возврате, но не передала сумму. С баланса пользователя {user} "
        "списано всё зачисленное ({amount}). Если возврат был частичным, верните разницу вручную."
    ),
}

#: User-facing fallback texts by language (``ru`` / ``en``); an owner-made content screen overrides them.
_TEXTS: Final[Mapping[str, Mapping[str, str]]] = {
    "ru": {
        "paid": "✅ Оплачено! Подписка «{title}» действует до {until}.",
        "paid_devices": (
            "✅ Оплачено! Добавлено устройств: {devices}. Подписка «{title}» действует до {until}."
        ),
        "paid_balance": "\nНа балансе осталось {balance}.",
        "connecting": "✅ Оплачено, подключаем… Сообщение обновится само, как только всё будет готово.",
        "credited": "💰 Зачислено {amount} на баланс (сейчас {balance}).",
        "credited_late": "\nАвтоматически покупка уже не пройдёт. Купите сами, когда будет удобно:",
        "credited_insufficient": "\nДля покупки «{title}» нужно {price}. Пополните ещё на {missing}.",
        "credited_held": "\nПокупка не завершена: подписка приостановлена. Напишите в поддержку.",
        "held": (
            "✅ Оплата получена, но подписка сейчас приостановлена. Покупка «{title}» ждёт решения "
            "поддержки. Деньги не пропадут: их зачтут в подписку или вернут на баланс."
        ),
        "refunded": (
            "Не получилось оформить «{title}»: {reason}\n{amount} вернулись на баланс (сейчас {balance})."
        ),
        "btn_connect": "🔗 Подключиться",
        "btn_buy": "Купить «{title}» за {price}",
        "btn_topup": "Пополнить на {missing}",
        "btn_menu": "Меню",
    },
    "en": {
        "paid": "✅ Paid! Your «{title}» subscription is active until {until}.",
        "paid_devices": (
            "✅ Paid! Devices added: {devices}. Your «{title}» subscription is active until {until}."
        ),
        "paid_balance": "\nBalance left: {balance}.",
        "connecting": (
            "✅ Paid, setting things up… This message will update as soon as everything is ready."
        ),
        "credited": "💰 {amount} added to your balance (now {balance}).",
        "credited_late": "\nThe order is no longer waiting, so buy it yourself whenever you like:",
        "credited_insufficient": "\n«{title}» costs {price}. Top up {missing} more.",
        "credited_held": (
            "\nThe purchase did not go through: your subscription is suspended. Please contact support."
        ),
        "held": (
            "✅ Payment received, but your subscription is suspended right now. The «{title}» purchase is "
            "waiting for support. Your money is safe: it will go to the subscription or back to your "
            "balance."
        ),
        "refunded": (
            "Could not complete «{title}»: {reason}\n{amount} returned to your balance (now {balance})."
        ),
        "btn_connect": "🔗 Connect",
        "btn_buy": "Buy «{title}» for {price}",
        "btn_topup": "Top up {missing}",
        "btn_menu": "Menu",
    },
}
LANGS: Final = ("ru", "en")

#: English of the refusal reasons that end up in «Не получилось оформить «…»: {reason}» (the reason is
#: stored in Russian in the order and in the notice payload; matched as a whole, see
#: :func:`localize_reason`).
REASONS_EN: Final[Mapping[str, str]] = {
    "Заказ повреждён.": "The order is corrupted.",
    "Тариф заказа повреждён.": "The order's plan is corrupted.",
    "Позиция заказа сейчас недоступна.": "An item in the order is not available right now.",
    "Неизвестный вид заказа.": "Unknown order type.",
    "Для этого тарифа докупка устройств недоступна.": "Adding devices is not available for this plan.",
    "Подписка не найдена или закрыта.": "The subscription was not found or is closed.",
    "Пользователь не найден.": "User not found.",
    "Лимит LTE уже обновился — пакет не нужен, деньги вернулись на баланс.": (
        "The LTE limit has already been renewed, so the pack is not needed. The money is back on the balance."
    ),
    "Пакет сейчас недоступен.": "The pack is not available right now.",
    "Предложение устарело — откройте докупку заново.": "The offer is outdated. Open the top-up again.",
    "Этот пакет больше не продаётся.": "This pack is no longer on sale.",
}
_MAX_DEVICES_RU: Final = re.compile(r"^Можно не больше (\d+) устройств на подписку\.$")


def localize_reason(reason: str, lang: str | None) -> str:
    """A stored Russian refusal reason in ``lang``; an unknown reason stays as it is."""
    if lang != "en":
        return reason
    if reason in REASONS_EN:
        return REASONS_EN[reason]
    found = _MAX_DEVICES_RU.match(reason)
    if found:
        return f"No more than {found.group(1)} devices per subscription."
    return reason


#: The Russian texts (kept for callers that read them directly).
_T: Final = _TEXTS["ru"]


def lang_of(language: str | None, default: str | None = None) -> str:
    """The user's language among :data:`LANGS` (``default`` — ``DEFAULT_LANGUAGE`` — otherwise ``ru``)."""
    for value in (language, default):
        code = (value or "").strip().lower()[:2]
        if code in LANGS:
            return code
    return "ru"


def _tx(lang: str) -> Mapping[str, str]:
    return _TEXTS.get(lang) or _T


DEFAULT_TZ: Final = "Europe/Moscow"


def money(amount_minor: int, currency: str, lang: str = "ru") -> str:
    return format_money(amount_minor, currency, lang, nbsp=True)


def fmt_date(value: datetime | None, tz: str | None = None) -> str:
    """``12.11.2026`` in the shop's time zone (``TIMEZONE``); «—» for none."""
    if value is None:
        return "—"
    try:
        zone = ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TZ)
    return value.astimezone(zone).strftime("%d.%m.%Y")


def _menu(lang: str = "ru") -> tuple[Button, ...]:
    return (Button(_tx(lang)["btn_menu"], action="menu"),)


def paid_notice(
    *,
    title: str,
    until: datetime | None,
    subscription_url: str,
    balance_minor: int,
    currency: str,
    tz: str | None,
    devices: int | None = None,
    lang: str = "ru",
) -> Notice:
    """«✅ Оплачено …» + «🔗 Подключиться» (the subscription page as a WebApp); ``devices``: an addon."""
    tx = _tx(lang)
    until_text = fmt_date(until, tz)
    if devices:
        text = tx["paid_devices"].format(title=title, until=until_text, devices=devices)
    else:
        text = tx["paid"].format(title=title, until=until_text)
    if balance_minor > 0:
        text += tx["paid_balance"].format(balance=money(balance_minor, currency, lang))
    return Notice(
        SCREEN_PAID,
        text,
        ((Button(tx["btn_connect"], web_app=subscription_url),), _menu(lang)),
        {"title": title, "until": until_text, "subscription_url": subscription_url, "devices": devices or 0},
    )


def connecting_notice(*, title: str, lang: str = "ru") -> Notice:
    return Notice(SCREEN_CONNECTING, _tx(lang)["connecting"], (), {"title": title})


def held_notice(*, title: str, lang: str = "ru") -> Notice:
    """A paid purchase waits for the owner's decision (the user's subscription is frozen / the user is
    banned): the money is safe."""
    return Notice(SCREEN_HELD, _tx(lang)["held"].format(title=title), (_menu(lang),), {"title": title})


def credited_notice(
    *,
    amount_minor: int,
    balance_minor: int,
    currency: str,
    reason: str | None = None,
    order_id: int | None = None,
    title: str | None = None,
    price_minor: int | None = None,
    lang: str = "ru",
) -> Notice:
    """«💰 Зачислено X ₽ на баланс (сейчас Y ₽)» + what to do next. ``reason``: ``late`` (window over or the
    purchase was replaced), ``insufficient`` (still not enough), ``held`` (frozen), ``None`` (plain
    top-up)."""
    tx = _tx(lang)
    params: dict[str, Any] = {
        "amount": money(amount_minor, currency, lang),
        "balance": money(balance_minor, currency, lang),
        "reason": reason or "",
    }
    text = tx["credited"].format(**params)
    rows: list[tuple[Button, ...]] = []
    if reason == "held":
        text += tx["credited_held"]
    elif order_id is not None and title and price_minor is not None:
        price = money(price_minor, currency, lang)
        params.update(title=title, price=price, order_id=order_id)
        if reason == "insufficient":
            missing = max(0, price_minor - balance_minor)
            params["missing"] = money(missing, currency, lang)
            text += tx["credited_insufficient"].format(title=title, price=price, missing=params["missing"])
            rows.append(
                (
                    Button(
                        tx["btn_topup"].format(missing=params["missing"]),
                        action="topup",
                        params={"order_id": order_id},
                    ),
                )
            )
        else:
            if reason == "late":
                text += tx["credited_late"]
            rows.append(
                (
                    Button(
                        tx["btn_buy"].format(title=title, price=price),
                        action="reorder",
                        params={"order_id": order_id},
                    ),
                )
            )
    rows.append(_menu(lang))
    return Notice(SCREEN_CREDITED, text, tuple(rows), params)


def refunded_notice(
    *, title: str, reason: str, amount_minor: int, balance_minor: int, currency: str, lang: str = "ru"
) -> Notice:
    text = _tx(lang)["refunded"].format(
        title=title,
        reason=localize_reason(reason, lang),
        amount=money(amount_minor, currency, lang),
        balance=money(balance_minor, currency, lang),
    )
    return Notice(SCREEN_REFUNDED, text, (_menu(lang),), {"title": title, "reason": reason})
