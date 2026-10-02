"""The default banner on every message: policy (C1/C3 dressing), router, request middleware (C2)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.methods import EditMessageCaption, EditMessageMedia, SendMessage
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from ui_harness import FakeHub, FakeTransport, Users, add_user, callback, make_router

from svbg.content.store import ContentStore
from svbg.tg.banner import _SCOPE, BannerMiddleware, BannerPolicy, banner_scope, view_scope
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import visible_len
from svbg.tg.ui.router import ScreenCtx, ScreenRouter, UiStateStore
from svbg.tg.ui.view import View
from svbg.tg.user.render import plain_view
from tests.dbkit import CountingDatabase, open_db
from tests.fakes.telegram import FakeTelegram

URL = "https://shop.example/"


def _media_url(base: str, item: Any) -> str:
    return f"{base.rstrip('/')}/m/tok{item.id}.jpg"


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


@pytest.fixture
async def content(db: CountingDatabase, tmp_path: Path) -> ContentStore:
    store = ContentStore(db, media_root=tmp_path)
    await store.load()  # installs the banner and puts it on the system screens
    return store


def _policy(content: ContentStore, tmp_path: Path, *, url: str | None = None) -> BannerPolicy:
    return BannerPolicy(content, public_url=lambda: url, media_url=_media_url, media_root=tmp_path)


# ---------------------------------------------------------------- policy


def test_visible_len_counts_after_parsing() -> None:
    assert visible_len("<b>a&lt;b</b>", "HTML") == 3
    assert visible_len("<b>a&lt;b</b>", None) == 13
    assert visible_len("👋<i>x</i>", "html") == 3  # a surrogate pair is two units


async def test_banner_is_on_while_a_screen_shows_it(content: ContentStore, tmp_path: Path, db: Any) -> None:
    policy = _policy(content, tmp_path)
    banner = policy.banner()
    assert banner is not None and policy.is_on()
    await db.raw("update screens set media_id = null where media_id = $1", banner.id)
    await content.reload()
    assert policy.banner() is None and not policy.is_on()
    view = View(text="Привет")
    assert policy.dress(view, bot_id=42) is view  # off: nothing changes


async def test_dress_table(content: ContentStore, tmp_path: Path) -> None:
    policy = _policy(content, tmp_path)
    bid = policy.banner().id  # type: ignore[union-attr]
    short = policy.dress(View(text="Привет"), bot_id=42)
    assert short.media is not None and short.media.key == f"m:{bid}" and short.media.media_id == bid
    long_text = "x" * 1100
    assert policy.dress(View(text=long_text), bot_id=42).preview is None  # no PUBLIC_URL: as is
    with_url = _policy(content, tmp_path, url=URL).dress(View(text=long_text), bot_id=42)
    assert with_url.media is None and with_url.preview is not None
    assert with_url.preview.url == f"https://shop.example/m/tok{bid}.jpg" and with_url.preview.show_above_text
    # an HTML text over 1024 raw but under it after parsing still fits a caption
    html = "<b>" + "<i>x</i>" * 200 + "</b>"
    assert policy.dress(View(text=html, parse_mode="HTML"), bot_id=42).media is not None
    removed = View(text="Привет", banner=False)
    assert policy.dress(removed, bot_id=42) is removed
    assert policy.dress(View(text="Привет"), bot_id=42, group=True).media is None  # admin chat: never a photo
    assert policy.dress(View(text="Привет"), bot_id=42, allow_upload=False).media is None  # no cached id yet


# ---------------------------------------------------------------- router (C1)


async def _router(db: CountingDatabase, content: ContentStore, tmp_path: Path, **kw: Any) -> ScreenRouter:
    router = make_router(
        FakeTransport(),
        Users(),
        UiStateStore(db),
        content,
        CallbackCodec(db, key=b"t" * 32),
        FakeHub(),
        media_root=tmp_path,
        banner=_policy(content, tmp_path, **kw),
    )

    @router.screen("code1")
    async def code1(ctx: ScreenCtx, arg: Any) -> View:
        return View(text="Экран из кода")

    @router.screen("code2")
    async def code2(ctx: ScreenCtx, arg: Any) -> View:
        return View(text="Второй экран")

    return router


async def test_code_screens_get_the_banner(
    db: CountingDatabase, content: ContentStore, tmp_path: Path
) -> None:
    router = await _router(db, content, tmp_path)
    transport: FakeTransport = router.transport  # type: ignore[assignment]
    users: Users = router.user_loader  # type: ignore[assignment]
    users.by_tg[111] = UserCtx(await add_user(db, 111, "user"), telegram_id=111)
    await router.dispatch_callback(callback(111, "v1:code1:o"))
    (edit,) = transport.of(EditMessageMedia)  # text → photo in place
    assert edit.media.caption == "Экран из кода"
    bid = router.banner.banner().id  # type: ignore[union-attr]
    assert content.file_id(bid, 42) == "TG-FILE-ID"  # uploaded once, cached
    await router.dispatch_callback(callback(111, "v1:code2:o", photo=True, cq_id="c2"))
    (caption,) = transport.of(EditMessageCaption)  # same picture: only the caption changes
    assert caption.caption == "Второй экран"
    await router.dispatch_callback(callback(111, "v1:home:o", photo=True, cq_id="c3"))
    assert len(transport.of(EditMessageCaption)) == 2  # content screen with the banner: same key
    # the owner removed the picture of «home»: no banner there
    await db.raw("update screens set media_id = null where code = 'home'")
    await content.reload()
    await router.dispatch_callback(callback(111, "v1:home:o", cq_id="c4"))
    sent = transport.of(SendMessage)[-1]  # photo → text: a new message
    assert sent.text.startswith("👋") and sent.link_preview_options.is_disabled  # type: ignore[union-attr]


# ---------------------------------------------------------------- middleware (C2)


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram() as fake:
        yield fake


async def _bot(tg: FakeTelegram, policy: BannerPolicy) -> Bot:
    bot = Bot(
        tg.add_bot(),
        session=AiohttpSession(api=TelegramAPIServer.from_base(tg.url)),
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    bot.session.middleware.register(BannerMiddleware(policy))
    return bot


def _uploads(tg: FakeTelegram) -> list[Any]:
    return [c for c in tg.calls_for("sendPhoto") if str(c.params.get("photo")).startswith("attach://")]


async def test_send_message_becomes_a_photo(tg: FakeTelegram, content: ContentStore, tmp_path: Path) -> None:
    policy = _policy(content, tmp_path)
    bot = await _bot(tg, policy)
    try:
        msg = await bot.send_message(1001, "<b>Привет</b>", disable_notification=True)
        assert msg.photo and msg.caption == "<b>Привет</b>"
        (call,) = tg.calls_for("sendPhoto")
        assert call.params["disable_notification"] is True and call.params["parse_mode"] == "HTML"
        bid = policy.banner().id  # type: ignore[union-attr]
        file_id = content.file_id(bid, bot.id)
        assert file_id and file_id.startswith(msg.photo[-1].file_id.rsplit("_", 1)[0])
        await bot.send_message(1001, "Ещё")
        assert tg.calls_for("sendPhoto")[-1].params["photo"] == file_id and len(_uploads(tg)) == 1
        # an edit of that photo goes as a caption edit
        await bot.edit_message_text("Новый текст", chat_id=1001, message_id=msg.message_id)
        assert tg.calls_for("editMessageText") == []
        assert tg.calls_for("editMessageCaption")[-1].params["caption"] == "Новый текст"
        # after a restart (empty memory) the "no text" refusal turns into a caption edit too
        bot.session.middleware.unregister(bot.session.middleware[0])
        bot.session.middleware.register(BannerMiddleware(_policy(content, tmp_path)))
        await bot.edit_message_text("Снова", chat_id=1001, message_id=msg.message_id)
        assert tg.calls_for("editMessageText")[-1].status == 400
        assert tg.calls_for("editMessageCaption")[-1].params["caption"] == "Снова"
    finally:
        await bot.session.close()


async def test_long_edited_and_explicit(tg: FakeTelegram, content: ContentStore, tmp_path: Path) -> None:
    bot = await _bot(tg, _policy(content, tmp_path))
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ok", callback_data="x")]])
    try:
        await bot.send_message(1001, "x" * 1100)  # too long, no PUBLIC_URL: as is
        assert tg.calls_for("sendMessage")[-1].params["text"] == "x" * 1100
        await bot.send_message(1001, "карточка", reply_markup=markup)  # no PUBLIC_URL: a photo is fine
        assert tg.calls_for("sendPhoto")[-1].params["caption"] == "карточка"
        own = LinkPreviewOptions(url="https://example.org/")
        await bot.send_message(1001, "своя ссылка", link_preview_options=own)
        assert tg.calls_for("sendMessage")[-1].params["link_preview_options"]["url"] == "https://example.org/"
        with banner_scope("off"):
            await bot.send_message(1001, "без картинки")
        with banner_scope("decided"):
            await bot.send_message(1001, "уже решено")
        assert [c.params["text"] for c in tg.calls_for("sendMessage")][-2:] == ["без картинки", "уже решено"]
        await bot.copy_message(1001, 1001, tg.calls_for("sendMessage")[-1].result["message_id"])
        assert tg.calls_for("copyMessage")[-1].ok
    finally:
        await bot.session.close()
    url_bot = await _bot(tg, _policy(content, tmp_path, url=URL))
    try:
        await url_bot.send_message(1001, "карточка", reply_markup=markup)  # will be edited: a preview
        preview = tg.calls_for("sendMessage")[-1].params["link_preview_options"]
        assert preview["url"].startswith("https://shop.example/m/tok") and preview["show_above_text"]
        await url_bot.send_message(1001, "y" * 1100)
        assert tg.calls_for("sendMessage")[-1].params["link_preview_options"]["prefer_large_media"]
        with banner_scope("text"):
            await url_bot.send_message(1001, "отчёт")
        assert "link_preview_options" in tg.calls_for("sendMessage")[-1].params
    finally:
        await url_bot.session.close()


async def test_documents_get_a_thumbnail(tg: FakeTelegram, content: ContentStore, tmp_path: Path) -> None:
    bot = await _bot(tg, _policy(content, tmp_path))
    try:
        await bot.send_document(1001, BufferedInputFile(b"backup", filename="b.tar"), caption="Копия")
        params = tg.calls_for("sendDocument")[-1].params
        assert str(params.get("thumbnail")).startswith("attach://")
        await bot.send_document(1001, "FILE-ID")
        assert "thumbnail" not in tg.calls_for("sendDocument")[-1].params
    finally:
        await bot.session.close()


async def test_one_upload_and_stale_file_id(tg: FakeTelegram, content: ContentStore, tmp_path: Path) -> None:
    policy = _policy(content, tmp_path)
    bot = await _bot(tg, policy)
    try:
        await asyncio.gather(*(bot.send_message(1001 + i, f"m{i}") for i in range(5)))
        assert len(tg.calls_for("sendPhoto")) == 5 and len(_uploads(tg)) == 1
        bid = policy.banner().id  # type: ignore[union-attr]
        await content.remember_file_id(bid, bot.id, "BAD")
        tg.fail_next(
            "400", method="sendPhoto", description="Bad Request: wrong file identifier/HTTP URL specified"
        )
        msg = await bot.send_message(1001, "дойдёт")
        assert msg.text == "дойдёт"  # sent without the banner, never lost
        assert not content.file_id(bid, bot.id)  # forgotten: the next one uploads again
        await bot.send_message(1001, "снова с картинкой")
        assert len(_uploads(tg)) == 2
    finally:
        await bot.session.close()


async def test_banner_off_passes_everything(
    tg: FakeTelegram, content: ContentStore, tmp_path: Path, db: CountingDatabase
) -> None:
    await db.raw("update screens set media_id = null")
    await content.reload()
    bot = await _bot(tg, _policy(content, tmp_path))
    try:
        await bot.send_message(1001, "просто текст")
        await bot.send_document(1001, BufferedInputFile(b"x", filename="a.txt"))
        assert tg.calls_for("sendPhoto") == [] and "thumbnail" not in tg.calls_for("sendDocument")[-1].params
    finally:
        await bot.session.close()


# ---------------------------------------------------------------- C3: views built outside a click


async def test_plain_view_follows_the_screen_picture(content: ContentStore, db: CountingDatabase) -> None:
    user = UserCtx(1, lang="ru")
    view = plain_view(user, content, "home")
    assert view.banner is None and view.picture is None  # the banner: the middleware adds it
    await db.raw("update screens set media_id = null where code = 'home'")
    await content.reload()
    removed = plain_view(user, content, "home")
    assert removed.banner is False
    with view_scope(removed):
        assert _SCOPE.get().mode == "off"
