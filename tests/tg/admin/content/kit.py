"""Test kit of the constructor UI: the router harness + editor + media library + premium probe."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.methods import EditMessageText, GetCustomEmojiStickers, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    PhotoSize,
    Sticker,
)

from svbg.content.editing import ContentEditor
from svbg.content.media import MediaLibrary
from svbg.content.premium import PremiumService
from svbg.tg.admin.content.screens import ContentScreens
from svbg.tg.admin.content.telegram import TransportProbeSender
from svbg.tg.ui import codec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.edit_mode import ACTIONS
from svbg.tg.ui.view import Redirect
from tests.dbkit import CountingDatabase
from tests.tg.ui.ui_harness import DATE, Env, FakeTransport, callback, make_env, tg_user

OWNER = 1001
ADMIN = 1002
USER = 2001
EMOJI = "5368324170671202286"


class CTransport(FakeTransport):
    """FakeTransport + ``getCustomEmojiStickers`` and a Telegram-like echo of entities / reply markup.

    ``premium=False`` makes it behave like a bot whose owner has no Premium: custom emoji are stripped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.known_emoji: set[str] = {EMOJI}
        self.premium = True

    async def call(self, method: TelegramMethod[Any], *, chat_id: int) -> Any:
        if isinstance(method, GetCustomEmojiStickers):
            self.events.append("GetCustomEmojiStickers")
            self.calls.append(method)
            return [
                Sticker(
                    file_id=f"f{e}",
                    file_unique_id=f"u{e}",
                    type="custom_emoji",
                    width=100,
                    height=100,
                    is_animated=False,
                    is_video=False,
                    custom_emoji_id=e,
                )
                for e in method.custom_emoji_ids
                if e in self.known_emoji
            ]
        result = await super().call(method, chat_id=chat_id)
        if isinstance(method, SendMessage) and isinstance(result, Message):
            entities = list(method.entities or [])
            markup = method.reply_markup
            if not self.premium:
                entities = [e for e in entities if e.type != "custom_emoji"]
                if isinstance(markup, InlineKeyboardMarkup):
                    markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [b.model_copy(update={"icon_custom_emoji_id": None}) for b in row]
                            for row in markup.inline_keyboard
                        ]
                    )
            result = result.model_copy(
                update={
                    "entities": entities or None,
                    "reply_markup": markup if isinstance(markup, InlineKeyboardMarkup) else None,
                }
            )
        return result


def jpeg_bytes(size: tuple[int, int] = (64, 48)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


@dataclass
class CEnv:
    env: Env
    transport: CTransport
    editor: ContentEditor
    screens: ContentScreens
    premium: PremiumService
    library: MediaLibrary
    files: dict[str, bytes] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    @property
    def db(self) -> CountingDatabase:
        return self.env.db

    @property
    def router(self) -> Any:
        return self.env.router

    async def add(self, tg_id: int, role: str = "user", perms: list[str] | None = None, **kw: Any) -> UserCtx:
        ctx = await self.env.add(tg_id, role, perms=frozenset(perms or ()), **kw)
        if perms:
            import json

            await self.db.raw(
                "update users set perms = $1::jsonb where id = $2", json.dumps(perms), ctx.user_id
            )
        return ctx

    async def click(
        self, tg_id: int, screen: str, action: str = codec.ACTION_OPEN, arg: str | None = None
    ) -> None:
        await self.router.dispatch_callback(callback(tg_id, codec.encode(screen, action, arg)))

    async def act(self, tg_id: int, action: str, arg: str | None = None) -> None:
        await self.click(tg_id, ACTIONS, action, arg)

    async def send(self, message: Message) -> bool:
        return await self.screens.handle_message(message)

    # ---- what the user sees

    def last(self) -> TelegramMethod[Any]:
        shown = [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText | SendPhoto)]
        return shown[-1]

    def last_text(self) -> str:
        m = self.last()
        return str(getattr(m, "text", None) or getattr(m, "caption", None) or "")

    def buttons(self) -> list[tuple[str, str | None]]:
        markup = getattr(self.last(), "reply_markup", None)
        if not isinstance(markup, InlineKeyboardMarkup):
            return []
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    def button_data(self, text_part: str) -> str:
        for text, data in self.buttons():
            if text_part in text and data is not None:
                return data
        raise AssertionError(f"no button with {text_part!r} in {self.buttons()}")

    async def press(self, tg_id: int, text_part: str) -> None:
        await self.router.dispatch_callback(callback(tg_id, self.button_data(text_part)))

    @property
    def toasts(self) -> list[str | None]:
        return self.transport.toasts


def msg(
    tg_id: int,
    text: str | None = None,
    *,
    entities: list[dict[str, Any]] | None = None,
    photo: bool = False,
    sticker: str | None = None,
    sticker_type: str | None = None,
    message_id: int = 700,
    media_group_id: str | None = None,
) -> Message:
    extra: dict[str, Any] = {}
    if media_group_id is not None:
        extra["media_group_id"] = media_group_id
    if text is not None:
        extra["text"] = text
        if entities:
            extra["entities"] = [MessageEntity.model_validate(e) for e in entities]
    if photo:
        extra["photo"] = [
            PhotoSize(file_id="small", file_unique_id="s1", width=10, height=8, file_size=100),
            PhotoSize(file_id="PHOTO-ID", file_unique_id="p1", width=64, height=48, file_size=2000),
        ]
    if sticker_type is not None:
        extra["sticker"] = Sticker(
            file_id="st",
            file_unique_id="stu",
            type=sticker_type,
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
            custom_emoji_id=sticker,
        )
    return Message(
        message_id=message_id,
        date=DATE,
        chat=Chat(id=tg_id, type="private"),
        from_user=tg_user(tg_id),
        **extra,
    )


@asynccontextmanager
async def build_cenv(db: CountingDatabase, tmp: Path) -> AsyncIterator[CEnv]:
    media_root = tmp / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    async with make_env(db, media_root=media_root) as env:
        transport = CTransport()
        env.router.transport = transport
        env.transport = transport
        editor = ContentEditor(db, env.content)
        library = MediaLibrary(db, media_root)
        sender = TransportProbeSender(transport)
        premium = PremiumService(db, lambda: sender, bot=lambda: (42, "42:TOKEN"))
        files: dict[str, bytes] = {"PHOTO-ID": jpeg_bytes()}
        downloads: list[str] = []

        async def download(file_id: str) -> bytes:
            downloads.append(file_id)
            return files[file_id]

        async def owner_ids() -> list[int]:
            return [OWNER]

        screens = ContentScreens(
            env.router,
            env.content,
            editor,
            db,
            library=library,
            premium=premium,
            owner_ids=owner_ids,
            download=download,
        )
        screens.install()

        @env.router.action("sys", "buy")
        async def _buy(_ctx: Any, _arg: Any) -> Redirect:
            return Redirect("home")

        yield CEnv(env, transport, editor, screens, premium, library, files, downloads)
