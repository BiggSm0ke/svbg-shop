"""Messages outside a click: billing's purchase message, the user path's own «ready» messages.

:class:`UserMessenger` implements :class:`svbg.billing.ports.Messenger`: billing asks it to *edit* the
purchase message in place (``orders.ui_ref``) — «⏳ Оформляю…» becomes «✅ Оплачено … + 🔗 Подключиться»
without any action of the user — or to send a new one when the old message cannot be edited (deleted,
older than 48 h). A screen with a picture (the default banner, the owner's own one) keeps it: its caption is
edited instead when the text fits a caption (1024); a longer text goes as a new message.
``Notice.screen`` is a content screen code: when the owner created such a screen its text wins (the
notice's ``params`` are its ``{placeholders}``), the notice's functional buttons are always kept.

Ordering: a background edit must never land *before* the screen the user's click is still drawing (the click
commits the purchase, then answers and edits the message into «⏳ Оформляю…»; the worker may finish the
purchase sooner than Telegram receives that edit, and the late «Оформляю…» would hide «🔗 Подключиться» for
good). With ``serialize`` (the screen router's per-user lock) an edit waits until the click is drawn — at
most :data:`SERIALIZE_WAIT_S`, then it goes ahead anyway (never a deadlock with a click that waits for a row
the job holds).

Errors: «message is not modified» counts as success; a message that is gone or cannot hold text → ``False``
(billing then sends a new one); the user blocked the bot → ``None`` from :meth:`send`; network failures and
Telegram 5xx propagate, so the job retries.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageCaption, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from svbg.billing.ports import Button, Notice, UiRef
from svbg.core.clock import now
from svbg.tg.ui import codec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import CAPTION_LIMIT, TEXT_LIMIT, as_markup, fit_text, utf16_len
from svbg.tg.ui.view import View
from svbg.tg.user import seeds
from svbg.tg.user.deps import Config, cfg_str
from svbg.tg.user.render import plain_view
from svbg.tg.user.texts import t

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from svbg.content.store import ContentStore
    from svbg.tg.user.directory import UserDirectory

__all__ = ["SERIALIZE_WAIT_S", "Caller", "Serializer", "UserMessenger", "link_button", "notice_button"]

log = logging.getLogger("svbg.tg.user.messenger")

#: ``call(method, chat_id)`` — the notifier (rate limits, 403 → ``None``) or the bot directly.
Caller = Callable[[TelegramMethod[Any], int], Awaitable[Any]]
#: ``serialize(telegram_id)`` — hold the user's screen lock (yields ``True`` when held, ``False`` on timeout).
Serializer = Callable[[int], AbstractAsyncContextManager[bool]]

#: How long a background edit waits for the click the user is making right now.
SERIALIZE_WAIT_S: Final = 3.0

_NOT_MODIFIED: Final = "message is not modified"
_NO_TEXT: Final = "there is no text in the message to edit"
_CANNOT_EDIT: Final = (
    "message to edit not found",
    "message_id_invalid",
    "message not found",
    "message can't be found",
    "message can't be edited",
    "there is no text in the message to edit",
)
_NO_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)


def link_button(text: str, url: str) -> InlineKeyboardButton:
    """A Web App button for an ``https`` page (Telegram requires it), a plain link otherwise."""
    if url.startswith("https://"):
        return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url), style="primary")
    return InlineKeyboardButton(text=text, url=url, style="primary")


def notice_button(button: Button) -> InlineKeyboardButton | None:
    """A billing button → Telegram. Logical actions map to the user path's callbacks."""
    if button.url is not None:
        return InlineKeyboardButton(text=button.text, url=button.url)
    if button.web_app is not None:
        return link_button(button.text, button.web_app)
    order_id = button.params.get("order_id")
    oid = str(int(order_id)) if isinstance(order_id, int) and not isinstance(order_id, bool) else None
    match button.action:
        case "menu":
            data = codec.encode(seeds.HOME)
        case "reorder" if oid is not None:
            data = codec.encode(seeds.BUY, "reorder", oid)
        case "topup" if oid is not None:
            data = codec.encode(seeds.SHORTFALL, codec.ACTION_OPEN, oid)
        case "connect":
            data = codec.encode(seeds.CONNECT)
        case _:
            log.warning("billing button with an unknown action %r skipped", button.action)
            return None
    style = "success" if button.action in ("reorder", "topup") else None
    return InlineKeyboardButton(text=button.text, callback_data=data, style=style)


