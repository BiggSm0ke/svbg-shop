"""Catalog tables (04 §5 «Каталог», 02 §3.3): plans, their prices and the locations (panel squads).

* ``plans`` — what is sold: name per language, availability, trial flag, limits written to the panel
  (``traffic_bytes`` 0 = unlimited, ``reset_strategy``, ``device_limit`` with the panel's triple semantics
  NULL = panel fallback / 0 = no limit / N), internal squads (≥ 1 for a plan on sale), optional external squad
  and panel tag, renewal policies, the paid extra-device option (``device_addon``), ``broken_reason`` (a squad
  of the plan disappeared from the panel: purchase hidden) and ``version`` (bumped by every edit; the editor
  uses it for compare-and-set where a change has side effects on live subscriptions);
* ``plan_prices`` — one price per (plan, period in days, currency); ``highlight`` marks the period the buy
  screen emphasises;
* ``locations`` — the catalog of internal squads taken from the panel (``GET /internal-squads``) with the
  display title per language, a flag and the order; a squad gone from the panel keeps its row with
  ``missing_since`` (plans referring to it are marked broken, not silently changed).

Foreign keys reference ``Column`` objects, not ``"table.column"`` strings, so the DDL never depends on name
lookups in the shared metadata.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "AVAILABILITY",
    "DEVICES_ON_RENEW",
    "RESET_STRATEGIES",
    "TABLES",
    "TRAFFIC_ON_RENEW",
    "locations",
    "plan_prices",
    "plans",
]

#: Who sees a plan in the buy list: everyone, users who never paid, users who paid before, deep link only.
AVAILABILITY: tuple[str, ...] = ("all", "new", "existing", "link")
#: ``trafficLimitStrategy`` of the panel (02 §3.2).
RESET_STRATEGIES: tuple[str, ...] = ("NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING")
TRAFFIC_ON_RENEW: tuple[str, ...] = ("reset", "carry_over", "keep")
DEVICES_ON_RENEW: tuple[str, ...] = ("keep", "reset")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


plans = sa.Table(
    "plans",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("name", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("availability", sa.Text, nullable=False, server_default=sa.text("'all'")),
    sa.Column("is_trial", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("traffic_bytes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.Column("reset_strategy", sa.Text, nullable=False, server_default=sa.text("'NO_RESET'")),
    # hwidDeviceLimit triple semantics: NULL = panel fallback, 0 = no limit, N = limit (02 §3.2).
    sa.Column("device_limit", sa.Integer, nullable=True),
    sa.Column("squads", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("ext_squad", sa.Text, nullable=True),
    sa.Column("panel_tag", sa.Text, nullable=True),
    sa.Column("traffic_on_renew", sa.Text, nullable=False, server_default=sa.text("'reset'")),
    sa.Column("devices_on_renew", sa.Text, nullable=False, server_default=sa.text("'keep'")),
    # {} = no paid extra devices; {"price_minor", "per_days", "max_devices"?, "currency"} otherwise.
    sa.Column("device_addon", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("broken_reason", sa.Text, nullable=True),
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("code ~ '^[a-z0-9][a-z0-9_]{0,31}$'", name="code_format"),
    sa.CheckConstraint("jsonb_typeof(name) = 'object'", name="name_object"),
    sa.CheckConstraint(_in("availability", AVAILABILITY), name="availability"),
    sa.CheckConstraint(_in("reset_strategy", RESET_STRATEGIES), name="reset_strategy"),
    sa.CheckConstraint(_in("traffic_on_renew", TRAFFIC_ON_RENEW), name="traffic_on_renew"),
    sa.CheckConstraint(_in("devices_on_renew", DEVICES_ON_RENEW), name="devices_on_renew"),
    sa.CheckConstraint("traffic_bytes >= 0", name="traffic_bytes"),
    sa.CheckConstraint("device_limit IS NULL OR device_limit >= 0", name="device_limit"),
    sa.CheckConstraint("jsonb_typeof(squads) = 'array'", name="squads_array"),
    # An empty activeInternalSquads removes the user from every node: a plan on sale always has squads.
    sa.CheckConstraint("NOT enabled OR jsonb_array_length(squads) >= 1", name="enabled_has_squads"),
    sa.CheckConstraint("jsonb_typeof(device_addon) = 'object'", name="device_addon_object"),
    sa.CheckConstraint("panel_tag IS NULL OR panel_tag ~ '^[A-Z0-9_]{1,16}$'", name="panel_tag_format"),
    sa.CheckConstraint("version >= 1", name="version"),
    # At most one trial plan: the trial has one source of truth.
    sa.Index("uq_plans_trial", "is_trial", unique=True, postgresql_where=sa.text("is_trial")),
)

plan_prices = sa.Table(
    "plan_prices",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "plan_id",
        sa.BigInteger,
        sa.ForeignKey(plans.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("days", sa.Integer, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),
    sa.Column("highlight", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.UniqueConstraint("plan_id", "days", "currency", name="uq_plan_prices_plan_days_currency"),
    sa.CheckConstraint("days BETWEEN 1 AND 3650", name="days"),
    sa.CheckConstraint("amount_minor > 0", name="amount_positive"),
    sa.CheckConstraint("currency ~ '^[A-Z]{3,5}$'", name="currency_format"),
)

locations = sa.Table(
    "locations",
    metadata,
    sa.Column("squad_uuid", sa.Text, primary_key=True),
    sa.Column("title", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("flag", sa.Text, nullable=True),
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("panel_name", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("members", sa.Integer, nullable=True),
    sa.Column("missing_since", UtcDateTime, nullable=True),
    sa.Column("synced_at", UtcDateTime, nullable=True),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("jsonb_typeof(title) = 'object'", name="title_object"),
    sa.CheckConstraint("length(squad_uuid) BETWEEN 1 AND 64", name="squad_uuid_len"),
    sa.CheckConstraint("flag IS NULL OR length(flag) BETWEEN 1 AND 16", name="flag_len"),
)

#: Every catalog table, in creation order.
TABLES: tuple[sa.Table, ...] = (plans, plan_prices, locations)
