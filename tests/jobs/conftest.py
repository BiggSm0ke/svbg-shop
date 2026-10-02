"""Fixtures for the jobs tests: a started database with the jobs schema, a fake error hub, tz data."""

from __future__ import annotations

import asyncio
import time
import zoneinfo
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.dbkit import CountingDatabase, open_db

REPO = Path(__file__).resolve().parents[2]


def _ensure_tzdata() -> bool:
    """Windows has no system tz database; reuse the one shipped with the local PostgreSQL if needed."""
    try:
        zoneinfo.ZoneInfo("Europe/Moscow")
        return True
    except zoneinfo.ZoneInfoNotFoundError:
        pass
    for candidate in (REPO / ".tools" / "pgsql" / "share" / "timezone",):
        if (candidate / "Europe" / "Moscow").exists():
            zoneinfo.reset_tzpath(to=[str(candidate)])
            zoneinfo.ZoneInfo.clear_cache()
            return True
    return False


HAVE_TZDATA = _ensure_tzdata()


@pytest.fixture
def tzdata() -> None:
    if not HAVE_TZDATA:
        pytest.skip("no IANA time zone database available (install tzdata)")


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    """The real ``Database`` on a fresh schema (pool of 12: worker lanes + the test's own transactions)."""
    async with open_db(pg_dsn, pool_size=12) as database:
        yield database


@dataclass
class Captured:
    exc: BaseException
    place: str
    handled: str
    context: Mapping[str, Any] | None
    module: str | None


@dataclass
class FakeHub:
    """Records ``capture`` calls; optionally fails to prove hub errors are isolated."""

    captured: list[Captured] = field(default_factory=list)
    fail: bool = False

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str,
    ) -> None:
        self.captured.append(Captured(exc, place, handled, context, module))
        if self.fail:
            raise RuntimeError("hub is broken")


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


async def wait_until(
    predicate: Callable[[], Awaitable[bool] | bool], timeout: float = 5.0, interval: float = 0.02
) -> float:
    """Poll ``predicate`` until true; returns elapsed seconds or fails the test."""
    start = time.monotonic()
    while True:
        res = predicate()
        if not isinstance(res, bool):
            res = await res
        if res:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            pytest.fail(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
