"""«Требует внимания» — deduplicated owner to-do items (04 D15, 07 §2.4.3).

Anything that needs a human (a component is down, a job is dead, a token expires) calls
:meth:`AttentionService.raise_item` with a stable ``dedup_key``. Repeated raises update the same row,
so periodic checkers may call it on every run without spamming. When the cause is gone the checker
calls :meth:`resolve` (or :meth:`auto_resolve` / :meth:`sync_health` for a whole family of keys).

Snoozing hides an item from :meth:`open_items` until the given time; a snooze survives repeated
raises of the same severity but is cancelled when the item escalates (warn → error) or re-opens.

Titles and bodies are passed through ``svbg.core.log.mask`` before storage: an exception message with
a token in it never lands in the admin UI.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.bus import Event, EventBus
from svbg.core.clock import now
from svbg.core.component import FIX_ACTION_MAX, Health, HealthReport
from svbg.core.log import mask
from svbg.core.tables import ATTENTION_SEVERITIES, attention_items

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

log = logging.getLogger("svbg.core.attention")

__all__ = [
    "AttentionItem",
    "AttentionService",
    "RaiseOutcome",
    "RaiseResult",
    "Severity",
]

Severity = Literal["info", "warn", "error"]
assert get_args(Severity) == ATTENTION_SEVERITIES

SEVERITY_RANK: dict[str, int] = {"info": 0, "warn": 1, "error": 2}
DEDUP_KEY_MAX = 200
TITLE_MAX = 200
BODY_MAX = 3500
COMPONENT_PREFIX = "component:"
EVENT_RAISED = "attention.raised"
EVENT_RESOLVED = "attention.resolved"

_KEY_RE = re.compile(r"^[^\s\x00-\x1f\x7f]+$")

# Owner-facing strings (Russian) kept in one place.
_TXT_COMPONENT_TITLE = {
    Health.DEGRADED: "«{name}» работает с ошибками",
    Health.DOWN: "«{name}» не работает",
}


class RaiseOutcome(enum.StrEnum):
    CREATED = "created"  # first time this key is seen
    REOPENED = "reopened"  # was resolved, is open again
    ESCALATED = "escalated"  # still open, severity went up
    UPDATED = "updated"  # still open, same or lower severity

    @property
    def notify(self) -> bool:
        """Whether the owner should be pinged about this raise (vs a silent refresh)."""
        return self is not RaiseOutcome.UPDATED


@dataclass(frozen=True, slots=True)
class AttentionItem:
    id: int
    dedup_key: str
    severity: Severity
    title: str
    body: str
    fix_action: str | None
    created_at: datetime
    updated_at: datetime
    snoozed_until: datetime | None
    resolved_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def is_snoozed(self, at: datetime | None = None) -> bool:
        return self.snoozed_until is not None and self.snoozed_until > (at or now())

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AttentionItem:
        return cls(
            id=row["id"],
            dedup_key=row["dedup_key"],
            severity=cast("Severity", row["severity"]),
            title=row["title"],
            body=row["body"],
            fix_action=row["fix_action"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            snoozed_until=row["snoozed_until"],
            resolved_at=row["resolved_at"],
        )


@dataclass(frozen=True, slots=True)
class RaiseResult:
    item: AttentionItem
    outcome: RaiseOutcome


def _check_key(dedup_key: str) -> str:
    if not isinstance(dedup_key, str) or not dedup_key or len(dedup_key) > DEDUP_KEY_MAX:
        raise ValueError(f"dedup_key must be 1..{DEDUP_KEY_MAX} chars")
    if not _KEY_RE.match(dedup_key):
        raise ValueError("dedup_key must not contain whitespace or control characters")
    return dedup_key


def _check_severity(severity: str) -> Severity:
    if severity not in SEVERITY_RANK:
        raise ValueError(f"severity must be one of {', '.join(ATTENTION_SEVERITIES)}")
    return cast("Severity", severity)


def _check_aware(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return value


def _clip(text: str, limit: int) -> str:
    text = mask(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rank_sql(column: sa.ColumnElement[Any]) -> sa.ColumnElement[int]:
    return sa.case(SEVERITY_RANK, value=column, else_=-1)


_T = attention_items
_ORDER = (_rank_sql(_T.c.severity).desc(), _T.c.updated_at.desc(), _T.c.id.desc())


class AttentionService:
    """Stateless facade over ``attention_items``; safe for concurrent use."""

    def __init__(self, db: Database, *, bus: EventBus | None = None) -> None:
        self._db = db
        self._bus = bus

    # -- writes --------------------------------------------------------------------------------

    async def raise_item(
        self,
        dedup_key: str,
        severity: Severity,
        title: str,
        body: str = "",
        fix_action: str | None = None,
    ) -> RaiseResult:
        """Create or refresh the item ``dedup_key`` (re-opens a resolved one)."""
        params = self._params(dedup_key, severity, title, body, fix_action)
        async with self._db.tx() as conn:
            result = await self._upsert(conn, params, now())
        await self._emit_raised(result)
        return result

    async def resolve(self, dedup_key: str) -> bool:
        """Close an open item. Returns ``False`` if there was nothing open under this key."""
        _check_key(dedup_key)
        async with self._db.tx() as conn:
            closed = await self._resolve_where(conn, _T.c.dedup_key == dedup_key, now())
        await self._emit_resolved(closed)
        return bool(closed)

    async def auto_resolve(self, prefix: str, keep: Iterable[str] = ()) -> int:
        """Resolve every open item whose key starts with ``prefix`` except those in ``keep``.

        For periodic checkers: raise what is wrong now, then ``auto_resolve(prefix, keep=raised)``.
        """
        if not prefix:
            raise ValueError("prefix must not be empty")
        cond = _T.c.dedup_key.startswith(prefix, autoescape=True)
        keep_list = sorted(set(keep))
        if keep_list:
            cond = sa.and_(cond, _T.c.dedup_key.not_in(keep_list))
        async with self._db.tx() as conn:
            closed = await self._resolve_where(conn, cond, now())
        await self._emit_resolved(closed)
        return len(closed)

    async def snooze(self, id: int, until: datetime) -> bool:
        """Hide an open item until ``until`` (aware). Returns ``False`` if not found or resolved."""
        _check_aware(until, "until")
        stmt = (
            sa.update(_T)
            .where(_T.c.id == id, _T.c.resolved_at.is_(None))
            .values(snoozed_until=until, updated_at=now())
            .returning(_T.c.id)
        )
        async with self._db.tx() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def snooze_for(self, id: int, delta: timedelta) -> bool:
        if delta <= timedelta(0):
            raise ValueError("snooze duration must be positive")
        return await self.snooze(id, now() + delta)

    async def unsnooze(self, id: int) -> bool:
        stmt = (
            sa.update(_T)
            .where(_T.c.id == id, _T.c.resolved_at.is_(None), _T.c.snoozed_until.is_not(None))
            .values(snoozed_until=None, updated_at=now())
            .returning(_T.c.id)
        )
        async with self._db.tx() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def sync_health(self, reports: Mapping[str, HealthReport]) -> list[RaiseResult]:
        """Mirror component health into items ``component:<name>`` in one transaction.

        DEGRADED → warn, DOWN → error (with the report's ``fix_action``); OK / DISABLED resolve the
        item; UNKNOWN leaves it as is (a hung probe is not evidence either way).
        """
        ts = now()
        raised: list[RaiseResult] = []
        resolved_keys: list[str] = []
        prepared: list[dict[str, Any]] = []
        for name, report in reports.items():
            key = _check_key(COMPONENT_PREFIX + name)
            if report.status.needs_attention:
                severity: Severity = "error" if report.status is Health.DOWN else "warn"
                title = _TXT_COMPONENT_TITLE[report.status].format(name=name)
                prepared.append(self._params(key, severity, title, report.summary, report.fix_action))
            elif report.status in (Health.OK, Health.DISABLED):
                resolved_keys.append(key)
        closed: list[AttentionItem] = []
        async with self._db.tx() as conn:
            for params in prepared:
                raised.append(await self._upsert(conn, params, ts))
            if resolved_keys:
                closed = await self._resolve_where(conn, _T.c.dedup_key.in_(resolved_keys), ts)
        for result in raised:
            await self._emit_raised(result)
        await self._emit_resolved(closed)
        return raised

    async def purge_resolved(self, older_than_days: int = 30) -> int:
        """Delete items resolved more than ``older_than_days`` ago."""
        if older_than_days < 0:
            raise ValueError("older_than_days must be >= 0")
        cutoff = now() - timedelta(days=older_than_days)
        stmt = sa.delete(_T).where(_T.c.resolved_at.is_not(None), _T.c.resolved_at < cutoff)
        async with self._db.tx() as conn:
            return (await conn.execute(stmt)).rowcount or 0

    # -- reads ---------------------------------------------------------------------------------

    async def open_items(self, *, include_snoozed: bool = False, limit: int = 100) -> list[AttentionItem]:
        """Open items, worst first (severity, then most recently updated)."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        q = sa.select(_T).where(_T.c.resolved_at.is_(None))
        if not include_snoozed:
            q = q.where(sa.or_(_T.c.snoozed_until.is_(None), _T.c.snoozed_until <= now()))
        q = q.order_by(*_ORDER).limit(limit)
        async with self._db.read() as conn:
            rows = (await conn.execute(q)).mappings().all()
        return [AttentionItem.from_row(r) for r in rows]

    async def open_counts(self) -> dict[Severity, int]:
        """Counts of visible (open, not snoozed) items per severity — for a badge on the admin home."""
        q = (
            sa.select(_T.c.severity, sa.func.count())
            .where(
                _T.c.resolved_at.is_(None),
                sa.or_(_T.c.snoozed_until.is_(None), _T.c.snoozed_until <= now()),
            )
            .group_by(_T.c.severity)
        )
        counts: dict[Severity, int] = {"info": 0, "warn": 0, "error": 0}
        async with self._db.read() as conn:
            for severity, count in (await conn.execute(q)).all():
                counts[cast("Severity", severity)] = int(count)
        return counts

    async def get(self, dedup_key: str) -> AttentionItem | None:
        _check_key(dedup_key)
        async with self._db.read() as conn:
            row = (await conn.execute(sa.select(_T).where(_T.c.dedup_key == dedup_key))).mappings().first()
        return AttentionItem.from_row(row) if row is not None else None

    async def get_by_id(self, id: int) -> AttentionItem | None:
        async with self._db.read() as conn:
            row = (await conn.execute(sa.select(_T).where(_T.c.id == id))).mappings().first()
        return AttentionItem.from_row(row) if row is not None else None

    # -- internals -----------------------------------------------------------------------------

    @staticmethod
    def _params(
        dedup_key: str, severity: str, title: str, body: str, fix_action: str | None
    ) -> dict[str, Any]:
        _check_key(dedup_key)
        sev = _check_severity(severity)
        clean_title = _clip(title.strip(), TITLE_MAX)
        if not clean_title:
            raise ValueError("title must not be empty")
        if fix_action is not None and (
            not fix_action or len(fix_action) > FIX_ACTION_MAX or any(c.isspace() for c in fix_action)
        ):
            raise ValueError(f"fix_action must be 1..{FIX_ACTION_MAX} chars without whitespace")
        return {
            "dedup_key": dedup_key,
            "severity": sev,
            "title": clean_title,
            "body": _clip(body, BODY_MAX),
            "fix_action": fix_action,
        }

    @staticmethod
    async def _upsert(conn: AsyncConnection, params: Mapping[str, Any], ts: datetime) -> RaiseResult:
        # Lock the existing row (if any) to learn the previous state for the outcome; the upsert itself
        # is race-free thanks to ON CONFLICT, and ``xmax = 0`` tells a real insert apart.
        prev = (
            await conn.execute(
                sa.select(_T.c.severity, _T.c.resolved_at)
                .where(_T.c.dedup_key == params["dedup_key"])
                .with_for_update()
            )
        ).first()
        ins = pg_insert(_T).values(**params, created_at=ts, updated_at=ts)
        new = ins.excluded
        reopening = _T.c.resolved_at.is_not(None)
        escalating = _rank_sql(new.severity) > _rank_sql(_T.c.severity)
        stmt = ins.on_conflict_do_update(
            index_elements=[_T.c.dedup_key],
            set_={
                "severity": new.severity,
                "title": new.title,
                "body": new.body,
                "fix_action": new.fix_action,
                "updated_at": new.updated_at,
                "created_at": sa.case((reopening, new.created_at), else_=_T.c.created_at),
                "snoozed_until": sa.case((reopening | escalating, sa.null()), else_=_T.c.snoozed_until),
                "resolved_at": sa.null(),
            },
        ).returning(*_T.c, (sa.literal_column("xmax") == 0).label("inserted"))
        row = (await conn.execute(stmt)).mappings().one()
        item = AttentionItem.from_row(row)
        if row["inserted"]:
            outcome = RaiseOutcome.CREATED
        elif prev is not None and prev.resolved_at is not None:
            outcome = RaiseOutcome.REOPENED
        elif prev is not None and SEVERITY_RANK[item.severity] > SEVERITY_RANK.get(prev.severity, -1):
            outcome = RaiseOutcome.ESCALATED
        else:
            outcome = RaiseOutcome.UPDATED
        return RaiseResult(item, outcome)

    @staticmethod
    async def _resolve_where(
        conn: AsyncConnection, cond: sa.ColumnElement[bool], ts: datetime
    ) -> list[AttentionItem]:
        stmt = (
            sa.update(_T)
            .where(cond, _T.c.resolved_at.is_(None))
            .values(resolved_at=ts, updated_at=ts, snoozed_until=None)
            .returning(*_T.c)
        )
        rows = (await conn.execute(stmt)).mappings().all()
        return [AttentionItem.from_row(r) for r in rows]

    async def _emit_raised(self, result: RaiseResult) -> None:
        if self._bus is None or not result.outcome.notify:
            return
        item = result.item
        await self._bus.publish(
            Event(
                EVENT_RAISED,
                {
                    "id": item.id,
                    "dedup_key": item.dedup_key,
                    "severity": item.severity,
                    "outcome": result.outcome.value,
                },
            )
        )

    async def _emit_resolved(self, items: Sequence[AttentionItem]) -> None:
        if self._bus is None:
            return
        for item in items:
            await self._bus.publish(Event(EVENT_RESOLVED, {"id": item.id, "dedup_key": item.dedup_key}))
