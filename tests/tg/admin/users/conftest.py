"""Fixtures of the admin user screens (see ``kit``)."""

from __future__ import annotations

import pytest

from tests.dbkit import CountingDatabase
from tests.tg.admin.users.kit import UEnv, build_uenv


@pytest.fixture
async def env(db: CountingDatabase) -> UEnv:
    return await build_uenv(db)
