"""Ad links / campaigns (01 §1.4, 06 §2.7).

* ``ad_links`` — ``code`` is the whole ``/start`` parameter, **as is** (Bedolaga campaign codes have no
  prefix, ≤ 64 characters ``[A-Za-z0-9_-]``); the deep-link router looks it up first (exact match).
  ``bonus`` keeps an imported Bedolaga bonus for reference (``{"type": "none"}`` — no bonus is given yet).
  ``clicks`` counts every ``/start`` through the link.
* ``ad_link_users`` — first touch: the link a user came from (one row per user; repeated starts through other
  links change nothing). Kept apart from ``users`` so the core table stays untouched.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["ad_link_users", "ad_links"]

ad_links = sa.Table(
    "ad_links",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("bonus", JSONB, nullable=False, server_default=sa.text("""'{"type": "none"}'::jsonb""")),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("clicks", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("owner_user_id", sa.BigInteger, nullable=True),  # Bedolaga partner (reference only)
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),
    sa.Column("legacy_id", sa.Text, nullable=True),
    sa.Column("created_by", sa.BigInteger, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("code ~ '^[A-Za-z0-9_-]{1,64}$'", name="code_format"),
    sa.CheckConstraint("length(title) BETWEEN 1 AND 128", name="title_len"),
    sa.CheckConstraint("jsonb_typeof(bonus) = 'object'", name="bonus_object"),
    sa.CheckConstraint("source IN ('bot', 'import')", name="source"),
    sa.CheckConstraint("clicks >= 0", name="clicks"),
    sa.Index(
        "uq_ad_links_legacy",
        "source",
        "legacy_id",
        unique=True,
        postgresql_where=sa.text("legacy_id IS NOT NULL"),
    ),
)

ad_link_users = sa.Table(
    "ad_link_users",
    metadata,
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("ad_link_id", sa.BigInteger, sa.ForeignKey("ad_links.id", ondelete="CASCADE"), nullable=False),
    sa.Column("attached_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),
    sa.CheckConstraint("source IN ('bot', 'import')", name="source"),
    sa.Index("ix_ad_link_users_link", "ad_link_id", "attached_at"),
)
