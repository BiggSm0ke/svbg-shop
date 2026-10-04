"""«👮 Команда»: custom roles made from switches, given to a person, the person sees exactly the sections the
role allows; take a role away; rename and delete; a manager never gives more than they have; old buttons."""

from __future__ import annotations

import json
from typing import Any

from aiogram.methods import DeleteMessage
from aiogram.types import Chat, Message, MessageOriginHiddenUser, MessageOriginUser

from svbg.core.perms import CORE_PERMS, SUPPORT_PERMS
from svbg.services import roles, staff_roles
from svbg.tg.admin import nav
from svbg.tg.admin.commands import commands_for
from svbg.tg.admin.menu import ROOT_SECTIONS
from svbg.tg.admin.roles import (
    ACTIONS,
    FULL_MASK,
    GROUPS,
    SCREEN_ADD,
    SCREEN_CONFIRM,
    SCREEN_EDIT,
    SCREEN_GROUP,
    SCREEN_LIST,
    SCREEN_ROLE,
    SCREEN_ROLES,
    RoleScreens,
    mask_of,
    perms_of,
)
from svbg.tg.ui.codec import encode
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.view import View
from tests.tg.admin.users.kit import (
    ADMIN,
    ADMIN_STATS,
    ALL_ADMIN,
    CONF_OWNER,
    OTHER,
    OWNER,
    SUPPORT,
    USER,
    UEnv,
)
from tests.tg.ui.ui_harness import DATE, text_message, tg_user

SECTION_LABELS = {e.label for e in ROOT_SECTIONS}


async def stored(env: UEnv, tg: int) -> tuple[str, list[str], int | None]:
    row = (await env.db.raw("select role, perms, staff_role_id from users where telegram_id = $1", tg))[0]
    perms = row["perms"] if isinstance(row["perms"], list) else json.loads(row["perms"])
    return row["role"], perms, row["staff_role_id"]


async def reload(env: UEnv, tg: int) -> UserCtx:
    """What the user loader builds after the cache was dropped."""
    role, perms, staff_role = await stored(env, tg)
    ctx = UserCtx(env.ids[tg], telegram_id=tg, role=role, perms=frozenset(perms), staff_role=staff_role)
    env.users.by_tg[tg] = ctx
    return ctx


async def make_role(env: UEnv, name: str, perms: Any) -> int:
    async with env.db.tx() as conn:
        owner = await roles.load_actor(conn, telegram_id=OWNER)
        out = await staff_roles.create_role(conn, owner, name, list(perms))
    assert out.role is not None
    return out.role.id


def stub(env: UEnv, code: str, **guard: Any) -> None:
    async def probe(_ctx: object, _arg: object) -> View:
        return View(text=f"probe {code}")

    env.router.screen(code, **guard)(probe)


def sections(env: UEnv) -> list[str]:
    return [lb for lb in env.labels() if lb.split(" · ")[0].rstrip(" ⚠️") in SECTION_LABELS]


def entries(env: UEnv) -> list[str]:
    return [lb for lb in env.labels() if not lb.startswith("⬅️") and lb != "🛠 Админка"]


def test_mask_round_trip() -> None:
    assert perms_of(FULL_MASK) == sorted(ALL_ADMIN, key=perms_of(FULL_MASK).index)
    assert mask_of(perms_of(5)) == 5 and perms_of(0) == []


async def test_only_the_owner_and_managers_open_the_team(env: UEnv) -> None:
    uid = env.ids[USER]
    for who in (ADMIN, ADMIN_STATS, SUPPORT, USER):
        for data in (
            encode(SCREEN_LIST),
            encode(SCREEN_EDIT, arg=str(uid)),
            encode(SCREEN_ROLES),
            encode(SCREEN_ADD),
            encode(ACTIONS, "as", f"{uid}:1"),
            encode(ACTIONS, "save", f"{uid}:admin:{FULL_MASK}"),
        ):
            await env.click(who, data)
            assert env.toasts[-1] == "Нет прав", (who, data)
    assert await stored(env, USER) == ("user", [], None)
    assert not env.rendered()


