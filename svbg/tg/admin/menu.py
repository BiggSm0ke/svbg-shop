"""«🛠 Админка»: one admin home split into sections (the owner's request; ideas from Bedolaga and Remnashop).

* ``adm`` — the root (``admin`` is the same screen: the old content hub's code, so old buttons and messages
  keep working; ``/admin`` opens it). Three lines of numbers from the cached dashboard (no SQL on an ordinary
  click), quick actions (search, a new broadcast, «Требует внимания», maintenance on, settings waiting for a
  restart) and the sections: 👥 Пользователи, 📦 Тарифы, 💳 Оплата, 🎯 Маркетинг, 📣 Связь, 🎨 Оформление,
  📊 Статистика, ⚙️ Система, 🧩 Модули (only when a module is wired).
* ``adm.u``, ``adm.pay``, ``adm.mk``, ``adm.c``, ``adm.l``, ``adm.sys``, ``adm.x``: hubs (:data:`HUBS`);
  ``adm.rw`` — «🔌 Панель Remnawave»; ``adm.s`` — «📊 Статистика» (:mod:`svbg.tg.admin.dashboard`).

A button is shown only when the viewer can open at least one thing behind it: role and right, the target
screen registered (a module that is not wired has no button), a settings slice with a key they may see. No
«Нет прав» from a visible button.

Search without a button: while the root or «👥 Пользователи» is the last admin screen a staff member opened
(15 minutes, in memory), a text they send in the private chat — an ID, @username, a subscription link — or a
forwarded message is a search query; the message is deleted and the answer replaces the admin message. It runs
before the support module (a staff member with an open ticket would otherwise copy the query into it) and
never takes a message a form is waiting for or something that looks like a token.

The module also sets the staff «/» command menus (:mod:`svbg.tg.admin.commands`).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.methods import DeleteMessage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from svbg.core.component import Health
from svbg.core.settings.service import Change, SettingsError
from svbg.services import roles
from svbg.services.roles import Act, Actor
from svbg.tg.admin import nav, slices
from svbg.tg.admin.commands import StaffCommands
from svbg.tg.admin.dashboard import Dashboard, DashboardScreens, render_live
from svbg.tg.admin.settings import can_open_settings, can_view
from svbg.tg.admin.users import settings_reader
from svbg.tg.admin.users.screens import installed_on
from svbg.tg.ui import codec
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.router import looks_like_secret
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.core.component import ComponentRegistry
    from svbg.core.settings.service import SettingsService
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["HUBS", "ROOT_SECTIONS", "AdminMenu", "Entry", "Hub", "setup"]

log = logging.getLogger("svbg.tg.admin.menu")

ACTIONS: Final = "adma"  # shared with the dashboard («rf» = refresh); «mx» = maintenance off
A_MAINT_OFF: Final = "mx"
SEARCH_TTL: Final = 15 * 60.0
SEARCH_CACHE: Final = 1024
TRACKED: Final = frozenset({nav.ROOT, nav.ROOT_ALIAS, nav.HUB_USERS})
HOME: Final = "home"

_T: Final[dict[str, str]] = {
    "title": "🛠 <b>Админка</b>",
    "no_numbers": "Цифры сейчас недоступны, загляните через минуту.",
    "search_hint": "Чтобы найти человека, пришлите сюда его ID, @username или ссылку подписки, или "
    "перешлите его сообщение.",
    "nothing": "По запросу «{q}» никого не нашлось.",
    "empty": "Здесь нет разделов, которые вам доступны.",
    "b_find": "🔍 Найти пользователя",
    "b_broadcast": "📨 Рассылка",
    "b_attention": "⚠️ Требует внимания · {n}",
    "b_maint": "🛠 Техработы включены · выключить",
    "b_restart": "♻️ Ждут перезапуска: {n}",
    "b_refresh": "🔄 Обновить",
    "b_home": "🏠 Меню",
    "maint_off": "Техработы выключены",
    "maint_failed": "Не получилось: {reason}",
    "unavailable": "Сейчас недоступно, попробуйте позже",
    "rw_ok": "Связь с панелью: ✅ на связи",
    "rw_off": "Связь с панелью: ⏸ панель не подключена",
    "rw_bad": "Связь с панелью: {icon} {text}",
    "rw_hint": "Здесь адрес панели и ключ доступа, мастер первой настройки и то, что включено в самой "
    "панели.",
    "b_wizard": "🧭 Мастер настройки",
    "b_panel_features": "🖥 Что включено в панели",
    "b_panel_settings": "⚙️ Настройки панели",
}

_HEALTH_ICON: Final = {Health.DEGRADED: "⚠️", Health.DOWN: "❌", Health.UNKNOWN: "❔", Health.DISABLED: "⏸"}


@dataclass(frozen=True, slots=True)
class Entry:
    """A button of the root or of a section. Shown when the viewer has ``role`` and ``perm``, the screen
    ``requires`` (default: ``target`` when it is opened) is registered and, for a settings slice, the viewer
    may see one of its keys."""

    label: str
    target: str
    action: str = codec.ACTION_OPEN
    arg: str | None = None
    role: str = "admin"
    perm: str | None = None
    requires: str | None = None
    slice: str | None = None


@dataclass(frozen=True, slots=True)
class Hub:
    code: str
    intro: str
    entries: tuple[Entry, ...]
    role: str = "admin"


def _slice(label: str, sid: str, *, role: str = "admin") -> Entry:
    return Entry(label, slices.SCREEN, arg=sid, role=role, perm="settings.business", slice=sid)


HUBS: Final[dict[str, Hub]] = {
    h.code: h
    for h in (
        Hub(
            nav.HUB_USERS,
            "Поиск, новые клиенты, оплаты и блокировки.",
            (
                Entry("🔍 Найти", "au.find", role="support"),
                Entry("🆕 Новые", "au.new", role="support"),
                Entry("💳 Недавно оплатили", "au.paid", perm="stats"),
                Entry("⛔ Заблокированные", "au.ban", perm="users.ban"),
                _slice("🚪 Вход в бот", "u.access"),
            ),
            role="support",
        ),
        Hub(
            nav.HUB_PAY,
            "Кассы, баланс клиентов и ручные оплаты.",
            (
                Entry("🏦 Кассы", "apay", role="owner"),
                _slice("💰 Баланс и пополнение", "pay.wallet"),
                Entry("🧾 Ждут подтверждения", "apay.rc", perm="payments.confirm"),
                Entry("💱 Валюта", "set.key", arg="CURRENCY", perm="settings.business"),
            ),
        ),
        Hub(
            nav.HUB_MARKETING,
            "Реклама считает переходы и оплаты по каждой ссылке. Ссылки на разделы просто открывают нужный "
            "экран бота, например тариф или промокод.",
            (
                Entry("🎟 Промокоды", "prm", perm="promo"),
                Entry("📢 Реклама", "ads", perm="promo"),
                Entry("🔗 Ссылки на разделы бота", "dl", perm="deeplinks"),
                _slice("🤝 Рефералка", "m.ref"),
            ),
        ),
        Hub(
            nav.HUB_COMM,
            "Рассылки, сообщения, которые бот сам пишет клиентам, поддержка и админ-группа.",
            (
                Entry("📨 Рассылки", "bc", perm="broadcast"),
                _slice("🔔 Уведомления клиентам", "c.notify"),
                _slice("💬 Поддержка", "c.support"),
                Entry("🛎 Админ-группа", "achat", role="owner"),
            ),
        ),
        Hub(
            nav.HUB_LOOK,
            "Как выглядит бот: экраны и кнопки, страницы с правилами, языки и картинки.",
            (
                Entry("✏️ Конструктор экранов", "ce.home", perm="content.edit"),
                Entry(
                    "✏️ Режим правки: вкл/выкл", "ce.a", action="mode", perm="content.edit", requires="ce.home"
                ),
                Entry("📄 Страницы (FAQ, правила, оферта)", "pgs", perm="settings.business"),
                _slice("🌐 Языки", "l.lang"),
                _slice("🖼 Картинки и баннер", "l.media"),
            ),
        ),
        Hub(
            nav.HUB_SYSTEM,
            "Состояние бота, панель, бэкапы, команда и все настройки.",
            (
                Entry("🩺 Состояние", "status", perm="system.view"),
                Entry("🔌 Панель Remnawave", nav.PANEL, role="owner"),
                Entry("💾 Бэкапы и обновления", "ops", role="owner"),
                _slice("🛠 Техработы", "sys.maint"),
                Entry("👮 Команда", "roles", role="owner"),
                _slice("🧭 Основное", "sys.main"),
                _slice("🧰 Сервер и .env", "sys.server", role="owner"),  # the log level alone is not a reason
                Entry("🔎 Все настройки", "settings_root", perm="settings.business"),
            ),
        ),
        Hub(
            nav.HUB_MODULES,
            "Дополнительные модули, подключённые к боту.",
            (
                Entry("🌐 Трафик LTE", "lte", perm="lte.view"),
                Entry("🛡 IP Guard", "ipguard", role="support"),
            ),
            role="support",
        ),
    )
}

#: The sections of the root, two per row, in this order (a hub or a screen opened directly).
ROOT_SECTIONS: Final[tuple[Entry, ...]] = (
    Entry("👥 Пользователи", nav.HUB_USERS, role="support"),
    Entry("📦 Тарифы", "plans", perm="plans"),
    Entry("💳 Оплата", nav.HUB_PAY),
    Entry("🎯 Маркетинг", nav.HUB_MARKETING),
    Entry("📣 Связь", nav.HUB_COMM),
    Entry("🎨 Оформление", nav.HUB_LOOK),
    Entry("📊 Статистика", nav.STATS, perm="stats"),
    Entry("⚙️ Система", nav.HUB_SYSTEM),
    Entry("🧩 Модули", nav.HUB_MODULES, role="support"),
)


@dataclass(frozen=True, slots=True)
class _Search:
    """Argument of the root shown for a typed query (never encoded into a button)."""

    query: str


def _actor(user: UserCtx) -> Actor:
    return Actor(user.user_id, user.telegram_id, user.role, user.perms)


def _esc(value: Any) -> str:
    import html

    return html.escape(str(value), quote=False)


class AdminMenu:
    """See the module docstring."""

    def __init__(
        self,
        router: ScreenRouter,
        dashboard: Dashboard,
        *,
        settings: SettingsService | None = None,
        components: ComponentRegistry | None = None,
        commands: StaffCommands | None = None,
        currency: Callable[[], str] = lambda: "RUB",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.router = router
        self.dashboard = dashboard
        self.settings = settings
        self.components = components
        self.commands = commands
        self.currency = currency
        self._clock = clock
        self._recent: OrderedDict[int, float] = OrderedDict()  # telegram id → when a search screen was shown
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        r.screen(nav.ROOT, required_role="support")(self._root_screen)
        r.screen(nav.ROOT_ALIAS, required_role="support")(self._root_screen)
        for hub in HUBS.values():
            r.screen(hub.code, required_role=hub.role)(self._hub_screen(hub))
        r.screen(nav.PANEL, required_role="owner")(self._panel_screen)
        r.action(ACTIONS, A_MAINT_OFF, required_role="owner")(self._maintenance_off)
        DashboardScreens(r, self.dashboard).install()

    # ------------------------------------------------------------ visibility

    def visible(self, user: UserCtx, entry: Entry) -> bool:
        if not user.at_least(entry.role) or (entry.perm is not None and not user.has_perm(entry.perm)):
            return False
        hub = HUBS.get(entry.target) if entry.action == codec.ACTION_OPEN else None
        if hub is not None:
            return any(self.visible(user, e) for e in hub.entries)
        if entry.slice is not None:
            return nav.has_screen(self.router, slices.SCREEN) and self.slice_visible(user, entry.slice)
        if entry.target == "set.key" and entry.arg:
            return self._key_visible(user, entry.arg)
        required = entry.requires or (entry.target if entry.action == codec.ACTION_OPEN else None)
        return required is None or nav.has_screen(self.router, required)

    def _key_visible(self, user: UserCtx, key: str) -> bool:
        registry = getattr(self.settings, "registry", None)
        defn = registry.find(key) if registry is not None else None
        return defn is not None and nav.has_screen(self.router, "set.key") and can_view(user, defn)

    def slice_visible(self, user: UserCtx, slice_id: str) -> bool:
        sl = slices.slice_of(slice_id)
        registry = getattr(self.settings, "registry", None)
        if sl is None or registry is None or not can_open_settings(user):
            return False
        keys = [*sl.keys, *sl.more, *sl.mirrors]
        defs = [registry.find(k) for k in keys]
        if sl.sections:
            by_section = registry.by_section()
            defs += [d for sid in sl.sections for d in by_section.get(sid, [])]
        if any(d is not None and can_view(user, d) for d in defs):
            return True
        return any(
            user.at_least(link.role)
            and (link.perm is None or user.has_perm(link.perm))
            and nav.has_route(self.router, link.screen, link.action)
            for link in sl.links
        )

    def _button(self, entry: Entry, label: str | None = None) -> InlineKeyboardButton:
        return nav_button(label or entry.label, entry.target, entry.action, entry.arg)

    # ------------------------------------------------------------ root

    async def _root_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        if isinstance(arg, _Search):
            users = installed_on(self.router)
            found = await users.search_view(ctx, arg.query) if users is not None else None
            self._remember(ctx.user)
            if found is not None:
                return found
            return await self.root_view(ctx, note=_esc(_T["nothing"].format(q=arg.query[:64])))
        return await self.root_view(ctx)

    async def root_view(self, ctx: ScreenCtx, *, note: str | None = None) -> View:
        user = ctx.user
        actor = _actor(user)
        stats_allowed = roles.authorize(actor, Act.STATS)
        stats = await self.dashboard.get() if stats_allowed else None
        lines = [_T["title"]]
        if stats is not None:
            lines += render_live(stats, currency=self.currency())
        elif stats_allowed:
            lines.append(_T["no_numbers"])
        if note:
            lines += ["", note]
        if roles.authorize(actor, Act.USERS_VIEW) and installed_on(self.router) is not None:
            lines += ["", _T["search_hint"]]
        self._remember(user)
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=self._root_keyboard(user, stats))

    def _root_keyboard(self, user: UserCtx, stats: Any) -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = []
        quick = [
            e
            for e in (
                Entry(_T["b_find"], "au.find", role="support"),
                Entry(_T["b_broadcast"], "bc", perm="broadcast"),
            )
            if self.visible(user, e)
        ]
        if quick:
            rows.append([self._button(e) for e in quick])
        attention = getattr(stats, "attention_count", 0)
        if attention and self.visible(user, Entry("", "status.att", perm="system.view")):
            rows.append([nav_button(_T["b_attention"].format(n=attention), "status.att")])
        if user.role == "owner" and self.settings is not None:
            if self.settings.current().get("MAINTENANCE_MODE") == "on":
                rows.append([nav_button(_T["b_maint"], ACTIONS, A_MAINT_OFF)])
            pending = len(self.settings.restart_pending)
            if pending and nav.has_screen(self.router, "settings_root"):
                rows.append([nav_button(_T["b_restart"].format(n=pending), "settings_root")])
        sections: list[InlineKeyboardButton] = []
        for entry in ROOT_SECTIONS:
            if not self.visible(user, entry):
                continue
            label = entry.label
            receipts = getattr(stats, "receipts_pending", 0)
            if entry.target == nav.HUB_PAY and receipts and user.has_perm("payments.confirm"):
                label += f" · 🧾 {receipts}"
            if entry.target == nav.HUB_SYSTEM and attention and user.has_perm("system.view"):
                label += " ⚠️"
            sections.append(self._button(entry, label))
        rows.extend(sections[i : i + 2] for i in range(0, len(sections), 2))
        refresh = (
            nav_button(_T["b_refresh"], ACTIONS, "rf")
            if roles.authorize(_actor(user), Act.STATS)
            else nav_button(_T["b_refresh"], nav.ROOT)
        )
        rows.append([refresh, nav_button(_T["b_home"], HOME)])
        return rows

    async def _maintenance_off(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        """«Техработы включены · выключить»: back to «Сами, если панель недоступна» (the default)."""
        if self.settings is None:
            return Toast(_T["unavailable"])
        try:
            result = await self.settings.apply(
                [Change("MAINTENANCE_MODE", "auto")], source="bot", actor_id=ctx.user.user_id
            )
        except (SettingsError, OSError) as e:
            return Toast(_T["maint_failed"].format(reason=str(e)[:150]), alert=True)
        if result.rejected:
            reason = next(iter(result.rejected.values()))
            return Toast(_T["maint_failed"].format(reason=reason[:150]), alert=True)
        return Redirect(nav.ROOT, toast=_T["maint_off"])

    # ------------------------------------------------------------ hubs

    def _hub_screen(self, hub: Hub) -> Callable[[ScreenCtx, Any], Any]:
        async def screen(ctx: ScreenCtx, _arg: Any) -> View:
            return self.hub_view(ctx.user, hub)

        screen.__name__ = f"hub_{hub.code.replace('.', '_')}"
        return screen

    def hub_view(self, user: UserCtx, hub: Hub) -> View:
        title = nav.TITLES.get(hub.code, hub.code)
        crumb = nav.breadcrumb(hub.code)
        head, _, _ = crumb.rpartition(" › ")
        lines = [
            f"{_esc(head)} › <b>{_esc(title)}</b>" if head else f"<b>{_esc(title)}</b>",
            "",
            _esc(hub.intro),
        ]
        rows = [[self._button(e)] for e in hub.entries if self.visible(user, e)]
        if not rows:
            lines += ["", _T["empty"]]
        if hub.code == nav.HUB_USERS:
            if installed_on(self.router) is not None and roles.authorize(_actor(user), Act.USERS_VIEW):
                lines += ["", _T["search_hint"]]
            self._remember(user)
        rows.append(nav.back_row(hub.code))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ «🔌 Панель Remnawave»

    async def _panel_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        user = ctx.user
        lines = [
            f"{_esc(nav.breadcrumb(nav.HUB_SYSTEM))} › <b>{_esc(nav.TITLES[nav.PANEL])} Remnawave</b>",
            "",
        ]
        comps = self.components
        if comps is not None and "remnawave" in comps:
            report = await comps.health("remnawave", limit_s=1.0)
            if report.status is Health.OK:
                lines.append(_T["rw_ok"])
            elif report.status is Health.DISABLED:
                lines.append(_T["rw_off"])
            else:
                icon = _HEALTH_ICON.get(report.status, "❔")
                lines.append(_T["rw_bad"].format(icon=icon, text=_esc(str(report.summary)[:200])))
        else:
            lines.append(_T["rw_off"])
        lines += ["", _T["rw_hint"]]
        entries = (
            Entry(_T["b_wizard"], "setup.wiz", role="owner"),
            Entry(_T["b_panel_features"], "status.panel", perm="system.view"),
            _slice(_T["b_panel_settings"], "sys.panel"),
        )
        rows = [[self._button(e)] for e in entries if self.visible(user, e)]
        rows.append(nav.back_row(nav.PANEL))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ search without a button

    def _remember(self, user: UserCtx) -> None:
        tg = user.telegram_id
        if tg is None:
            return
        self._recent[tg] = self._clock()
        self._recent.move_to_end(tg)
        while len(self._recent) > SEARCH_CACHE:
            self._recent.popitem(last=False)

    def armed(self, telegram_id: int) -> bool:
        at = self._recent.get(telegram_id)
        return at is not None and self._clock() - at <= SEARCH_TTL

    def seen_callback(self, telegram_id: int, data: str | None) -> None:
        """Any other admin button ends the search mode (the root and «👥 Пользователи» arm it again when they
        render)."""
        if telegram_id not in self._recent:
            return
        decoded = codec.decode(data)
        if decoded is None or decoded.screen not in TRACKED or decoded.action != codec.ACTION_OPEN:
            self._recent.pop(telegram_id, None)

    def seen_message(self, message: Message) -> None:
        """A command (``/start``, ``/broadcast``, a deep link …) leaves the admin root too: the text that
        follows is not a search query (``/admin`` arms it again when the root renders)."""
        if message.from_user is None or message.from_user.id not in self._recent:
            return
        if (message.text or "").lstrip().startswith("/"):
            self._recent.pop(message.from_user.id, None)

    @staticmethod
    def query_of(message: Message) -> str | None:
        """What to search for: the text, or the sender of a forwarded message."""
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            return str(origin.sender_user.id)
        if isinstance(origin, MessageOriginHiddenUser):
            return origin.sender_user_name[:128] or None
        if origin is not None:
            return None
        text = (message.text or "").strip()
        if not text or text.startswith("/") or looks_like_secret(text):
            return None
        return text[:128]

    def wants(self, message: Message) -> bool:
        """Cheap filter (no SQL): a private message of someone whose last admin screen invites a search."""
        if message.chat.type != "private" or message.from_user is None:
            return False
        return self.armed(message.from_user.id) and self.query_of(message) is not None

    async def handle_search(self, message: Message) -> bool:
        """``True`` when the message was a search query (answered); ``False`` lets other handlers see it."""
        if message.from_user is None or installed_on(self.router) is None:
            return False
        query = self.query_of(message)
        if query is None:
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for the admin search")
            return False
        if user is None or not roles.authorize(_actor(user), Act.USERS_VIEW):
            return False
        if await self.router.is_awaiting(message):
            return False
        deleted = await self._delete(message)
        await self.router.show(user, message.chat.id, nav.ROOT, _Search(query), new=not deleted)
        return True

    async def _delete(self, message: Message) -> bool:
        try:
            ok = await self.router.transport.call(
                DeleteMessage(chat_id=message.chat.id, message_id=message.message_id), chat_id=message.chat.id
            )
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.debug("could not delete a search query: %s", type(e).__name__)
            return False
        return ok is not False and ok is not None

    # ------------------------------------------------------------ aiogram

    def aiogram_router(self, name: str = "svbg-admin-menu") -> Router:
        """``/admin``, the search by a typed message, the observer of admin buttons, the staff menus."""
        router = Router(name=name)

        async def on_admin(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        async def search_filter(message: Message) -> bool:
            self.seen_message(message)
            return self.wants(message)

        async def on_search(message: Message) -> None:
            if not await self.handle_search(message):
                raise SkipHandler

        async def observe(query: CallbackQuery) -> bool:
            self.seen_callback(query.from_user.id, query.data)
            return False  # only looks: the screen router answers every button

        async def never(_query: CallbackQuery) -> None:  # pragma: no cover - the filter never passes
            raise SkipHandler

        async def on_startup() -> None:
            if self.commands is not None:
                self.commands.spawn(self.commands.sync_later())

        router.message.register(on_admin, Command("admin"))
        router.message.register(on_search, search_filter)
        router.callback_query.register(never, observe)
        router.startup.register(on_startup)
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /admin")
            return False
        if user is None or not user.at_least("support"):
            return False
        await self.router.show(user, message.chat.id, nav.ROOT, new=True)
        return True


def setup(router: Any, deps: Any) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    read = settings_reader(getattr(deps, "settings", None))

    def value(key: str, default: str) -> str:
        try:
            return str(read()[key] or default)
        except KeyError:
            return default

    directory = getattr(deps, "users", None)

    def configured_owners() -> frozenset[int]:
        if directory is None:
            return frozenset()
        try:
            return frozenset(directory.configured_owner_ids())
        except Exception:  # an owner list that cannot be read only skips their menus
            log.exception("configured owners are not readable")
            return frozenset()

    async def call(method: Any, chat_id: int) -> Any:
        return await router.transport.call(method, chat_id=chat_id)

    commands = StaffCommands(call, db=getattr(deps, "db", None), configured_owners=configured_owners).attach(
        router
    )
    menu = AdminMenu(
        router,
        Dashboard(deps.db, timezone=lambda: value("TIMEZONE", "Europe/Moscow")),
        settings=getattr(deps, "settings", None),
        components=getattr(deps, "components", None),
        commands=commands,
        currency=lambda: value("CURRENCY", "RUB"),
    )
    menu.install()
    on_stop = getattr(deps, "on_stop", None)
    if callable(on_stop):
        on_stop("staff command menus", commands.stop)
    return menu.aiogram_router()
