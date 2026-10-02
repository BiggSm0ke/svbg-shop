"""Pages fixtures: the full schema plus the page tables (attached only while a pages test runs, until
integration registers ``svbg.pages.tables``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from svbg.db import schema
from svbg.pages import tables as page_tables
from svbg.pages.service import PageService
from tests.dbkit import CountingDatabase, open_db
from tests.promo.kit import attach_tables, detach_tables

TABLES = (page_tables.pages, page_tables.page_versions, page_tables.page_consents)
REGISTERED = "svbg.pages.tables" in schema.TABLE_MODULES
detach_tables(TABLES, registered=REGISTERED)


@pytest.fixture
def pages_schema() -> Iterator[None]:
    attach_tables(TABLES)
    try:
        yield
    finally:
        detach_tables(TABLES, registered=REGISTERED)


@pytest.fixture
async def db(pg_dsn: str, pages_schema: None) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def pages(db: CountingDatabase) -> PageService:
    svc = PageService(db)
    await svc.load()
    return svc
