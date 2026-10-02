"""«🔗 Ссылки» — the deep-link builder (07 §2.4.4): target → parameters → link + «Скопировать», QR, stats.

Screens (code-defined, on the :class:`~svbg.tg.ui.router.ScreenRouter`; all need role admin + ``deeplinks``):

* ``dl`` — the list of short links (newest first, 10 per page) with «➕ Новая ссылка»;
* ``dl.n`` — the target of a new link: main menu, a screen, a plan, a top-up, "promo only";
  ``dl.sc`` / ``dl.pl`` — screens a user may open / plans on sale or by link;
* ``dl.d`` — the draft card: target, promo, ad tag, referral code, expiry, limit, title, the equivalent
  *direct* link when the draft is a single part (``p_std``), problems found (unknown promo …) and
  «✅ Создать ссылку»; ``dl.ex`` — expiry presets;
* ``dl.l`` — a link card: the URL with «📋 Скопировать» (``copy_text``), «🔳 QR», on/off, the numbers for
  1/7/30 days.

The draft lives in memory per admin (≤ 2 h; lost on restart — the admin starts again), so rendering the
wizard costs no SQL; a card costs 2 statements (link + stats), creating or switching a link one statement
with its ``admin_audit`` row. The draft carries the code of the future link: a double tap on «Создать» opens
the same link instead of creating two.
"""

from __future__ import annotations

import html
import io
import logging
import time
import zoneinfo
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import segno
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CopyTextButton, InlineKeyboardButton, Message

from svbg.content.defaults import HOME, SeedButton
from svbg.core import clock
from svbg.deeplinks import codec
from svbg.deeplinks.codec import Kind
from svbg.deeplinks.hook import from_app, router_can_open
from svbg.deeplinks.model import Intent, IntentError
from svbg.deeplinks.service import Actor, DeeplinkService, LinkRow, LinkStats, describe
from svbg.tg.admin import nav as admin_nav
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.forms import Field, Form, ValidationError, integer
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import MediaRef, Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "LINKS_BUTTON",
    "PERM",
    "SCREEN_CARD",
    "SCREEN_DRAFT",
    "SCREEN_LIST",
    "LinkBuilder",
    "setup",
]

log = logging.getLogger("svbg.tg.admin.deeplinks")

PERM: Final = "deeplinks"
SCREEN_LIST: Final = "dl"
SCREEN_NEW: Final = "dl.n"
SCREEN_SCREENS: Final = "dl.sc"
SCREEN_PLANS: Final = "dl.pl"
SCREEN_DRAFT: Final = "dl.d"
SCREEN_EXPIRY: Final = "dl.ex"
SCREEN_CARD: Final = "dl.l"
ACTIONS: Final = "dla"

F_TOPUP: Final = "dl.f.topup"
F_PROMO: Final = "dl.f.promo"
F_AD: Final = "dl.f.ad"
F_REF: Final = "dl.f.ref"
F_MAX: Final = "dl.f.max"
F_TITLE: Final = "dl.f.title"

PAGE: Final = 10
MAX_TARGETS: Final = 40
DRAFT_TTL_S: Final = 7200.0
DRAFTS_MAX: Final = 1000
EXPIRY_DAYS: Final = (1, 7, 30, 90)
MAX_USES_LIMIT: Final = 1_000_000

#: The entry on the home screen for admins (``svbg.content.defaults``; added by the integration step).
LINKS_BUTTON: Final = SeedButton(
    system_key="deeplinks",
    label={"ru": "🔗 Ссылки", "en": "🔗 Links"},
    action={"type": "screen", "target": SCREEN_LIST},
    row=9,
    sort=2,
    visible_if={"role": {"gte": "admin"}},
)

_SCREEN_LABELS: Final = {
    "home": "🏠 Главная",
    "buy": "🛒 Покупка",
    "bal": "💰 Баланс",
    "connect": "🔗 Подключение",
    "dev": "📱 Устройства",
    "lang": "🌐 Язык",
    "chan": "📣 Канал",
}

