"""Smart search (ids, @username, fuzzy names via pg_trgm, panel username, shortUuid, links, payment ids),
``/user`` and the admin-group «👤 Карточка» button."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, Message

from svbg.tg.admin.users.screens import SCREEN_FIND, SCREEN_RESULTS, card_button
from svbg.tg.admin.users.search import QueryError, parse_query, search
from svbg.tg.ui.codec import encode
from tests.tg.admin.users.kit import ADMIN, OTHER, SUPPORT, USER, UEnv, add_instance, add_payment, add_sub
from tests.tg.ui.ui_harness import DATE, text_message, tg_user


async def found(env: UEnv, text: str) -> list[int]:
    async with env.db.read() as conn:
        return [f.user_id for f in await search(conn, parse_query(text))]


# ------------------------------------------------------------------------------------------- parsing


def test_parse_query_variants() -> None:
    q = parse_query("  #42 ")
    assert q.number == 42 and q.bot_id_only
    q = parse_query("@Ivan_Petrov")
    assert q.username == "Ivan_Petrov" and q.low == "ivan_petrov"
    q = parse_query("https://sub.example.com/api/sub/AbCdEf12?x=1")
    assert q.raw == "AbCdEf12" and q.token == "AbCdEf12"
    q = parse_query("0190a3b4-1111-7222-8333-444455556666")
    assert q.uuid == "0190a3b4-1111-7222-8333-444455556666"
    assert parse_query("7").number == 7  # a single digit is a valid id


@pytest.mark.parametrize("bad", ["", " ", "a", "x" * 129])
def test_parse_query_rejects(bad: str) -> None:
    with pytest.raises(QueryError):
        parse_query(bad)


def test_huge_number_is_not_an_id() -> None:
    assert parse_query("99999999999999999999").number is None


# ------------------------------------------------------------------------------------------- matching


async def test_search_by_ids_and_names(env: UEnv) -> None:
    uid = env.ids[USER]
    other = await env.add(OTHER, username="maria_k", first_name="Мария")
    assert await found(env, str(USER)) == [uid]
    assert await found(env, f"#{uid}") == [uid]
    assert await found(env, "@ivan_petrov") == [uid]
    assert await found(env, "IVAN_PETROV") == [uid]
    assert (await found(env, "ivan_petorv"))[:1] == [uid]  # a typo: pg_trgm similarity
    assert await found(env, "петров") == []
    assert await found(env, "Мари") == [other]


async def test_search_by_panel_identity_link_and_payment(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid, username="sv_5005", short_uuid="AbCdEf12", panel_user_id=777)
    inst = await add_instance(env.db)
    pay = await add_payment(env.db, inst, uid, 100)
    assert await found(env, "sv_5005") == [uid]
    assert await found(env, "AbCdEf12") == [uid]
    assert await found(env, "https://sub.example/AbCdEf12") == [uid]
    assert await found(env, "777") == [uid]  # panel user id
    assert await found(env, pay) == [uid]
    public = (await env.db.raw("select public_id from users where id = $1", uid))[0]["public_id"]
    assert await found(env, public) == [uid]


async def test_like_wildcards_are_literal(env: UEnv) -> None:
    await env.add(OTHER, username="a_b", first_name="Икс")
    assert await found(env, "%%") == []
    assert await found(env, "a_b") == [env.ids[OTHER]]


# ------------------------------------------------------------------------------------------- screens


async def test_search_form_and_results(env: UEnv) -> None:
    await env.add(OTHER, username="ivan_sidorov", first_name="Иван")
    await env.click(SUPPORT, encode(SCREEN_FIND))
    assert "Кого ищем" in env.text
    await env.type(SUPPORT, "x")
    assert "Слишком коротко" in env.text
    await env.type(SUPPORT, "Иван")
    assert "Найдено по «Иван»: 2" in env.text
    assert any("ivan_petrov" in lb for lb in env.labels())
    await env.press(SUPPORT, "ivan_petrov")
    assert "<code>5005</code>" in env.text


async def test_single_hit_opens_the_card_and_nothing_found(env: UEnv) -> None:
    await env.click(SUPPORT, encode(SCREEN_FIND))
    await env.type(SUPPORT, "@ivan_petrov")
    assert "<code>5005</code>" in env.text and "Иван" in env.text
    await env.click(SUPPORT, encode(SCREEN_FIND))
    await env.type(SUPPORT, "zzzzzz")
    assert "никого не нашлось" in env.text


async def test_search_is_staff_only(env: UEnv) -> None:
    await env.add(OTHER)
    await env.click(OTHER, encode(SCREEN_FIND))
    await env.click(OTHER, encode(SCREEN_RESULTS))
    assert env.toasts[-2:] == ["Нет прав", "Нет прав"]


async def test_user_command(env: UEnv) -> None:
    assert await env.screens.handle_command(text_message(SUPPORT, "/user ivan"), "ivan_petrov")
    assert "<code>5005</code>" in env.text
    assert await env.screens.handle_command(text_message(ADMIN, "/user"), None)
    assert "Кого ищем" in env.text
    assert not await env.screens.handle_command(text_message(USER, "/user x"), "ivan")
    assert not await env.screens.handle_command(text_message(ADMIN, "/user x", chat_type="group"), "ivan")


# ------------------------------------------------------------------------------------------- admin group


def group_press(tg_id: int, data: str) -> tuple[CallbackQuery, list[tuple[str | None, bool | None]]]:
    answers: list[tuple[str | None, bool | None]] = []
    msg = Message(message_id=1, date=DATE, chat=Chat(id=-100500, type="supergroup"), text="card")
    query = CallbackQuery(id="g1", from_user=tg_user(tg_id), chat_instance="ci", data=data, message=msg)

    async def answer(text: str | None = None, show_alert: bool | None = None, **_kw: Any) -> None:
        answers.append((text, show_alert))

    object.__setattr__(query, "answer", answer)
    return query, answers


async def test_group_member_without_a_bot_role_gets_no_rights(env: UEnv) -> None:
    data = card_button(env.ids[USER]).callback_data
    assert data is not None
    stranger = 8_888_888  # in the admin group, but not a user of the bot at all
    query, answers = group_press(stranger, data)
    await env.screens.handle_group_button(query)
    assert answers == [("Нет прав", True)]
    await env.add(OTHER)  # a bot user without a role
    query, answers = group_press(OTHER, data)
    await env.screens.handle_group_button(query)
    assert answers == [("Нет прав", True)]
    denied = [r for r in await env.audit() if r["action"] == "access_denied"]
    assert [r["details"]["telegram_id"] for r in denied] == [stranger, OTHER]
    query, _ = group_press(OTHER, data)  # throttled: one audit row per minute per person
    await env.screens.handle_group_button(query)
    assert len([r for r in await env.audit() if r["action"] == "access_denied"]) == 2
    assert not env.rendered()


async def test_group_button_opens_the_card_in_private(env: UEnv) -> None:
    query, answers = group_press(SUPPORT, f"auc:{env.ids[USER]}")
    await env.screens.handle_group_button(query)
    assert answers == [("Карточка открыта в личке с ботом", False)]
    assert env.rendered()[-1].chat_id == SUPPORT and "<code>5005</code>" in env.text


async def test_group_button_rechecks_the_database_role(env: UEnv) -> None:
    """The cached context still says «support», the database already revoked the role."""
    await env.db.raw("update users set role = 'user' where telegram_id = $1", SUPPORT)
    query, answers = group_press(SUPPORT, f"auc:{env.ids[USER]}")
    await env.screens.handle_group_button(query)
    assert answers == [("Нет прав", True)] and not env.rendered()


async def test_group_button_forged_data(env: UEnv) -> None:
    query, answers = group_press(SUPPORT, "auc:abc")
    await env.screens.handle_group_button(query)
    assert answers[0][0] == "Нет прав"
