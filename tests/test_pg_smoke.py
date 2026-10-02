import asyncpg


async def test_pg_cluster_boots(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        version = await conn.fetchval("show server_version")
        assert version.startswith("17")
        assert await conn.fetchval("select 1") == 1
    finally:
        await conn.close()


async def test_each_test_gets_empty_db(pg_dsn: str) -> None:
    conn = await asyncpg.connect(pg_dsn)
    try:
        n = await conn.fetchval("select count(*) from pg_tables where schemaname = 'public'")
        assert n == 0
    finally:
        await conn.close()
