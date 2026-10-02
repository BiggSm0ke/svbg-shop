"""«📄 Страницы» — the owner's editor of FAQ, rules, offer, consent and custom pages (04 §9.1
``settings.business``).

Screens: ``pgs`` (list), ``pgs.c`` (card: status, version, consent, a text preview), ``pgs.w``
(«пришлите текст сообщением»), ``pgs.v`` (versions → «вернуть»), ``pgs.d`` (delete a custom page).

The text is written as an ordinary Telegram message: any formatting the owner uses (bold, links, spoilers,
premium emoji, quotes…) arrives as entities and is stored as is — nothing is lost to HTML/Markdown parsing.
While ``pgs.w`` waits, the next message of that admin is captured by :meth:`PageAdminScreens.handle_message`
(include :meth:`aiogram_router` **before** the screen router). The capture lives in memory for
:data:`CAPTURE_TTL_S` (a restart drops it: the admin presses «✏️ Текст» again) and is bound to the page
version it started from (compare-and-set: a concurrent edit is not overwritten).

Access is checked by the router on every screen, action and form step, and again for a captured message.
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message

from svbg.content.defaults import HOME
from svbg.pages.service import DEFAULT_LANG, Page, PageError, PageService
from svbg.pages.user import ALIAS_PREFIX, PageUserScreens
from svbg.pages.user import SCREEN as USER_SCREEN
from svbg.tg.ui.forms import Field, Form
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "CAPTURE_TTL_S",
    "PERM",
    "SCREEN_CARD",
    "SCREEN_LIST",
    "SCREEN_WAIT",
    "PageAdminScreens",
]

log = logging.getLogger("svbg.tg.admin.pages")

PERM: Final = "settings.business"
SCREEN_LIST: Final = "pgs"
SCREEN_CARD: Final = "pgs.c"
SCREEN_WAIT: Final = "pgs.w"
SCREEN_VERSIONS: Final = "pgs.v"
SCREEN_DELETE: Final = "pgs.d"
ACTIONS: Final = "pgsa"
F_NEW: Final = "pgs.f.new"
F_TITLE: Final = "pgs.f.title"
CAPTURE_TTL_S: Final = 900.0
LANGS: Final = ("ru", "en")
PREVIEW_CHARS: Final = 300

_T: Final[dict[str, str]] = {
    "list_title": "📄 <b>Страницы</b>\n"
    "FAQ, правила, оферта, согласие и свои страницы. Включённые видят пользователи.",
    "new": "➕ Новая страница",
    "menu": "🏠 Меню",
    "to_list": "⬅️ К страницам",
    "to_card": "⬅️ К странице",
    "on": "🟢 включена",
    "off": "⏸ выключена",
    "b_text": "✏️ Текст",
    "b_text_lang": "🌐 Текст ({lang})",
    "b_title": "🏷 Название",
    "b_on": "▶️ Включить",
    "b_off": "⏸ Выключить",
    "b_preview": "👁 Как видит пользователь",
    "b_versions": "🕘 Версии",
    "b_consent": "📢 Запросить согласие заново",
    "b_delete": "🗑 Удалить",
    "cancel": "✖️ Отмена",
    "wait": "✏️ <b>{title}</b> ({lang})\n\nПришлите новый текст страницы одним сообщением. "
    "Оформление Telegram (жирный, ссылки, спойлеры, цитаты, премиум-эмодзи) сохранится как есть. "
    "До 4096 символов.",
    "wait_text_only": "Нужен текст сообщением (фото и файлы на страницах не поддерживаются).",
    "saved": "✅ Сохранено",
    "consent_on": "Согласие: пользователи принимают версию {v}",
    "consent_off": "Согласие: не запрашивается (включите страницу)",
    "consent_asked": "📢 Согласие запрошено заново — пользователи примут текст при следующем /start.",
    "versions_title": "🕘 <b>Версии</b> · {title}\n"
    "Нажмите на версию, чтобы вернуть её (станет новой версией).",
    "restored": "✅ Возвращена версия {v}",
    "del_title": "🗑 Удалить страницу <b>{title}</b>? Это нельзя отменить.",
    "del_yes": "🗑 Да, удалить",
    "deleted": "🗑 Удалено",
    "not_found": "Страница не найдена",
    "f_code": "✏️ Код новой страницы — латиница и «_», например delivery. Он будет в кнопке "
    "«screen:page.<код>».",
    "f_title": "🏷 Название страницы (видно в списке и в заголовке):",
}


@dataclass(frozen=True, slots=True)
class _Capture:
    code: str
    lang: str
    version: int
    expires: float


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


class PageAdminScreens:
    def __init__(
        self,
        router: ScreenRouter,
        service: PageService,
        *,
        user_screens: PageUserScreens | None = None,
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
    ) -> None:
        self.router = router
        self.service = service
        self.user_screens = user_screens
        self.timezone = timezone
        self._capture: dict[int, _Capture] = {}
        self._notes: dict[int, str] = {}
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
            (SCREEN_CARD, self._card_screen),
            (SCREEN_WAIT, self._wait_screen),
            (SCREEN_VERSIONS, self._versions_screen),
            (SCREEN_DELETE, self._delete_screen),
        ):
            r.screen(code, **guard)(fn)
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "new": self._a_new,
            "title": self._a_title,
            "en": self._a_enabled,
            "cons": self._a_consent,
            "rst": self._a_restore,
            "del": self._a_delete,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(fn)
        for form in (
            Form(
                F_NEW,
                (
                    Field("code", _T["f_code"], text_validator(max_len=24)),
                    Field("title", _T["f_title"], text_validator(max_len=64)),
                ),
                self._f_new,
            ),
            Form(F_TITLE, (Field("title", _T["f_title"], text_validator(max_len=64)),), self._f_title),
        ):
            r.form(
                Form(
                    form.name,
                    form.fields,
                    on_done=form.on_done,
                    on_cancel=self._cancel,
                    required_role="admin",
                    perm=PERM,
                )
            )

    def aiogram_router(self, name: str = "svbg-pages-admin") -> Router:
        """``/pages`` and the captured page texts (include before the screen router)."""
        router = Router(name=name)

        async def on_command(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        async def capturing(message: Message) -> bool:
            return self.capturing(message)

        async def on_message(message: Message) -> None:
            if not await self.handle_message(message):
                raise SkipHandler

        router.message.register(on_command, Command("pages"))
        router.message.register(on_message, capturing)
        return router

    async def _allowed(self, message: Message) -> UserCtx | None:
        if message.from_user is None or message.chat.type != "private":
            return None
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for a page message")
            return None
        if user is None or not (user.at_least("admin") and user.has_perm(PERM)):
            return None
        return user

    async def handle_command(self, message: Message) -> bool:
        user = await self._allowed(message)
        if user is None:
            return False
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    # ------------------------------------------------------------ capture

    def capturing(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        cap = self._capture.get(message.from_user.id)
        if cap is None:
            return False
        if cap.expires < time.monotonic():
            self._capture.pop(message.from_user.id, None)
            return False
        return True

    async def handle_message(self, message: Message) -> bool:
        """The page text the admin sent after «✏️ Текст». ``False``: not ours (the update goes on)."""
        if not self.capturing(message):
            return False
        assert message.from_user is not None
        tg_id = message.from_user.id
        user = await self._allowed(message)
        if user is None:  # rights were taken away meanwhile
            self._capture.pop(tg_id, None)
            return False
        cap = self._capture[tg_id]
        text = message.text if message.text is not None else message.caption
        entities = message.entities if message.text is not None else message.caption_entities
        if text is not None and text.startswith("/"):
            self._capture.pop(tg_id, None)
            if text.split()[0].split("@")[0].lower() != "/cancel":
                return False  # another command abandons the capture
            await self.router.show(user, message.chat.id, SCREEN_CARD, cap.code, new=True)
            return True
        if not text or message.caption is not None:
            self._notes[user.user_id] = _T["wait_text_only"]
            await self.router.show(user, message.chat.id, SCREEN_WAIT, f"{cap.code}:{cap.lang}", new=True)
            return True
        try:
            await self.service.save_text(
                cap.code, cap.lang, text, entities, (user.user_id, user.role), expected_version=cap.version
            )
        except PageError as e:
            self._capture.pop(tg_id, None)
            self._notes[user.user_id] = f"⚠️ {e}"
            await self.router.show(user, message.chat.id, SCREEN_CARD, cap.code, new=True)
            return True
        self._capture.pop(tg_id, None)
        self._notes[user.user_id] = _T["saved"]
        await self.router.show(user, message.chat.id, SCREEN_CARD, cap.code, new=True)
        return True

    # ------------------------------------------------------------ helpers

    def _page(self, code: Any) -> Page | None:
        return self.service.get(code) if isinstance(code, str) else None

    @staticmethod
    def _actor(ctx: ScreenCtx) -> tuple[int | None, str | None]:
        return (ctx.user.user_id, ctx.user.role)

    def _note(self, ctx: ScreenCtx) -> str | None:
        return self._notes.pop(ctx.user.user_id, None)

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return Redirect(SCREEN_LIST)

    def _when(self, page: Page) -> str:
        if page.updated_at is None:
            return "—"
        try:
            return page.updated_at.astimezone(ZoneInfo(self.timezone())).strftime("%d.%m.%Y %H:%M")
        except (ZoneInfoNotFoundError, ValueError):
            return page.updated_at.strftime("%d.%m.%Y %H:%M UTC")

    # ------------------------------------------------------------ list & card

    async def _list_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        rows: list[list[InlineKeyboardButton]] = []
        for page in self.service.all():
            state = "🟢" if page.enabled else "⏸"
            label = f"{state} {page.title_for(DEFAULT_LANG)} · v{page.version}"
            rows.append([nav_button(label[:64], SCREEN_CARD, arg=page.code)])
        rows.append([nav_button(_T["new"], ACTIONS, "new")])
        rows.append([nav_button(_T["menu"], HOME)])
        return View(text=_T["list_title"], parse_mode="HTML", keyboard=rows)

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        return self.card_view(page, note=self._note(ctx))

    def card_view(self, page: Page, *, note: str | None = None) -> View:
        block = page.block_for(DEFAULT_LANG)
        preview = block.text if block is not None else ""
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS] + "…"
        langs = ", ".join(sorted(page.body)) or "—"
        lines = [
            f"<b>{_esc(page.title_for(DEFAULT_LANG))}</b> · {_T['on'] if page.enabled else _T['off']}",
            f"Код: <code>{page.code}</code> · версия {page.version} · {self._when(page)}",
            f"Языки текста: {langs}",
            f"Кнопка в конструкторе: <code>screen:{ALIAS_PREFIX}{page.code}</code>",
        ]
        if page.kind == "consent":
            lines.append(
                _T["consent_on"].format(v=page.consent_version)
                if page.enabled and page.consent_version
                else _T["consent_off"]
            )
        lines += ["", f"<blockquote>{_esc(preview)}</blockquote>"]
        if note:
            lines.insert(0, f"{_esc(note)}\n")
        code = page.code
        rows: list[list[InlineKeyboardButton]] = [
            [
                nav_button(_T["b_text"], SCREEN_WAIT, arg=f"{code}:{DEFAULT_LANG}"),
                *(
                    nav_button(_T["b_text_lang"].format(lang=lang.upper()), SCREEN_WAIT, arg=f"{code}:{lang}")
                    for lang in LANGS
                    if lang != DEFAULT_LANG
                ),
            ],
            [
                nav_button(_T["b_title"], ACTIONS, "title", code),
                nav_button(_T["b_versions"], SCREEN_VERSIONS, arg=code),
            ],
            [nav_button(_T["b_off"] if page.enabled else _T["b_on"], ACTIONS, "en", code)],
            [nav_button(_T["b_preview"], USER_SCREEN, arg=code)],
        ]
        if page.kind == "consent" and page.enabled:
            rows.append([nav_button(_T["b_consent"], ACTIONS, "cons", f"{code}:{page.version}")])
        if not page.system:
            rows.append([nav_button(_T["b_delete"], SCREEN_DELETE, arg=code)])
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ text capture

    async def _wait_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        page = self._page(parts[0]) if len(parts) == 2 and parts[1] in LANGS else None
        if page is None or ctx.user.telegram_id is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        lang = parts[1]
        self._capture[ctx.user.telegram_id] = _Capture(
            page.code, lang, page.version, time.monotonic() + CAPTURE_TTL_S
        )
        await self.router.ui_state.set_awaiting(
            ctx.user.user_id, None
        )  # a half-filled form must not eat the text
        text = _T["wait"].format(title=_esc(page.title_for(DEFAULT_LANG)), lang=lang.upper())
        note = self._note(ctx)
        if note:
            text = f"⚠️ {_esc(note)}\n\n{text}"
        return View(
            text=text, parse_mode="HTML", keyboard=[[nav_button(_T["cancel"], SCREEN_CARD, arg=page.code)]]
        )

    # ------------------------------------------------------------ actions

    async def _a_new(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await ctx.start_form(F_NEW)

    async def _f_new(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        try:
            page = await self.service.create(
                str(data.get("code") or ""), str(data.get("title") or ""), self._actor(ctx)
            )
        except PageError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["new"], ACTIONS, "new"), nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        if self.user_screens is not None:
            self.user_screens.register_alias(page.code)
        return self.card_view(page, note=_T["saved"])

    async def _a_title(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None:
            return Toast(_T["not_found"])
        return await ctx.start_form(F_TITLE, {"code": page.code})

    async def _f_title(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        try:
            page = await self.service.set_title(
                str(data.get("code")), DEFAULT_LANG, str(data.get("title") or ""), self._actor(ctx)
            )
        except PageError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        return self.card_view(page, note=_T["saved"])

    async def _a_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None:
            return Toast(_T["not_found"])
        try:
            updated = await self.service.set_enabled(page.code, not page.enabled, self._actor(ctx))
        except PageError as e:
            return Toast(str(e), alert=True)
        return self.card_view(updated)

    async def _a_consent(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.split(":") if isinstance(arg, str) else []
        page = self._page(parts[0]) if len(parts) == 2 else None
        if page is None or page.kind != "consent":
            return Toast(_T["not_found"])
        if parts[1] != str(page.version):
            return self.card_view(page, note="Текст уже изменили — проверьте его ещё раз")
        try:
            updated = await self.service.request_consent(page.code, self._actor(ctx))
        except PageError as e:
            return Toast(str(e), alert=True)
        return self.card_view(updated, note=_T["consent_asked"])

    async def _versions_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        rows: list[list[InlineKeyboardButton]] = []
        tz = self.timezone()
        for v in await self.service.versions(page.code):
            try:
                when = v.created_at.astimezone(ZoneInfo(tz)).strftime("%d.%m %H:%M")
            except (ZoneInfoNotFoundError, ValueError):
                when = v.created_at.strftime("%d.%m %H:%M")
            mark = "• " if v.version == page.version else ""
            rows.append(
                [
                    nav_button(
                        f"{mark}v{v.version} · {when} · {v.preview}"[:64],
                        ACTIONS,
                        "rst",
                        f"{page.code}:{v.version}",
                    )
                ]
            )
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=page.code)])
        return View(
            text=_T["versions_title"].format(title=_esc(page.title_for(DEFAULT_LANG))),
            parse_mode="HTML",
            keyboard=rows,
        )

    async def _a_restore(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.rsplit(":", 1) if isinstance(arg, str) else []
        page = self._page(parts[0]) if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) < 10 else None
        if page is None:
            return Toast(_T["not_found"])
        version = int(parts[1])
        if version == page.version:
            return Toast("Это текущая версия")
        try:
            updated = await self.service.restore(page.code, version, self._actor(ctx))
        except PageError as e:
            return Toast(str(e), alert=True)
        return self.card_view(updated, note=_T["restored"].format(v=version))

    async def _delete_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None or page.system:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        rows = [
            [
                nav_button(_T["del_yes"], ACTIONS, "del", page.code, style="danger"),
                nav_button(_T["cancel"], SCREEN_CARD, arg=page.code),
            ]
        ]
        return View(
            text=_T["del_title"].format(title=_esc(page.title_for(DEFAULT_LANG))),
            parse_mode="HTML",
            keyboard=rows,
        )

    async def _a_delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self._page(arg)
        if page is None:
            return Toast(_T["not_found"])
        try:
            await self.service.delete(page.code, self._actor(ctx))
        except PageError as e:
            return Toast(str(e), alert=True)
        return Redirect(SCREEN_LIST, toast=_T["deleted"])
