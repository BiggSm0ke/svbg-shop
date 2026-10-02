"""Smart user search for the admin card (04 §9, 01 §1.6): one SQL per query.

What the admin may type (one line, ≤ 128 characters):

* a number — ``users.id``, Telegram id or the panel user id (``#123`` forces the bot id);
* ``@username`` or a part of a name / username — exact match first, then ``pg_trgm`` similarity (typos are
  forgiven) and substring;
* a panel username (``sv_123456``) or a ``shortUuid``, or a whole subscription link — its last path segment is
  the ``shortUuid``;
* a payment id (UUID) or a user's / subscription's ``public_id``.

Every branch is one ``SELECT user_id, score`` of a ``UNION ALL``; the best score per user wins, the result is
capped. Fuzzy branches use the trigram GIN indexes of :data:`INDEX_DDL` (the integration step adds them to the
migration; the queries are correct without them, only slower).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import sqlalchemy as sa

from svbg.core.tables import users
from svbg.payments.tables import payments
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "INDEX_DDL",
    "MAX_QUERY",
    "MIN_QUERY",
    "Found",
    "Query",
    "QueryError",
    "ensure_indexes",
    "parse_query",
    "search",
]

MIN_QUERY: Final = 2
MAX_QUERY: Final = 128
DEFAULT_LIMIT: Final = 10
_BIGINT_MAX: Final = 2**63 - 1
_UUID_RE: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")  # panel username / shortUuid
_NUM_RE: Final = re.compile(r"^#?\d{1,19}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")

#: Trigram / helper indexes the search and the dashboard rely on (``CREATE INDEX IF NOT EXISTS``; idempotent).
INDEX_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS ix_users_username_trgm ON users USING gin (lower(username) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_users_first_name_trgm ON users USING gin (lower(first_name) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_subscriptions_panel_username_trgm ON subscriptions "
    "USING gin (lower(panel_username) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_subscriptions_short_uuid_trgm ON subscriptions "
    "USING gin (lower(panel_short_uuid) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_payments_paid_at ON payments (paid_at) WHERE status = 'paid'",
)


class QueryError(ValueError):
    """The text cannot be searched; ``str(error)`` is shown to the admin."""


@dataclass(frozen=True, slots=True)
class Query:
    raw: str  # cleaned text (a link is reduced to its last path segment)
    low: str  # lower-case text for fuzzy matching
    number: int | None = None  # digits → ids
    bot_id_only: bool = False  # ``#123``: only ``users.id``
    username: str | None = None  # ``@name`` → exact username first
    uuid: str | None = None  # payment id / public ids
    token: str | None = None  # panel username or shortUuid


@dataclass(frozen=True, slots=True)
class Found:
    user_id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None
    role: str
    banned_at: datetime | None
    score: float


def parse_query(text: str) -> Query:
    """Normalise the admin's input; :class:`QueryError` for empty / too short / too long text."""
    value = _CONTROL_RE.sub(" ", text or "").strip()
    if len(value) > MAX_QUERY:
        raise QueryError(f"Слишком длинно: максимум {MAX_QUERY} символов")
    if "://" in value or value.startswith(("t.me/", "www.")):
        parts = urlsplit(value if "://" in value else f"https://{value}")
        segments = [s for s in parts.path.split("/") if s]
        if segments:
            value = segments[-1]
    value = " ".join(value.split())
    if len(value) < MIN_QUERY and not value.isdigit():
        raise QueryError("Слишком коротко: хотя бы 2 символа")
    number: int | None = None
    if _NUM_RE.match(value):
        n = int(value.lstrip("#"))
        if 0 < n <= _BIGINT_MAX:
            number = n
    username = value[1:] if value.startswith("@") and len(value) > 1 else None
    plain = username or value
    return Query(
        raw=plain,
        low=plain.lower(),
        number=number,
        bot_id_only=value.startswith("#"),
        username=username,
        uuid=value.lower() if _UUID_RE.match(value) else None,
        token=value if _TOKEN_RE.match(value) and number is None else None,
    )


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _case_variants(text: str) -> set[str]:
    return {text, text.lower(), text.upper(), text.capitalize(), text.title()}


