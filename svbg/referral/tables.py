"""Referral tables (05 §2.3.2, 07 §2.5).

* ``referral_codes`` — one personal code per user (``r_<code>`` deep links). Generated lazily on the first
  open of the «Пригласить» screen; imported Bedolaga codes land here too (``source='import'``);
* ``referrals`` — who invited whom: one row per invited user (the PK makes re-binding impossible), written
  only by :meth:`svbg.referral.service.ReferralService.attach_referrer` (no retro-binding, no self-referral);
* ``referral_rewards`` — one journal for both modes. ``kind='days'``: one row per ``(referred_user_id, side)``
  (structural idempotency: a second worker or a repeated hook hits the unique index); ``kind='wallet_pct'``:
  one row per ``(payment_id, side)`` where ``payment_id`` is the reference of the paid purchase
  (``order:<id>``). ``user_id`` is the recipient of the side (the inviter or the invited user) — the deferred
  sides of a user and the inviter's caps are read by it without a join.

Statuses: ``granted`` (``days`` / ``amount_minor`` hold the fact), ``deferred`` (waits until ``retry_until``
for a subscription or a free slot under the cap), ``expired`` (the window passed, silently), ``denied``
(anti-abuse, ``reason``), ``legacy`` (imported pairs that the new bot must never reward again).
"""

from __future__ import annotations

from typing import Final

import sqlalchemy as sa

from svbg.db.meta import UtcDateTime, metadata, now_default

__all__ = [
    "DAYS_PREDICATE",
    "PCT_PREDICATE",
    "REWARD_KINDS",
    "REWARD_SIDES",
    "REWARD_STATUSES",
    "TABLES",
    "referral_codes",
    "referral_rewards",
    "referrals",
]

REWARD_SIDES: Final[tuple[str, ...]] = ("inviter", "invitee")
REWARD_KINDS: Final[tuple[str, ...]] = ("days", "wallet_pct")
REWARD_STATUSES: Final[tuple[str, ...]] = ("granted", "deferred", "expired", "denied", "legacy")
#: Predicates of the partial unique indexes (``ON CONFLICT`` must repeat them literally).
DAYS_PREDICATE: Final = "kind = 'days'"
PCT_PREDICATE: Final = "kind = 'wallet_pct'"


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


referral_codes = sa.Table(
    "referral_codes",
    metadata,
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'bot'")),  # bot | import | admin
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    # Same alphabet and length as a deep-link value after ``r_`` (svbg.deeplinks.codec).
    sa.CheckConstraint("code ~ '^[A-Za-z0-9_-]{1,62}$'", name="code_format"),
)

referrals = sa.Table(
    "referrals",
    metadata,
    sa.Column(
        "referred_user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("referrer_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("attached_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'link'")),  # link | import | admin
    sa.CheckConstraint("referred_user_id <> referrer_id", name="not_self"),
    sa.Index("ix_referrals_referrer_id", "referrer_id"),
    sa.Index("ix_referrals_attached_at", "attached_at"),
)

referral_rewards = sa.Table(
    "referral_rewards",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "referred_user_id",
        sa.BigInteger,
        sa.ForeignKey("referrals.referred_user_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("days", sa.Integer, nullable=True),
    sa.Column("amount_minor", sa.BigInteger, nullable=True),
    sa.Column("currency", sa.Text, nullable=True),
    sa.Column("payment_id", sa.Text, nullable=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("trigger", sa.Text, nullable=True),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("granted_at", UtcDateTime, nullable=True),
    sa.Column("retry_until", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("side", REWARD_SIDES), name="side"),
    sa.CheckConstraint(_in("kind", REWARD_KINDS), name="kind"),
    sa.CheckConstraint(_in("status", REWARD_STATUSES), name="status"),
    sa.CheckConstraint("days IS NULL OR days > 0", name="days_positive"),
    sa.CheckConstraint("amount_minor IS NULL OR amount_minor > 0", name="amount_positive"),
    sa.CheckConstraint("status <> 'deferred' OR retry_until IS NOT NULL", name="deferred_has_retry"),
    sa.CheckConstraint("kind <> 'wallet_pct' OR payment_id IS NOT NULL", name="pct_has_payment"),
    sa.CheckConstraint("status <> 'granted' OR granted_at IS NOT NULL", name="granted_has_time"),
    sa.Index(
        "uq_referral_rewards_days_side",
        "referred_user_id",
        "side",
        unique=True,
        postgresql_where=sa.text(DAYS_PREDICATE),
    ),
    sa.Index(
        "uq_referral_rewards_pct_payment_side",
        "payment_id",
        "side",
        unique=True,
        postgresql_where=sa.text(PCT_PREDICATE),
    ),
    # Recipient's journal: caps of the inviter (granted in 30 days / ever), screen counters.
    sa.Index("ix_referral_rewards_user", "user_id", "kind", "side", "status"),
    # Deferred sides are few; the hourly review and the "a subscription appeared" hook read only them.
    sa.Index(
        "ix_referral_rewards_deferred",
        "retry_until",
        postgresql_where=sa.text("status = 'deferred'"),
    ),
)

#: Creation order (``referrals`` before ``referral_rewards``).
TABLES: Final[tuple[sa.Table, ...]] = (referral_codes, referrals, referral_rewards)
