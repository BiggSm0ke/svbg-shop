"""Notifier limits/priorities/retries in virtual time (no network)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import Awaitable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from aiogram.types import Message, MessageEntity

from svbg.tg.notifier import (
    Limits,
    Notifier,
    NotifierClosedError,
    NotifierOverloadedError,
    Priority,
)


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

    async def run(self, aws: Iterable[Awaitable[Any]], *, limit: float = 10_000) -> list[Any]:
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
        return [t.result() if not t.exception() else t.exception() for t in tasks]

    async def advance(self, seconds: float) -> None:
        """Move time forward waking every sleeper due in the interval."""
        end = self.t + seconds
        while True:
            await self.settle()
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if not self._sleepers or self._sleepers[0][0] > end:
                break
            target, _, fut = heapq.heappop(self._sleepers)
            self.t = max(self.t, target)
            fut.set_result(None)
        self.t = end
        await self.settle()


class FakeBot:
    """Records (time, chat_id, text); scripted failures per chat."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.sent: list[tuple[float, int | str, str]] = []
        self.attempts: list[tuple[float, int | str]] = []
        self.failures: dict[int | str, list[Exception]] = {}
        self._ids = itertools.count(1)

    def fail(self, chat_id: int, *errors: Exception) -> None:
        self.failures.setdefault(chat_id, []).extend(errors)

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self.attempts.append((self.clock(), method.chat_id))
        queue = self.failures.get(method.chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((self.clock(), method.chat_id, getattr(method, "text", "")))
        return Message.model_validate(
            {
                "message_id": next(self._ids),
                "date": 0,
                "chat": {"id": method.chat_id, "type": "private" if method.chat_id > 0 else "supergroup"},
                "text": getattr(method, "text", None),
            }
        )


_M = SendMessage(chat_id=1, text="x")


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=_M, message=f"Too Many Requests: retry after {seconds}", retry_after=seconds
    )


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def bot(clock: VirtualClock) -> FakeBot:
    return FakeBot(clock)


def make(bot: FakeBot, clock: VirtualClock, **kw: Any) -> Notifier:
    limits = kw.pop("limits", None)
    return Notifier(SimpleNamespace(current=bot), limits=limits, clock=clock, sleep=clock.sleep, **kw)


def assert_window(times: list[float], limit: int, period: float) -> None:
    times = sorted(times)
    for i in range(len(times) - limit):
        assert times[i + limit] - times[i] >= period - 1e-9, (i, times[i], times[i + limit])


async def test_group_limit_20_per_minute(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    results = await clock.run(n.send(-100, f"m{i}") for i in range(45))
    assert all(isinstance(r, Message) for r in results)
    times = [t for t, chat, _ in bot.sent if chat == -100]
    assert len(times) == 45
    assert_window(times, 20, 60.0)
    assert times[19] == 0.0  # the first 20 go immediately
    assert times[20] == pytest.approx(60.0)
    assert [text for *_, text in bot.sent] == [f"m{i}" for i in range(45)]  # order kept


async def test_private_chat_one_per_second(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run(n.send(42, f"m{i}") for i in range(5))
    times = [t for t, *_ in bot.sent]
    assert times == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0])


