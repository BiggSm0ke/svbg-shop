"""Fixtures for error hub tests.

``errors_db`` yields a database handle with ``tx()``/``read()`` (the ``svbg.db.engine.Database`` interface)
on a fresh PostgreSQL database containing the error tables.

``Database`` (SQLAlchemy asyncio) needs the ``greenlet`` package. While it is not installed, a small local
adapter executes the same SQLAlchemy Core statements directly through asyncpg, so the hub's SQL is still
tested against a real PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from svbg.core.errors.tables import error_events, error_groups
from svbg.db.meta import metadata

HAS_GREENLET = importlib.util.find_spec("greenlet") is not None
_DIALECT = PGDialect_asyncpg()


class _Result:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def one(self) -> dict[str, Any]:
        assert len(self._rows) == 1, f"expected one row, got {len(self._rows)}"
        return self._rows[0]

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return next(iter(self.one().values()))

    def scalar(self) -> Any:
        row = self.first()
        return None if row is None else next(iter(row.values()))


class _AdapterConn:
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
        head = sql.lstrip().upper()
        if head.startswith(("SELECT", "WITH")) or " RETURNING " in sql.upper():
            rows = await self._conn.fetch(sql, *args)
            return _Result([dict(r) for r in rows], len(rows))
        status = await self._conn.execute(sql, *args)
        tail = status.rsplit(" ", 1)[-1]
        return _Result([], int(tail) if tail.isdigit() else 0)

    async def scalar(self, stmt: sa.Executable) -> Any:
        return (await self.execute(stmt)).scalar()


class AdapterDatabase:
    """Minimal ``Database`` stand-in (tx/read) over a single asyncpg pool."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[_AdapterConn]:
        async with self._pool.acquire() as conn, conn.transaction():
            yield _AdapterConn(conn)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[_AdapterConn]:
        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            yield _AdapterConn(conn)


async def _init_conn(conn: asyncpg.Connection) -> None:
    for name in ("json", "jsonb"):
        await conn.set_type_codec(name, encoder=lambda v: v, decoder=json.loads, schema="pg_catalog")


@pytest.fixture
def error_tables() -> Iterator[list[sa.Table]]:
    yield [error_groups, error_events]


@pytest.fixture
async def errors_db(pg_dsn: str, error_tables: list[sa.Table]) -> AsyncIterator[Any]:
    # Only our tables: keeps these tests independent from other packages' table modules.
    raw_dsn = pg_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    ddl = await asyncpg.connect(raw_dsn)
    try:
        for table in error_tables:
            await ddl.execute(str(sa.schema.CreateTable(table).compile(dialect=_DIALECT)))
            for index in table.indexes:
                await ddl.execute(str(sa.schema.CreateIndex(index).compile(dialect=_DIALECT)))
    finally:
        await ddl.close()
    assert set(error_tables) <= set(metadata.tables.values())

    if HAS_GREENLET:
        from svbg.db.engine import Database

        db = Database(pg_dsn, pool_size=4)
        await db.start()
        try:
            yield db
        finally:
            await db.close()
        return

    pool = await asyncpg.create_pool(raw_dsn, min_size=1, max_size=4, init=_init_conn)
    try:
        yield AdapterDatabase(pool)
    finally:
        await pool.close()
