"""``/start`` and ``/menu``: register the user and show the home screen as a fresh main message.

Deep links with a payload (``setup_…``, later ``ref_…``/``l_…``) are handled by routers included *before*
this one; anything they do not consume ends up here. Stage 2: the payload is parsed
(:func:`svbg.tg.user.deeplink.parse_start_payload`) and handed to the optional ``on_start`` hook, which may
keep it as a pending intent (stage 3b acts on it) and pick another first screen — e.g. the required channel
gate. Without the hook ``/start`` simply opens the menu. Stage 3b: the app passes the deep-link service's hook
(:meth:`svbg.deeplinks.service.DeeplinkService.start_hook`) wrapping its own gate (new-user event, required
channel, consent page), so ad tags, referral codes, promo links and targets are handled here.

While the bot has no owner yet (``OWNER_IDS`` empty and nobody promoted through the owner link) everybody
except the owner gets "Бот настраивается" — a public bot must not show half-configured screens (03 §2.2).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from svbg.content import defaults
from svbg.core.errors import Capturer, guard
from svbg.tg.user.deeplink import DeepLink, parse_start_payload

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenRouter
    from svbg.tg.user.directory import UserDirectory

__all__ = ["TEXTS", "TEXTS_EN", "StartHook", "build_start_router"]

log = logging.getLogger("svbg.tg.user")

TEXTS: Final = {
    "setting_up": "⏳ Бот настраивается. Загляните чуть позже.",
    "error": "⚠️ Что-то пошло не так. Мы уже знаем об ошибке, попробуйте ещё раз через минуту.",
}
TEXTS_EN: Final = {
    "setting_up": "⏳ The bot is being set up. Please come back a bit later.",
    "error": "⚠️ Something went wrong. We already know about it, please try again in a minute.",
}


def _text(key: str, lang: str | None) -> str:
    """``TEXTS[key]`` in ``lang`` (Russian fallback)."""
    return TEXTS_EN[key] if lang == "en" else TEXTS[key]


#: ``(user, chat_id, deep link or None)`` → ``(screen, arg)`` to open instead of the home screen, or ``None``.
StartHook = Callable[["UserCtx", int, DeepLink | None], Awaitable[tuple[str, Any] | None]]


_RAW_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _start_link(payload: str | None) -> DeepLink | None:
    """The parsed deep link; a well-formed payload the stage-2 parser refuses (``t_tiktok``: an ad code that
    only looks like a top-up) goes on as ``legacy_code`` — the deep-link service decides (exact ``ad_links``
    match first, 07 §2.4.4)."""
    link = parse_start_payload(payload)
    if link is not None or not payload:
        return link
    raw = payload.strip()
    if not _RAW_RE.match(raw) or raw.startswith("setup_"):
        return None
    return DeepLink("legacy_code", raw, raw)


def build_start_router(
    *,
    screens: ScreenRouter,
    users: UserDirectory,
    hub: Capturer | None,
    name: str = "svbg-start",
    on_start: StartHook | None = None,
) -> Router:
    router = Router(name=name)

    async def reply_error(_exc: BaseException, message: Message) -> None:
        try:
            code = (message.from_user.language_code or "") if message.from_user is not None else ""
            await message.answer(_text("error", "en" if code.lower().startswith("en") else "ru"))
        except TelegramAPIError as e:
            log.warning("could not tell the user about the error: %s", type(e).__name__)

    @router.message(CommandStart(), F.chat.type == "private")
    @router.message(Command("menu"), F.chat.type == "private")
    async def on_start_message(message: Message, command: CommandObject | None = None) -> None:
        tg_user = message.from_user
        if tg_user is None:
            return
        async with guard("tg:start", hub=hub, on_error=lambda exc: reply_error(exc, message)):
            user = await users.load(tg_user)
            if user is None:
                return  # a bot or a banned user: stay silent
            if user.role != "owner" and not await users.has_owner():
                await message.answer(_text("setting_up", user.lang))
                return
            screen: str = defaults.HOME
            arg: Any = None
            if on_start is not None:
                is_start = command is not None and command.command == "start"
                link = _start_link(command.args if is_start and command is not None else None)
                picked = await on_start(user, message.chat.id, link)
                if picked is not None:
                    screen, arg = picked
            await screens.show(user, message.chat.id, screen, arg, new=True)

    return router
