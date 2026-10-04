"""Migration 0007: built-in roles on a fresh install, existing staff moved into equivalent roles without
losing a single right (real PostgreSQL, real ``alembic upgrade``)."""

from __future__ import annotations

import json

import asyncpg
import pytest

from svbg.core.perms import CORE_PERMS
from svbg.db import migrations
from svbg.services import roles
from svbg.services.roles import Act, Actor
from tests.dbkit import open_db

pytestmark = pytest.mark.pg

#: Rights checked before and after (core, module views, module rights given one by one).
PROBES = (*CORE_PERMS, "ip_guard.view", "lte.view", "lte.config", "ip_guard.unblock")


async def _run(dsn: str, sql: str, *args: object) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


async def test_fresh_install_gets_the_two_presets(pg_dsn: str) -> None:
    await migrations.upgrade(pg_dsn)
    rows = await _run(pg_dsn, "select name, perms from staff_roles order by id")
    names = [r["name"] for r in rows]
    assert names == ["Администратор", "Поддержка"]
    admin, support = (set(json.loads(r["perms"])) for r in rows)
    assert "roles.manage" not in admin and {"stats", "settings.business", "users.view", "tickets"} <= admin
    assert {"users.view", "users.help", "tickets"} <= support and "stats" not in support


async def test_existing_staff_keep_every_right(pg_dsn: str) -> None:
    await migrations.upgrade(pg_dsn, "0006_captcha")
    staff = {
        11: ("support", []),
        12: ("admin", ["*"]),
        13: ("admin", ["stats"]),
        14: ("admin", ["stats"]),
        15: ("admin", ["*", "lte.config"]),
        16: ("owner", []),
        17: ("user", []),
    }
    for tg, (role, perms) in staff.items():
        await _run(
            pg_dsn,
            "insert into users (telegram_id, role, perms) values ($1, $2, $3::jsonb)",
            tg,
            role,
            json.dumps(perms),
        )
    before = {
        tg: Actor(None, tg, role, frozenset(perms) if role == "admin" else frozenset())
        for tg, (role, perms) in staff.items()
    }
    await migrations.upgrade(pg_dsn)

    rows = await _run(
        pg_dsn,
        "select u.telegram_id, u.role, r.name from users u left join staff_roles r on r.id = u.staff_role_id "
        "order by u.telegram_id",
    )
    by_tg = {r["telegram_id"]: (r["role"], r["name"]) for r in rows}
    assert by_tg[11] == ("support", "Поддержка")
    assert by_tg[12] == ("admin", "Администратор")
    assert by_tg[13][1] == by_tg[14][1] == "Админ 1"  # one role per distinct set of rights
    assert by_tg[15][1] == "Админ 2"
    assert by_tg[16] == ("owner", None) and by_tg[17] == ("user", None)

    async with open_db(pg_dsn, schema=False) as db, db.read() as conn:
        for tg, old in before.items():
            new = await roles.load_actor(conn, telegram_id=tg)
            assert new is not None
            assert new.scoped is (tg not in (16, 17)), tg
            for act in Act:
                assert roles.authorize(new, act) is roles.authorize(old, act), (tg, act)
            for perm in PROBES:
                assert new.has_perm(perm) is old.has_perm(perm), (tg, perm)
