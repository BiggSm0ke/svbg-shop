"""Fixtures for the catalog tests: the full schema (with the catalog tables) on a fresh database."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

import svbg.catalog.tables  # noqa: F401 - registers the catalog tables before ``create_schema``
from svbg.catalog.service import CatalogService
from tests.dbkit import CountingDatabase, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def catalog(db: CountingDatabase) -> CatalogService:
    service = CatalogService(db)
    await service.load()
    return service
