"""«👤 Пользователи»: smart search and the user card with every operation (04 §9, §9.1; 01 §1.6).

Screens (code-defined, on the :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``au.find`` — asks for a query (form ``au.f.find``); ``au.q`` — results for a query (one hit → the card);
  :meth:`UserScreens.search_view` serves the admin root, where a typed ID or a forwarded message is a query;
* ``au.new`` (newest users), ``au.paid`` (latest payments), ``au.ban`` (banned users): 20 rows, one SQL each;
* ``au.days`` — «➕ Дни» with ready values (+1 … +90, −1, −7) or «✏️ Своё», then the reason;
* ``au`` — the card: who, role, status, balance (not for Support), subscription, money counters; buttons by
  the viewer's rights: ``±дни``, «Выдать тариф», «Баланс», «Устройства», «Новая ссылка», «Написать»,
  «Заблокировать»/«Разблокировать», histories, «Роль» (owner);
* ``au.pay`` payments, ``au.ord`` orders, ``au.led`` wallet ledger (not for Support), ``au.ev`` events
  (subscription journal + admin actions on the user); ``au.pl`` — plan choice for «Выдать тариф»;
  ``au.cf`` — «Сбросить устройства?» / «Перевыпустить ссылку?» confirmation.

Access: the router checks the viewer's cached role on **every** screen, action and form step (04 §9.1 matrix),
and every operation re-checks the role in the database inside its own transaction
(:mod:`svbg.tg.admin.users.ops`). Callback arguments are re-validated here (they can be forged).

Commands (private chat, staff): ``/user [запрос]``, ``/find [запрос]``. In the admin group the button
:func:`card_button` (``auc:<id>``) opens the card in the presser's private chat — after a fresh role check;
a group member who is not staff of the bot gets «Нет прав» and the attempt is audited.

Click budget: the card, the histories and the confirmations are one SQL each; nothing calls the panel.
"""

from __future__ import annotations

import html
import logging
import re
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, Message

from svbg.core.clock import now
from svbg.core.money import format_money, parse_money
from svbg.core.tables import users
from svbg.services import roles
from svbg.services.roles import Act, Actor
from svbg.subscriptions.lifecycle import MAX_DAYS
from svbg.tg.admin.users import queries
from svbg.tg.admin.users.ops import OpResult, UserOps, signed_money
from svbg.tg.admin.users.search import QueryError, parse_query, search
from svbg.tg.ui.codec import ACTION_OPEN
from svbg.tg.ui.forms import Field, Form, ValidationError, integer
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "ADMIN_HOME",
    "GROUP_PREFIX",
    "ROLE_SCREEN",
    "SCREEN_BANNED",
    "SCREEN_CARD",
    "SCREEN_DAYS",
    "SCREEN_FIND",
    "SCREEN_NEW",
    "SCREEN_PAID",
    "SCREEN_RESULTS",
    "UserScreens",
    "card_button",
    "installed_on",
]

log = logging.getLogger("svbg.tg.admin.users")

ADMIN_HOME: Final = "adm"  # svbg.tg.admin.nav.ROOT
USERS_HUB: Final = "adm.u"  # svbg.tg.admin.nav.HUB_USERS
ROLE_SCREEN: Final = "rl"  # svbg.tg.admin.roles.SCREEN_EDIT
SCREEN_FIND: Final = "au.find"
SCREEN_RESULTS: Final = "au.q"
SCREEN_CARD: Final = "au"
SCREEN_PAYMENTS: Final = "au.pay"
SCREEN_ORDERS: Final = "au.ord"
SCREEN_LEDGER: Final = "au.led"
SCREEN_EVENTS: Final = "au.ev"
SCREEN_PLANS: Final = "au.pl"
SCREEN_CONFIRM: Final = "au.cf"
SCREEN_DAYS: Final = "au.days"
SCREEN_NEW: Final = "au.new"
SCREEN_PAID: Final = "au.paid"
SCREEN_BANNED: Final = "au.ban"
ACTIONS: Final = "aua"
GROUP_PREFIX: Final = "auc"

F_FIND: Final = "au.f.find"
F_DAYS: Final = "au.f.days"
F_DAYS_REASON: Final = "au.f.dayr"  # a ready value was tapped: only the reason is asked
F_PLAN: Final = "au.f.plan"
F_WALLET: Final = "au.f.wal"
F_BAN: Final = "au.f.ban"
F_UNBAN: Final = "au.f.unban"
F_MSG: Final = "au.f.msg"

_ID_RE: Final = re.compile(r"^\d{1,18}$")
_GROUP_RE: Final = re.compile(rf"^{GROUP_PREFIX}:(\d{{1,18}})$")
_PAY_ID_RE: Final = re.compile(r"^[0-9a-f-]{36}$")
_DENIED_AUDIT_S: Final = 60.0
_LAST_CARD_MAX: Final = 1000
DAY_PRESETS: Final[tuple[int, ...]] = (1, 3, 7, 30, 90, -1, -7)
LIST_LIMIT: Final = 20

_INSTALLED: weakref.WeakKeyDictionary[Any, UserScreens] = weakref.WeakKeyDictionary()


def installed_on(router: Any) -> UserScreens | None:
    """The user screens installed on ``router`` (the admin root searches through them)."""
    return _INSTALLED.get(router)


