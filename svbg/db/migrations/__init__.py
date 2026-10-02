"""Alembic migrations and a small programmatic API used by ``svbg migrate`` and the tests.

* :func:`upgrade` — bring a database to ``head`` (async; runs Alembic on a dedicated connection inside a
  worker thread so it never blocks or re-enters the caller's event loop). Concurrent runs are serialized by
  a PostgreSQL advisory lock taken in ``env.py``.
* :func:`offline_sql` — the SQL script Alembic would execute (``alembic upgrade head --sql``); needs neither a
  database nor the asyncio extension of SQLAlchemy.
* :func:`current_revision` / :func:`head_revision` — for ``svbg health`` and startup checks.

The DSN is passed through ``Config.attributes`` and never written into ``alembic.ini`` or logged.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

__all__ = [
    "ADVISORY_LOCK_ID",
    "MIGRATIONS_DIR",
    "alembic_config",
    "current_revision",
    "head_revision",
    "offline_sql",
    "upgrade",
]

log = logging.getLogger("svbg.db.migrations")

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent
# pg_advisory_xact_lock key: "svbg" in ASCII, so two processes never migrate at the same time.
ADVISORY_LOCK_ID: Final = 0x73766267


def alembic_config(dsn: str | None = None, *, configure_logging: bool = False) -> Config:
    """Alembic ``Config`` pointing at this package; ``dsn`` travels in ``attributes`` (never in the file)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_path_separator", "os")
    cfg.attributes["configure_logging"] = configure_logging
    if dsn is not None:
        cfg.attributes["dsn"] = dsn
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def offline_sql(revision: str = "head", *, dsn: str | None = None) -> str:
    """SQL text of ``upgrade <revision>`` in offline mode (no connection is made)."""
    cfg = alembic_config(dsn)
    buf = io.StringIO()
    cfg.output_buffer = buf
    command.upgrade(cfg, revision, sql=True)
    return buf.getvalue()


def _upgrade_sync(dsn: str, revision: str) -> None:
    command.upgrade(alembic_config(dsn), revision)


async def upgrade(dsn: str, revision: str = "head") -> None:
    """Apply migrations up to ``revision``. Raises on failure (the transaction is rolled back)."""
    log.info("applying database migrations up to %s", revision)
    await asyncio.to_thread(_upgrade_sync, dsn, revision)
    log.info("database schema is at %s", revision)


async def current_revision(dsn: str) -> str | None:
    """Revision stored in ``alembic_version`` (None for an empty database)."""
    import asyncpg

    from svbg.db.migrations.env_support import asyncpg_dsn

    conn = await asyncpg.connect(asyncpg_dsn(dsn), timeout=10)
    try:
        exists = await conn.fetchval("select to_regclass('public.alembic_version') is not null")
        if not exists:
            return None
        value = await conn.fetchval("select version_num from alembic_version limit 1")
        return None if value is None else str(value)
    finally:
        await conn.close()
