"""Support tickets (07 §2.4.6): ``tickets``, ``ticket_messages`` (``svbg.support.tables``).

``tests/e2e/test_migrations.py`` checks that ``upgrade head`` produces exactly the schema of
``svbg.db.schema.create_schema``. Downgrade drops both tables.

Revision ID: 0005_tickets
Revises: 0004_stage3_4
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_tickets"
down_revision: str | None = "0004_stage3_4"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("first_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_tickets_status")),
        sa.CheckConstraint(
            "thread_id IS NULL OR chat_id IS NOT NULL", name=op.f("ck_tickets_thread_has_chat")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_tickets_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_tickets_closed_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(
        "uq_tickets_open_user",
        "tickets",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_tickets_user", "tickets", ["user_id", "id"], unique=False)
    op.create_index("ix_tickets_thread", "tickets", ["chat_id", "thread_id"], unique=False)
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("dir", sa.Text(), nullable=False),
        sa.Column("user_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("group_msg_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("dir IN ('in', 'out')", name=op.f("ck_ticket_messages_dir")),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_messages_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
    )
    op.create_index("ix_ticket_messages_ticket", "ticket_messages", ["ticket_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ticket_messages_ticket", table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index("ix_tickets_thread", table_name="tickets")
    op.drop_index("ix_tickets_user", table_name="tickets")
    op.drop_index("uq_tickets_open_user", table_name="tickets", postgresql_where=sa.text("status = 'open'"))
    op.drop_table("tickets")