_T: Final[dict[str, str]] = {
    "find_prompt": "🔍 Кого ищем? Пришлите ID, @username, имя, ссылку подписки, shortUuid или номер платежа.",
    "results": "🔍 Найдено по «{q}»: {n}",
    "results_more": "Показаны первые {n}. Уточните запрос, если нужного нет.",
    "nothing": "🔍 По «{q}» никого не нашлось.",
    "again": "🔍 Искать ещё",
    "admin": "⬅️ Админка",
    "users_hub": "⬅️ Пользователи",
    "admin_root": "🛠 Админка",
    "to_card": "⬅️ К карточке",
    "not_found": "Пользователь не найден",
    "more": "Дальше ➡️",
    "refresh": "🔄 Обновить",
    "no_name": "без имени",
    "card_title": "👤 <b>{name}</b>{handle}",
    "card_ids": "Telegram ID: <code>{tg}</code> · № {uid}",
    "card_role": "Роль: {role}",
    "card_dates": "С нами с {created} · был(а) {seen}",
    "card_banned": "⛔️ Заблокирован(а) с {at}",
    "card_bot_blocked": "🔕 Заблокировал(а) бота {at}",
    "card_no_captcha": "🧩 Капчу ещё не прошёл(а)",
    "card_wallet": "💰 Баланс: <b>{balance}</b>",
    "card_paid": "💳 Оплат: {n} на {total}{last}",
    "card_paid_last": ", последняя {at}",
    "card_paid_support": "💳 Оплат: {n}",
    "card_no_sub": "📶 Подписки нет",
    "sub_active": "📶 🟢 Подписка до {until} ({left})",
    "sub_trial": "📶 🎁 Пробная до {until} ({left})",
    "sub_expired": "📶 ⛔️ Подписка истекла {until}",
    "sub_frozen": "📶 ⏸ Подписка приостановлена",
    "sub_pending": "📶 ⏳ Подключается к панели",
    "sub_closed": "📶 Подписка закрыта",
    "sub_disabled": "Отключена в панели: {reason}",
    "sub_plan": "Тариф: {plan}",
    "sub_devices": "Устройства: {devices}",
    "sub_traffic": "Трафик: {used} из {limit}",
    "sub_panel": "Панель: <code>{username}</code>{short}",
    "sub_link": "Ссылка: {url}",
    "days_left": "{n} дн.",
    "today": "сегодня",
    "unlimited": "безлимит",
    "devices_unlimited": "без лимита",
    "devices_panel": "по умолчанию панели",
    "b_days": "➕ Дни",
    "b_copy_link": "📋 Ссылка",
    "b_custom": "✏️ Своё",
    "days_title": "➕ <b>Дни</b> · {who}\n\nСколько дней добавить? Минус убавляет. Причину спрошу дальше.",
    "days_picked": "➕ {days} дн.",
    "b_find": "🔍 Найти",
    "new_hint": "Последние 20 человек, которые пришли в бот.",
    "paid_hint": "Последние оплаты, без тестовых и перенесённых из другого бота.",
    "ban_hint": "Кого заблокировали в боте, сначала последние.",
    "list_empty": "Пока никого.",
    "b_plan": "🎁 Выдать тариф",
    "b_wallet": "💰 Баланс",
    "b_ledger": "👛 Движения",
    "b_devices": "📱 Сбросить устройства",
    "b_reissue": "🔗 Новая ссылка",
    "b_message": "✉️ Написать",
    "b_ban": "🚫 Заблокировать",
    "b_unban": "✅ Разблокировать",
    "b_payments": "💳 Оплаты",
    "b_orders": "🧾 Заказы",
    "b_events": "📜 События",
    "b_role": "🎖 Роль",
    "b_yes": "✅ Да",
    "b_no": "⬅️ Нет",
    "cf_devices": "📱 Сбросить все устройства пользователя?\n\nОн подключится заново на каждом устройстве.",
    "cf_reissue": "🔗 Перевыпустить ссылку подписки?\n\n"
    "Старая ссылка перестанет работать на всех устройствах.",
    "pay_title": "💳 <b>Оплаты</b> · {who}",
    "ord_title": "🧾 <b>Заказы</b> · {who}",
    "led_title": "👛 <b>Движения по балансу</b> · {who}",
    "ev_title": "📜 <b>События</b> · {who}",
    "empty": "Пока ничего нет.",
    "plans_title": "🎁 <b>Выдать тариф</b> · {who}\n\nВыберите тариф. Срок и причину спрошу дальше.",
    "plans_empty": "Тарифов пока нет — создайте их в «📦 Тарифы».",
    "f_days": "➕ Сколько дней добавить? Отрицательное число — убавить (например, 7 или -3).",
    "f_plan_days": "📅 На сколько дней выдать тариф «{plan}»?",
    "f_reason": "📝 Причина (обязательно, её увидит владелец в отчёте):",
    "f_wallet": "💰 Сумма в {cur}: со знаком «-», чтобы списать (например, 150 или -99,50).",
    "f_ban": "🚫 Почему блокируем? (причина обязательна)",
    "f_unban": "✅ Почему разблокируем? (причина обязательна)",
    "f_msg": "✉️ Текст сообщения пользователю (до 3500 символов):",
    "ok": "✅ {text}",
    "fail": "⚠️ {text}",
    "group_opened": "Карточка открыта в личке с ботом",
    "group_no_dm": "Напишите боту в личку /start — тогда карточка откроется там",
}

PAY_STATUS: Final[Mapping[str, str]] = {
    "pending": "⏳ ждёт",
    "paid": "✅ оплачен",
    "expired": "⌛️ истёк",
    "canceled": "✖️ отменён",
    "failed": "❌ ошибка",
    "refunded": "↩️ возврат",
    "mismatch": "⚠️ сумма не совпала",
}
ORDER_KIND: Final[Mapping[str, str]] = {
    "new": "покупка",
    "renew": "продление",
    "change": "смена тарифа",
    "addon_devices": "устройства",
    "topup": "пополнение",
}
ORDER_STATUS: Final[Mapping[str, str]] = {
    "draft": "черновик",
    "awaiting_funds": "ждёт денег",
    "awaiting_payment": "ждёт оплаты",
    "paid": "оплачен",
    "fulfilled": "выполнен",
    "credited": "зачислено",
    "canceled": "отменён",
    "expired": "истёк",
    "held": "задержан",
}
LEDGER_REASON: Final[Mapping[str, str]] = {
    "topup": "пополнение",
    "purchase": "покупка",
    "refund": "возврат",
    "bonus": "бонус",
    "promo": "промокод",
    "admin_adjust": "корректировка",
    "import_opening": "перенос",
}
EVENT_KIND: Final[Mapping[str, str]] = {
    "purchase_new": "покупка",
    "purchase_renew": "продление",
    "trial_converted": "триал → оплата",
    "plan_changed": "смена тарифа",
    "devices_added": "доп. устройства",
    "admin_grant": "дни от админа",
    "extended": "дни добавлены",
    "reissue_requested": "перевыпуск ссылки",
    "devices_reset_requested": "сброс устройств",
    "closed": "закрыта",
    "subs.grant": "выданы дни",
    "subs.give_plan": "выдан тариф",
    "wallet.adjust": "баланс изменён",
    "user.ban": "блокировка",
    "user.unban": "разблокировка",
    "user.devices_reset": "сброс устройств",
    "user.reissue": "перевыпуск ссылки",
    "user.message": "сообщение",
    "role.set": "роль изменена",
}
_TITLES: Final[Mapping[str, str]] = {
    SCREEN_NEW: "🆕 Новые",
    SCREEN_PAID: "💳 Недавно оплатили",
    SCREEN_BANNED: "⛔ Заблокированные",
}
DISABLED_REASON: Final[Mapping[str, str]] = {
    "BOT_BAN": "блок в боте",
    "admin": "вручную",
    "ip_guard": "IP Guard",
    "channel_left": "вышел из канала",
    "closed": "закрыта",
    "hold": "заморозка",
}


