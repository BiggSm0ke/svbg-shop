"""Tables of the settings module (03 §8.1, stage-0 contract).

* ``settings`` — one row per explicitly set runtime key. No row = the registry default ("default is not
  frozen", 07 §3.3). Secrets are stored as an encrypted JSON string ``"enc:v1:…"``.
* ``settings_audit`` — every change attempt (applied or not). Secret values are stored only as a keyed
  fingerprint plus the encrypted value needed for undo — never in plain text.

Merge base of ``.env`` and the registry version live in ``config_meta`` (owned by the platform module).
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["settings", "settings_audit"]

settings = sa.Table(
    "settings",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("value", JSONB, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_by", sa.BigInteger, nullable=True),
    sa.CheckConstraint("length(key) > 0", name="key_not_empty"),
    sa.CheckConstraint("length(source) > 0", name="source_not_empty"),
)

settings_audit = sa.Table(
    "settings_audit",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("batch_id", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    # Envelope {"v": value, "src": row source} / {"fp": …, "enc": …, "src": …} for secrets;
    # NULL = no row (registry default).
    sa.Column("old", JSONB, nullable=True),
    sa.Column("new", JSONB, nullable=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("actor_id", sa.BigInteger, nullable=True),
    sa.Column("ts", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("applied", sa.Boolean, nullable=False),
    sa.Column("error", sa.Text, nullable=True),
    sa.CheckConstraint("length(batch_id) > 0", name="batch_not_empty"),
    sa.Index("ix_settings_audit_key_ts", "key", "ts"),
    sa.Index("ix_settings_audit_batch", "batch_id"),
    sa.Index("ix_settings_audit_ts", "ts"),
)
