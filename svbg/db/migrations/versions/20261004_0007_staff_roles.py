"""Custom staff roles: ``staff_roles`` and ``users.staff_role_id`` (``svbg.services.staff_roles``).

Two built-in roles are created: «Администратор» (every core right except the owner's ones and «Команда и
роли») and «Поддержка» (client cards, help with devices and links, tickets). Both also get the «view» rights
of the IP Guard and LTE modules, which every staff member had before. Existing staff move into equivalent
roles without losing anything:

* ``support`` → «Поддержка»;
* ``admin`` → their rights as they worked so far (``*`` expanded, plus the Support column and module views
  they had by rank). The same set as «Администратор» → that role; any other set → a new role «Админ N», one
  per distinct set.

Owners are not touched. The rights are copied into ``users.perms`` (the role is the source of truth, the copy
keeps every reader cheap). ``tests/e2e/test_migrations.py`` checks that ``upgrade head`` produces exactly the
schema of ``svbg.db.schema.create_schema``. Downgrade drops the column and the table (members keep their rank
and rights as classic staff).

Revision ID: 0007_staff_roles
Revises: 0006_captcha
Create Date: 2026-10-04
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_staff_roles"
down_revision: str | None = "0006_captcha"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen copies (svbg.core.perms at the time of this revision).
_ADMIN_COLUMN = (
    "settings.business",
    "plans",
    "promo",
    "payments.confirm",
    "wallet.adjust",
    "subs.grant",
    "payments.refund",
    "broadcast",
    "users.ban",
    "stats",
    "system.view",
    "content.edit",
    "deeplinks",
    "tickets",
    "broadcast.send",
    "users.delete",
)
_BY_RANK = ("users.view", "users.help", "tickets", "ip_guard.view", "lte.view")
_ADMIN_ROLE = "Администратор"
_SUPPORT_ROLE = "Поддержка"


def _array(values: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{v}'" for v in values) + "]::text[]"


def _jsonb(values: list[str]) -> str:
    return "'" + json.dumps(values, ensure_ascii=False) + "'::jsonb"


def upgrade() -> None:
    op.create_table(
        "staff_roles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "perms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name=op.f("ck_staff_roles_name")),
        sa.CheckConstraint("jsonb_typeof(perms) = 'array'", name=op.f("ck_staff_roles_perms_array")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_staff_roles_name")),
    )
    op.add_column("users", sa.Column("staff_role_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_users_staff_role_id_staff_roles"),
        "users",
        "staff_roles",
        ["staff_role_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_users_staff_role_id",
        "users",
        ["staff_role_id"],
        unique=False,
        postgresql_where=sa.text("staff_role_id IS NOT NULL"),
    )

    admin_perms = sorted({*_ADMIN_COLUMN, *_BY_RANK})
    support_perms = sorted(_BY_RANK)
    op.execute(
        "INSERT INTO staff_roles (name, perms) VALUES "
        f"('{_ADMIN_ROLE}', {_jsonb(admin_perms)}), ('{_SUPPORT_ROLE}', {_jsonb(support_perms)})"
    )
    # support → «Поддержка»
    op.execute(
        "UPDATE users SET staff_role_id = r.id, perms = r.perms FROM staff_roles r "
        f"WHERE r.name = '{_SUPPORT_ROLE}' AND users.role = 'support'"
    )
    # admins: the rights they really had (``*`` expanded, the Support column and module views by rank)
    op.execute(
        "UPDATE users SET perms = (SELECT coalesce(jsonb_agg(DISTINCT s.p ORDER BY s.p), '[]'::jsonb) FROM ("
        " SELECT jsonb_array_elements_text(users.perms) AS p"
        f" UNION ALL SELECT unnest({_array(_ADMIN_COLUMN)}) WHERE users.perms @> '[\"*\"]'::jsonb"
        f" UNION ALL SELECT unnest({_array(_BY_RANK)})"
        ") s WHERE s.p <> '*') "
        "WHERE role = 'admin'"
    )
    op.execute(
        "UPDATE users SET staff_role_id = r.id, perms = r.perms FROM staff_roles r "
        f"WHERE r.name = '{_ADMIN_ROLE}' AND users.role = 'admin' "
        "AND users.perms @> r.perms AND r.perms @> users.perms"
    )
    # any other set of rights: a role of its own, «Админ 1», «Админ 2» … (by the oldest member)
    op.execute(
        "INSERT INTO staff_roles (name, perms) "
        "SELECT 'Админ ' || row_number() OVER (ORDER BY g.first_id), g.perms FROM ("
        " SELECT perms, min(id) AS first_id FROM users"
        " WHERE role = 'admin' AND staff_role_id IS NULL GROUP BY perms"
        ") g"
    )
    op.execute(
        "UPDATE users SET staff_role_id = r.id FROM staff_roles r "
        "WHERE users.role = 'admin' AND users.staff_role_id IS NULL "
        "AND r.name LIKE 'Админ %' AND r.perms = users.perms"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_users_staff_role_id", table_name="users", postgresql_where=sa.text("staff_role_id IS NOT NULL")
    )
    op.drop_constraint(op.f("fk_users_staff_role_id_staff_roles"), "users", type_="foreignkey")
    op.drop_column("users", "staff_role_id")
    op.drop_table("staff_roles")
