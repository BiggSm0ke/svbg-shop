"""``subscription_events`` (05 §3.2 X2): every change of the term, the trial flag, the plan or the hold.

Written in the transaction of the change itself. A reference (``ref_type``/``ref_id``, e.g. ``order``/<id>)
makes the row unique per ``(subscription, kind, ref)``: a retried fulfill finds its own row and does nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.subscriptions.tables import EVENT_REF_PREDICATE, subscription_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["Recorded", "find_by_ref", "record"]


@dataclass(frozen=True, slots=True)
class Recorded:
    subscription_id: int
    kind: str
    old_expire: datetime | None
    new_expire: datetime | None
    details: Mapping[str, Any]


async def record(
    conn: AsyncConnection,
    subscription_id: int,
    kind: str,
    *,
    source: str,
    old_expire: datetime | None = None,
    new_expire: datetime | None = None,
    delta_seconds: int | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> bool:
    """Insert one event; ``False`` when the same ``(subscription, kind, ref)`` is already recorded."""
    if (ref_type is None) != (ref_id is None):
        raise ValueError("ref_type and ref_id go together")
    if delta_seconds is None and old_expire is not None and new_expire is not None:
        delta_seconds = int((new_expire - old_expire).total_seconds())
    stmt = pg_insert(subscription_events).values(
        subscription_id=subscription_id,
        kind=kind,
        source=source,
        old_expire=old_expire,
        new_expire=new_expire,
        delta_seconds=delta_seconds,
        ref_type=ref_type,
        ref_id=ref_id,
        details=dict(details or {}),
    )
    if ref_id is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["subscription_id", "kind", "ref_type", "ref_id"],
            index_where=sa.text(EVENT_REF_PREDICATE),
        )
    row = (await conn.execute(stmt.returning(subscription_events.c.id))).first()
    return row is not None


async def find_by_ref(
    conn: AsyncConnection, ref_type: str, ref_id: str, kinds: Iterable[str]
) -> Recorded | None:
    """The event an earlier run of the same operation (``ref``) left behind, if any."""
    row = (
        (
            await conn.execute(
                sa.select(
                    subscription_events.c.subscription_id,
                    subscription_events.c.kind,
                    subscription_events.c.old_expire,
                    subscription_events.c.new_expire,
                    subscription_events.c.details,
                )
                .where(
                    subscription_events.c.ref_type == ref_type,
                    subscription_events.c.ref_id == ref_id,
                    subscription_events.c.kind.in_(list(kinds)),
                )
                .order_by(subscription_events.c.id)
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Recorded(
        subscription_id=int(row["subscription_id"]),
        kind=str(row["kind"]),
        old_expire=row["old_expire"],
        new_expire=row["new_expire"],
        details=dict(row["details"] or {}),
    )
