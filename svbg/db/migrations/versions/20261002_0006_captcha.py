"""Entry captcha: ``users.captcha_passed_at`` (``svbg.tg.user.captcha``).

Every user already in the database is marked passed (``now()``): the captcha is for people who come after the
update, nobody who already uses the bot has to solve it. ``tests/e2e/test_migrations.py`` checks that
``upgrade head`` produces exactly the schema of ``svbg.db.schema.create_schema``. Downgrade drops the column.

Revision ID: 0006_captcha
Revises: 0005_tickets
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_captcha"
down_revision: str | None = "0005_tickets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("captcha_passed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET captcha_passed_at = now() WHERE captcha_passed_at IS NULL")


def downgrade() -> None:
    op.drop_column("users", "captcha_passed_at")