_T: Final[dict[str, str]] = {
    "list_title": "🔗 <b>Ссылки</b>",
    "list_hint": "Ссылка открывает любой раздел бота и может сразу применить промокод. "
    "Переходы считаются с первого дня.",
    "list_empty": "Ссылок пока нет.",
    "list_page": "Стр. {page} из {pages}",
    "new": "➕ Новая ссылка",
    "settings": "⚙️ Настройки ссылок",
    "back": "⬅️ Назад",
    "prev": "◀️",
    "next": "▶️",
    "to_list": "⬅️ К ссылкам",
    "cancel": "✖️ Отмена",
    "target_title": "🎯 <b>Куда ведёт ссылка?</b>",
    "t_home": "🏠 Главное меню",
    "t_screen": "📄 Экран",
    "t_plan": "📦 Тариф",
    "t_topup": "💳 Пополнение",
    "t_promo": "🎟 Только промокод",
    "screens_title": "📄 <b>Какой экран открыть?</b>",
    "screens_empty": "Подходящих экранов нет.",
    "plans_title": "📦 <b>Какой тариф открыть?</b>",
    "plans_empty": "Тарифов в продаже нет.",
    "plan_link_only": " 🔒",
    "draft_title": "🔗 <b>Новая ссылка</b>",
    "d_promo": "🎟 Промокод: {v}",
    "d_ad": "🏷 Метка: {v}",
    "d_ref": "🤝 Реферальный код: {v}",
    "d_exp": "⏳ Срок: {v}",
    "d_max": "🔢 Лимит: {v}",
    "d_title": "✏️ Название: {v}",
    "none": "—",
    "exp_none": "бессрочно",
    "exp_days": "{n} дн. после создания",
    "max_none": "без лимита",
    "max_n": "{n} человек",
    "direct": "⚡ Прямая ссылка (без лимитов и срока):\n<code>{url}</code>",
    "problems": "⚠️ {lines}",
    "b_target": "🎯 Цель",
    "b_promo": "🎟 Промокод",
    "b_ad": "🏷 Метка",
    "b_ref": "🤝 Реф. код",
    "b_exp": "⏳ Срок",
    "b_max": "🔢 Лимит",
    "b_title": "✏️ Название",
    "b_create": "✅ Создать ссылку",
    "b_copy_direct": "📋 Прямая",
    "exp_title": "⏳ <b>Сколько действует ссылка?</b>",
    "exp_forever": "♾ Бессрочно",
    "exp_n": "{n} дн.",
    "stale": "Черновик устарел — начните заново",
    "created": "✅ Ссылка создана",
    "card_status_on": "🟢 работает",
    "card_status_off": "⏸ выключена",
    "card_status_expired": "⌛ срок истёк",
    "card_status_used_up": "🔚 лимит исчерпан",
    "card_status": "Статус: {v}",
    "card_uses": "Пришло людей: {uses}{max}",
    "card_until": "Действует до: {v}",
    "card_stats": "📊 За 1 / 7 / 30 дн.\nПереходы: {h}\nЛюди: {u}\nНовые: {n}",
    "card_no_username": "Имя бота пока неизвестно — параметр ссылки: <code>{payload}</code>",
    "b_copy": "📋 Скопировать",
    "b_qr": "🔳 QR",
    "b_off": "⏸ Выключить",
    "b_on": "▶️ Включить",
    "turned_on": "▶️ Ссылка включена",
    "turned_off": "⏸ Ссылка выключена",
    "gone": "Ссылки нет",
    "qr_caption": "🔳 QR ссылки «{title}»\n{url}",
    "qr_no_url": "Имя бота неизвестно — QR не собрать",
    "f_topup": "Сумма пополнения в {cur}, целое число (например, 500):",
    "f_promo": "Промокод (латиница, цифры, «_» и «-»). «Пропустить» — убрать промокод:",
    "f_ad": "Рекламная метка — код из раздела рекламы. «Пропустить» — убрать метку:",
    "f_ref": "Реферальный код пользователя. «Пропустить» — убрать:",
    "f_max": "Сколько человек могут прийти по ссылке? «Пропустить» — без лимита:",
    "f_title": "Название ссылки для статистики (до 64 символов):",
    "bad_code": "Только латиница, цифры, «_» и «-», до 62 символов",
}


# ------------------------------------------------------------------------------------------- drafts


@dataclass(slots=True)
class Draft:
    code: str = field(default_factory=codec.new_link_code)
    screen: str | None = None
    plan: str | None = None
    topup: int | None = None
    promo: str | None = None
    ad: str | None = None
    ref: str | None = None
    title: str | None = None
    exp_days: int | None = None
    max_uses: int | None = None
    problems: list[str] | None = None  # cached result of ``check_spec`` (None = not checked yet)

    def spec(self) -> Intent:
        return Intent(
            screen=self.screen, plan=self.plan, topup=self.topup, promo=self.promo, ad=self.ad, ref=self.ref
        )

    def set_target(self, **kw: Any) -> None:
        self.screen = self.plan = self.topup = None
        for name, value in kw.items():
            setattr(self, name, value)
        self.problems = None