async def test_global_limit_25_per_second(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run(n.send(1000 + i, "hi") for i in range(100))
    times = [t for t, *_ in bot.sent]
    assert len(times) == 100
    assert_window(times, 25, 1.0)
    assert times.count(0.0) == 25
    assert max(times) == pytest.approx(3.0)


async def test_priorities_across_chats(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock, limits=Limits(global_limit=1, global_window=1.0))
    await clock.run([n.send(1, "warmup")])
    tasks = [
        n.send(2, "low1", priority=Priority.LOW),
        n.send(3, "low2", priority=Priority.LOW),
        n.send(4, "normal", priority=Priority.NORMAL),
        n.send(5, "high", priority=Priority.HIGH),
        n.send(6, "critical", priority=Priority.CRITICAL),
    ]
    await clock.run(tasks)
    order = [text for *_, text in bot.sent[1:]]
    assert order == ["critical", "high", "normal", "low1", "low2"]


async def test_priorities_within_chat(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run([n.send(-5, "warmup")])
    await clock.run(
        [
            n.send(7, "a-low", priority=Priority.LOW),
            n.send(7, "b-low", priority=Priority.LOW),
            n.send(7, "c-crit", priority=Priority.CRITICAL),
        ]
    )
    texts = [text for _, chat, text in bot.sent if chat == 7]
    # the first request may already hold the chat slot; the critical one overtakes the rest
    assert texts.index("c-crit") < texts.index("b-low")


async def test_429_blocks_chat_and_retries(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    bot.fail(7, retry_after(5))
    results = await clock.run([n.send(7, "x"), n.send(8, "other")])
    assert all(isinstance(r, Message) for r in results)
    sent = {chat: t for t, chat, _ in bot.sent}
    assert sent[8] == 0.0  # other chats are not affected
    assert sent[7] >= 5.0
    assert n.stats["retry_429"] == 1
    assert n.stats["sent"] == 2


async def test_429_keeps_position_in_chat(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    bot.fail(-9, retry_after(3))
    await clock.run(n.send(-9, f"m{i}") for i in range(3))
    assert [text for *_, text in bot.sent] == ["m0", "m1", "m2"]
    assert bot.sent[0][0] >= 3.0


async def test_429_gives_up_after_max_retries(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock, limits=Limits(max_retries_429=2))
    bot.fail(7, retry_after(1), retry_after(1), retry_after(1))
    (result,) = await clock.run([n.send(7, "x")])
    assert isinstance(result, TelegramRetryAfter)
    assert n.stats["failed"] == 1


async def test_429_with_huge_retry_after_fails_fast(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock, limits=Limits(max_retry_after=60))
    bot.fail(7, retry_after(3600))
    (result,) = await clock.run([n.send(7, "x")])
    assert isinstance(result, TelegramRetryAfter)
    assert clock.t < 1


async def test_403_calls_on_blocked(bot: FakeBot, clock: VirtualClock) -> None:
    blocked: list[int | str] = []

    async def on_blocked(chat_id: int | str) -> None:
        blocked.append(chat_id)

    n = make(bot, clock, on_blocked=on_blocked)
    bot.fail(11, TelegramForbiddenError(method=_M, message="Forbidden: bot was blocked by the user"))
    (result,) = await clock.run([n.send(11, "x")])
    assert result is None
    assert blocked == [11]
    assert n.stats["blocked"] == 1


async def test_on_blocked_errors_are_isolated(
    bot: FakeBot, clock: VirtualClock, caplog: pytest.LogCaptureFixture
) -> None:
    def on_blocked(chat_id: int | str) -> None:
        raise RuntimeError("db down")

    n = make(bot, clock, on_blocked=on_blocked)
    bot.fail(11, TelegramForbiddenError(method=_M, message="Forbidden: user is deactivated"))
    (result,) = await clock.run([n.send(11, "x")])
    assert result is None
    assert "on_blocked hook failed" in caplog.text


async def test_transient_errors_retry_with_backoff(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    bot.fail(
        3, TelegramServerError(method=_M, message="Bad Gateway"), TelegramServerError(method=_M, message="x")
    )
    (result,) = await clock.run([n.send(3, "x")])
    assert isinstance(result, Message)
    assert [t for t, _ in bot.attempts] == pytest.approx([0.0, 1.0, 3.0])
    assert n.stats["retry_transient"] == 2


async def test_transient_errors_give_up(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock, limits=Limits(max_retries_transient=1))
    bot.fail(3, *(TelegramServerError(method=_M, message="x") for _ in range(3)))
    (result,) = await clock.run([n.send(3, "x")])
    assert isinstance(result, TelegramServerError)


async def test_bad_request_propagates_without_retry(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    bot.fail(3, TelegramBadRequest(method=_M, message="Bad Request: chat not found"))
    result, following = await clock.run([n.send(3, "x"), n.send(3, "next")])
    assert isinstance(result, TelegramBadRequest)
    assert isinstance(following, Message)
    assert len(bot.attempts) == 2  # the failed one was not retried; the chat is not stuck


async def test_entities_and_parse_mode_are_exclusive(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    with pytest.raises(ValueError, match="either"):
        await n.send(1, "x", entities=[MessageEntity(type="bold", offset=0, length=1)], parse_mode="HTML")


async def test_coalesce_key_keeps_only_latest(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run([n.send(-1, "busy")])
    for _ in range(19):
        await clock.run([n.send(-1, "fill")])
    results = await clock.run(
        [
            n.send(-1, "digest 1", priority=Priority.LOW, coalesce_key="new_users"),
            n.send(-1, "digest 2", priority=Priority.LOW, coalesce_key="new_users"),
            n.send(-1, "digest 3", priority=Priority.LOW, coalesce_key="new_users"),
        ]
    )
    assert results[0] is None and results[1] is None
    assert isinstance(results[2], Message)
    digests = [text for *_, text in bot.sent if text.startswith("digest")]
    assert digests == ["digest 3"]
    assert n.stats["superseded"] == 2


async def test_close_fails_waiting_requests(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    t1 = asyncio.ensure_future(n.send(5, "a"))
    t2 = asyncio.ensure_future(n.send(5, "b"))
    await clock.settle()
    await n.close()
    await clock.settle()
    assert isinstance(t1.result(), Message)
    with pytest.raises(NotifierClosedError):
        t2.result()
    with pytest.raises(NotifierClosedError):
        await n.send(5, "c")


async def test_overload_rejects_low_priority(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock, limits=Limits(max_pending=2))
    tasks = [asyncio.ensure_future(n.send(5, f"m{i}")) for i in range(3)]
    await clock.settle()
    with pytest.raises(NotifierOverloadedError):
        await n.send(6, "low", priority=Priority.LOW)
    task_crit = asyncio.ensure_future(n.send(6, "crit", priority=Priority.CRITICAL))
    await clock.run([*tasks, task_crit])
    assert n.stats["rejected"] == 1
    assert n.pending == 0


async def test_cancelled_waiter_does_not_block_chat(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    first = asyncio.ensure_future(n.send(5, "a"))
    second = asyncio.ensure_future(n.send(5, "b"))
    third = asyncio.ensure_future(n.send(5, "c"))
    await clock.settle()
    second.cancel()
    await clock.run([first, third])
    assert [text for *_, text in bot.sent] == ["a", "c"]
    assert n.pending == 0


async def test_idle_chat_gates_are_swept(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run(n.send(i, "x") for i in range(1, 30))
    assert len(n._chats) == 29
    await clock.advance(120)
    await clock.run([n.send(500, "trigger sweep")])
    assert len(n._chats) <= 1


async def test_uses_current_bot_of_holder(clock: VirtualClock) -> None:
    first, second = FakeBot(clock), FakeBot(clock)
    holder = SimpleNamespace(current=first)
    n = Notifier(holder, clock=clock, sleep=clock.sleep)
    await clock.run([n.send(1, "a")])
    holder.current = second
    await clock.run([n.send(2, "b")])
    assert [t for *_, t in first.sent] == ["a"]
    assert [t for *_, t in second.sent] == ["b"]


def test_limits_validation() -> None:
    with pytest.raises(ValueError):
        Limits(group_limit=0)
    with pytest.raises(ValueError):
        Limits(global_window=0)


async def test_snapshot_counters(bot: FakeBot, clock: VirtualClock) -> None:
    n = make(bot, clock)
    await clock.run([n.send(1, "a"), n.send(-2, "b")])
    snap = n.snapshot()
    assert snap["sent"] == 2 and snap["pending"] == 0 and snap["chats"] == 2
