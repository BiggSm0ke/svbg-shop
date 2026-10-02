"""Subscription tables (04 §5, 07 §2.5, 02 §3.1) — the stage 1 subset.

* ``subscriptions`` — one bot subscription = one panel user (``panel_user_id`` UNIQUE). ``desired_*`` is the
  bot's intent (money and plan), ``panel_*`` the last snapshot of the panel (runtime), ``overrides`` the
  fields an admin changed by hand in the panel (respected, never fought: 02 §6.3);
* ``subscription_events`` — audit of term changes (``delta_seconds``, old/new expiry) and panel-side
  surprises;
* ``panel_squad_substitutions`` / ``panel_squad_twins`` — squad substitutions contributed by owner modules (07
  §2.4.3): the **core** applies them (forward before PATCH, reverse before comparing) even when the module
  that wrote them is degraded or unloaded;
* ``trial_grants`` — one trial per bot user **and** per Telegram id (anti-abuse, 02 §4.1, 06 M2);
* ``channel_members`` — cache of the required channel membership (``chat_member`` updates, ``getChatMember``).

Stage 2 additions to ``subscriptions``: ``is_trial``, ``extra_devices`` (paid device addon on top of the
plan's limit, 06 M1), ``hold_zeroed`` (freeze, 05 §2.2.4 / X5), ``cooldowns`` (rate-limited user actions).
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "DISABLED_REASONS",
    "HOLD_KINDS",
    "LINK_STATES",
    "channel_members",
    "panel_squad_substitutions",
    "panel_squad_twins",
    "subscription_events",
    "subscriptions",
    "trial_grants",
]

LINK_STATES: tuple[str, ...] = ("pending", "linked", "panel_missing", "closed")
DESIRED_STATUSES: tuple[str, ...] = ("active", "disabled")
#: Why the bot (not the panel admin) disabled the panel user. ``BOT_BAN`` is lifted by the bot's unban.
DISABLED_REASONS: tuple[str, ...] = ("BOT_BAN", "admin", "ip_guard", "channel_left", "closed", "hold")
#: Why a subscription is frozen (X5). ``ip_guard`` disables with reason ``ip_guard``, ``admin`` with ``hold``.
HOLD_KINDS: tuple[str, ...] = ("ip_guard", "admin")


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    cond = f"{column} IN ({quoted})"
    return f"{column} IS NULL OR {cond}" if nullable else cond


# Same UUIDv7-by-SQL expression as ``users.public_id`` (svbg.core.tables): raw inserts get a valid id too.
_UUID7_SQL = (
    "(encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid()) placing "
    "substring(int8send(floor(extract(epoch from clock_timestamp()) * 1000)::bigint) from 3) "
    "from 1 for 6), 52, 1), 53, 1), 'hex')::uuid)::text"
)

subscriptions = sa.Table(
    "subscriptions",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("public_id", sa.Text, nullable=False, unique=True, server_default=sa.text(_UUID7_SQL)),
    # NULL = unclaimed (imported panel user without telegramId, 02 §7.1).
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    sa.Column("plan_id", sa.BigInteger, nullable=True),  # NULL = legacy / imported without a plan
    sa.Column("plan_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("link_state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    # Identity in the panel (02 §3.1): numeric id is the link key; shortUuid survives 2.x→3.x; username as is.
    sa.Column("panel_user_id", sa.BigInteger, nullable=True, unique=True),
    sa.Column("panel_short_uuid", sa.Text, nullable=True, unique=True),
    sa.Column("panel_username", sa.Text, nullable=True, unique=True),
    sa.Column("subscription_url", sa.Text, nullable=True),  # ONLY from the panel's answer
    # Money and intent (owned by the bot).
    sa.Column("paid_until", UtcDateTime, nullable=True),
    sa.Column("desired_expire_at", UtcDateTime, nullable=True),
    sa.Column("desired_traffic_bytes", sa.BigInteger, nullable=True),  # 0 = unlimited
    sa.Column("desired_reset_strategy", sa.Text, nullable=True),
    # hwidDeviceLimit triple semantics: NULL = panel fallback, 0 = no limit, N = limit (02 §3.2).
    sa.Column("desired_device_limit", sa.Integer, nullable=True),
    sa.Column("desired_squads", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("desired_ext_squad", sa.Text, nullable=True),
    sa.Column("desired_tag", sa.Text, nullable=True),
    sa.Column("desired_status", sa.Text, nullable=False, server_default=sa.text("'active'")),
    # Last panel snapshot (owned by the panel).
    sa.Column("panel_status", sa.Text, nullable=True),
    sa.Column("panel_expire_at", UtcDateTime, nullable=True),
    sa.Column("panel_traffic_limit", sa.BigInteger, nullable=True),
    sa.Column("panel_used_traffic", sa.BigInteger, nullable=True),
    sa.Column("panel_reset_strategy", sa.Text, nullable=True),
    sa.Column("panel_device_limit", sa.Integer, nullable=True),
    sa.Column("panel_squads", JSONB, nullable=True),
    sa.Column("panel_ext_squad", sa.Text, nullable=True),
    sa.Column("panel_tag", sa.Text, nullable=True),
    sa.Column("panel_telegram_id", sa.BigInteger, nullable=True),
    sa.Column("panel_online_at", UtcDateTime, nullable=True),
    sa.Column("panel_first_connected_at", UtcDateTime, nullable=True),
    sa.Column("panel_sub_last_opened_at", UtcDateTime, nullable=True),
    sa.Column("panel_last_traffic_reset_at", UtcDateTime, nullable=True),
    sa.Column("panel_state_ts", UtcDateTime, nullable=True),
    sa.Column("overrides", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("disabled_reason", sa.Text, nullable=True),
    sa.Column("hold_kind", sa.Text, nullable=True),
    sa.Column("hold_since", UtcDateTime, nullable=True),
    sa.Column("hold_frozen_seconds", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    # Stage 2 (appended: ALTER TABLE ADD COLUMN keeps migrated and created schemas identical).
    sa.Column("hold_zeroed", sa.Boolean, nullable=False, server_default=sa.text("false")),
    sa.Column("is_trial", sa.Boolean, nullable=False, server_default=sa.text("false")),
    # Paid extra devices on top of the plan's device_limit (06 M1: 5 included, +N paid, up to 15).
    sa.Column("extra_devices", sa.Integer, nullable=False, server_default=sa.text("0")),
    # {"reissue": "<iso ts>", "devices_reset": "<iso ts>"}: last run of rate-limited user actions.
    sa.Column("cooldowns", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.CheckConstraint(_in("link_state", LINK_STATES), name="link_state"),
    sa.CheckConstraint(_in("desired_status", DESIRED_STATUSES), name="desired_status"),
    sa.CheckConstraint(_in("disabled_reason", DISABLED_REASONS, nullable=True), name="disabled_reason"),
    sa.CheckConstraint("jsonb_typeof(desired_squads) = 'array'", name="desired_squads_array"),
    sa.CheckConstraint("jsonb_typeof(overrides) = 'object'", name="overrides_object"),
    sa.CheckConstraint("jsonb_typeof(cooldowns) = 'object'", name="cooldowns_object"),
    sa.CheckConstraint(_in("hold_kind", HOLD_KINDS, nullable=True), name="hold_kind"),
    sa.CheckConstraint("hold_kind IS NULL OR hold_since IS NOT NULL", name="hold_has_since"),
    sa.CheckConstraint("hold_frozen_seconds >= 0", name="hold_frozen_seconds"),
    sa.CheckConstraint("extra_devices >= 0", name="extra_devices"),
    sa.CheckConstraint(
        "desired_device_limit IS NULL OR desired_device_limit >= 0", name="desired_device_limit"
    ),
    sa.CheckConstraint(
        "desired_traffic_bytes IS NULL OR desired_traffic_bytes >= 0", name="desired_traffic_bytes"
    ),
    sa.CheckConstraint("panel_user_id IS NULL OR panel_user_id > 0", name="panel_user_id_positive"),
    sa.CheckConstraint(
        "link_state <> 'linked' OR panel_user_id IS NOT NULL", name="linked_has_panel_user_id"
    ),
    sa.Index("ix_subscriptions_user_id", "user_id", postgresql_where=sa.text("user_id IS NOT NULL")),
    # Reconciliation walks linked subscriptions in id order (keyset) to find the ones gone from the panel.
    sa.Index(
        "ix_subscriptions_linked_id",
        "id",
        postgresql_where=sa.text("link_state = 'linked'"),
    ),
    # Bot-side reminders (no-webhook mode) select by expiry.
    sa.Index(
        "ix_subscriptions_panel_expire_at",
        "panel_expire_at",
        postgresql_where=sa.text("link_state = 'linked'"),
    ),
)

subscription_events = sa.Table(
    "subscription_events",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),  # bot | panel | webhook | sync | import | admin | system
    sa.Column("delta_seconds", sa.BigInteger, nullable=True),
    sa.Column("old_expire", UtcDateTime, nullable=True),
    sa.Column("new_expire", UtcDateTime, nullable=True),
    sa.Column("ref_type", sa.Text, nullable=True),
    sa.Column("ref_id", sa.Text, nullable=True),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("ts", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("length(kind) > 0", name="kind_not_empty"),
    sa.Index("ix_subscription_events_sub_ts", "subscription_id", "ts"),
    # One event per (subscription, kind, reference): a retried job does not log its effect twice.
    sa.Index(
        "uq_subscription_events_ref",
        "subscription_id",
        "kind",
        "ref_type",
        "ref_id",
        unique=True,
        postgresql_where=sa.text("ref_id IS NOT NULL"),
    ),
    # "Was this order already applied?" looks up by reference alone (idempotent fulfill).
    sa.Index(
        "ix_subscription_events_ref_lookup",
        "ref_type",
        "ref_id",
        postgresql_where=sa.text("ref_id IS NOT NULL"),
    ),
)

#: Predicate of ``uq_subscription_events_ref`` (``ON CONFLICT`` must repeat it literally).
EVENT_REF_PREDICATE = "ref_id IS NOT NULL"

panel_squad_substitutions = sa.Table(
    "panel_squad_substitutions",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("base_squad_uuid", sa.Text, nullable=False),
    sa.Column("substitute_squad_uuid", sa.Text, nullable=False),
    sa.Column("owner_module", sa.Text, nullable=False),
    sa.Column("source_ref", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("subscription_id", "base_squad_uuid", name="uq_panel_squad_substitutions_sub_base"),
    sa.CheckConstraint("base_squad_uuid <> substitute_squad_uuid", name="not_identity"),
    sa.CheckConstraint("length(owner_module) > 0", name="owner_module_not_empty"),
    sa.Index("ix_panel_squad_substitutions_owner", "owner_module"),
)

panel_squad_twins = sa.Table(
    "panel_squad_twins",
    metadata,
    sa.Column("substitute_squad_uuid", sa.Text, primary_key=True),
    sa.Column("base_squad_uuid", sa.Text, nullable=False),
    sa.Column("owner_module", sa.Text, nullable=False),
    sa.CheckConstraint("base_squad_uuid <> substitute_squad_uuid", name="not_identity"),
)

trial_grants = sa.Table(
    "trial_grants",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    # Kept even when the user row goes away: the same Telegram account never gets a second trial.
    sa.Column("telegram_id", sa.BigInteger, nullable=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),  # bot | import | admin
    sa.Column("granted_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("user_id", name="uq_trial_grants_user_id"),
    sa.UniqueConstraint("telegram_id", name="uq_trial_grants_telegram_id"),
    sa.CheckConstraint("user_id IS NOT NULL OR telegram_id IS NOT NULL", name="has_owner"),
)

channel_members = sa.Table(
    "channel_members",
    metadata,
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("telegram_id", sa.BigInteger, nullable=False),
    sa.Column("status", sa.Text, nullable=False),  # creator|administrator|member|restricted|left|kicked
    sa.Column("is_member", sa.Boolean, nullable=False),
    # Time of the fact (``ChatMemberUpdated.date`` or the moment of ``getChatMember``): an older fact never
    # overwrites a newer one (updates may arrive out of order).
    sa.Column("seen_at", UtcDateTime, nullable=False),
    sa.PrimaryKeyConstraint("chat_id", "telegram_id", name="pk_channel_members"),
    sa.CheckConstraint("length(status) > 0", name="status_not_empty"),
)
