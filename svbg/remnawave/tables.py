"""Remnawave sync tables: webhook inbox, import runs, reconciliation state (02 §5.3, §6.2, §7.1).

* ``rw_inbox`` — one row per **distinct** webhook body (``hash = sha256(raw body)``): panel retries resend the
  same bytes, so ``ON CONFLICT DO NOTHING`` is the dedup. Only a slim, secret-free subset of ``data`` is
  stored;
* ``import_runs`` — progress and report of a panel import (resumable by ``cursor``);
* ``rw_sync_state`` — the reconciliation lease ("one pass at a time", lives in the DB, not in memory) and the
  last pass report for «Состояние».
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["IMPORT_MODES", "INBOX_STATUSES", "import_runs", "rw_inbox", "rw_sync_state"]

INBOX_STATUSES: tuple[str, ...] = ("new", "done", "skipped", "error")
IMPORT_MODES: tuple[str, ...] = ("dry_run", "apply", "shadow")
IMPORT_STATUSES: tuple[str, ...] = ("running", "done", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


rw_inbox = sa.Table(
    "rw_inbox",
    metadata,
    sa.Column("hash", sa.Text, primary_key=True),
    sa.Column("ts", UtcDateTime, nullable=False),  # payload.timestamp (the panel's clock)
    sa.Column("received_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("panel_user_id", sa.BigInteger, nullable=True),
    sa.Column("slim", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'new'")),
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("locked_until", UtcDateTime, nullable=True),
    sa.Column("note", sa.Text, nullable=True),
    sa.Column("processed_at", UtcDateTime, nullable=True),
    sa.CheckConstraint(_in("status", INBOX_STATUSES), name="status"),
    sa.CheckConstraint("length(hash) = 64", name="hash_len"),
    # The processor's claim path: new rows in panel-time order.
    sa.Index("ix_rw_inbox_new_ts", "ts", postgresql_where=sa.text("status = 'new'")),
    sa.Index("ix_rw_inbox_received_at", "received_at"),
)

import_runs = sa.Table(
    "import_runs",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("source", sa.Text, nullable=False),  # 'panel' (stage 1); 'remnashop' / 'bedolaga' later
    sa.Column("mode", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'running'")),
    sa.Column("filters", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("started_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("finished_at", UtcDateTime, nullable=True),
    sa.Column("cursor", sa.Text, nullable=True),
    sa.Column("report", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.CheckConstraint(_in("mode", IMPORT_MODES), name="mode"),
    sa.CheckConstraint(_in("status", IMPORT_STATUSES), name="status"),
    sa.Index("ix_import_runs_source_started", "source", "started_at"),
)

rw_sync_state = sa.Table(
    "rw_sync_state",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),  # 'full' | 'fast'
    sa.Column("holder", sa.Text, nullable=True),
    sa.Column("locked_until", UtcDateTime, nullable=True),
    sa.Column("last_started_at", UtcDateTime, nullable=True),
    sa.Column("last_ok_at", UtcDateTime, nullable=True),
    sa.Column("sub_domain", sa.Text, nullable=True),  # configuration.misc.subPublicDomain seen last time
    sa.Column("report", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
)
