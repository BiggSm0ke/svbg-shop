"""Payment core tables (07 §4, 04 §5 «Деньги», D12–D14).

* ``payment_instances`` — one row per configured cash desk: the provider (plugin slug), the instance slug used
  by the ``PAY_<SLUG>_*`` settings, switches, the provider config as **encrypted** JSON (``enc:v1:``), the
  secret webhook token of the URL ``/webhooks/pay/{id}/{token}`` (encrypted as well) and the outgoing proxy
  (D13, encrypted: it may carry a password). ``kv`` is the plugin's small key-value store (``ctx.kv``).
* ``payments`` — one row per invoice. ``id`` is the opaque UUIDv7 — the only identifier a provider ever sees.
  ``UNIQUE(instance_id, external_id)`` makes crediting idempotent; the status machine is
  ``pending → paid | expired | canceled | failed | mismatch``, ``paid → refunded``, and a late payment wins:
  ``pending | expired | canceled | failed → paid`` (CAS in :mod:`svbg.payments.core`). ``next_check_at`` /
  ``check_step`` / ``checks_used`` / ``poll_plan`` drive the reconciler and the presence poller (D14).
* ``payment_events`` — every webhook (accepted or rejected with a reason) with a shortened body; TTL 90 days.
  Accepted bodies are deduplicated by ``sha256(body)`` per instance (a partial unique index), rejected ones
  are not, so a provider retry signed with a fresh timestamp is never mistaken for a replay.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = [
    "ACCEPTED_OUTCOMES",
    "EVENT_OUTCOMES",
    "LATE_PAYABLE",
    "PAYMENT_STATUSES",
    "POLL_PLANS",
    "payment_events",
    "payment_instances",
    "payments",
]

#: Payment statuses (04 §5).
PAYMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "paid",
    "expired",
    "canceled",
    "failed",
    "refunded",
    "mismatch",
)
#: Statuses from which a confirmed payment still becomes ``paid`` («поздняя оплата побеждает», D12).
LATE_PAYABLE: tuple[str, ...] = ("pending", "expired", "canceled", "failed")
#: How the reconciler checks a pending payment (D14): webhook + 3 safety checks, or presence polling + decay.
POLL_PLANS: tuple[str, ...] = ("domain", "nodomain")
#: What happened to a webhook / status report.
EVENT_OUTCOMES: tuple[str, ...] = (
    "applied",  # the state change was applied (or confirmed an already applied one)
    "duplicate",  # the same body was accepted before
    "ignored",  # authentic, but nothing to do (ping, intermediate status, a status that cannot regress)
    "verify_queued",  # weak authentication: the status is re-read from the provider by a job
    "mismatch",  # amount or currency differ from the invoice: nothing credited, alert raised
    "unknown_payment",  # authentic, but no such invoice
    "stale",  # outside the freshness window (replay or wrong clock): 401
    "test_rejected",  # test-mode event on a live instance
    "bad_signature",  # authentication failed (token was right): 401
    "malformed",  # authentic transport, unreadable payload
)
#: Outcomes of authentic, processed events: these take part in the ``sha256(body)`` deduplication.
ACCEPTED_OUTCOMES: tuple[str, ...] = (
    "applied",
    "ignored",
    "verify_queued",
    "mismatch",
    "unknown_payment",
)


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

payment_instances = sa.Table(
    "payment_instances",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("provider", sa.Text, nullable=False),  # plugin slug (Manifest.slug)
    sa.Column("slug", sa.Text, nullable=False, unique=True),  # PAY_<SLUG>_*
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("method_kinds", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("currencies", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("min_minor", sa.BigInteger, nullable=True),
    sa.Column("max_minor", sa.BigInteger, nullable=True),
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("100")),
    sa.Column("config", sa.Text, nullable=False),  # enc:v1:<json>
    sa.Column("webhook_token", sa.Text, nullable=False),  # enc:v1:<token>
    sa.Column("proxy_url", sa.Text, nullable=True),  # enc:v1:<url>
    sa.Column("kv", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("slug ~ '^[a-z][a-z0-9_]{0,31}$'", name="slug_format"),
    sa.CheckConstraint("length(provider) BETWEEN 1 AND 32", name="provider_len"),
    sa.CheckConstraint("jsonb_typeof(method_kinds) = 'array'", name="method_kinds_array"),
    sa.CheckConstraint("jsonb_typeof(currencies) = 'array'", name="currencies_array"),
    sa.CheckConstraint("jsonb_typeof(kv) = 'object'", name="kv_object"),
    sa.CheckConstraint("min_minor IS NULL OR max_minor IS NULL OR min_minor <= max_minor", name="min_le_max"),
)

payments = sa.Table(
    "payments",
    metadata,
    sa.Column("id", sa.Text, primary_key=True, server_default=sa.text(_UUID7_SQL)),
    sa.Column(
        "instance_id",
        sa.BigInteger,
        sa.ForeignKey("payment_instances.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("order_id", sa.BigInteger, nullable=True),  # orders.id (billing); NULL = no order
    sa.Column("external_id", sa.Text, nullable=True),
    sa.Column("merchant_ref", sa.Text, nullable=True),  # imported legacy reference (06 §2.4.2)
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    sa.Column("amount_minor", sa.BigInteger, nullable=False),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("paid_amount_minor", sa.BigInteger, nullable=True),
    sa.Column("paid_currency", sa.Text, nullable=True),
    sa.Column("method_kind", sa.Text, nullable=True),
    sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("checkout", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("is_test", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("is_imported", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("confirmed_by", sa.BigInteger, nullable=True),  # users.id of the admin (manual payments)
    sa.Column("error", sa.Text, nullable=True),  # short owner-facing reason of failed / mismatch
    sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("expires_at", UtcDateTime, nullable=True),  # provider's invoice expiry (informational)
    sa.Column("paid_at", UtcDateTime, nullable=True),
    sa.Column("poll_plan", sa.Text, nullable=True),  # NULL = never polled (no fetch_status / no invoice)
    sa.Column("next_check_at", UtcDateTime, nullable=True),
    sa.Column("check_step", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("checks_used", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("last_checked_at", UtcDateTime, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.UniqueConstraint("instance_id", "external_id", name="uq_payments_instance_external"),
    sa.CheckConstraint(_in("status", PAYMENT_STATUSES), name="status"),
    sa.CheckConstraint(_in("poll_plan", POLL_PLANS, nullable=True), name="poll_plan"),
    sa.CheckConstraint("amount_minor > 0", name="amount_positive"),
    sa.CheckConstraint("paid_amount_minor IS NULL OR paid_amount_minor >= 0", name="paid_amount"),
    sa.CheckConstraint("length(currency) BETWEEN 3 AND 8", name="currency_len"),
    sa.CheckConstraint("status <> 'paid' OR paid_at IS NOT NULL", name="paid_has_time"),
    sa.CheckConstraint("check_step >= 0 AND checks_used >= 0", name="poll_counters"),
    sa.CheckConstraint("jsonb_typeof(checkout) = 'object'", name="checkout_object"),
    sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
    sa.Index("ix_payments_user_created", "user_id", "created_at"),
    sa.Index("ix_payments_order_id", "order_id", postgresql_where=sa.text("order_id IS NOT NULL")),
    # Reconciler / poller: due pending payments.
    sa.Index(
        "ix_payments_due",
        "next_check_at",
        postgresql_where=sa.text("status = 'pending' AND next_check_at IS NOT NULL"),
    ),
)

payment_events = sa.Table(
    "payment_events",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "instance_id",
        sa.BigInteger,
        sa.ForeignKey("payment_instances.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("payment_id", sa.Text, nullable=True),  # no FK: an event may name an unknown payment
    sa.Column("external_id", sa.Text, nullable=True),
    sa.Column("body_sha256", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=True),  # provider state as reported (PaymentState value)
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("accepted", sa.Boolean, nullable=False),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column("signed_at", UtcDateTime, nullable=True),
    sa.Column("summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("received_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint(_in("outcome", EVENT_OUTCOMES), name="outcome"),
    sa.CheckConstraint("length(body_sha256) = 64", name="body_sha256_len"),
    sa.CheckConstraint("jsonb_typeof(summary) = 'object'", name="summary_object"),
    sa.Index(
        "uq_payment_events_body",
        "instance_id",
        "body_sha256",
        unique=True,
        postgresql_where=sa.text("accepted"),
    ),
    sa.Index("ix_payment_events_received_at", "received_at"),
    sa.Index("ix_payment_events_payment", "payment_id", postgresql_where=sa.text("payment_id IS NOT NULL")),
)

#: Predicate of ``uq_payment_events_body`` (``ON CONFLICT`` must repeat it literally).
EVENT_DEDUP_PREDICATE = "accepted"
