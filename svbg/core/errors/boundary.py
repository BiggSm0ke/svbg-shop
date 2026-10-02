"""Error boundaries: isolate a unit of work (update, screen, slot, job) and report failures to the hub.

Usage::

    async with guard("screen:home", hub=hub, user_id=u.id, on_error=show_fallback):
        await render_home(...)

    @guard("job:sync", hub=hub, module="remnawave")
    async def sync() -> None: ...

    async with timeout_guard("update", 25, hub=hub, on_error=toast_sorry):
        await dispatch(update)

``asyncio.CancelledError`` (and other ``BaseException`` such as ``KeyboardInterrupt``/``SystemExit``) is
never captured or swallowed. ``on_error`` may be sync or async; if it fails, that failure is captured too
(place ``<place>:on_error``) but never propagates. With ``reraise=True`` the original exception propagates
after being captured; an exception already captured by an inner guard is not reported twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Any, Protocol, Self

from svbg.core.errors.sanitize import clean

log = logging.getLogger("svbg.errors")

_CAPTURED_ATTR = "__svbg_captured__"


class Capturer(Protocol):
    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = ...,
        user_id: int | None = ...,
        context: Mapping[str, Any] | None = ...,
        handled: str = ...,
    ) -> str | None: ...


OnError = Callable[[BaseException], Awaitable[Any] | Any]


def _mark_captured(exc: BaseException) -> None:
    with contextlib.suppress(AttributeError, TypeError):  # exotic exceptions without __dict__
        setattr(exc, _CAPTURED_ATTR, True)


def was_captured(exc: BaseException) -> bool:
    return bool(getattr(exc, _CAPTURED_ATTR, False))


class guard:
    """Async context manager and decorator capturing ``Exception`` into the error hub."""

    __slots__ = ("context", "handled", "hub", "module", "on_error", "place", "reraise", "user_id")

    def __init__(
        self,
        place: str,
        *,
        hub: Capturer | None,
        module: str | None = None,
        user_id: int | None = None,
        on_error: OnError | None = None,
        reraise: bool = False,
        handled: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.place = place
        self.hub = hub
        self.module = module
        self.user_id = user_id
        self.on_error = on_error
        self.reraise = reraise
        self.handled = handled
        self.context = context

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is None or not isinstance(exc, Exception):
            return False  # success, CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit
        await self._report(exc, self.place)
        if self.on_error is not None:
            await self._run_on_error(exc)
        return not self.reraise

    async def _report(self, exc: BaseException, place: str) -> None:
        if was_captured(exc):
            return
        _mark_captured(exc)
        if self.hub is None:
            log.error("unhandled error at %s: %s", place, clean(f"{type(exc).__name__}: {exc}"))
            return
        kwargs: dict[str, Any] = {"module": self.module, "user_id": self.user_id, "context": self.context}
        if self.handled is not None:
            kwargs["handled"] = self.handled
        try:
            await self.hub.capture(exc, place, **kwargs)
        except Exception as e:  # noqa: BLE001 - the hub must not break the boundary (ErrorHub never raises)
            # No traceback: it may carry data of the original error.
            log.error("error hub raised while capturing at %s: %s", place, type(e).__name__)  # noqa: TRY400

    async def _run_on_error(self, exc: BaseException) -> None:
        assert self.on_error is not None
        try:
            result = self.on_error(exc)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001 - fallback failures are reported, never propagated
            await self._report(e, f"{self.place}:on_error")

    def __call__[**P, R](self, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | None]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("guard can decorate only async functions")
        boundary = self  # stateless: safe to share between concurrent calls

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
            async with boundary:
                return await fn(*args, **kwargs)
            return None

        return wrapper


class timeout_guard:
    """:class:`guard` around ``asyncio.timeout(seconds)``: a timeout becomes a captured ``TimeoutError``.

    Cancellation coming from outside still propagates as ``CancelledError``. One instance must not be
    entered concurrently (create one per use; the decorator form does that).
    """

    __slots__ = ("_guard", "_stack", "seconds")

    def __init__(
        self,
        place: str,
        seconds: float,
        *,
        hub: Capturer | None,
        module: str | None = None,
        user_id: int | None = None,
        on_error: OnError | None = None,
        reraise: bool = False,
        handled: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self.seconds = seconds
        ctx = {**(context or {}), "timeout_s": seconds}
        self._guard = guard(
            place,
            hub=hub,
            module=module,
            user_id=user_id,
            on_error=on_error,
            reraise=reraise,
            handled=handled,
            context=ctx,
        )
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        if self._stack is not None:
            raise RuntimeError("timeout_guard instance is already active")
        stack = AsyncExitStack()
        await stack.enter_async_context(self._guard)
        await stack.enter_async_context(asyncio.timeout(self.seconds))
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        stack, self._stack = self._stack, None
        assert stack is not None
        return await stack.__aexit__(exc_type, exc, tb)

    def __call__[**P, R](self, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | None]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("timeout_guard can decorate only async functions")
        g = self._guard

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
            inst = timeout_guard(
                g.place,
                self.seconds,
                hub=g.hub,
                module=g.module,
                user_id=g.user_id,
                on_error=g.on_error,
                reraise=g.reraise,
                handled=g.handled,
                context=g.context,
            )
            async with inst:
                return await fn(*args, **kwargs)
            return None

        return wrapper
