"""Fixtures: a database with the broadcast tables and ``users.notify_marketing``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from svbg.core import clock
from tests.broadcasts.kit import NOW, prepare
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn, pool_size=12) as database:
        await prepare(database)
        yield database


@pytest.fixture
def frozen() -> Iterator[None]:
    """``clock.now()`` = NOW (subscription states are computed against it)."""
    clock.set_clock(NOW)
    try:
        yield
    finally:
        clock.reset_clock()
