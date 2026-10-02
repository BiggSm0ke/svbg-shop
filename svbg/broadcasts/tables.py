"""Broadcast tables (07 §2.4.5, §2.5).

* ``broadcasts`` — one row per broadcast: the source message (``source_chat_id``/``source_msg_id``, cleared
  when Telegram can no longer copy it), its normalized copy (``content``: type, text/caption, entities,
  ``file_id``), keyboard (``buttons``: the constructor button model), audience (``segment``: preset + DSL),
  options (pin / silent / delete after N hours), the run state (``status``, ``cursor`` = last processed
  ``users.id``, ``skip`` = ids above the cursor already handled when a batch was interrupted) and counters;
* ``broadcast_msgs`` — sent messages to delete later, written only with the «удалить через N ч» option.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["BROADCAST_STATUSES", "broadcast_msgs", "broadcasts"]

BROADCAST_STATUSES: tuple[str, ...] = ("draft", "running", "paused", "done", "canceled")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


broadcasts = sa.Table(
    "broadcasts",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'draft'")),
    sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("source_chat_id", sa.BigInteger, nullable=True),
    sa.Column("source_msg_id", sa.BigInteger, nullable=True),
    sa.Column("content", JSONB, nullable=False),
    sa.Column("buttons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("segment", JSONB, nullable=False, server_default=sa.text("""'{"preset": "all"}'::jsonb""")),
    sa.Column("options", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("cursor", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("skip", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("total", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("sent", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("failed", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("blocked", sa.Integer, nullable=False, server_default=sa.text("0")),
    # {"chat_id": int, "message_id": int}: the progress message edited every few seconds.
    sa.Column("progress_msg", JSONB(none_as_null=True), nullable=True),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("started_at", UtcDateTime, nullable=True),
    sa.Column("finished_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("status", BROADCAST_STATUSES), name="status"),
    sa.CheckConstraint("jsonb_typeof(content) = 'object'", name="content_object"),
    sa.CheckConstraint("jsonb_typeof(buttons) = 'array'", name="buttons_array"),
    sa.CheckConstraint("jsonb_typeof(segment) = 'object'", name="segment_object"),
    sa.CheckConstraint("jsonb_typeof(options) = 'object'", name="options_object"),
    sa.CheckConstraint("jsonb_typeof(skip) = 'array'", name="skip_array"),
    sa.CheckConstraint(
        "cursor >= 0 AND total >= 0 AND sent >= 0 AND failed >= 0 AND blocked >= 0", name="counters"
    ),
    sa.Index("ix_broadcasts_created_at", "created_at"),
    sa.Index("ix_broadcasts_live", "status", postgresql_where=sa.text("status IN ('running', 'paused')")),
)

broadcast_msgs = sa.Table(
    "broadcast_msgs",
    metadata,
    sa.Column(
        "broadcast_id", sa.BigInteger, sa.ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("user_id", sa.BigInteger, nullable=False),
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("msg_id", sa.BigInteger, nullable=False),
    sa.Column("delete_at", UtcDateTime, nullable=False),
    sa.PrimaryKeyConstraint("broadcast_id", "user_id", name="pk_broadcast_msgs"),
    sa.Index("ix_broadcast_msgs_due", "broadcast_id", "delete_at"),
)
