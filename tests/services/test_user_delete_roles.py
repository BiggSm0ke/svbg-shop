"""«🗑 Удалить полностью» together with custom staff roles: the right is given through a role, a role holder
deletes plain users only, a member of any role (even one without rights) is staff and is not deleted."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from svbg.core.perms import CORE_PERMS
from svbg.services import roles, staff_roles
from svbg.services.roles import Actor, RoleError
from svbg.services.user_delete import UserDeleter
from svbg.tg.admin.roles import GROUPS, LABELS
from tests.dbkit import CountingDatabase, add_user, open_db
from tests.services.test_user_delete import TG, FakeApi, make, one, owners, seed

OWNER = 1001
CONF_OWNER = 7007
CLEANER = 3101  # a role with «users.delete»
MANAGER = 3102  # a role with «roles.manage» but without «users.delete»
EMPTY = 3103  # a member of a role without rights
SUPPORT_ROLE = 3104  # a member of «Поддержка»-like role


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


def test_delete_right_is_in_the_role_editor() -> None:
    assert "users.delete" in CORE_PERMS
    users_group = dict(GROUPS)["👥 Пользователи"]
    assert "users.delete" in users_group
    assert LABELS["users.delete"] == "Удалять клиента полностью"
    assert roles.PERM_LABELS["users.delete"]


async def _actor(db: CountingDatabase, tg: int) -> Actor | None:
    async with db.read() as conn:
        return await roles.load_actor(conn, telegram_id=tg, owner_ids=await owners())


async def _role(db: CountingDatabase, actor_tg: int, name: str, perms: list[str]) -> int:
    async with db.tx() as conn:
        out = await staff_roles.create_role(conn, await _actor(db, actor_tg), name, perms)
    assert out.role is not None
    return out.role.id


async def _give(db: CountingDatabase, actor_tg: int, user_id: int, role_id: int) -> None:
    async with db.tx() as conn:
        await staff_roles.assign(conn, await _actor(db, actor_tg), user_id, role_id, owner_ids=await owners())


async def _team(db: CountingDatabase) -> dict[str, int]:
    ids = {
        "cleaner": await add_user(db, CLEANER),
        "manager": await add_user(db, MANAGER),
        "empty": await add_user(db, EMPTY),
        "support": await add_user(db, SUPPORT_ROLE),
    }
    cleaners = await _role(db, OWNER, "Чистильщик", ["users.view", "users.delete"])
    managers = await _role(db, OWNER, "Менеджер", ["users.view", "roles.manage"])
    nothing = await _role(db, OWNER, "Пустая", [])
    helpers = await _role(db, OWNER, "Помощь", ["users.view", "users.help", "tickets"])
    await _give(db, OWNER, ids["cleaner"], cleaners)
    await _give(db, OWNER, ids["manager"], managers)
    await _give(db, OWNER, ids["empty"], nothing)
    await _give(db, OWNER, ids["support"], helpers)
    ids.update(cleaners=cleaners, managers=managers, nothing=nothing, helpers=helpers)
    return ids


async def test_role_with_delete_right_deletes_plain_users_only(db: CountingDatabase) -> None:
    data = await seed(db)  # OWNER, classic ADMIN / SUPPORT and the user TG with rows everywhere
    ids = await _team(db)
    deleter, _, forgotten = make(db, FakeApi())
    cleaner = await _actor(db, CLEANER)
    assert cleaner is not None and cleaner.scoped and cleaner.role == "admin"
    # staff of every kind, owners and oneself are out of reach
    for key in ("manager", "empty", "support"):
        assert (await deleter.delete(CLEANER, ids[key])).code == "staff", key
        assert not await deleter.allowed(CLEANER, ids[key])
    assert (await deleter.delete(CLEANER, ids["cleaner"])).code == "self"
    owner = await one(db, "select id from users where telegram_id = $1", OWNER)
    conf = await add_user(db, CONF_OWNER)
    assert (await deleter.delete(CLEANER, owner)).code == "owner"
    assert (await deleter.delete(CLEANER, conf)).code == "owner"
    # a role without the right (even a manager) cannot delete
    for tg in (MANAGER, EMPTY, SUPPORT_ROLE):
        assert (await deleter.delete(tg, data["uid"])).denied, tg
    # the owner does not delete a member of a role either: take the role away first
    assert (await deleter.delete(OWNER, ids["empty"])).code == "staff"
    # the plain user goes
    assert await deleter.allowed(CLEANER, data["uid"])
    result = await deleter.delete(CLEANER, data["uid"])
    assert result.ok and forgotten == [(data["uid"], TG)]
    assert await one(db, "select count(*) from users where id = $1", data["uid"]) == 0
    audit = await db.raw(
        "select actor_id, action from admin_audit where action = 'user.delete' order by id desc limit 1"
    )
    assert audit[0]["actor_id"] == ids["cleaner"]


async def test_role_taken_away_then_deleted(db: CountingDatabase) -> None:
    await seed(db)
    ids = await _team(db)
    deleter, _, _ = make(db, FakeApi())
    async with db.tx() as conn:
        await staff_roles.unassign(conn, await _actor(db, OWNER), ids["support"], owner_ids=await owners())
    result = await deleter.delete(OWNER, ids["support"])
    assert result.ok
    async with db.read() as conn:
        role = await staff_roles.get_role(conn, ids["helpers"])
    assert role is not None and role.members == 0  # the role stays, without the deleted person
    assert await one(db, "select count(*) from staff_roles") == 4


async def test_delete_right_is_given_only_by_who_has_it(db: CountingDatabase) -> None:
    await seed(db)
    ids = await _team(db)
    # the manager has no «users.delete»: cannot put it into a role, cannot hand out a role that has it
    manager = await _actor(db, MANAGER)
    with pytest.raises(RoleError) as err:
        async with db.tx() as conn:
            await staff_roles.create_role(conn, manager, "Удаляторы", ["users.view", "users.delete"])
    assert err.value.code == "escalation"
    target = await add_user(db, 3200)
    with pytest.raises(RoleError) as err:
        async with db.tx() as conn:
            await staff_roles.assign(conn, manager, target, ids["cleaners"], owner_ids=await owners())
    assert err.value.code == "escalation"
    # the owner switches it on in an existing role: its members get it at once
    async with db.tx() as conn:
        out = await staff_roles.toggle_perm(conn, await _actor(db, OWNER), ids["helpers"], "users.delete")
    assert out.role is not None and "users.delete" in out.role.perms
    helper = await _actor(db, SUPPORT_ROLE)
    assert helper is not None and helper.role == "admin" and helper.has_perm("users.delete")
    deleter, _, _ = make(db, FakeApi())
    assert (await deleter.delete(SUPPORT_ROLE, target)).ok


async def test_panel_writes_stopped_keep_the_panel(db: CountingDatabase) -> None:
    data = await seed(db)
    api = FakeApi()
    deleter = UserDeleter(db, owner_ids=owners, panel=lambda: api, writes_stopped=lambda: "shadow")
    result = await deleter.delete(OWNER, data["uid"])
    assert not result.ok and result.code == "panel" and "запись в панель остановлена" in result.text
    assert api.deleted == []
    assert await one(db, "select count(*) from users where id = $1", data["uid"]) == 1
    only_bot = await deleter.delete(OWNER, data["uid"], panel=False)  # «Удалить только в боте» still works
    assert only_bot.ok and api.deleted == []