def card_button(user_id: int, text: str = "👤 Карточка") -> InlineKeyboardButton:
    """A button for admin-group cards (receipts, alerts): opens the user card in the presser's private
    chat."""
    return InlineKeyboardButton(text=text, callback_data=f"{GROUP_PREFIX}:{int(user_id)}")


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _actor_of(user: UserCtx) -> Actor:
    return Actor(user.user_id, user.telegram_id, user.role, user.perms)


def _can(user: UserCtx, act: Act) -> bool:
    return roles.authorize(_actor_of(user), act)


def _uid(arg: Any) -> int | None:
    if isinstance(arg, str) and _ID_RE.match(arg):
        return int(arg)
    if isinstance(arg, int) and not isinstance(arg, bool) and arg > 0:
        return arg
    return None


def _split(arg: Any, n: int) -> list[str] | None:
    if not isinstance(arg, str):
        return None
    parts = arg.split(":", n - 1)
    return parts if len(parts) == n else None


def _gb(n: int) -> str:
    value = n / 1024**3
    return f"{value:.1f}".replace(".", ",").removesuffix(",0") + " ГБ"


def _signed_days(value: str) -> int:
    raw = value.strip().replace("−", "-").replace(" ", "")
    n = integer(min_value=-MAX_DAYS, max_value=MAX_DAYS)(raw)
    if n == 0:
        raise ValidationError("Нужно число, отличное от нуля")
    return int(n)


def _reason(value: str) -> str:
    try:
        return roles.require_reason(value)
    except roles.RoleError as e:
        raise ValidationError(e.text) from None


def _query_validator(value: str) -> str:
    try:
        parse_query(value)
    except QueryError as e:
        raise ValidationError(str(e)) from None
    return value.strip()


