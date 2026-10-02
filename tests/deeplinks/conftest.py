"""Fixtures for deep-link tests: a fresh database with the application schema plus the deep-link tables."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

import svbg.deeplinks.tables  # noqa: F401 - registers deeplinks/deeplink_hits/deeplink_daily on the metadata
from svbg.core import clock
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture(autouse=True)
def _real_clock() -> Iterator[None]:
    yield
    clock.reset_clock()
