"""Test kit for broadcasts: schema with the broadcast tables, users in any state, a fake bot and fast time."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import CopyMessage, DeleteMessage, EditMessageText, PinChatMessage, TelegramMethod
from aiogram.types import Chat, Message, MessageId

import svbg.broadcasts.tables  # noqa: F401 - registers the tables before create_schema
from svbg.tg.notifier import Limits, Notifier
from tests.dbkit import CountingDatabase

DATE = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_PANEL_IDS = itertools.count(900_000)
MARKETING_DDL = "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_marketing boolean NOT NULL DEFAULT true"


async def prepare(db: CountingDatabase) -> None:
    """The stage-3 column the integration migration adds (``users.notify_marketing``)."""
    await db.raw(MARKETING_DDL)


async def mk_user(
    db: CountingDatabase,
    tg: int | None,
    *,
    role: str = "user",
    lang: str | None = None,
    wallet: int = 0,
    banned: bool = False,
    blocked: bool = False,
    marketing: bool = True,
    sub: dict[str, Any] | None = None,
    paid: bool = False,
) -> int:
    """A user; ``sub`` = {"days": float (paid_until − NOW), "trial": bool, "hold": bool, "state": link_state,
    "plan": code, "none_paid_until": bool}; ``paid`` adds a paid order."""
    rows = await db.raw(
        "insert into users (telegram_id, role, language, wallet_minor, banned_at, bot_blocked_at,"
        " notify_marketing) values ($1, $2, $3, $4, $5, $6, $7) returning id",
        tg,
        role,
        lang,
        wallet,
        NOW if banned else None,
        NOW if blocked else None,
        marketing,
    )
    uid = int(rows[0]["id"])
    if sub is not None:
        paid_until = None if sub.get("none_paid_until") else NOW + timedelta(days=float(sub.get("days", 10)))
        hold = bool(sub.get("hold"))
        await db.raw(
            "insert into subscriptions (user_id, link_state, paid_until, is_trial, hold_kind, hold_since,"
            " plan_snapshot, panel_user_id) values ($1, $2, $3, $4, $5, $6, $7, $8)",
            uid,
            sub.get("state", "linked"),
            paid_until,
            bool(sub.get("trial")),
            "admin" if hold else None,
            NOW if hold else None,
            {"code": sub["plan"]} if sub.get("plan") else {},
            next(_PANEL_IDS),
        )
    if paid:
        await db.raw(
            "insert into orders (user_id, kind, status, currency, total_minor)"
            " values ($1, 'new', 'paid', 'RUB', 100)",
            uid,
        )
    return uid


async def mk_many(db: CountingDatabase, n: int, *, start_tg: int = 100_000) -> None:
    """``n`` plain users with telegram ids ``start_tg..`` in one statement."""
    await db.raw(
        "insert into users (telegram_id) select g from generate_series($1::bigint, $2::bigint) g",
        start_tg,
        start_tg + n - 1,
    )


class FastClock:
    """Virtual time for the notifier and the sender: ``sleep`` advances time and yields once."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        if delay > 0:
            self.t += delay
        await asyncio.sleep(0)


class FakeBot:
    """``await bot(method)`` like aiogram's ``Bot``: records calls, blocked chats → 403, faults → 400."""

    def __init__(self) -> None:
        self.calls: list[TelegramMethod[Any]] = []
        self.blocked: set[int] = set()
        self.bad: set[int] = set()
        self.source_gone = False
        self.latency = 0.0
        self.on_call: Callable[[int, TelegramMethod[Any]], Awaitable[None]] | None = None
        self._ids = itertools.count(5000)

    async def __call__(self, method: TelegramMethod[Any]) -> Any:
        self.calls.append(method)
        if self.on_call is not None:
            await self.on_call(len(self.calls), method)
        if self.latency:
            await asyncio.sleep(self.latency)
        chat_id = getattr(method, "chat_id", 0)
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        if isinstance(method, CopyMessage) and self.source_gone:
            raise TelegramBadRequest(method=method, message="Bad Request: message to copy not found")
        if chat_id in self.bad:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(self._ids))
        if isinstance(method, EditMessageText | PinChatMessage | DeleteMessage):
            return True
        return Message(
            message_id=next(self._ids),
            date=DATE,
            chat=Chat(id=int(chat_id), type="private"),
            text=getattr(method, "text", None),
        )

    def of(self, cls: type) -> list[Any]:
        return [c for c in self.calls if isinstance(c, cls)]

    def deliveries(self) -> list[int]:
        """Chat ids of every copy/send to recipients (not edits, pins or deletes)."""
        skip = (EditMessageText, PinChatMessage, DeleteMessage)
        return [int(c.chat_id) for c in self.calls if not isinstance(c, skip)]


def make_notifier(bot: FakeBot, clock: FastClock | None = None, **limits: Any) -> Notifier:
    kw: dict[str, Any] = {}
    if clock is not None:
        kw = {"clock": clock, "sleep": clock.sleep}
    return Notifier(SimpleNamespace(current=bot), limits=Limits(**limits), **kw)


def job(bid: int) -> Any:
    """What handlers read from a claimed job (only ``payload``)."""
    return SimpleNamespace(payload={"broadcast_id": bid})
