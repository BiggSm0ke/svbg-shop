"""Fixtures for content tests: ``db`` is a fresh database with the ui/content tables."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database
