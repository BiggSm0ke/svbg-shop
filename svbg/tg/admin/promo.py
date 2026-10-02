"""«🎟 Промокоды» — the owner's promo editor (04 §9.1 право ``promo``; 01 §1.4).

Screens (code-defined, on the :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``prm`` — the list, newest first, 8 per page («🟢 AUTUMN · −20 % на покупку · 3/100»), «➕ Новый»,
  «🔎 Найти по коду»;
* ``prm.k`` — the kind of a new promo; then a short wizard: the code («Пропустить» — generated) and the
  value(s) of the kind; ``plan_gift`` first picks the plan (``prm.g``);
* ``prm.c`` — the card: what it gives, uses («3 из 100», за 7 дней, ждут оплаты), limits, the ``pr_`` deep
  link; buttons for every limit; discounts also get the minimal sum, the allowed plans (``prm.p``) and how
  long an activated discount waits;
* ``prm.d`` — delete (only an unused promo; a used one can only be switched off).

Codes that give money or days right away (:data:`~svbg.promo.rules.VALUE_KINDS`) ask for a reason when they
are created and when their amount, days or limit change; the service holds admins to the per-use limits
(``ADMIN_WALLET_ADJUST_MAX`` / ``ADMIN_GRANT_DAYS_MAX``) and re-reads the role in its transaction.

Access: owner, or admin with ``promo`` — the router checks it on **every** screen, action and form step;
arguments from callbacks are re-validated here (callback data can be forged). Every change writes
``admin_audit`` in its transaction (:class:`~svbg.promo.service.PromoService`). A card costs 2 SQL.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message

from svbg.core.clock import now
from svbg.core.money import format_money, parse_money
from svbg.promo.rules import KIND_TITLES, KINDS, MAX_USES, VALUE_KINDS, Promo, PromoError, check_code
from svbg.promo.service import Actor, PromoService
from svbg.tg.admin import nav
from svbg.tg.ui.forms import Field, Form, ValidationError, integer
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "PERM",
    "SCREEN_CARD",
    "SCREEN_KINDS",
    "SCREEN_LIST",
    "PromoAdminScreens",
]

log = logging.getLogger("svbg.tg.admin.promo")

PERM: Final = "promo"
SCREEN_LIST: Final = "prm"
SCREEN_CARD: Final = "prm.c"
SCREEN_KINDS: Final = "prm.k"
SCREEN_GIFT: Final = "prm.g"
SCREEN_PLANS: Final = "prm.p"
SCREEN_DELETE: Final = "prm.d"
ACTIONS: Final = "prma"
F_NEW: Final = "prm.f.new."  # + kind
F_EDIT: Final = "prm.f.e."  # + field
F_FIND: Final = "prm.f.find"
PAGE_SIZE: Final = 8
_ID_RE: Final = re.compile(r"^\d{1,18}$")
_DATE_RE: Final = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
EDIT_FIELDS: Final = ("days", "percent", "amount", "max", "exp", "min", "hours", "title", "code")
#: Edits of a :data:`VALUE_KINDS` code that need a reason (form ``F_EDIT + field + REASONED``).
REASON_FIELDS: Final = frozenset({"days", "amount", "max"})
REASONED: Final = ".r"
REASON_MIN: Final = 3
REASON_MAX: Final = 500
STATUS_ICONS: Final = {"on": "🟢", "off": "⏸", "scheduled": "🕒", "expired": "⌛", "exhausted": "🈵"}
STATUS_TEXT: Final = {
    "on": "🟢 действует",
    "off": "⏸ выключен",
    "scheduled": "🕒 ещё не начался",
    "expired": "⌛ срок истёк",
    "exhausted": "🈵 использования закончились",
}

_T: Final[dict[str, str]] = {
    "list_title": "🎟 <b>Промокоды</b>",
    "list_empty": "Промокодов пока нет.",
    "list_hint": "Нажмите на промокод, чтобы изменить его.",
    "new": "➕ Новый промокод",
    "find": "🔎 Найти по коду",
    "prev": "◀️",
    "next": "▶️",
    "to_list": "⬅️ К промокодам",
    "to_card": "⬅️ К промокоду",
    "kinds_title": "🎟 <b>Новый промокод</b>\nЧто он даёт?",
    "gift_title": "🎀 <b>Какой тариф подарить?</b>",
    "gift_none": "Тарифов пока нет — создайте тариф в /plans.",
    "plans_title": "📦 <b>Тарифы для скидки</b> · {code}\n"
    "Отмеченные — скидка действует. Ничего не отмечено — на всё.",
    "del_title": "🗑 Удалить промокод <b>{code}</b>? Это нельзя отменить.",
    "del_yes": "🗑 Да, удалить",
    "del_no": "✖️ Отмена",
    "deleted": "🗑 Удалено",
    "saved": "✅ Сохранено",
    "created": "✅ Промокод создан",
    "not_found": "Промокод не найден",
    "b_on": "▶️ Включить",
    "b_off": "⏸ Выключить",
    "b_value": "✏️ Значение",
    "b_max": "🔢 Лимит",
    "b_exp": "📅 Срок",
    "b_once": "👤 Один раз на человека: {v}",
    "b_newu": "🆕 Только новым: {v}",
    "b_min": "💰 Мин. сумма",
    "b_plans": "📦 Тарифы",
    "b_hours": "⏳ Сколько ждёт",
    "b_gift": "🎀 Тариф",
    "b_title": "🏷 Заметка",
    "b_code": "✏️ Код",
    "b_delete": "🗑 Удалить",
    "yes": "да",
    "no": "нет",
    "f_code": "✏️ Код промокода: 3–48 символов, латиница, цифры, «_» и «-» (например, AUTUMN20). "
    "«Пропустить» — придумаю сам.",
    "f_days": "📅 Сколько дней? (например, 7)",
    "f_percent": "🏷 Скидка в процентах, 1–100 (например, 20):",
    "f_amount": "💰 Сумма в {cur} (например, 100):",
    "f_max": "🔢 Сколько раз всего можно использовать? 0 — без ограничения.",
    "f_exp": "📅 До какого дня действует? Дата ДД.ММ.ГГГГ, число дней от сегодня, или 0 — бессрочно.",
    "f_min": "💰 Минимальная сумма заказа в {cur}. 0 — без минимума.",
    "f_hours": "⏳ Сколько часов скидка ждёт оплаты после ввода кода? (например, 72)",
    "f_title": "🏷 Заметка для себя (видна только админам). «-» — убрать.",
    "f_find": "🔎 Код промокода:",
    "f_reason": "📝 Причина (видна в журнале и отчёте), например «розыгрыш в канале»:",
}


class _Stop(Exception):  # control flow, not an error
    def __init__(self, result: HandlerResult) -> None:
        super().__init__("stop")
        self.result = result


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _money(amount: int, currency: str) -> str:
    try:
        return format_money(amount, currency, "ru")
    except (ValueError, KeyError):
        return f"{amount} {currency}"


def _code_validator(raw: str) -> str:
    try:
        return check_code(raw)
    except PromoError as e:
        raise ValidationError(str(e)) from None


def _money_validator(currency: Callable[[], str], *, allow_zero: bool = False) -> Callable[[str], int]:
    def check(raw: str) -> int:
        try:
            value = parse_money(raw, currency())
        except ValueError:
            raise ValidationError("Нужна сумма, например 100 или 99,50") from None
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError("Сумма должна быть больше нуля")
        return value

    return check


def _reason_validator(raw: str) -> str:
    value = " ".join(raw.split())
    if len(value) < REASON_MIN:
        raise ValidationError("Укажите причину (хотя бы пару слов)")
    if len(value) > REASON_MAX:
        raise ValidationError(f"Слишком длинно: максимум {REASON_MAX} символов")
    return value


def _title_validator(raw: str) -> str:
    value = raw.strip()
    if len(value) > 200:
        raise ValidationError("Слишком длинно: максимум 200 символов")
    return "" if value in ("-", "—") else value


class PromoAdminScreens:
    """Registers the promo editor on a router (``install``); ``/promos`` opens it in a private chat."""

    def __init__(
        self,
        router: ScreenRouter,
        service: PromoService,
        *,
        catalog: Any | None = None,
        lang: str = "ru",
    ) -> None:
        self.router = router
        self.service = service
        self.catalog = catalog if catalog is not None else service.catalog
        self.lang = lang
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard = {"required_role": "admin", "perm": PERM}
        screens: dict[str, Callable[[ScreenCtx, Any], Awaitable[Any]]] = {
            SCREEN_LIST: self._list_screen,
            SCREEN_CARD: self._card_screen,
            SCREEN_KINDS: self._kinds_screen,
            SCREEN_GIFT: self._gift_screen,
            SCREEN_PLANS: self._plans_screen,
            SCREEN_DELETE: self._delete_screen,
        }
        for code, fn in screens.items():
            r.screen(code, **guard)(self._wrap(fn))
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "kind": self._a_kind,
            "gpl": self._a_gift_plan,
            "en": self._a_enabled,
            "once": self._a_once,
            "newu": self._a_new_users,
            "pl": self._a_plan_toggle,
            "edit": self._a_edit,
            "del": self._a_delete,
            "find": self._a_find,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(self._wrap(fn))
        cur = self.service.currency
        days = Field("days", _T["f_days"], integer(min_value=1, max_value=3650))
        trial_days = Field("days", _T["f_days"], integer(min_value=1, max_value=365))
        percent = Field("percent", _T["f_percent"], integer(min_value=1, max_value=100))
        amount = Field("amount_minor", _T["f_amount"].format(cur=cur), _money_validator(lambda: cur))
        code = Field("code", _T["f_code"], _code_validator, optional=True)
        reason = Field("reason", _T["f_reason"], _reason_validator)
        new_fields: dict[str, tuple[Field, ...]] = {
            "days": (code, days),
            "percent": (code, percent),
            "fixed": (code, amount),
            "wallet": (code, amount),
            "trial_extend": (code, trial_days),
            "plan_gift": (code, days),
            "wallet_days": (code, amount, days),
        }
        forms = [
            Form(F_NEW + kind, (*fields, reason) if kind in VALUE_KINDS else fields, self._f_new)
            for kind, fields in new_fields.items()
        ]
        edits: dict[str, Field] = {
            "days": Field("value", _T["f_days"], integer(min_value=1, max_value=3650)),
            "percent": Field("value", _T["f_percent"], integer(min_value=1, max_value=100)),
            "amount": Field("value", _T["f_amount"].format(cur=cur), _money_validator(lambda: cur)),
            "max": Field("value", _T["f_max"], integer(min_value=0, max_value=MAX_USES)),
            "exp": Field("value", _T["f_exp"], self._expiry_validator),
            "min": Field(
                "value", _T["f_min"].format(cur=cur), _money_validator(lambda: cur, allow_zero=True)
            ),
            "hours": Field("value", _T["f_hours"], integer(min_value=1, max_value=87_600)),
            "title": Field("value", _T["f_title"], _title_validator),
            "code": Field("value", _T["f_code"].split(" «Пропустить»")[0], _code_validator),
        }
        forms += [Form(F_EDIT + name, (f,), self._f_edit) for name, f in edits.items()]
        forms += [
            Form(F_EDIT + name + REASONED, (edits[name], reason), self._f_edit)
            for name in sorted(REASON_FIELDS)
        ]
        forms.append(Form(F_FIND, (Field("code", _T["f_find"], text_validator(max_len=64)),), self._f_find))
        for form in forms:
            r.form(
                Form(
                    form.name,
                    form.fields,
                    on_done=self._wrap(form.on_done),
                    on_cancel=self._cancel,
                    required_role="admin",
                    perm=PERM,
                )
            )

    def _wrap(
        self, fn: Callable[[ScreenCtx, Any], Awaitable[Any]]
    ) -> Callable[[ScreenCtx, Any], Awaitable[Any]]:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result

        run.__name__ = getattr(fn, "__name__", "promo_handler")
        return run

    def aiogram_router(self, name: str = "svbg-promo-admin") -> Router:
        """``/promos`` in a private chat (owner, admin with ``promo``)."""
        router = Router(name=name)

        async def on_command(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        router.message.register(on_command, Command("promos"))
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /promos")
            return False
        if user is None or not (user.at_least("admin") and user.has_perm(PERM)):
            return False
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _actor(ctx: ScreenCtx) -> Actor:
        return Actor(ctx.user.user_id, ctx.user.role, telegram_id=ctx.user.telegram_id)

    def _plans(self) -> list[Any]:
        snap = getattr(self.catalog, "snapshot", None)
        return [p for p in getattr(snap, "plans", ()) if not p.is_trial]

    async def _promo(self, arg: Any, *, screen: bool = True) -> Promo:
        promo = await self.service.get(int(arg)) if isinstance(arg, str) and _ID_RE.match(arg) else None
        if promo is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]) if screen else Toast(_T["not_found"]))
        return promo

    async def _update(self, ctx: ScreenCtx, promo_id: int, **changes: Any) -> Promo:
        try:
            return await self.service.update(promo_id, self._actor(ctx), **changes)
        except PromoError as e:
            raise _Stop(Toast(str(e), alert=True)) from None

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return Redirect(SCREEN_LIST)

    def _expiry_validator(self, raw: str) -> str | None:
        value = raw.strip()
        if value in ("0", "-", "—"):
            return None
        try:
            tz = ZoneInfo(self.service.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
        if value.isdigit():
            n = int(value)
            if not 1 <= n <= 3650:
                raise ValidationError("Число дней: от 1 до 3650")
            day = (now().astimezone(tz) + timedelta(days=n)).date()
        else:
            m = _DATE_RE.match(value)
            if m is None:
                raise ValidationError("Нужна дата ДД.ММ.ГГГГ, число дней или 0")
            try:
                day = datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=tz).date()
            except ValueError:
                raise ValidationError("Такой даты нет") from None
        until = datetime.combine(day, time(23, 59, 59), tzinfo=tz)
        if until <= now():
            raise ValidationError("Дата уже прошла")
        return until.isoformat()  # form data is JSON

    def _date(self, at: datetime) -> str:
        try:
            return at.astimezone(ZoneInfo(self.service.timezone)).strftime("%d.%m.%Y")
        except (ZoneInfoNotFoundError, ValueError):
            return at.strftime("%d.%m.%Y")

    # ------------------------------------------------------------ list

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        page = int(arg) if isinstance(arg, str) and arg.isdigit() and len(arg) < 6 else 0
        promos, total = await self.service.page(page * PAGE_SIZE, PAGE_SIZE)
        at = now()
        lines = [_T["list_title"], "", _T["list_hint"] if total else _T["list_empty"]]
        rows: list[list[InlineKeyboardButton]] = []
        for p in promos:
            uses = f"{p.uses}/{p.max_uses}" if p.max_uses is not None else str(p.uses)
            label = f"{STATUS_ICONS[p.status(at)]} {p.code} · {self.service.describe(p)} · {uses}"
            rows.append([nav_button(label[:64], SCREEN_CARD, arg=str(p.id))])
        pager: list[InlineKeyboardButton] = []
        if page > 0:
            pager.append(nav_button(_T["prev"], SCREEN_LIST, arg=str(page - 1)))
        if (page + 1) * PAGE_SIZE < total:
            pager.append(nav_button(_T["next"], SCREEN_LIST, arg=str(page + 1)))
        if pager:
            rows.append(pager)
        rows.append([nav_button(_T["new"], SCREEN_KINDS), nav_button(_T["find"], ACTIONS, "find")])
        rows.append(nav.back_row(SCREEN_LIST))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _a_find(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await ctx.start_form(F_FIND)

    async def _f_find(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        promo = await self.service.find(str(data.get("code") or ""))
        if promo is None:
            return View(
                text=f"🔎 {_T['not_found']}",
                keyboard=[[nav_button(_T["find"], ACTIONS, "find"), nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        return await self.card_view(promo)

    # ------------------------------------------------------------ card

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return await self.card_view(await self._promo(arg))

    async def card_view(self, promo: Promo, *, note: str | None = None) -> View:
        stats = await self.service.stats(promo.id)
        at = now()
        cur = self.service.currency
        lines = [f"🎟 <b>{_esc(promo.code)}</b> · {STATUS_TEXT[promo.status(at)]}"]
        if note:
            lines.insert(0, note + "\n")
        lines.append(f"Вид: {KIND_TITLES.get(promo.kind, promo.kind)}")
        lines.append(f"Даёт: {_esc(self.service.describe(promo))}")
        limit = (
            f"{promo.uses} из {promo.max_uses}"
            if promo.max_uses is not None
            else f"{promo.uses} (без лимита)"
        )
        used = f"Использовано: {limit} · за 7 дней: {stats.uses_7d}"
        if promo.is_discount:
            used += f" · из них ждут оплаты: {stats.pending}"
        lines.append(used)
        if promo.is_discount and stats.discount_minor:
            lines.append(f"Скидок выдано на {_money(stats.discount_minor, cur)}")
        once = _T["yes"] if promo.once_per_user else _T["no"]
        newu = _T["yes"] if promo.new_users_only else _T["no"]
        lines.append(f"Один раз на человека: {once} · Только новым: {newu}")
        span = f"до {self._date(promo.expires_at)}" if promo.expires_at else "бессрочно"
        if promo.starts_at:
            span = f"с {self._date(promo.starts_at)} " + span
        lines.append(f"Срок: {span}")
        if promo.is_discount:
            mins = _money(promo.min_amount_minor, cur) if promo.min_amount_minor else "нет"
            names = [self.service.plan_title(pid) or f"#{pid}" for pid in promo.plan_ids]
            lines.append(f"Мин. сумма: {mins} · Тарифы: {_esc(', '.join(names)) if names else 'все'}")
            lines.append(f"После ввода скидка ждёт оплаты {promo.pending_hours or 72} ч.")
        if promo.title:
            lines.append(f"Заметка: {_esc(promo.title)}")
        bot = self.router.transport.bot_username
        if promo.linkable and bot:
            lines.append(f"🔗 <code>https://t.me/{bot}?start=pr_{_esc(promo.code)}</code>")
        if promo.source == "import":
            lines.append("Перенесён из Bedolaga")
        pid = str(promo.id)
        rows: list[list[InlineKeyboardButton]] = [
            [nav_button(_T["b_off"] if promo.enabled else _T["b_on"], ACTIONS, "en", pid)],
        ]
        value_field = {"days": "days", "trial_extend": "days", "plan_gift": "days", "percent": "percent"}.get(
            promo.kind, "amount"
        )
        value_row = [nav_button(_T["b_value"], ACTIONS, "edit", f"{pid}:{value_field}")]
        if promo.kind == "wallet_days":
            value_row.append(nav_button("📅 Дни", ACTIONS, "edit", f"{pid}:days"))
        if promo.kind == "plan_gift":
            value_row.append(nav_button(_T["b_gift"], SCREEN_GIFT, arg=pid))
        rows.append(value_row)
        rows.append(
            [
                nav_button(_T["b_max"], ACTIONS, "edit", f"{pid}:max"),
                nav_button(_T["b_exp"], ACTIONS, "edit", f"{pid}:exp"),
            ]
        )
        rows.append([nav_button(_T["b_once"].format(v=once), ACTIONS, "once", pid)])
        rows.append([nav_button(_T["b_newu"].format(v=newu), ACTIONS, "newu", pid)])
        if promo.is_discount:
            rows.append(
                [
                    nav_button(_T["b_min"], ACTIONS, "edit", f"{pid}:min"),
                    nav_button(_T["b_plans"], SCREEN_PLANS, arg=pid),
                    nav_button(_T["b_hours"], ACTIONS, "edit", f"{pid}:hours"),
                ]
            )
        extra = [nav_button(_T["b_title"], ACTIONS, "edit", f"{pid}:title")]
        if promo.uses == 0:
            extra += [
                nav_button(_T["b_code"], ACTIONS, "edit", f"{pid}:code"),
                nav_button(_T["b_delete"], SCREEN_DELETE, arg=pid),
            ]
        rows.append(extra)
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ create

    async def _kinds_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        rows = [[nav_button(KIND_TITLES[k], ACTIONS, "kind", k)] for k in KINDS]
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text=_T["kinds_title"], parse_mode="HTML", keyboard=rows)

    async def _a_kind(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if arg not in KINDS:
            return Redirect(SCREEN_KINDS)
        if arg == "plan_gift":
            return Redirect(SCREEN_GIFT, "new")
        return await ctx.start_form(F_NEW + str(arg), {"kind": arg})

    async def _gift_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        """``arg``: ``new`` (a new gift promo) or a promo id (change its plan)."""
        target = arg if arg == "new" else str((await self._promo(arg)).id)
        plans = self._plans()
        rows = [[nav_button(p.title(self.lang)[:60], ACTIONS, "gpl", f"{target}:{p.id}")] for p in plans]
        back = (
            nav_button(_T["to_list"], SCREEN_KINDS)
            if target == "new"
            else nav_button(_T["to_card"], SCREEN_CARD, arg=target)
        )
        rows.append([back])
        return View(text=_T["gift_title"] if plans else _T["gift_none"], parse_mode="HTML", keyboard=rows)

    async def _a_gift_plan(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        if len(parts) != 2 or not _ID_RE.match(parts[1]) or not (parts[0] == "new" or _ID_RE.match(parts[0])):
            return Toast(_T["not_found"])
        plan_id = int(parts[1])
        if plan_id not in {p.id for p in self._plans()}:
            return Toast(_T["not_found"])
        if parts[0] == "new":
            return await ctx.start_form(F_NEW + "plan_gift", {"kind": "plan_gift", "plan_id": plan_id})
        promo = await self._promo(parts[0], screen=False)
        updated = await self._update(ctx, promo.id, plan_id=plan_id)
        return await self.card_view(updated, note=_T["saved"])

    async def _f_new(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        kind = str(data.get("kind") or "")
        values = {
            k: data.get(k) for k in ("days", "percent", "amount_minor", "plan_id") if data.get(k) is not None
        }
        try:
            promo = await self.service.create(
                self._actor(ctx),
                kind=kind,
                code=data.get("code") or None,
                values=values,
                reason=data.get("reason") or None,
            )
        except PromoError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["new"], SCREEN_KINDS), nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        return await self.card_view(promo, note=_T["created"])

    # ------------------------------------------------------------ edits

    async def _a_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        promo = await self._promo(arg, screen=False)
        return await self.card_view(await self._update(ctx, promo.id, enabled=not promo.enabled))

    async def _a_once(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        promo = await self._promo(arg, screen=False)
        return await self.card_view(await self._update(ctx, promo.id, once_per_user=not promo.once_per_user))

    async def _a_new_users(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        promo = await self._promo(arg, screen=False)
        return await self.card_view(
            await self._update(ctx, promo.id, new_users_only=not promo.new_users_only)
        )

    async def _a_edit(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        if len(parts) != 2 or parts[1] not in EDIT_FIELDS:
            return Toast(_T["not_found"])
        promo = await self._promo(parts[0], screen=False)
        form = F_EDIT + parts[1]
        if promo.kind in VALUE_KINDS and parts[1] in REASON_FIELDS:
            form += REASONED
        return await ctx.start_form(form, {"id": promo.id, "field": parts[1]})

    async def _f_edit(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        promo_id, field, value = data.get("id"), data.get("field"), data.get("value")
        if not isinstance(promo_id, int) or field not in EDIT_FIELDS:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        changes: dict[str, Any] = {
            "days": {"days": value},
            "percent": {"percent": value},
            "amount": {"amount_minor": value},
            "max": {"max_uses": value or None},
            "exp": {"expires_at": _parse_iso(value)},
            "min": {"min_amount_minor": value or None},
            "hours": {"pending_hours": value},
            "title": {"title": value or None},
            "code": {"code": value},
        }[str(field)]
        try:
            updated = await self.service.update(
                promo_id, self._actor(ctx), reason=data.get("reason") or None, **changes
            )
        except PromoError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["to_card"], SCREEN_CARD, arg=str(promo_id))]],
            )
        return await self.card_view(updated, note=_T["saved"])

    # ------------------------------------------------------------ allowed plans

    async def _plans_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._plans_view(await self._promo(arg))

    def _plans_view(self, promo: Promo) -> View:
        rows = [
            [
                nav_button(
                    f"{'✅' if p.id in promo.plan_ids else '▫️'} {p.title(self.lang)}"[:60],
                    ACTIONS,
                    "pl",
                    f"{promo.id}:{p.id}",
                )
            ]
            for p in self._plans()
        ]
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(promo.id))])
        return View(text=_T["plans_title"].format(code=_esc(promo.code)), parse_mode="HTML", keyboard=rows)

    async def _a_plan_toggle(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        if len(parts) != 2 or not _ID_RE.match(parts[1]):
            return Toast(_T["not_found"])
        promo = await self._promo(parts[0], screen=False)
        plan_id = int(parts[1])
        if not promo.is_discount or plan_id not in {p.id for p in self._plans()}:
            return Toast(_T["not_found"])
        try:
            updated = await self.service.toggle_plan(promo.id, plan_id, self._actor(ctx))
        except PromoError as e:
            return Toast(str(e), alert=True)
        return self._plans_view(updated)

    # ------------------------------------------------------------ delete

    async def _delete_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        promo = await self._promo(arg)
        pid = str(promo.id)
        rows = [
            [
                nav_button(_T["del_yes"], ACTIONS, "del", f"{pid}:{promo.version}", style="danger"),
                nav_button(_T["del_no"], SCREEN_CARD, arg=pid),
            ]
        ]
        return View(text=_T["del_title"].format(code=_esc(promo.code)), parse_mode="HTML", keyboard=rows)

    async def _a_delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        if len(parts) != 2 or not _ID_RE.match(parts[1]):
            return Toast(_T["not_found"])
        promo = await self._promo(parts[0], screen=False)
        if promo.version != int(parts[1]):
            return Redirect(SCREEN_CARD, str(promo.id), toast="Промокод уже изменили — проверьте ещё раз")
        try:
            await self.service.delete(promo.id, self._actor(ctx))
        except PromoError as e:
            return Toast(str(e), alert=True)
        return Redirect(SCREEN_LIST, toast=_T["deleted"])


def _parse_iso(value: Any) -> datetime | None:
    """Form data is JSON: a datetime from the validator comes back as an ISO string after a restart."""
    if isinstance(value, datetime) or value is None:
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
