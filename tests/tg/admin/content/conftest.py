"""Fixtures of the constructor UI tests (see ``kit``); ``db`` comes from ``tests/tg/admin/conftest.py``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.dbkit import CountingDatabase
from tests.tg.admin.content.kit import CEnv, build_cenv


@pytest.fixture
async def ce(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[CEnv]:
    async with build_cenv(db, tmp_path) as env:
        yield env
