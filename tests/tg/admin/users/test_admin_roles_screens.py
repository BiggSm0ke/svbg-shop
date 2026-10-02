"""«👥 Роли»: list, editor with rights checkboxes, owner confirmation, owner-only access, cache
invalidation."""

from __future__ import annotations

from svbg.tg.admin.roles import ACTIONS, FULL_MASK, SCREEN_EDIT, SCREEN_LIST, mask_of, perms_of
from svbg.tg.ui.codec import encode
from tests.tg.admin.users.kit import ADMIN, ALL_ADMIN, CONF_OWNER, OWNER, SUPPORT, USER, UEnv


async def stored(env: UEnv, tg: int) -> tuple[str, list[str]]:
    row = (await env.db.raw("select role, perms from users where telegram_id = $1", tg))[0]
    return row["role"], row["perms"]


def test_mask_round_trip() -> None:
    assert perms_of(FULL_MASK) == sorted(ALL_ADMIN, key=perms_of(FULL_MASK).index)
    assert mask_of(perms_of(5)) == 5 and perms_of(0) == []


async def test_only_the_owner_manages_roles(env: UEnv) -> None:
    uid = env.ids[USER]
    for who in (ADMIN, SUPPORT, USER):
        for data in (
            encode(SCREEN_LIST),
            encode(SCREEN_EDIT, arg=str(uid)),
            encode(ACTIONS, "save", f"{uid}:admin:{FULL_MASK}"),
        ):
            await env.click(who, data)
            assert env.toasts[-1] == "Нет прав"
    assert await stored(env, USER) == ("user", [])
    assert not env.rendered()


async def test_staff_list(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    assert "Роли" in env.text and f"<code>{CONF_OWNER}</code>" in env.text
    labels = env.labels()
    assert any("👑 Влад" in lb for lb in labels)
    assert any("🛡 Админ" in lb and "(15)" in lb for lb in labels)
    assert any("🎧 Саппорт" in lb for lb in labels)
    assert not any("Иван" in lb for lb in labels)


async def test_make_an_admin_with_selected_rights(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(env.ids[USER])))
    assert "Сейчас: пользователь" in env.text and "💾 Сохранить" not in " ".join(env.labels())
    await env.press(OWNER, "админ")
    assert "Будет: админ (15 прав)" in env.text  # a new admin starts with every Admin right
    await env.press(OWNER, "Начислять и списывать баланс")
    await env.press(OWNER, "Возвраты")
    assert "Будет: админ (13 прав)" in env.text
    assert "▫️ Возвраты" in env.labels()
    await env.press(OWNER, "Сохранить")
    assert "✅ Сохранено." in env.text and "Сейчас: админ (13 прав)" in env.text
    role, perms = await stored(env, USER)
    assert (
        role == "admin"
        and "wallet.adjust" not in perms
        and "payments.refund" not in perms
        and len(perms) == 13
    )
    assert env.directory.invalidated == [USER]
    audit = await env.audit()
    assert audit[-1]["action"] == "role.set" and audit[-1]["actor_id"] == env.ids[OWNER]


async def test_support_and_revoke(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(env.ids[ADMIN])))
    await env.press(OWNER, "поддержка")
    assert "Поддержка видит карточки" in env.text
    await env.press(OWNER, "Сохранить")
    assert await stored(env, ADMIN) == ("support", [])
    await env.press(OWNER, "• поддержка")  # selecting the current role changes nothing
    await env.press(OWNER, "пользователь")
    await env.press(OWNER, "Сохранить")
    assert await stored(env, ADMIN) == ("user", [])


async def test_owner_needs_confirmation(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(env.ids[USER])))
    await env.press(OWNER, "владелец")
    await env.press(OWNER, "Сохранить")
    assert "Сделать" in env.text and "владельцем" in env.text
    assert await stored(env, USER) == ("user", [])
    await env.press(OWNER, "Да, сделать владельцем")
    assert await stored(env, USER) == ("owner", [])


async def test_refusals_are_shown(env: UEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "save", f"{env.ids[OWNER]}:user:0"))
    assert "Свою роль менять нельзя" in env.text
    await env.click(OWNER, encode(SCREEN_EDIT, arg="999999"))
    assert "не найден" in env.text
    await env.click(OWNER, encode(SCREEN_EDIT, arg=f"{env.ids[USER]}:admin:99999"))
    assert "Роли" in env.text  # a forged mask: back to the list


async def test_owner_role_revoked_in_the_database_wins(env: UEnv) -> None:
    await env.db.raw("update users set role = 'admin' where telegram_id = $1", OWNER)
    await env.click(OWNER, encode(ACTIONS, "save", f"{env.ids[USER]}:admin:{FULL_MASK}"))
    assert "Нет прав" in env.text
    assert await stored(env, USER) == ("user", [])
