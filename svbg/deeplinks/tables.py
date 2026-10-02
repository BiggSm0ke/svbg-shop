"""Deep-link tables (07 §2.5 «Диплинки»): short links, hits from day one, daily aggregates.

``promo_id`` / ``ad_link_id`` are plain ids without foreign keys: ``promocodes`` and ``ad_links`` belong to
the promo/ads modules (another table module); the integration step may add the FKs once both exist. The
intent itself references the promo and the ad tag by *code* (what links and admins use).

Links are never deleted (only disabled), so the history in ``deeplink_hits`` is not lost; the FK is
``SET NULL`` anyway and every hit keeps its raw ``payload``.
"""

from __future__ import annotations

import sqlalchemy as sa

import svbg.core.tables  # noqa: F401 - ``users`` must be on the metadata for the foreign keys
from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["HIT_KINDS", "deeplink_daily", "deeplink_hits", "deeplinks"]

HIT_KINDS: tuple[str, ...] = (
    "screen",
    "plan",
    "promo",
    "topup",
    "ref",
    "ad",
    "link",
    "legacy_ref",
    "ad_code",  # a bare payload equal to ``ad_links.code`` (old Bedolaga campaign links)
)

deeplinks = sa.Table(
    "deeplinks",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("intent", JSONB, nullable=False),
    sa.Column("promo_id", sa.BigInteger, nullable=True),
    sa.Column("ad_link_id", sa.BigInteger, nullable=True),
    sa.Column("expires_at", UtcDateTime, nullable=True),
    sa.Column("max_uses", sa.Integer, nullable=True),
    sa.Column("uses", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("code ~ '^[A-Za-z0-9_-]{1,62}$'", name="code_format"),
    sa.CheckConstraint("length(btrim(title)) BETWEEN 1 AND 64", name="title_len"),
    sa.CheckConstraint("jsonb_typeof(intent) = 'object'", name="intent_object"),
    sa.CheckConstraint("max_uses IS NULL OR max_uses > 0", name="max_uses_positive"),
    sa.CheckConstraint("uses >= 0", name="uses_non_negative"),
    sa.Index("ix_deeplinks_created_at", "created_at"),
)

# One row per tap of a deep link (``/start <payload>``), written from the first day (07 §2.4.4).
deeplink_hits = sa.Table(
    "deeplink_hits",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("link_id", sa.BigInteger, sa.ForeignKey("deeplinks.id", ondelete="SET NULL"), nullable=True),
    sa.Column("payload", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("is_new", sa.Boolean, nullable=False),
    sa.Column("ts", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("length(payload) BETWEEN 1 AND 64", name="payload_len"),
    sa.CheckConstraint(
        "kind IN (" + ", ".join(f"'{k}'" for k in HIT_KINDS) + ")",
        name="kind",
    ),
    sa.Index("ix_deeplink_hits_link_id_ts", "link_id", "ts"),
    sa.Index("ix_deeplink_hits_link_id_user_id", "link_id", "user_id"),
    sa.Index("ix_deeplink_hits_ts", "ts"),
    sa.Index("ix_deeplink_hits_user_id", "user_id"),
)

# Daily aggregates (stats screens and the funnel come in stage 6; the table is filled from now on).
# ``link_key`` = ``l:<deeplinks.id>`` for short links, else the raw payload (direct links).
deeplink_daily = sa.Table(
    "deeplink_daily",
    metadata,
    sa.Column("day", sa.Date, nullable=False),
    sa.Column("link_key", sa.Text, nullable=False),
    sa.Column("link_id", sa.BigInteger, nullable=True),
    sa.Column("hits", sa.Integer, nullable=False),
    sa.Column("users", sa.Integer, nullable=False),
    sa.Column("new_users", sa.Integer, nullable=False),
    sa.PrimaryKeyConstraint("day", "link_key", name="pk_deeplink_daily"),
    sa.CheckConstraint("hits >= users AND users >= new_users AND new_users >= 0", name="counts"),
    sa.Index("ix_deeplink_daily_link_id_day", "link_id", "day"),
)
