"""Support tickets (07 §2.4.6): topic per user, copies both ways, close / reopen, rights, link mode."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.core.clock import now
from svbg.ops.daily_report import collect, render
from tests.support.conftest import ADMIN, GROUP, MEMBER, SUPPORT, USER, Kit

pytestmark = pytest.mark.pg


async def _ticket(kit: Kit) -> int:
    """The user writes after «Написать в поддержку»: returns the topic thread."""
    await kit.add(USER, name="Иван")
    await kit.sub(USER)
    kit.service.arm(USER)
    assert await kit.ui.handle_private(kit.user_text(USER, "не работает VPN"))
    [topic] = kit.calls("createForumTopic", chat_id=GROUP)
    return int(topic.result["message_thread_id"])  # type: ignore[index]


async def test_topic_card_and_copies_both_ways(kit: Kit) -> None:
    await kit.add(ADMIN, "admin", name="Админ")
    thread = await _ticket(kit)
    topic = kit.tg.topics(GROUP)[thread]
    assert topic["name"].startswith("🎫 User5005 · Стандарт · до ")
    card = kit.calls("sendMessage", chat_id=GROUP, message_thread_id=thread)[0]
    datas = [b.get("callback_data") for row in card.params["reply_markup"]["inline_keyboard"] for b in row]
    uid = kit.ids[USER]
    assert datas == [f"tk:c:{uid}", f"auc:{uid}", f"tk:e:{uid}", f"tk:b:{uid}"]
    [copy_in] = kit.calls("copyMessage", chat_id=GROUP, message_thread_id=thread)
    assert [c.params["text"] for c in kit.calls("sendMessage", chat_id=USER)] == [
        "✅ Передали в поддержку. Ответ придёт сюда."
    ]
    # a second message (a photo): same topic, no new card and no second «передали»
    assert await kit.ui.handle_private(kit.user_photo(USER, "photo-1"))
    assert len(kit.calls("createForumTopic")) == 1 and len(kit.calls("copyMessage", chat_id=GROUP)) == 2
    assert len(kit.calls("sendMessage", chat_id=USER)) == 1
    # the admin answers with a reply to the user's first message → the copy quotes the user's original
    group_id = int(copy_in.result["message_id"])  # type: ignore[index]
    answer = kit.staff_text(ADMIN, thread, "перезагрузите роутер", reply_to=group_id)
    assert await kit.service.staff_message(answer)
    [out] = kit.calls("copyMessage", chat_id=USER)
    assert out.params["from_chat_id"] == GROUP and out.params["reply_parameters"]["message_id"] == 1
    # any message in the topic (not a reply) goes to the user too
    assert await kit.service.staff_message(kit.staff_text(ADMIN, thread, "и ещё"))
    assert len(kit.calls("copyMessage", chat_id=USER)) == 2
    [row] = await kit.sql("select * from tickets")
    assert row["status"] == "open" and row["thread_id"] == thread and row["first_reply_at"] is not None
    dirs = [r["dir"] for r in await kit.sql("select dir from ticket_messages order by id")]
    assert dirs == ["in", "in", "out", "out"]
    # time to the first answer goes to the daily report
    at = now()
    data = await collect(kit.db, at - timedelta(hours=1), at + timedelta(hours=1), at.date(), partial=True)
    assert "🎫 Обращений: 1 · с ответом: 1 · первый ответ (медиана): меньше минуты" in render(
        data, currency="RUB", tz_name="Europe/Moscow"
    )


async def test_close_and_reopen(kit: Kit) -> None:
    await kit.add(SUPPORT, "support", name="Саппорт")
    thread = await _ticket(kit)
    uid = kit.ids[USER]
    assert await kit.ui.press(kit.query(SUPPORT, f"tk:c:{uid}")) == ("Обращение закрыто", False)
    topic = kit.tg.topics(GROUP)[thread]
    assert topic["closed"] and topic["name"].startswith("✅ User5005 · Стандарт")
    assert kit.calls("sendMessage", chat_id=USER)[-1].params["text"].startswith("✅ Обращение закрыто")
    [row] = await kit.sql("select * from tickets")
    assert row["status"] == "closed" and row["closed_by"] == kit.ids[SUPPORT]
    assert (await kit.ui.press(kit.query(SUPPORT, f"tk:c:{uid}")))[0] == "Уже закрыто"
    # the user's next message reopens the same topic as a new episode
    assert await kit.ui.handle_private(kit.user_text(USER, "снова не работает"))
    topic = kit.tg.topics(GROUP)[thread]
    assert not topic["closed"] and topic["name"].startswith("🎫 User5005")
    assert len(kit.calls("createForumTopic")) == 1 and len(kit.calls("reopenForumTopic")) == 1
    rows = await kit.sql("select status, thread_id from tickets order by id")
    assert [(r["status"], r["thread_id"]) for r in rows] == [("closed", thread), ("open", thread)]
    cards = [c for c in kit.calls("sendMessage", chat_id=GROUP) if c.params.get("reply_markup")]
    assert len(cards) == 2 and cards[-1].params["text"].startswith("🔓")


async def test_member_without_role_has_no_rights(kit: Kit) -> None:
    await kit.add(ADMIN, "admin")  # an admin without «subs.grant» / «users.ban»
    thread = await _ticket(kit)
    uid = kit.ids[USER]
    for who in (MEMBER, ADMIN):
        assert await kit.ui.press(kit.query(who, f"tk:e:{uid}")) == ("Нет прав", True)
        assert await kit.ui.press(kit.query(who, f"tk:b:{uid}")) == ("Нет прав", True)
    assert await kit.ui.press(kit.query(MEMBER, f"tk:c:{uid}")) == ("Нет прав", True)
    assert (await kit.sql("select status from tickets"))[0]["status"] == "open"
    # a member's message in the topic is not copied to the user
    assert await kit.service.staff_message(kit.staff_text(MEMBER, thread, "привет"))
    assert kit.calls("copyMessage", chat_id=USER) == []


async def test_not_armed_and_link_mode_create_no_topics(kit: Kit) -> None:
    await kit.add(USER, name="Иван")
    assert not await kit.ui.handle_private(kit.user_text(USER, "просто текст"))  # no dialog started
    kit.cfg["SUPPORT_MODE"] = "link"
    kit.service.arm(USER)
    assert not await kit.ui.handle_private(kit.user_text(USER, "помогите"))
    assert not await kit.ui.handle_private(kit.user_text(USER, "/start"))
    assert kit.calls("createForumTopic") == [] and kit.calls("copyMessage") == []
