"""Schema aggregator.

Imports every ``tables`` module so ``svbg.db.meta.metadata`` is complete, and provides
``create_schema`` for tests. Production uses Alembic migrations (``svbg/db/migrations``).
"""

from __future__ import annotations

import importlib
import re

from svbg.db.meta import metadata

# Modules that declare tables. Add new ones here (keep sorted).
TABLE_MODULES: tuple[str, ...] = (
    "svbg.ads.tables",
    "svbg.billing.tables",
    "svbg.broadcasts.tables",
    "svbg.catalog.tables",
    "svbg.content.tables",
    "svbg.core.errors.tables",
    "svbg.core.settings.tables",
    "svbg.core.tables",
    "svbg.deeplinks.tables",
    "svbg.ext.ip_guard.tables",
    "svbg.ext.lte.service",  # ``lte_kv``, ``lte_event_cursor`` (runtime tables of the LTE module)
    "svbg.ext.lte.tables",
    "svbg.importers",  # ``legacy_id_map``, ``legacy_transactions``
    "svbg.jobs.tables",
    "svbg.pages.tables",
    "svbg.payments.tables",
    "svbg.promo.tables",
    "svbg.referral.tables",
    "svbg.remnawave.tables",
    "svbg.services.notify_user",
    "svbg.services.tables",
    "svbg.subscriptions.tables",
    "svbg.support.tables",
    "svbg.tg.ui.tables",
    "svbg.tg.user.tables",
)

#: Modules whose ``INDEX_DDL`` (raw ``CREATE INDEX IF NOT EXISTS``: trigram / expression indexes that are
#: not declared on the tables) belongs to the schema. ``create_schema`` runs them after ``create_all``; the
#: Alembic revisions carry frozen copies (``0004_stage3_4``).
INDEX_DDL_MODULES: tuple[str, ...] = ("svbg.tg.admin.users.search",)

EXTENSIONS: tuple[str, ...] = ("pg_trgm",)

_INDEX_NAME_RE = re.compile(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\w+)")


def load_all() -> None:
    for name in TABLE_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            # Allow partially built trees during development; a missing *table module* is skipped,
            # but a missing dependency inside an existing module must still fail loudly.
            if e.name != name:
                raise


def index_ddl() -> tuple[str, ...]:
    """Every statement of :data:`INDEX_DDL_MODULES` (in order)."""
    out: list[str] = []
    for name in INDEX_DDL_MODULES:
        out.extend(importlib.import_module(name).INDEX_DDL)
    return tuple(out)


def index_ddl_names() -> frozenset[str]:
    """Names of the indexes created by :func:`index_ddl` (Alembic autogenerate ignores them)."""
    return frozenset(name for ddl in index_ddl() for name in _INDEX_NAME_RE.findall(ddl))


async def create_schema(dsn: str) -> None:
    """Create extensions and all tables (tests / first boot without Alembic)."""
    # Imported lazily: SQLAlchemy's asyncio extension needs ``greenlet``; ``load_all`` and ``metadata``
    # must stay importable without it (Alembic offline mode, schema introspection in tests).
    from sqlalchemy.ext.asyncio import create_async_engine

    from svbg.db.engine import normalize_dsn

    load_all()
    sa_url, _ = normalize_dsn(dsn)
    engine = create_async_engine(sa_url)
    try:
        async with engine.begin() as conn:
            for ext in EXTENSIONS:
                await conn.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            await conn.run_sync(metadata.create_all)
            for ddl in index_ddl():
                await conn.exec_driver_sql(ddl)
    finally:
        await engine.dispose()
