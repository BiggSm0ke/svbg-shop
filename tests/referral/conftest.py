"""Referral fixtures: the full schema plus the referral tables on a fresh database."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from svbg.core.bus import Event, EventBus
from svbg.referral.service import EVENT_ATTACHED, ReferralService
from tests.dbkit import CountingDatabase, open_db
from tests.referral.kit import PROD, Env, FakePoster, FakeSender, attach_tables, detach_tables


@pytest.fixture
def referral_schema() -> Iterator[None]:
    attach_tables()
    try:
        yield
    finally:
        detach_tables()


@pytest.fixture
async def db(pg_dsn: str, referral_schema: None) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn, pool_size=6) as database:
        yield database


@pytest.fixture
def cfg() -> dict[str, object]:
    return dict(PROD)


@pytest.fixture
async def env(db: CountingDatabase, cfg: dict[str, object]) -> Env:
    bus = EventBus()
    poster, sender = FakePoster(), FakeSender()
    svc = ReferralService(
        db,
        config=lambda: cfg,
        bus=bus,
        poster=poster,
        sender=sender,
        bot_username=lambda: "svbg_shop_bot",
    )
    svc.install(bus)
    environment = Env(db, svc, cfg, poster, sender, bus)

    async def record(event: Event) -> None:
        environment.events.append(event)

    bus.subscribe(EVENT_ATTACHED, record)
    return environment
