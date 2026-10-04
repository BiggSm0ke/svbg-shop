"""«📣 Рекламные ссылки» — the owner's ad-link screens (право ``promo``; 01 §1.4, 06 §2.7).

Screens: ``ads`` (list from memory: clicks per link), ``ads.c`` (card: the link to copy, the funnel
«переходы → регистрации → триалы → оплатили → выручка» in one SQL, the imported Bedolaga bonus for reference),
``ads.d`` (delete — only a link nobody came through). Forms: new link (name, code or «Пропустить»), rename,
change the code (only before the first click). Access is checked by the router on every step; arguments
from callbacks are re-validated.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CopyTextButton, InlineKeyboardButton, Message

from svbg.ads.service import AdError, AdLink, AdService, check_new_code
from svbg.core.money import format_money
from svbg.tg.admin import nav
from svbg.tg.ui.forms import Field, Form, ValidationError
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["ACTIONS", "PERM", "SCREEN_CARD", "SCREEN_LIST", "AdAdminScreens"]

log = logging.getLogger("svbg.ads.admin")

PERM: Final = "promo"
SCREEN_LIST: Final = "ads"
SCREEN_CARD: Final = "ads.c"
SCREEN_DELETE: Final = "ads.d"
ACTIONS: Final = "adsa"
F_NEW: Final = "ads.f.new"
F_TITLE: Final = "ads.f.title"
F_CODE: Final = "ads.f.code"
_ID_RE: Final = re.compile(r"^\d{1,18}$")
PAGE: Final = 10  # links per page of the list

_T: Final[dict[str, str]] = {
    "list_hint": "У каждой ссылки свои переходы, пробные и оплаты. Человек засчитывается той ссылке, "
    "по которой пришёл впервые.",
    "list_empty": "Ссылок пока нет. Нажмите «➕ Новая ссылка» и дайте её блогеру или поставьте в пост.",
    "new": "➕ Новая ссылка",
    "to_list": "⬅️ Реклама",
    "copy": "📋 Скопировать ссылку",
    "b_on": "▶️ Включить",
    "b_off": "⏸ Выключить",
    "b_title": "✏️ Название",
    "b_code": "🔤 Код",
    "b_delete": "🗑 Удалить",
    "del_title": "🗑 Удалить ссылку <b>{title}</b>? Это нельзя отменить.",
    "del_yes": "🗑 Да, удалить",
    "cancel": "✖️ Отмена",
    "deleted": "🗑 Удалено",
    "saved": "✅ Сохранено",
    "created": "✅ Ссылка создана",
    "not_found": "Ссылка не найдена",
    "f_title": "✏️ Название ссылки (видно только вам), например «Канал Ивана»:",
    "f_code": "🔤 Код в ссылке: 3–32 символа, латиница, цифры, «_» и «-» (например, ivan_tg). "
    "«Пропустить» — придумаю сам.",
    "f_code_edit": "🔤 Новый код в ссылке: 3–32 символа, латиница, цифры, «_» и «-».",
}


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _code_validator(raw: str) -> str:
    try:
        return check_new_code(raw)
    except AdError as e:
        raise ValidationError(str(e)) from None


class AdAdminScreens:
    def __init__(
        self, router: ScreenRouter, service: AdService, *, currency: Callable[[], str] = lambda: "RUB"
    ) -> None:
        self.router = router
        self.service = service
        self.currency = currency
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard = {"required_role": "admin", "perm": PERM}
        for code, fn in (
            (SCREEN_LIST, self._list_screen),
            (SCREEN_CARD, self._card_screen),
            (SCREEN_DELETE, self._delete_screen),
        ):
            r.screen(code, **guard)(fn)
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "new": self._a_new,
            "en": self._a_enabled,
            "title": self._a_title,
            "code": self._a_code,
            "del": self._a_delete,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(fn)
        for form in (
            Form(
                F_NEW,
                (
                    Field("title", _T["f_title"], text_validator(max_len=128)),
                    Field("code", _T["f_code"], _code_validator, optional=True),
                ),
                self._f_new,
            ),
            Form(F_TITLE, (Field("title", _T["f_title"], text_validator(max_len=128)),), self._f_title),
            Form(F_CODE, (Field("code", _T["f_code_edit"], _code_validator),), self._f_code),
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

    def aiogram_router(self, name: str = "svbg-ads-admin") -> Router:
        """``/ads`` in a private chat (owner, admin with ``promo``)."""
        router = Router(name=name)

        async def on_command(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        router.message.register(on_command, Command("ads"))
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /ads")
            return False
        if user is None or not (user.at_least("admin") and user.has_perm(PERM)):
            return False
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _actor(ctx: ScreenCtx) -> tuple[int | None, str | None]:
        return (ctx.user.user_id, ctx.user.role)

    def _link(self, arg: Any) -> AdLink | None:
        return self.service.get(int(arg)) if isinstance(arg, str) and _ID_RE.match(arg) else None

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return Redirect(SCREEN_LIST)

    def _money(self, amount: int) -> str:
        try:
            return format_money(amount, self.currency(), "ru")
        except (ValueError, KeyError):
            return str(amount)

    # ------------------------------------------------------------ screens

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        links = self.service.all()
        pages = max(1, -(-len(links) // PAGE))
        page = int(arg) if isinstance(arg, str) and arg.isdigit() and len(arg) < 6 else 0
        page = min(page, pages - 1)
        rows = [
            [
                nav_button(
                    f"{'🟢' if link.enabled else '⏸'} {link.title} · {link.clicks} перех."[:64],
                    SCREEN_CARD,
                    arg=str(link.id),
                )
            ]
            for link in links[page * PAGE : (page + 1) * PAGE]
        ]
        if pages > 1:
            pager = []
            if page > 0:
                pager.append(nav_button("◀️", SCREEN_LIST, arg=str(page - 1)))
            pager.append(nav_button(f"{page + 1} из {pages}", SCREEN_LIST, arg=str(page)))
            if page < pages - 1:
                pager.append(nav_button("▶️", SCREEN_LIST, arg=str(page + 1)))
            rows.append(pager)
        rows.append([nav_button(_T["new"], ACTIONS, "new")])
        rows.append(nav.back_row(SCREEN_LIST))
        lines = [nav.header(SCREEN_LIST), "", _T["list_hint"]]
        if not links:
            lines += ["", _T["list_empty"]]
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        return await self.card_view(link)

    async def card_view(self, link: AdLink, *, note: str | None = None) -> View:
        stats = await self.service.stats(link.id)
        url = link.url(self.router.transport.bot_username)
        lines = [
            f"📣 <b>{_esc(link.title)}</b> · {'🟢 работает' if link.enabled else '⏸ выключена'}",
            f"Код: <code>{_esc(link.code)}</code>",
        ]
        if url:
            lines.append(f"🔗 <code>{_esc(url)}</code>")
        lines += [
            "",
            f"Переходы: {link.clicks}",
            f"Пришло новых: {stats.users} (за 30 дней: {stats.users_30d})",
            f"Взяли триал: {stats.trials} · Оплатили: {stats.payers}",
            f"Выручка: {self._money(stats.revenue_minor)}",
        ]
        if link.has_bonus:
            lines.append(f"Бонус из Bedolaga: {_esc(str(link.bonus.get('type')))} (не выдаётся)")
        if link.source == "import":
            lines.append("Перенесена из Bedolaga")
        if note:
            lines.insert(0, f"{note}\n")
        lid = str(link.id)
        rows: list[list[InlineKeyboardButton]] = []
        if url:
            rows.append([InlineKeyboardButton(text=_T["copy"], copy_text=CopyTextButton(text=url))])
        rows.append([nav_button(_T["b_off"] if link.enabled else _T["b_on"], ACTIONS, "en", lid)])
        edit = [nav_button(_T["b_title"], ACTIONS, "title", lid)]
        if link.clicks == 0 and stats.users == 0:
            edit += [
                nav_button(_T["b_code"], ACTIONS, "code", lid),
                nav_button(_T["b_delete"], SCREEN_DELETE, arg=lid),
            ]
        rows.append(edit)
        rows.append(nav.with_admin([nav_button(_T["to_list"], SCREEN_LIST)]))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _delete_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        rows = [
            [
                nav_button(_T["del_yes"], ACTIONS, "del", str(link.id), style="danger"),
                nav_button(_T["cancel"], SCREEN_CARD, arg=str(link.id)),
            ]
        ]
        return View(text=_T["del_title"].format(title=_esc(link.title)), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ actions

    async def _a_new(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await ctx.start_form(F_NEW)

    async def _f_new(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        try:
            link = await self.service.create(
                self._actor(ctx), title=str(data.get("title") or ""), code=data.get("code")
            )
        except AdError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["new"], ACTIONS, "new"), nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        return await self.card_view(link, note=_T["created"])

    async def _a_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Toast(_T["not_found"])
        try:
            updated = await self.service.update(link.id, self._actor(ctx), enabled=not link.enabled)
        except AdError as e:
            return Toast(str(e), alert=True)
        return await self.card_view(updated)

    async def _a_title(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Toast(_T["not_found"])
        return await ctx.start_form(F_TITLE, {"id": link.id})

    async def _a_code(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Toast(_T["not_found"])
        return await ctx.start_form(F_CODE, {"id": link.id})

    async def _edit_done(self, ctx: ScreenCtx, data: dict[str, Any], **changes: Any) -> HandlerResult:
        link_id = data.get("id")
        if not isinstance(link_id, int):
            return Redirect(SCREEN_LIST)
        try:
            updated = await self.service.update(link_id, self._actor(ctx), **changes)
        except AdError as e:
            return View(
                text=f"⚠️ {_esc(str(e))}",
                parse_mode="HTML",
                keyboard=[[nav_button(_T["to_list"], SCREEN_LIST)]],
            )
        return await self.card_view(updated, note=_T["saved"])

    async def _f_title(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._edit_done(ctx, data, title=data.get("title"))

    async def _f_code(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._edit_done(ctx, data, code=data.get("code"))

    async def _a_delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        link = self._link(arg)
        if link is None:
            return Toast(_T["not_found"])
        try:
            await self.service.delete(link.id, self._actor(ctx))
        except AdError as e:
            return Toast(str(e), alert=True)
        return Redirect(SCREEN_LIST, toast=_T["deleted"])
