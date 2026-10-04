"""«🛎 Админ-группа»: connecting the group (request_chat / ID), human errors with «Проверить снова», topic
switches, owner-only access, disconnect and the module ``setup`` wiring
(through the real settings pipeline)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Chat, ChatShared, Message, User

from svbg.core.attention import AttentionService
from svbg.core.bus import EventBus
from svbg.core.component import ComponentRegistry
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings import SettingsService, core_registry
from svbg.core.settings.service import Change
from svbg.services.admin_chat import K_PAYMENTS, SCREEN, AdminChatService
from svbg.tg.admin.connect_chat import REQUEST_ID, ConnectChat, setup
from svbg.tg.admin_chat_sink import AdminChatSink
from svbg.tg.ui.router import ScreenRouter
from tests.fakes.telegram import Call, FakeTelegram
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


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


class Setup(SimpleNamespace):
    env: ChatEnv
    settings: SettingsService
    screens: ConnectChat


async def make_settings(env: ChatEnv, tmp_path: Path, service: AdminChatService) -> SettingsService:
    components = ComponentRegistry()
    components.register(service)
    settings = SettingsService(
        env.db, core_registry(), Crypto([generate_key()]), components, environ={}, env_path=tmp_path / ".env"
    )
    await settings.load()
    return settings


@pytest.fixture
async def s(pg_dsn: str, tg: FakeTelegram, tmp_path: Path) -> AsyncIterator[Setup]:
    env = await build_env(pg_dsn, tg, chat=None)
    settings = await make_settings(env, tmp_path, env.service)
    screens = ConnectChat(env.router, env.service, settings=settings, users=env.directory.load)
    screens.install()
    try:
        yield Setup(env=env, settings=settings, screens=screens)
    finally:
        await env.close()


def tg_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name=f"U{user_id}", language_code="ru")


async def open_screen(s: Setup, user_id: int = OWNER) -> Call:
    user = await s.env.directory.load(tg_user(user_id))
    assert user is not None
    await s.env.router.show(user, user_id, SCREEN, new=True)
    return sends(s.env, user_id)[-1]


def _visible(s: Setup, user_id: int) -> list[Call]:
    """Successful sends and edits in ``user_id``'s chat, in the order Telegram handled them."""
    return [
        c
        for c in s.env.tg.calls
        if c.ok and c.method in ("sendMessage", "editMessageText") and c.params.get("chat_id") == user_id
    ]


def last_screen(s: Setup, user_id: int = OWNER) -> Call:
    return [c for c in _visible(s, user_id) if "🛎 Админ-группа" in c.params["text"].split("\n", 1)[0]][-1]


def last_text(s: Setup, user_id: int = OWNER) -> str:
    """Text of the newest message the user sees (sent or edited)."""
    return str(_visible(s, user_id)[-1].params["text"])


def screen_msg_id(s: Setup, user_id: int = OWNER) -> int:
    call = last_screen(s, user_id)
    return int(call.result["message_id"])


async def press(s: Setup, label: str, user_id: int = OWNER) -> str | None:
    screen = last_screen(s)
    return await click(s.env, user_id, button(screen, label), chat_id=user_id, message_id=screen_msg_id(s))


def shared(user_id: int, chat_id: int = GROUP, request_id: int = REQUEST_ID) -> Message:
    return Message(
        message_id=900,
        date=START,
        chat=Chat(id=user_id, type="private"),
        from_user=tg_user(user_id),
        chat_shared=ChatShared(request_id=request_id, chat_id=chat_id),
    )


# ------------------------------------------------------------------ connect


