"""Custom staff roles: rights by rank vs exactly the role's, members rewritten with the role, the manager
never gives more than they have, owners stay out of reach, every change audited."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from svbg.core.perms import CORE_PERMS, SUPPORT_PERMS, implicit, tier_of
from svbg.services import roles, staff_roles
from svbg.services.roles import Act, Actor, RoleError
from svbg.tg.ui.context import UserCtx
from tests.dbkit import CountingDatabase, add_user, open_db

OWNER_TG = 100
CONF_OWNER_TG = 999


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


def test_rank_rights_and_tiers() -> None:
    assert implicit("users.view") and implicit("tickets") and implicit("ip_guard.view")
    assert not implicit("system.view") and not implicit("stats") and not implicit("roles.manage")
    assert tier_of([]) == "user"
    assert tier_of(SUPPORT_PERMS) == "support" and tier_of(["users.view", "lte.view"]) == "support"
    assert tier_of(["broadcast"]) == "admin" and tier_of(["roles.manage"]) == "admin"


def test_scoped_actor_has_exactly_the_role() -> None:
    classic = Actor(1, 1, "support")
    assert classic.has_perm("users.view") and classic.has_perm("ip_guard.view")
    scoped = Actor(1, 1, "admin", frozenset({"broadcast"}), scoped=True)
    assert scoped.has_perm("broadcast")
    assert not scoped.has_perm("users.view") and not scoped.has_perm("tickets")
    assert not roles.authorize(scoped, Act.USERS_VIEW) and not roles.authorize(scoped, Act.TICKETS)
    manager = Actor(1, 1, "admin", frozenset({"roles.manage"}), scoped=True)
    assert roles.authorize(manager, Act.ROLES_MANAGE)
    assert not roles.authorize(Actor(1, 1, "admin", frozenset({"*", "roles.manage"})), Act.ROLES_MANAGE)
    ctx = UserCtx(1, role="admin", perms=frozenset({"broadcast"}), staff_role=7)
    assert ctx.has_perm("broadcast") and not ctx.has_perm("users.view") and ctx.scoped
    assert roles.actor_of(ctx).scoped and not roles.actor_of(ctx).has_perm("users.view")
    star = UserCtx(1, role="admin", perms=frozenset({"*"}))
    assert star.has_perm("stats") and star.has_perm("users.view") and not star.has_perm("roles.manage")


async def _people(db: CountingDatabase) -> dict[str, int]:
    ids = {
        "owner": await add_user(db, OWNER_TG, "owner"),
        "conf": await add_user(db, CONF_OWNER_TG, "user"),
        "a": await add_user(db, 201),
        "b": await add_user(db, 202),
        "star": await add_user(db, 203, "admin"),
    }
    await db.raw("update users set perms = '[\"*\"]'::jsonb where id = $1", ids["star"])
    return ids


async def _actor(db: CountingDatabase, user_id: int) -> Actor | None:
    async with db.read() as conn:
        return await roles.load_actor(conn, user_id=user_id, owner_ids={CONF_OWNER_TG})


async def _row(db: CountingDatabase, user_id: int) -> Any:
    return (await db.raw("select role, perms, staff_role_id from users where id = $1", user_id))[0]


def _list(value: Any) -> list[str]:
    return value if isinstance(value, list) else json.loads(value)


async def test_role_lifecycle_rewrites_members_and_audits(db: CountingDatabase) -> None:
    ids = await _people(db)
    owner = await _actor(db, ids["owner"])
    async with db.tx() as conn:
        out = await staff_roles.create_role(conn, owner, "  Модератор  ", ["users.view", "tickets"])
    role = out.role
    assert role is not None and role.name == "Модератор" and role.tier == "support"
    async with db.tx() as conn:
        with pytest.raises(RoleError, match="уже есть"):
            await staff_roles.create_role(conn, owner, "модератор")
    async with db.tx() as conn:
        out = await staff_roles.assign(conn, owner, ids["a"], role.id, owner_ids={CONF_OWNER_TG})
    assert [m.user_id for m in out.affected] == [ids["a"]]
    row = await _row(db, ids["a"])
    assert row["role"] == "support" and row["staff_role_id"] == role.id
    assert sorted(_list(row["perms"])) == ["tickets", "users.view"]

    async with db.tx() as conn:  # a right above Support lifts every member to «admin»
        out = await staff_roles.toggle_perm(conn, owner, role.id, "broadcast")
    assert [(m.user_id, m.role) for m in out.affected] == [(ids["a"], "admin")]
    member = await _actor(db, ids["a"])
    assert member is not None and member.scoped and member.has_perm("broadcast")
    assert not member.has_perm("users.help")

    async with db.tx() as conn:
        out = await staff_roles.rename_role(conn, owner, role.id, "Старший модератор")
        assert out.role is not None and out.role.name == "Старший модератор"
        out = await staff_roles.delete_role(conn, owner, role.id)
    assert [m.user_id for m in out.affected] == [ids["a"]]
    row = await _row(db, ids["a"])
    assert (row["role"], _list(row["perms"]), row["staff_role_id"]) == ("user", [], None)
    actions = [r["action"] for r in await db.raw("select action from admin_audit order by id")]
    assert actions == ["role.create", "role.assign", "role.perms", "role.rename", "role.delete"]


async def test_a_manager_never_gives_more_than_they_have(db: CountingDatabase) -> None:
    ids = await _people(db)
    owner = await _actor(db, ids["owner"])
    async with db.tx() as conn:
        lead = (
            await staff_roles.create_role(conn, owner, "Тимлид", ["roles.manage", "broadcast", "users.view"])
        ).role
        wide = (await staff_roles.create_role(conn, owner, "Админ", list(CORE_PERMS))).role
        narrow = (await staff_roles.create_role(conn, owner, "Рассылки", ["broadcast"])).role
        assert lead is not None and wide is not None and narrow is not None
        await staff_roles.assign(conn, owner, ids["a"], lead.id)
    manager = await _actor(db, ids["a"])
    assert manager is not None and manager.scoped

    async def refused(op: Any, match: str) -> None:
        with pytest.raises(RoleError, match=match):
            async with db.tx() as conn:
                await op(conn)

    await refused(lambda c: staff_roles.create_role(c, manager, "Финансы", ["stats"]), "есть у вас")
    await refused(lambda c: staff_roles.toggle_perm(c, manager, narrow.id, "stats"), "есть у вас")
    await refused(lambda c: staff_roles.toggle_perm(c, manager, wide.id, "broadcast"), "которых нет у вас")
    await refused(lambda c: staff_roles.delete_role(c, manager, wide.id), "которых нет у вас")
    await refused(lambda c: staff_roles.toggle_perm(c, manager, lead.id, "users.help"), "Свою роль")
    await refused(lambda c: staff_roles.assign(c, manager, ids["b"], wide.id), "которых нет у вас")
    await refused(lambda c: staff_roles.assign(c, manager, ids["owner"], narrow.id), "Владельца")
    await refused(lambda c: staff_roles.unassign(c, manager, ids["owner"]), "Владельца")
    await refused(lambda c: staff_roles.unassign(c, manager, ids["star"]), "которых нет у вас")
    await refused(lambda c: staff_roles.unassign(c, manager, ids["a"]), "Свою роль")
    await refused(
        lambda c: roles.set_role(c, manager, ids["b"], "owner", owner_ids={CONF_OWNER_TG}), "Нет прав"
    )
    # owners from the settings are out of reach even for an owner
    await refused(
        lambda c: staff_roles.assign(c, owner, ids["conf"], narrow.id, owner_ids={CONF_OWNER_TG}), "OWNER_IDS"
    )

    async with db.tx() as conn:  # within their rights it works
        await staff_roles.assign(conn, manager, ids["b"], narrow.id)
        out = await staff_roles.create_role(conn, manager, "Помощник", ["users.view"])
        assert out.role is not None
        await staff_roles.toggle_perm(conn, manager, out.role.id, "broadcast")
    row = await _row(db, ids["b"])
    assert row["role"] == "admin" and _list(row["perms"]) == ["broadcast"]
    async with db.read() as conn:
        assert staff_roles.grantable(manager, CORE_PERMS) == {"roles.manage", "broadcast", "users.view"}
        mine = await staff_roles.get_role(conn, lead.id)
        assert mine is not None and not staff_roles.can_edit(manager, mine, lead.id)
        assert staff_roles.can_edit(owner, mine, None)


async def test_find_user_by_id_username_and_number(db: CountingDatabase) -> None:
    uid = await add_user(db, 777)
    await db.raw("update users set username = 'Ivan_Petrov' where id = $1", uid)
    async with db.read() as conn:
        for query in ("777", "@ivan_petrov", "ivan_petrov", "t.me/Ivan_Petrov", f"№{uid}", f"#{uid}"):
            found = await staff_roles.find_user(conn, query)
            assert found is not None and found.user_id == uid, query
        assert await staff_roles.find_user(conn, "@nobody_here") is None
        assert await staff_roles.find_user(conn, "привет") is None
