"""Broadcast rows: reading, draft edits, status transitions and the audience query (no Telegram here).

Every draft edit is a single conditional ``UPDATE … WHERE status = 'draft'`` (a draft already started by
another admin is never changed). Transitions are conditional too, so a double click or two admins at once
cannot start or resume a broadcast twice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.broadcasts.segments import DEFAULT_SEGMENT, MARKETING_COLUMN, recipients_where, validate_segment
from svbg.broadcasts.tables import broadcasts
from svbg.core import clock
from svbg.core.tables import users

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "DELETE_AFTER_CHOICES",
    "MARKETING_RECHECK",
    "Audience",
    "AudienceConfig",
    "Broadcast",
    "BroadcastRepo",
    "Recipient",
]

log = logging.getLogger("svbg.broadcasts")

#: «Удалить через N ч»: bots may delete their messages in private chats only within 48 hours.
DELETE_AFTER_CHOICES: Final[tuple[int | None, ...]] = (None, 1, 6, 12, 24, 47)
MAX_SKIP: Final = 1000
#: Seconds between checks for a missing ``users.notify_marketing`` column.
MARKETING_RECHECK: Final = 60.0


@dataclass(frozen=True, slots=True)
class Broadcast:
    id: int
    status: str
    created_by: int | None
    source_chat_id: int | None
    source_msg_id: int | None
    content: Mapping[str, Any]
    buttons: Sequence[Mapping[str, Any]]
    segment: Mapping[str, Any]
    options: Mapping[str, Any]
    cursor: int
    skip: Sequence[int]
    total: int
    sent: int
    failed: int
    blocked: int
    progress_msg: Mapping[str, Any] | None
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Broadcast:
        def obj(v: Any) -> Mapping[str, Any]:
            return v if isinstance(v, Mapping) else {}

        def arr(v: Any) -> list[Any]:
            return list(v) if isinstance(v, list) else []

        return cls(
            id=int(row["id"]),
            status=str(row["status"]),
            created_by=row["created_by"],
            source_chat_id=row["source_chat_id"],
            source_msg_id=row["source_msg_id"],
            content=obj(row["content"]),
            buttons=[b for b in arr(row["buttons"]) if isinstance(b, Mapping)],
            segment=obj(row["segment"]) or DEFAULT_SEGMENT,
            options=obj(row["options"]),
            cursor=int(row["cursor"]),
            skip=[int(x) for x in arr(row["skip"]) if isinstance(x, int) and not isinstance(x, bool)],
            total=int(row["total"]),
            sent=int(row["sent"]),
            failed=int(row["failed"]),
            blocked=int(row["blocked"]),
            progress_msg=row["progress_msg"] if isinstance(row["progress_msg"], Mapping) else None,
            last_error=row["last_error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.blocked

    @property
    def pin(self) -> bool:
        return bool(self.options.get("pin"))

    @property
    def silent(self) -> bool:
        return bool(self.options.get("silent"))

    @property
    def delete_after_h(self) -> int | None:
        value = self.options.get("delete_after_h")
        return value if isinstance(value, int) and value in DELETE_AFTER_CHOICES else None

    @property
    def can_copy(self) -> bool:
        return self.source_chat_id is not None and self.source_msg_id is not None


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: int
    telegram_id: int
    lang: str


@dataclass(frozen=True, slots=True)
class AudienceConfig:
    default_lang: str = "ru"  # accepted for old callers; the bot is Russian-only
    channel_id: int | None = None
    langs: tuple[str, ...] = ("ru",)  # accepted for old callers; ignored


class Audience:
    """Builds the recipients query and checks whether ``users.notify_marketing`` exists.

    The positive answer is cached for the life of the process (columns are never dropped). A missing column
    is re-checked every :data:`MARKETING_RECHECK` seconds, so the opt-out applies as soon as the stage-3
    migration adds it, without a restart. Until then nobody can have opted out (the toggle has nowhere to be
    stored), so the broadcast goes to the whole segment — logged once.
    """

    def __init__(
        self,
        config: Callable[[], AudienceConfig] = AudienceConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._monotonic = monotonic
        self._marketing = False
        self._recheck_at: float | None = None  # None: never checked
        self._warned = False

    @property
    def config(self) -> AudienceConfig:
        return self._config()

    async def marketing(self, conn: AsyncConnection) -> bool:
        if self._marketing:
            return True
        now = self._monotonic()
        if self._recheck_at is not None and now < self._recheck_at:
            return False
        found = bool(
            await conn.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = :col AND table_schema = current_schema())"
                ),
                {"col": MARKETING_COLUMN},
            )
        )
        self._marketing = found
        self._recheck_at = None if found else now + MARKETING_RECHECK
        if not found and not self._warned:
            self._warned = True
            log.warning("users.%s is missing: the marketing opt-out cannot be applied yet", MARKETING_COLUMN)
        return found

    def where(self, segment: Mapping[str, Any], *, marketing: bool, at: datetime | None = None) -> Any:
        cfg = self.config
        return recipients_where(
            segment,
            at=at or clock.now(),
            marketing=marketing,
            channel_id=cfg.channel_id,
        )

    def lang_expr(self) -> Any:
        """Every recipient reads Russian (the bot has no other language)."""
        return sa.literal("ru")

    async def count_expr(self, conn: AsyncConnection, segment: Mapping[str, Any]) -> Any:
        """``(SELECT count(*) …)`` as a scalar subquery, to embed into an ``INSERT``/``UPDATE``."""
        marketing = await self.marketing(conn)
        return (
            sa.select(sa.func.count())
            .select_from(users)
            .where(self.where(segment, marketing=marketing))
            .scalar_subquery()
        )

    async def count(self, conn: AsyncConnection, segment: Mapping[str, Any]) -> int:
        return int(await conn.scalar(sa.select(await self.count_expr(conn, segment))) or 0)

    async def batch(
        self,
        conn: AsyncConnection,
        segment: Mapping[str, Any],
        *,
        after: int,
        skip: Sequence[int],
        limit: int,
    ) -> list[Recipient]:
        marketing = await self.marketing(conn)
        cond = [self.where(segment, marketing=marketing), users.c.id > after]
        if skip:
            cond.append(users.c.id.not_in(list(skip)))
        stmt = (
            sa.select(users.c.id, users.c.telegram_id, self.lang_expr().label("lang"))
            .where(*cond)
            .order_by(users.c.id)
            .limit(limit)
        )
        rows = (await conn.execute(stmt)).all()
        return [Recipient(int(r.id), int(r.telegram_id), str(r.lang)) for r in rows]


class BroadcastRepo:
    def __init__(self, db: Database, audience: Audience | None = None) -> None:
        self.db = db
        self.audience = audience or Audience()

    # ------------------------------------------------------------ reading

    async def get(self, bid: int) -> Broadcast | None:
        async with self.db.read() as conn:
            row = (await conn.execute(sa.select(broadcasts).where(broadcasts.c.id == bid))).mappings().first()
        return None if row is None else Broadcast.from_row(row)

    async def recent(self, limit: int = 10) -> list[Broadcast]:
        stmt = sa.select(broadcasts).order_by(broadcasts.c.id.desc()).limit(limit)
        async with self.db.read() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [Broadcast.from_row(r) for r in rows]

    async def count(self, segment: Mapping[str, Any]) -> int:
        async with self.db.read() as conn:
            return await self.audience.count(conn, segment)

    # ------------------------------------------------------------ drafts

    async def create(
        self,
        *,
        actor_id: int | None,
        source_chat_id: int | None,
        source_msg_id: int | None,
        content: Mapping[str, Any],
    ) -> int:
        """A new draft for everybody (its recipient count computed in the same statement)."""
        async with self.db.tx() as conn:
            total = await self.audience.count_expr(conn, DEFAULT_SEGMENT)
            bid = await conn.scalar(
                sa.insert(broadcasts)
                .values(
                    created_by=actor_id,
                    source_chat_id=source_chat_id,
                    source_msg_id=source_msg_id,
                    content=dict(content),
                    segment=dict(DEFAULT_SEGMENT),
                    total=total,
                )
                .returning(broadcasts.c.id)
            )
        return int(bid)

    async def update_draft(self, bid: int, **values: Any) -> Broadcast | None:
        """Change a draft in one statement; ``None`` when it is gone or no longer a draft."""
        stmt = (
            sa.update(broadcasts)
            .where(broadcasts.c.id == bid, broadcasts.c.status == "draft")
            .values(updated_at=sa.func.now(), **values)
            .returning(*broadcasts.c)
        )
        async with self.db.tx() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        return None if row is None else Broadcast.from_row(row)

    async def set_segment(self, bid: int, segment: Mapping[str, Any]) -> Broadcast | None:
        """Store a validated segment and its recipient count (one ``UPDATE`` with the count as a subquery);
        ``None`` when the draft is gone or already started."""
        cfg = self.audience.config
        seg = validate_segment(segment, channel_id=cfg.channel_id)
        async with self.db.tx() as conn:
            total = await self.audience.count_expr(conn, seg)
            row = (
                (
                    await conn.execute(
                        sa.update(broadcasts)
                        .where(broadcasts.c.id == bid, broadcasts.c.status == "draft")
                        .values(segment=seg, total=total, updated_at=sa.func.now())
                        .returning(*broadcasts.c)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else Broadcast.from_row(row)

    async def delete_draft(self, bid: int) -> bool:
        stmt = (
            sa.delete(broadcasts)
            .where(broadcasts.c.id == bid, broadcasts.c.status == "draft")
            .returning(broadcasts.c.id)
        )
        async with self.db.tx() as conn:
            return (await conn.scalar(stmt)) is not None

    async def clone(self, bid: int, actor_id: int | None) -> int | None:
        """A new draft with the same message, buttons, segment and options."""
        src = sa.select(
            sa.literal("draft").label("status"),
            sa.literal(actor_id, sa.BigInteger).label("created_by"),
            broadcasts.c.source_chat_id,
            broadcasts.c.source_msg_id,
            broadcasts.c.content,
            broadcasts.c.buttons,
            broadcasts.c.segment,
            broadcasts.c.options,
            broadcasts.c.total,
        ).where(broadcasts.c.id == bid)
        stmt = (
            sa.insert(broadcasts)
            .from_select(
                [
                    "status",
                    "created_by",
                    "source_chat_id",
                    "source_msg_id",
                    "content",
                    "buttons",
                    "segment",
                    "options",
                    "total",
                ],
                src,
            )
            .returning(broadcasts.c.id)
        )
        async with self.db.tx() as conn:
            new_id = await conn.scalar(stmt)
        return None if new_id is None else int(new_id)

    # ------------------------------------------------------------ run state

    @staticmethod
    async def transition(
        conn: AsyncConnection, bid: int, from_: Sequence[str], to: str, **values: Any
    ) -> Broadcast | None:
        stmt = (
            sa.update(broadcasts)
            .where(broadcasts.c.id == bid, broadcasts.c.status.in_(list(from_)))
            .values(status=to, updated_at=sa.func.now(), **values)
            .returning(*broadcasts.c)
        )
        row = (await conn.execute(stmt)).mappings().first()
        return None if row is None else Broadcast.from_row(row)

    @staticmethod
    async def save_progress(
        conn: AsyncConnection,
        bid: int,
        *,
        cursor: int,
        skip: Sequence[int],
        sent: int,
        failed: int,
        blocked: int,
    ) -> Broadcast | None:
        """Advance the cursor and counters (whatever the status: a stop must not lose what was sent)."""
        stmt = (
            sa.update(broadcasts)
            .where(broadcasts.c.id == bid)
            .values(
                cursor=sa.func.greatest(broadcasts.c.cursor, cursor),
                skip=sorted(set(skip))[:MAX_SKIP],
                sent=broadcasts.c.sent + sent,
                failed=broadcasts.c.failed + failed,
                blocked=broadcasts.c.blocked + blocked,
                updated_at=sa.func.now(),
            )
            .returning(*broadcasts.c)
        )
        row = (await conn.execute(stmt)).mappings().first()
        return None if row is None else Broadcast.from_row(row)