class UserScreens:
    """Registers the search and the user card on a router (see module docstring)."""

    def __init__(
        self,
        router: ScreenRouter,
        db: Database,
        ops: UserOps,
        *,
        currency: Callable[[], str] = lambda: "RUB",
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
        owner_ids: Callable[[], Awaitable[frozenset[int]]] | None = None,
        invalidate: Callable[[int | None], None] | None = None,
        plans: Callable[[], Any] = lambda: None,
        lang: str = "ru",
    ) -> None:
        self.router = router
        self.db = db
        self.ops = ops
        self.currency = currency
        self.timezone = timezone
        self.owner_ids = owner_ids
        self.invalidate = invalidate
        self.plans = plans
        self.lang = lang
        self._last_card: dict[int, int] = {}  # viewer user id → last card shown (form «Отмена» returns there)
        self._denied_at: dict[int, float] = {}
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        staff: dict[str, Any] = {"required_role": "support"}
        admin: dict[str, Any] = {"required_role": "admin"}
        grant: dict[str, Any] = {"required_role": "admin", "perm": "subs.grant"}
        money: dict[str, Any] = {"required_role": "admin", "perm": "wallet.adjust"}
        ban: dict[str, Any] = {"required_role": "admin", "perm": "users.ban"}
        stats: dict[str, Any] = {"required_role": "admin", "perm": "stats"}
        _INSTALLED[r] = self
        screens: Sequence[tuple[str, Callable[[ScreenCtx, Any], Awaitable[Any]], dict[str, Any]]] = (
            (SCREEN_FIND, self._find_screen, staff),
            (SCREEN_RESULTS, self._results_screen, staff),
            (SCREEN_CARD, self._card_screen, staff),
            (SCREEN_PAYMENTS, self._payments_screen, staff),
            (SCREEN_ORDERS, self._orders_screen, staff),
            (SCREEN_LEDGER, self._ledger_screen, admin),
            (SCREEN_EVENTS, self._events_screen, staff),
            (SCREEN_PLANS, self._plans_screen, grant),
            (SCREEN_CONFIRM, self._confirm_screen, staff),
            (SCREEN_DAYS, self._days_screen, grant),
            (SCREEN_NEW, self._new_screen, staff),
            (SCREEN_PAID, self._paid_screen, stats),
            (SCREEN_BANNED, self._banned_screen, ban),
        )
        for code, fn, guard in screens:
            r.screen(code, **guard)(fn)
        actions: Sequence[tuple[str, Callable[[ScreenCtx, Any], Awaitable[Any]], dict[str, Any]]] = (
            ("days", self._a_days, grant),
            ("dp", self._a_day_preset, grant),
            ("plan", self._a_plan, grant),
            ("wal", self._a_wallet, money),
            ("ban", self._a_ban, ban),
            ("unban", self._a_unban, ban),
            ("msg", self._a_message, staff),
            ("rd", self._a_reset_devices, staff),
            ("ri", self._a_reissue, staff),
        )
        for name, fn, guard in actions:
            r.action(ACTIONS, name, **guard)(fn)
        reason = Field("reason", _T["f_reason"], _reason)
        forms: Sequence[tuple[Form, dict[str, Any]]] = (
            (Form(F_FIND, (Field("q", _T["find_prompt"], _query_validator),), self._f_find), staff),
            (Form(F_DAYS, (Field("days", _T["f_days"], _signed_days), reason), self._f_days), grant),
            (Form(F_DAYS_REASON, (reason,), self._f_days), grant),
            (
                Form(
                    F_PLAN,
                    (Field("days", "📅 На сколько дней?", integer(min_value=1, max_value=MAX_DAYS)), reason),
                    self._f_plan,
                ),
                grant,
            ),
            (
                Form(
                    F_WALLET,
                    (Field("amount", _T["f_wallet"].format(cur=self.currency()), self._money), reason),
                    self._f_wallet,
                ),
                money,
            ),
            (Form(F_BAN, (Field("reason", _T["f_ban"], _reason),), self._f_ban), ban),
            (Form(F_UNBAN, (Field("reason", _T["f_unban"], _reason),), self._f_unban), ban),
            (
                Form(F_MSG, (Field("text", _T["f_msg"], text_validator(max_len=3500)),), self._f_message),
                staff,
            ),
        )
        for form, guard in forms:
            r.form(
                Form(
                    form.name,
                    form.fields,
                    on_done=form.on_done,
                    on_cancel=self._cancel,
                    required_role=guard["required_role"],
                    perm=guard.get("perm"),
                )
            )

    def aiogram_router(self, name: str = "svbg-admin-users") -> Router:
        """``/user`` and ``/find`` (private chat, staff) and the admin-group «👤 Карточка» button."""
        router = Router(name=name)

        async def on_command(message: Message, command: CommandObject) -> None:
            if not await self.handle_command(message, command.args):
                raise SkipHandler

        async def on_group_button(query: CallbackQuery) -> None:
            await self.handle_group_button(query)

        router.message.register(on_command, Command("user", "find"))
        router.callback_query.register(on_group_button, F.data.startswith(f"{GROUP_PREFIX}:"))
        return router

    async def handle_command(self, message: Message, args: str | None) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /user")
            return False
        if user is None or not _can(user, Act.USERS_VIEW):
            return False
        query = (args or "").strip()
        if query:
            await self.router.show(user, message.chat.id, SCREEN_RESULTS, query[:128], new=True)
        else:
            await self.router.show(user, message.chat.id, SCREEN_FIND, new=True)
        return True

    async def handle_group_button(self, query: CallbackQuery) -> None:
        """«👤 Карточка» in the admin group: fresh role check, then the card in the presser's private chat."""
        match = _GROUP_RE.match(query.data or "")
        answer, alert = roles.DENIED, True
        tg_id = query.from_user.id
        try:
            owners = await self.owner_ids() if self.owner_ids is not None else frozenset()
            async with self.db.tx() as conn:
                actor = await roles.load_actor(conn, telegram_id=tg_id, owner_ids=owners)
                allowed = match is not None and roles.authorize(actor, Act.USERS_VIEW)
                if not allowed and self._may_audit_denial(tg_id):
                    await roles.audit(
                        conn,
                        actor,
                        "access_denied",
                        target=f"group:{GROUP_PREFIX}",
                        details={
                            "telegram_id": tg_id,
                            "chat_id": query.message.chat.id if query.message else None,
                        },
                    )
            if allowed and match is not None:
                user = await self.router.user_loader(query.from_user)
                if user is not None and _can(user, Act.USERS_VIEW):
                    shown = await self.router.show(user, tg_id, SCREEN_CARD, match[1], new=True)
                    answer, alert = (_T["group_opened"], False) if shown else (_T["group_no_dm"], True)
        except Exception:
            log.exception("admin group card button failed")
            answer, alert = "Не получилось, попробуйте ещё раз", False
        try:
            await query.answer(answer, show_alert=alert)
        except TelegramAPIError as exc:
            log.warning("answerCallbackQuery failed: %s", type(exc).__name__)

    def _may_audit_denial(self, tg_id: int) -> bool:
        at = time.monotonic()
        last = self._denied_at.get(tg_id)
        if last is not None and at - last < _DENIED_AUDIT_S:
            return False
        if len(self._denied_at) > _LAST_CARD_MAX:
            self._denied_at.clear()
        self._denied_at[tg_id] = at
        return True

    # ------------------------------------------------------------ formatting

    def _tz(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone())
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return UTC

    def _date(self, value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.astimezone(self._tz()).strftime("%d.%m.%Y")

    def _datetime(self, value: datetime | None) -> str:
        if value is None:
            return "—"
        return value.astimezone(self._tz()).strftime("%d.%m.%Y %H:%M")

    def _money(self, raw: str) -> int:
        value = raw.strip().replace("−", "-")
        sign = 1
        if value[:1] in ("-", "+"):
            sign = -1 if value[0] == "-" else 1
            value = value[1:].strip()
        try:
            amount = parse_money(value, self.currency())
        except ValueError:
            raise ValidationError("Нужна сумма, например 150 или -99,50") from None
        if amount == 0:
            raise ValidationError("Сумма должна быть больше нуля")
        return sign * amount

    def _fmt_money(self, amount: int, currency: str | None = None) -> str:
        cur = currency or self.currency()
        try:
            return format_money(amount, cur, "ru")
        except (ValueError, KeyError):
            return f"{amount} {cur}"

    @staticmethod
    def _who(card: queries.Card) -> str:
        name = (card.first_name or "").strip()[:64] or _T["no_name"]
        handle = f" @{card.username[:64]}" if card.username else ""
        return _esc(name + handle)

    def _remember(self, ctx: ScreenCtx, uid: int) -> None:
        if len(self._last_card) > _LAST_CARD_MAX:
            self._last_card.clear()
        self._last_card[ctx.user.user_id] = uid

    # ------------------------------------------------------------ search

    async def _find_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await ctx.start_form(F_FIND)

    async def _f_find(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._results_screen(ctx, str(data.get("q") or ""))

    async def _results_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        text = arg if isinstance(arg, str) else ""
        view = await self.search_view(ctx, text)
        if view is not None:
            return view
        return View(
            text=_T["nothing"].format(q=_esc(text.strip()[:64])),
            parse_mode="HTML",
            keyboard=[[nav_button(_T["again"], SCREEN_FIND)], self._back_row()],
        )

    async def search_view(self, ctx: ScreenCtx, text: str) -> View | None:
        """The answer to a query: the card (one hit), the list (several), the reason a query is not
        searchable; ``None`` when nobody was found (one SQL)."""
        try:
            q = parse_query(text)
        except QueryError as e:
            return View(
                text=_esc(str(e)),
                parse_mode="HTML",
                keyboard=[[nav_button(_T["again"], SCREEN_FIND)], self._back_row()],
            )
        async with self.db.read() as conn:
            found = await search(conn, q)
        shown = _esc(text.strip()[:64])
        if len(found) == 1:
            return await self.card_view(ctx, found[0].user_id)
        if not found:
            return None
        lines = [_T["results"].format(q=shown, n=len(found))]
        if len(found) >= 10:
            lines.append(_T["results_more"].format(n=len(found)))
        rows: list[list[InlineKeyboardButton]] = []
        for hit in found:
            name = (hit.first_name or "").strip()[:32] or _T["no_name"]
            label = ("⛔️ " if hit.banned_at else "") + name
            if hit.username:
                label += f" @{hit.username[:32]}"
            label += f" · {hit.telegram_id if hit.telegram_id is not None else '#' + str(hit.user_id)}"
            rows.append([nav_button(label[:64], SCREEN_CARD, arg=str(hit.user_id))])
        rows.append([nav_button(_T["again"], SCREEN_FIND)])
        rows.append(self._back_row())
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    @staticmethod
    def _back_row() -> list[InlineKeyboardButton]:
        return [nav_button(_T["users_hub"], USERS_HUB), nav_button(_T["admin_root"], ADMIN_HOME)]

    # ------------------------------------------------------------ card

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        uid = _uid(arg)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        return await self.card_view(ctx, uid)

    async def card_view(self, ctx: ScreenCtx, uid: int, *, note: str | None = None) -> View:
        async with self.db.read() as conn:
            card = await queries.load_card(conn, uid, currency=self.currency())
        if card is None:
            return View(
                text=_T["not_found"],
                keyboard=[[nav_button(_T["again"], SCREEN_FIND)], self._back_row()],
            )
        self._remember(ctx, uid)
        text = self._card_text(ctx.user, card)
        if note:
            text = f"{note}\n\n{text}"
        return View(text=text, parse_mode="HTML", keyboard=self._card_keyboard(ctx.user, card))

    def _card_text(self, viewer: UserCtx, card: queries.Card) -> str:
        name = _esc((card.first_name or "").strip()[:64] or _T["no_name"])
        handle = f" (@{_esc(card.username[:64])})" if card.username else ""
        lines = [
            _T["card_title"].format(name=name, handle=handle),
            _T["card_ids"].format(
                tg=card.telegram_id if card.telegram_id is not None else "—", uid=card.user_id
            ),
        ]
        if card.role != "user":
            role = roles.ROLE_LABELS.get(card.role, card.role)
            if card.role == "admin" and card.perms:
                role += f" ({len(card.perms)} прав)"
            lines.append(_T["card_role"].format(role=role))
        lines.append(
            _T["card_dates"].format(
                created=self._date(card.created_at), seen=self._datetime(card.last_seen_at)
            )
        )
        if card.banned_at is not None:
            lines.append(_T["card_banned"].format(at=self._datetime(card.banned_at)))
        if card.bot_blocked_at is not None:
            lines.append(_T["card_bot_blocked"].format(at=self._date(card.bot_blocked_at)))
        if not card.captcha_passed and card.role == "user":
            lines.append(_T["card_no_captcha"])
        lines.append("")
        if viewer.at_least("admin"):  # 04 §9.1: Support sees no sums of payments and no wallet
            lines.append(_T["card_wallet"].format(balance=_esc(self._fmt_money(card.wallet_minor))))
            last = _T["card_paid_last"].format(at=self._date(card.last_paid_at)) if card.last_paid_at else ""
            lines.append(
                _T["card_paid"].format(
                    n=card.paid_count, total=_esc(self._fmt_money(card.paid_total_minor)), last=last
                )
            )
        else:
            lines.append(_T["card_paid_support"].format(n=card.paid_count))
        lines.append("")
        lines.extend(self._sub_lines(card))
        return "\n".join(lines)

    def _sub_lines(self, card: queries.Card) -> list[str]:
        if card.sub_id is None:
            return [_T["card_no_sub"]]
        at = now()
        if card.link_state in {"closed", "panel_missing"}:
            return [_T["sub_closed"]]
        lines: list[str] = []
        if card.link_state == "pending":
            lines.append(_T["sub_pending"])
        elif card.hold_kind is not None:
            lines.append(_T["sub_frozen"])
        elif card.paid_until is None or card.paid_until <= at:
            lines.append(_T["sub_expired"].format(until=self._date(card.paid_until)))
        else:
            days = int((card.paid_until - at).total_seconds() // 86_400)
            left = _T["days_left"].format(n=days) if days > 0 else _T["today"]
            key = "sub_trial" if card.is_trial else "sub_active"
            lines.append(_T[key].format(until=self._date(card.paid_until), left=left))
        if card.disabled_reason:
            lines.append(_T["sub_disabled"].format(reason=DISABLED_REASON.get(card.disabled_reason, "—")))
        if card.plan_name:
            lines.append(_T["sub_plan"].format(plan=_esc(card.plan_name[:64])))
        if card.device_limit is None:
            devices = _T["devices_panel"]
        elif card.device_limit == 0:
            devices = _T["devices_unlimited"]
        else:
            devices = str(card.device_limit)
            if card.extra_devices:
                devices += f" (из них доп. {card.extra_devices})"
        lines.append(_T["sub_devices"].format(devices=devices))
        if card.traffic_used is not None or card.traffic_limit is not None:
            limit = _T["unlimited"] if not card.traffic_limit else _gb(card.traffic_limit)
            lines.append(_T["sub_traffic"].format(used=_gb(card.traffic_used or 0), limit=limit))
        if card.panel_username:
            short = f" · <code>{_esc(card.short_uuid)}</code>" if card.short_uuid else ""
            lines.append(_T["sub_panel"].format(username=_esc(card.panel_username), short=short))
        if card.subscription_url:
            lines.append(_T["sub_link"].format(url=_esc(card.subscription_url[:200])))
        return lines

    def _card_keyboard(self, viewer: UserCtx, card: queries.Card) -> list[list[InlineKeyboardButton]]:
        uid = str(card.user_id)
        rows: list[list[InlineKeyboardButton]] = []
        pair: list[InlineKeyboardButton] = []

        def add(button: InlineKeyboardButton) -> None:
            pair.append(button)
            if len(pair) == 2:
                rows.append(list(pair))
                pair.clear()

        def flush() -> None:
            if pair:
                rows.append(list(pair))
                pair.clear()

        # An admin never grants days, plans or money to themselves (UserOps refuses it too).
        own = card.user_id == viewer.user_id and viewer.role != "owner"
        if _can(viewer, Act.SUBS_GRANT) and not own:
            if card.live:
                add(nav_button(_T["b_days"], SCREEN_DAYS, arg=uid))
            add(nav_button(_T["b_plan"], SCREEN_PLANS, arg=uid))
        if _can(viewer, Act.WALLET_ADJUST) and not own:
            add(nav_button(_T["b_wallet"], ACTIONS, "wal", uid))
        if viewer.at_least("admin"):
            add(nav_button(_T["b_ledger"], SCREEN_LEDGER, arg=uid))
        flush()
        if card.linked:
            add(nav_button(_T["b_devices"], SCREEN_CONFIRM, arg=f"rd:{uid}"))
            add(nav_button(_T["b_reissue"], SCREEN_CONFIRM, arg=f"ri:{uid}"))
        flush()
        if card.telegram_id is not None:
            add(nav_button(_T["b_message"], ACTIONS, "msg", uid))
        if _can(viewer, Act.USERS_BAN) and card.user_id != viewer.user_id and card.role != "owner":
            if card.banned_at is None:
                add(nav_button(_T["b_ban"], ACTIONS, "ban", uid, style="danger"))
            else:
                add(nav_button(_T["b_unban"], ACTIONS, "unban", uid, style="success"))
        flush()
        rows.append(
            [
                nav_button(_T["b_payments"], SCREEN_PAYMENTS, arg=uid),
                nav_button(_T["b_orders"], SCREEN_ORDERS, arg=uid),
                nav_button(_T["b_events"], SCREEN_EVENTS, arg=uid),
            ]
        )
        if viewer.role == "owner" and card.user_id != viewer.user_id:
            rows.append([nav_button(_T["b_role"], ROLE_SCREEN, arg=uid)])
        if card.subscription_url and card.subscription_url.startswith(("https://", "http://")):
            link = card.subscription_url[:256]
            rows.append([InlineKeyboardButton(text=_T["b_copy_link"], copy_text=CopyTextButton(text=link))])
        rows.append([nav_button(_T["refresh"], SCREEN_CARD, arg=uid), nav_button(_T["again"], SCREEN_FIND)])
        rows.append(self._back_row())
        return rows

    # ------------------------------------------------------------ histories

    async def _header(self, uid: int) -> str | None:
        async with self.db.read() as conn:
            row = (
                await conn.execute(sa.select(users.c.first_name, users.c.username).where(users.c.id == uid))
            ).first()
        if row is None:
            return None
        name = (row.first_name or "").strip()[:48] or _T["no_name"]
        return _esc(name + (f" @{row.username[:48]}" if row.username else ""))

    def _history_view(
        self, title: str, lines: list[str], uid: int, more: InlineKeyboardButton | None
    ) -> View:
        rows: list[list[InlineKeyboardButton]] = []
        if more is not None:
            rows.append([more])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(uid))])
        body = "\n".join(lines) if lines else _T["empty"]
        return View(text=f"{title}\n\n{body}", parse_mode="HTML", keyboard=rows)

    async def _page_arg(self, arg: Any) -> tuple[int, str | None] | None:
        if not isinstance(arg, str):
            return None
        uid_raw, _, cursor = arg.partition(":")
        uid = _uid(uid_raw)
        if uid is None:
            return None
        return uid, cursor or None

    async def _more(self, ctx: ScreenCtx, screen: str, uid: int, cursor: str) -> InlineKeyboardButton:
        data = await ctx.callback(screen, ACTION_OPEN, f"{uid}:{cursor}")
        return InlineKeyboardButton(text=_T["more"], callback_data=data)

    async def _payments_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = await self._page_arg(arg)
        if parsed is None or (parsed[1] is not None and not _PAY_ID_RE.match(parsed[1])):
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        uid, before = parsed
        async with self.db.read() as conn:
            rows = await queries.load_payments(conn, uid, before=before)
        show_sums = ctx.user.at_least("admin")
        page, extra = rows[: queries.PAGE], len(rows) > queries.PAGE
        lines = []
        for p in page:
            status = PAY_STATUS.get(p.status, p.status)
            amount = (
                self._fmt_money(
                    p.paid_amount_minor if p.paid_amount_minor is not None else p.amount_minor, p.currency
                )
                if show_sums
                else "•••"
            )
            test = " · тест" if p.is_test else ""
            lines.append(
                f"{self._datetime(p.created_at)} · {_esc(amount)} · {_esc(p.method[:24])} · {status}{test}\n"
                f"<code>{_esc(p.id)}</code>"
            )
        more = await self._more(ctx, SCREEN_PAYMENTS, uid, page[-1].id) if extra else None
        who = await self._header(uid) or "—"
        return self._history_view(_T["pay_title"].format(who=who), lines, uid, more)

    async def _orders_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = await self._page_arg(arg)
        if parsed is None or (parsed[1] is not None and _uid(parsed[1]) is None):
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        uid, before = parsed
        async with self.db.read() as conn:
            rows = await queries.load_orders(conn, uid, before=int(before) if before else None)
        show_sums = ctx.user.at_least("admin")
        page, extra = rows[: queries.PAGE], len(rows) > queries.PAGE
        lines = []
        for o in page:
            what = ORDER_KIND.get(o.kind, o.kind)
            if o.plan_name:
                what += f" «{_esc(o.plan_name[:32])}»"
            if o.days:
                what += f" {o.days} дн."
            amount = f" · {_esc(self._fmt_money(o.total_minor, o.currency))}" if show_sums else ""
            status = ORDER_STATUS.get(o.status, o.status)
            lines.append(f"{self._datetime(o.created_at)} · №{o.id} · {what}{amount} · {status}")
        more = await self._more(ctx, SCREEN_ORDERS, uid, str(page[-1].id)) if extra else None
        who = await self._header(uid) or "—"
        return self._history_view(_T["ord_title"].format(who=who), lines, uid, more)

    async def _ledger_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = await self._page_arg(arg)
        if parsed is None or (parsed[1] is not None and _uid(parsed[1]) is None):
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        uid, before = parsed
        async with self.db.read() as conn:
            rows = await queries.load_ledger(conn, uid, before=int(before) if before else None)
        page, extra = rows[: queries.PAGE], len(rows) > queries.PAGE
        lines = []
        for e in page:
            note = f" — {_esc(e.note[:60])}" if e.note else ""
            delta = _esc(signed_money(e.amount_minor, e.currency))
            after = _esc(self._fmt_money(e.balance_after, e.currency))
            why = LEDGER_REASON.get(e.reason, _esc(e.reason))
            lines.append(f"{self._datetime(e.created_at)} · {delta} · {why}{note} → {after}")
        more = await self._more(ctx, SCREEN_LEDGER, uid, str(page[-1].id)) if extra else None
        who = await self._header(uid) or "—"
        return self._history_view(_T["led_title"].format(who=who), lines, uid, more)

    async def _events_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parsed = await self._page_arg(arg)
        if parsed is None or (parsed[1] is not None and (_uid(parsed[1]) is None or int(parsed[1]) > 1000)):
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        uid, page_raw = parsed
        page_no = int(page_raw) if page_raw else 0
        async with self.db.read() as conn:
            rows = await queries.load_events(conn, uid, page=page_no)
        show_sums = ctx.user.at_least("admin")
        page, extra = rows[: queries.PAGE], len(rows) > queries.PAGE
        lines = []
        for e in page:
            what = EVENT_KIND.get(e.kind, _esc(e.kind))
            if e.delta_seconds:
                days = round(e.delta_seconds / 86_400)
                if days:
                    what += f" ({'+' if days > 0 else '−'}{abs(days)} дн.)"
            if e.amount_minor is not None and show_sums:
                what += f" ({_esc(signed_money(e.amount_minor, self.currency()))})"
            who = (
                f" · {roles.ROLE_LABELS.get(e.actor_role or '', '')}"
                if e.source == "admin" and e.actor_role
                else ""
            )
            reason = f" — {_esc(e.reason[:80])}" if e.reason else ""
            lines.append(f"{self._datetime(e.ts)} · {what}{who}{reason}")
        more = await self._more(ctx, SCREEN_EVENTS, uid, str(page_no + 1)) if extra else None
        who_line = await self._header(uid) or "—"
        return self._history_view(_T["ev_title"].format(who=who_line), lines, uid, more)

    # ------------------------------------------------------------ plan choice, confirmations

    async def _plans_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        uid = _uid(arg)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        snap = self.plans()
        plans = [p for p in getattr(snap, "plans", ()) if not getattr(p, "is_trial", False)]
        who = await self._header(uid)
        if who is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        rows = [
            [
                nav_button(
                    ("" if p.enabled else "⏸ ") + p.title(self.lang)[:48], ACTIONS, "plan", f"{uid}:{p.id}"
                )
            ]
            for p in plans[:30]
        ]
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(uid))])
        text = _T["plans_title"].format(who=who) if plans else _T["plans_empty"]
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def _confirm_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        parts = _split(arg, 2)
        uid = _uid(parts[1]) if parts else None
        if parts is None or uid is None or parts[0] not in ("rd", "ri"):
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        text = _T["cf_devices"] if parts[0] == "rd" else _T["cf_reissue"]
        return View(
            text=text,
            keyboard=[
                [
                    nav_button(_T["b_yes"], ACTIONS, parts[0], str(uid), style="danger"),
                    nav_button(_T["b_no"], SCREEN_CARD, arg=str(uid)),
                ]
            ],
        )

    # ------------------------------------------------------------ «➕ Дни» with ready values

    async def _days_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        uid = _uid(arg)
        who = await self._header(uid) if uid is not None else None
        if uid is None or who is None:
            return Redirect(USERS_HUB, toast=_T["not_found"])
        self._remember(ctx, uid)
        buttons = [
            nav_button(f"{'+' if n > 0 else '−'}{abs(n)}", ACTIONS, "dp", f"{uid}:{n}") for n in DAY_PRESETS
        ]
        rows = [buttons[:3], buttons[3:5], buttons[5:]]
        rows.append([nav_button(_T["b_custom"], ACTIONS, "days", str(uid))])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(uid))])
        return View(text=_T["days_title"].format(who=who), parse_mode="HTML", keyboard=rows)

    async def _a_day_preset(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = _split(arg, 2)
        uid = _uid(parts[0]) if parts else None
        try:
            days = int(parts[1]) if parts else 0
        except ValueError:
            days = 0
        if uid is None or days not in DAY_PRESETS:
            return Toast(_T["not_found"])
        view = await self._start(ctx, F_DAYS_REASON, uid, days=days)
        if isinstance(view, View):
            shown = f"+{days}" if days > 0 else f"−{abs(days)}"
            view.text = f"{_T['days_picked'].format(days=shown)}\n\n{view.text}"
        return view

    # ------------------------------------------------------------ lists: new, paid, banned

    def _list_view(self, screen: str, hint: str, items: Sequence[tuple[int, str]]) -> View:
        rows = [[nav_button(label[:64], SCREEN_CARD, arg=str(uid))] for uid, label in items]
        rows.append([nav_button(_T["b_find"], SCREEN_FIND)])
        rows.append(self._back_row())
        crumb = f"🛠 Админка › 👥 Пользователи › <b>{_TITLES[screen]}</b>"
        body = hint if items else f"{hint}\n\n{_T['list_empty']}"
        return View(text=f"{crumb}\n\n{body}", parse_mode="HTML", keyboard=rows)

    @staticmethod
    def _person(first_name: str | None, username: str | None) -> str:
        name = (first_name or "").strip()[:28] or _T["no_name"]
        return name + (f" @{username[:28]}" if username else "")

    async def _new_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        async with self.db.read() as conn:
            found = await queries.recent_users(conn, limit=LIST_LIMIT)
        items = [(r.user_id, f"{self._person(r.first_name, r.username)} · {self._date(r.at)}") for r in found]
        return self._list_view(SCREEN_NEW, _T["new_hint"], items)

    async def _paid_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        async with self.db.read() as conn:
            found = await queries.recent_payers(conn, limit=LIST_LIMIT)
        items = [
            (
                r.user_id,
                f"{self._short_dt(r.at)} · {self._person(r.first_name, r.username)} · "
                f"{self._fmt_money(r.amount_minor or 0, r.currency)}",
            )
            for r in found
        ]
        return self._list_view(SCREEN_PAID, _T["paid_hint"], items)

    async def _banned_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        async with self.db.read() as conn:
            found = await queries.banned_users(conn, limit=LIST_LIMIT)
        items = [
            (r.user_id, f"{self._person(r.first_name, r.username)} · с {self._date(r.at)}") for r in found
        ]
        return self._list_view(SCREEN_BANNED, _T["ban_hint"], items)

    def _short_dt(self, value: datetime | None) -> str:
        return "—" if value is None else value.astimezone(self._tz()).strftime("%d.%m %H:%M")

    # ------------------------------------------------------------ actions → forms

    async def _start(self, ctx: ScreenCtx, form: str, uid: int | None, **extra: Any) -> HandlerResult:
        if uid is None:
            return Toast(_T["not_found"])
        self._remember(ctx, uid)
        return await ctx.start_form(form, {"uid": uid, "op": uuid.uuid4().hex, **extra})

    async def _a_days(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._start(ctx, F_DAYS, _uid(arg))

    async def _a_plan(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = _split(arg, 2)
        uid = _uid(parts[0]) if parts else None
        pid = _uid(parts[1]) if parts else None
        snap = self.plans()
        plan = None if pid is None or snap is None else snap.plan(pid)
        if uid is None or plan is None:
            return Toast(_T["not_found"])
        view = await self._start(ctx, F_PLAN, uid, pid=pid)
        if isinstance(view, View):
            view.text = view.text.replace(
                "📅 На сколько дней?", _T["f_plan_days"].format(plan=plan.title(self.lang))
            )
        return view

    async def _a_wallet(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._start(ctx, F_WALLET, _uid(arg))

    async def _a_ban(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._start(ctx, F_BAN, _uid(arg))

    async def _a_unban(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._start(ctx, F_UNBAN, _uid(arg))

    async def _a_message(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._start(ctx, F_MSG, _uid(arg))

    async def _a_reset_devices(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        uid = _uid(arg)
        if uid is None:
            return Toast(_T["not_found"])
        return await self._done(ctx, uid, await self.ops.reset_devices(self._tg(ctx), uid))

    async def _a_reissue(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        uid = _uid(arg)
        if uid is None:
            return Toast(_T["not_found"])
        return await self._done(ctx, uid, await self.ops.reissue(self._tg(ctx), uid))

    # ------------------------------------------------------------ form results

    @staticmethod
    def _tg(ctx: ScreenCtx) -> int:
        tg = ctx.user.telegram_id if ctx.user.telegram_id is not None else getattr(ctx.tg_user, "id", None)
        if tg is None:  # pragma: no cover - every interactive user has a Telegram id
            raise RuntimeError("no telegram id for the acting user")
        return int(tg)

    async def _done(self, ctx: ScreenCtx, uid: int, result: OpResult) -> HandlerResult:
        if result.invalidate is not None and self.invalidate is not None:
            try:
                self.invalidate(result.invalidate)
            except Exception:
                log.exception("user cache invalidation failed")
        if result.denied:
            return View(text=roles.DENIED, keyboard=[[nav_button(_T["admin"], ADMIN_HOME)]])
        note = _T["ok" if result.ok else "fail"].format(text=_esc(result.text))
        return await self.card_view(ctx, uid, note=note)

    @staticmethod
    def _form_uid(data: Mapping[str, Any]) -> int | None:
        return _uid(data.get("uid"))

    @staticmethod
    def _op(data: Mapping[str, Any]) -> str:
        op = data.get("op")
        return op if isinstance(op, str) and re.fullmatch(r"[0-9a-f]{32}", op) else uuid.uuid4().hex

    async def _f_days(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid = self._form_uid(data)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        result = await self.ops.grant_days(
            self._tg(ctx), uid, int(data["days"]), str(data["reason"]), self._op(data)
        )
        return await self._done(ctx, uid, result)

    async def _f_plan(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid, pid = self._form_uid(data), _uid(data.get("pid"))
        if uid is None or pid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        result = await self.ops.give_plan(
            self._tg(ctx), uid, pid, int(data["days"]), str(data["reason"]), self._op(data)
        )
        return await self._done(ctx, uid, result)

    async def _f_wallet(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid = self._form_uid(data)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        result = await self.ops.adjust_wallet(
            self._tg(ctx), uid, int(data["amount"]), str(data["reason"]), self._op(data)
        )
        return await self._done(ctx, uid, result)

    async def _f_ban(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid = self._form_uid(data)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        return await self._done(ctx, uid, await self.ops.ban(self._tg(ctx), uid, str(data["reason"])))

    async def _f_unban(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid = self._form_uid(data)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        return await self._done(ctx, uid, await self.ops.unban(self._tg(ctx), uid, str(data["reason"])))

    async def _f_message(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        uid = self._form_uid(data)
        if uid is None:
            return Redirect(ADMIN_HOME, toast=_T["not_found"])
        return await self._done(ctx, uid, await self.ops.message(self._tg(ctx), uid, str(data["text"])))

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        uid = self._last_card.get(ctx.user.user_id)
        if uid is None:
            return Redirect(ADMIN_HOME)
        return await self.card_view(ctx, uid, note="Отменено.")