async def test_team_list(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    assert "Команда" in env.text and f"<code>{CONF_OWNER}</code>" in env.text
    labels = env.labels()
    assert labels[:2] == ["➕ Добавить человека", "🎭 Роли"]
    assert any("👑 Влад" in lb for lb in labels)
    assert any("🛡 Админ" in lb for lb in labels)
    assert any("🎧 Саппорт" in lb for lb in labels)
    assert not any("Иван" in lb for lb in labels)


async def test_a_role_from_switches_shows_exactly_its_sections(env: UEnv) -> None:
    stub(env, "bc", required_role="admin", perm="broadcast")
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "🎭 Роли")
    assert "Ролей пока нет" in env.text
    await env.press(OWNER, "➕ Новая роль")
    assert "Как назвать роль" in env.text
    assert await env.type(OWNER, "Рассыльщик")
    assert "Роль создана" in env.text and "Рассыльщик" in env.text
    assert "📣 Связь · 0/3" in env.labels()
    await env.press(OWNER, "📣 Связь")
    assert "▫️ Готовить рассылки" in env.labels()
    assert not any("." in lb.split(" ", 1)[1] for lb in entries(env))  # human labels, no codes
    await env.press(OWNER, "Готовить рассылки")
    assert "✅ Готовить рассылки" in env.labels()
    rid = (await env.db.raw("select id from staff_roles where name = 'Рассыльщик'"))[0]["id"]

    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(env.ids[USER])))
    assert "Сейчас: без роли" in env.text
    await env.press(OWNER, "Рассыльщик")
    assert "Роль «Рассыльщик» выдана" in env.text and "• Рассыльщик" in env.labels()
    assert await stored(env, USER) == ("admin", ["broadcast"], rid)
    assert env.directory.invalidated[-1] == USER
    actions = [r["action"] for r in await env.audit()]
    assert actions[-3:] == ["role.create", "role.perms", "role.assign"]

    await reload(env, USER)
    await env.click(USER, encode(nav.ROOT))
    assert sections(env) == ["📣 Связь"]
    assert "🔍 Найти пользователя" not in env.labels()
    await env.press(USER, "📣 Связь")
    assert entries(env) == ["📨 Рассылки"]
    for closed in ("au.find", "au", SCREEN_LIST, nav.STATS):
        await env.click(USER, encode(closed))
        assert env.toasts[-1] == "Нет прав", closed
    await env.click(USER, encode(nav.HUB_USERS))  # an old button: an empty section, nothing to open
    assert "нет разделов" in env.text and entries(env) == []
    assert [c.command for c in commands_for("admin", ["broadcast"], scoped=True)] == ["admin", "broadcast"]

    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(env.ids[USER])))
    await env.press(OWNER, "🚫 Убрать из команды")
    assert "Убран из команды" in env.text
    assert await stored(env, USER) == ("user", [], None)
    await reload(env, USER)
    await env.click(USER, encode(nav.ROOT))
    assert env.toasts[-1] == "Нет прав"
    assert [r["action"] for r in await env.audit()][-1] == "role.unassign"


async def test_the_support_preset_opens_users_only(env: UEnv) -> None:
    await env.add(OTHER, first_name="Пётр")
    rid = await make_role(env, "Поддержка", SUPPORT_PERMS)
    await env.click(OWNER, encode(ACTIONS, "as", f"{env.ids[OTHER]}:{rid}"))
    assert (await stored(env, OTHER))[0] == "support"  # no sums, no money: the rank of the Support column
    await reload(env, OTHER)
    await env.click(OTHER, encode(nav.ROOT))
    assert sections(env) == ["👥 Пользователи"]
    await env.press(OTHER, "👥 Пользователи")
    assert "🔍 Найти" in entries(env) and "💳 Недавно оплатили" not in entries(env)
    await env.click(OTHER, encode("au.find"))
    assert env.toasts[-1] != "Нет прав"


