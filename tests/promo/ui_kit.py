"""A screen-router harness for the promo / pages / ads screens: the real router and forms over real
PostgreSQL, a recording Telegram transport, users by Telegram id with roles and permissions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from svbg.content.store import ContentStore
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.dbkit import CountingDatabase
from tests.promo.kit import add_user
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message

OWNER = 1001
ADMIN = 2002
ADMIN_NOPERM = 3003
SUPPORT = 4004
USER = 5005


@dataclass
class UiEnv:
    db: CountingDatabase
    transport: FakeTransport
    hub: FakeHub
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    denied: list[tuple[int, str]] = field(default_factory=list)

    async def add(
        self, tg_id: int, role: str = "user", perms: frozenset[str] = frozenset(), **kw: int
    ) -> UserCtx:
        uid = await add_user(self.db, tg_id, role=role, **kw)
        if perms:  # services re-read rights from the database
            sql = "update users set perms = $1::jsonb where id = $2"
            await self.db.raw(sql, json.dumps(sorted(perms)), uid)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.users.by_tg[tg_id] = ctx
        return ctx

    async def staff(self, perm: str) -> None:
        await self.add(OWNER, "owner")
        await self.add(ADMIN, "admin", frozenset({perm}))
        await self.add(ADMIN_NOPERM, "admin", frozenset({"plans"}))
        await self.add(SUPPORT, "support")
        await self.add(USER, "user")

    def uid(self, tg_id: int) -> int:
        return self.users.by_tg[tg_id].user_id

    async def main_id(self, tg_id: int) -> int:
        state = await self.ui_state.get(self.uid(tg_id))
        return state.main_msg_id or 10

    async def click(self, tg_id: int, data: str) -> None:
        await self.router.dispatch_callback(callback(tg_id, data, message_id=await self.main_id(tg_id)))

    async def press(self, tg_id: int, label: str) -> None:
        await self.click(tg_id, self.button(label))

    async def type(self, tg_id: int, text: str) -> bool:
        return await self.router.dispatch_message(text_message(tg_id, text, message_id=700))

    async def send(self, message: Message) -> bool:
        return await self.router.dispatch_message(message)

    def rendered(self) -> list[SendMessage | EditMessageText]:
        return [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText)]

    @property
    def last(self) -> SendMessage | EditMessageText:
        return self.rendered()[-1]

    @property
    def text(self) -> str:
        return self.last.text

    def buttons(self) -> list[InlineKeyboardButton]:
        markup = self.last.reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return [b for row in markup.inline_keyboard for b in row]

    def labels(self) -> list[str]:
        return [b.text for b in self.buttons()]

    def button(self, label: str) -> str:
        for b in self.buttons():
            if label in b.text:
                assert b.callback_data is not None
                return b.callback_data
        raise AssertionError(f"no button {label!r} in {self.labels()}")

    @property
    def toasts(self) -> list[str | None]:
        return self.transport.toasts


async def build_ui(db: CountingDatabase, *, home_screen: bool = True) -> UiEnv:
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    env = UiEnv(db, transport, hub, users, None, ui_state)  # type: ignore[arg-type]

    async def on_denied(user: UserCtx, place: str) -> None:
        env.denied.append((user.user_id, place))

    env.router = make_router(
        transport, users, ui_state, content, CallbackCodec(db, key=b"q" * 32), hub, on_denied=on_denied
    )
    return env
