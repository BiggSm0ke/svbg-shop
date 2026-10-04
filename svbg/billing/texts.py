"""User- and owner-facing texts of billing (one place; content screens may override the user ones).

Screen ids (system screens of the screen engine, 07 §2.4.1) are the ``SCREEN_*`` constants; the user path
seeds them with these texts as defaults. Everything is Russian (the bot has no other language).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svbg.billing.ports import Button, Notice
from svbg.core.money import format_money

__all__ = [
    "ATTENTION",
    "ERRORS",
    "SCREEN_CONNECTING",
    "SCREEN_CREDITED",
    "SCREEN_HELD",
    "SCREEN_PAID",
    "SCREEN_REFUNDED",
    "connecting_notice",
    "credited_notice",
    "fmt_date",
    "held_notice",
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

#: User-facing fallback texts; an owner-made content screen overrides them.
_T: Final[Mapping[str, str]] = {
    "paid": "✅ Оплачено! Подписка «{title}» действует до {until}.",
    "paid_devices": "✅ Оплачено! Добавлено устройств: {devices}. Подписка «{title}» действует до {until}.",
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
}


DEFAULT_TZ: Final = "Europe/Moscow"


def money(amount_minor: int, currency: str, _lang: str | None = None) -> str:
    return format_money(amount_minor, currency, nbsp=True)


def fmt_date(value: datetime | None, tz: str | None = None) -> str:
    """``12.11.2026`` in the shop's time zone (``TIMEZONE``); «—» for none."""
    if value is None:
        return "—"
    try:
        zone = ZoneInfo(tz or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(DEFAULT_TZ)
    return value.astimezone(zone).strftime("%d.%m.%Y")


def _menu() -> tuple[Button, ...]:
    return (Button(_T["btn_menu"], action="menu"),)


def paid_notice(
    *,
    title: str,
    until: datetime | None,
    subscription_url: str,
    balance_minor: int,
    currency: str,
    tz: str | None,
    devices: int | None = None,
) -> Notice:
    """«✅ Оплачено …» + «🔗 Подключиться» (the subscription page as a WebApp); ``devices``: an addon."""
    tx = _T
    until_text = fmt_date(until, tz)
    if devices:
        text = tx["paid_devices"].format(title=title, until=until_text, devices=devices)
    else:
        text = tx["paid"].format(title=title, until=until_text)
    if balance_minor > 0:
        text += tx["paid_balance"].format(balance=money(balance_minor, currency))
    return Notice(
        SCREEN_PAID,
        text,
        ((Button(tx["btn_connect"], web_app=subscription_url),), _menu()),
        {"title": title, "until": until_text, "subscription_url": subscription_url, "devices": devices or 0},
    )


def connecting_notice(*, title: str) -> Notice:
    return Notice(SCREEN_CONNECTING, _T["connecting"], (), {"title": title})


def held_notice(*, title: str) -> Notice:
    """A paid purchase waits for the owner's decision (the user's subscription is frozen / the user is
    banned): the money is safe."""
    return Notice(SCREEN_HELD, _T["held"].format(title=title), (_menu(),), {"title": title})


def credited_notice(
    *,
    amount_minor: int,
    balance_minor: int,
    currency: str,
    reason: str | None = None,
    order_id: int | None = None,
    title: str | None = None,
    price_minor: int | None = None,
) -> Notice:
    """«💰 Зачислено X ₽ на баланс (сейчас Y ₽)» + what to do next. ``reason``: ``late`` (window over or the
    purchase was replaced), ``insufficient`` (still not enough), ``held`` (frozen), ``None`` (plain
    top-up)."""
    tx = _T
    params: dict[str, Any] = {
        "amount": money(amount_minor, currency),
        "balance": money(balance_minor, currency),
        "reason": reason or "",
    }
    text = tx["credited"].format(**params)
    rows: list[tuple[Button, ...]] = []
    if reason == "held":
        text += tx["credited_held"]
    elif order_id is not None and title and price_minor is not None:
        price = money(price_minor, currency)
        params.update(title=title, price=price, order_id=order_id)
        if reason == "insufficient":
            missing = max(0, price_minor - balance_minor)
            params["missing"] = money(missing, currency)
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
    rows.append(_menu())
    return Notice(SCREEN_CREDITED, text, tuple(rows), params)


def refunded_notice(
    *, title: str, reason: str, amount_minor: int, balance_minor: int, currency: str
) -> Notice:
    text = _T["refunded"].format(
        title=title,
        reason=reason,
        amount=money(amount_minor, currency),
        balance=money(balance_minor, currency),
    )
    return Notice(SCREEN_REFUNDED, text, (_menu(),), {"title": title, "reason": reason})