async def test_a_manager_never_gives_more_than_they_have(env: UEnv) -> None:
    lead = await make_role(env, "Тимлид", ["roles.manage", "broadcast", "users.view"])
    wide = await make_role(env, "Широкая", ["broadcast", "stats"])
    narrow = await make_role(env, "Рассылки", ["broadcast"])
    me = env.ids[ADMIN_STATS]
    await env.click(OWNER, encode(ACTIONS, "as", f"{me}:{lead}"))
    await reload(env, ADMIN_STATS)

    await env.click(ADMIN_STATS, encode(nav.HUB_SYSTEM))
    assert entries(env) == ["👮 Команда"]
    await env.press(ADMIN_STATS, "👮 Команда")
    await env.press(ADMIN_STATS, "🎭 Роли")
    labels = env.labels()
    assert any(lb.startswith("🔒 Тимлид") for lb in labels)  # their own role
    assert any(lb.startswith("🔒 Широкая") for lb in labels)  # a right they do not have
    assert any(lb.startswith("Рассылки") for lb in labels)

    await env.click(ADMIN_STATS, encode(ACTIONS, "tg", f"{wide}:broadcast"))
    assert "⚠️ У этой роли есть права, которых нет у вас." in env.text
    await env.click(ADMIN_STATS, encode(ACTIONS, "tg", f"{narrow}:stats"))
    assert "⚠️ Можно выдать только те права, которые есть у вас." in env.text
    await env.click(ADMIN_STATS, encode(ACTIONS, "tg", f"{lead}:users.help"))
    assert "⚠️ Свою роль может поменять только владелец." in env.text
    stats_group = next(i for i, (title, _) in enumerate(GROUPS) if title.startswith("📊"))
    await env.click(ADMIN_STATS, encode(SCREEN_GROUP, arg=f"{narrow}:{stats_group}"))
    assert "Здесь нечего выдать." in env.text
    rows = await env.db.raw("select id, perms from staff_roles order by id")
    assert {r["id"]: sorted(r["perms"]) for r in rows}[narrow] == ["broadcast"]

    await env.click(ADMIN_STATS, encode(SCREEN_EDIT, arg=str(env.ids[OWNER])))
    assert "Это владелец" in env.text and "Рассылки" not in env.labels()
    await env.click(ADMIN_STATS, encode(ACTIONS, "as", f"{env.ids[OWNER]}:{narrow}"))
    assert "⚠️ Владельца может менять только владелец." in env.text
    await env.click(ADMIN_STATS, encode(SCREEN_EDIT, arg=str(env.ids[ADMIN])))
    assert "есть права, которых нет у вас" in env.text
    await env.click(ADMIN_STATS, encode(ACTIONS, "rm", str(env.ids[ADMIN])))
    assert "⚠️ У этого человека есть права, которых нет у вас." in env.text
    await env.click(ADMIN_STATS, encode(SCREEN_EDIT, arg=str(me)))
    assert "Это вы" in env.text
    await env.click(ADMIN_STATS, encode(SCREEN_CONFIRM, arg=str(env.ids[USER])))
    assert env.toasts[-1] == "Нет прав"
    await env.click(ADMIN_STATS, encode(ACTIONS, "save", f"{env.ids[USER]}:owner:0"))
    assert "⚠️ Нет прав" in env.text
    assert (await stored(env, OWNER))[0] == "owner" and (await stored(env, USER))[0] == "user"

    await env.click(ADMIN_STATS, encode(SCREEN_EDIT, arg=str(env.ids[USER])))
    assert "Широкая" not in env.labels() and "👑 Сделать владельцем" not in env.labels()
    await env.press(ADMIN_STATS, "Рассылки")
    assert (await stored(env, USER))[2] == narrow


async def test_rename_and_delete_a_role(env: UEnv) -> None:
    rid = await make_role(env, "Модератор", ["users.view", "broadcast"])
    await env.click(OWNER, encode(ACTIONS, "as", f"{env.ids[USER]}:{rid}"))
    await env.click(OWNER, encode(SCREEN_ROLE, arg=str(rid)))
    assert "Прав: 2 · людей с ролью: 1" in env.text and "Иван" in env.text
    await env.press(OWNER, "✏️ Переименовать")
    assert await env.type(OWNER, "Старший модератор")
    assert "Переименовано" in env.text and "Старший модератор" in env.text
    await env.press(OWNER, "🗑 Удалить")
    assert "Удалить роль «Старший модератор»?" in env.text and "у 1 чел." in env.text
    env.directory.invalidated.clear()
    await env.press(OWNER, "Да, удалить")
    assert env.toasts[-1] == "Роль удалена" and "Ролей пока нет" in env.text
    assert await stored(env, USER) == ("user", [], None)
    assert env.directory.invalidated == [USER]
    assert [r["action"] for r in await env.audit()][-2:] == ["role.rename", "role.delete"]


def forwarded(tg: int, origin: Any, message_id: int = 600) -> Message:
    return Message(
        message_id=message_id,
        date=DATE,
        chat=Chat(id=tg, type="private"),
        from_user=tg_user(tg),
        text="привет",
        forward_origin=origin,
    )


