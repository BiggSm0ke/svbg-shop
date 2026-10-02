"""Billing tables (04 §5 «Деньги», 07 §4.5, 05 §3.3 X4).

* ``orders`` — purchases (``new | renew | change | addon_devices``) and wallet top-ups (``topup``). Purchase:
  ``draft → awaiting_funds → paid → fulfilled`` (+ ``canceled``, ``expired``, ``held``); top-up:
  ``awaiting_payment → credited`` (a late payment wins: ``canceled``/``expired`` top-ups are still credited).
  ``snapshot`` freezes the price, discounts and plan; ``ui_ref`` is the chat message that shows the purchase
  (edited in place when it completes). At most one ``awaiting_funds`` purchase per user — enforced by a
  partial unique index, not only by code.
* ``order_items`` — positions of an order (X4): ``plan_period``, ``devices`` and module item types.
* ``wallet_ledger`` — every movement of ``users.wallet_minor``; ``UNIQUE(user_id, reason, ref_type, ref_id)``
  makes each credit / debit idempotent; ``balance_after`` is the balance right after the entry (never < 0).
* ``manual_receipts`` — receipts of manual (bank transfer) payments: the admin card and its decision (CAS).

``users.wallet_minor`` itself belongs to ``users`` (core); billing reads and writes it through
:data:`users_wallet` (a lightweight table clause), so this module never redefines core's table.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "ITEM_STATUSES",
    "ORDER_KINDS",
    "ORDER_STATUSES",
    "PURCHASE_KINDS",
    "PURCHASE_STATUSES",
    "RECEIPT_STATUSES",
    "TOPUP_STATUSES",
    "UI_STAGES",
    "manual_receipts",
    "order_items",
    "orders",
    "users_wallet",
    "wallet_ledger",
]

PURCHASE_KINDS: tuple[str, ...] = ("new", "renew", "change", "addon_devices")
#: ``addon_lte``: an LTE pack bought for the current period (``svbg.ext.lte.packs``).
ORDER_KINDS: tuple[str, ...] = (*PURCHASE_KINDS, "addon_lte", "topup")
PURCHASE_STATUSES: tuple[str, ...] = (
    "draft",
    "awaiting_funds",
    "paid",
    "fulfilled",
    "canceled",
    "expired",
    "held",
)
TOPUP_STATUSES: tuple[str, ...] = ("awaiting_payment", "credited", "canceled", "expired")
ORDER_STATUSES: tuple[str, ...] = (
    "draft",
    "awaiting_funds",
    "awaiting_payment",
    "paid",
    "fulfilled",
    "credited",
    "canceled",
    "expired",
    "held",
)
ITEM_STATUSES: tuple[str, ...] = ("pending", "fulfilled", "refunded")
RECEIPT_STATUSES: tuple[str, ...] = ("submitted", "confirmed", "rejected")
#: How far the purchase message got: ``connecting`` («✅ Оплачено, подключаем…»), then ``done``
#: («🔗 Подключиться» is shown).
UI_STAGES: tuple[str, ...] = ("connecting", "done")


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    cond = f"{column} IN ({quoted})"
    return f"{column} IS NULL OR {cond}" if nullable else cond


# Same UUIDv7-by-SQL expression as ``users.public_id`` (svbg.core.tables).
_UUID7_SQL = (
    "(encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid()) placing "
    "substring(int8send(floor(extract(epoch from clock_timestamp()) * 1000)::bigint) from 3) "
    "from 1 for 6), 52, 1), 53, 1), 'hex')::uuid)::text"
)

#: ``users`` columns billing touches (``wallet_minor`` is added to core's ``users`` by the stage 2 migration).
users_wallet = sa.table(
    "users",
    sa.column("id", sa.BigInteger),
    sa.column("telegram_id", sa.BigInteger),
    sa.column("wallet_minor", sa.BigInteger),
    sa.column("banned_at", UtcDateTime),
    sa.column("language", sa.Text),
    sa.column("role", sa.Text),
)

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("public_id", sa.Text, nullable=False, unique=True, server_default=sa.text(_UUID7_SQL)),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    # Purchase: the price paid from the wallet. Top-up: the amount credited to the wallet.
    sa.Column("total_minor", sa.BigInteger, nullable=False),
    sa.Column(
        "parent_order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    ),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("plan_id", sa.BigInteger, nullable=True),  # informational; the snapshot is what was bought
    sa.Column("snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("autocomplete_until", UtcDateTime, nullable=True),
    sa.Column("ui_ref", JSONB(none_as_null=True), nullable=True),  # {"chat_id", "message_id", "at"}
    sa.Column("ui_stage", sa.Text, nullable=True),
    sa.Column("note", sa.Text, nullable=True),  # short reason of held / canceled (owner-facing)
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("paid_at", UtcDateTime, nullable=True),
    sa.Column("fulfilled_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("kind", ORDER_KINDS), name="kind"),
    sa.CheckConstraint(_in("status", ORDER_STATUSES), name="status"),
    sa.CheckConstraint(
        "(kind = 'topup' AND " + _in("status", TOPUP_STATUSES) + ") OR "
        "(kind <> 'topup' AND " + _in("status", PURCHASE_STATUSES) + ")",
        name="status_of_kind",
    ),
    sa.CheckConstraint("total_minor >= 0 AND (kind <> 'topup' OR total_minor > 0)", name="total"),
    sa.CheckConstraint("parent_order_id IS NULL OR kind = 'topup'", name="parent_only_topup"),
    sa.CheckConstraint("parent_order_id IS NULL OR parent_order_id <> id", name="parent_not_self"),
    sa.CheckConstraint(
        "status <> 'awaiting_funds' OR autocomplete_until IS NOT NULL", name="waiting_has_window"
    ),
    sa.CheckConstraint(_in("ui_stage", UI_STAGES, nullable=True), name="ui_stage"),
    sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
    sa.CheckConstraint("ui_ref IS NULL OR jsonb_typeof(ui_ref) = 'object'", name="ui_ref_object"),
    sa.CheckConstraint("length(currency) BETWEEN 3 AND 8", name="currency_len"),
    sa.Index("ix_orders_user_created", "user_id", "created_at"),
    sa.Index("ix_orders_parent", "parent_order_id", postgresql_where=sa.text("parent_order_id IS NOT NULL")),
    # 07 §4.5: one purchase per user waits for money; a new one cancels the previous (the index is the proof).
    sa.Index(
        "uq_orders_user_awaiting_funds",
        "user_id",
        unique=True,
        postgresql_where=sa.text("status = 'awaiting_funds'"),
    ),
    # Sweeper: waiting purchases whose window is over.
    sa.Index(
        "ix_orders_awaiting_until",
        "autocomplete_until",
        postgresql_where=sa.text("status = 'awaiting_funds'"),
    ),
)

order_items = sa.Table(
    "order_items",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
    sa.Column("position", sa.SmallInteger, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    sa.UniqueConstraint("order_id", "position", name="uq_order_items_order_position"),
    sa.CheckConstraint(_in("status", ITEM_STATUSES), name="status"),
    sa.CheckConstraint("amount_minor >= 0", name="amount"),
    sa.CheckConstraint("length(type) BETWEEN 1 AND 32", name="type_len"),
    sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
)

wallet_ledger = sa.Table(
    "wallet_ledger",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("balance_after", sa.BigInteger, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("ref_type", sa.Text, nullable=False),
    sa.Column("ref_id", sa.Text, nullable=False),
    sa.Column("actor_id", sa.BigInteger, nullable=True),  # users.id of the admin (admin_adjust)
    sa.Column("note", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("user_id", "reason", "ref_type", "ref_id", name="uq_wallet_ledger_ref"),
    sa.CheckConstraint("amount_minor <> 0", name="amount_not_zero"),
    sa.CheckConstraint("balance_after >= 0", name="balance_after"),
    sa.CheckConstraint("length(reason) BETWEEN 1 AND 32", name="reason_len"),
    sa.CheckConstraint(
        "length(ref_type) BETWEEN 1 AND 32 AND length(ref_id) BETWEEN 1 AND 128", name="ref_len"
    ),
    sa.Index("ix_wallet_ledger_user_id", "user_id", "id"),
    sa.Index("ix_wallet_ledger_ref", "ref_type", "ref_id"),
)

manual_receipts = sa.Table(
    "manual_receipts",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "payment_id", sa.Text, sa.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, unique=True
    ),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),  # the invoice amount (what the user should pay)
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("file_id", sa.Text, nullable=True),  # Telegram file id of the receipt photo / document
    sa.Column("comment", sa.Text, nullable=True),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'submitted'")),
    sa.Column(
        "card_ref", JSONB(none_as_null=True), nullable=True
    ),  # the admin card {"chat_id", "message_id"}
    sa.Column("decided_by", sa.BigInteger, nullable=True),  # users.id of the admin
    sa.Column("decided_amount_minor", sa.BigInteger, nullable=True),
    sa.Column("decision_reason", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("decided_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("status", RECEIPT_STATUSES), name="status"),
    sa.CheckConstraint("amount_minor > 0", name="amount"),
    sa.CheckConstraint("status = 'submitted' OR decided_at IS NOT NULL", name="decided_has_time"),
    sa.CheckConstraint(
        "status <> 'rejected' OR length(btrim(coalesce(decision_reason, ''))) > 0", name="reject_reason"
    ),
    sa.Index("ix_manual_receipts_open", "created_at", postgresql_where=sa.text("status = 'submitted'")),
)
