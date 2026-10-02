"""«🔗 Ссылки»: access, the builder wizard, the link card (copy, QR, on/off), list paging, budgets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.methods import EditMessageMedia, EditMessageText, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from svbg.content.store import ContentStore
from svbg.core import clock
from svbg.deeplinks.model import Intent
from svbg.deeplinks.service import DeeplinkService
from svbg.tg.admin.deeplinks import (
    ACTIONS,
    LINKS_BUTTON,
    SCREEN_CARD,
    SCREEN_DRAFT,
    SCREEN_LIST,
    LinkBuilder,
    setup,
)
from svbg.tg.ui.codec import CallbackCodec, encode
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from svbg.tg.ui.view import View
from tests.dbkit import CountingDatabase, add_user
from tests.deeplinks.kit import FakeAds, FakeCatalog, FakePromo, FakeReferral
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message

OWNER, ADMIN, ADMIN_NOPERM, SUPPORT, USER = 1001, 2002, 3003, 4004, 5005
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


@dataclass
class AEnv:
    db: CountingDatabase
    transport: FakeTransport
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    service: DeeplinkService
    builder: LinkBuilder
    promo: FakePromo
    denied: list[tuple[int, str]]

    async def add(self, tg_id: int, role: str = "user", perms: frozenset[str] = frozenset()) -> UserCtx:
        uid = await add_user(self.db, tg_id, role)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.users.by_tg[tg_id] = ctx
        return ctx

    async def click(self, tg_id: int, data: str) -> None:
        state = await self.ui_state.get(self.users.by_tg[tg_id].user_id)
        await self.router.dispatch_callback(callback(tg_id, data, message_id=state.main_msg_id or 10))

    async def press(self, tg_id: int, label: str) -> None:
        await self.click(tg_id, self.button(label))

    async def type(self, tg_id: int, text: str) -> bool:
        return await self.router.dispatch_message(text_message(tg_id, text, message_id=700))

    def rendered(self) -> list[Any]:
        return [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText | SendPhoto)]

    @property
    def text(self) -> str:
        last = self.rendered()[-1]
        return last.caption if isinstance(last, SendPhoto) else last.text

    def buttons(self) -> list[InlineKeyboardButton]:
        markup = self.rendered()[-1].reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return [b for row in markup.inline_keyboard for b in row]

    def labels(self) -> list[str]:
        return [b.text for b in self.buttons()]

    def button(self, label: str) -> str:
        for b in self.buttons():
            if label in b.text and b.callback_data is not None:
                return b.callback_data
        raise AssertionError(f"no button {label!r} in {self.labels()}")

    @property
    def toasts(self) -> list[str | None]:
        return self.transport.toasts


@pytest.fixture
async def env(db: CountingDatabase) -> AEnv:
    clock.set_clock(NOW)
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    denied: list[tuple[int, str]] = []

    async def on_denied(user: UserCtx, place: str) -> None:
        denied.append((user.user_id, place))

    router = make_router(
        transport, users, ui_state, content, CallbackCodec(db, key=b"d" * 32), hub, on_denied=on_denied
    )

    async def user_screen(_ctx: Any, _arg: Any) -> View:
        return View(text="user screen")

    router.screen("buy")(user_screen)
    router.screen("bal")(user_screen)
    router.screen("secret", required_role="admin")(user_screen)
    promo = FakePromo()
    deps = _Deps(db, router, FakeCatalog(), promo)
    service = deps.deeplinks
    builder = LinkBuilder(router, service, catalog=deps.catalog)
    builder.install()
    builder.install()  # idempotent
    e = AEnv(db, transport, users, router, ui_state, service, builder, promo, denied)
    await e.add(OWNER, "owner")
    await e.add(ADMIN, "admin", frozenset({"deeplinks"}))
    await e.add(ADMIN_NOPERM, "admin", frozenset({"plans"}))
    await e.add(SUPPORT, "support")
    await e.add(USER, "user")
    return e


class _Deps:
    def __init__(
        self, db: CountingDatabase, router: ScreenRouter, catalog: FakeCatalog, promo: FakePromo
    ) -> None:
        self.db = db
        self.screens = router
        self.catalog = catalog
        self.hub = FakeHub()
        self.settings = None
        self.deeplinks = DeeplinkService(
            db,
            ui_state=router.ui_state,
            catalog=catalog,
            promo=promo,
            ads=FakeAds(),
            referral=FakeReferral(),
            config=lambda: {"CURRENCY": "RUB", "TIMEZONE": "Europe/Moscow"},
        )


# ------------------------------------------------------------------------------------------- access


@pytest.mark.parametrize("who", [ADMIN_NOPERM, SUPPORT, USER], ids=["admin-without-perm", "support", "user"])
async def test_denied_without_the_permission(env: AEnv, who: int) -> None:
    link = await env.service.create_link(Intent(screen="buy"), title="x", actor=None)
    for data in (
        encode(SCREEN_LIST),
        encode(SCREEN_CARD, arg=str(link.id)),
        encode(SCREEN_DRAFT),
        encode(ACTIONS, "new"),
        encode(ACTIONS, "off", str(link.id)),
        encode(ACTIONS, "create"),
    ):
        await env.click(who, data)
        assert env.toasts[-1] == "Нет прав"
    assert not env.rendered()
    assert len(env.denied) == 6
    row = await env.service.get_link(link.id)
    assert row is not None and row.enabled


@pytest.mark.parametrize("who", [OWNER, ADMIN], ids=["owner", "admin-with-perm"])
async def test_allowed_roles(env: AEnv, who: int) -> None:
    await env.click(who, encode(SCREEN_LIST))
    assert "Ссылки" in env.text and "Ссылок пока нет" in env.text


async def test_links_command(db: CountingDatabase, env: AEnv) -> None:
    builder = env.builder
    assert await builder.handle_command(text_message(OWNER, "/links"))
    assert "Ссылки" in env.text
    assert not await builder.handle_command(text_message(ADMIN_NOPERM, "/links"))
    assert not await builder.handle_command(text_message(USER, "/links"))
    assert not await builder.handle_command(text_message(OWNER, "/links", chat_type="group"))


async def test_setup_wires_the_module(db: CountingDatabase) -> None:
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    router = make_router(transport, users, UiStateStore(db), None, CallbackCodec(db, key=b"s" * 32), hub)
    deps = _Deps(db, router, FakeCatalog(), FakePromo())
    assert await setup(router, deps) is not None
    assert (ACTIONS, "create") in router._actions
    other = make_router(transport, users, UiStateStore(db), None, CallbackCodec(db, key=b"s" * 32), hub)
    deps.deeplinks = None  # type: ignore[assignment]  # the app did not provide the service: built here
    assert await setup(other, deps) is not None


def test_home_button_seed() -> None:
    assert LINKS_BUTTON.action == {"type": "screen", "target": SCREEN_LIST}
    assert LINKS_BUTTON.visible_if == {"role": {"gte": "admin"}}


# ------------------------------------------------------------------------------------------- wizard


async def test_build_a_plan_link_with_promo(env: AEnv) -> None:
    await env.click(OWNER, encode(SCREEN_LIST))
    await env.press(OWNER, "Новая ссылка")
    assert "Куда ведёт ссылка" in env.text
    await env.press(OWNER, "Тариф")
    assert any("STD" in label for label in env.labels())
    assert any("VIP 🔒" in label for label in env.labels())  # link-only plans are marked
    assert not any("OLD" in label for label in env.labels())  # hidden plans are not offered
    await env.press(OWNER, "STD")
    assert "Цель: тариф «STD»" in env.text
    assert "https://t.me/svbg_bot?start=p_std" in env.text  # the direct equivalent
    copy = [b for b in env.buttons() if b.copy_text is not None]
    assert copy and copy[0].copy_text.text == "https://t.me/svbg_bot?start=p_std"

    await env.press(OWNER, "Промокод")
    assert await env.type(OWNER, "НЕТ")  # Cyrillic: not allowed in a link
    assert "латиница" in env.text
    assert await env.type(OWNER, "AUTUMN")
    assert "Промокод: AUTUMN" in env.text and "Прямая ссылка" not in env.text
    await env.press(OWNER, "Лимит")
    assert await env.type(OWNER, "5")
    await env.press(OWNER, "Срок")
    await env.press(OWNER, "7 дн.")
    await env.press(OWNER, "Название")
    assert await env.type(OWNER, "Канал  · осень")
    assert "Название: Канал · осень" in env.text and "Лимит: 5 человек" in env.text
    await env.press(OWNER, "Создать ссылку")

    links, total = await env.service.list_links()
    assert total == 1
    link = links[0]
    assert (link.title, link.max_uses, link.intent) == (
        "Канал · осень",
        5,
        Intent(plan="std", promo="AUTUMN"),
    )
    assert link.expires_at is not None and (link.expires_at - NOW).days == 7
    url = f"https://t.me/svbg_bot?start=l_{link.code}"
    assert url in env.text and "Статус: 🟢 работает" in env.text
    assert env.toasts[-1] == "✅ Ссылка создана"
    copy = [b for b in env.buttons() if b.copy_text is not None]
    assert copy[0].copy_text.text == url
    audit = await env.db.raw("select action, actor_id from admin_audit")
    assert [r["action"] for r in audit] == ["deeplink.create"]
    # the draft is gone: «Создать» again is stale, nothing is created twice
    await env.click(OWNER, encode(ACTIONS, "create"))
    assert env.toasts[-1] == "Черновик устарел — начните заново"
    assert (await env.service.list_links())[1] == 1


async def test_double_tap_on_create_opens_the_same_link(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    await env.press(OWNER, "Экран")
    labels = env.labels()
    assert "🛒 Покупка" in labels and "💰 Баланс" in labels
    assert not any("secret" in label for label in labels)  # admin-only screens are not linkable
    await env.press(OWNER, "Покупка")
    draft_data = env.button("Создать ссылку")
    await env.click(OWNER, draft_data)
    # replay the same draft (two taps in flight): the code is taken → the existing link is shown
    builder_drafts = env.builder.drafts
    first = (await env.service.list_links())[0][0]
    builder_drafts._items[env.users.by_tg[OWNER].user_id] = (10**12, _draft_with_code(first.code, "buy"))
    await env.click(OWNER, draft_data)
    assert (await env.service.list_links())[1] == 1
    assert f"l_{first.code}" in env.text


def _draft_with_code(code: str, screen: str) -> Any:
    from svbg.tg.admin.deeplinks import Draft

    return Draft(code=code, screen=screen)


async def test_problems_and_topup(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    await env.press(OWNER, "Пополнение")
    assert "Сумма пополнения в RUB" in env.text
    assert await env.type(OWNER, "abc")
    assert "целое число" in env.text
    assert await env.type(OWNER, "500")
    assert "Цель: пополнение на 500" in env.text and "start=t_500" in env.text
    await env.press(OWNER, "Промокод")
    assert await env.type(OWNER, "NOPE")
    await env.press(OWNER, "Метка")
    assert await env.type(OWNER, "nowhere")
    assert "Промокода «NOPE» нет" in env.text and "Рекламной метки «nowhere» нет" in env.text
    await env.press(OWNER, "Метка")
    await env.press(OWNER, "Пропустить")
    assert "Метка: —" in env.text and "nowhere" not in env.text


async def test_promo_only_link(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    await env.press(OWNER, "Только промокод")
    assert await env.type(OWNER, "WEEK")
    assert "Цель: главное меню" in env.text and "start=pr_WEEK" in env.text
    await env.press(OWNER, "Создать ссылку")
    link = (await env.service.list_links())[0][0]
    assert link.intent == Intent(promo="WEEK")


async def test_empty_draft_cannot_be_created(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    await env.press(OWNER, "Главное меню")
    assert "Создать ссылку" not in " ".join(env.labels())
    assert "Выберите цель" in env.text
    await env.click(OWNER, encode(ACTIONS, "create"))
    assert env.toasts[-1] == "Выберите цель или добавьте промокод"


async def test_forged_arguments_are_rejected(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    await env.click(OWNER, encode(ACTIONS, "scr", "secret"))
    assert "Какой экран открыть" in env.text
    await env.click(OWNER, encode(ACTIONS, "pl", "old"))
    assert "Какой тариф открыть" in env.text
    await env.click(OWNER, encode(ACTIONS, "exp", "13"))
    assert "Сколько действует" in env.text
    await env.click(OWNER, encode(SCREEN_CARD, arg="abc"))
    assert "Ссылки" in env.text
    await env.click(OWNER, encode(SCREEN_CARD, arg="999999"))
    assert "Ссылок пока нет" in env.text  # screens answer at once: the list is shown without a toast
    await env.click(OWNER, encode(ACTIONS, "off", "999999"))
    assert env.toasts[-1] == "Ссылки нет"


async def test_stale_draft_after_restart(env: AEnv) -> None:
    await env.click(OWNER, encode(ACTIONS, "new"))
    env.builder.drafts.drop(env.users.by_tg[OWNER].user_id)
    await env.click(OWNER, encode(SCREEN_DRAFT))
    assert "Ссылки" in env.text
    await env.click(OWNER, encode(ACTIONS, "promo"))
    assert env.toasts[-1] == "Черновик устарел — начните заново"


# ------------------------------------------------------------------------------------------- card


async def test_card_toggle_qr_and_stats(env: AEnv) -> None:
    link = await env.service.create_link(Intent(plan="std"), title="Сторис", actor=None, max_uses=3)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(link.id)))
    assert "Сторис" in env.text and "Пришло людей: 0 из 3" in env.text and "Цель: тариф «STD»" in env.text
    assert "Переходы: 0 / 0 / 0" in env.text
    await env.press(OWNER, "Выключить")
    assert "⏸ выключена" in env.text and env.toasts[-1] == "⏸ Ссылка выключена"
    await env.press(OWNER, "Включить")
    assert "🟢 работает" in env.text
    rows = await env.db.raw("select action from admin_audit order by id")
    assert [r["action"] for r in rows] == ["deeplink.disable", "deeplink.enable"]
    mark = len(env.transport.calls)
    await env.press(OWNER, "QR")
    # the main message carries the banner photo: the QR replaces that photo in place
    (photo,) = [c for c in env.transport.calls[mark:] if isinstance(c, SendPhoto | EditMessageMedia)]
    caption = photo.caption if isinstance(photo, SendPhoto) else photo.media.caption
    assert f"l_{link.code}" in (caption or "")


async def test_card_click_budget(env: AEnv) -> None:
    link = await env.service.create_link(Intent(plan="std"), title="x", actor=None)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(link.id)))  # warm caches, the main message
    before = env.db.queries
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(link.id)))
    assert env.db.queries - before <= 2


async def test_list_pages(env: AEnv) -> None:
    for i in range(12):
        await env.service.create_link(Intent(topup=100 + i), title=f"L{i}", actor=None)
    await env.click(OWNER, encode(SCREEN_LIST))
    assert "Стр. 1 из 2" in env.text and any("L11" in x for x in env.labels())
    await env.press(OWNER, "▶️")
    assert "Стр. 2 из 2" in env.text and any("L0" in x for x in env.labels())
    await env.press(OWNER, "◀️")
    assert "Стр. 1 из 2" in env.text
