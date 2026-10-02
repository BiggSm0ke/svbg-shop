"""«📣 Рассылки»: compose by message, buttons, audience, options, test, confirm, control, access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from aiogram.methods import CopyMessage, EditMessageText, SendMessage
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, PhotoSize

from svbg.broadcasts.sender import JOB_RUN
from svbg.core import clock
from svbg.tg.admin.broadcasts import ACTIONS, SCREEN_CARD, SCREEN_LIST, BroadcastScreens, build
from svbg.tg.ui.codec import CallbackCodec, encode
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.broadcasts.kit import DATE, FakeBot, make_notifier, mk_user
from tests.dbkit import CountingDatabase
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message, tg_user

OWNER = 1001
ADMIN = 2002
ADMIN_NOPERM = 3003
SUPPORT = 4004
USER = 5005
EMOJI_ID = "5368324170671202286"


@dataclass
class UEnv:
    db: CountingDatabase
    transport: FakeTransport
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    screens: BroadcastScreens
    bot: FakeBot
    denied: list[tuple[int, str]]

    async def add(self, tg_id: int, role: str = "user", perms: frozenset[str] = frozenset()) -> UserCtx:
        uid = await mk_user(self.db, tg_id, role=role)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.users.by_tg[tg_id] = ctx
        return ctx

    async def click(self, tg_id: int, data: str) -> None:
        state = await self.ui_state.get(self.users.by_tg[tg_id].user_id)
        await self.router.dispatch_callback(callback(tg_id, data, message_id=state.main_msg_id or 10))

    async def press(self, tg_id: int, label: str) -> None:
        await self.click(tg_id, self.button(label))

    async def send(self, tg_id: int, message: Message) -> bool:
        return await self.screens.handle_message(message)

    async def type(self, tg_id: int, text: str, entities: list[MessageEntity] | None = None) -> bool:
        message = text_message(tg_id, text, message_id=700)
        if entities:
            message = message.model_copy(update={"entities": entities})
        return await self.screens.handle_message(message)

    def rendered(self) -> list[SendMessage | EditMessageText]:
        return [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText)]

    @property
    def text(self) -> str:
        return self.rendered()[-1].text

    def labels(self) -> list[str]:
        markup = self.rendered()[-1].reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return [b.text for row in markup.inline_keyboard for b in row]

    def button(self, label: str) -> str:
        markup = self.rendered()[-1].reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        found: list[InlineKeyboardButton] = [
            b for row in markup.inline_keyboard for b in row if label in b.text
        ]
        assert found, f"no button {label!r} in {self.labels()}"
        assert found[0].callback_data is not None
        return found[0].callback_data

    @property
    def toasts(self) -> list[str | None]:
        return self.transport.toasts

    async def row(self, bid: int) -> dict[str, Any]:
        return dict((await self.db.raw("select * from broadcasts where id = $1", bid))[0])


@pytest.fixture
async def env(db: CountingDatabase) -> UEnv:
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    ui_state = UiStateStore(db)
    denied: list[tuple[int, str]] = []

    async def on_denied(user: UserCtx, place: str) -> None:
        denied.append((user.user_id, place))

    router = make_router(
        transport, users, ui_state, None, CallbackCodec(db, key=b"b" * 32), hub, on_denied=on_denied
    )
    bot = FakeBot()
    screens, _sender = build(router, db, make_notifier(bot, private_limit=100))
    e = UEnv(db, transport, users, router, ui_state, screens, bot, denied)
    await e.add(OWNER, "owner")
    await e.add(ADMIN, "admin", frozenset({"broadcast", "broadcast.send"}))
    await e.add(ADMIN_NOPERM, "admin", frozenset({"plans"}))
    await e.add(SUPPORT, "support", frozenset({"*"}))
    await e.add(USER)
    return e


def photo_message(tg_id: int, message_id: int = 800) -> Message:
    caption = "⭐ Новинка ||спойлер||"
    return Message(
        message_id=message_id,
        date=DATE,
        chat=Chat(id=tg_id, type="private"),
        from_user=tg_user(tg_id),
        photo=[PhotoSize(file_id="PHOTO", file_unique_id="p", width=9, height=9)],
        caption=caption,
        caption_entities=[
            MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=EMOJI_ID),
            MessageEntity(type="spoiler", offset=10, length=11),
        ],
    )


async def test_compose_configure_test_and_run(env: UEnv) -> None:
    assert await env.screens.handle_command(text_message(ADMIN, "/broadcast"))
    assert "Рассылки" in env.text and "Рассылок пока не было" in env.text
    await env.press(ADMIN, "Новая рассылка")
    assert "Пришлите или перешлите" in env.text
    assert await env.send(ADMIN, photo_message(ADMIN))
    bid = int((await env.db.raw("select id from broadcasts"))[0]["id"])
    row = await env.row(bid)
    assert (row["source_chat_id"], row["source_msg_id"], row["status"]) == (ADMIN, 800, "draft")
    assert row["content"]["type"] == "photo" and row["content"]["file_id"] == "PHOTO"
    assert [e["type"] for e in row["content"]["entities"]] == ["custom_emoji", "spoiler"]
    assert f"Рассылка #{bid}" in env.text and "🖼 фото" in env.text and "премиум-эмодзи" in env.text
    assert "≈ 5" in env.text  # every staff member and the user are recipients by default
    assert (await env.ui_state.get(env.users.by_tg[ADMIN].user_id)).awaiting is None

    # buttons: a mistake keeps the prompt, then a valid list
    await env.press(ADMIN, "Кнопки")
    assert "Кнопки рассылки" in env.text
    assert await env.type(ADMIN, "без разделителя")
    assert "⚠️" in env.text and "Текст | действие" in env.text
    assert await env.type(ADMIN, "Купить | screen:buy | зелёная ;; Канал | t.me/svbg")
    assert "Кнопки: 2" in env.text

    # audience: preset, then a DSL condition (an unsupported atom is refused)
    await env.press(ADMIN, "Получатели")
    assert "Получатели рассылки" in env.text and "✅ Все" in env.labels()
    await env.press(ADMIN, "Никогда не платили")
    assert env.toasts[-1] == "Получателей: 5" and "Никогда не платили" in env.text
    await env.press(ADMIN, "Получатели")
    await env.press(ADMIN, "Своё условие")
    assert await env.type(ADMIN, '{"is_new": true}')
    assert "Условие не подходит" in env.text
    assert await env.type(ADMIN, "не json")
    assert "Это не JSON" in env.text
    assert await env.type(ADMIN, '{"role": {"gte": "admin"}}')
    assert "Никогда не платили + своё условие" in env.text and "≈ 3" in env.text

    # options
    await env.press(ADMIN, "📌")
    assert "закрепить: да" in env.text
    await env.press(ADMIN, "🔕")
    await env.press(ADMIN, "🗑 ✗")
    assert "удалить: через 1 ч" in env.text and "без звука: да" in env.text

    # test to self: the exact copy with the buttons, no pin
    await env.press(ADMIN, "Тест себе")
    assert env.toasts[-1] == "Отправил вам точную копию ✓"
    copy = env.bot.of(CopyMessage)[-1]
    assert (copy.chat_id, copy.from_chat_id, copy.message_id) == (ADMIN, ADMIN, 800)
    assert copy.reply_markup is not None and len(copy.reply_markup.inline_keyboard[0]) == 2

    # confirm with the count, start
    await env.press(ADMIN, "Отправить")
    assert "Отправить рассылку" in env.text and "Получателей: <b>3</b>" in env.text
    await env.press(ADMIN, "Да, отправить 3")
    assert env.toasts[-1] == "🚀 Рассылка запущена"
    row = await env.row(bid)
    assert row["status"] == "running" and row["total"] == 3 and row["progress_msg"]["chat_id"] == ADMIN
    jobs = await env.db.raw("select kind from jobs")
    assert [j["kind"] for j in jobs] == [JOB_RUN]
    assert "▶️ идёт" in env.text and "Пауза" in " ".join(env.labels())
    progress = [c for c in env.bot.of(SendMessage) if "Рассылка" in c.text]
    assert progress and progress[-1].chat_id == ADMIN

    # control from the card
    await env.press(ADMIN, "Пауза")
    assert (await env.row(bid))["status"] == "paused" and "Продолжить" in " ".join(env.labels())
    await env.press(ADMIN, "Продолжить")
    assert (await env.row(bid))["status"] == "running"
    await env.press(ADMIN, "Остановить")
    assert (await env.row(bid))["status"] == "canceled" and "⏹ остановлена" in env.text
    actions = [a["action"] for a in await env.db.raw("select action from admin_audit order by id")]
    assert actions == ["broadcast.start", "broadcast.pause", "broadcast.resume", "broadcast.stop"]

    # control from the progress message answers with a toast only
    await env.click(ADMIN, encode(ACTIONS, "pause", str(bid)))
    assert env.toasts[-1] == "Статус рассылки уже изменился"

    # clone into a new draft
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(bid)))
    await env.press(ADMIN, "Копия в черновик")
    new = (await env.db.raw("select id, status, content, buttons from broadcasts where id <> $1", bid))[0]
    assert new["status"] == "draft" and new["content"]["file_id"] == "PHOTO" and len(new["buttons"]) == 2
    assert f"Рассылка #{new['id']}" in env.text


async def test_replace_delete_and_cancel(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая рассылка")
    assert await env.type(OWNER, "Первый текст")
    bid = int((await env.db.raw("select id from broadcasts"))[0]["id"])
    await env.press(OWNER, "Заменить сообщение")
    assert await env.send(OWNER, photo_message(OWNER, 901))
    row = await env.row(bid)
    assert row["source_msg_id"] == 901 and row["content"]["type"] == "photo"
    await env.press(OWNER, "Кнопки")
    assert await env.type(OWNER, "/cancel")
    assert f"Рассылка #{bid}" in env.text
    assert (await env.ui_state.get(env.users.by_tg[OWNER].user_id)).awaiting is None
    await env.press(OWNER, "Кнопки")
    assert not await env.type(OWNER, "/start")  # another command abandons the input and goes on
    assert (await env.ui_state.get(env.users.by_tg[OWNER].user_id)).awaiting is None
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(bid)))
    await env.press(OWNER, "Удалить черновик")
    assert env.toasts[-1] == "Черновик удалён" and not await env.db.raw("select id from broadcasts")


async def test_access_is_checked_everywhere(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая рассылка")
    assert await env.type(OWNER, "Текст")
    bid = str((await env.db.raw("select id from broadcasts"))[0]["id"])
    calls = len(env.rendered())
    for who in (ADMIN_NOPERM, SUPPORT, USER):
        for data in (
            encode(SCREEN_LIST),
            encode(SCREEN_CARD, arg=bid),
            encode(ACTIONS, "go", bid),
            encode(ACTIONS, "stop", bid),
            encode(ACTIONS, "opt", f"{bid}:pin"),
        ):
            await env.click(who, data)
            assert env.toasts[-1] == "Нет прав"
        assert not await env.screens.handle_command(text_message(who, "/broadcast"))
        # even a forged awaiting state does not let them compose
        await env.ui_state.set_awaiting(
            env.users.by_tg[who].user_id,
            {
                "kind": "bc",
                "v": 1,
                "step": "compose",
                "bid": None,
                "exp": (clock.now() + timedelta(minutes=5)).isoformat(),
            },
        )
        assert not await env.type(who, "чужая рассылка")
    assert len(env.rendered()) == calls
    assert len(env.denied) == 15
    assert len(await env.db.raw("select id from broadcasts")) == 1
    assert not await env.screens.handle_command(text_message(OWNER, "/broadcast", chat_type="group"))


async def test_forged_and_stale_input(env: UEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "opt", "999:pin"))
    assert env.toasts[-1] == "Рассылка не найдена"
    await env.click(OWNER, encode(ACTIONS, "opt", "abc"))
    assert env.toasts[-1] == "Рассылка не найдена"
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая рассылка")
    assert await env.type(OWNER, "Текст")
    bid = str((await env.db.raw("select id from broadcasts"))[0]["id"])
    await env.click(OWNER, encode(ACTIONS, "opt", f"{bid}:bogus"))
    assert env.toasts[-1] == "Рассылка не найдена"
    await env.click(OWNER, encode(ACTIONS, "seg", f"{bid}:nope"))
    assert env.toasts[-1] == "Рассылка не найдена"
    await env.db.raw("update broadcasts set status = 'done'")
    await env.click(OWNER, encode(ACTIONS, "btn", bid))
    assert env.toasts[-1] == "Рассылка уже запущена — менять её нельзя"
    await env.click(OWNER, encode(ACTIONS, "go", bid))
    assert env.toasts[-1] == "Рассылка уже запущена или удалена"
    # an expired input state is dropped, the message goes on to other handlers
    uid = env.users.by_tg[OWNER].user_id
    past = (clock.now() - timedelta(seconds=1)).isoformat()
    await env.ui_state.set_awaiting(uid, {"kind": "bc", "v": 1, "step": "compose", "bid": None, "exp": past})
    assert not await env.type(OWNER, "поздно")
    assert (await env.ui_state.get(uid)).awaiting is None
    # someone else's form state is not ours
    await env.ui_state.set_awaiting(uid, {"kind": "form", "v": 1, "form": "x", "step": 0, "data": {}})
    assert not await env.type(OWNER, "текст формы")


async def test_unsupported_message_is_explained(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая рассылка")
    sticker_less = Message(
        message_id=5, date=DATE, chat=Chat(id=OWNER, type="private"), from_user=tg_user(OWNER)
    )
    assert await env.send(OWNER, sticker_less)
    assert "нельзя разослать" in env.text and not await env.db.raw("select id from broadcasts")


async def test_sql_per_click(env: UEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая рассылка")
    assert await env.type(OWNER, "Текст")
    bid = str((await env.db.raw("select id from broadcasts"))[0]["id"])
    await env.click(OWNER, encode(SCREEN_CARD, arg=bid))  # warm the ui_state cache
    budget = {
        encode(SCREEN_CARD, arg=bid): 1,
        encode(ACTIONS, "opt", f"{bid}:pin"): 2,
        encode(ACTIONS, "seg", f"{bid}:all"): 2,
        encode(ACTIONS, "nobtn", bid): 1,
        "v1:bc.ok:o:" + bid: 2,
        encode(SCREEN_LIST): 1,
    }
    for data, limit in budget.items():
        mark = env.db.queries
        await env.click(OWNER, data)
        assert env.db.queries - mark <= limit, (data, env.db.counter.since(mark) if env.db.counter else None)


async def test_setup_registers_screens_router_and_jobs(db: CountingDatabase) -> None:
    from types import SimpleNamespace

    from aiogram import Router

    from svbg.broadcasts.sender import JOB_CLEANUP
    from svbg.tg.admin.broadcasts import setup

    router = make_router(FakeTransport(), Users(), UiStateStore(db), None, None, FakeHub())
    registered: dict[str, Any] = {}
    deps = SimpleNamespace(
        db=db,
        notifier=make_notifier(FakeBot()),
        settings=SimpleNamespace(current=lambda: {"DEFAULT_LANGUAGE": "en", "REQUIRED_CHANNEL_ID": -100}),
        register_job=registered.__setitem__,
    )
    result = setup(router, deps)
    assert isinstance(result, Router)
    assert set(registered) == {JOB_RUN, JOB_CLEANUP}
    # without register_job the module still wires its screens (the jobs are reported as missing)
    other = make_router(FakeTransport(), Users(), UiStateStore(db), None, None, FakeHub())
    assert isinstance(setup(other, SimpleNamespace(db=db, notifier=deps.notifier)), Router)


async def test_draft_only_admin_cannot_send_or_resume(env: UEnv) -> None:
    """Decision C4: ``broadcast`` = draft, preview, «Тест себе»; ``broadcast.send`` = start/resume."""
    drafter = 6006
    await env.add(drafter, "admin", frozenset({"broadcast"}))
    await env.click(drafter, encode(SCREEN_LIST))
    await env.press(drafter, "Новая рассылка")
    assert await env.type(drafter, "Текст")
    bid = str((await env.db.raw("select id from broadcasts"))[0]["id"])
    for action in ("go", "resume"):
        await env.click(drafter, encode(ACTIONS, action, bid))
        assert env.toasts[-1] == "Нет прав", action
    assert (await env.row(int(bid)))["status"] == "draft"
    await env.click(ADMIN, encode(ACTIONS, "go", bid))
    assert (await env.row(int(bid)))["status"] == "running"
