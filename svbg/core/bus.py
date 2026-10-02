"""In-process async event bus.

Fire-and-observe notifications between core services (``payment.succeeded``, ``attention.raised``,
``settings.changed`` …). Delivery is best effort and in-memory: anything that must survive a restart
is enqueued as a job by a subscriber, not delivered here.

Guarantees:

* handlers of one event run concurrently; one slow or failing handler never affects the others;
* handler exceptions (and per-handler timeouts) go to the ``on_error`` hook and are never raised to
  the publisher; cancelling the publisher cancels its handlers;
* the subscriber list is copy-on-write, so (un)subscribing from inside a handler is safe;
* payloads are read-only mappings, so a handler cannot change what the next handler sees at top level.

Subscription patterns: an exact name (``payment.succeeded``), ``"*"`` (everything) or a dotted prefix
wildcard (``payment.*`` matches ``payment.succeeded`` and ``payment.refund.done``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from svbg.core.clock import now

log = logging.getLogger("svbg.core.bus")

__all__ = ["ALL", "Event", "EventBus", "Handler", "OnError"]

ALL = "*"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
_PATTERN_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\.\*$")
_NAME_MAX = 128
DEFAULT_HANDLER_TIMEOUT = 30.0

Handler = Callable[["Event"], Awaitable[None]]
OnError = Callable[["Event", Handler, BaseException], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable notification. ``payload`` must hold only plain data and never secrets."""

    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=now)

    def __post_init__(self) -> None:
        if len(self.name) > _NAME_MAX or not _NAME_RE.match(self.name):
            raise ValueError(f"invalid event name: {self.name!r}")
        if self.ts.tzinfo is None:
            raise ValueError("event ts must be timezone-aware")
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


def _handler_name(handler: Handler) -> str:
    return getattr(handler, "__qualname__", None) or type(handler).__qualname__


def _check_pattern(pattern: str) -> None:
    if pattern == ALL:
        return
    if len(pattern) > _NAME_MAX or not (_NAME_RE.match(pattern) or _PATTERN_RE.match(pattern)):
        raise ValueError(f"invalid subscription pattern: {pattern!r}")


class EventBus:
    """See module docstring. Use from one event loop."""

    def __init__(
        self,
        *,
        on_error: OnError | None = None,
        handler_timeout: float | None = DEFAULT_HANDLER_TIMEOUT,
    ) -> None:
        if handler_timeout is not None and handler_timeout <= 0:
            raise ValueError("handler_timeout must be positive or None")
        self._on_error = on_error
        self._timeout = handler_timeout
        self._exact: dict[str, tuple[Handler, ...]] = {}
        self._prefix: dict[str, tuple[Handler, ...]] = {}  # "payment." -> handlers
        self._all: tuple[Handler, ...] = ()
        self._background: set[asyncio.Task[int]] = set()
        self._closed = False

    # -- subscriptions -------------------------------------------------------------------------

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """Subscribe ``handler`` (an ``async def``) to ``pattern``. Returns an unsubscribe callable.

        Subscribing the same handler to the same pattern twice is a no-op.
        """
        _check_pattern(pattern)
        if not callable(handler):
            raise TypeError("handler must be callable")
        if inspect.isfunction(handler) and not inspect.iscoroutinefunction(handler):
            raise TypeError(f"handler {_handler_name(handler)} must be an async function")

        if pattern == ALL:
            if handler not in self._all:
                self._all = (*self._all, handler)
        else:
            table, key = self._slot(pattern)
            current = table.get(key, ())
            if handler not in current:
                table[key] = (*current, handler)

        def unsubscribe() -> None:
            self.unsubscribe(pattern, handler)

        return unsubscribe

    def unsubscribe(self, pattern: str, handler: Handler) -> bool:
        if pattern == ALL:
            if handler in self._all:
                self._all = tuple(h for h in self._all if h != handler)
                return True
            return False
        table, key = self._slot(pattern)
        current = table.get(key, ())
        if handler not in current:
            return False
        rest = tuple(h for h in current if h != handler)
        if rest:
            table[key] = rest
        else:
            del table[key]
        return True

    def _slot(self, pattern: str) -> tuple[dict[str, tuple[Handler, ...]], str]:
        if pattern.endswith(".*"):
            return self._prefix, pattern[:-1]
        return self._exact, pattern

    def handlers_for(self, name: str) -> tuple[Handler, ...]:
        """Handlers that receive ``name``, deduplicated, in subscription-kind order (exact, prefix, *)."""
        found: list[Handler] = list(self._exact.get(name, ()))
        if self._prefix:
            for prefix, handlers in self._prefix.items():
                if name.startswith(prefix):
                    found.extend(handlers)
        found.extend(self._all)
        if len(found) <= 1:
            return tuple(found)
        unique: list[Handler] = []
        for h in found:
            if h not in unique:  # equality, so bound methods of one object dedupe too
                unique.append(h)
        return tuple(unique)

    # -- publishing ----------------------------------------------------------------------------

    async def publish(self, event: Event) -> int:
        """Deliver ``event`` to all matching handlers and wait for them.

        Returns the number of handlers that failed (raised or timed out). Never raises because of a
        handler; ``asyncio.CancelledError`` of the publisher propagates as usual.
        """
        if not isinstance(event, Event):
            raise TypeError("publish() expects an Event")
        handlers = self.handlers_for(event.name)
        if not handlers:
            return 0
        if len(handlers) == 1:
            return int(not await self._run(event, handlers[0]))
        results = await asyncio.gather(*(self._run(event, h) for h in handlers))
        return sum(1 for ok in results if not ok)

    def publish_nowait(self, event: Event) -> asyncio.Task[int] | None:
        """Schedule delivery in the background (for latency-sensitive callers).

        Returns the task (or ``None`` when nobody listens). Pending tasks are awaited by :meth:`drain`.
        """
        if self._closed:
            raise RuntimeError("event bus is closed")
        if not isinstance(event, Event):
            raise TypeError("publish_nowait() expects an Event")
        if not self.handlers_for(event.name):
            return None
        task = asyncio.get_running_loop().create_task(self.publish(event), name=f"bus:{event.name}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def drain(self, grace_s: float | None = None) -> None:
        """Wait for background deliveries; after ``grace_s`` seconds the leftovers are cancelled."""
        pending = set(self._background)
        if not pending:
            return
        _done, still = await asyncio.wait(pending, timeout=grace_s)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
            log.warning("event bus: cancelled %d background deliveries on drain", len(still))

    async def aclose(self, grace_s: float | None = 5.0) -> None:
        self._closed = True
        await self.drain(grace_s)

    async def _run(self, event: Event, handler: Handler) -> bool:
        try:
            if self._timeout is None:
                await handler(event)
            else:
                async with asyncio.timeout(self._timeout):
                    await handler(event)
            return True
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the publisher (or loop shutdown) is cancelling us
            await self._report(event, handler, exc)  # handler cancelled something of its own
            return False
        except Exception as exc:  # noqa: BLE001 - isolation boundary; logged with traceback in _report
            await self._report(event, handler, exc)
            return False

    async def _report(self, event: Event, handler: Handler, exc: BaseException) -> None:
        # Payload is never logged: it may contain user data.
        if isinstance(exc, TimeoutError):
            log.warning(
                "event %s: handler %s timed out after %ss", event.name, _handler_name(handler), self._timeout
            )
        else:
            log.error(
                "event %s: handler %s failed",
                event.name,
                _handler_name(handler),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        hook = self._on_error
        if hook is None:
            return
        try:
            res = hook(event, handler, exc)
            if res is not None:
                await res
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event %s: on_error hook failed", event.name)
