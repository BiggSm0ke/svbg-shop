"""«🗑 Удалить полностью» with custom roles: a role holder with the right sees the button on plain users only;
a member of any role (even one without rights) gets no button and a forged one is refused."""

from __future__ import annotations

import json

from svbg.tg.admin.users.screens import ACTIONS, SCREEN_CARD, SCREEN_DELETE
from svbg.tg.ui.codec import encode
from svbg.tg.ui.context import UserCtx
from tests.tg.admin.users.kit import OWNER, USER, UEnv
from tests.tg.admin.users.test_admin_users_delete import DELETE, attach, exists

CLEANER = 3301
MEMBER = 3302


async def _role(env: UEnv, name: str, perms: list[str]) -> int:
    rows = await env.db.raw(
        "insert into staff_roles (name, perms) values ($1, $2::jsonb) returning id", name, json.dumps(perms)
    )
    return int(rows[0]["id"])


async def _member(env: UEnv, tg: int, role_id: int, role: str, perms: frozenset[str]) -> int:
    uid = await env.add(tg, role, perms, first_name=f"Сотрудник {tg}")
    await env.db.raw("update users set staff_role_id = $2 where id = $1", uid, role_id)
    env.users.by_tg[tg] = UserCtx(uid, telegram_id=tg, role=role, perms=perms, staff_role=role_id)
    return uid


async def test_role_holder_and_role_members(env: UEnv) -> None:
    attach(env)
    cleaners = await _role(env, "Чистильщик", ["users.delete", "users.view"])
    nothing = await _role(env, "Пустая", [])
    await _member(env, CLEANER, cleaners, "admin", frozenset({"users.view", "users.delete"}))
    member = await _member(env, MEMBER, nothing, "user", frozenset())
    # a member of a role without rights: no button even for the owner; a forged screen and action refuse
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(member)))
    assert DELETE not in env.labels()
    await env.click(OWNER, encode(SCREEN_DELETE, arg=f"{member}:1"))
    assert "удалить нельзя" in env.text
    await env.click(OWNER, encode(ACTIONS, "del", f"{member}:0"))
    assert "Сначала снимите с него роль" in env.text and await exists(env, member)
    # the role holder: the button on a plain user, not on staff
    await env.click(CLEANER, encode(SCREEN_CARD, arg=str(member)))
    assert DELETE not in env.labels()
    uid = env.ids[USER]
    await env.click(CLEANER, encode(SCREEN_CARD, arg=str(uid)))
    assert DELETE in env.labels()
    await env.press(CLEANER, DELETE)
    await env.press(CLEANER, "Да, удалить навсегда")
    assert "Пользователь удалён" in env.text and not await exists(env, uid)
