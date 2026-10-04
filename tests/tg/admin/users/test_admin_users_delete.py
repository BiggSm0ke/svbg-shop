"""«🗑 Удалить полностью» in the user card: who sees it, the confirmation with the panel toggle, the panel
failure path and forged buttons."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from svbg.core.bus import Event, EventBus
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.services.user_delete import UserDeleter
from svbg.tg.admin import users as users_module
from svbg.tg.admin.users.screens import ACTIONS, SCREEN_CARD, SCREEN_DELETE
from svbg.tg.ui.codec import CallbackCodec, encode
from svbg.tg.ui.router import UiStateStore
from tests.dbkit import CountingDatabase, add_user
from tests.tg.admin.users.kit import ADMIN, ADMIN_STATS, CONF_OWNER, OWNER, SUPPORT, USER, UEnv, add_sub
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, make_router

DELETE = "🗑 Удалить полностью"


class FakeApi:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.deleted: list[int] = []

    async def delete_user(self, id: int, *, lane: Any = None) -> bool:
        if self.fail is not None:
            raise self.fail
        self.deleted.append(id)
        return True


class FakeChat:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def post(self, kind: str, text: str, *, html: bool = False, **_kw: Any) -> None:
        self.posts.append((kind, text))


def attach(env: UEnv, api: FakeApi | None = None) -> tuple[FakeChat, list[Any]]:
    chat, forgotten = FakeChat(), []
    env.screens.deleter = UserDeleter(
        env.db,
        owner_ids=env.directory.configured_async,
        panel=(lambda: api) if api is not None else None,
        forget=lambda uid, tg: forgotten.append((uid, tg)),
        admin_chat=chat,
    )
    return chat, forgotten


async def exists(env: UEnv, uid: int) -> bool:
    return bool((await env.db.raw("select count(*) from users where id = $1", uid))[0][0])


async def test_who_sees_the_button(env: UEnv) -> None:
    uid = env.ids[USER]
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(uid)))
    assert DELETE not in env.labels()  # no deleter wired: no button
    attach(env)
    for viewer, shown in ((OWNER, True), (ADMIN, True), (ADMIN_STATS, False), (SUPPORT, False)):
        await env.click(viewer, encode(SCREEN_CARD, arg=str(uid)))
        assert (DELETE in env.labels()) is shown, viewer
    for target in (env.ids[ADMIN], env.ids[SUPPORT], env.ids[OWNER]):  # staff, own card
        await env.click(OWNER, encode(SCREEN_CARD, arg=str(target)))
        assert DELETE not in env.labels()
    conf_owner = await env.add(CONF_OWNER)  # owner by OWNER_IDS, stored as a plain user
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(conf_owner)))
    assert DELETE not in env.labels()


async def test_confirm_toggle_and_delete_only_in_the_bot(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid)
    api = FakeApi()
    chat, forgotten = attach(env, api)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(uid)))
    await env.press(OWNER, DELETE)
    text = env.text
    assert "Удалить полностью?" in text and "Иван @ivan_petrov" in text and "<code>5005</code>" in text
    assert "подписки: 1 (в панели: 1)" in text and "оплаты: нет, заказы: нет" in text
    assert "В панели Remnawave его тоже удалим." in text and "капча, пробный период" in text
    assert len(text) < 900
    assert env.labels() == ["✅ Удалить и в панели", "Да, удалить навсегда", "Отмена"]
    await env.press(OWNER, "Удалить и в панели")
    assert "В панели пользователь останется." in env.text and "⬜️ Удалить и в панели" in env.labels()
    await env.press(OWNER, "Да, удалить навсегда")
    assert "Пользователь удалён" in env.text and "В панели остался." in env.text
    assert api.deleted == [] and not await exists(env, uid)
    assert forgotten == [(uid, USER)] and len(chat.posts) == 1
    audit = await env.db.raw("select * from admin_audit where action = 'user.delete'")
    assert audit[0]["target"] == f"user:{uid}" and audit[0]["details"]["panel"] == "kept"
    # the card of a deleted user is gone; a second press of an old button is harmless
    await env.click(OWNER, encode(ACTIONS, "del", f"{uid}:1"))
    assert "Пользователь не найден" in env.text


async def test_cancel_returns_to_the_card(env: UEnv) -> None:
    uid = env.ids[USER]
    attach(env)
    await env.click(ADMIN, encode(SCREEN_DELETE, arg=f"{uid}:1"))
    assert "Удалить и в панели" not in " ".join(env.labels())  # no panel user: no toggle
    await env.press(ADMIN, "Отмена")
    assert "Telegram ID: <code>5005</code>" in env.text and await exists(env, uid)


async def test_panel_failure_then_delete_only_in_the_bot(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid)
    api = FakeApi(RemnawaveError(ErrorKind.TRANSIENT, 503))
    attach(env, api)
    await env.click(ADMIN, encode(SCREEN_DELETE, arg=f"{uid}:1"))
    await env.press(ADMIN, "Да, удалить навсегда")
    assert "Панель не дала удалить пользователя: панель не отвечает" in env.text
    assert "В боте пока ничего не удалено." in env.text and await exists(env, uid)
    assert env.labels() == ["Удалить только в боте", "🔄 Попробовать снова", "⬅️ К карточке"]
    await env.press(ADMIN, "Попробовать снова")
    assert "Панель не дала" in env.text and await exists(env, uid)
    await env.press(ADMIN, "Удалить только в боте")
    assert "Пользователь удалён" in env.text and not await exists(env, uid)


async def test_panel_user_is_deleted_first(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid, panel_user_id=4242)
    api = FakeApi()
    attach(env, api)
    await env.click(OWNER, encode(SCREEN_DELETE, arg=f"{uid}:1"))
    await env.press(OWNER, "Да, удалить навсегда")
    assert api.deleted == [4242] and "В панели тоже удалён." in env.text


async def test_forged_buttons_are_refused(env: UEnv) -> None:
    uid = env.ids[USER]
    attach(env)
    await env.click(SUPPORT, encode(ACTIONS, "del", f"{uid}:0"))
    assert env.toasts[-1] == "Нет прав"
    await env.click(ADMIN_STATS, encode(SCREEN_DELETE, arg=f"{uid}:0"))
    assert env.toasts[-1] == "Нет прав"
    assert await exists(env, uid)
    # an admin who lost the right after the screen was opened: the service re-reads it
    await env.db.raw("update users set perms = '[\"stats\"]'::jsonb where id = $1", env.ids[ADMIN])
    await env.click(ADMIN, encode(ACTIONS, "del", f"{uid}:0"))
    assert "Нет прав" in env.text and await exists(env, uid)
    # staff cannot be deleted even with a forged button
    await env.click(OWNER, encode(SCREEN_DELETE, arg=f"{env.ids[SUPPORT]}:0"))
    assert "удалить нельзя" in env.text
    await env.click(OWNER, encode(ACTIONS, "del", f"{env.ids[SUPPORT]}:0"))
    assert "Это сотрудник" in env.text and await exists(env, env.ids[SUPPORT])


class Directory:
    def __init__(self) -> None:
        self.invalidated: list[int | None] = []

    def configured_owner_ids(self) -> frozenset[int]:
        return frozenset({1})

    def invalidate(self, telegram_id: int | None = None) -> None:
        self.invalidated.append(telegram_id)


class Captcha:
    def __init__(self) -> None:
        self.forgotten: list[int] = []

    async def gate(self, _ctx: Any, _decoded: Any) -> None:
        return None

    def forget(self, user_id: int) -> None:
        self.forgotten.append(user_id)


async def test_setup_wires_the_deleter_and_drops_cached_state(db: CountingDatabase) -> None:
    router = make_router(
        FakeTransport(), Users(), UiStateStore(db), None, CallbackCodec(db, key=b"d" * 32), FakeHub()
    )
    captcha = Captcha()
    router.gate = captcha.gate
    bus, chat, directory = EventBus(), FakeChat(), Directory()
    events: list[Event] = []

    async def seen(event: Event) -> None:
        events.append(event)

    bus.subscribe("user.deleted", seen)
    deps = SimpleNamespace(
        db=db, settings=None, users=directory, notifier=None, catalog=None, bus=bus, admin_chat=chat
    )
    screens = users_module.build(router, deps)
    assert screens.deleter is not None
    uid = await add_user(db, 5005)
    await router.ui_state.get(uid)  # cached screen state
    result = await screens.deleter.delete(1, uid)  # an owner by OWNER_IDS; no panel needed (no subscription)
    await bus.drain()
    assert result.ok and directory.invalidated == [5005] and captcha.forgotten == [uid]
    assert uid not in router.ui_state._cache
    assert [e.payload["telegram_id"] for e in events] == [5005]
    assert chat.posts and chat.posts[0][0] == "new_users"
