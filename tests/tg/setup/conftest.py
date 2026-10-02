"""Fixtures for the owner link: a real PostgreSQL database and the settings-screens harness."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.dbkit import CountingDatabase, open_db
from tests.tg.admin.settings_harness import SEnv, build_senv


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def senv(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[SEnv]:
    env = await build_senv(db, tmp_path / ".env")
    yield env
    await env.screens.drain()