class UserMessenger:
    """See module docstring."""

    def __init__(
        self,
        call: Caller,
        *,
        content: ContentStore | None = None,
        users: UserDirectory | None = None,
        config: Config | None = None,
        bot_username: Callable[[], str | None] = lambda: None,
        serialize: Serializer | None = None,
    ) -> None:
        self._call = call
        self._serialize = serialize
        self._content = content
        self._users = users
        self._config = config
        self._bot_username = bot_username

    # ------------------------------------------------------------------------------------------ helpers

    def lang_of(self, telegram_id: int | None) -> str:
        if telegram_id is not None and self._users is not None:
            cached = self._users.peek(telegram_id)
            if cached is not None:
                return cached.lang
        default = cfg_str(self._config, "DEFAULT_LANGUAGE", "ru") if self._config is not None else "ru"
        return default if default in ("ru", "en") else "ru"

    def notice_view(self, notice: Notice, lang: str) -> View:
        """The notice as a message: content text (if the owner made the screen) or the notice's own text."""
        rows: list[list[InlineKeyboardButton]] = []
        for row in notice.buttons:
            built = [b for b in (notice_button(x) for x in row) if b is not None]
            if built:
                rows.append(built)
        entry = None if self._content is None else self._content.get_screen(notice.screen)
        if entry is None or not entry.screen.enabled:
            text, _ = fit_text(notice.text, None, TEXT_LIMIT)
            return View(text=text or "…", keyboard=rows)
        values = {k: _plain(v) for k, v in notice.params.items()}
        user = UserCtx(0, lang=lang)
        return plain_view(
            user,
            self._content,
            notice.screen,
            values,
            top=rows,
            bot_username=self._bot_username(),
            fallback_text=notice.text,
        )

    @asynccontextmanager
    async def _after_click(self, chat_id: int) -> AsyncIterator[None]:
        """Wait for the user's click in progress (if any) before touching the message."""
        if self._serialize is None:
            yield
            return
        async with self._serialize(chat_id) as held:
            if not held:
                log.debug("edit for chat %s goes ahead without waiting for the click", chat_id)
            yield

    # ------------------------------------------------------------------------------------------ views

    async def edit_view(self, ref: UiRef, view: View) -> bool:
        """Edit ``ref`` into ``view``. ``False``: the message cannot become this view (send a new one)."""
        text, entities = fit_text(view.text, list(view.entities or ()) or None, TEXT_LIMIT)
        method = EditMessageText(
            chat_id=ref.chat_id,
            message_id=ref.message_id,
            text=text,
            entities=entities,
            parse_mode=None,
            link_preview_options=_NO_PREVIEW,
            reply_markup=_markup(view),
        )
        try:
            async with self._after_click(ref.chat_id):
                try:
                    await self._call(method, ref.chat_id)
                except TelegramBadRequest as e:
                    if _NO_TEXT not in (e.message or "").lower() or utf16_len(text) > CAPTION_LIMIT:
                        raise
                    # a screen with a picture (the default banner…): keep the picture, edit its caption
                    caption = EditMessageCaption(
                        chat_id=ref.chat_id,
                        message_id=ref.message_id,
                        caption=text,
                        caption_entities=entities,
                        parse_mode=None,
                        reply_markup=_markup(view),
                    )
                    await self._call(caption, ref.chat_id)
        except TelegramBadRequest as e:
            desc = (e.message or "").lower()
            if _NOT_MODIFIED in desc:
                return True
            if not any(s in desc for s in _CANNOT_EDIT):
                log.warning("edit of a user message refused: %s", e.message)
            return False
        except TelegramForbiddenError:
            return False
        return True

    async def send_view(self, chat_id: int, view: View) -> UiRef | None:
        """Send ``view`` as a new message. ``None``: the user blocked the bot."""
        text, entities = fit_text(view.text, list(view.entities or ()) or None, TEXT_LIMIT)
        method = SendMessage(
            chat_id=chat_id,
            text=text,
            entities=entities,
            parse_mode=None,
            link_preview_options=_NO_PREVIEW,
            reply_markup=_markup(view),
        )
        try:
            sent = await self._call(method, chat_id)
        except TelegramForbiddenError:
            return None
        if not isinstance(sent, Message):
            return None
        return UiRef(chat_id, sent.message_id, now())

    async def deliver(self, ref: UiRef | None, chat_id: int | None, view: View) -> UiRef | None:
        """Edit ``ref`` when possible, else send a new message to ``chat_id``."""
        if ref is not None and await self.edit_view(ref, view):
            return ref
        if chat_id is None:
            return None
        return await self.send_view(chat_id, view)

    # ------------------------------------------------------------------------------------------ Messenger

    async def edit(self, ref: UiRef, notice: Notice) -> bool:
        return await self.edit_view(ref, self.notice_view(notice, self.lang_of(ref.chat_id)))

    async def send(self, telegram_id: int, notice: Notice) -> UiRef | None:
        return await self.send_view(telegram_id, self.notice_view(notice, self.lang_of(telegram_id)))


def _markup(view: View) -> InlineKeyboardMarkup | None:
    return as_markup(view.keyboard)


def _plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return ""
    return str(value)


def menu_row(lang: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(lang, "btn_menu"), callback_data=codec.encode(seeds.HOME))]