class Drafts:
    """In-memory drafts per admin (bounded, expiring)."""

    def __init__(self, *, ttl: float = DRAFT_TTL_S, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._items: OrderedDict[int, tuple[float, Draft]] = OrderedDict()
        self._ttl = ttl
        self._monotonic = monotonic

    def start(self, user_id: int) -> Draft:
        draft = Draft()
        self._put(user_id, draft)
        return draft

    def get(self, user_id: int) -> Draft | None:
        item = self._items.get(user_id)
        if item is None:
            return None
        if item[0] <= self._monotonic():
            del self._items[user_id]
            return None
        self._put(user_id, item[1])
        return item[1]

    def drop(self, user_id: int) -> None:
        self._items.pop(user_id, None)

    def _put(self, user_id: int, draft: Draft) -> None:
        self._items[user_id] = (self._monotonic() + self._ttl, draft)
        self._items.move_to_end(user_id)
        while len(self._items) > DRAFTS_MAX:
            self._items.popitem(last=False)


class _Stop(Exception):  # control flow, not an error
    def __init__(self, result: HandlerResult) -> None:
        super().__init__("stop")
        self.result = result


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _code_validator(value: str) -> str:
    v = value.strip()
    if not codec.valid_value(Kind.PROMO, v):
        raise ValidationError(_T["bad_code"])
    return v


def _qr_png(data: str) -> bytes:
    out = io.BytesIO()
    segno.make(data, error="m").save(out, kind="png", scale=8, border=3)
    return out.getvalue()


# ------------------------------------------------------------------------------------------- screens


class LinkBuilder:
    def __init__(
        self,
        router: ScreenRouter,
        service: DeeplinkService,
        *,
        catalog: Any = None,
        drafts: Drafts | None = None,
    ) -> None:
        self.router = router
        self.service = service
        self.catalog = catalog if catalog is not None else service.catalog
        self.drafts = drafts or Drafts()
        self._user_can_open = router_can_open(router)
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard = {"required_role": "admin", "perm": PERM}
        for code, fn in (
            (SCREEN_LIST, self._list_screen),
            (SCREEN_NEW, self._target_screen),
            (SCREEN_SCREENS, self._screens_screen),
            (SCREEN_PLANS, self._plans_screen),
            (SCREEN_DRAFT, self._draft_screen),
            (SCREEN_EXPIRY, self._expiry_screen),
            (SCREEN_CARD, self._card_screen),
        ):
            r.screen(code, **guard)(self._wrap(fn))
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "new": self._a_new,
            "home": self._a_home,
            "scr": self._a_screen,
            "pl": self._a_plan,
            "topup": self._a_topup,
            "promo": self._a_promo,
            "ad": self._a_ad,
            "ref": self._a_ref,
            "max": self._a_max,
            "title": self._a_title,
            "exp": self._a_expiry,
            "create": self._a_create,
            "on": self._a_on,
            "off": self._a_off,
            "qr": self._a_qr,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(self._wrap(fn))
        forms = (
            Form(
                F_TOPUP,
                (Field("amount", self._topup_prompt(), integer(min_value=1, max_value=codec.TOPUP_MAX)),),
                self._f_topup,
            ),
            Form(F_PROMO, (Field("v", _T["f_promo"], _code_validator, optional=True),), self._f_promo),
            Form(F_AD, (Field("v", _T["f_ad"], _code_validator, optional=True),), self._f_ad),
            Form(F_REF, (Field("v", _T["f_ref"], _code_validator, optional=True),), self._f_ref),
            Form(
                F_MAX,
                (Field("v", _T["f_max"], integer(min_value=1, max_value=MAX_USES_LIMIT), optional=True),),
                self._f_max,
            ),
            Form(F_TITLE, (Field("v", _T["f_title"], text_validator(max_len=64)),), self._f_title),
        )
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

    def _topup_prompt(self) -> str:
        return _T["f_topup"].format(cur=self.service.currency())

    def _wrap(
        self, fn: Callable[[ScreenCtx, Any], Awaitable[Any]]
    ) -> Callable[[ScreenCtx, Any], Awaitable[Any]]:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result

        run.__name__ = getattr(fn, "__name__", "deeplinks_handler")
        return run

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return await self._draft_view(ctx) if self.drafts.get(ctx.user.user_id) else Redirect(SCREEN_LIST)

    def aiogram_router(self, name: str = "svbg-deeplinks") -> Router:
        """``/links`` in a private chat (owner, admin with ``deeplinks``)."""
        router = Router(name=name)

        async def on_links(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        router.message.register(on_links, Command("links"))
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /links")
            return False
        if user is None or not (user.at_least("admin") and user.has_perm(PERM)):
            return False
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    # ------------------------------------------------------------ helpers

    def _draft(self, ctx: ScreenCtx) -> Draft:
        draft = self.drafts.get(ctx.user.user_id)
        if draft is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["stale"]))
        return draft

    def _url(self, payload: str) -> str | None:
        return codec.start_url(self.router.transport.bot_username, payload)

    def _plan_title(self, value: str) -> str | None:
        snap = getattr(self.catalog, "snapshot", None)
        if snap is None:
            return None
        plan = snap.by_code(value)
        if plan is None and value.isdigit():
            plan = snap.plan(int(value))
        if plan is None:
            return None
        title = getattr(plan, "title", None)
        return str(title("ru")) if callable(title) else str(plan.code)

    def _local(self, moment: datetime) -> str:
        tz_name = self.service.setting("TIMEZONE", "Europe/Moscow")
        try:
            zone = zoneinfo.ZoneInfo(tz_name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            zone = zoneinfo.ZoneInfo("UTC")
        return moment.astimezone(zone).strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _actor(user: UserCtx) -> Actor:
        return Actor(user_id=user.user_id, role=user.role)

    # ------------------------------------------------------------ list

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        offset = int(arg) if isinstance(arg, str) and arg.isdigit() and len(arg) <= 9 else 0
        rows, total = await self.service.list_links(offset=offset, limit=PAGE)
        now = clock.now()
        lines = [_T["list_title"], _T["list_hint"]]
        keyboard: list[list[InlineKeyboardButton]] = []
        if not rows:
            lines.append(_T["list_empty"])
        for link in rows:
            icon = "🟢" if link.unusable(now) is None else "⏸"
            uses = f"{link.uses}/{link.max_uses}" if link.max_uses else str(link.uses)
            keyboard.append([nav_button(f"{icon} {link.title} · {uses}", SCREEN_CARD, arg=str(link.id))])
        if total > PAGE:
            page = offset // PAGE
            pages = (total + PAGE - 1) // PAGE
            lines.append(_T["list_page"].format(page=page + 1, pages=pages))
            nav: list[InlineKeyboardButton] = []
            if offset > 0:
                nav.append(nav_button(_T["prev"], SCREEN_LIST, arg=str(max(0, offset - PAGE))))
            if offset + PAGE < total:
                nav.append(nav_button(_T["next"], SCREEN_LIST, arg=str(offset + PAGE)))
            keyboard.append(nav)
        keyboard.append([nav_button(_T["new"], ACTIONS, "new", style="success")])
        if admin_nav.has_screen(self.router, "set.v") and (
            ctx.user.role == "owner" or ctx.user.has_perm("settings.business")
        ):
            keyboard.append([nav_button(_T["settings"], "set.v", arg="m.links")])
        keyboard.append(admin_nav.back_row(SCREEN_LIST))
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    # ------------------------------------------------------------ target

    async def _a_new(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self.drafts.start(ctx.user.user_id)
        return Redirect(SCREEN_NEW)

    async def _target_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        if self.drafts.get(ctx.user.user_id) is None:
            self.drafts.start(ctx.user.user_id)
        keyboard = [
            [nav_button(_T["t_home"], ACTIONS, "home"), nav_button(_T["t_screen"], SCREEN_SCREENS)],
            [nav_button(_T["t_plan"], SCREEN_PLANS), nav_button(_T["t_topup"], ACTIONS, "topup")],
            [nav_button(_T["t_promo"], ACTIONS, "home", "promo")],
            [nav_button(_T["cancel"], SCREEN_LIST)],
        ]
        return View(text=_T["target_title"], parse_mode="HTML", keyboard=keyboard)

    def linkable_screens(self) -> list[tuple[str, str]]:
        """``(code, label)`` of screens an ordinary user may open by a direct ``s_`` link."""
        probe = UserCtx(user_id=0)
        found: dict[str, str] = {}
        routes: dict[str, Any] = getattr(self.router, "_screens", {})
        for code in routes:
            if codec.valid_value(Kind.SCREEN, code) and self._user_can_open(probe, code):
                found[code] = _SCREEN_LABELS.get(code, code)
        content = self.router.content
        if content is not None:
            for code, entry in content.snapshot.by_code.items():
                if code in found or not codec.valid_value(Kind.SCREEN, code):
                    continue
                if self._user_can_open(probe, code):
                    title = entry.screen.title.get("ru") or next(iter(entry.screen.title.values()), "")
                    found[code] = _SCREEN_LABELS.get(code) or f"📄 {title or code}"
        return sorted(found.items(), key=lambda kv: (kv[0] != HOME, kv[1]))[:MAX_TARGETS]

    async def _screens_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        self._draft(ctx)
        items = self.linkable_screens()
        keyboard: list[list[InlineKeyboardButton]] = []
        for i in range(0, len(items), 2):
            keyboard.append(
                [nav_button(label[:60], ACTIONS, "scr", code) for code, label in items[i : i + 2]]
            )
        keyboard.append([nav_button(_T["back"], SCREEN_NEW)])
        text = _T["screens_title"] if items else _T["screens_title"] + "\n\n" + _T["screens_empty"]
        return View(text=text, parse_mode="HTML", keyboard=keyboard)

    def _linkable_plans(self) -> list[Any]:
        snap = getattr(self.catalog, "snapshot", None)
        plans = getattr(snap, "plans", ()) if snap is not None else ()
        return [
            p
            for p in plans
            if getattr(p, "enabled", False)
            and not getattr(p, "is_trial", False)
            and getattr(p, "broken_reason", None) is None
            and codec.valid_value(Kind.PLAN, str(p.code))
        ][:MAX_TARGETS]

    async def _plans_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        self._draft(ctx)
        plans = self._linkable_plans()
        keyboard = []
        for p in plans:
            label = self._plan_title(str(p.code)) or str(p.code)
            if getattr(p, "availability", "all") == "link":
                label += _T["plan_link_only"]
            keyboard.append([nav_button(f"📦 {label}"[:60], ACTIONS, "pl", str(p.code))])
        keyboard.append([nav_button(_T["back"], SCREEN_NEW)])
        text = _T["plans_title"] if plans else _T["plans_title"] + "\n\n" + _T["plans_empty"]
        return View(text=text, parse_mode="HTML", keyboard=keyboard)

    async def _a_home(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        draft = self._draft(ctx)
        draft.set_target()
        if arg == "promo":
            return await ctx.start_form(F_PROMO)
        return await self._draft_view(ctx)

    async def _a_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        draft = self._draft(ctx)
        if not isinstance(arg, str) or arg not in dict(self.linkable_screens()):
            return Redirect(SCREEN_SCREENS)
        draft.set_target(screen=arg)
        return await self._draft_view(ctx)

    async def _a_plan(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        draft = self._draft(ctx)
        if not isinstance(arg, str) or arg not in {str(p.code) for p in self._linkable_plans()}:
            return Redirect(SCREEN_PLANS)
        draft.set_target(plan=arg)
        return await self._draft_view(ctx)

    async def _a_topup(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_TOPUP)

    async def _f_topup(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        self._draft(ctx).set_target(topup=int(data["amount"]))
        return await self._draft_view(ctx)

    # ------------------------------------------------------------ parameters

    async def _a_promo(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_PROMO)

    async def _a_ad(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_AD)

    async def _a_ref(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_REF)

    async def _a_max(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_MAX)

    async def _a_title(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        self._draft(ctx)
        return await ctx.start_form(F_TITLE)

    async def _set(self, ctx: ScreenCtx, name: str, value: Any) -> HandlerResult:
        draft = self._draft(ctx)
        setattr(draft, name, value)
        if name in ("promo", "ad"):
            draft.problems = None
        return await self._draft_view(ctx)

    async def _f_promo(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set(ctx, "promo", data.get("v"))

    async def _f_ad(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set(ctx, "ad", data.get("v"))

    async def _f_ref(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set(ctx, "ref", data.get("v"))

    async def _f_max(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set(ctx, "max_uses", data.get("v"))

    async def _f_title(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set(ctx, "title", " ".join(str(data["v"]).split()))

    async def _expiry_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        self._draft(ctx)
        presets = [nav_button(_T["exp_n"].format(n=n), ACTIONS, "exp", str(n)) for n in EXPIRY_DAYS]
        keyboard = [
            [nav_button(_T["exp_forever"], ACTIONS, "exp", "0")],
            presets,
            [nav_button(_T["back"], SCREEN_DRAFT)],
        ]
        return View(text=_T["exp_title"], parse_mode="HTML", keyboard=keyboard)

    async def _a_expiry(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        draft = self._draft(ctx)
        if arg == "0":
            draft.exp_days = None
        elif isinstance(arg, str) and arg.isdigit() and int(arg) in EXPIRY_DAYS:
            draft.exp_days = int(arg)
        else:
            return Redirect(SCREEN_EXPIRY)
        return await self._draft_view(ctx)

    # ------------------------------------------------------------ draft card

    def _auto_title(self, draft: Draft) -> str:
        spec = draft.spec()
        parts = [line.split(": ", 1)[-1] for line in describe(spec, plan_title=self._plan_title)]
        return " · ".join(parts)[:64] or "Ссылка"

    async def _draft_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        self._draft(ctx)
        return await self._draft_view(ctx)

    async def _draft_view(self, ctx: ScreenCtx) -> View:
        draft = self._draft(ctx)
        spec = draft.spec()
        if draft.problems is None:
            draft.problems = await self.service.check_spec(spec)
        lines = [_T["draft_title"]]
        body = list(describe(spec, plan_title=self._plan_title))[:1]
        none = _T["none"]
        body += [
            _T["d_promo"].format(v=_esc(draft.promo or none)),
            _T["d_ad"].format(v=_esc(draft.ad or none)),
            _T["d_ref"].format(v=_esc(draft.ref or none)),
            _T["d_exp"].format(
                v=_T["exp_days"].format(n=draft.exp_days) if draft.exp_days else _T["exp_none"]
            ),
            _T["d_max"].format(v=_T["max_n"].format(n=draft.max_uses) if draft.max_uses else _T["max_none"]),
            _T["d_title"].format(v=_esc(draft.title or self._auto_title(draft))),
        ]
        lines.append("\n".join(_esc(b) if i == 0 else b for i, b in enumerate(body)))
        direct = spec.direct_payload()
        direct_url = self._url(direct) if direct else None
        if direct_url:
            lines.append(_T["direct"].format(url=_esc(direct_url)))
        if draft.problems:
            lines.append(_T["problems"].format(lines=_esc("\n⚠️ ".join(draft.problems))))
        keyboard: list[list[InlineKeyboardButton]] = [
            [nav_button(_T["b_target"], SCREEN_NEW), nav_button(_T["b_promo"], ACTIONS, "promo")],
            [nav_button(_T["b_ad"], ACTIONS, "ad"), nav_button(_T["b_ref"], ACTIONS, "ref")],
            [nav_button(_T["b_exp"], SCREEN_EXPIRY), nav_button(_T["b_max"], ACTIONS, "max")],
            [nav_button(_T["b_title"], ACTIONS, "title")],
        ]
        if direct_url:
            keyboard.append(
                [InlineKeyboardButton(text=_T["b_copy_direct"], copy_text=CopyTextButton(text=direct_url))]
            )
        if not spec.empty:
            keyboard.append([nav_button(_T["b_create"], ACTIONS, "create", style="success")])
        keyboard.append([nav_button(_T["cancel"], SCREEN_LIST)])
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    async def _a_create(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        draft = self._draft(ctx)
        existing = await self.service.get_by_code(draft.code)
        if existing is not None:  # a double tap: the link already exists
            self.drafts.drop(ctx.user.user_id)
            return await self._card_view(ctx, existing, LinkStats(), toast=_T["created"])
        expires = clock.now() + timedelta(days=draft.exp_days) if draft.exp_days else None
        try:
            link = await self.service.create_link(
                draft.spec(),
                title=draft.title or self._auto_title(draft),
                actor=self._actor(ctx.user),
                expires_at=expires,
                max_uses=draft.max_uses,
                code=draft.code,
            )
        except (ValueError, IntentError) as e:
            return Toast(str(e), alert=True)
        self.drafts.drop(ctx.user.user_id)
        log.info("deep link %s created by user %s", link.id, ctx.user.user_id)
        return await self._card_view(ctx, link, LinkStats(), toast=_T["created"])

    # ------------------------------------------------------------ link card

    async def _load(self, arg: Any) -> LinkRow:
        if not isinstance(arg, str) or not arg.isdigit() or len(arg) > 18:
            raise _Stop(Redirect(SCREEN_LIST))
        link = await self.service.get_link(int(arg))
        if link is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["gone"]))
        return link

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        link = await self._load(arg)
        return await self._card_view(ctx, link, await self.service.stats(link.id))

    async def _card_view(
        self, ctx: ScreenCtx, link: LinkRow, stats: LinkStats, *, toast: str | None = None
    ) -> View:
        del ctx
        now = clock.now()
        url = self._url(link.payload)
        lines = [f"🔗 <b>{_esc(link.title)}</b>"]
        lines.append(
            f"<code>{_esc(url)}</code>" if url else _T["card_no_username"].format(payload=link.payload)
        )
        info = list(describe(link.intent, plan_title=self._plan_title))
        reason = link.unusable(now)
        status = {
            None: _T["card_status_on"],
            "disabled": _T["card_status_off"],
            "expired": _T["card_status_expired"],
            "used_up": _T["card_status_used_up"],
        }.get(reason, _T["card_status_off"])
        info.append(_T["card_status"].format(v=status))
        info.append(
            _T["card_uses"].format(uses=link.uses, max=f" из {link.max_uses}" if link.max_uses else "")
        )
        if link.expires_at is not None:
            info.append(_T["card_until"].format(v=self._local(link.expires_at)))
        lines.append("\n".join(_esc(x) for x in info))
        lines.append(
            _T["card_stats"].format(
                h=" / ".join(map(str, stats.hits)),
                u=" / ".join(map(str, stats.users)),
                n=" / ".join(map(str, stats.new_users)),
            )
        )
        keyboard: list[list[InlineKeyboardButton]] = []
        if url:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=_T["b_copy"], copy_text=CopyTextButton(text=url), style="primary"
                    ),
                    nav_button(_T["b_qr"], ACTIONS, "qr", str(link.id)),
                ]
            )
        toggle = (
            nav_button(_T["b_off"], ACTIONS, "off", str(link.id))
            if link.enabled
            else nav_button(_T["b_on"], ACTIONS, "on", str(link.id), style="success")
        )
        keyboard.append([toggle])
        keyboard.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=keyboard, toast=toast)

    async def _toggle(self, ctx: ScreenCtx, arg: Any, enabled: bool) -> HandlerResult:
        if not isinstance(arg, str) or not arg.isdigit() or len(arg) > 18:
            return Redirect(SCREEN_LIST)
        link = await self.service.set_enabled(int(arg), enabled, actor=self._actor(ctx.user))
        if link is None:
            return Redirect(SCREEN_LIST, toast=_T["gone"])
        toast = _T["turned_on"] if enabled else _T["turned_off"]
        return await self._card_view(ctx, link, await self.service.stats(link.id), toast=toast)

    async def _a_on(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._toggle(ctx, arg, True)

    async def _a_off(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._toggle(ctx, arg, False)

    async def _a_qr(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = await self._load(arg)
        url = self._url(link.payload)
        if url is None:
            return Toast(_T["qr_no_url"], alert=True)
        media = MediaRef(
            "photo", BufferedInputFile(_qr_png(url), filename=f"link-{link.code}.png"), key=f"qr:dl:{link.id}"
        )
        back = [nav_button(_T["back"], SCREEN_CARD, arg=str(link.id))]
        caption = _T["qr_caption"].format(title=link.title, url=url)
        del ctx
        return View(text=caption, media=media, keyboard=[back])


# ------------------------------------------------------------------------------------------- setup


async def setup(router: ScreenRouter, deps: Any) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``).

    Uses ``deps.deeplinks`` (the app's :class:`DeeplinkService`, shared with ``/start``) when present;
    otherwise builds its own over ``deps`` (links still work: both read the same tables).
    """
    service = getattr(deps, "deeplinks", None)
    if not isinstance(service, DeeplinkService):
        service = from_app(deps)
    builder = LinkBuilder(router, service, catalog=getattr(deps, "catalog", None))
    builder.install()
    return builder.aiogram_router()
