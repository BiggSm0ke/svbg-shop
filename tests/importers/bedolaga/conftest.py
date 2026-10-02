"""Fixtures: a synthetic Bedolaga source built from the owner's real schema dump and a fresh SvBG target.

* ``bedolaga_template`` (session): one database per test session loaded with ``.tools/bedolaga/db-schema.sql``
  (structure only); roles, owners, grants and psql meta-commands of the dump are filtered out;
* ``src_dsn``: a fresh copy of it (``CREATE DATABASE … TEMPLATE``) per test;
* ``target``: the application schema (``create_schema``) plus the stage-3/4 tables that are not registered in
  ``svbg.db.schema`` yet, and ``import_runs.mode`` widened to ``shadow`` (the integration migration asked
  for in
  the report).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest

import svbg.ads.tables
import svbg.importers
import svbg.promo.tables
import svbg.referral.tables  # noqa: F401
from tests.dbkit import CountingDatabase, open_db
from tests.pgcluster import REPO, PgCluster

SCHEMA = REPO / ".tools" / "bedolaga" / "db-schema.sql"
_SKIP_PREFIXES = (
    "GRANT ",
    "REVOKE ",
    "SELECT pg_catalog.set_config",
    "CREATE EXTENSION",
    "COMMENT ON EXTENSION",
)


def filtered_ddl(path: Path = SCHEMA) -> str:
    """The dump without roles / owners / grants / extensions / psql meta-commands (``\\restrict``)."""
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not buf and (not line.strip() or line.startswith("--") or line.startswith("\\")):
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    keep = []
    for st in statements:
        head = st.lstrip()
        if head.startswith(_SKIP_PREFIXES) or (head.startswith("ALTER ") and " OWNER TO " in head):
            continue
        keep.append(st)
    return "\n".join(keep)


@pytest.fixture(scope="session")
def bedolaga_template(pg_cluster: PgCluster) -> Iterator[str]:
    name = f"bedolaga_tpl_{uuid.uuid4().hex[:8]}"

    async def create() -> None:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(pg_cluster.dsn(name))
        try:
            await conn.execute(filtered_ddl())
        finally:
            await conn.close()

    async def drop() -> None:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(create())
    try:
        yield name
    finally:
        asyncio.run(drop())


@pytest.fixture
async def src_dsn(pg_cluster: PgCluster, bedolaga_template: str) -> AsyncIterator[str]:
    name = f"bsrc_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{bedolaga_template}"')
    finally:
        await admin.close()
    try:
        yield pg_cluster.dsn(name)
    finally:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def widen_import_modes(db: CountingDatabase) -> None:
    await db.raw("ALTER TABLE import_runs DROP CONSTRAINT IF EXISTS ck_import_runs_mode")
    await db.raw(
        "ALTER TABLE import_runs ADD CONSTRAINT ck_import_runs_mode "
        "CHECK (mode IN ('dry_run', 'shadow', 'apply'))"
    )


@pytest.fixture
async def target(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn, pool_size=4) as db:
        await widen_import_modes(db)
        yield db