async def test_add_a_person_by_id_username_or_forward(env: UEnv) -> None:
    team = env.router._screens[SCREEN_ADD].fn.__self__
    assert isinstance(team, RoleScreens)
    first = text_message(OWNER, str(USER), message_id=501)
    assert not team.wants(first)  # «добавить» is not open: an ordinary message
    await env.click(OWNER, encode(SCREEN_ADD))
    assert "Добавить в команду" in env.text
    assert team.wants(first) and await team.handle_add(first)
    assert "Иван" in env.text and "Сейчас: без роли" in env.text
    assert [m.message_id for m in env.transport.of(DeleteMessage)] == [501]
    assert not team.wants(text_message(OWNER, "@ivan_petrov"))  # found: the mode ends

    for msg in (
        text_message(OWNER, "@ivan_petrov", message_id=502),
        forwarded(OWNER, MessageOriginUser(type="user", date=DATE, sender_user=tg_user(USER))),
    ):
        await env.click(OWNER, encode(SCREEN_ADD))
        assert await team.handle_add(msg)
        assert "Иван" in env.text
    await env.click(OWNER, encode(SCREEN_ADD))
    hidden = MessageOriginHiddenUser(type="hidden_user", date=DATE, sender_user_name="Иван")
    assert await team.handle_add(forwarded(OWNER, hidden, message_id=601))
    assert "скрыл аккаунт" in env.text
    assert await team.handle_add(text_message(OWNER, "@nobody_like_this", message_id=503))
    assert "Не нашёл «@nobody_like_this»" in env.text
    team.seen_callback(OWNER, encode(SCREEN_LIST))  # any other button ends the mode
    assert not team.wants(text_message(OWNER, "5005"))
    assert not team.wants(text_message(OWNER, "/start"))

    team.arm(env.users.by_tg[ADMIN])  # never for someone without «Команда и роли»
    assert not await team.handle_add(text_message(ADMIN, str(USER), message_id=504))


async def test_old_buttons_and_the_owner_flow(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.click(OWNER, encode(SCREEN_EDIT, arg=f"{uid}:admin:5"))  # an old editor button
    assert "Иван" in env.text and "Сейчас: без роли" in env.text
    await env.click(OWNER, encode(ACTIONS, "save", f"{env.ids[SUPPORT]}:user:0"))
    assert "✅ Сохранено." in env.text and await stored(env, SUPPORT) == ("user", [], None)

    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(uid)))
    await env.press(OWNER, "👑 Сделать владельцем")
    assert "Сделать" in env.text and "владельцем" in env.text
    assert (await stored(env, USER))[0] == "user"
    await env.press(OWNER, "Да, сделать владельцем")
    assert (await stored(env, USER))[0] == "owner"

    await env.click(OWNER, encode(ACTIONS, "save", f"{env.ids[OWNER]}:user:0"))
    assert "Свою роль менять нельзя" in env.text
    await env.click(OWNER, encode(SCREEN_EDIT, arg="999999"))
    assert "не найден" in env.text
    await env.click(OWNER, encode(SCREEN_CONFIRM, arg=f"{uid}:admin:99999"))
    assert "Команда" in env.text  # a forged mask: back to the team


async def test_owner_role_revoked_in_the_database_wins(env: UEnv) -> None:
    rid = await make_role(env, "Рассылки", ["broadcast"])
    await env.db.raw("update users set role = 'admin' where telegram_id = $1", OWNER)
    await env.click(OWNER, encode(ACTIONS, "as", f"{env.ids[USER]}:{rid}"))
    assert "Нет прав" in env.text
    await env.click(OWNER, encode(ACTIONS, "save", f"{env.ids[USER]}:admin:{FULL_MASK}"))
    assert "Нет прав" in env.text
    assert await stored(env, USER) == ("user", [], None)


def test_every_core_right_has_a_section_and_a_label() -> None:
    async def no_owners() -> frozenset[int]:
        return frozenset()

    screens = RoleScreens(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        owner_ids=no_owners,
        module_perms=lambda: [("ip_guard.view", "IP Guard: смотреть")],
    )
    groups = screens.groups()
    listed = [code for _, rows in groups for code, _ in rows]
    assert len(listed) == len(set(listed)) and set(listed) == {*CORE_PERMS, "ip_guard.view"}
    assert all(label and label != code for _, rows in groups for code, label in rows)
    assert groups[-1][0] == "🧩 Модули"
