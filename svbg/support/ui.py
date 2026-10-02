"""Bot side of the support (07 §2.4.6): the «💬 Поддержка» screen, the user's and staff's messages, the
buttons of the ticket card.

* ``system:support`` (and the home button in modes ``tickets`` / ``both``) → screen ``spt``: «напишите
  вопрос» (arms the dialog) and/or the ``SUPPORT_URL`` link. In mode ``link`` the home button stays a plain
  link (:meth:`svbg.tg.user.base.Base.support_row`).
* Private messages (not a command, no form waiting) of a user in a dialog →
  :meth:`TicketService.user_message`; messages in the topics of the support group →
  :meth:`TicketService.staff_message`.
* Card buttons ``tk:c|e|b:<users.id>``: «Закрыть» (Support+), «Продлить» (``subs.grant``) and «Блок»
  (``users.ban``) — the role is re-read on every press; the last two open the form of «👤 Пользователи»
  (``au.f.days`` / ``au.f.ban``) in the presser's private chat. «Карточка» is the users module's own button.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any, Final

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from svbg.content.defaults import HOME
from svbg.core.errors import guard
from svbg.services import roles
from svbg.services.roles import Act
from svbg.support.service import CALLBACK, TicketService, copyable, t
from svbg.tg.ui.renderer import SYSTEM_SCREEN, nav_button
from svbg.tg.ui.view import Redirect, View
from svbg.tg.user.texts import t as user_t

if TYPE_CHECKING:
    from svbg.core.errors import Capturer
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["GO", "SCREEN", "SupportUi"]

log = logging.getLogger("svbg.support")

SCREEN: Final = "spt"
GO: Final = "tk.go"  # opens a «👤 Пользователи» form for a ticket's user in the presser's private chat
USERS_CARD: Final = "au"  # svbg.tg.admin.users.screens.SCREEN_CARD
_FORMS: Final = {"e": "au.f.days", "b": "au.f.ban"}  # svbg.tg.admin.users.screens.F_DAYS / F_BAN
_ACTS: Final = {"e": Act.SUBS_GRANT, "b": Act.USERS_BAN}
_BUTTON_RE: Final = re.compile(rf"^{CALLBACK}:([ceb]):(\d{{1,18}})$")
_GO_RE: Final = re.compile(r"^([eb]):(\d{1,18})$")
_OPENED: Final = "Открыл в личке с ботом"
_NO_DM: Final = "Не получилось открыть: напишите боту в личку /start и нажмите ещё раз"


class SupportUi:
    def __init__(self, service: TicketService, router: ScreenRouter, *, hub: Capturer | None = None) -> None:
        self.service = service
        self.router = router
        self.hub = hub

    def install(self) -> None:
        self.router.action(SYSTEM_SCREEN, "support")(self.sys_support)
        self.router.screen(SCREEN)(self.screen)
        self.router.screen(GO, required_role="support")(self.go)

    # ------------------------------------------------------------------ screens

    async def sys_support(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return Redirect(SCREEN)

    async def screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        svc, lang = self.service, ctx.lang
        link, tg = svc.link(), ctx.user.telegram_id
        dialog = svc.tickets_on() and svc.chat_id() is not None and tg is not None
        rows: list[list[InlineKeyboardButton]] = []
        if dialog:
            assert tg is not None
            svc.arm(tg)
        show_link = link is not None and (svc.mode() != "tickets" or not dialog)
        if show_link:
            rows.append([InlineKeyboardButton(text=t(lang, "link"), url=link)])
        rows.append([nav_button(user_t(lang, "btn_menu"), HOME)])
        text = t(lang, "prompt") if dialog else t(lang, "title") if show_link else t(lang, "unavailable")
        return View(text=text, keyboard=rows)

    async def go(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        match = _GO_RE.match(arg) if isinstance(arg, str) else None
        if match is None:
            return Redirect(HOME)
        form = _FORMS[match[1]]
        if form not in self.router.forms:  # «👤 Пользователи» is not wired: at least the card
            return Redirect(USERS_CARD, match[2])
        return await ctx.start_form(form, {"uid": int(match[2]), "op": uuid.uuid4().hex})

    # ------------------------------------------------------------------ aiogram

    def aiogram_router(self, name: str = "svbg-support") -> Router:
        router = Router(name=name)
        router.callback_query.register(self.on_button, F.data.startswith(f"{CALLBACK}:"))
        router.message.register(self.on_private, F.chat.type == "private")
        router.message.register(self.on_group, F.chat.type == "supergroup", F.message_thread_id)
        return router

    async def on_private(self, message: Message) -> None:
        handled = True
        async with guard("support:user", hub=self.hub, module="support"):
            handled = await self.handle_private(message)
        if not handled:
            raise SkipHandler

    async def handle_private(self, message: Message) -> bool:
        if not self.service.tickets_on() or message.from_user is None or not copyable(message):
            return False
        if (message.text or "").startswith("/") or await self.router.is_awaiting(message):
            return False
        return await self.service.user_message(message)

    async def on_group(self, message: Message) -> None:
        handled = True
        async with guard("support:staff", hub=self.hub, module="support"):
            handled = await self.service.staff_message(message)
        if not handled:
            raise SkipHandler

    async def on_button(self, query: CallbackQuery) -> None:
        text, alert = roles.DENIED, True
        async with guard("support:button", hub=self.hub, module="support"):
            text, alert = await self.press(query)
        try:
            await query.answer(text[:190], show_alert=alert)
        except TelegramAPIError as exc:
            log.warning("answerCallbackQuery failed: %s", type(exc).__name__)

    async def press(self, query: CallbackQuery) -> tuple[str, bool]:
        """A card button: ``(toast, alert)``; the role is checked against the database now."""
        match = _BUTTON_RE.match(query.data or "")
        if match is None:
            return roles.DENIED, True
        op, uid, tg_id = match[1], int(match[2]), query.from_user.id
        if op == "c":
            text = await self.service.close(uid, tg_id)
            return text, text == roles.DENIED
        if not roles.authorize(await self.service.actor(tg_id), _ACTS[op]):
            return roles.DENIED, True
        user = await self.router.user_loader(query.from_user)
        if user is None:
            return roles.DENIED, True
        shown = await self.router.show(user, tg_id, GO, f"{op}:{uid}", new=True)
        return (_OPENED, False) if shown else (_NO_DM, True)
