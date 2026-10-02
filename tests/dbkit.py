"""Test helpers around the real :class:`svbg.db.engine.Database` (no SQL shims).

* :class:`SqlCounter` counts the statements an engine sends to PostgreSQL (SQLAlchemy's
  ``before_cursor_execute`` event on ``engine.sync_engine``) — for "SQL per click" budgets on hot paths.
  Pool pre-pings and transaction BEGIN/COMMIT are not statements of the code under test and are not counted.
* :class:`CountingDatabase` is the production ``Database`` with a counter attached on ``start()``
  (:attr:`CountingDatabase.queries`) and :meth:`CountingDatabase.raw` for test setup/assertions — plain SQL on
  a separate small asyncpg pool, so it is never counted and never competes with the pool under test.
* :func:`open_db` creates the full schema (``create_schema``) and yields a started ``CountingDatabase``.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

import asyncpg
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from svbg.db.engine import Database
from svbg.db.schema import create_schema

__all__ = ["CountingDatabase", "SqlCounter", "add_user", "open_db"]

_RECENT = 50  # statements kept for assertion messages


class SqlCounter:
    """Counts statements executed through ``engine`` while attached (also usable as a context manager)."""

    def __init__(self, engine: AsyncEngine | Engine) -> None:
        self._engine: Engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
        self.count = 0
        self.recent: deque[str] = deque(maxlen=_RECENT)
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        self._attached = True

    def _on_execute(
        self,
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        self.count += 1
        self.recent.append(statement)

    def since(self, mark: int) -> list[str]:
        """The (recent) statements executed after ``count`` was ``mark`` — for failure messages."""
        n = self.count - mark
        return list(self.recent)[-n:] if n > 0 else []

    def close(self) -> None:
        if self._attached:
            event.remove(self._engine, "before_cursor_execute", self._on_execute)
            self._attached = False

    def __enter__(self) -> SqlCounter:
        return self

    def __exit__(
        self, _t: type[BaseException] | None, _e: BaseException | None, _tb: TracebackType | None
    ) -> None:
        self.close()


def _encode_json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


async def _init_raw(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=_encode_json, decoder=json.loads, schema="pg_catalog")


class CountingDatabase(Database):
    """The real ``Database`` plus a statement counter and an uncounted ``raw()`` for tests."""

    def __init__(self, dsn: str, **kwargs: Any) -> None:
        super().__init__(dsn, **kwargs)
        self.counter: SqlCounter | None = None
        self._raw_pool: asyncpg.Pool | None = None

    @property
    def queries(self) -> int:
        """Statements executed through the pool since ``start()``."""
        return self.counter.count if self.counter is not None else 0

    async def start(self) -> None:
        await super().start()
        if self.counter is None:
            self.counter = SqlCounter(self.engine)

    async def raw(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """Plain SQL (``$1`` placeholders) for test setup/assertions; not counted. JSON is decoded."""
        if self._raw_pool is None:
            self._raw_pool = await asyncpg.create_pool(self.pg_dsn, min_size=1, max_size=2, init=_init_raw)
        return list(await self._raw_pool.fetch(sql, *args))

    async def close(self) -> None:
        pool, self._raw_pool = self._raw_pool, None
        if pool is not None:
            await pool.close()
        if self.counter is not None:
            self.counter.close()
            self.counter = None
        await super().close()


@asynccontextmanager
async def open_db(dsn: str, *, schema: bool = True, **kwargs: Any) -> AsyncIterator[CountingDatabase]:
    """A started database on ``dsn``; with ``schema`` every table of the application is created first."""
    if schema:
        await create_schema(dsn)
    db = CountingDatabase(dsn, **kwargs)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def add_user(db: CountingDatabase, telegram_id: int, role: str = "user") -> int:
    rows = await db.raw(
        "insert into users (telegram_id, role) values ($1, $2) returning id", telegram_id, role
    )
    return int(rows[0]["id"])
