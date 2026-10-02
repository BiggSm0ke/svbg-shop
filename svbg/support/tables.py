"""Support tickets (07 §2.4.6, §2.5): ``tickets`` and ``ticket_messages``.

* ``tickets`` — one row per *episode* of a user's conversation with support. Every user has **one** forum
  topic (``chat_id``/``thread_id``) that all their episodes reuse: closing renames the topic «✅ …» and closes
  it, the user's next message starts a new row in the same topic (reopened). At most one open row per user.
  ``first_reply_at`` — the first staff answer of the episode (daily report: time to first reply).
* ``ticket_messages`` — copies in both directions (``in``: user → topic, ``out``: topic → user):
  the message id in the user's private chat ↔ the message id in the group, so a reply on either side quotes
  the matching message on the other.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import UtcDateTime, metadata, now_default

__all__ = ["TICKET_STATUSES", "ticket_messages", "tickets"]

TICKET_STATUSES: tuple[str, ...] = ("open", "closed")

tickets = sa.Table(
    "tickets",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("chat_id", sa.BigInteger, nullable=True),
    sa.Column("thread_id", sa.BigInteger, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
    sa.Column("opened_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("first_reply_at", UtcDateTime, nullable=True),
    sa.Column("closed_at", UtcDateTime, nullable=True),
    sa.Column("closed_by", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.CheckConstraint("status IN ('open', 'closed')", name="status"),
    sa.CheckConstraint("thread_id IS NULL OR chat_id IS NOT NULL", name="thread_has_chat"),
    sa.Index("uq_tickets_open_user", "user_id", unique=True, postgresql_where=sa.text("status = 'open'")),
    sa.Index("ix_tickets_user", "user_id", "id"),
    sa.Index("ix_tickets_thread", "chat_id", "thread_id"),
)

ticket_messages = sa.Table(
    "ticket_messages",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("ticket_id", sa.BigInteger, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
    sa.Column("dir", sa.Text, nullable=False),
    sa.Column("user_msg_id", sa.BigInteger, nullable=False),
    sa.Column("group_msg_id", sa.BigInteger, nullable=False),
    sa.Column("ts", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("dir IN ('in', 'out')", name="dir"),
    sa.Index("ix_ticket_messages_ticket", "ticket_id"),
)
