"""Fixtures for the settings screens (see ``settings_harness``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.dbkit import CountingDatabase, open_db
from tests.tg.admin.settings_harness import EnvFactory, SEnv, build_senv


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def make_senv(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[EnvFactory]:
    made: list[SEnv] = []

    async def factory(**kw: Any) -> SEnv:
        env = await build_senv(db, tmp_path / ".env", **kw)
        made.append(env)
        return env

    yield factory
    for env in made:
        await env.screens.drain()


@pytest.fixture
async def senv(make_senv: EnvFactory) -> SEnv:
    return await make_senv()
