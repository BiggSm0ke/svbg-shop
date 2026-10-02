"""The SQL counter used for "SQL per click" budgets counts exactly the statements of the code under test."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from svbg.jobs.tables import jobs
from tests.dbkit import CountingDatabase, SqlCounter, open_db

pytestmark = pytest.mark.pg


async def test_counts_statements_not_pings_or_raw(e2e_dsn: str) -> None:
    async with open_db(e2e_dsn, schema=False) as db:
        assert db.queries == 0, "start()'s own probe happens before the counter is attached"
        for _ in range(3):  # pool checkouts with pre-ping and BEGIN/ROLLBACK: no statements
            async with db.read():
                pass
            async with db.tx():
                pass
        assert db.queries == 0
        await db.raw("select 1")  # test setup/assertion SQL is never counted
        assert db.queries == 0

        async with db.read() as conn:
            await conn.execute(sa.select(sa.func.count()).select_from(jobs))
            await conn.exec_driver_sql("select 1")
        assert db.queries == 2
        assert db.counter is not None and db.counter.since(0)[-1] == "select 1"

        async with db.tx() as conn:  # executemany is one round trip, counted once
            await conn.execute(
                sa.insert(jobs),
                [{"kind": "k", "queue": "default", "payload": {}} for _ in range(3)],
            )
        assert db.queries == 3
        rows = await db.raw("select count(*) as n from jobs")
        assert rows[0]["n"] == 3


async def test_counter_detaches(e2e_dsn: str) -> None:
    db = CountingDatabase(e2e_dsn)
    await db.start()
    try:
        with SqlCounter(db.engine) as extra:
            async with db.read() as conn:
                await conn.exec_driver_sql("select 1")
        assert extra.count == 1
        async with db.read() as conn:
            await conn.exec_driver_sql("select 1")
        assert extra.count == 1  # detached
        assert db.queries == 2
    finally:
        await db.close()
    assert db.queries == 0 and db.counter is None
