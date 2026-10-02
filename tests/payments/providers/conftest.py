"""Shared helpers of the wave A provider tests: the real core on a real database with one instance of the
provider under test (:class:`~svbg.payments.testkit.CoreHarness`) and small database helpers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest

from svbg.core.crypto import Crypto
from svbg.payments.testkit import CoreHarness, CountingHttp
from svbg.sdk import PaymentProvider
from tests.dbkit import CountingDatabase

HarnessFactory = Callable[..., Awaitable[CoreHarness]]


@pytest.fixture
async def make_harness(db: CountingDatabase, crypto: Crypto) -> AsyncIterator[HarnessFactory]:
    """``await make_harness(Provider, config, http=…, is_test=…, core_kwargs=…)``; closed after the test."""
    made: list[CoreHarness] = []

    async def factory(
        provider: type[PaymentProvider],
        config: Mapping[str, Any],
        *,
        http: Any = None,
        is_test: bool = False,
        has_domain: bool = True,
        core_kwargs: Mapping[str, Any] | None = None,
    ) -> CoreHarness:
        harness = await CoreHarness.create(
            db,
            crypto,
            provider,
            config,
            http=http if http is not None else CountingHttp(),
            is_test=is_test,
            has_domain=has_domain,
            core_kwargs=core_kwargs,
        )
        made.append(harness)
        return harness

    try:
        yield factory
    finally:
        for harness in made:
            await harness.close()


async def add_user(
    db: CountingDatabase, telegram_id: int, role: str = "user", perms: Sequence[str] = ()
) -> int:
    rows = await db.raw(
        "insert into users (telegram_id, role, perms) values ($1, $2, $3::jsonb) returning id",
        telegram_id,
        role,
        json.dumps(list(perms)),
    )
    return int(rows[0]["id"])


async def payment_row(db: CountingDatabase, payment_id: str) -> dict[str, Any]:
    rows = await db.raw("select * from payments where id = $1", payment_id)
    return dict(rows[0])


async def outcomes(db: CountingDatabase) -> list[str]:
    return [r["outcome"] for r in await db.raw("select outcome from payment_events order by id")]
