"""IP Guard tables (05 §2.2.5).

* ``ip_guard_blocks`` — one row per block; a partial UNIQUE keeps **one active block per subscription**. The
  evidence is the top-50 IP keys with their nodes plus counters (never the full list: personal data,
  05 §2.2.5);
  ``evidence_purge_at`` drives the TTL purge. ``events`` is a short log (≤ 100) the card shows.
* ``ip_guard_alerts`` — warnings and the other cards: ``warn``, ``not_blocked`` (``reason``: pool,
  unconfirmed, incomplete, grace, whitelist, dismissed, precondition), ``block_failed``, ``anomaly`` (the mass
  block fuse with its quarantine window and ``members``), ``digest`` (summary of a pass).
* ``ip_guard_exempt`` — the white list as data (``until IS NULL``) and the 6 h «ложная тревога» protection
  (``until`` set).
* ``ip_guard_nodes`` — nodes the module has seen, the ``cdn`` flag (CDN nodes are never polled: their IPs are
  the CDN's) and when the node was first seen (the «пометьте CDN» alert). Stays here until the core gets its
  ``node_meta``.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "ALERT_KINDS",
    "BLOCK_REASONS",
    "BLOCK_STATUSES",
    "NOT_BLOCKED_REASONS",
    "ip_guard_alerts",
    "ip_guard_blocks",
    "ip_guard_exempt",
    "ip_guard_nodes",
]

BLOCK_STATUSES: tuple[str, ...] = ("active", "unblocked", "closed")
BLOCK_REASONS: tuple[str, ...] = ("auto", "manual", "anomaly")
ALERT_KINDS: tuple[str, ...] = ("warn", "not_blocked", "block_failed", "anomaly", "digest")
NOT_BLOCKED_REASONS: tuple[str, ...] = (
    "pool",
    "unconfirmed",
    "incomplete",
    "grace",
    "whitelist",
    "dismissed",
    "precondition",
)


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    cond = f"{column} IN ({quoted})"
    return f"{column} IS NULL OR {cond}" if nullable else cond


#: Predicate of the "one active block per subscription" index (``ON CONFLICT`` repeats it literally).
ACTIVE_BLOCK_PREDICATE = "status = 'active'"

ip_guard_blocks = sa.Table(
    "ip_guard_blocks",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("panel_user_id", sa.BigInteger, nullable=True),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("blocked_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("blocked_by", sa.BigInteger, nullable=True),  # users.id of the admin; NULL = automatic
    sa.Column("ip_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("live_ip_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("subnet_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("frozen_seconds", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("zeroed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("card_chat_id", sa.BigInteger, nullable=True),
    sa.Column("card_msg_id", sa.BigInteger, nullable=True),
    sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("confirmed_by", sa.BigInteger, nullable=True),
    sa.Column("confirmed_at", UtcDateTime, nullable=True),
    sa.Column("unblock_mode", sa.Text, nullable=True),
    sa.Column("unblocked_by", sa.BigInteger, nullable=True),
    sa.Column("unblocked_at", UtcDateTime, nullable=True),
    sa.Column("outcome", sa.Text, nullable=True),
    sa.Column("new_paid_until", UtcDateTime, nullable=True),
    sa.Column("closed_by", sa.BigInteger, nullable=True),
    sa.Column("closed_at", UtcDateTime, nullable=True),
    sa.Column("events", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("evidence_purge_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("status", BLOCK_STATUSES), name="status"),
    sa.CheckConstraint(_in("reason", BLOCK_REASONS), name="reason"),
    sa.CheckConstraint(_in("unblock_mode", ("plain", "revoke"), nullable=True), name="unblock_mode"),
    sa.CheckConstraint("frozen_seconds >= 0", name="frozen_seconds"),
    sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
    sa.CheckConstraint("jsonb_typeof(events) = 'array'", name="events_array"),
    sa.Index(
        "uq_ip_guard_blocks_active",
        "subscription_id",
        unique=True,
        postgresql_where=sa.text(ACTIVE_BLOCK_PREDICATE),
    ),
    sa.Index("ix_ip_guard_blocks_sub", "subscription_id", "blocked_at"),
    sa.Index(
        "ix_ip_guard_blocks_panel_user",
        "panel_user_id",
        postgresql_where=sa.text("panel_user_id IS NOT NULL"),
    ),
    sa.Index("ix_ip_guard_blocks_status", "status", "blocked_at"),
    sa.Index(
        "ix_ip_guard_blocks_purge",
        "evidence_purge_at",
        postgresql_where=sa.text("evidence_purge_at IS NOT NULL"),
    ),
)

ip_guard_alerts = sa.Table(
    "ip_guard_alerts",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=True,
    ),
    sa.Column("panel_user_id", sa.BigInteger, nullable=True),
    sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("card_chat_id", sa.BigInteger, nullable=True),
    sa.Column("card_msg_id", sa.BigInteger, nullable=True),
    sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("acked_by", sa.BigInteger, nullable=True),
    sa.Column("acked_at", UtcDateTime, nullable=True),
    sa.Column("quarantine_until", UtcDateTime, nullable=True),
    # anomaly: {"<panel_user_id>": {"sub": id, "state": "pending|blocked|dismissed|skipped", "ip": W}}
    sa.Column("members", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint(_in("kind", ALERT_KINDS), name="kind"),
    sa.CheckConstraint(
        "kind <> 'not_blocked' OR " + _in("reason", NOT_BLOCKED_REASONS), name="not_blocked_reason"
    ),
    sa.CheckConstraint("jsonb_typeof(metrics) = 'object'", name="metrics_object"),
    sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
    sa.CheckConstraint("jsonb_typeof(members) = 'object'", name="members_object"),
    sa.Index("ix_ip_guard_alerts_sub", "subscription_id", "created_at"),
    sa.Index("ix_ip_guard_alerts_kind", "kind", "created_at"),
    sa.Index(
        "ix_ip_guard_alerts_quarantine",
        "quarantine_until",
        postgresql_where=sa.text("kind = 'anomaly'"),
    ),
)

ip_guard_exempt = sa.Table(
    "ip_guard_exempt",
    metadata,
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("actor_id", sa.BigInteger, nullable=True),
    sa.Column("until", UtcDateTime, nullable=True),  # NULL = white list; set = «ложная тревога» until then
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("length(btrim(reason)) > 0", name="reason_not_empty"),
)

ip_guard_nodes = sa.Table(
    "ip_guard_nodes",
    metadata,
    sa.Column("node_uuid", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("address", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("cdn", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("note", sa.Text, nullable=True),
    sa.Column("first_seen_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("length(node_uuid) BETWEEN 1 AND 64", name="uuid_len"),
)