def _branches(q: Query) -> list[sa.Select[tuple[int, float]]]:
    u, s = users, subscriptions
    out: list[sa.Select[tuple[int, float]]] = []

    def pick(
        user_col: sa.ColumnElement[int], score: float | sa.ColumnElement[float]
    ) -> list[sa.ColumnElement]:
        lit = sa.literal(score, sa.Float) if isinstance(score, int | float) else score
        return [user_col.label("user_id"), sa.cast(lit, sa.Float).label("score")]

    if q.number is not None:
        if q.bot_id_only:
            out.append(sa.select(*pick(u.c.id, 100.0)).where(u.c.id == q.number))
        else:
            out.append(
                sa.select(*pick(u.c.id, 100.0)).where(sa.or_(u.c.id == q.number, u.c.telegram_id == q.number))
            )
            out.append(
                sa.select(*pick(s.c.user_id, 90.0)).where(
                    s.c.user_id.is_not(None), s.c.panel_user_id == q.number
                )
            )
        return out
    if q.uuid is not None:
        out.append(sa.select(*pick(payments.c.user_id, 95.0)).where(payments.c.id == q.uuid))
        out.append(sa.select(*pick(u.c.id, 95.0)).where(u.c.public_id == q.uuid))
        out.append(
            sa.select(*pick(s.c.user_id, 95.0)).where(s.c.user_id.is_not(None), s.c.public_id == q.uuid)
        )
    if q.token is not None:
        out.append(
            sa.select(*pick(s.c.user_id, 90.0)).where(
                s.c.user_id.is_not(None),
                sa.or_(s.c.panel_short_uuid == q.token, sa.func.lower(s.c.panel_username) == q.token.lower()),
            )
        )
    uname = sa.func.lower(u.c.username)
    fname = sa.func.lower(u.c.first_name)
    out.append(sa.select(*pick(u.c.id, 80.0)).where(uname == (q.username or q.raw).lower()))
    pattern = _like(q.low)
    sim = sa.func.greatest(
        sa.func.coalesce(sa.func.similarity(uname, q.low), 0.0),
        sa.func.coalesce(sa.func.similarity(fname, q.low), 0.0),
    )
    # ``lower()`` and pg_trgm only fold/split non-ASCII letters in a UTF-8 locale database; the case variants
    # of the query keep Cyrillic names findable in a ``C``-locale database too (substring match, no typos).
    variants = sorted({_like(v) for v in _case_variants(q.raw)} - {pattern})
    raw_like = [col.like(v, escape="\\") for v in variants for col in (u.c.username, u.c.first_name)]
    out.append(
        sa.select(*pick(u.c.id, 10.0 + 50.0 * sim)).where(
            sa.or_(
                uname.like(pattern, escape="\\"),
                fname.like(pattern, escape="\\"),
                uname.op("%")(q.low),
                fname.op("%")(q.low),
                *raw_like,
            )
        )
    )
    if q.username is None:
        pname = sa.func.lower(s.c.panel_username)
        short = sa.func.lower(s.c.panel_short_uuid)
        psim = sa.func.coalesce(sa.func.similarity(pname, q.low), 0.0)
        out.append(
            sa.select(*pick(s.c.user_id, 10.0 + 40.0 * psim)).where(
                s.c.user_id.is_not(None),
                sa.or_(pname.like(pattern, escape="\\"), short.like(pattern, escape="\\")),
            )
        )
    return out


async def search(conn: AsyncConnection, q: Query, *, limit: int = DEFAULT_LIMIT) -> list[Found]:
    """Matching users, best first (one SQL)."""
    hits = sa.union_all(*_branches(q)).subquery("hits")
    best = sa.func.max(hits.c.score).label("score")
    stmt = (
        sa.select(
            users.c.id,
            users.c.telegram_id,
            users.c.username,
            users.c.first_name,
            users.c.role,
            users.c.banned_at,
            best,
        )
        .select_from(hits.join(users, users.c.id == hits.c.user_id))
        .group_by(users.c.id)
        .order_by(best.desc(), users.c.id.desc())
        .limit(max(1, min(limit, 50)))
    )
    rows = (await conn.execute(stmt)).all()
    return [
        Found(
            int(r.id),
            int(r.telegram_id) if r.telegram_id is not None else None,
            r.username,
            r.first_name,
            str(r.role),
            r.banned_at,
            float(r.score),
        )
        for r in rows
    ]


async def ensure_indexes(conn: AsyncConnection) -> None:
    """Create :data:`INDEX_DDL` (tests and installs without the stage-3 migration)."""
    await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for ddl in INDEX_DDL:
        await conn.exec_driver_sql(ddl)
