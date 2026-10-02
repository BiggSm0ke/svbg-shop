"""LTE quotas: the 14 ``lte_*`` tables (05 §2.1.12 — the single source of the schema).

* ``lte_groups`` — a group of nodes with limits in three states (``has_*=false`` — not set, ``NULL`` —
  unlimited, ``0`` — unavailable); activation without ``default`` is impossible; ``version`` guards
  concurrent edits through ``callback_data``;
* ``lte_group_nodes`` — membership spans ``[counted_from, counted_to)``; a draft has ``counted_to =
  counted_from`` (never "since the beginning of time");
* ``lte_twins`` — the manual map "base → twin" and the last invariant check;
* ``lte_overrides`` — individual limits, exemptions and "no block" (one table instead of three);
* ``lte_anchors`` / ``lte_periods`` — the series state and its periods (one live period per subscription);
* ``lte_period_usage`` — usage per (period, group): decisions are taken on it;
* ``lte_counters`` / ``lte_node_state`` — panel history counters (3 days, LTE nodes only) and read marks;
* ``lte_usage_hourly`` (72 h) / ``lte_usage_daily`` (70 days, MSK dates) — buckets for exact series starts
  and re-summing periods;
* ``lte_blocks`` — block state (retries live in ``jobs``); one live block per (subscription, group);
* ``lte_credits`` / ``lte_packs`` — bought or granted gigabytes of a period and the pack catalog.

Until the integration step registers the module (``svbg.db.schema.TABLE_MODULES`` + an Alembic revision)
the tables live on their own :data:`lte_metadata`, so importing this module never changes the shared
``svbg.db.meta.metadata`` (the migration test compares it with Alembic). Integration flips
:data:`REGISTERED` to ``True``: the tables move to the shared metadata and the two FK stubs disappear.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, NAMING, UtcDateTime, now_default
from svbg.db.meta import metadata as shared_metadata
from svbg.ext.lte.model import (
    ANCHOR_KINDS,
    BLOCK_MODES,
    BLOCK_REASONS,
    BLOCK_STATUSES,
    CREDIT_SOURCES,
    CREDIT_STATUSES,
    EXEMPT_KINDS,
    GROUP_STATES,
    LIVE_BLOCK_STATUSES,
    OVERRIDE_APPLIES_TO,
    OVERRIDE_KINDS,
    PERIOD_STATES,
)

__all__ = [
    "LTE_TABLES",
    "REGISTERED",
    "create_tables",
    "lte_anchors",
    "lte_blocks",
    "lte_counters",
    "lte_credits",
    "lte_group_nodes",
    "lte_groups",
    "lte_metadata",
    "lte_node_state",
    "lte_overrides",
    "lte_packs",
    "lte_period_usage",
    "lte_periods",
    "lte_twins",
    "lte_usage_daily",
    "lte_usage_hourly",
]


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    cond = f"{column} IN ({quoted})"
    return f"{column} IS NULL OR {cond}" if nullable else cond


def _sub_fk() -> sa.ForeignKey:
    return sa.ForeignKey("subscriptions.id", ondelete="CASCADE")


def _group_fk() -> sa.ForeignKey:
    return sa.ForeignKey("lte_groups.id", ondelete="CASCADE")


#: Integration: ``True`` together with "svbg.ext.lte.tables" in ``svbg.db.schema.TABLE_MODULES``.
REGISTERED = True

if REGISTERED:
    lte_metadata = shared_metadata
else:  # pragma: no branch - flipped by the integration step
    lte_metadata = sa.MetaData(naming_convention=NAMING)
    # FK targets owned by the core: they exist in the database and are never created from here.
    sa.Table("subscriptions", lte_metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    sa.Table("orders", lte_metadata, sa.Column("id", sa.BigInteger, primary_key=True))

_LIVE = "status IN ({})".format(", ".join(f"'{s}'" for s in LIVE_BLOCK_STATUSES))

lte_groups = sa.Table(
    "lte_groups",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("slug", sa.Text, nullable=False, unique=True),
    sa.Column("name", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),  # {"ru": …, "en": …}
    sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'draft'")),
    sa.Column("enforce", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("margin_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("margin_pct", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("limit_default_bytes", sa.BigInteger, nullable=True),
    sa.Column("has_default", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("limit_trial_bytes", sa.BigInteger, nullable=True),
    sa.Column("has_trial", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("squad_uuid", sa.Text, nullable=True),  # optional squad of the group's inbounds only
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{0,31}$'", name="slug_format"),
    sa.CheckConstraint("jsonb_typeof(name) = 'object'", name="name_object"),
    sa.CheckConstraint(_in("state", GROUP_STATES), name="state"),
    sa.CheckConstraint("margin_bytes >= 0", name="margin_bytes"),
    sa.CheckConstraint("margin_pct BETWEEN 0 AND 100", name="margin_pct"),
    sa.CheckConstraint("limit_default_bytes IS NULL OR limit_default_bytes >= 0", name="limit_default"),
    sa.CheckConstraint("limit_trial_bytes IS NULL OR limit_trial_bytes >= 0", name="limit_trial"),
    sa.CheckConstraint("has_default OR limit_default_bytes IS NULL", name="default_value_needs_flag"),
    sa.CheckConstraint("has_trial OR limit_trial_bytes IS NULL", name="trial_value_needs_flag"),
    sa.CheckConstraint("state <> 'active' OR has_default", name="active_has_default"),
    sa.CheckConstraint("version > 0", name="version_positive"),
)

lte_group_nodes = sa.Table(
    "lte_group_nodes",
    lte_metadata,
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column("node_uuid", sa.Text, nullable=False),
    sa.Column("counted_from", UtcDateTime, nullable=False),
    sa.Column("counted_to", UtcDateTime, nullable=True),
    sa.PrimaryKeyConstraint("group_id", "node_uuid", "counted_from", name="pk_lte_group_nodes"),
    sa.CheckConstraint("counted_to IS NULL OR counted_to >= counted_from", name="span"),
    sa.CheckConstraint("node_uuid = lower(node_uuid)", name="node_uuid_lower"),
    sa.Index("ix_lte_group_nodes_node_uuid", "node_uuid"),
)

lte_twins = sa.Table(
    "lte_twins",
    lte_metadata,
    sa.Column("base_squad_uuid", sa.Text, primary_key=True),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column("twin_squad_uuid", sa.Text, nullable=False),
    sa.Column("checked_at", UtcDateTime, nullable=True),
    sa.Column("problem", sa.Text, nullable=True),
    sa.CheckConstraint("base_squad_uuid <> twin_squad_uuid", name="not_identity"),
    sa.Index("ix_lte_twins_group_id", "group_id"),
)

lte_anchors = sa.Table(
    "lte_anchors",
    lte_metadata,
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), primary_key=True),
    sa.Column("anchor_at", UtcDateTime, nullable=False),
    sa.Column("anchor_kind", sa.Text, nullable=False),
    sa.Column("anchor_source", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("series_open", sa.Boolean, nullable=False, server_default=sa.text("true")),
    sa.Column("series_started_at", UtcDateTime, nullable=True),
    sa.Column("series_closed_at", UtcDateTime, nullable=True),
    sa.Column("coverage_end", UtcDateTime, nullable=True),
    sa.Column("is_trial", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("review_reason", sa.Text, nullable=True),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint(_in("anchor_kind", ANCHOR_KINDS), name="anchor_kind"),
    sa.CheckConstraint("series_open OR series_closed_at IS NOT NULL", name="closed_has_time"),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.Index(
        "ix_lte_anchors_review", "subscription_id", postgresql_where=sa.text("review_reason IS NOT NULL")
    ),
)

lte_periods = sa.Table(
    "lte_periods",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("anchor_at", UtcDateTime, nullable=False),
    sa.Column("idx", sa.Integer, nullable=False),
    sa.Column("starts_at", UtcDateTime, nullable=False),
    sa.Column("planned_end_at", UtcDateTime, nullable=False),
    sa.Column("ended_at", UtcDateTime, nullable=True),
    sa.Column("end_cause", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'open'")),
    sa.Column("is_trial", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("start_estimated", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.CheckConstraint(_in("state", PERIOD_STATES), name="state"),
    sa.CheckConstraint("idx >= 0", name="idx"),
    sa.CheckConstraint("planned_end_at > starts_at", name="planned_after_start"),
    sa.CheckConstraint("(state = 'closed') = (ended_at IS NOT NULL)", name="closed_has_end"),
    sa.CheckConstraint("ended_at IS NULL OR ended_at >= starts_at", name="end_after_start"),
    # One live period per subscription.
    sa.Index(
        "uq_lte_periods_live",
        "subscription_id",
        unique=True,
        postgresql_where=sa.text("state <> 'closed'"),
    ),
    sa.Index("ix_lte_periods_sub_starts", "subscription_id", "starts_at"),
    # Timers of the cycle: open periods by their planned end.
    sa.Index("ix_lte_periods_open_end", "planned_end_at", postgresql_where=sa.text("state = 'open'")),
    # Retention of closed periods.
    sa.Index("ix_lte_periods_closed_end", "ended_at", postgresql_where=sa.text("state = 'closed'")),
)

lte_overrides = sa.Table(
    "lte_overrides",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=True),  # NULL — every group
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("limit_bytes", sa.BigInteger, nullable=True),  # kind='limit': NULL — unlimited
    sa.Column("applies_to", sa.Text, nullable=False, server_default=sa.text("'all'")),
    sa.Column("period_id", sa.BigInteger, sa.ForeignKey("lte_periods.id", ondelete="CASCADE"), nullable=True),
    sa.Column("valid_until", UtcDateTime, nullable=True),
    sa.Column("exempt_kind", sa.Text, nullable=True),
    sa.Column("reason", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("actor_id", sa.BigInteger, nullable=True),  # NULL — the system
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("revoked_at", UtcDateTime, nullable=True),
    sa.Column("revoke_reason", sa.Text, nullable=True),
    sa.CheckConstraint(_in("kind", OVERRIDE_KINDS), name="kind"),
    sa.CheckConstraint(_in("applies_to", OVERRIDE_APPLIES_TO), name="applies_to"),
    sa.CheckConstraint(_in("exempt_kind", EXEMPT_KINDS, nullable=True), name="exempt_kind"),
    sa.CheckConstraint("(kind = 'exempt') = (exempt_kind IS NOT NULL)", name="exempt_has_kind"),
    sa.CheckConstraint("kind = 'limit' OR limit_bytes IS NULL", name="limit_only_for_limit"),
    sa.CheckConstraint("limit_bytes IS NULL OR limit_bytes >= 0", name="limit_bytes"),
    sa.CheckConstraint("revoked_at IS NULL OR revoke_reason IS NOT NULL", name="revoked_has_reason"),
    sa.Index("ix_lte_overrides_live", "subscription_id", postgresql_where=sa.text("revoked_at IS NULL")),
    # One live exemption per (subscription, group or "every group").
    sa.Index(
        "uq_lte_overrides_live_exempt",
        "subscription_id",
        sa.text("coalesce(group_id, 0)"),
        unique=True,
        postgresql_where=sa.text("kind = 'exempt' AND revoked_at IS NULL"),
    ),
)

lte_period_usage = sa.Table(
    "lte_period_usage",
    lte_metadata,
    sa.Column(
        "period_id", sa.BigInteger, sa.ForeignKey("lte_periods.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column("used_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("after_block_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("estimated_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("last_delta_at", UtcDateTime, nullable=True),
    sa.PrimaryKeyConstraint("period_id", "group_id", name="pk_lte_period_usage"),
    sa.CheckConstraint(
        "used_bytes >= 0 AND after_block_bytes >= 0 AND estimated_bytes >= 0", name="non_negative"
    ),
)

lte_counters = sa.Table(
    "lte_counters",
    lte_metadata,
    sa.Column("node_uuid", sa.Text, nullable=False),
    sa.Column("usage_date", sa.Date, nullable=False),
    sa.Column("panel_user_id", sa.BigInteger, nullable=False),
    sa.Column("total", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("accounted", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("baseline", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("carry", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("vanished_at", UtcDateTime, nullable=True),
    sa.Column("seen_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.PrimaryKeyConstraint("node_uuid", "usage_date", "panel_user_id", name="pk_lte_counters"),
    sa.CheckConstraint("total >= 0 AND accounted >= 0 AND baseline >= 0 AND carry >= 0", name="non_negative"),
    # The key invariant of the accounting is guarded by the database too.
    sa.CheckConstraint("baseline + accounted = total + carry", name="key_invariant"),
    sa.Index("ix_lte_counters_usage_date", "usage_date"),
)

lte_node_state = sa.Table(
    "lte_node_state",
    lte_metadata,
    sa.Column("node_uuid", sa.Text, primary_key=True),
    sa.Column("first_read_at", UtcDateTime, nullable=True),
    sa.Column("last_ok_read_at", UtcDateTime, nullable=True),
    sa.Column("last_ok_read_date", sa.Date, nullable=True),
    sa.Column("gap_anchor_at", UtcDateTime, nullable=True),
    sa.Column("gap_tail", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("disconnected_since", UtcDateTime, nullable=True),
    sa.Column("xray_uptime_s", sa.BigInteger, nullable=True),
    sa.CheckConstraint("gap_tail >= 0", name="gap_tail"),
)

lte_usage_hourly = sa.Table(
    "lte_usage_hourly",
    lte_metadata,
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column("hour_utc", UtcDateTime, nullable=False),
    sa.Column("bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.PrimaryKeyConstraint("subscription_id", "group_id", "hour_utc", name="pk_lte_usage_hourly"),
    sa.CheckConstraint("bytes >= 0", name="bytes"),
    sa.Index("ix_lte_usage_hourly_hour_utc", "hour_utc"),
)

lte_usage_daily = sa.Table(
    "lte_usage_daily",
    lte_metadata,
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column("msk_date", sa.Date, nullable=False),
    sa.Column("bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("after_block_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.PrimaryKeyConstraint("subscription_id", "group_id", "msk_date", name="pk_lte_usage_daily"),
    sa.CheckConstraint("bytes >= 0 AND after_block_bytes >= 0", name="non_negative"),
    sa.Index("ix_lte_usage_daily_msk_date", "msk_date"),
)

lte_blocks = sa.Table(
    "lte_blocks",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column(
        "period_id", sa.BigInteger, sa.ForeignKey("lte_periods.id", ondelete="SET NULL"), nullable=True
    ),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("mode", sa.Text, nullable=False, server_default=sa.text("'enforce'")),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
    sa.Column("used_at_block", sa.BigInteger, nullable=True),
    sa.Column("limit_at_block", sa.BigInteger, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("applied_at", UtcDateTime, nullable=True),
    sa.Column("release_reason", sa.Text, nullable=True),
    sa.Column("released_at", UtcDateTime, nullable=True),
    sa.Column("resend_done_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("reason", BLOCK_REASONS), name="reason"),
    sa.CheckConstraint(_in("mode", BLOCK_MODES), name="mode"),
    sa.CheckConstraint(_in("status", BLOCK_STATUSES), name="status"),
    sa.CheckConstraint("status = 'active' OR release_reason IS NOT NULL", name="release_has_reason"),
    sa.CheckConstraint(
        "(status IN ('released', 'cancelled')) = (released_at IS NOT NULL)", name="released_has_time"
    ),
    # One live block per (subscription, group).
    sa.Index(
        "uq_lte_blocks_live",
        "subscription_id",
        "group_id",
        unique=True,
        postgresql_where=sa.text(_LIVE),
    ),
    sa.Index("ix_lte_blocks_live_group", "group_id", postgresql_where=sa.text(_LIVE)),
)

lte_credits = sa.Table(
    "lte_credits",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("subscription_id", sa.BigInteger, _sub_fk(), nullable=False),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=False),
    sa.Column(
        "period_id", sa.BigInteger, sa.ForeignKey("lte_periods.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("bytes", sa.BigInteger, nullable=False),
    sa.Column(
        "order_id",
        sa.BigInteger,
        sa.ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    ),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
    sa.Column("amount_minor", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("expired_at", UtcDateTime, nullable=True),
    sa.CheckConstraint("bytes > 0", name="bytes_positive"),
    sa.CheckConstraint("amount_minor >= 0", name="amount_minor"),
    sa.CheckConstraint(_in("source", CREDIT_SOURCES), name="source"),
    sa.CheckConstraint(_in("status", CREDIT_STATUSES), name="status"),
    sa.CheckConstraint("status = 'active' OR expired_at IS NOT NULL", name="inactive_has_time"),
    sa.Index("ix_lte_credits_period_active", "period_id", postgresql_where=sa.text("status = 'active'")),
)

lte_packs = sa.Table(
    "lte_packs",
    lte_metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("group_id", sa.BigInteger, _group_fk(), nullable=True),  # NULL — any group
    sa.Column("gb", sa.Integer, nullable=False),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.Text, nullable=False, server_default=sa.text("'RUB'")),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.CheckConstraint("gb > 0 AND gb <= 100000", name="gb"),
    sa.CheckConstraint("amount_minor >= 0", name="amount_minor"),
    sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
)

#: In creation order (FK dependencies first).
LTE_TABLES: tuple[sa.Table, ...] = (
    lte_groups,
    lte_group_nodes,
    lte_twins,
    lte_anchors,
    lte_periods,
    lte_overrides,
    lte_period_usage,
    lte_counters,
    lte_node_state,
    lte_usage_hourly,
    lte_usage_daily,
    lte_blocks,
    lte_credits,
    lte_packs,
)


def create_tables(sync_conn: sa.Connection) -> None:
    """Create the ``lte_*`` tables (tests; before the migration exists) — ``run_sync(create_tables)``.

    The core schema (``subscriptions``, ``orders``) must exist already.
    """
    lte_metadata.create_all(sync_conn, tables=list(LTE_TABLES))
