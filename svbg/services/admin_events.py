"""Sales events → admin supergroup topics (07 §2.4.2, §5 «Этап 2»): 💳 Оплаты, 🎁 Триалы, 👤 Новые
пользователи, 📦 Подписки.

Sources (all on the in-process :class:`~svbg.core.bus.EventBus`):

* ``payment.paid`` (payment core, after the crediting commit) → 💳 «Пополнение …»;
* ``order.fulfilled`` (billing, durable through ``subscriptions.event`` jobs) → 💳 «Оплата с баланса …»;
* ``trial.activated`` → 🎁; ``subscription.term_changed`` / ``frozen`` / ``unfrozen`` / ``channel_left`` /
  ``channel_returned`` → 📦;
* :meth:`AdminEvents.new_user` (called by the app from ``/start`` of a user seen for the first time) → 👤 —
  low priority: a burst folds into a digest «👤 +37 новых пользователей за 5 мин» in the admin chat service.

Without a connected admin chat the service falls back to the owners' private chats; there the low-value
streams (👤 new users, 🎁 trials) are not sent at all (``group_ready``) — money and subscription changes are.

Every handler reads at most one row (the user's name and Telegram id) and then only *enqueues* into the admin
chat service (no Telegram I/O here). Names coming from users are HTML-escaped. Nothing here may break the
business flow: a failure is logged and the event is skipped.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING, Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa

from svbg.core.bus import Event, EventBus
from svbg.core.money import format_money
from svbg.core.tables import users

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = ["EVENTS", "AdminEvents", "Poster"]

log = logging.getLogger("svbg.services.admin_events")

K_PAYMENTS: Final = "payments"
K_TRIALS: Final = "trials"
K_NEW_USERS: Final = "new_users"
K_SUBSCRIPTIONS: Final = "subscriptions"

#: Bus events this relay listens to.
EVENTS: Final = (
    "payment.paid",
    "order.fulfilled",
    "trial.activated",
    "subscription.term_changed",
    "subscription.frozen",
    "subscription.unfrozen",
    "subscription.channel_left",
    "subscription.channel_returned",
)

_ORDER_KINDS: Final[Mapping[str, str]] = {
    "new": "покупка подписки",
    "renew": "продление",
    "change": "смена тарифа",
    "addon_devices": "доп. устройства",
}
_TERM_KINDS: Final[Mapping[str, str]] = {
    "purchase_new": "🆕 Новая подписка",
    "purchase_renew": "🔄 Продление",
    "trial_converted": "⭐ Триал → платная подписка",
    "plan_changed": "🔀 Смена тарифа",
    "extended": "➕ Добавлены дни",
    "unfrozen": "▶️ Разморожена",
}
_METHODS: Final[Mapping[str, str]] = {
    "sbp": "СБП",
    "card": "карта",
    "intl_card": "зарубежная карта",
    "crypto": "крипта",
    "stars": "Telegram Stars",
    "wallet": "кошелёк",
    "manual": "перевод",
}


class Poster(Protocol):
    """``AdminChatService.post`` subset."""

    async def post(self, kind: str, text: str, *, html: bool = False) -> Any: ...


#: ``instance_id`` → ``(title, method kind)`` of a payment instance (the payment core's registry).
InstanceInfo = Callable[[int], tuple[str, str | None] | None]


def _money(amount: Any, currency: Any) -> str:
    try:
        return format_money(int(amount), str(currency))
    except (TypeError, ValueError):
        return f"{amount} {currency}"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AdminEvents:
    """See module docstring. ``install(bus)`` subscribes; the returned function unsubscribes."""

    def __init__(
        self,
        db: Database,
        poster: Poster,
        *,
        instance_info: InstanceInfo | None = None,
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
        group_ready: Callable[[], bool] = lambda: True,
    ) -> None:
        self._db = db
        self._poster = poster
        self._instance_info = instance_info
        self._timezone = timezone
        self._group_ready = group_ready

    def install(self, bus: EventBus) -> Callable[[], None]:
        handlers: Mapping[str, Callable[[Event], Awaitable[None]]] = {
            "payment.paid": self._payment_paid,
            "order.fulfilled": self._order_fulfilled,
            "trial.activated": self._trial,
            "subscription.term_changed": self._term_changed,
            "subscription.frozen": self._simple("⏸ Подписка заморожена"),
            "subscription.unfrozen": self._simple("▶️ Подписка разморожена"),
            "subscription.channel_left": self._simple("🚪 Отписка от канала — подписка отключена"),
            "subscription.channel_returned": self._simple("↩️ Вернулся в канал — подписка включена"),
        }
        off = [bus.subscribe(name, self._guarded(fn)) for name, fn in handlers.items()]

        def uninstall() -> None:
            for fn in off:
                fn()

        return uninstall

    def _guarded(self, fn: Callable[[Event], Awaitable[None]]) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            try:
                await fn(event)
            except Exception:  # isolation boundary: a notification never breaks the sale
                log.exception("admin topic notification for %s failed", event.name)

        return handler

    # ------------------------------------------------------------------------------------------ helpers

    async def _who(self, user_id: int | None) -> str:
        """``Аня (@anya, id 123)`` — HTML-escaped; one SQL."""
        if user_id is None:
            return "пользователь ?"
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(users.c.first_name, users.c.username, users.c.telegram_id).where(
                        users.c.id == user_id
                    )
                )
            ).first()
        if row is None:
            return f"пользователь #{user_id}"
        name = escape((row.first_name or "").strip()[:64]) or "без имени"
        parts = [f"@{escape(row.username[:64])}"] if row.username else []
        parts.append(f"id <code>{row.telegram_id}</code>" if row.telegram_id else f"#{user_id}")
        return f"{name} ({', '.join(parts)})"

    def _date(self, value: Any) -> str:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return escape(value[:32])
        if not isinstance(value, datetime):
            return "—"
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
            value = value.astimezone(ZoneInfo(self._timezone()))
        return value.strftime("%d.%m.%Y %H:%M")

    async def _post(self, kind: str, text: str) -> None:
        await self._poster.post(kind, text, html=True)

    # ------------------------------------------------------------------------------------------ handlers

    async def new_user(self, user_id: int) -> None:
        """A user pressed ``/start`` for the first time (low priority: digests in a burst)."""
        if not self._group_ready():
            return
        try:
            await self._post(K_NEW_USERS, f"👤 Новый пользователь: {await self._who(user_id)}")
        except Exception:  # isolation boundary
            log.exception("admin topic notification for a new user failed")

    async def _payment_paid(self, event: Event) -> None:
        p = event.payload
        amount = _money(p.get("amount_minor"), p.get("currency"))
        lines = [f"💰 <b>Пополнение {escape(amount)}</b>", f"👤 {await self._who(_int(p.get('user_id')))}"]
        instance_id = _int(p.get("instance_id"))
        info = self._instance_info(instance_id) if self._instance_info and instance_id is not None else None
        if info is not None:
            title, method = info
            label = _METHODS.get(method or "", method or "")
            lines.append(
                f"Способ: {escape(label)} · {escape(title)}" if label else f"Способ: {escape(title)}"
            )
        lines.append(f"Платёж: <code>{escape(str(p.get('payment_id') or '?'))}</code>")
        await self._post(K_PAYMENTS, "\n".join(lines))

    async def _order_fulfilled(self, event: Event) -> None:
        p = event.payload
        kind = _ORDER_KINDS.get(str(p.get("kind")), str(p.get("kind")))
        amount = _money(p.get("total_minor"), p.get("currency"))
        text = (
            f"🛒 <b>Оплата с баланса: {escape(kind)} — {escape(amount)}</b>\n"
            f"👤 {await self._who(_int(p.get('user_id')))}\n"
            f"Заказ №{_int(p.get('order_id')) or '?'}"
        )
        await self._post(K_PAYMENTS, text)

    async def _trial(self, event: Event) -> None:
        if not self._group_ready():
            return
        p = event.payload
        days = _int(p.get("days")) or 0
        text = (
            f"🎁 Триал на {days} дн.: {await self._who(_int(p.get('user_id')))}\n"
            f"До {self._date(p.get('paid_until'))}"
        )
        await self._post(K_TRIALS, text)

    async def _term_changed(self, event: Event) -> None:
        p = event.payload
        kind = str(p.get("kind") or "")
        title = _TERM_KINDS.get(kind)
        if title is None:
            return  # technical changes (imports, corrections) are not news for the topic
        text = (
            f"{title}: {await self._who(_int(p.get('user_id')))}\n"
            f"Подписка до {self._date(p.get('new_paid_until'))}"
        )
        await self._post(K_SUBSCRIPTIONS, text)

    def _simple(self, title: str) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            user_id = _int(event.payload.get("user_id"))
            await self._post(K_SUBSCRIPTIONS, f"{title}: {await self._who(user_id)}")

        return handler
