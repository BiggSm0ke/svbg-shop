"""Admin chat tables (07 §2.4.2, §2.5): ``admin_topics`` and ``admin_cards``.

* ``admin_topics`` — one row per topic kind (core: ``payments``, ``errors`` …; modules: ``lte`` …): in which
  chat the topic lives, its ``message_thread_id`` (created by the bot, never written to ``.env``), the
  owner's switch (``enabled``) and what happens to messages of a disabled topic (``fallback``: ``system`` —
  into «⚙️ Система», ``drop`` — not sent). ``last_error`` / ``recreated_at`` explain self-healing in
  «Состояние».
* ``admin_cards`` — messages that are edited in place instead of being re-sent (IP Guard card, ticket,
  manual payment): ``(kind, ref)`` → the message in the admin chat. ``state`` keeps a digest of the last
  rendered content so an unchanged card is not edited again.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["TOPIC_FALLBACKS", "admin_cards", "admin_topics"]

TOPIC_FALLBACKS: tuple[str, ...] = ("system", "drop")

admin_topics = sa.Table(
    "admin_topics",
    metadata,
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("chat_id", sa.BigInteger, nullable=True),
    sa.Column("thread_id", sa.BigInteger, nullable=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("icon", sa.Text, nullable=True),  # emoji shown in the topic name / used to pick the icon
    sa.Column("icon_emoji_id", sa.Text, nullable=True),  # custom emoji id of the topic icon, if any
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("fallback", sa.Text, nullable=False, server_default=sa.text("'system'")),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("recreated_at", UtcDateTime, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("fallback IN ('system', 'drop')", name="fallback"),
    sa.CheckConstraint("length(kind) BETWEEN 1 AND 32", name="kind_len"),
    sa.CheckConstraint("thread_id IS NULL OR chat_id IS NOT NULL", name="thread_has_chat"),
)

admin_cards = sa.Table(
    "admin_cards",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("ref", sa.Text, nullable=False),
    sa.Column("chat_id", sa.BigInteger, nullable=True),
    sa.Column("msg_id", sa.BigInteger, nullable=True),
    sa.Column("thread_id", sa.BigInteger, nullable=True),
    sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("kind", "ref", name="uq_admin_cards_kind_ref"),
    sa.CheckConstraint("length(kind) > 0 AND length(ref) BETWEEN 1 AND 200", name="ref_len"),
    sa.CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
)
