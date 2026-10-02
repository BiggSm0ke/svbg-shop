"""AdminChatService against the fake Bot API over HTTP and a real PostgreSQL (topics, routing, healing)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import sqlalchemy as sa
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import InlineKeyboardButton, MessageEntity

import svbg.services.tables  # noqa: F401 - registers admin_* tables before create_schema
from svbg.core.component import Health, ProbeError
from svbg.db.engine import Database
from svbg.db.schema import create_schema
from svbg.services.admin_chat import (
    CORE_TOPICS,
    K_ERRORS,
    K_NEW_USERS,
    K_PAYMENTS,
    K_SYSTEM,
    K_TICKETS,
    K_TRIALS,
    AdminChatService,
    TopicDef,
)
from svbg.services.tables import admin_cards, admin_topics
from svbg.tg.notifier import Notifier
from svbg.tg.runner import BotHolder
from tests.fakes.telegram import FakeTelegram

GROUP = -1001234567890
OWNER = 111
CORE_ENABLED = [d.kind for d in CORE_TOPICS if d.default_enabled]


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[Database]:
    await create_schema(pg_dsn)
    database = Database(pg_dsn)
    await database.start()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def bot(tg: FakeTelegram) -> AsyncIterator[Bot]:
    token = tg.add_bot()
    b = Bot(token, session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)))
    tg.make_admin(GROUP, b.id, can_manage_topics=True, can_pin_messages=True, can_delete_messages=True)
    try:
        yield b
    finally:
        await b.session.close()


Factory = Callable[..., AdminChatService]


@pytest.fixture
async def make(db: Database, bot: Bot) -> AsyncIterator[Factory]:
    made: list[tuple[AdminChatService, Notifier]] = []

    def factory(
        *, owners: tuple[int, ...] = (OWNER,), chat: int | None = GROUP, **kw: Any
    ) -> AdminChatService:
        holder = BotHolder(bot)
        notifier = Notifier(holder)

        async def owner_ids() -> frozenset[int]:
            return frozenset(owners)

        kw.setdefault("retry_base", 0.01)
        svc = AdminChatService(db, notifier, holder, owners=owner_ids, **kw)
        svc.set_chat(chat)
        made.append((svc, notifier))
        return svc

    yield factory
    for svc, notifier in made:
        await svc.stop(grace=1.0)
        await notifier.close()


async def started(svc: AdminChatService) -> AdminChatService:
    await svc.start()
    return svc


async def topic_rows(db: Database) -> dict[str, dict[str, Any]]:
    async with db.read() as conn:
        rows = (await conn.execute(sa.select(admin_topics))).mappings().all()
    return {r["kind"]: dict(r) for r in rows}


def sends_to(tg: FakeTelegram, chat_id: int) -> list[Any]:
    return [c for c in tg.calls_for("sendMessage") if c.params.get("chat_id") == chat_id]


# ------------------------------------------------------------------ probe


async def test_check_chat_accepts_forum_with_admin_rights(make: Factory) -> None:
    svc = make()
    check = await svc.check_chat(GROUP)
    assert check.chat_id == GROUP and check.title == f"Group {GROUP}" and check.warnings == ()


async def test_check_chat_human_errors(make: Factory, tg: FakeTelegram, bot: Bot) -> None:
    svc = make()
    with pytest.raises(ProbeError, match="отрицательное число"):
        await svc.check_chat(12345)

    tg.chats[-1002] = {"is_forum": False}
    with pytest.raises(ProbeError, match=r"выключены темы.*Проверить снова"):
        await svc.check_chat(-1002)

    with pytest.raises(ProbeError, match="не администратор"):
        await svc.check_chat(-1003)  # the bot is an ordinary member there

    tg.make_admin(-1004, bot.id, can_pin_messages=True)
    with pytest.raises(ProbeError, match="нет прав: управление темами"):
        await svc.check_chat(-1004)

    tg.make_admin(-1005, bot.id, can_manage_topics=True)
    check = await svc.check_chat(-1005)
    assert check.warnings == ("закрепление сообщений", "удаление сообщений")


async def test_check_chat_refuses_a_public_group(make: Factory, tg: FakeTelegram, bot: Bot) -> None:
    svc = make()
    for chat_id, extra in ((-1006, {"username": "svbg_admins"}), (-1007, {"active_usernames": ["svbg_ops"]})):
        tg.make_admin(chat_id, bot.id, can_manage_topics=True)
        tg.chats[chat_id] = extra
        with pytest.raises(ProbeError, match=r"Группа публичная \(@svbg_(admins|ops)\).*Частная"):
            await svc.check_chat(chat_id)
    tg.chats[-1006] = {}  # made private: accepted
    assert (await svc.check_chat(-1006)).chat_id == -1006

    tg.fail_next("400", method="getChat", description="Bad Request: chat not found")
    with pytest.raises(ProbeError, match="не видит эту группу"):
        await svc.check_chat(-1006)

    tg.fail_next("403", method="getChat", description="Forbidden: bot was kicked from the supergroup chat")
    with pytest.raises(ProbeError, match="удалили из группы"):
        await svc.check_chat(-1007)

    tg.chats[-1008] = {"type": "group"}
    with pytest.raises(ProbeError, match="не супергруппа"):
        await svc.check_chat(-1008)


async def test_component_probe_and_reconfigure(make: Factory, tg: FakeTelegram, db: Database) -> None:
    svc = await started(make(chat=None))
    assert svc.name == "admin_chat"
    assert (await svc.health()).status is Health.DISABLED
    await svc.probe({"ADMIN_CHAT_ID": None})  # owner DMs: nothing to check
    with pytest.raises(ProbeError):
        await svc.probe({"ADMIN_CHAT_ID": -1003})

    await svc.reconfigure({"ADMIN_CHAT_ID": GROUP})
    assert svc.chat_id == GROUP
    async with asyncio.timeout(5):  # topics are created in the background
        while len(tg.topics(GROUP)) < len(CORE_ENABLED):
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    report = await svc.health()
    assert report.status is Health.OK and "тем: 9" in report.summary
    assert set(await topic_rows(db)) == set(CORE_ENABLED)


# ------------------------------------------------------------------ topics


async def test_creates_nine_topics_with_icons_and_stores_thread_ids(
    make: Factory, tg: FakeTelegram, db: Database
) -> None:
    tg.forum_icons.pop("🧳")  # no icon for «Бэкапы» in the set: colour + emoji in the name
    svc = await started(make())
    report = await svc.ensure_topics()
    assert report.ok and sorted(report.created) == sorted(CORE_ENABLED) and report.existing == []
    assert K_TICKETS not in report.created  # tickets are off until the module is enabled

    created = tg.topics(GROUP)
    assert len(created) == 9
    by_name = {t["name"]: t for t in created.values()}
    assert by_name["Оплаты и пополнения"]["icon_custom_emoji_id"] == tg.forum_icons["💰"]
    assert by_name["Ошибки"]["icon_custom_emoji_id"] == tg.forum_icons["❗️"]
    assert by_name["💾 Бэкапы"].get("icon_custom_emoji_id") is None

    rows = await topic_rows(db)
    assert set(rows) == set(CORE_ENABLED)
    assert {r["thread_id"] for r in rows.values()} == set(created)
    assert all(r["chat_id"] == GROUP and r["enabled"] for r in rows.values())

    again = await svc.ensure_topics()
    assert again.created == [] and sorted(again.existing) == sorted(CORE_ENABLED)

    restarted = await started(make())  # a new process reads the ids from the database
    assert (await restarted.ensure_topics()).created == []
    assert len(tg.calls_for("createForumTopic")) == 9


async def test_ensure_topics_stops_on_missing_rights(make: Factory, tg: FakeTelegram, bot: Bot) -> None:
    tg.make_admin(GROUP, bot.id, can_pin_messages=True)  # rights to manage topics were taken away
    svc = await started(make())
    report = await svc.ensure_topics()
    assert not report.ok and report.created == []
    assert list(report.failed.values()) == [
        "у бота нет прав в группе (нужен администратор с управлением темами)"
    ]
    assert len(tg.calls_for("createForumTopic")) == 1  # not hammered for every topic
    health = await svc.health()
    assert health.status is Health.DEGRADED and "Оплаты" in health.summary
    assert health.fix_action == "screen:achat"


async def test_post_goes_to_its_topic(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    pay_thread = svc.state(K_PAYMENTS).thread_id
    result = await svc.post(
        K_PAYMENTS,
        "💳 Оплата 500 ₽",
        entities=[MessageEntity(type="bold", offset=0, length=2)],
        buttons=[[InlineKeyboardButton(text="Открыть", callback_data="v1:x:o")]],
        wait=True,
    )
    assert result is not None and result.delivered
    assert result.chat_id == GROUP and result.thread_id == pay_thread
    call = sends_to(tg, GROUP)[-1]
    assert call.params["message_thread_id"] == pay_thread
    assert call.params["entities"] == [{"type": "bold", "offset": 0, "length": 2}]
    assert call.params["reply_markup"]["inline_keyboard"][0][0]["text"] == "Открыть"
    assert "disable_notification" not in call.params  # payments are loud

    await svc.post(K_NEW_USERS, "👤 Новый пользователь #5", wait=True)
    assert sends_to(tg, GROUP)[-1].params["disable_notification"] is True  # low priority is silent

    unknown = await svc.post("nope", "x", wait=True)  # a typo in a kind never loses a message
    assert unknown is not None and unknown.thread_id == svc.state(K_SYSTEM).thread_id


async def test_post_validates_arguments(make: Factory) -> None:
    svc = make()
    with pytest.raises(ValueError, match="empty"):
        await svc.post(K_PAYMENTS, "  ")
    with pytest.raises(ValueError, match="either"):
        await svc.post(K_PAYMENTS, "x", html=True, entities=[MessageEntity(type="bold", offset=0, length=1)])
    with pytest.raises(ValueError, match="card_ref"):
        await svc.post(K_PAYMENTS, "x", card_ref="a\nb")
    with pytest.raises(ValueError, match="kind"):
        TopicDef("Bad Kind", "x", "x")
    with pytest.raises(ValueError, match="already registered"):
        svc.register_topic(TopicDef(K_PAYMENTS, "Другое", "💳"))


async def test_disable_topic_falls_back_to_system_or_drops(
    make: Factory, tg: FakeTelegram, db: Database
) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    trials_thread = svc.state(K_TRIALS).thread_id
    assert trials_thread is not None

    await svc.set_enabled(K_TRIALS, False)
    assert tg.topics(GROUP)[trials_thread]["closed"] is True
    assert (await topic_rows(db))[K_TRIALS]["enabled"] is False

    result = await svc.post(K_TRIALS, "🎁 Триал взял #7", wait=True)
    assert result is not None and result.thread_id == svc.state(K_SYSTEM).thread_id
    call = sends_to(tg, GROUP)[-1]
    assert call.params["text"] == "🎁 Триалы\n🎁 Триал взял #7"
    assert call.params["entities"][0] == {"type": "bold", "offset": 0, "length": len("🎁 Триалы") + 1}

    await svc.set_fallback(K_TRIALS, "drop")
    before = len(sends_to(tg, GROUP))
    dropped = await svc.post(K_TRIALS, "🎁 Триал взял #8", wait=True)
    assert dropped is not None and dropped.dropped and not dropped.delivered
    assert len(sends_to(tg, GROUP)) == before
    assert (await topic_rows(db))[K_TRIALS]["fallback"] == "drop"

    await svc.set_enabled(K_TRIALS, True)
    assert tg.topics(GROUP)[trials_thread]["closed"] is False
    result = await svc.post(K_TRIALS, "🎁 Триал взял #9", wait=True)
    assert result is not None and result.thread_id == trials_thread


async def test_html_fallback_header_and_disabled_system_uses_general(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    await svc.set_enabled(K_PAYMENTS, False)
    await svc.set_enabled(K_SYSTEM, False)
    result = await svc.post(K_PAYMENTS, "<b>500 ₽</b> от #1", html=True, wait=True)
    assert result is not None and result.thread_id is None  # «General» topic
    call = sends_to(tg, GROUP)[-1]
    assert call.params["text"] == "<b>💳 Оплаты и пополнения</b>\n<b>500 ₽</b> от #1"
    assert call.params["parse_mode"] == "HTML" and "message_thread_id" not in call.params


async def test_deleted_topic_is_recreated_and_message_resent(
    make: Factory, tg: FakeTelegram, db: Database
) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    old = svc.state(K_ERRORS).thread_id
    assert old is not None
    tg.delete_topic(GROUP, old)  # an admin deleted «Ошибки» by hand

    result = await svc.post(K_ERRORS, "🚨 Что-то сломалось", wait=True)
    assert result is not None and result.thread_id not in (None, old)
    failed = [c for c in sends_to(tg, GROUP) if not c.ok]
    assert len(failed) == 1 and "thread not found" in (failed[0].description or "")
    assert sends_to(tg, GROUP)[-1].params["message_thread_id"] == result.thread_id
    assert len(tg.calls_for("createForumTopic")) == 10

    row = (await topic_rows(db))[K_ERRORS]
    assert row["thread_id"] == result.thread_id and row["recreated_at"] is not None
    assert row["last_error"] == "тема была удалена — создана заново"
    assert svc.stats["recreated"] == 1

    again = await svc.post(K_ERRORS, "🚨 Ещё раз", wait=True)
    assert again is not None and again.thread_id == result.thread_id


async def test_five_failures_go_to_owner_dm_and_recover(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make(recheck_interval=3600))
    await svc.ensure_topics()
    tg.fail_next(
        "400",
        method="sendMessage",
        chat_id=GROUP,
        count=1000,
        description="Bad Request: not enough rights to send text messages to the chat",
    )
    result = await svc.post(K_ERRORS, "🚨 Ошибка оплаты", wait=True)
    assert result is not None and result.message_id is None and set(result.dm) == {OWNER}
    group_attempts = len(sends_to(tg, GROUP))
    assert group_attempts == 5
    dm = sends_to(tg, OWNER)[-1]
    assert dm.params["text"].startswith("⚠️ Админ-чат недоступен: у бота нет прав в группе")
    assert dm.params["text"].endswith("🚨 Ошибка оплаты")
    health = await svc.health()
    assert health.status is Health.DOWN and health.fix_action == "screen:achat"
    assert svc.down

    second = await svc.post(K_PAYMENTS, "💳 Оплата", wait=True)  # the chat is down: straight to the owner
    assert second is not None and set(second.dm) == {OWNER}
    assert len(sends_to(tg, GROUP)) == group_attempts

    tg.clear_faults()
    svc._recheck_interval = 0  # the half-open check is due
    third = await svc.post(K_PAYMENTS, "💳 Оплата 2", wait=True)
    assert third is not None and third.chat_id == GROUP
    assert not svc.down and (await svc.health()).status is Health.OK


async def test_transient_failure_is_retried(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    tg.fail_next("500", method="sendMessage", chat_id=GROUP, count=5)  # notifier retries 3, we retry again
    notifier_retries = svc._notifier.limits
    assert notifier_retries.max_retries_transient == 3
    svc._notifier.limits = type(notifier_retries)(transient_backoff=0.01)
    result = await svc.post(K_PAYMENTS, "💳 Оплата", wait=True)
    assert result is not None and result.chat_id == GROUP
    assert svc.stats["failures"] == 1 and not svc.down


async def test_without_admin_chat_messages_go_to_owners(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make(chat=None, owners=(OWNER, 222)))
    result = await svc.post(K_PAYMENTS, "Оплата 100 ₽", wait=True)
    assert result is not None and set(result.dm) == {OWNER, 222}
    assert sends_to(tg, OWNER)[-1].params["text"] == "💳 Оплаты и пополнения\nОплата 100 ₽"
    assert tg.calls_for("createForumTopic") == []

    nobody = await started(make(chat=None, owners=()))
    assert await nobody.post(K_PAYMENTS, "x", wait=True) is None
    assert nobody.stats["undelivered"] == 1


async def test_cards_are_edited_in_place(make: Factory, tg: FakeTelegram, bot: Bot, db: Database) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    first = await svc.post(K_TICKETS, "🎫 Тикет #1: открыт", card_ref="ticket:1", wait=True)
    assert first is not None and first.message_id is not None
    assert first.thread_id == svc.state(K_SYSTEM).thread_id  # tickets are off → «Система»

    second = await svc.post(K_TICKETS, "🎫 Тикет #1: в работе", card_ref="ticket:1", wait=True)
    assert second is not None and second.message_id == first.message_id
    edit = tg.calls_for("editMessageText")[-1]
    assert edit.params["message_id"] == first.message_id and edit.params["text"].endswith("в работе")

    edits = len(tg.calls_for("editMessageText"))
    same = await svc.post(K_TICKETS, "🎫 Тикет #1: в работе", card_ref="ticket:1", wait=True)
    assert same is not None and same.message_id == first.message_id
    assert len(tg.calls_for("editMessageText")) == edits  # unchanged card: no request at all

    await bot.delete_message(GROUP, first.message_id)  # someone deleted the card
    third = await svc.post(K_TICKETS, "🎫 Тикет #1: закрыт", card_ref="ticket:1", wait=True)
    assert third is not None and third.message_id not in (None, first.message_id)
    async with db.read() as conn:
        row = (await conn.execute(sa.select(admin_cards))).mappings().one()
    assert (row["kind"], row["ref"], row["msg_id"]) == (K_TICKETS, "ticket:1", third.message_id)

    fresh = await started(make())  # the card mapping survives a restart
    fourth = await fresh.post(K_TICKETS, "🎫 Тикет #1: переоткрыт", card_ref="ticket:1", wait=True)
    assert fourth is not None and fourth.message_id == third.message_id


async def test_edit_of_a_deleted_message_is_resent_and_followed(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    gone = await svc.edit(GROUP, 999, "🚨 отчёт ×2", resend_kind=K_ERRORS, wait=True)
    assert gone is not None and gone.message_id is not None and gone.message_id != 999
    assert gone.thread_id == svc.state(K_ERRORS).thread_id
    moved = await svc.edit(GROUP, 999, "🚨 отчёт ×3", resend_kind=K_ERRORS, wait=True)
    assert moved is not None and moved.message_id == gone.message_id
    assert tg.calls_for("editMessageText")[-1].params["message_id"] == gone.message_id

    assert await svc.edit(GROUP, 998, "без пересылки", wait=True) is None


async def test_queued_edits_of_one_message_coalesce(make: Factory, tg: FakeTelegram) -> None:
    svc = make()  # not started yet: everything waits in the queue
    await svc.edit(GROUP, 5, "v1")
    await svc.edit(GROUP, 5, "v2")
    last = asyncio.ensure_future(svc.edit(GROUP, 5, "v3", wait=True))
    await asyncio.sleep(0)
    assert svc.pending == 1
    await svc.start()
    await asyncio.wait_for(last, 5)
    texts = [c.params["text"] for c in tg.calls_for("editMessageText")]
    assert texts == ["v3"]


async def test_stop_resolves_waiters_and_refuses_new_posts(make: Factory) -> None:
    svc = make()  # never started: the waiter can only be released by stop()
    waiter = asyncio.ensure_future(svc.post(K_PAYMENTS, "x", wait=True))
    await asyncio.sleep(0)
    await svc.stop(grace=0.05)
    assert await asyncio.wait_for(waiter, 1) is None
    assert await svc.post(K_PAYMENTS, "y", wait=True) is None


async def test_module_topic_registration(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    svc.register_topic(TopicDef("lte", "Трафик LTE", "🌐", owner_module="lte"))
    svc.register_topic(TopicDef("lte", "Трафик LTE", "🌐", owner_module="lte"))  # idempotent
    report = await svc.ensure_topics()
    assert "lte" in report.created and len(tg.topics(GROUP)) == 10
    result = await svc.post("lte", "🌐 Блок LTE у #3", wait=True)
    assert result is not None and result.thread_id == svc.state("lte").thread_id
