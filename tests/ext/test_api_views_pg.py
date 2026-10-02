"""Module view loaders on real PostgreSQL: a failing or hanging loader never breaks the screen's connection
(an SQL error aborts a PG transaction, a cancelled query invalidates the connection)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.ext import ModuleSpec, ViewLoader, enabled_setting
from tests.dbkit import open_db
from tests.ext.test_api_host import Cfg, make

pytestmark = pytest.mark.pg


def module(name: str, sql: str) -> ModuleSpec:
    async def load(conn: Any, user: Any, view: Mapping[str, Any]) -> Any:
        return (await conn.execute(sa.text(sql))).scalar()

    return ModuleSpec(
        name=name,
        title=name,
        enabled_key=f"{name.upper()}_ENABLED",
        settings=(enabled_setting(name, name, "Тест."),),
        views=(ViewLoader("home", load),),
    )


async def test_failed_loader_rolls_back_to_its_savepoint_on_the_screen_connection(pg_dsn: str) -> None:
    async with open_db(pg_dsn, schema=False) as db:
        host, hub, _ = make(module("lte", "select 1/0"), module("ipg", "select 7"))
        await host.start(Cfg(LTE_ENABLED=True, IPG_ENABLED=True))
        async with db.read() as conn:
            assert (await conn.execute(sa.text("select 1"))).scalar() == 1  # the core read before
            models = await host.load_views("home", conn, None, {})
            assert models == {"ipg": 7}
            assert (await conn.execute(sa.text("select 2"))).scalar() == 2  # the core read after
        assert [(c[0], c[2]) for c in hub.captured] == [("module:lte:view:home", "DBAPIError")]


async def test_loaders_on_their_own_connection_survive_a_timeout_and_an_error(pg_dsn: str) -> None:
    async with open_db(pg_dsn, schema=False) as db:
        host, hub, _ = make(
            module("lte", "select pg_sleep(2)"),
            module("ipg", "select 1/0"),
            module("ads", "select 7"),
            slot_timeout=0.3,
            deps={"db": db},
        )
        await host.start(Cfg(LTE_ENABLED=True, IPG_ENABLED=True, ADS_ENABLED=True))
        async with db.read() as conn:
            assert (await conn.execute(sa.text("select 1"))).scalar() == 1
            models = await host.load_views("home", conn, None, {})
            assert models == {"ads": 7}  # a fresh connection after each failure
            assert (await conn.execute(sa.text("select 2"))).scalar() == 2
        assert [c[2] for c in hub.captured] == ["TimeoutError", "DBAPIError"]
