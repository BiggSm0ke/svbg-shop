"""Telegram side of the constructor: the premium-emoji probe sender, icon extraction/validation, downloads.

Everything goes through the screen router's :class:`~svbg.tg.ui.router.UiTransport`
(``call(method, chat_id)``), so tests drive it with a recording fake and production uses the live bot.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aiogram.exceptions import TelegramAPIError
from aiogram.methods import DeleteMessage, GetCustomEmojiStickers, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

from svbg.content.premium import ProbeEcho
from svbg.tg.banner import banner_scope
from svbg.tg.ui import codec
from svbg.tg.ui.edit_mode import ACTIONS

if TYPE_CHECKING:
    from svbg.tg.ui.router import UiTransport

__all__ = [
    "DOWNLOAD_LIMIT",
    "IconPick",
    "TransportProbeSender",
    "bot_downloader",
    "check_custom_emoji",
    "entities_json",
    "pick_icon",
]

log = logging.getLogger("svbg.tg.admin.content")

#: The cloud Bot API lets a bot download files up to 20 MB (``getFile``).
DOWNLOAD_LIMIT: Final = 20 * 1024 * 1024
NOOP_DATA: Final = codec.encode(ACTIONS, "noop")

REGULAR_STICKER: Final = "Это обычный стикер — пришлите премиум-эмодзи из набора эмодзи"


def entities_json(entities: list[MessageEntity] | None) -> list[dict[str, Any]]:
    """Bot API entities as plain JSON (what the content model stores)."""
    out: list[dict[str, Any]] = []
    for e in entities or ():
        raw = e.model_dump(mode="json", exclude_none=True)
        out.append({k: v for k, v in raw.items() if v is not None})
    return out


def _markup_json(markup: InlineKeyboardMarkup | None) -> dict[str, Any] | None:
    if markup is None:
        return None
    dumped: dict[str, Any] = markup.model_dump(mode="json", exclude_none=True)
    return dumped


class TransportProbeSender:
    """:class:`svbg.content.premium.ProbeSender` over the UI transport."""

    def __init__(self, transport: UiTransport) -> None:
        self._transport = transport

    async def send(self, chat_id: int, text: str, entities: list[dict[str, Any]], emoji_id: str) -> ProbeEcho:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="проверка", callback_data=NOOP_DATA, icon_custom_emoji_id=emoji_id
                    )
                ]
            ]
        )
        with banner_scope("off"):  # the probe reads back ``entities``: it must stay a plain text message
            sent = await self._transport.call(
                SendMessage(
                    chat_id=chat_id,
                    text=text,
                    entities=[MessageEntity.model_validate(e) for e in entities],
                    parse_mode=None,
                    reply_markup=markup,
                    disable_notification=True,
                ),
                chat_id=chat_id,
            )
        if not isinstance(sent, Message):
            raise TypeError("the probe was not delivered")
        return ProbeEcho(sent.message_id, entities_json(sent.entities), _markup_json(sent.reply_markup))

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self._transport.call(DeleteMessage(chat_id=chat_id, message_id=message_id), chat_id=chat_id)


@dataclass(frozen=True, slots=True)
class IconPick:
    emoji_id: str | None = None
    error: str | None = None


def pick_icon(message: Message) -> IconPick:
    """The custom emoji id from a message: a ``custom_emoji`` entity, or a custom-emoji sticker."""
    sticker = message.sticker
    if sticker is not None:
        if sticker.type == "custom_emoji" and sticker.custom_emoji_id:
            return IconPick(sticker.custom_emoji_id)
        return IconPick(error=REGULAR_STICKER)
    entities = message.entities if message.text is not None else message.caption_entities
    for e in entities or ():
        if e.type == "custom_emoji" and e.custom_emoji_id:
            return IconPick(e.custom_emoji_id)
    if message.text or message.caption:
        return IconPick(
            error="В сообщении нет премиум-эмодзи. Пришлите один эмодзи из премиум-набора "
            "(или стикер из набора эмодзи)."
        )
    return IconPick(error="Пришлите премиум-эмодзи сообщением или стикер из набора эмодзи.")


async def check_custom_emoji(transport: UiTransport, emoji_id: str, chat_id: int) -> bool | None:
    """``getCustomEmojiStickers``: ``True`` — a known id, ``False`` — unknown, ``None`` — the call failed."""
    try:
        stickers = await transport.call(GetCustomEmojiStickers(custom_emoji_ids=[emoji_id]), chat_id=chat_id)
    except (TelegramAPIError, OSError, TimeoutError) as e:
        log.warning("getCustomEmojiStickers failed: %s", type(e).__name__)
        return None
    if stickers is None:
        return None
    return any(getattr(s, "custom_emoji_id", None) == emoji_id for s in stickers)


Downloader = Callable[[str], Awaitable[bytes]]


def bot_downloader(holder: Any) -> Downloader:
    """``file_id → bytes`` through the live bot (``BotHolder``)."""

    async def download(file_id: str) -> bytes:
        bot = holder.get()
        if bot is None:
            raise RuntimeError("bot is not configured")
        file = await bot.get_file(file_id)
        if file.file_path is None:
            raise RuntimeError("Telegram returned no file path")
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf, timeout=120)
        return buf.getvalue()

    return download
