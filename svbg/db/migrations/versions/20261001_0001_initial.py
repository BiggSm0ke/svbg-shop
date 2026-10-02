"""Initial schema: platform, settings, errors, jobs, content and UI tables.

Generated from ``svbg.db.meta.metadata`` the way ``alembic revision --autogenerate`` does against an empty
PostgreSQL 17 database, then reviewed by hand. ``tests/e2e/test_migrations.py`` checks that this revision
produces exactly the same tables, columns, constraints and indexes as ``svbg.db.schema.create_schema``.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

# UUIDv7 computed by PostgreSQL (same expression as ``svbg.core.tables``; frozen here on purpose).
_UUID7_SQL = (
    "(encode(set_bit(set_bit(overlay(uuid_send(gen_random_uuid()) placing "
    "substring(int8send(floor(extract(epoch from clock_timestamp()) * 1000)::bigint) from 3) "
    "from 1 for 6), 52, 1), 53, 1), 'hex')::uuid)::text"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_minor IS NULL OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name=op.f("ck_admin_audit_money_reason"),
        ),
        sa.CheckConstraint("length(action) > 0", name=op.f("ck_admin_audit_action_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit")),
    )
    op.create_index("ix_admin_audit_actor_ts", "admin_audit", ["actor_id", "ts"], unique=False)
    op.create_index(
        "ix_admin_audit_batch",
        "admin_audit",
        ["batch_id"],
        unique=False,
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    op.create_index(
        "ix_admin_audit_target",
        "admin_audit",
        ["target"],
        unique=False,
        postgresql_where=sa.text("target IS NOT NULL"),
    )
    op.create_index("ix_admin_audit_ts", "admin_audit", ["ts"], unique=False)
    op.create_table(
        "attention_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("fix_action", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("severity IN ('info', 'warn', 'error')", name=op.f("ck_attention_items_severity")),
        sa.CheckConstraint("length(dedup_key) > 0", name=op.f("ck_attention_items_dedup_key_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attention_items")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_attention_items_dedup_key")),
    )
    op.create_index(
        "ix_attention_items_open",
        "attention_items",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_table(
        "config_meta",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_config_meta")),
    )
    op.create_table(
        "content_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("old", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor", sa.BigInteger(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_audit")),
    )
    op.create_index("ix_content_audit_batch", "content_audit", ["batch_id"], unique=False)
    op.create_index("ix_content_audit_ts", "content_audit", ["ts"], unique=False)
    op.create_table(
        "error_groups",
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("place", sa.Text(), nullable=False),
        sa.Column("module", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), server_default="error", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("users_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "sample",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chat_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("episode_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reopened", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("severity IN ('info', 'warn', 'error')", name=op.f("ck_error_groups_severity")),
        sa.CheckConstraint("status IN ('open', 'muted', 'resolved')", name=op.f("ck_error_groups_status")),
        sa.PrimaryKeyConstraint("fingerprint", name=op.f("pk_error_groups")),
    )
    op.create_index("ix_error_groups_last_seen", "error_groups", ["last_seen"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("queue", sa.Text(), server_default="default", nullable=False),
        sa.Column("lane", sa.Text(), server_default="background", nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="ready", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="10", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("ordering_key", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("caused_by", sa.Text(), nullable=True),
        sa.Column("rerun", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("kind <> ''", name=op.f("ck_jobs_kind")),
        sa.CheckConstraint("lane IN ('interactive', 'background')", name=op.f("ck_jobs_lane")),
        sa.CheckConstraint("status IN ('ready', 'running', 'done', 'dead')", name=op.f("ck_jobs_status")),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts")),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_finished_updated_at",
        "jobs",
        ["status", "updated_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('done', 'dead')"),
    )
    op.create_index(
        "ix_jobs_ordering_key_active",
        "jobs",
        ["ordering_key", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('ready', 'running') AND ordering_key IS NOT NULL"),
    )
    op.create_index(
        "ix_jobs_ready_lane_next_run_at",
        "jobs",
        ["lane", "next_run_at"],
        unique=False,
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "ix_jobs_running_locked_until",
        "jobs",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_jobs_dedup_key_active",
        "jobs",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('ready', 'running') AND dedup_key IS NOT NULL"),
    )
    op.create_table(
        "media",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column(
            "file_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("jsonb_typeof(file_ids) = 'object'", name=op.f("ck_media_file_ids_object")),
        sa.CheckConstraint("kind IN ('photo', 'animation', 'video', 'document')", name=op.f("ck_media_kind")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_media_sha256_hex")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media")),
        sa.UniqueConstraint("sha256", name=op.f("uq_media_sha256")),
    )
    op.create_table(
        "scheduler_state",
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("task", name=op.f("pk_scheduler_state")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("length(key) > 0", name=op.f("ck_settings_key_not_empty")),
        sa.CheckConstraint("length(source) > 0", name=op.f("ck_settings_source_not_empty")),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "settings_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("old", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("length(batch_id) > 0", name=op.f("ck_settings_audit_batch_not_empty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings_audit")),
    )
    op.create_index("ix_settings_audit_batch", "settings_audit", ["batch_id"], unique=False)
    op.create_index("ix_settings_audit_key_ts", "settings_audit", ["key", "ts"], unique=False)
    op.create_index("ix_settings_audit_ts", "settings_audit", ["ts"], unique=False)
    op.create_table(
        "short_tokens",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(token) BETWEEN 8 AND 32", name=op.f("ck_short_tokens_token_len")),
        sa.PrimaryKeyConstraint("token", name=op.f("pk_short_tokens")),
    )
    op.create_index("ix_short_tokens_expires_at", "short_tokens", ["expires_at"], unique=False)
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "public_id",
            sa.Text(),
            server_default=sa.text(_UUID7_SQL),
            nullable=False,
        ),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.Column(
            "perms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("jsonb_typeof(perms) = 'array'", name=op.f("ck_users_perms_array")),
        sa.CheckConstraint("role IN ('user', 'support', 'admin', 'owner')", name=op.f("ck_users_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("public_id", name=op.f("uq_users_public_id")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_index(
        "ix_users_staff_role", "users", ["role"], unique=False, postgresql_where=sa.text("role <> 'user'")
    )
    op.create_table(
        "error_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["fingerprint"],
            ["error_groups.fingerprint"],
            name=op.f("fk_error_events_fingerprint_error_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_error_events")),
    )
    op.create_index("ix_error_events_fingerprint_id", "error_events", ["fingerprint", "id"], unique=False)
    op.create_index(
        "ix_error_events_fingerprint_user", "error_events", ["fingerprint", "user_id"], unique=False
    )
    op.create_index("ix_error_events_ts", "error_events", ["ts"], unique=False)
    op.create_table(
        "screens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), server_default=sa.text("'custom'"), nullable=False),
        sa.Column(
            "title",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "body",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("media_id", sa.BigInteger(), nullable=True),
        sa.Column("media_mode", sa.Text(), server_default=sa.text("'attach'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "code IS NULL OR code ~ '^[a-z][a-z0-9_]{0,31}$'", name=op.f("ck_screens_code_format")
        ),
        sa.CheckConstraint("kind = 'custom' OR code IS NOT NULL", name=op.f("ck_screens_system_has_code")),
        sa.CheckConstraint("kind IN ('system', 'custom')", name=op.f("ck_screens_kind")),
        sa.CheckConstraint("media_mode IN ('attach', 'preview')", name=op.f("ck_screens_media_mode")),
        sa.ForeignKeyConstraint(
            ["media_id"], ["media.id"], name=op.f("fk_screens_media_id_media"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screens")),
        sa.UniqueConstraint("code", name=op.f("uq_screens_code")),
    )
    op.create_table(
        "ui_state",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("main_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("main_shape", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("awaiting", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pending_intent", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_ui_state_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_ui_state")),
    )
    op.create_table(
        "user_identities",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(provider) > 0 AND length(subject) > 0", name=op.f("ck_user_identities_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_identities_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("provider", "subject", name="pk_user_identities"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"], unique=False)
    op.create_table(
        "screen_buttons",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("screen_id", sa.BigInteger(), nullable=False),
        sa.Column("system_key", sa.Text(), nullable=True),
        sa.Column("row", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sort", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("label", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("icon_custom_emoji_id", sa.Text(), nullable=True),
        sa.Column("style", sa.Text(), nullable=True),
        sa.Column("action", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("visible_if", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("jsonb_typeof(label) = 'object'", name=op.f("ck_screen_buttons_label_object")),
        sa.CheckConstraint(
            "style IS NULL OR style IN ('primary', 'success', 'danger')", name=op.f("ck_screen_buttons_style")
        ),
        sa.CheckConstraint("row >= 0 AND row < 100", name=op.f("ck_screen_buttons_row_range")),
        sa.ForeignKeyConstraint(
            ["screen_id"],
            ["screens.id"],
            name=op.f("fk_screen_buttons_screen_id_screens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screen_buttons")),
    )
    op.create_index(
        "ix_screen_buttons_screen_order", "screen_buttons", ["screen_id", "row", "sort"], unique=False
    )
    op.create_index(
        "uq_screen_buttons_system_key",
        "screen_buttons",
        ["screen_id", "system_key"],
        unique=True,
        postgresql_where=sa.text("system_key IS NOT NULL"),
    )


def downgrade() -> None:
    # Indexes and constraints go with their tables; the pg_trgm extension is left in place (it may be shared).
    op.drop_table("screen_buttons")
    op.drop_table("user_identities")
    op.drop_table("ui_state")
    op.drop_table("screens")
    op.drop_table("error_events")
    op.drop_table("users")
    op.drop_table("short_tokens")
    op.drop_table("settings_audit")
    op.drop_table("settings")
    op.drop_table("scheduler_state")
    op.drop_table("media")
    op.drop_table("jobs")
    op.drop_table("error_groups")
    op.drop_table("content_audit")
    op.drop_table("config_meta")
    op.drop_table("attention_items")
    op.drop_table("admin_audit")
