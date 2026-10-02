"""UI engine tables: per-user UI state and short tokens for long callback arguments."""

from __future__ import annotations

import sqlalchemy as sa

import svbg.core.tables  # noqa: F401 - ``users`` must be on the metadata for the foreign key
from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["short_tokens", "ui_state"]

# One row per user: the "main" message that screens edit in place, pending text input (forms) and an
# intent that must survive onboarding (deep links, 07 §2.4.4).
ui_state = sa.Table(
    "ui_state",
    metadata,
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("chat_id", sa.BigInteger, nullable=True),
    sa.Column("main_msg_id", sa.BigInteger, nullable=True),
    # Shape of the main message ({"k": "text"|"photo"|..., "m": media key}) so the renderer can pick
    # editMessageText / editMessageMedia without asking Telegram. Extension over the stage-0 contract.
    sa.Column("main_shape", JSONB, nullable=True),
    sa.Column("awaiting", JSONB, nullable=True),
    sa.Column("pending_intent", JSONB, nullable=True),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
)

# Callback arguments that do not fit into 64 bytes: ``v1:<screen>:<action>:~<token>``.
short_tokens = sa.Table(
    "short_tokens",
    metadata,
    sa.Column("token", sa.Text, primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("expires_at", UtcDateTime, nullable=False),
    sa.CheckConstraint("length(token) BETWEEN 8 AND 32", name="token_len"),
    sa.Index("ix_short_tokens_expires_at", "expires_at"),
)
