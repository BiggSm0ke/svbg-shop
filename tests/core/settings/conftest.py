"""Fixtures for settings tests: a real PostgreSQL database with the settings tables, fake components.

``svbg.db.engine.Database`` (SQLAlchemy asyncio) needs ``greenlet``, which the shared environment lacks; a
small adapter runs the very same SQLAlchemy Core statements through asyncpg, so the SQL is still exercised on
a real PostgreSQL 17. The adapter also simulates database outages (``fail_writes``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.exc import DBAPIError, OperationalError

from svbg.core.component import ComponentRegistry, HealthReport, ProbeError
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.registry import Registry, core_registry
from svbg.core.settings.service import SettingsService
from svbg.core.settings.tables import settings, settings_audit
from svbg.core.tables import config_meta

_DIALECT = PGDialect_asyncpg()


class _Result:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any:
        row = self.first()
        return None if row is None else next(iter(row.values()))


class _Conn:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, stmt: sa.Executable) -> _Result:
        compiled = stmt.compile(dialect=_DIALECT, compile_kwargs={"render_postcompile": True})
        params = compiled.construct_params()
        procs = compiled._bind_processors
        args = []
        for name in compiled.positiontup or ():
            value = params[name]
            proc = procs.get(name)
            args.append(proc(value) if proc is not None and value is not None else value)
        sql = compiled.string
        try:
            if sql.lstrip().upper().startswith(("SELECT", "WITH")) or " RETURNING " in sql.upper():
                rows = await self._conn.fetch(sql, *args)
                return _Result([dict(r) for r in rows], len(rows))
            status = await self._conn.execute(sql, *args)
        except asyncpg.PostgresError as exc:
            raise DBAPIError(sql, args, exc) from exc
        tail = status.rsplit(" ", 1)[-1]
        return _Result([], int(tail) if tail.isdigit() else 0)


class ShimDatabase:
    """``tx()``/``read()`` like :class:`svbg.db.engine.Database`; ``fail_writes`` simulates an outage."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None
        self.fail_writes = False
        self.fail_reads = False

    async def start(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)

    async def create_tables(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            for table in (settings, settings_audit, config_meta):
                await conn.execute(str(sa.schema.CreateTable(table).compile(dialect=_DIALECT)))
                for index in table.indexes:
                    await conn.execute(str(sa.schema.CreateIndex(index).compile(dialect=_DIALECT)))

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[_Conn]:
        if self.fail_writes:
            raise OperationalError("begin", None, ConnectionError("database is down"))
        assert self.pool is not None
        async with self.pool.acquire() as conn, conn.transaction():
            yield _Conn(conn)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[_Conn]:
        if self.fail_reads:
            raise OperationalError("begin", None, ConnectionError("database is down"))
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            try:
                yield _Conn(conn)
            finally:
                await tr.rollback()

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        assert self.pool is not None
        return await self.pool.fetch(sql, *args)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[ShimDatabase]:
    database = ShimDatabase(pg_dsn)
    await database.start()
    await database.create_tables()
    try:
        yield database
    finally:
        await database.close()


@dataclass
class FakeComponent:
    """Records probe/reconfigure calls; failures are configurable."""

    name: str
    probe_error: BaseException | None = None
    probe_delay: float = 0.0
    reconfigure_error: BaseException | None = None
    fail_reconfigure_times: int = 0
    probes: list[dict[str, Any]] = field(default_factory=list)
    reconfigs: list[dict[str, Any]] = field(default_factory=list)

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        self.probes.append(dict(candidate))
        if self.probe_delay:
            await asyncio.sleep(self.probe_delay)
        if self.probe_error is not None:
            raise self.probe_error

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        self.reconfigs.append(dict(cfg))
        if self.reconfigure_error is not None and self.fail_reconfigure_times > 0:
            self.fail_reconfigure_times -= 1
            raise self.reconfigure_error

    async def health(self) -> HealthReport:
        return HealthReport.ok()


@pytest.fixture
def crypto() -> Crypto:
    return Crypto([generate_key()])


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / ".env"


@pytest.fixture
def components() -> ComponentRegistry:
    reg = ComponentRegistry()
    reg.register(FakeComponent("bot"))
    reg.register(FakeComponent("remnawave"))
    return reg


ServiceFactory = Callable[..., Awaitable[SettingsService]]


@pytest.fixture
def make_service(
    db: ShimDatabase, crypto: Crypto, components: ComponentRegistry, env_path: Path
) -> ServiceFactory:
    async def factory(
        *,
        environ: Mapping[str, str] | None = None,
        registry: Registry | None = None,
        crypto_: Crypto | None = None,
        load: bool = True,
        **kwargs: Any,
    ) -> SettingsService:
        service = SettingsService(
            db,
            registry or core_registry(),
            crypto_ or crypto,
            components,
            environ=environ or {},
            env_path=env_path,
            **kwargs,
        )
        if load:
            await service.load()
        return service

    return factory


async def wait_until(
    predicate: Callable[[], Awaitable[bool] | bool], timeout: float = 5.0, interval: float = 0.02
) -> float:
    """Poll ``predicate`` until true; returns elapsed seconds or fails the test."""
    start = time.monotonic()
    while True:
        res = predicate()
        if not isinstance(res, bool):
            res = await res
        if res:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            pytest.fail(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


__all__ = ["FakeComponent", "ProbeError", "ShimDatabase", "wait_until"]
