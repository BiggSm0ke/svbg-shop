"""Ads fixtures: the full schema plus the ad tables (attached only while an ads test runs, until integration
registers ``svbg.ads.tables``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from svbg.ads import tables as ad_tables
from svbg.ads.service import AdService
from svbg.db import schema
from tests.dbkit import CountingDatabase, open_db
from tests.promo.kit import attach_tables, detach_tables

TABLES = (ad_tables.ad_links, ad_tables.ad_link_users)
REGISTERED = "svbg.ads.tables" in schema.TABLE_MODULES
detach_tables(TABLES, registered=REGISTERED)


@pytest.fixture
def ads_schema() -> Iterator[None]:
    attach_tables(TABLES)
    try:
        yield
    finally:
        detach_tables(TABLES, registered=REGISTERED)


@pytest.fixture
async def db(pg_dsn: str, ads_schema: None) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def ads(db: CountingDatabase) -> AdService:
    svc = AdService(db)
    await svc.load()
    return svc
