"""A burst of 200 notifications in virtual time: nothing lost, priorities kept, digests, no 429.

The bot is an in-process fake that enforces Telegram's group limit exactly (20 messages per 60 s per group,
otherwise ``429 retry_after``), so the test proves that the service never even triggers flood control.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CreateForumTopic, EditMessageText, GetForumTopicIconStickers, SendMessage
from aiogram.types import ForumTopic, Message

import svbg.services.tables  # noqa: F401 - registers admin_* tables before create_schema
from svbg.core.component import Health
from svbg.db.engine import Database
from svbg.db.schema import create_schema
from svbg.services.admin_chat import (
    K_ERRORS,
    K_NEW_USERS,
    K_PANEL,
    K_PAYMENTS,
    K_SUBSCRIPTIONS,
    K_TRIALS,
    AdminChatService,
    PostResult,
)
from svbg.tg.notifier import Notifier
from svbg.tg.runner import BotUnavailableError

GROUP = -1009
OWNER = 1


class VirtualClock:
    """Virtual time: ``sleep`` parks the caller; :meth:`run` jumps to the next wake-up when idle."""

    def __init__(self) -> None:
        self.t = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._n = itertools.count()

    def __call__(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.t + delay, next(self._n), fut))
        await fut

    async def settle(self) -> None:
        loop = asyncio.get_running_loop()
        quiet = 0
        while quiet < 3:
            await asyncio.sleep(0)
            quiet = quiet + 1 if not loop._ready else 0  # type: ignore[attr-defined]

    async def run(self, aws: Iterable[Awaitable[Any]], *, limit: float = 7200) -> list[Any]:
        tasks = [asyncio.ensure_future(a) for a in aws]
        while True:
            await self.settle()
            if all(t.done() for t in tasks):
                break
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if not self._sleepers:
                raise AssertionError("deadlock: tasks pending but nobody sleeps")
            target, _, fut = heapq.heappop(self._sleepers)
            self.t = max(self.t, target)
            assert self.t <= limit, "virtual time limit exceeded"
            fut.set_result(None)
        return [t.result() for t in tasks]


@dataclass
class Sent:
    t: float
    chat_id: int
    thread_id: int | None
    text: str


class FloodBot:
    """In-process Bot: records sends and answers 429 like Telegram when a group gets > 20 msgs/min."""

    def __init__(self, clock: VirtualClock, *, group_limit: int = 20, window: float = 60.0) -> None:
        self.clock = clock
        self.id = 4242
        self.sent: list[Sent] = []
        self.flood_errors = 0
        self._limit = group_limit
        self._window = window
        self._stamps: dict[int, list[float]] = {}
        self._ids = itertools.count(1)
        self._threads = itertools.count(100)
        # (method, chat_id) → an exception to raise instead of sending (outages, a broken chat)
        self.fault: Callable[[Any, int], BaseException | None] | None = None
        self.unavailable_until = -1.0  # the holder has no bot until then (BotUnavailableError)

    def _flood_check(self, method: Any, chat_id: int) -> None:
        exc = self.fault(method, chat_id) if self.fault is not None else None
        if exc is not None:
            raise exc
        if chat_id > 0:
            return
        now = self.clock()
        stamps = [s for s in self._stamps.get(chat_id, []) if now - s < self._window]
        if len(stamps) >= self._limit:
            self.flood_errors += 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=30)
        stamps.append(now)
        self._stamps[chat_id] = stamps

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        if isinstance(method, GetForumTopicIconStickers):
            return []
        if isinstance(method, CreateForumTopic):
            return ForumTopic(message_thread_id=next(self._threads), name=method.name, icon_color=7322096)
        if isinstance(method, SendMessage):
            self._flood_check(method, method.chat_id)
            self.sent.append(Sent(self.clock(), method.chat_id, method.message_thread_id, method.text))
            return Message.model_validate(
                {
                    "message_id": next(self._ids),
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "supergroup" if method.chat_id < 0 else "private"},
                    "text": method.text,
                }
            )
        if isinstance(method, EditMessageText):
            self._flood_check(method, method.chat_id)
            return True
        raise AssertionError(f"unexpected method {type(method).__name__}")


class Holder:
    def __init__(self, bot: FloodBot) -> None:
        self.bot = bot

    @property
    def current(self) -> Any:
        if self.bot.clock() < self.bot.unavailable_until:  # a bot token hot swap / restart
            raise BotUnavailableError("bot is restarting")
        return self.bot

    def get(self) -> Any:
        return self.bot


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[Database]:
    await create_schema(pg_dsn)
    database = Database(pg_dsn)
    await database.start()
    try:
        yield database
    finally:
        await database.close()


Env = tuple[AdminChatService, FloodBot, VirtualClock]


async def _service(
    db: Database, clock: VirtualClock, bot: FloodBot, **kw: Any
) -> tuple[AdminChatService, Notifier]:
    holder = Holder(bot)
    notifier = Notifier(holder, clock=clock, sleep=clock.sleep)

    async def owners() -> frozenset[int]:
        return frozenset({OWNER})

    svc = AdminChatService(db, notifier, holder, owners=owners, clock=clock, sleep=clock.sleep, **kw)
    svc.set_chat(GROUP)
    await svc.start()
    await svc.ensure_topics()  # 9 topics before the burst (no database work while time is virtual)
    return svc, notifier


@pytest.fixture
async def env(db: Database) -> AsyncIterator[Env]:
    clock = VirtualClock()
    bot = FloodBot(clock)
    svc, notifier = await _service(db, clock, bot)
    try:
        yield svc, bot, clock
    finally:
        await svc.stop(grace=0.1)
        await notifier.close()


MIX = {K_ERRORS: 20, K_PAYMENTS: 30, K_SUBSCRIPTIONS: 50, K_NEW_USERS: 60, K_TRIALS: 40}
LOW = {K_NEW_USERS, K_TRIALS}


def burst() -> list[tuple[str, str]]:
    items = [(kind, f"{kind} #{i}") for kind, n in MIX.items() for i in range(n)]
    random.Random(7).shuffle(items)
    assert len(items) == 200
    return items


def assert_no_flood(bot: FloodBot) -> None:
    assert bot.flood_errors == 0
    group = sorted(s.t for s in bot.sent if s.chat_id == GROUP)
    for i, t in enumerate(group):  # any 60-second window holds at most 20 messages
        assert sum(1 for u in group[i:] if u - t < 60.0) <= 20


def assert_nothing_lost(svc: AdminChatService, bot: FloodBot, posted: list[tuple[str, str]]) -> None:
    texts = [s.text for s in bot.sent if s.chat_id == GROUP]
    for kind, text in posted:
        if kind in LOW:
            continue
        assert sum(1 for t in texts if t == text) == 1, text  # every important message exactly once
    accounted = 0
    for kind in LOW:
        thread = svc.state(kind).thread_id
        for s in bot.sent:
            if s.thread_id != thread:
                continue
            if s.text.startswith("<b>"):
                head = s.text.split("\n", 1)[0]
                accounted += int(head.split("+", 1)[1].split(" ", 1)[0])
            else:
                accounted += 1
    assert accounted == sum(1 for kind, _ in posted if kind in LOW)


async def test_burst_of_200_at_once(env: tuple[AdminChatService, FloodBot, VirtualClock]) -> None:
    svc, bot, clock = env
    posted = burst()
    waiters = [svc.post(kind, text, wait=True) for kind, text in posted]
    results: list[PostResult | None] = await clock.run(waiters)

    assert all(r is not None and r.delivered for r in results)
    assert_no_flood(bot)
    assert_nothing_lost(svc, bot, posted)

    order = ["digest" if s.text.startswith("<b>") else s.text.split(" #", 1)[0] for s in bot.sent]
    # errors first, then payments, then subscriptions; low priority glued into one digest per topic, last
    assert order == [K_ERRORS] * 20 + [K_PAYMENTS] * 30 + [K_SUBSCRIPTIONS] * 50 + ["digest"] * 2
    digests = [s.text for s in bot.sent if s.text.startswith("<b>")]
    assert any(d.startswith("<b>👤 +60 новых пользователей за ") for d in digests)
    assert any(d.startswith("<b>🎁 +40 триалов за ") for d in digests)
    assert "…и ещё 35" in next(d for d in digests if "👤" in d)  # 25 lines listed, the rest counted
    low = [r for r, (kind, _) in zip(results, posted, strict=True) if kind in LOW]
    assert all(r is not None and r.digest in (40, 60) for r in low)
    assert len([s for s in bot.sent if s.chat_id == GROUP]) == 102


async def test_burst_spread_over_a_minute(env: tuple[AdminChatService, FloodBot, VirtualClock]) -> None:
    svc, bot, clock = env
    posted = burst()
    waiters: list[asyncio.Future[PostResult | None]] = []

    async def producer() -> None:
        for kind, text in posted:  # 200 notifications over 60 seconds
            waiters.append(asyncio.ensure_future(svc.post(kind, text, wait=True)))
            await clock.sleep(0.3)

    await clock.run([producer()])
    results = await clock.run(waiters)
    assert all(r is not None and r.delivered for r in results)
    assert_no_flood(bot)
    assert_nothing_lost(svc, bot, posted)

    sent_at = {s.text: s.t for s in bot.sent}
    posted_at = {text: i * 0.3 for i, (_, text) in enumerate(posted)}
    rank = {K_ERRORS: 0, K_PAYMENTS: 1, K_SUBSCRIPTIONS: 2}
    urgent = [(rank[kind], text) for kind, text in posted if kind in rank]
    for s in bot.sent:  # nothing more urgent was waiting when a message went out
        prio = 3 if s.text.startswith("<b>") else rank.get(s.text.split(" #", 1)[0], 3)
        overtaken = [
            text for r, text in urgent if r < prio and posted_at[text] < s.t - 1e-6 and sent_at[text] > s.t
        ]
        assert not overtaken, (s.text, overtaken[:3])
    worst_error = max(sent_at[text] - posted_at[text] for r, text in urgent if r == 0)
    assert worst_error <= 61  # 20 errors in a minute fit the group budget: none waits longer than a window
    assert sum(1 for s in bot.sent if s.text.startswith("<b>")) >= 2  # low priority was digested


async def test_error_sink_style_waiter_cancellation_drops_the_item(
    env: tuple[AdminChatService, FloodBot, VirtualClock],
) -> None:
    svc, bot, clock = env
    fillers = [svc.post(K_SUBSCRIPTIONS, f"fill #{i}", wait=True) for i in range(21)]
    await clock.run(fillers[:20])  # the group window is now full for a minute
    late = asyncio.ensure_future(svc.post(K_ERRORS, "cancelled report", wait=True))
    await asyncio.sleep(0)
    late.cancel()  # the hub gave up waiting (sink timeout): the report must not be sent later
    await clock.run(fillers[20:])
    assert "cancelled report" not in [s.text for s in bot.sent]


# ------------------------------------------------------------------ transient outages (nothing is lost)


def outage(bot: FloodBot, until: float, *, chats: Callable[[int], bool] = lambda _c: True) -> None:
    def fault(method: Any, chat_id: int) -> BaseException | None:
        if chats(chat_id) and bot.clock() < until:
            return TelegramNetworkError(method=method, message="connection reset")
        return None

    bot.fault = fault


async def test_telegram_outage_keeps_everything_queued(env: Env) -> None:
    svc, bot, clock = env
    outage(bot, until=300.0)  # five minutes without Telegram
    posted = [(K_ERRORS, "error #1"), (K_PAYMENTS, "payment #1"), (K_PANEL, "node down #1")]
    posted += [(K_SUBSCRIPTIONS, f"sub #{i}") for i in range(5)]
    seen: dict[str, Any] = {}

    async def observer() -> None:
        await clock.sleep(200.0)
        seen["health"] = await svc.health()
        seen["pending"] = svc.pending

    results = await clock.run([*(svc.post(k, t, wait=True) for k, t in posted), observer()])

    assert all(r is not None and r.chat_id == GROUP and r.message_id for r in results[:-1])
    texts = [s.text for s in bot.sent if s.chat_id == GROUP]
    assert sorted(texts) == sorted(t for _, t in posted)  # each exactly once, in the group
    assert texts[:3] == ["error #1", "payment #1", "node down #1"]  # priorities kept after the outage
    assert not [s for s in bot.sent if s.chat_id == OWNER]  # no DM fallback for a mere outage
    assert min(s.t for s in bot.sent) >= 300.0
    assert svc.stats["undelivered"] == 0 and svc.stats["requeued"] > 0
    assert not svc.down and not svc.waiting
    health = seen["health"]
    assert health.status is Health.DEGRADED and "Telegram временно недоступен" in health.summary
    assert seen["pending"] == len(posted)
    assert (await svc.health()).status is Health.OK


async def test_bot_restart_holds_the_queue(env: Env) -> None:
    svc, bot, clock = env
    bot.unavailable_until = 40.0  # token hot swap: BotUnavailableError for 40 s
    svc.set_chat(None)  # owner DMs only: the same rule for the private fallback
    results = await clock.run([svc.post(K_PAYMENTS, f"payment #{i}", wait=True) for i in range(3)])
    assert all(r is not None and r.dm and OWNER in r.dm for r in results)
    dms = [s for s in bot.sent if s.chat_id == OWNER]
    assert len(dms) == 3 and min(s.t for s in dms) >= 40.0
    assert svc.stats["undelivered"] == 0


async def test_dm_fallback_failing_transiently_is_retried(env: Env) -> None:
    svc, bot, clock = env

    def fault(method: Any, chat_id: int) -> BaseException | None:
        if chat_id == GROUP:  # the group is broken for good
            return TelegramBadRequest(method=method, message="Bad Request: chat not found")
        if bot.clock() < 120.0:  # and the private chats are unreachable for two minutes
            return TelegramNetworkError(method=method, message="connection reset")
        return None

    bot.fault = fault
    (result,) = await clock.run([svc.post(K_ERRORS, "error #1", wait=True)])
    assert result is not None and OWNER in result.dm
    dms = [s for s in bot.sent if s.chat_id == OWNER]
    assert len(dms) == 1 and dms[0].t >= 120.0
    assert "Админ-чат недоступен" in dms[0].text and "error #1" in dms[0].text
    group_tries = svc.stats["failures"] - svc.stats["requeued"]
    assert group_tries == 5  # the group was given up after max_attempts, then only the DM was retried
    assert svc.stats["undelivered"] == 0


async def test_item_failing_for_too_long_is_dropped_and_the_pump_goes_on(db: Database) -> None:
    clock = VirtualClock()
    bot = FloodBot(clock)
    svc, notifier = await _service(db, clock, bot, give_up_after=120.0)
    try:
        outage(bot, until=10_000.0, chats=lambda c: c == GROUP)  # the group endpoint never comes back
        first = asyncio.ensure_future(svc.post(K_ERRORS, "stuck", wait=True))
        (result,) = await clock.run([first], limit=1000)
        assert result is None and svc.stats["undelivered"] == 1
        bot.fault = None
        (later,) = await clock.run([svc.post(K_PAYMENTS, "after", wait=True)], limit=2000)
        assert later is not None and later.chat_id == GROUP
    finally:
        await svc.stop(grace=0.1)
        await notifier.close()


# ------------------------------------------------------------------ headroom for urgent items


async def test_low_priority_burst_leaves_room_for_an_error(env: Env) -> None:
    svc, bot, clock = env
    start = 61.0  # the topic creations of the fixture have left the window
    crit: dict[str, float] = {}

    async def producer() -> None:
        await clock.sleep(start)
        for i in range(20):  # new users trickle in while the pump is idle: each would go out alone
            await svc.post(K_NEW_USERS, f"user #{i}")  # fire and forget, like the real producers
            await clock.sleep(1.0)
        crit["posted"] = clock()
        await svc.post(K_ERRORS, "critical report", wait=True)
        crit["sent"] = clock()

    await clock.run([producer()])
    await clock.run([svc.post(K_TRIALS, "flush", wait=True)])  # queued after the folded users

    assert crit["sent"] - crit["posted"] < 1.0  # not a minute behind the new users
    low_alone = [s for s in bot.sent if s.text.startswith("user #") and s.t < start + 60]
    assert len(low_alone) <= 14  # LOW may fill at most 70 % of the 20/min window
    assert_no_flood(bot)
    digests = [s.text for s in bot.sent if s.text.startswith("<b>👤")]
    shown = len(low_alone) + sum(int(d.split("+", 1)[1].split(" ", 1)[0]) for d in digests)
    assert shown == 20  # nothing lost: the rest was folded into a digest
