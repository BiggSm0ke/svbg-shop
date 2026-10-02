"""Promo fixtures: the full schema plus the promo tables on a fresh database."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from svbg.core.bus import EventBus
from svbg.promo.service import PromoService
from svbg.services.roles import Limits
from tests.dbkit import CountingDatabase, open_db
from tests.promo.kit import FakeCatalog, attach_tables, detach_tables, trial_service

#: Admins: 31 days and 500 ₽ per use (owners are not limited).
LIMITS = Limits(grant_days_max=31, wallet_adjust_max_minor=50_000)


@pytest.fixture
def promo_schema() -> Iterator[None]:
    attach_tables()
    try:
        yield
    finally:
        detach_tables()


@pytest.fixture
async def db(pg_dsn: str, promo_schema: None) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
def catalog() -> FakeCatalog:
    return FakeCatalog.build()


@pytest.fixture
async def service(db: CountingDatabase, catalog: FakeCatalog) -> PromoService:
    svc = PromoService(
        db,
        catalog=catalog,
        trial=trial_service(db, catalog),
        currency=lambda: "RUB",
        limits=lambda: LIMITS,
    )
    await svc.load()
    return svc


@pytest.fixture
def bus() -> EventBus:
    return EventBus()
