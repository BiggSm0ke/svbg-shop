"""Content tables (07 §2.5): screens, their buttons, media files and the content audit trail."""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["content_audit", "media", "screen_buttons", "screens"]

media = sa.Table(
    "media",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("sha256", sa.Text, nullable=False, unique=True),
    sa.Column("path", sa.Text, nullable=True),  # relative to DATA_DIR/media
    sa.Column("mime", sa.Text, nullable=True),
    sa.Column("size", sa.BigInteger, nullable=True),
    sa.Column("width", sa.Integer, nullable=True),
    sa.Column("height", sa.Integer, nullable=True),
    sa.Column("duration", sa.Integer, nullable=True),
    sa.Column("file_ids", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),  # {bot_id: file_id}
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("kind IN ('photo', 'animation', 'video', 'document')", name="kind"),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    sa.CheckConstraint("jsonb_typeof(file_ids) = 'object'", name="file_ids_object"),
)

screens = sa.Table(
    "screens",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=True, unique=True),
    sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'custom'")),
    sa.Column("title", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column(
        "body", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    ),  # {lang: {text, entities}}
    sa.Column("media_id", sa.BigInteger, sa.ForeignKey("media.id", ondelete="SET NULL"), nullable=True),
    sa.Column("media_mode", sa.Text, nullable=False, server_default=sa.text("'attach'")),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),  # CAS for edits
    sa.Column("updated_by", sa.BigInteger, nullable=True),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("kind IN ('system', 'custom')", name="kind"),
    sa.CheckConstraint("media_mode IN ('attach', 'preview')", name="media_mode"),
    sa.CheckConstraint("code IS NULL OR code ~ '^[a-z][a-z0-9_]{0,31}$'", name="code_format"),
    sa.CheckConstraint("kind = 'custom' OR code IS NOT NULL", name="system_has_code"),
)

screen_buttons = sa.Table(
    "screen_buttons",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "screen_id",
        sa.BigInteger,
        sa.ForeignKey("screens.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("system_key", sa.Text, nullable=True),
    sa.Column("row", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("sort", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("label", JSONB, nullable=False),  # {lang: text}
    sa.Column("icon_custom_emoji_id", sa.Text, nullable=True),
    sa.Column("style", sa.Text, nullable=True),
    sa.Column("action", JSONB, nullable=False),
    sa.Column("visible_if", JSONB, nullable=True),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.CheckConstraint("style IS NULL OR style IN ('primary', 'success', 'danger')", name="style"),
    sa.CheckConstraint("jsonb_typeof(label) = 'object'", name="label_object"),
    sa.CheckConstraint("row >= 0 AND row < 100", name="row_range"),
    sa.Index("ix_screen_buttons_screen_order", "screen_id", "row", "sort"),
    # A system button exists at most once per screen (seeding relies on it).
    sa.Index(
        "uq_screen_buttons_system_key",
        "screen_id",
        "system_key",
        unique=True,
        postgresql_where=sa.text("system_key IS NOT NULL"),
    ),
)

content_audit = sa.Table(
    "content_audit",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("batch_id", sa.Text, nullable=False),
    sa.Column("entity", sa.Text, nullable=False),  # screen | button | media
    sa.Column("entity_id", sa.Text, nullable=True),
    sa.Column("old", JSONB, nullable=True),
    sa.Column("new", JSONB, nullable=True),
    sa.Column("actor", sa.BigInteger, nullable=True),
    sa.Column("ts", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Index("ix_content_audit_batch", "batch_id"),
    sa.Index("ix_content_audit_ts", "ts"),
)
