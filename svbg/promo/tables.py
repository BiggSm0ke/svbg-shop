"""Promo tables (01 §1.4, 06 §2.6, 07 §2.4.4).

* ``promocodes`` — one code of one kind (:data:`svbg.promo.rules.KINDS`) with its limits. ``code`` is kept
  as typed (Bedolaga codes are imported as is, with their case); lookups are case-insensitive through the
  unique index on ``lower(code)``. ``uses`` is the counter shown to the owner (recomputed from
  ``promo_uses`` after an import); ``version`` is a compare-and-set for edits.
* ``promo_uses`` — one row per use. Immediate kinds (days, wallet, …) write it in the activation transaction;
  discount kinds write it when the discounted order is fulfilled (``order_id``, unique per promo).
* ``promo_pending`` — a discount waiting for checkout (07 §2.4.4 ``pending_promo``): one per user, replaced by
  a newer one, removed when used or expired. Kept in its own table so ``users`` stays untouched.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["USE_SOURCES", "promo_pending", "promo_uses", "promocodes"]

#: Where a use came from: typed in the bot, a deep link, checkout (discount), the importer, the old site.
USE_SOURCES: tuple[str, ...] = ("bot", "link", "checkout", "admin", "import", "site")
_KINDS_SQL = "kind IN ('days', 'percent', 'fixed', 'wallet', 'trial_extend', 'plan_gift', 'wallet_days')"

promocodes = sa.Table(
    "promocodes",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=True),  # owner's note
    sa.Column("days", sa.Integer, nullable=True),
    sa.Column("amount_minor", sa.BigInteger, nullable=True),
    sa.Column("currency", sa.Text, nullable=True),
    sa.Column("percent", sa.SmallInteger, nullable=True),
    sa.Column("plan_id", sa.BigInteger, nullable=True),  # plan_gift: the plan given
    sa.Column("pending_hours", sa.Integer, nullable=True),  # discounts: how long it waits for checkout
    sa.Column("max_uses", sa.Integer, nullable=True),  # NULL = unlimited
    sa.Column("uses", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("once_per_user", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("new_users_only", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("min_amount_minor", sa.BigInteger, nullable=True),  # discounts: minimal order subtotal
    sa.Column("plan_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),  # discounts: allowed
    sa.Column("starts_at", UtcDateTime, nullable=True),
    sa.Column("expires_at", UtcDateTime, nullable=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),  # bot | import
    sa.Column("legacy_id", sa.Text, nullable=True),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("created_by", sa.BigInteger, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint(_KINDS_SQL, name="kind"),
    sa.CheckConstraint("length(code) BETWEEN 1 AND 64", name="code_len"),
    sa.CheckConstraint("days IS NULL OR days BETWEEN 1 AND 3650", name="days"),
    sa.CheckConstraint("amount_minor IS NULL OR amount_minor > 0", name="amount"),
    sa.CheckConstraint("percent IS NULL OR percent BETWEEN 1 AND 100", name="percent"),
    sa.CheckConstraint("pending_hours IS NULL OR pending_hours BETWEEN 1 AND 87600", name="pending_hours"),
    sa.CheckConstraint("max_uses IS NULL OR max_uses > 0", name="max_uses"),
    sa.CheckConstraint("uses >= 0", name="uses"),
    sa.CheckConstraint("min_amount_minor IS NULL OR min_amount_minor > 0", name="min_amount"),
    sa.CheckConstraint("jsonb_typeof(plan_ids) = 'array'", name="plan_ids_array"),
    sa.CheckConstraint("source IN ('bot', 'import')", name="source"),
    sa.Index("uq_promocodes_code_lower", sa.text("lower(code)"), unique=True),
    sa.Index(
        "uq_promocodes_legacy",
        "source",
        "legacy_id",
        unique=True,
        postgresql_where=sa.text("legacy_id IS NOT NULL"),
    ),
)

promo_uses = sa.Table(
    "promo_uses",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("promo_id", sa.BigInteger, sa.ForeignKey("promocodes.id", ondelete="CASCADE"), nullable=False),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("order_id", sa.BigInteger, nullable=True),  # discounts: the order that got the discount
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),
    sa.Column("effect", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("used_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("source IN ('bot', 'link', 'checkout', 'admin', 'import', 'site')", name="source"),
    sa.Index("ix_promo_uses_promo_user", "promo_id", "user_id"),
    sa.Index("ix_promo_uses_user", "user_id"),
    sa.Index(
        "uq_promo_uses_order",
        "promo_id",
        "order_id",
        unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL"),
    ),
)

promo_pending = sa.Table(
    "promo_pending",
    metadata,
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("promo_id", sa.BigInteger, sa.ForeignKey("promocodes.id", ondelete="CASCADE"), nullable=False),
    sa.Column("until", UtcDateTime, nullable=False),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("source IN ('bot', 'link', 'admin', 'import')", name="source"),
    sa.Index("ix_promo_pending_until", "until"),
)
