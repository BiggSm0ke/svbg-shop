"""Outgoing Telegram traffic with Telegram's rate limits, priorities and retries.

Every proactive send (notifications, admin chat cards, broadcasts, error reports) goes through
:class:`Notifier`. Limits (defaults follow the Bot API FAQ):

* global — at most ``global_limit`` requests per ``global_window`` (25/s);
* private chat (``chat_id > 0``) — ``private_limit`` per ``private_window`` (1/s);
* group / channel (``chat_id < 0``) — ``group_limit`` per ``group_window`` (20/min, exact sliding window,
  so no 60-second window ever contains more than 20 messages).

Requests to one chat are sent one at a time and in order of ``(priority, arrival)``; the global gate
hands out slots by the same order, so ``CRITICAL`` errors and payments overtake ``LOW`` digests.

Failures: ``429`` blocks the chat for ``retry_after`` and retries (keeping the queue position); network
errors and ``5xx`` retry with exponential backoff; ``403`` (blocked / kicked) calls ``on_blocked(chat_id)``
and returns ``None``; other API errors (``400`` …) propagate to the caller.

The clock and ``sleep`` are injectable so tests can run minutes of rate-limited traffic in virtual time.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import heapq
import inspect
import itertools
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import LinkPreviewOptions, Message, MessageEntity
from aiohttp import ClientError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from svbg.core.log import mask

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import ForceReply, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

__all__ = [
    "TRANSPORT_ERRORS",
    "Limits",
    "Notifier",
    "NotifierClosedError",
    "NotifierError",
    "NotifierOverloadedError",
    "Priority",
]

log = logging.getLogger("svbg.tg.notifier")

T = TypeVar("T")

ChatId = int | str
BlockedHook = Callable[[ChatId], Awaitable[None] | None]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_SWEEP_INTERVAL: Final = 60.0

# Errors raised below aiogram (aiohttp, sockets, SOCKS proxy) that mean "the network is unwell".
TRANSPORT_ERRORS: Final[tuple[type[Exception], ...]] = (
    ClientError,
    OSError,
    ProxyError,
    ProxyConnectionError,
    ProxyTimeoutError,
)
_TRANSIENT: Final = (TelegramServerError, TelegramNetworkError, *TRANSPORT_ERRORS)


class Priority(enum.IntEnum):
    """Lower value is sent first."""

    CRITICAL = 0  # errors, security, owner alerts
    HIGH = 1  # payments, anti-abuse
    NORMAL = 2  # user notifications
    LOW = 3  # new users, trials, digests, broadcasts


class NotifierError(Exception):
    """Base error of the notifier."""


class NotifierClosedError(NotifierError):
    """The notifier was closed while the request was waiting."""


class NotifierOverloadedError(NotifierError):
    """Too many requests are waiting; low-priority traffic is rejected."""


class _BotSource(Protocol):
    @property
    def current(self) -> Bot: ...


@dataclass(frozen=True, slots=True)
class Limits:
    """Rate limits and retry policy."""

    global_limit: int = 25
    global_window: float = 1.0
    private_limit: int = 1
    private_window: float = 1.0
    group_limit: int = 20
    group_window: float = 60.0
    max_retries_429: int = 5
    max_retry_after: float = 600.0
    max_retries_transient: int = 3
    transient_backoff: float = 1.0
    max_pending: int = 10_000

    def __post_init__(self) -> None:
        for name in ("global_limit", "private_limit", "group_limit", "max_pending"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        for name in ("global_window", "private_window", "group_window"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")


class _Window:
    """Exact sliding-window limiter: at most ``limit`` events in any ``period`` seconds."""

    __slots__ = ("blocked_until", "limit", "period", "stamps")

    def __init__(self, limit: int, period: float) -> None:
        self.limit = limit
        self.period = period
        self.stamps: deque[float] = deque()
        self.blocked_until = 0.0

    def delay(self, now: float) -> float:
        stamps = self.stamps
        while stamps and now - stamps[0] >= self.period:
            stamps.popleft()
        wait = self.blocked_until - now
        if len(stamps) >= self.limit:
            wait = max(wait, stamps[0] + self.period - now)
        return max(wait, 0.0)

    def take(self, now: float) -> None:
        self.stamps.append(now)

    def block(self, until: float) -> None:
        self.blocked_until = max(self.blocked_until, until)

    def idle(self, now: float) -> bool:
        return self.delay(now) == 0.0 and not self.stamps


class _Gate:
    """Grants slots of a :class:`_Window` in ``(priority, seq)`` order.

    With ``serial=True`` the next slot is granted only after the previous holder called
    :meth:`release` — requests to one chat never overlap, so Telegram sees them in order.
    """

    def __init__(self, window: _Window, clock: Clock, sleep: Sleep, *, serial: bool) -> None:
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._serial = serial
        self._heap: list[tuple[int, int, int, asyncio.Future[None]]] = []
        self._tiebreak = itertools.count()
        self._pump: asyncio.Task[None] | None = None
        self._busy = False
        self._released = asyncio.Event()
        self._closed = False

    @property
    def waiting(self) -> int:
        return sum(1 for *_, fut in self._heap if not fut.done())

    def idle(self, now: float) -> bool:
        return not self._heap and not self._busy and self.window.idle(now)

    async def acquire(self, priority: int, seq: int) -> None:
        if self._closed:
            raise NotifierClosedError
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, seq, next(self._tiebreak), fut))
        if self._pump is None or self._pump.done():
            self._pump = asyncio.get_running_loop().create_task(self._run())
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                self.release()  # granted right before cancellation — give the slot back
            raise

    def release(self) -> None:
        if self._serial:
            self._busy = False
            self._released.set()

    async def _run(self) -> None:
        heap = self._heap
        while heap:
            fut = heap[0][3]
            if fut.done():
                heapq.heappop(heap)
                continue
            if self._busy:
                self._released.clear()
                await self._released.wait()
                continue
            delay = self.window.delay(self._clock())
            if delay > 0:
                await self._sleep(delay)
                continue
            heapq.heappop(heap)
            self.window.take(self._clock())
            if self._serial:
                self._busy = True
            fut.set_result(None)

    async def close(self) -> None:
        self._closed = True
        for *_, fut in self._heap:
            if not fut.done():
                fut.set_exception(NotifierClosedError())
        self._heap.clear()
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump


@dataclass(slots=True)
class _Pending:
    seq: int
    superseded: bool = False


class Notifier:
    """Rate-limited, prioritised sender. One instance per process; safe for concurrent use."""

    def __init__(
        self,
        bot_holder: _BotSource,
        *,
        limits: Limits | None = None,
        on_blocked: BlockedHook | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._holder = bot_holder
        self.limits = limits or Limits()
        self.on_blocked = on_blocked
        self._clock = clock
        self._sleep = sleep
        self._seq = itertools.count()
        self._global = _Gate(
            _Window(self.limits.global_limit, self.limits.global_window), clock, sleep, serial=False
        )
        self._chats: dict[ChatId, _Gate] = {}
        self._coalesce: dict[tuple[ChatId, str], _Pending] = {}
        self._pending = 0
        self._last_sweep = clock()
        self._closed = False
        self.stats: dict[str, int] = {
            "sent": 0,
            "retry_429": 0,
            "retry_transient": 0,
            "blocked": 0,
            "failed": 0,
            "superseded": 0,
            "rejected": 0,
        }

    # ------------------------------------------------------------------ public API

    @property
    def pending(self) -> int:
        return self._pending

    async def send(
        self,
        chat_id: ChatId,
        text: str,
        *,
        entities: list[MessageEntity] | None = None,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup
        | ReplyKeyboardMarkup
        | ReplyKeyboardRemove
        | ForceReply
        | None = None,
        thread_id: int | None = None,
        priority: Priority = Priority.NORMAL,
        disable_notification: bool | None = None,
        link_preview: bool = False,
        coalesce_key: str | None = None,
    ) -> Message | None:
        """Send a text message. Returns ``None`` if the chat blocked the bot or the message was superseded.

        ``entities`` and ``parse_mode`` are mutually exclusive; with neither, the text is sent as plain
        text (the bot's default parse mode is not applied). ``coalesce_key``: a newer message with the
        same key to the same chat replaces this one while it is still waiting (digests).
        """
        if entities is not None and parse_mode is not None:
            raise ValueError("pass either entities or parse_mode, not both")
        method = SendMessage(
            chat_id=chat_id,
            text=text,
            entities=entities,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            message_thread_id=thread_id,
            disable_notification=disable_notification,
            link_preview_options=None if link_preview else LinkPreviewOptions(is_disabled=True),
        )
        return await self.call(method, chat_id=chat_id, priority=priority, coalesce_key=coalesce_key)

    async def call(
        self,
        method: TelegramMethod[T],
        *,
        chat_id: ChatId,
        priority: Priority = Priority.NORMAL,
        coalesce_key: str | None = None,
    ) -> T | None:
        """Run any chat-bound Bot API method (sendPhoto, copyMessage, editMessageText…) under the limits."""
        if self._closed:
            raise NotifierClosedError
        if self._pending >= self.limits.max_pending and priority >= Priority.LOW:
            self.stats["rejected"] += 1
            raise NotifierOverloadedError("notifier queue is full")
        pending = self._register(chat_id, coalesce_key)
        self._pending += 1
        try:
            return await self._deliver(method, chat_id, priority, pending)
        finally:
            self._pending -= 1
            if coalesce_key is not None and self._coalesce.get((chat_id, coalesce_key)) is pending:
                del self._coalesce[(chat_id, coalesce_key)]
            self._maybe_sweep()

    async def close(self) -> None:
        """Fail all waiting requests with :class:`NotifierClosedError` and stop background tasks."""
        self._closed = True
        gates = [self._global, *self._chats.values()]
        self._chats.clear()
        for gate in gates:
            await gate.close()

    # ------------------------------------------------------------------ internals

    def _register(self, chat_id: ChatId, coalesce_key: str | None) -> _Pending:
        if coalesce_key is None:
            return _Pending(next(self._seq))
        key = (chat_id, coalesce_key)
        previous = self._coalesce.get(key)
        if previous is not None and not previous.superseded:
            previous.superseded = True
            pending = _Pending(previous.seq)  # inherit the queue position
        else:
            pending = _Pending(next(self._seq))
        self._coalesce[key] = pending
        return pending

    def _chat_gate(self, chat_id: ChatId) -> _Gate:
        gate = self._chats.get(chat_id)
        if gate is None:
            lim = self.limits
            is_private = isinstance(chat_id, int) and chat_id > 0
            window = (
                _Window(lim.private_limit, lim.private_window)
                if is_private
                else _Window(lim.group_limit, lim.group_window)
            )
            gate = _Gate(window, self._clock, self._sleep, serial=True)
            self._chats[chat_id] = gate
        return gate

    async def _deliver(
        self, method: TelegramMethod[T], chat_id: ChatId, priority: Priority, pending: _Pending
    ) -> T | None:
        lim = self.limits
        retries_429 = retries_transient = 0
        while True:
            backoff = 0.0
            gate = self._chat_gate(chat_id)
            await gate.acquire(int(priority), pending.seq)
            try:
                if pending.superseded:
                    self.stats["superseded"] += 1
                    return None
                await self._global.acquire(int(priority), pending.seq)
                bot = self._holder.current
                result = await bot(method)
            except TelegramRetryAfter as exc:
                retries_429 += 1
                self.stats["retry_429"] += 1
                if retries_429 > lim.max_retries_429 or exc.retry_after > lim.max_retry_after:
                    self.stats["failed"] += 1
                    raise
                log.warning("429 for chat %s, retry in %ss", chat_id, exc.retry_after)
                gate.window.block(self._clock() + exc.retry_after)
                continue
            except TelegramForbiddenError as exc:
                self.stats["blocked"] += 1
                log.info("chat %s is unreachable: %s", chat_id, mask(exc.message))
                await self._notify_blocked(chat_id)
                return None
            except _TRANSIENT as exc:
                retries_transient += 1
                self.stats["retry_transient"] += 1
                if retries_transient > lim.max_retries_transient:
                    self.stats["failed"] += 1
                    raise
                backoff = lim.transient_backoff * 2 ** (retries_transient - 1)
                log.warning(
                    "transient error for chat %s (%s), retry %d in %.1fs",
                    chat_id,
                    mask(str(exc)),
                    retries_transient,
                    backoff,
                )
            else:
                self.stats["sent"] += 1
                return result
            finally:
                gate.release()
            if backoff:
                await self._sleep(backoff)

    async def _notify_blocked(self, chat_id: ChatId) -> None:
        hook = self.on_blocked
        if hook is None:
            return
        try:
            res = hook(chat_id)
            if inspect.isawaitable(res):
                await res
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_blocked hook failed for chat %s", chat_id)

    def _maybe_sweep(self) -> None:
        now = self._clock()
        if now - self._last_sweep < _SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for chat_id in [c for c, g in self._chats.items() if g.idle(now)]:
            del self._chats[chat_id]

    def snapshot(self) -> dict[str, Any]:
        """Counters for the «Состояние» screen / logs."""
        return {"chats": len(self._chats), "pending": self._pending, **self.stats}
