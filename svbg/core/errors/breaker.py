"""Per-module circuit breaker ("предохранитель").

More than ``threshold`` errors within ``window`` seconds opens the breaker (module ``degraded``: optional
parts such as slots and background loops should be switched off by the module). After ``quiet`` seconds
without errors it becomes HALF_OPEN (optional parts may try again); after another ``quiet`` seconds without
errors, or on :meth:`record_success`, it closes. Any error while HALF_OPEN re-opens it. :meth:`reset` closes
it manually (the "Включить снова" button).

State is evaluated lazily against the injected clock; transitions fire ``on_change(module, old, new)``
(sync or async; failures are logged and never propagate).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from svbg.core.errors.sanitize import Clock, default_clock

log = logging.getLogger("svbg.errors")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateHook = Callable[[str, BreakerState, BreakerState], Awaitable[None] | None]

_background: set[asyncio.Task[Any]] = set()


class CircuitBreaker:
    def __init__(
        self,
        module: str,
        *,
        threshold: int = 5,
        window: float = 300.0,
        quiet: float = 600.0,
        clock: Clock | None = None,
        on_change: StateHook | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.module = module
        self.threshold = threshold
        self.window = timedelta(seconds=window)
        self.quiet = timedelta(seconds=quiet)
        self._clock = clock or default_clock
        self._on_change = on_change
        self._errors: deque[datetime] = deque(maxlen=threshold + 1)
        self._state = BreakerState.CLOSED
        self._last_error: datetime | None = None
        self.opened_at: datetime | None = None

    # --- state ---------------------------------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        self._advance(self._clock())
        return self._state

    def allows(self) -> bool:
        """True when optional work of the module may run (CLOSED or HALF_OPEN)."""
        return self.state is not BreakerState.OPEN

    @property
    def degraded(self) -> bool:
        return self.state is BreakerState.OPEN

    def errors_in_window(self) -> int:
        now = self._clock()
        return sum(1 for ts in self._errors if now - ts < self.window)

    def _advance(self, now: datetime) -> None:
        if self._state is BreakerState.CLOSED or self._last_error is None:
            return
        quiet_for = now - self._last_error
        if self._state is BreakerState.OPEN and quiet_for >= self.quiet:
            self._set(BreakerState.HALF_OPEN)
        if self._state is BreakerState.HALF_OPEN and quiet_for >= self.quiet * 2:
            self._close()

    # --- events --------------------------------------------------------------------------------------------

    def record_error(self) -> BreakerState:
        now = self._clock()
        self._advance(now)
        self._last_error = now
        self._errors.append(now)
        if self._state is BreakerState.HALF_OPEN:
            self.opened_at = now
            self._set(BreakerState.OPEN)
        elif self._state is BreakerState.CLOSED:
            recent = sum(1 for ts in self._errors if now - ts < self.window)
            if recent > self.threshold:
                self.opened_at = now
                self._set(BreakerState.OPEN)
        return self._state

    def record_success(self) -> BreakerState:
        self._advance(self._clock())
        if self._state is BreakerState.HALF_OPEN:
            self._close()
        return self._state

    def reset(self) -> None:
        if self._state is not BreakerState.CLOSED:
            self._close()

    def _close(self) -> None:
        self._errors.clear()
        self.opened_at = None
        self._set(BreakerState.CLOSED)

    def _set(self, new: BreakerState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        log.warning("module %s breaker: %s → %s", self.module, old.value, new.value)
        if self._on_change is not None:
            _fire(self._on_change, self.module, old, new)


def _fire(hook: StateHook, module: str, old: BreakerState, new: BreakerState) -> None:
    try:
        result = hook(module, old, new)
    except Exception:
        log.exception("breaker state hook failed for module %s", module)
        return
    if not inspect.isawaitable(result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if inspect.iscoroutine(result):
            result.close()
        log.warning("breaker state hook for %s is async but no event loop is running", module)
        return

    async def runner() -> None:
        try:
            await result
        except Exception:
            log.exception("breaker state hook failed for module %s", module)

    task = loop.create_task(runner(), name=f"breaker-hook:{module}")
    _background.add(task)
    task.add_done_callback(_background.discard)
