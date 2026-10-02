"""Pages (04 §5, 07 §2.5): FAQ, rules, offer, consent and the owner's own pages, with versions.

* ``pages`` — the current text per language with Bot API entities (``{lang: {text, entities}}``), ``version``
  (+1 on every change, compare-and-set) and, for the consent page, ``consent_version`` — the version users
  must accept (raised only when the owner asks for a new consent; a typo fix does not re-ask everybody).
* ``page_versions`` — every saved version (history, «вернуть эту версию»).
* ``page_consents`` — which consent version a user accepted, and when.
"""

from __future__ import annotations

import sqlalchemy as sa

from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default

__all__ = ["PAGE_KINDS", "page_consents", "page_versions", "pages"]

PAGE_KINDS: tuple[str, ...] = ("faq", "rules", "offer", "consent", "custom")

pages = sa.Table(
    "pages",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("code", sa.Text, nullable=False, unique=True),
    sa.Column("kind", sa.Text, nullable=False, server_default=sa.text("'custom'")),
    sa.Column("title", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),  # {lang: text}
    sa.Column(
        "body", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    ),  # {lang: {text, entities}}
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("consent_version", sa.Integer, nullable=True),
    sa.Column("updated_by", sa.BigInteger, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("updated_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.CheckConstraint("code ~ '^[a-z][a-z0-9_]{0,23}$'", name="code_format"),
    sa.CheckConstraint("kind IN ('faq', 'rules', 'offer', 'consent', 'custom')", name="kind"),
    sa.CheckConstraint("jsonb_typeof(title) = 'object'", name="title_object"),
    sa.CheckConstraint("jsonb_typeof(body) = 'object'", name="body_object"),
    sa.CheckConstraint("version >= 1", name="version"),
    sa.CheckConstraint(
        "consent_version IS NULL OR consent_version BETWEEN 1 AND version", name="consent_version"
    ),
)

page_versions = sa.Table(
    "page_versions",
    metadata,
    sa.Column("page_id", sa.BigInteger, sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("title", JSONB, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("actor", sa.BigInteger, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.PrimaryKeyConstraint("page_id", "version", name="pk_page_versions"),
)

page_consents = sa.Table(
    "page_consents",
    metadata,
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("page_id", sa.BigInteger, sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("accepted_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.PrimaryKeyConstraint("user_id", "page_id", name="pk_page_consents"),
)