async def test_owner_connects_by_picking_a_group(s: Setup) -> None:
    screen = await open_screen(s)
    assert "Сейчас уведомления приходят вам в личку" in screen.params["text"]
    assert [t for t, _ in buttons(screen)] == [
        "👥 Выбрать группу",
        "🔢 Ввести ID",
        "✅ Сообщать, когда нода панели падает",
        "⬅️ Связь",
        "🛠 Админка",
    ]

    assert await press(s, "👥 Выбрать группу") is None
    keyboard = sends(s.env, OWNER)[-1].params["reply_markup"]
    request = keyboard["keyboard"][0][0]["request_chat"]
    assert request["request_id"] == REQUEST_ID and request["chat_is_forum"] is True
    assert request["chat_has_username"] is False  # Telegram offers private groups only
    assert request["chat_is_channel"] is False
    rights = request["bot_administrator_rights"]
    assert rights["can_manage_topics"] and rights["can_pin_messages"] and rights["can_delete_messages"]
    assert keyboard["keyboard"][1][0]["text"] == "✖️ Не подключать"

    await s.screens.on_chat_shared(shared(OWNER))
    checking = next(c for c in sends(s.env, OWNER) if c.params["text"] == "Проверяю группу…")
    assert checking.params["reply_markup"] == {"remove_keyboard": True}
    assert s.settings.current()["ADMIN_CHAT_ID"] == GROUP and s.env.service.chat_id == GROUP
    await asyncio.sleep(0.2)  # the background ensure of reconfigure must not duplicate topics
    assert len(s.env.tg.topics(GROUP)) == 9

    final = last_screen(s)
    text = final.params["text"]
    assert f"✅ Группа «Group {GROUP}» подключена. Темы готовы: 9 из 9." in text
    assert f"Группа: <code>{GROUP}</code>" in text and "✅ работает" in text
    assert "Темы: включено 9 из 10." in text
    assert not [t for t, d in buttons(final) if ":tog:" in d]  # the switches are one tap away
    await press(s, "🗂 Темы")
    final = last_screen(s)
    assert "🛎 Админ-группа › <b>🗂 Темы</b>" in final.params["text"]
    toggles = [t for t, d in buttons(final) if ":tog:" in d]
    assert len(toggles) == 10 and sum(t.startswith("✅ ") for t in toggles) == 9
    assert "⬜ 🎫 Тикеты" in toggles


async def test_group_without_topics_gives_a_clear_error_and_recheck(s: Setup) -> None:
    s.env.tg.chats[GROUP] = {"is_forum": False}
    await open_screen(s)
    await s.screens.on_chat_shared(shared(OWNER))
    text = last_screen(s).params["text"]
    assert "❌ Не удалось подключить группу: В группе выключены темы." in text
    assert s.settings.current()["ADMIN_CHAT_ID"] is None and s.env.tg.topics(GROUP) == {}
    assert button(last_screen(s), "🔄 Проверить снова") == f"v1:{SCREEN}:chk:{GROUP}"

    del s.env.tg.chats[GROUP]  # the owner switched topics on
    await press(s, "🔄 Проверить снова")
    assert "подключена. Темы готовы: 9 из 9" in last_screen(s).params["text"]
    assert s.settings.current()["ADMIN_CHAT_ID"] == GROUP


async def test_bot_without_topic_rights_gets_a_clear_error(s: Setup) -> None:
    s.env.tg.make_admin(GROUP, s.env.bot.id, can_pin_messages=True)
    await open_screen(s)
    await s.screens.on_chat_shared(shared(OWNER))
    text = last_screen(s).params["text"]
    assert "У бота нет прав: управление темами" in text
    assert s.settings.current()["ADMIN_CHAT_ID"] is None


async def test_non_owner_cannot_connect(s: Setup) -> None:
    await s.screens.on_chat_shared(shared(ADMIN))
    assert sends(s.env, ADMIN)[-1].params["text"] == "Нет прав"
    assert s.settings.current()["ADMIN_CHAT_ID"] is None and s.env.tg.topics(GROUP) == {}

    screen = await open_screen(s)  # the owner's screen; others clicking its buttons are refused
    for user_id in (ADMIN, MEMBER):
        toast = await click(
            s.env, user_id, button(screen, "👥 Выбрать группу"), chat_id=user_id, message_id=1
        )
        assert toast == "Нет прав"
    with pytest.raises(SkipHandler):
        await s.screens.on_chat_shared(shared(OWNER, request_id=1))  # someone else's request: not ours


async def test_connect_by_id_form(s: Setup) -> None:
    await open_screen(s)
    await press(s, "🔢 Ввести ID")
    assert "Пришлите ID супергруппы" in last_text(s)

    def text_message(text: str) -> Message:
        return Message(
            message_id=901,
            date=START,
            chat=Chat(id=OWNER, type="private"),
            from_user=tg_user(OWNER),
            text=text,
        )

    assert await s.env.router.dispatch_message(text_message("abc"))
    assert "Нужно целое число" in last_text(s)
    assert await s.env.router.dispatch_message(text_message(str(GROUP)))
    assert "подключена. Темы готовы: 9 из 9" in last_screen(s).params["text"]
    assert s.settings.current()["ADMIN_CHAT_ID"] == GROUP


# ------------------------------------------------------------------ topics & disconnect


