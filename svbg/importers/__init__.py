"""Importers from other bots (02 §7, 06 §2): shared bookkeeping tables.

* ``legacy_id_map`` — ``(source, entity, old_id) → new_id`` for every imported row whose new key is not the
old
  one (payments, orders, promo uses, unclaimed panel subscriptions …) and for rows that keep their id (users,
  subscriptions: ``old_id = new_id``) — so a repeated run updates instead of inserting, and the importer can
  tell "its" rows from rows the bot created itself. ``data`` keeps what the importer wrote last (the shadow
  re-run only refreshes a row the bot has not changed since) and per-entity extras that have no column yet
  (06 §2.3: first payment / top-up time, personal discount, restrictions, notification flags);
* ``legacy_transactions`` — the source's money history, read-only (06 §2.4.5): shown in the user card and in
  statistics, never folded into ``wallet_ledger`` (the opening balance already contains it). ``pair_key``
  joins the two legs of a site payment («+ оплата на сайте, заказ tc_…» / «− продление через сайт») so the
  revenue is counted once.

The import runs themselves are rows of :data:`svbg.remnawave.tables.import_runs` (``source='bedolaga'``).
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["legacy_id_map", "legacy_transactions"]

legacy_id_map = sa.Table(
    "legacy_id_map",
    metadata,
    sa.Column("source", sa.Text, nullable=False),  # 'bedolaga'
    sa.Column("entity", sa.Text, nullable=False),  # 'user' | 'subscription' | 'payment' | 'order' | …
    sa.Column("old_id", sa.Text, nullable=False),
    sa.Column("new_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.BigInteger, nullable=True),  # import_runs.id of the last write
    sa.Column("data", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.PrimaryKeyConstraint("source", "entity", "old_id", name="pk_legacy_id_map"),
    sa.CheckConstraint("length(source) BETWEEN 1 AND 32 AND length(entity) BETWEEN 1 AND 32", name="names"),
    sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
    sa.Index("ix_legacy_id_map_new", "source", "entity", "new_id"),
)

legacy_transactions = sa.Table(
    "legacy_transactions",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("legacy_id", sa.Text, nullable=False),
    # No foreign key: the history of a user that was not imported (deleted, no money) is still kept.
    sa.Column("user_id", sa.BigInteger, nullable=True),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),  # signed, as in the source
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=True),
    sa.Column("payment_method", sa.Text, nullable=True),
    sa.Column("external_id", sa.Text, nullable=True),
    sa.Column("is_completed", sa.Boolean, nullable=True),
    sa.Column("pair_key", sa.Text, nullable=True),  # site payment pair (06 §2.4.5): 'tc_…' or 'win:<id>'
    sa.Column("counts_as_revenue", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", UtcDateTime, nullable=True),
    sa.Column("completed_at", UtcDateTime, nullable=True),
    sa.Column("imported_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("source", "legacy_id", name="uq_legacy_transactions_source_legacy_id"),
    sa.CheckConstraint("length(currency) BETWEEN 3 AND 8", name="currency_len"),
    sa.Index("ix_legacy_transactions_user", "user_id", "created_at"),
    sa.Index(
        "ix_legacy_transactions_external", "external_id", postgresql_where=sa.text("external_id IS NOT NULL")
    ),
)
