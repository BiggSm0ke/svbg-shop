"""Shared test fixtures.

- ``pg_cluster`` (session): one throwaway PostgreSQL server for the whole run.
- ``pg_dsn`` (function): a fresh, empty database on that server for each test that asks for it.
  Tests that need the application schema should call the schema helper from ``svbg.db`` on it.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest

from tests.pgcluster import PgCluster, start_cluster

_counter = itertools.count()


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[PgCluster]:
    cluster = start_cluster()
    try:
        yield cluster
    finally:
        cluster.stop()


@pytest.fixture
async def pg_dsn(pg_cluster: PgCluster) -> AsyncIterator[str]:
    name = f"t_{next(_counter)}_{uuid.uuid4().hex[:8]}"
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
