"""Alembic environment: PostgreSQL through asyncpg, metadata from ``svbg.db.meta`` (all table modules).

Ways to run:

* ``svbg migrate`` / :func:`svbg.db.migrations.upgrade` — the DSN comes in ``config.attributes["dsn"]``;
* ``alembic -c alembic.ini upgrade head`` — the DSN is resolved like the bot does it (``data/.env`` →
  environment ``DATABASE_URL``); it is never stored in ``alembic.ini``;
* a caller that already holds a synchronous connection passes it as ``config.attributes["connection"]``;
* offline (``--sql``) — no database needed, the SQL script is printed.

Online runs take ``pg_advisory_xact_lock`` first, so two processes never migrate concurrently.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from svbg.db import schema
from svbg.db.meta import metadata
from svbg.db.migrations import ADVISORY_LOCK_ID
from svbg.db.migrations.env_support import resolve_dsn, sqlalchemy_url

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

schema.load_all()
target_metadata = metadata

#: Indexes created from raw DDL (``schema.index_ddl``: trigram / expression indexes); they are not on the
#: metadata, so autogenerate must not offer to drop them.
_RAW_INDEXES = schema.index_ddl_names()


def _include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    return not (type_ == "index" and reflected and compare_to is None and name in _RAW_INDEXES)


_CONFIGURE: dict[str, Any] = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
    "include_object": _include_object,
}


def _dsn() -> str:
    dsn = resolve_dsn(config.attributes.get("dsn") or config.get_main_option("sqlalchemy.url"))
    if not dsn:
        raise RuntimeError("DATABASE_URL не задан: впишите его в data/.env или в окружение контейнера")
    return sqlalchemy_url(dsn)


def run_migrations_offline() -> None:
    """Emit SQL to the output buffer; only the dialect matters, so no DSN is required."""
    explicit = config.attributes.get("dsn")
    url = sqlalchemy_url(explicit) if explicit else "postgresql+asyncpg://"
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_CONFIGURE)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_CONFIGURE)
    with context.begin_transaction():
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_ID})
        context.run_migrations()


async def run_async_migrations() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_dsn(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
