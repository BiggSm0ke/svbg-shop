from __future__ import annotations

from tests.dbkit import CountingDatabase


async def test_tables_created(db: CountingDatabase) -> None:
    rows = await db.raw("select tablename from pg_tables where schemaname = 'public' order by 1")
    names = {r["tablename"] for r in rows}
    assert {"screens", "screen_buttons", "media", "content_audit", "ui_state", "short_tokens"} <= names