async def connected(s: Setup) -> None:
    await open_screen(s)
    await s.screens.on_chat_shared(shared(OWNER))
    assert s.env.service.chat_id == GROUP


async def test_topic_switches_and_fallback(s: Setup) -> None:
    await connected(s)
    thread = s.env.service.state(K_PAYMENTS).thread_id
    assert thread is not None
    await press(s, "🗂 Темы")
    toggle = button(last_screen(s), "✅ 💳 Оплаты и пополнения")

    assert await click(s.env, MEMBER, toggle, chat_id=MEMBER, message_id=1) == "Нет прав"
    assert s.env.service.state(K_PAYMENTS).enabled

    await press(s, "✅ 💳 Оплаты и пополнения")
    assert not s.env.service.state(K_PAYMENTS).enabled
    assert s.env.tg.topics(GROUP)[thread]["closed"] is True
    screen = last_screen(s)
    assert ("↪️ в Систему", f"v1:{SCREEN}:fb:{K_PAYMENTS}") in buttons(screen)

    await press(s, "↪️ в Систему")
    assert s.env.service.state(K_PAYMENTS).fallback == "drop"
    assert "🚫 не слать" in [t for t, _ in buttons(last_screen(s))]

    await press(s, "⬜ 💳 Оплаты и пополнения")
    assert s.env.service.state(K_PAYMENTS).enabled
    assert s.env.tg.topics(GROUP)[thread]["closed"] is False

    s.env.tg.delete_topic(GROUP, thread)
    s.env.service.state(K_PAYMENTS).thread_id = None  # as after a failed delivery
    await press(s, "🧱 Создать темы")
    assert "Темы готовы: 9 из 9" in last_screen(s).params["text"]
    assert s.env.service.state(K_PAYMENTS).thread_id not in (None, thread)


async def test_disconnect(s: Setup) -> None:
    await connected(s)
    await press(s, "🔌 Отключить")
    confirm = [c for c in _visible(s, OWNER) if c.method == "editMessageText"][-1]
    assert confirm.params["text"].startswith("Отключить админ-чат?")
    await click(
        s.env,
        OWNER,
        button(confirm, "🔌 Да, отключить"),
        chat_id=OWNER,
        message_id=confirm.params["message_id"],
    )
    assert s.settings.current()["ADMIN_CHAT_ID"] is None and s.env.service.chat_id is None
    assert "🔌 Админ-чат отключён" in last_screen(s).params["text"]


# ------------------------------------------------------------------ setup()


async def test_setup_wires_service_sink_component_and_screens(
    pg_dsn: str, tg: FakeTelegram, tmp_path: Path
) -> None:
    env = await build_env(pg_dsn, tg, chat=None)
    stops: list[str] = []
    components = ComponentRegistry()
    settings = SettingsService(
        env.db, core_registry(), Crypto([generate_key()]), components, environ={}, env_path=tmp_path / ".env"
    )
    await settings.load()
    bus = EventBus()
    router = ScreenRouter(
        transport=env.router.transport, user_loader=env.directory.load, ui_state=env.router.ui_state
    )

    async def owner_ids() -> frozenset[int]:
        return frozenset({OWNER})

    deps: Any = SimpleNamespace(
        db=env.db,
        notifier=env.notifier,
        holder=env.holder,
        settings=settings,
        components=components,
        hub=env.hub,
        users=env.directory,
        attention=AttentionService(env.db, bus=bus),
        bus=bus,
        on_stop=lambda name, fn: stops.append(name),
        owner_ids=owner_ids,
    )
    try:
        aiogram_router = await setup(router, deps)
        assert aiogram_router.name == "svbg-admin-chat"
        service = components.get("admin_chat")
        assert isinstance(service, AdminChatService) and service.chat_id is None
        assert isinstance(env.hub._sink, AdminChatSink)
        assert stops == ["admin chat", "admin chat buttons"]

        rejected = await settings.apply([Change("ADMIN_CHAT_ID", "-1009999")], source="bot", actor_id=None)
        assert not rejected.ok and "не администратор" in rejected.rejected["ADMIN_CHAT_ID"]
        applied = await settings.apply([Change("ADMIN_CHAT_ID", str(GROUP))], source="bot", actor_id=None)
        assert applied.ok and applied.reloaded == ["admin_chat"] and service.chat_id == GROUP
        async with asyncio.timeout(5):  # reconfigure creates the topics in the background
            while len(tg.topics(GROUP)) < 9:
                await asyncio.sleep(0.02)
        await service.stop(grace=1.0)
    finally:
        await env.close()
