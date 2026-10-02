"""ErrorHub → «🚨 Ошибки»: one message per group, throttled edits, mute buttons with role checks, attention
relay, and the rule that the admin chat's own delivery errors never come back into the chat."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.core.attention import AttentionService
from svbg.core.bus import Event, EventBus
from svbg.core.errors import ErrorGroupView
from svbg.core.errors.tables import error_groups
from svbg.services.admin_chat import K_ERRORS, K_PAYMENTS, K_SYSTEM, PostResult
from svbg.tg.admin_chat_sink import (
    A_STATUS,
    ACTIONS,
    POST_EVENT,
    AdminChatDeliveryError,
    AdminChatSink,
    AttentionRelay,
    ErrorActions,
    error_buttons,
)
from svbg.tg.notifier import Priority
from tests.fakes.telegram import FakeTelegram
from tests.tg.test_admin_chat_harness import (
    ADMIN,
    GROUP,
    MEMBER,
    OWNER,
    START,
    ChatEnv,
    build_env,
    button,
    buttons,
    click,
    sends,
)


def boom(n: int = 0) -> Exception:
    try:
        raise ValueError(f"оплата не прошла, token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi #{n}")
    except ValueError as exc:
        return exc


class RecordingSink:
    def __init__(self) -> None:
        self.new: list[ErrorGroupView] = []
        self.updates: list[Any] = []

    async def send_new(self, view: ErrorGroupView) -> Any:
        self.new.append(view)
        return {"dm": {str(OWNER): 77}}

    async def update(self, view: ErrorGroupView, msg_ref: Any) -> None:
        self.updates.append(msg_ref)


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


@pytest.fixture
async def env(pg_dsn: str, tg: FakeTelegram) -> AsyncIterator[ChatEnv]:
    e = await build_env(pg_dsn, tg, sink=AdminChatSink)
    await e.service.ensure_topics()
    ErrorActions(e.router, e.hub, e.service, clock=e.clock).install()
    try:
        yield e
    finally:
        await e.close()


def error_sends(env: ChatEnv) -> list[Any]:
    thread = env.service.state(K_ERRORS).thread_id
    return [c for c in sends(env, GROUP) if c.params.get("message_thread_id") == thread]


async def capture(env: ChatEnv, n: int = 1, *, user_id: int | None = 5) -> str:
    fp = None
    exc = boom()
    for _ in range(n):
        fp = await env.hub.capture(
            exc, "screen:subscription", user_id=user_id, handled="экран показан без блока"
        )
    await env.hub.drain()
    await asyncio.sleep(0.05)
    assert fp is not None
    return fp


async def test_first_occurrence_is_one_message_repeats_edit_it_at_most_once_a_minute(env: ChatEnv) -> None:
    fp = await capture(env, 50)
    reports = error_sends(env)
    assert len(reports) == 1  # 50 identical exceptions → one message
    report = reports[0]
    text = report.params["text"]
    assert report.params["parse_mode"] == "HTML" and text.startswith("🚨")
    assert "<blockquote expandable>" in text and "123456:ABCDEF" not in text  # secrets are masked
    assert button(report, "🔕 Заглушить 1 ч") == f"v1:{ACTIONS}:m1:{fp}"
    assert button(report, "⚙️ Состояние") == f"v1:{ACTIONS}:{A_STATUS}"
    msg_id = report.result["message_id"]

    async with env.db.read() as conn:
        ref = (await conn.execute(sa.select(error_groups.c.chat_ref))).scalar_one()
    assert ref == {"chat": GROUP, "msg": msg_id}

    env.clock.now = START + timedelta(seconds=30)
    await capture(env, 3)
    await env.hub.flush()
    await env.hub.drain()
    assert env.tg.calls_for("editMessageText") == []  # throttled: not more than once a minute

    env.clock.now = START + timedelta(seconds=61)
    assert await env.hub.flush() == 1
    await env.hub.drain()
    edits = env.tg.calls_for("editMessageText")
    assert len(edits) == 1 and edits[0].params["message_id"] == msg_id
    assert "×53" in edits[0].params["text"]
    assert len(error_sends(env)) == 1  # still one message

    assert await env.hub.flush() == 0  # nothing new: no edit
    env.clock.now = START + timedelta(seconds=90)
    await capture(env, 1)
    assert len(env.tg.calls_for("editMessageText")) == 1

    other = ValueError("другая ошибка")
    await env.hub.capture(other, "job:payments")
    await env.hub.drain()
    await asyncio.sleep(0.05)
    assert len(error_sends(env)) == 2  # a different exception is a different message


async def test_mute_buttons_check_roles_on_every_click(env: ChatEnv) -> None:
    fp = await capture(env)
    report = error_sends(env)[0]
    msg_id = report.result["message_id"]
    mute_1h = button(report, "🔕 Заглушить 1 ч")

    assert await click(env, MEMBER, mute_1h, message_id=msg_id) == "Нет прав"
    group = await env.hub.get(fp)
    assert group is not None and group.status == "open"

    toast = await click(env, ADMIN, mute_1h, message_id=msg_id)
    assert toast == "🔕 Заглушено до 01.10 13:00 UTC"
    group = await env.hub.get(fp)
    assert group is not None and group.status == "muted"
    assert group.muted_until == START + timedelta(hours=1)
    await asyncio.sleep(0.1)
    edit = env.tg.calls_for("editMessageText")[-1]
    assert edit.params["message_id"] == msg_id and "🔕 Заглушено до" in edit.params["text"]
    assert [t for t, _ in buttons(edit)] == ["🔔 Включить", "⚙️ Состояние"]

    env.clock.now = START + timedelta(minutes=5)
    await capture(env, 5)  # muted: counted, not delivered
    env.clock.now = START + timedelta(minutes=7)
    await env.hub.flush()
    await env.hub.drain()
    assert len(error_sends(env)) == 1
    assert len(env.tg.calls_for("editMessageText")) == 1

    unmute = button(edit, "🔔 Включить")
    assert await click(env, MEMBER, unmute, message_id=msg_id) == "Нет прав"
    assert (
        await click(env, OWNER, unmute, message_id=msg_id) == "🔔 Уведомления об этой ошибке снова включены"
    )
    group = await env.hub.get(fp)
    assert group is not None and group.status == "open"

    toast_24h = await click(env, OWNER, button(report, "🔕 24 ч"), message_id=msg_id)
    assert toast_24h == "🔕 Заглушено до 02.10 12:07 UTC"

    assert (
        await click(env, ADMIN, f"v1:{ACTIONS}:m1:not-a-fingerprint", message_id=msg_id) == "Кнопка устарела"
    )
    assert await click(env, ADMIN, f"v1:{ACTIONS}:m1:{'0' * 40}", message_id=msg_id) == (
        "Эта ошибка уже удалена из журнала"
    )


async def test_status_button_opens_the_screen_in_private_chat(env: ChatEnv) -> None:
    await capture(env)
    report = error_sends(env)[0]
    msg_id = report.result["message_id"]
    status = button(report, "⚙️ Состояние")

    assert await click(env, MEMBER, status, message_id=msg_id) == "Нет прав"
    assert await click(env, ADMIN, status, message_id=msg_id) == "Нет прав"  # admin without system.view
    assert sends(env, OWNER) == []
    assert await click(env, OWNER, status, message_id=msg_id) == "Открыл «Состояние» в личном чате с ботом"
    async with asyncio.timeout(5):
        while not sends(env, OWNER):
            await asyncio.sleep(0.02)
    assert env.tg.message(GROUP, msg_id) is not None  # the report itself is untouched
    assert all(c.params.get("message_id") != msg_id for c in env.tg.calls_for("editMessageText"))


async def test_own_send_errors_are_not_reported_and_go_to_owner_dm(env: ChatEnv) -> None:
    env.tg.fail_next(
        "400",
        method="sendMessage",
        chat_id=GROUP,
        count=1000,
        description="Bad Request: not enough rights to send text messages to the chat",
    )
    await capture(env)
    async with asyncio.timeout(5):
        while not sends(env, OWNER):
            await asyncio.sleep(0.02)
    await env.hub.drain()

    failed = [c for c in env.tg.calls_for("sendMessage") if c.params.get("chat_id") == GROUP]
    assert len(failed) == 5 and not any(c.ok for c in failed)  # five attempts, then the owner's DM
    dm = sends(env, OWNER)[-1]
    assert dm.params["text"].startswith("<b>⚠️ Админ-чат недоступен: у бота нет прав в группе")
    assert "Что проверить" in dm.params["text"]

    async with env.db.read() as conn:
        groups = (await conn.execute(sa.select(error_groups.c.place, error_groups.c.chat_ref))).all()
    assert [g.place for g in groups] == ["screen:subscription"]  # the chat's failure was not reported
    assert groups[0].chat_ref == {"dm": {str(OWNER): dm.result["message_id"]}}

    env.clock.now = START + timedelta(seconds=61)
    await capture(env, 2)
    await env.hub.flush()
    await env.hub.drain()
    async with env.db.read() as conn:
        assert (await conn.execute(sa.select(sa.func.count()).select_from(error_groups))).scalar_one() == 1


async def test_sink_without_admin_chat_uses_fallback(pg_dsn: str, tg: FakeTelegram) -> None:
    fallback = RecordingSink()
    env = await build_env(pg_dsn, tg, chat=None, sink=lambda svc: AdminChatSink(svc, fallback))
    try:
        await env.hub.capture(boom(), "job:x")
        await env.hub.drain()
        assert len(fallback.new) == 1
        env.clock.now = START + timedelta(seconds=61)
        await env.hub.capture(boom(), "job:x")
        await env.hub.flush()
        await env.hub.drain()
        assert fallback.updates == [{"dm": {str(OWNER): 77}}]
        assert tg.calls_for("sendMessage") == []
    finally:
        await env.close()


async def test_sink_update_failures_and_refs(env: ChatEnv) -> None:
    sink = AdminChatSink(env.service)
    view = ErrorGroupView(
        fingerprint="a" * 40, place="p", title="t", hint="h", first_seen=START, last_seen=START
    )
    await sink.update(view, {"dropped": True})  # topic disabled with «не отправлять»: nothing to edit
    await sink.update(view, "garbage")
    with pytest.raises(AdminChatDeliveryError):
        await sink.update(view, {"chat": -42, "msg": 5})  # not our chat, message gone: the hub retries

    await env.service.set_enabled(K_ERRORS, False)
    await env.service.set_fallback(K_ERRORS, "drop")
    assert await sink.send_new(view) == {"dropped": True}

    muted = ErrorGroupView(
        fingerprint="b" * 40,
        place="p",
        title="t",
        hint="h",
        first_seen=START,
        last_seen=START,
        status="muted",
        muted_until=START + timedelta(hours=1),
    )
    assert [b.text for row in error_buttons(muted) for b in row] == ["🔔 Включить", "⚙️ Состояние"]


async def test_attention_and_bus_posts_reach_topics(env: ChatEnv) -> None:
    bus = EventBus()
    attention = AttentionService(env.db, bus=bus)
    off = AttentionRelay(env.service, attention).install(bus)
    system = env.service.state(K_SYSTEM).thread_id

    await attention.raise_item(
        "component:remnawave", "error", "Панель недоступна", "Проверьте токен <панели>"
    )
    raised = await env.tg.wait_for(
        "sendMessage",
        lambda c: c.params.get("message_thread_id") == system and "внимания" in c.params["text"],
    )
    assert raised.params["text"] == (
        "🔴 <b>Требует внимания:</b> Панель недоступна\nПроверьте токен &lt;панели&gt;"
    )
    assert button(raised, "⚙️ Состояние") == f"v1:{ACTIONS}:{A_STATUS}"

    await attention.resolve("component:remnawave")
    await env.tg.wait_for("sendMessage", lambda c: "Решено" in c.params["text"])

    await bus.publish(Event(POST_EVENT, {"kind": K_PAYMENTS, "text": "💳 <b>+500 ₽</b>", "html": True}))
    paid = await env.tg.wait_for("sendMessage", lambda c: "+500" in c.params["text"])
    assert paid.params["message_thread_id"] == env.service.state(K_PAYMENTS).thread_id
    assert paid.params["parse_mode"] == "HTML"

    before = len(env.tg.calls)
    await bus.publish(Event(POST_EVENT, {"kind": K_PAYMENTS, "text": ""}))  # ignored, never raises
    await asyncio.sleep(0.05)
    assert len(env.tg.calls) == before
    off()


class SlowService:
    """Just enough of AdminChatService: ``post`` waits for ``gate`` (a report stuck in the queue)."""

    configured = True

    def __init__(self, results: list[PostResult | None]) -> None:
        self.gate = asyncio.Event()
        self.results = results
        self.posts = 0

    async def post(self, kind: str, text: str, **kw: Any) -> PostResult | None:
        self.posts += 1
        assert kind == K_ERRORS and kw["wait"] is True and kw["priority"] == Priority.CRITICAL
        await self.gate.wait()
        return self.results.pop(0)


async def test_report_outliving_the_hub_timeout_is_not_posted_twice() -> None:
    service = SlowService([PostResult(K_ERRORS, chat_id=GROUP, message_id=7), None])
    sink = AdminChatSink(service)  # type: ignore[arg-type]
    view = ErrorGroupView(
        fingerprint="c" * 40, place="p", title="t", hint="h", first_seen=START, last_seen=START,
        episode_started_at=START,
    )  # fmt: skip
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):  # ErrorHub's sink_timeout ran out (e.g. a Telegram outage)
            await sink.send_new(view)
    service.gate.set()  # the report was still queued and is delivered now
    await asyncio.sleep(0)
    assert await sink.send_new(view) == {"chat": GROUP, "msg": 7}  # the hub's retry picks it up
    assert service.posts == 1

    later = ErrorGroupView(
        fingerprint="c" * 40, place="p", title="t", hint="h", first_seen=START, last_seen=START,
        episode_started_at=START + timedelta(hours=2),
    )  # fmt: skip
    assert await sink.send_new(later) is None  # a new episode is a new report; this one was not delivered
    assert service.posts == 2
    service.results.append(PostResult(K_ERRORS, chat_id=GROUP, message_id=9))
    assert await sink.send_new(later) == {"chat": GROUP, "msg": 9}  # a failed delivery is posted again
    assert service.posts == 3
