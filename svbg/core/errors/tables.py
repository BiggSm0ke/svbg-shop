"""Error hub tables: grouped errors and a short-lived ring of individual events.

``error_groups`` holds one row per fingerprint (see :mod:`svbg.core.errors.fingerprint`) with counters
and the delivery state (message reference in the owner chat, throttling marks, current "episode").
``error_events`` keeps individual occurrences for 14 days (purged by :meth:`ErrorHub.purge`).
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata

GROUP_STATUSES = ("open", "muted", "resolved")
SEVERITIES = ("info", "warn", "error")

error_groups = sa.Table(
    "error_groups",
    metadata,
    sa.Column("fingerprint", sa.Text, primary_key=True),
    sa.Column("place", sa.Text, nullable=False),
    sa.Column("module", sa.Text, nullable=True),
    sa.Column("severity", sa.Text, nullable=False, server_default="error"),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("first_seen", UtcDateTime, nullable=False),
    sa.Column("last_seen", UtcDateTime, nullable=False),
    sa.Column("count", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("users_count", sa.Integer, nullable=False, server_default="0"),
    # Masked technical sample of the latest occurrence (type, message, stack, handled, hint, version, ...).
    sa.Column("sample", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.Column("muted_until", UtcDateTime, nullable=True),
    # Opaque reference returned by the sink (e.g. {"chat_id": ..., "message_id": ...}).
    sa.Column("chat_ref", JSONB, nullable=True),
    # --- delivery bookkeeping (not in the stage-0 contract list; needed for throttling/episodes) ---
    sa.Column("episode_started_at", UtcDateTime, nullable=False),
    sa.Column("episode_count", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reopened", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("notified_at", UtcDateTime, nullable=True),
    sa.Column("notified_count", sa.BigInteger, nullable=False, server_default="0"),
    sa.CheckConstraint(f"status IN {GROUP_STATUSES!r}", name="status"),
    sa.CheckConstraint(f"severity IN {SEVERITIES!r}", name="severity"),
)

sa.Index("ix_error_groups_last_seen", error_groups.c.last_seen)

error_events = sa.Table(
    "error_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column(
        "fingerprint",
        sa.Text,
        sa.ForeignKey("error_groups.fingerprint", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("ts", UtcDateTime, nullable=False),
    sa.Column("user_id", sa.BigInteger, nullable=True),
    sa.Column("context", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
)

sa.Index("ix_error_events_ts", error_events.c.ts)
sa.Index("ix_error_events_fingerprint_user", error_events.c.fingerprint, error_events.c.user_id)
sa.Index("ix_error_events_fingerprint_id", error_events.c.fingerprint, error_events.c.id)
