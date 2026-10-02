"""Fixtures of the ops tests: a second empty database (restore target), PostgreSQL client tools, fast KDF."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from svbg.core.crypto import Crypto, generate_key
from svbg.ops.crypt import KdfParams
from svbg.ops.pgtools import PgToolError, PgTools
from tests.dbkit import CountingDatabase, open_db
from tests.pgcluster import PgCluster

_counter = itertools.count()

#: scrypt n = 2**10: milliseconds instead of ~0.1 s per derivation (the format is the same).
FAST_KDF = KdfParams(n=2**10, r=8, p=1)
PASSWORD = "correct horse battery"


@pytest.fixture
async def target_dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    """A second, empty database on the test server (the restore target)."""
    name = f"r_{next(_counter)}_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
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


@pytest.fixture(scope="session")
def pg_tools(pg_cluster: PgCluster) -> PgTools:
    """``pg_dump`` / ``pg_restore`` next to the test server's binaries (``.tools/pgsql/bin``) or on PATH."""
    bindir = getattr(pg_cluster, "_bin", None)
    tools = PgTools(Path(bindir)) if bindir is not None else PgTools.locate()
    try:
        tools.version("pg_dump")
        tools.version("pg_restore")
    except PgToolError as exc:
        pytest.skip(f"PostgreSQL client tools are not available: {exc}")
    return tools


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
def secret_key() -> str:
    return generate_key()


async def seed(db: CountingDatabase, key: str, *, users: int = 25) -> dict[str, Any]:
    """Some rows in several tables, an encrypted setting and a payment instance with encrypted config."""
    crypto = Crypto([key])
    for i in range(users):
        await db.raw("insert into users (telegram_id, username) values ($1, $2)", 7000 + i, f"user{i}")
    await db.raw(
        "insert into settings (key, value, source) values ($1, to_jsonb($2::text), 'bot')",
        "REMNAWAVE_TOKEN",
        crypto.encrypt("panel-token-123"),
    )
    await db.raw(
        "insert into payment_instances (provider, slug, title, config, webhook_token) "
        "values ($1, $2, $3, $4, $5)",
        "rollypay",
        "rollypay",
        "RollyPay",
        crypto.encrypt('{"api_key": "x"}'),
        crypto.encrypt("w" * 40),
    )
    await db.raw("create table if not exists alembic_version (version_num varchar(32) primary key)")
    await db.raw("insert into alembic_version values ('test_rev_1')")
    return {"users": users}
