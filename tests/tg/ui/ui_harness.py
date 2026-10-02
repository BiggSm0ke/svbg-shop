"""Test doubles for router/forms tests: a recording Telegram transport, an error hub and builders."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageText,
    SendAnimation,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    PhotoSize,
    User,
)

from svbg.content.store import ContentStore
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.dbkit import CountingDatabase, add_user

DATE = datetime(2026, 10, 1, tzinfo=UTC)
_SENDS = (SendMessage, SendPhoto, SendAnimation, SendVideo, SendDocument)
_EDITS = (EditMessageText, EditMessageCaption, EditMessageMedia)


class FakeTransport:
    bot_id: int | None = 42
    bot_username: str | None = "svbg_bot"

    def __init__(self) -> None:
        self.calls: list[TelegramMethod[Any]] = []
        self.answers: list[AnswerCallbackQuery] = []
        self.events: list[str] = []
        self.failures: list[tuple[type, Exception]] = []
        self.delay: dict[type, float] = {}
        self._ids = itertools.count(1000)

    def fail_next(self, method_cls: type, exc: Exception) -> None:
        self.failures.append((method_cls, exc))

    async def answer(self, method: AnswerCallbackQuery) -> None:
        self.answers.append(method)
        self.events.append("answer")

    async def call(self, method: TelegramMethod[Any], *, chat_id: int) -> Any:
        self.events.append(type(method).__name__)
        self.calls.append(method)
        if type(method) in self.delay:
            await asyncio.sleep(self.delay[type(method)])
        for i, (cls, exc) in enumerate(self.failures):
            if isinstance(method, cls):
                del self.failures[i]
                raise exc
        if isinstance(method, _SENDS):
            extra: dict[str, Any] = {}
            if isinstance(method, SendPhoto):
                extra["photo"] = [
                    PhotoSize(file_id="small", file_unique_id="u1", width=10, height=10),
                    PhotoSize(file_id="TG-FILE-ID", file_unique_id="u2", width=100, height=100),
                ]
                extra["caption"] = method.caption
            elif isinstance(method, SendMessage):
                extra["text"] = method.text
            return Message(
                message_id=next(self._ids), date=DATE, chat=Chat(id=chat_id, type="private"), **extra
            )
        if isinstance(method, (*_EDITS, DeleteMessage)):
            return True
        return True

    def of(self, cls: type) -> list[Any]:
        return [c for c in self.calls if isinstance(c, cls)]

    @property
    def toasts(self) -> list[str | None]:
        return [a.text for a in self.answers]


@dataclass
class FakeHub:
    captured: list[tuple[BaseException, str, dict[str, Any]]] = field(default_factory=list)

    async def capture(self, exc: BaseException, place: str, **kw: Any) -> str | None:
        self.captured.append((exc, place, kw))
        return None


class Users:
    """User loader backed by a dict ``telegram_id -> UserCtx`` (tests change roles between clicks)."""

    def __init__(self) -> None:
        self.by_tg: dict[int, UserCtx] = {}
        self.fail: Exception | None = None

    async def __call__(self, tg_user: User) -> UserCtx | None:
        if self.fail is not None:
            raise self.fail
        return self.by_tg.get(tg_user.id)


def tg_user(tg_id: int) -> User:
    return User(id=tg_id, is_bot=False, first_name="Тест", language_code="ru")


def callback(
    tg_id: int,
    data: str | None,
    *,
    message_id: int = 10,
    photo: bool = False,
    inaccessible: bool = False,
    cq_id: str = "cq1",
) -> CallbackQuery:
    chat = Chat(id=tg_id, type="private")
    msg: Message | InaccessibleMessage
    if inaccessible:
        msg = InaccessibleMessage(chat=chat, message_id=message_id, date=0)
    elif photo:
        msg = Message(
            message_id=message_id,
            date=DATE,
            chat=chat,
            photo=[PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)],
            caption="old",
        )
    else:
        msg = Message(message_id=message_id, date=DATE, chat=chat, text="old")
    return CallbackQuery(id=cq_id, from_user=tg_user(tg_id), chat_instance="ci", data=data, message=msg)


def text_message(
    tg_id: int, text: str | None, *, message_id: int = 500, chat_type: str = "private"
) -> Message:
    return Message(
        message_id=message_id,
        date=DATE,
        chat=Chat(id=tg_id, type=chat_type),
        from_user=tg_user(tg_id),
        text=text,
    )


@dataclass
class Env:
    db: CountingDatabase
    transport: FakeTransport
    hub: FakeHub
    users: Users
    content: ContentStore
    ui_state: UiStateStore
    codec: CallbackCodec
    router: ScreenRouter

    async def add(self, tg_id: int, role: str = "user", **kw: Any) -> UserCtx:
        uid = await add_user(self.db, tg_id, role)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, **kw)
        self.users.by_tg[tg_id] = ctx
        return ctx

    def restarted(self, **kw: Any) -> Env:
        """A new router with cold caches over the same database (process restart)."""
        ui_state = UiStateStore(self.db)
        router = make_router(
            self.transport, self.users, ui_state, self.content, CallbackCodec(self.db), self.hub, **kw
        )
        return Env(
            self.db, self.transport, self.hub, self.users, self.content, ui_state, router.codec, router
        )  # type: ignore[arg-type]


def make_router(
    transport: FakeTransport,
    users: Users,
    ui_state: UiStateStore,
    content: ContentStore | None,
    codec: CallbackCodec | None,
    hub: FakeHub,
    **kw: Any,
) -> ScreenRouter:
    kw.setdefault("answer_deadline", 0.2)
    kw.setdefault("handler_timeout", 5.0)
    return ScreenRouter(
        transport=transport,
        user_loader=users,
        ui_state=ui_state,
        content=content,
        codec=codec,
        hub=hub,
        **kw,
    )


@asynccontextmanager
async def make_env(db: CountingDatabase, *, media_root: Path | None = None, **kw: Any) -> AsyncIterator[Env]:
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    codec = CallbackCodec(db, key=b"t" * 32)
    router = make_router(transport, users, ui_state, content, codec, hub, media_root=media_root, **kw)
    yield Env(db, transport, hub, users, content, ui_state, codec, router)


Factory = Callable[..., Any]
