"""Roles × actions (04 §9.1): the matrix, fresh role reads, limits, reasons, audit, role changes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa

from svbg.services import roles
from svbg.services.roles import ADMIN_PERMS, Act, Actor, Limits, RoleError
from tests.dbkit import CountingDatabase, add_user, open_db


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


def actor(role: str, *perms: str, banned: bool = False) -> Actor:
    return Actor(1, 100, role, frozenset(perms), banned=banned)


# ------------------------------------------------------------------------------------------- matrix

MATRIX_CASES = [
    # (act, owner, admin with the right, admin without rights, support, user)
    (Act.USERS_VIEW, True, True, True, True, False),
    (Act.USERS_DEVICES, True, True, True, True, False),
    (Act.USERS_REISSUE, True, True, True, True, False),
    (Act.USERS_MESSAGE, True, True, True, True, False),
    (Act.WALLET_ADJUST, True, True, False, False, False),
    (Act.SUBS_GRANT, True, True, False, False, False),
    (Act.USERS_BAN, True, True, False, False, False),
    (Act.STATS, True, True, False, False, False),
    (Act.SYSTEM_VIEW, True, True, False, False, False),
    (Act.ROLES_MANAGE, True, False, False, False, False),
]


@pytest.mark.parametrize(("act", "owner", "admin_ok", "admin_no", "support", "user"), MATRIX_CASES)
def test_matrix(act: Act, owner: bool, admin_ok: bool, admin_no: bool, support: bool, user: bool) -> None:
    perm = roles.MATRIX[act].perm
    assert roles.authorize(actor("owner"), act) is owner
    assert roles.authorize(actor("admin", *(ADMIN_PERMS if perm is None else (perm,))), act) is admin_ok
    assert roles.authorize(actor("admin"), act) is admin_no
    assert roles.authorize(actor("support", *ADMIN_PERMS), act) is support  # perms never lift Support
    assert roles.authorize(actor("user", *ADMIN_PERMS), act) is user


def test_star_and_banned() -> None:
    assert roles.authorize(actor("admin", "*"), Act.WALLET_ADJUST)
    assert not roles.authorize(actor("admin", "*"), Act.ROLES_MANAGE)
    assert not roles.authorize(actor("owner", banned=True), Act.USERS_VIEW)
    assert not roles.authorize(None, Act.USERS_VIEW)
    assert actor("admin", "*").has_perm("stats") and not actor("support", "*").has_perm("stats")


# ------------------------------------------------------------------------------------------- limits, reason


def test_limits_from_settings() -> None:
    lim = Limits.from_settings({}, currency_exponent=2, max_plan_price_minor=169900)
    assert lim == Limits(31, 169900)
    lim = Limits.from_settings(
        {"ADMIN_GRANT_DAYS_MAX": 7, "ADMIN_WALLET_ADJUST_MAX": 500},
        currency_exponent=2,
        max_plan_price_minor=1,
    )
    assert lim == Limits(7, 50000)
    bad = Limits.from_settings(
        {"ADMIN_GRANT_DAYS_MAX": "x", "ADMIN_WALLET_ADJUST_MAX": True},
        currency_exponent=2,
        max_plan_price_minor=None,
    )
    assert bad == Limits(31, None)


def test_check_limit() -> None:
    lim = Limits(31, 1000)
    roles.check_limit(actor("admin"), Act.SUBS_GRANT, -31, lim)
    with pytest.raises(RoleError, match="владелец"):
        roles.check_limit(actor("admin"), Act.SUBS_GRANT, 32, lim)
    with pytest.raises(RoleError):
        roles.check_limit(actor("admin"), Act.WALLET_ADJUST, -1001, lim)
    with pytest.raises(RoleError):
        roles.check_limit(actor("admin"), Act.WALLET_ADJUST, 1, Limits(31, None))
    roles.check_limit(actor("owner"), Act.WALLET_ADJUST, 10**9, Limits(31, None))


@pytest.mark.parametrize("bad", [None, "", "  ", "ok"])
def test_reason_is_mandatory(bad: str | None) -> None:
    with pytest.raises(RoleError) as e:
        roles.require_reason(bad)
    assert e.value.code == "reason"


def test_reason_is_normalised() -> None:
    assert roles.require_reason("  возврат \n за  простой ") == "возврат за простой"
    assert len(roles.require_reason("я" * 900)) == roles.REASON_MAX


# ------------------------------------------------------------------------------------------- database


async def test_load_actor(db: CountingDatabase) -> None:
    uid = await add_user(db, 10, "admin")
    await db.raw("update users set perms = $2::jsonb where id = $1", uid, json.dumps(["stats", "bogus"]))
    banned = await add_user(db, 11, "support")
    await db.raw("update users set banned_at = now() where id = $1", banned)
    async with db.tx() as conn:
        a = await roles.load_actor(conn, telegram_id=10, lock=True)
        assert a is not None and a.role == "admin" and a.perms == frozenset({"stats", "bogus"})
        assert roles.authorize(a, Act.STATS) and not roles.authorize(a, Act.WALLET_ADJUST)
        b = await roles.load_actor(conn, telegram_id=11)
        assert b is not None and b.banned and not roles.authorize(b, Act.USERS_VIEW)
        assert await roles.load_actor(conn, telegram_id=12) is None
        c = await roles.load_actor(conn, telegram_id=12, owner_ids={12})
        assert c is not None and c.role == "owner" and c.user_id is None
        d = await roles.load_actor(conn, telegram_id=10, owner_ids={10})
        assert d is not None and d.role == "owner" and d.perms == frozenset()
        e = await roles.load_actor(conn, user_id=uid)
        assert e == a
        with pytest.raises(ValueError):
            await roles.load_actor(conn)


async def test_audit_money_needs_a_reason(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        with pytest.raises(RoleError):
            await roles.audit(conn, actor("owner"), "wallet.adjust", amount_minor=100)
        await roles.audit(conn, None, "system.thing", target="x" * 500, details={"a": 1})
    rows = await db.raw("select * from admin_audit")
    assert len(rows) == 1 and rows[0]["actor_id"] is None and len(rows[0]["target"]) == 200
    # the table enforces the same rule for any writer
    with pytest.raises(sa.exc.IntegrityError):
        async with db.tx() as conn:
            await conn.execute(sa.text("insert into admin_audit (action, amount_minor) values ('x', 1)"))


async def test_set_role(db: CountingDatabase) -> None:
    owner = await add_user(db, 1, "owner")
    target = await add_user(db, 2)
    async with db.tx() as conn:
        me = await roles.load_actor(conn, telegram_id=1)
        change = await roles.set_role(conn, me, target, "admin", ["stats", "plans"])
    assert change.changed and change.new_perms == ("plans", "stats") and change.telegram_id == 2
    row = (await db.raw("select role, perms from users where id = $1", target))[0]
    assert row["role"] == "admin" and row["perms"] == ["plans", "stats"]
    audit = await db.raw("select * from admin_audit")
    assert audit[0]["action"] == "role.set" and audit[0]["details"]["new_perms"] == ["plans", "stats"]
    async with db.tx() as conn:  # the same again: no-op, no audit
        assert not (await roles.set_role(conn, me, target, "admin", ["plans", "stats"])).changed
        star = await roles.set_role(conn, me, target, "admin", ["*"])
        assert star.new_perms == ADMIN_PERMS
        down = await roles.set_role(conn, me, target, "support", ["stats"])
        assert down.new_perms == ()
    assert len(await db.raw("select * from admin_audit")) == 3
    assert owner


async def test_set_role_refusals(db: CountingDatabase) -> None:
    owner = await add_user(db, 1, "owner")
    admin = await add_user(db, 2, "admin")
    await db.raw("update users set perms = $2::jsonb where id = $1", admin, json.dumps(list(ADMIN_PERMS)))
    target = await add_user(db, 3)
    configured = await add_user(db, 4)
    banned = await add_user(db, 5)
    await db.raw("update users set banned_at = now() where id = $1", banned)

    async def attempt(by: int, uid: int, role: str, perms: list[str] | None = None) -> str:
        try:
            async with db.tx() as conn:
                me = await roles.load_actor(conn, telegram_id=by, owner_ids={1})
                await roles.set_role(conn, me, uid, role, perms or [], owner_ids={4})
        except RoleError as e:
            return e.code
        return "ok"

    assert await attempt(2, target, "admin") == "denied"  # an admin cannot grant roles at all
    assert await attempt(2, admin, "owner") == "denied"
    assert await attempt(1, owner, "user") == "self"
    assert await attempt(1, configured, "user") == "configured_owner"
    assert await attempt(1, banned, "support") == "banned"
    assert await attempt(1, target, "boss") == "role"
    assert await attempt(1, target, "admin", ["root"]) == "perms"
    assert await attempt(1, 999, "admin") == "not_found"
    assert await attempt(1, target, "owner") == "ok"
    assert (await db.raw("select role from users where id = $1", target))[0]["role"] == "owner"
    assert await attempt(1, banned, "user") == "ok"  # revoking is always possible


async def test_staff_list(db: CountingDatabase) -> None:
    await add_user(db, 1, "support")
    await add_user(db, 2, "owner")
    await add_user(db, 3)
    await add_user(db, 4, "admin")
    async with db.read() as conn:
        staff = await roles.staff_list(conn)
    assert [(m.telegram_id, m.role) for m in staff] == [(2, "owner"), (4, "admin"), (1, "support")]
