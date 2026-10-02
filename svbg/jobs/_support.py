"""Small helpers shared by the queue, the worker and the scheduler."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from svbg.core.log import mask

log = logging.getLogger("svbg.jobs")

#: Hard cap for error texts stored in the database.
MAX_ERROR_LEN = 2000


class ErrorCapture(Protocol):
    """The part of ``svbg.core.errors.hub.ErrorHub`` the jobs package needs."""

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str,
    ) -> Any: ...


def describe_error(exc: BaseException) -> str:
    """Short, masked one-line description of an exception for ``last_error`` columns."""
    message = str(exc).strip()
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return clip(mask(text))


def clip(text: str, limit: int = MAX_ERROR_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def report(
    hub: ErrorCapture | None,
    exc: BaseException,
    place: str,
    *,
    handled: str,
    context: Mapping[str, Any] | None = None,
    module: str | None = None,
) -> None:
    """Send ``exc`` to the error hub; a failing hub is logged and never propagates."""
    if hub is None:
        return
    try:
        await hub.capture(exc, place, module=module, context=dict(context or {}), handled=handled)
    except Exception:  # the error hub must never break the caller
        log.exception("error hub failed while reporting %s", place)
