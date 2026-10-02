"""Database access for settings: rows, audit, ``config_meta`` entries.

SQLAlchemy Core only. JSON values are sent as text and cast to ``jsonb`` on the server, and read back as
text, so the exact JSON (including ``null`` and strings like ``"enc:v1:…"``) never depends on driver codecs.
Encryption is done by the service; this module only moves already-encoded values.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.settings.tables import settings, settings_audit
from svbg.core.tables import config_meta
from svbg.db.meta import JSONB

__all__ = ["AuditEntry", "AuditWrite", "DatabaseLike", "RowWrite", "SettingsStore", "StoredRow"]


class DatabaseLike(Protocol):
    """The part of :class:`svbg.db.engine.Database` used here."""

    def tx(self) -> AbstractAsyncContextManager[Any]: ...

    def read(self) -> AbstractAsyncContextManager[Any]: ...


@dataclass(frozen=True, slots=True)
class StoredRow:
    key: str
    value: Any  # decoded JSON (secrets: "enc:v1:…")
    source: str
    updated_at: datetime | None
    updated_by: int | None


@dataclass(frozen=True, slots=True)
class RowWrite:
    """Upsert (``delete=False``) or delete one ``settings`` row."""

    key: str
    value: Any = None  # JSON-able, secrets already encrypted
    source: str = "bot"
    actor_id: int | None = None
    delete: bool = False


@dataclass(frozen=True, slots=True)
class AuditWrite:
    key: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    applied: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    batch_id: str
    key: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    source: str
    actor_id: int | None
    ts: datetime
    applied: bool
    error: str | None


def _jsonb(value: Any) -> sa.ColumnElement[Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return sa.cast(sa.literal(text, sa.Text), JSONB)


def _jsonb_or_null(value: dict[str, Any] | None) -> sa.ColumnElement[Any]:
    return sa.null() if value is None else _jsonb(value)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


_AUDIT_COLUMNS = (
    settings_audit.c.id,
    settings_audit.c.batch_id,
    settings_audit.c.key,
    sa.cast(settings_audit.c.old, sa.Text).label("old"),
    sa.cast(settings_audit.c.new, sa.Text).label("new"),
    settings_audit.c.source,
    settings_audit.c.actor_id,
    settings_audit.c.ts,
    settings_audit.c.applied,
    settings_audit.c.error,
)


def _audit_entry(row: Any) -> AuditEntry:
    return AuditEntry(
        id=int(row["id"]),
        batch_id=row["batch_id"],
        key=row["key"],
        old=_loads(row["old"]),
        new=_loads(row["new"]),
        source=row["source"],
        actor_id=row["actor_id"],
        ts=row["ts"],
        applied=bool(row["applied"]),
        error=row["error"],
    )


class SettingsStore:
    def __init__(self, db: DatabaseLike) -> None:
        self.db = db

    # ---- settings rows

    async def load(self) -> dict[str, StoredRow]:
        query = sa.select(
            settings.c.key,
            sa.cast(settings.c.value, sa.Text).label("value"),
            settings.c.source,
            settings.c.updated_at,
            settings.c.updated_by,
        )
        async with self.db.read() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return {
            r["key"]: StoredRow(r["key"], _loads(r["value"]), r["source"], r["updated_at"], r["updated_by"])
            for r in rows
        }

    async def write(
        self,
        *,
        batch_id: str,
        source: str,
        actor_id: int | None,
        rows: Sequence[RowWrite] = (),
        audit: Sequence[AuditWrite] = (),
        mark_failed: Sequence[tuple[str, str, str]] = (),
    ) -> None:
        """One transaction: upsert/delete rows, append audit records, mark earlier audit records as not
        applied (``(batch_id, key, error)`` — used by automatic rollback)."""
        ts = now()
        async with self.db.tx() as conn:
            for row in rows:
                if row.delete:
                    await conn.execute(sa.delete(settings).where(settings.c.key == row.key))
                    continue
                stmt = pg_insert(settings).values(
                    key=row.key,
                    value=_jsonb(row.value),
                    source=row.source,
                    updated_at=ts,
                    updated_by=row.actor_id,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[settings.c.key],
                    set_={
                        "value": stmt.excluded.value,
                        "source": stmt.excluded.source,
                        "updated_at": stmt.excluded.updated_at,
                        "updated_by": stmt.excluded.updated_by,
                    },
                )
                await conn.execute(stmt)
            for failed_batch, key, error in mark_failed:
                await conn.execute(
                    sa.update(settings_audit)
                    .where(settings_audit.c.batch_id == failed_batch, settings_audit.c.key == key)
                    .values(applied=False, error=error)
                )
            if audit:
                await conn.execute(
                    sa.insert(settings_audit).values(
                        [
                            {
                                "batch_id": batch_id,
                                "key": a.key,
                                "old": _jsonb_or_null(a.old),
                                "new": _jsonb_or_null(a.new),
                                "source": source,
                                "actor_id": actor_id,
                                "ts": ts,
                                "applied": a.applied,
                                "error": a.error,
                            }
                            for a in audit
                        ]
                    )
                )

    # ---- audit

    async def batch(self, batch_id: str) -> list[AuditEntry]:
        query = (
            sa.select(*_AUDIT_COLUMNS)
            .where(settings_audit.c.batch_id == batch_id)
            .order_by(settings_audit.c.id)
        )
        async with self.db.read() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_audit_entry(r) for r in rows]

    async def history(self, key: str | None = None, *, limit: int = 20) -> list[AuditEntry]:
        query = sa.select(*_AUDIT_COLUMNS).order_by(settings_audit.c.id.desc()).limit(max(1, min(limit, 500)))
        if key is not None:
            query = query.where(settings_audit.c.key == key)
        async with self.db.read() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_audit_entry(r) for r in rows]

    async def purge_audit(self, *, older_than_days: int = 90, keep_last: int = 5000) -> int:
        """Delete audit records older than N days or beyond the newest ``keep_last``; returns the count."""
        cutoff = now() - timedelta(days=older_than_days)
        boundary = (
            sa.select(settings_audit.c.id)
            .order_by(settings_audit.c.id.desc())
            .offset(keep_last)
            .limit(1)
            .scalar_subquery()
        )
        stmt = sa.delete(settings_audit).where(
            sa.or_(settings_audit.c.ts < cutoff, settings_audit.c.id <= boundary)
        )
        async with self.db.tx() as conn:
            result = await conn.execute(stmt)
        return int(result.rowcount or 0)

    # ---- config_meta

    async def meta_get(self, key: str) -> Any:
        query = sa.select(sa.cast(config_meta.c.value, sa.Text)).where(config_meta.c.key == key)
        async with self.db.read() as conn:
            text = (await conn.execute(query)).scalar()
        return _loads(text)

    async def meta_set(self, key: str, value: Any) -> None:
        stmt = pg_insert(config_meta).values(key=key, value=_jsonb(value), updated_at=now())
        stmt = stmt.on_conflict_do_update(
            index_elements=[config_meta.c.key],
            set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
        )
        async with self.db.tx() as conn:
            await conn.execute(stmt)
