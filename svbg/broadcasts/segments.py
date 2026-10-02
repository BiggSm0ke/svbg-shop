"""Who receives a broadcast: presets + the visibility DSL compiled to SQL (07 §2.4.5).

A segment is JSON ``{"preset": "<code>", "dsl": {...} | null}``; the recipients are
:func:`base_filter` (has a Telegram id, not banned, has not blocked the bot, has not turned off
«акции и новости» = ``users.notify_marketing``) AND the preset AND the optional DSL
(:func:`svbg.tg.ui.conditions.to_sql`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa

from svbg.core.tables import users
from svbg.db.meta import UtcDateTime
from svbg.subscriptions.tables import subscriptions
from svbg.tg.ui.conditions import ConditionError, to_sql

__all__ = [
    "DEFAULT_SEGMENT",
    "MARKETING_COLUMN",
    "PRESETS",
    "Preset",
    "SegmentError",
    "base_filter",
    "recipients_where",
    "validate_segment",
]

MARKETING_COLUMN: Final = "notify_marketing"
DEFAULT_SEGMENT: Final[Mapping[str, Any]] = {"preset": "all"}


class SegmentError(ValueError):
    """Invalid segment; ``str(error)`` is shown to the admin."""


@dataclass(frozen=True, slots=True)
class Preset:
    code: str
    title: str
    dsl: Mapping[str, Any] | None = None
    #: Days range for «истекла N–M дней назад» (not expressible in the DSL).
    expired_days: tuple[int, int] | None = None


PRESETS: Final[Mapping[str, Preset]] = {
    p.code: p
    for p in (
        Preset("all", "Все"),
        Preset("active", "С активной подпиской", {"sub": "active"}),
        Preset("trial", "Триал без оплаты", {"sub": "trial", "has_paid": False}),
        Preset("expired30", "Подписка истекла 1–30 дней назад", expired_days=(1, 30)),
        Preset("nosub", "Без подписки", {"sub": "none"}),
        Preset("never_paid", "Никогда не платили", {"has_paid": False}),
        Preset("balance", "Баланс больше нуля", {"balance_minor": {"gt": 0}}),
        Preset("lang_ru", "Язык: русский", {"lang": "ru"}),
        Preset("lang_en", "Язык: английский", {"lang": "en"}),
    )
}


def validate_segment(raw: Any, **sql_kw: Any) -> dict[str, Any]:
    """Normalized segment or :class:`SegmentError` (the DSL is compiled to SQL to catch unsupported atoms)."""
    if not isinstance(raw, Mapping):
        raise SegmentError("Сегмент повреждён")
    preset = raw.get("preset", "all")
    if preset not in PRESETS:
        raise SegmentError(f"Неизвестный пресет «{preset}»")
    out: dict[str, Any] = {"preset": preset}
    dsl = raw.get("dsl")
    if dsl:
        if not isinstance(dsl, Mapping):
            raise SegmentError("Условие должно быть JSON-объектом")
        try:
            to_sql(dsl, **sql_kw)
        except ConditionError as e:
            raise SegmentError(f"Условие не подходит: {e}") from None
        out["dsl"] = dict(dsl)
    return out


def base_filter(*, marketing: bool = True) -> sa.ColumnElement[bool]:
    """Users a broadcast may reach at all. ``marketing=False`` when the column is not migrated yet."""
    parts: list[sa.ColumnElement[bool]] = [
        users.c.telegram_id.is_not(None),
        users.c.telegram_id > 0,
        users.c.banned_at.is_(None),
        users.c.bot_blocked_at.is_(None),
    ]
    if marketing:
        parts.append(sa.literal_column(f"users.{MARKETING_COLUMN}", sa.Boolean).is_(True))
    return sa.and_(*parts)


def _expired_between(at: datetime, days: tuple[int, int]) -> sa.ColumnElement[bool]:
    """The current subscription expired between ``days[1]`` and ``days[0]`` days ago (not frozen)."""
    cur = subscriptions.alias("cur")
    live_first = sa.case((cur.c.link_state.in_(("pending", "linked")), 0), else_=1)
    current_id = (
        sa.select(cur.c.id)
        .where(cur.c.user_id == users.c.id, cur.c.link_state != "closed")
        .order_by(live_first, cur.c.id.desc())
        .limit(1)
        .correlate(users)
        .scalar_subquery()
    )
    lo = sa.literal(at - timedelta(days=days[1]), UtcDateTime)
    hi = sa.literal(at - timedelta(days=days[0]), UtcDateTime)
    return (
        sa.select(sa.literal(1))
        .select_from(subscriptions)
        .where(
            subscriptions.c.id == current_id,
            subscriptions.c.hold_kind.is_(None),
            subscriptions.c.paid_until > lo,
            subscriptions.c.paid_until <= hi,
        )
        .exists()
    )


def recipients_where(
    segment: Mapping[str, Any],
    *,
    at: datetime,
    marketing: bool = True,
    default_lang: str = "ru",
    channel_id: int | None = None,
) -> sa.ColumnElement[bool]:
    """WHERE clause over ``users`` for ``segment`` (validated again: stored JSON is not trusted)."""
    kw: dict[str, Any] = {"at": at, "default_lang": default_lang, "channel_id": channel_id}
    seg = validate_segment(segment, **kw)
    preset = PRESETS[seg["preset"]]
    parts = [base_filter(marketing=marketing)]
    if preset.dsl is not None:
        parts.append(to_sql(preset.dsl, **kw))
    if preset.expired_days is not None:
        parts.append(_expired_between(at, preset.expired_days))
    if "dsl" in seg:
        parts.append(to_sql(seg["dsl"], **kw))
    return sa.and_(*parts)
