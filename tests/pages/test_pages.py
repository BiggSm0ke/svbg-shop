"""Pages: storage with versions and entities, consent, the owner's editor (message capture) and user
screens."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, MessageEntity, PhotoSize
from aiogram.types import Message as TgMessage

from svbg.pages.service import PageError, PageService
from svbg.pages.user import SCREEN as PAGE
from svbg.pages.user import PageUserScreens
from svbg.tg.admin.pages import ACTIONS, SCREEN_CARD, SCREEN_LIST, SCREEN_WAIT, PageAdminScreens
from svbg.tg.ui.codec import encode
from tests.dbkit import CountingDatabase
from tests.promo.ui_kit import ADMIN, ADMIN_NOPERM, OWNER, SUPPORT, USER, UiEnv, build_ui
from tests.tg.ui.ui_harness import text_message, tg_user

ACTOR = (1, "owner")
TEXT = "Тест 🔥 секрет"
ENTITIES = [
    MessageEntity(type="bold", offset=0, length=4),
    MessageEntity(type="custom_emoji", offset=5, length=2, custom_emoji_id="5368324170671202286"),
    MessageEntity(type="spoiler", offset=8, length=6),
]


def formatted(tg_id: int, text: str | None = TEXT, *, photo: bool = False) -> TgMessage:
    extra: dict[str, object] = {}
    if photo:
        extra = {
            "photo": [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
            "caption": text,
            "caption_entities": ENTITIES,
        }
    else:
        extra = {"text": text, "entities": ENTITIES}
    return TgMessage(
        message_id=900,
        date=datetime(2026, 10, 2, tzinfo=UTC),
        chat=Chat(id=tg_id, type="private"),
        from_user=tg_user(tg_id),
        **extra,
    )


# ------------------------------------------------------------------------------------------- service


async def test_seed_is_idempotent_and_off(db: CountingDatabase, pages: PageService) -> None:
    assert [p.code for p in pages.all()] == ["faq", "rules", "offer", "consent"]
    assert all(not p.enabled and p.version == 1 for p in pages.all())
    again = PageService(db)
    assert await again.load() == 4
    assert len(await db.raw("select 1 from page_versions")) == 4


async def test_text_versions_and_cas(db: CountingDatabase, pages: PageService) -> None:
    page = await pages.save_text("faq", "ru", TEXT, ENTITIES, ACTOR)
    assert page.version == 2
    block = page.block_for("en")  # falls back to ru
    assert block is not None and block.text == TEXT
    assert [e["type"] for e in block.entities] == ["bold", "custom_emoji", "spoiler"]
    assert block.entities[1]["custom_emoji_id"] == "5368324170671202286"
    stored = (await db.raw("select body from pages where code = 'faq'"))[0]["body"]
    assert stored["ru"]["entities"][2] == {"type": "spoiler", "offset": 8, "length": 6}
    with pytest.raises(PageError, match="уже изменили"):
        await pages.save_text("faq", "ru", "старое", None, ACTOR, expected_version=1)
    with pytest.raises(PageError, match="не подходит"):
        await pages.save_text("faq", "ru", "abc", [{"type": "bold", "offset": 0, "length": 50}], ACTOR)
    en = await pages.save_text("faq", "en", "Hello", None, ACTOR, expected_version=2)
    # Russian-only bot: an English text may still be stored by an old editor, but nobody sees it
    assert en.version == 3 and en.block_for("en").text == TEXT  # type: ignore[union-attr]
    assert en.block_for("ru").text == TEXT  # type: ignore[union-attr]
    restored = await pages.restore("faq", 1, ACTOR)
    assert restored.version == 4 and restored.block_for("ru").text.startswith("Здесь будут")  # type: ignore[union-attr]
    with pytest.raises(PageError):
        await pages.restore("faq", 99, ACTOR)
    versions = await pages.versions("faq")
    assert [v.version for v in versions] == [4, 3, 2, 1] and versions[1].preview == TEXT
    titled = await pages.set_title("faq", "ru", " Помощь ", ACTOR)
    assert titled.title_for("ru") == "Помощь"
    with pytest.raises(PageError):
        await pages.set_title("faq", "ru", " ", ACTOR)
    actions = [r["action"] for r in await db.raw("select action from admin_audit order by id")]
    assert actions == ["page.edit", "page.edit", "page.restore", "page.title"]


async def test_consent_flow(db: CountingDatabase, pages: PageService) -> None:
    uid = int((await db.raw("insert into users (telegram_id) values (7) returning id"))[0]["id"])
    mark = db.queries
    assert await pages.needs_consent(uid) is None  # consent off: no SQL
    assert db.queries == mark
    page = await pages.set_enabled("consent", True, ACTOR)
    assert page.consent_version == 1
    assert (await pages.needs_consent(uid)) is not None
    assert not await pages.accept(uid, "consent", 2)
    assert not await pages.accept(uid, "faq", 1)
    assert await pages.accept(uid, "consent", 1)
    assert await pages.needs_consent(uid) is None
    await pages.save_text("consent", "ru", "Новые условия", None, ACTOR)
    assert await pages.needs_consent(uid) is None  # a text fix does not re-ask
    asked = await pages.request_consent("consent", ACTOR)
    assert asked.consent_version == 2 and (await pages.needs_consent(uid)) is not None
    with pytest.raises(PageError):
        await pages.request_consent("faq", ACTOR)
    await pages.set_enabled("consent", False, ACTOR)
    assert await pages.needs_consent(uid) is None


async def test_custom_pages(db: CountingDatabase, pages: PageService) -> None:
    page = await pages.create("Delivery", "Доставка", ACTOR)
    assert page.code == "delivery" and not page.system and not page.enabled
    for bad in ("1abc", "with space", "x" * 25, ""):
        with pytest.raises(PageError, match="Код страницы"):
            await pages.create(bad, "X", ACTOR)
    with pytest.raises(PageError, match="уже есть"):
        await pages.create("delivery", "Ещё", ACTOR)
    with pytest.raises(PageError, match="только выключить"):
        await pages.delete("faq", ACTOR)
    await pages.delete("delivery", ACTOR)
    assert pages.get("delivery") is None and not await db.raw("select 1 from pages where code = 'delivery'")
    with pytest.raises(PageError, match="не найдена"):
        await pages.save_text("delivery", "ru", "x", None, ACTOR)


# ------------------------------------------------------------------------------------------- screens


@pytest.fixture
async def env(db: CountingDatabase, pages: PageService) -> tuple[UiEnv, PageAdminScreens, PageUserScreens]:
    ui = await build_ui(db)
    user_screens = PageUserScreens(pages)
    user_screens.register(ui.router)
    admin = PageAdminScreens(ui.router, pages, user_screens=user_screens)
    admin.install()
    await ui.staff("settings.business")
    return ui, admin, user_screens


@pytest.mark.parametrize("who", [ADMIN_NOPERM, SUPPORT, USER], ids=["admin-without-perm", "support", "user"])
async def test_editor_denied(env: tuple[UiEnv, PageAdminScreens, PageUserScreens], who: int) -> None:
    ui, admin, _ = env
    for data in (
        encode(SCREEN_LIST),
        encode(SCREEN_CARD, arg="faq"),
        encode(SCREEN_WAIT, arg="faq:ru"),
        encode(ACTIONS, "en", "faq"),
    ):
        await ui.click(who, data)
        assert ui.toasts[-1] == "Нет прав"
    assert not admin.capturing(formatted(who))
    assert not await admin.handle_command(text_message(who, "/pages"))


async def test_write_a_page_with_formatting(
    env: tuple[UiEnv, PageAdminScreens, PageUserScreens], pages: PageService
) -> None:
    ui, admin, _ = env
    assert await admin.handle_command(text_message(OWNER, "/pages"))
    assert "Страницы" in ui.text and "⏸ ❓ Вопросы и ответы · v1" in ui.labels()
    await ui.press(OWNER, "Вопросы и ответы")
    assert "screen:page.faq" in ui.text and "⏸ выключена" in ui.text
    await ui.press(OWNER, "✏️ Текст")
    assert "Пришлите новый текст" in ui.text and admin.capturing(formatted(OWNER))
    assert not admin.capturing(formatted(ADMIN))
    assert await admin.handle_message(formatted(OWNER, photo=True))
    assert "Нужен текст сообщением" in ui.text
    assert await admin.handle_message(formatted(OWNER))
    assert "Сохранено" in ui.text and "версия 2" in ui.text and not admin.capturing(formatted(OWNER))
    assert not await admin.handle_message(formatted(OWNER))  # nothing is waiting any more
    await ui.press(OWNER, "Включить")
    assert "🟢 включена" in ui.text
    # the user sees the text with every entity
    await ui.click(USER, encode(PAGE, arg="faq"))
    assert ui.text == TEXT
    sent = ui.last.entities
    assert sent is not None and [e.type for e in sent] == ["bold", "custom_emoji", "spoiler"]
    assert sent[1].custom_emoji_id == "5368324170671202286"
    await ui.click(USER, encode("page.faq"))  # the alias for content buttons
    assert ui.text == TEXT
    await ui.click(USER, encode("sys", "faq"))
    assert ui.text == TEXT


async def test_capture_cancel_and_stale_version(
    env: tuple[UiEnv, PageAdminScreens, PageUserScreens], pages: PageService
) -> None:
    ui, admin, _ = env
    await ui.click(ADMIN, encode(SCREEN_WAIT, arg="rules:en"))  # an old button: edits the Russian text
    assert "(EN)" not in ui.text and "Правила" in ui.text
    assert await admin.handle_message(text_message(ADMIN, "/cancel"))
    assert "Правила" in ui.text and not admin.capturing(formatted(ADMIN))
    await ui.click(ADMIN, encode(SCREEN_WAIT, arg="rules:ru"))
    assert not await admin.handle_message(text_message(ADMIN, "/start"))  # another command: not ours
    await ui.click(ADMIN, encode(SCREEN_WAIT, arg="rules:ru"))
    await pages.save_text("rules", "ru", "чужая правка", None, ACTOR)
    assert await admin.handle_message(formatted(ADMIN))
    assert "уже изменили" in ui.text
    assert pages.get("rules").block_for("ru").text == "чужая правка"  # type: ignore[union-attr]
    await ui.click(ADMIN, encode(SCREEN_WAIT, arg="nope:ru"))
    assert not admin.capturing(formatted(ADMIN))
    # rights taken away while waiting: the message is not captured
    await ui.click(ADMIN, encode(SCREEN_WAIT, arg="faq:ru"))
    ui.users.by_tg[ADMIN] = ui.users.by_tg[ADMIN_NOPERM]
    assert not await admin.handle_message(formatted(ADMIN))


async def test_disabled_page_is_hidden_from_users_but_previewable(
    env: tuple[UiEnv, PageAdminScreens, PageUserScreens],
) -> None:
    ui, _, _ = env
    await ui.click(OWNER, encode(PAGE, arg="offer"))
    assert ui.text.startswith("Здесь будет текст публичной оферты")
    await ui.click(USER, encode(PAGE, arg="offer"))
    assert not ui.text.startswith("Здесь будет текст")
    await ui.click(USER, encode(PAGE, arg="../etc"))
    assert not ui.text.startswith("Здесь будет")


async def test_consent_screen(
    env: tuple[UiEnv, PageAdminScreens, PageUserScreens], pages: PageService
) -> None:
    ui, _, user_screens = env
    await ui.click(OWNER, encode(SCREEN_CARD, arg="consent"))
    assert "не запрашивается" in ui.text
    await ui.press(OWNER, "Включить")
    assert "принимают версию 1" in ui.text
    uid = ui.uid(USER)
    assert await pages.needs_consent(uid) is not None
    await ui.click(USER, encode(PAGE, arg="consent"))
    assert "Принимаю" in ui.labels()[0]
    await ui.press(USER, "Принимаю")
    assert ui.toasts[-1] == "Спасибо!" and await pages.needs_consent(uid) is None
    # an old button after a new consent request shows the current text again
    old = encode(PAGE, "ok", "consent:1")
    await ui.click(OWNER, encode(SCREEN_CARD, arg="consent"))
    await pages.save_text("consent", "ru", "Новые условия", None, ACTOR)
    await ui.click(OWNER, encode(ACTIONS, "cons", "consent:1"))
    assert "Текст уже изменили" in ui.text
    await ui.click(OWNER, encode(ACTIONS, "cons", "consent:2"))
    assert "Согласие запрошено заново" in ui.text
    await ui.click(USER, old)
    assert ui.text == "Новые условия" and await pages.needs_consent(uid) is not None

    async def after(_ctx: object) -> object:
        from svbg.tg.ui.view import Redirect

        return Redirect(PAGE, "faq")

    user_screens.after_consent = after  # type: ignore[assignment]
    await pages.set_enabled("faq", True, ACTOR)
    await ui.press(USER, "Принимаю")
    assert ui.text.startswith("Здесь будут ответы")


async def test_versions_new_page_and_delete(
    env: tuple[UiEnv, PageAdminScreens, PageUserScreens], pages: PageService
) -> None:
    ui, admin, _ = env
    await ui.click(OWNER, encode(ACTIONS, "new"))
    await ui.type(OWNER, "Bad Code")
    await ui.type(OWNER, "Доставка")
    assert "Код страницы" in ui.text
    await ui.press(OWNER, "Новая страница")
    await ui.type(OWNER, "delivery")
    await ui.type(OWNER, "Доставка")
    assert "Доставка" in ui.text and "screen:page.delivery" in ui.text
    await ui.click(OWNER, encode(PAGE, arg="delivery"))
    await ui.click(OWNER, encode("page.delivery"))  # alias registered right away
    assert ui.text == "Доставка"
    await pages.save_text("delivery", "ru", "Курьером", None, ACTOR)
    await ui.click(OWNER, encode("pgs.v", arg="delivery"))
    assert [lb.split(" · ")[0] for lb in ui.labels()[:2]] == ["• v2", "v1"]
    await ui.press(OWNER, "• v2")
    assert ui.toasts[-1] == "Это текущая версия"
    await ui.press(OWNER, "v1 ·")
    assert "Возвращена версия 1" in ui.text and "версия 3" in ui.text
    await ui.press(OWNER, "Название")
    await ui.type(OWNER, "Доставка и оплата")
    assert "<b>Доставка и оплата</b>" in ui.text
    await ui.press(OWNER, "Удалить")
    await ui.press(OWNER, "Да, удалить")
    assert ui.toasts[-1] == "🗑 Удалено" and pages.get("delivery") is None
    await ui.click(OWNER, encode("pgs.d", arg="faq"))  # system pages cannot be deleted
    assert "Удалить страницу" not in ui.text
    await ui.click(OWNER, encode(ACTIONS, "del", "faq"))
    assert "только выключить" in str(ui.toasts[-1])
    assert admin.aiogram_router() is not None
