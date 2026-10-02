"""The default banner in the bot: shown on screens (uploaded once, then sent by ``file_id``), recognised in
the screen card («🗑 Убрать картинку»), taken off every screen / put back from the constructor's home, undo."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import EditMessageCaption, EditMessageMedia, EditMessageText, SendMessage, SendPhoto
from aiogram.types import InputFile

from svbg.content.banner import banner_sha256, is_banner
from svbg.content.store import ContentStore
from svbg.tg.admin.content.screens import SCREEN_HOME
from svbg.tg.ui import codec
from svbg.tg.ui.edit_mode import SCREEN_EDITOR
from tests.dbkit import CountingDatabase
from tests.tg.admin.content.kit import OWNER, USER, CEnv, build_cenv, msg
from tests.tg.ui.ui_harness import callback


@pytest.fixture
async def ce(db: CountingDatabase, tmp_path: Path) -> AsyncIterator[CEnv]:
    """The constructor over an installation that got the default banner (as after an update)."""
    async with build_cenv(db, tmp_path) as env:
        await ContentStore(db, media_root=env.library.root).load()  # installs the banner, migrates once
        await env.env.content.reload()
        yield env


def _banner_codes(ce: CEnv) -> set[str]:
    snap = ce.env.content.snapshot
    return {str(e.code or e.id) for e in snap.by_id.values() if is_banner(snap.get_media(e.screen.media_id))}


def _uploads(ce: CEnv) -> list[Any]:
    """Calls that sent the picture's bytes (not a cached ``file_id``)."""
    out: list[Any] = []
    for call in ce.transport.calls:
        file = call.photo if isinstance(call, SendPhoto) else None
        if isinstance(call, EditMessageMedia):
            file = call.media.media
        if isinstance(file, InputFile):
            out.append(call)
    return out


async def test_banner_is_uploaded_once_then_sent_by_file_id(ce: CEnv) -> None:
    user = await ce.add(USER, "user")
    await ce.click(USER, "home")
    first = ce.last()  # the text message becomes the picture in place
    assert isinstance(first, EditMessageMedia) and isinstance(first.media.media, InputFile)
    rows = await ce.db.raw("select file_ids from media where sha256 = $1", banner_sha256())
    assert rows[0]["file_ids"] == {"42": "TG-FILE-ID"}  # learned after the first upload

    # another screen with the banner, clicked on that message: the same picture, only the caption changes
    for screen in ("info", "home"):
        state = await ce.env.ui_state.get(user.user_id)
        assert state.main_msg_id is not None
        data = codec.encode(screen, codec.ACTION_OPEN)
        await ce.router.dispatch_callback(callback(USER, data, message_id=state.main_msg_id, photo=True))
        assert isinstance(ce.transport.calls[-1], EditMessageCaption)
    # a fresh message and a reloaded snapshot reuse the file_id too
    await ce.env.content.reload()
    await ce.router.show(user, USER, "home", new=True)
    last = ce.transport.of(SendPhoto)[-1]
    assert last.photo == "TG-FILE-ID"
    # after a restart (a new store reads the file_id back from the database)
    fresh = ContentStore(ce.db)
    await fresh.load()
    entry = fresh.get_screen("home")
    assert entry is not None and entry.screen.media_id is not None
    assert fresh.file_id(entry.screen.media_id, 42) == "TG-FILE-ID"
    assert len(_uploads(ce)) == 1


async def test_concurrent_first_screens_upload_the_banner_once(ce: CEnv) -> None:
    users = [USER + i for i in range(6)]
    for tg_id in users:
        await ce.add(tg_id, "user")
    ce.transport.delay[EditMessageMedia] = 0.05  # the first upload is still in flight when the others arrive
    await asyncio.gather(*(ce.click(tg_id, "home") for tg_id in users))
    sent = ce.transport.of(EditMessageMedia)
    assert len(sent) == len(users)
    assert len(_uploads(ce)) == 1  # the others waited for it and sent its file_id
    assert sum(p.media.media == "TG-FILE-ID" for p in sent) == len(users) - 1


async def test_screen_card_marks_the_banner_and_removes_it(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user")
    home = ce.env.content.get_screen("home")
    assert home is not None
    await ce.click(OWNER, SCREEN_EDITOR, arg=str(home.id))
    assert "Медиа: 🖼 заглушка" in ce.last_text()
    assert "🗑 Убрать медиа" not in [t for t, _ in ce.buttons()]
    await ce.press(OWNER, "🗑 Убрать картинку")
    assert ce.toasts[-1] == "⚡ Применено"
    entry = ce.env.content.get_screen("home")
    assert entry is not None and entry.screen.media_id is None
    assert "home" not in _banner_codes(ce) and "info" in _banner_codes(ce)
    history = await ce.editor.history(home.id)
    assert history[0].summary == "Заглушка убрана"
    await ce.click(USER, "home")
    assert isinstance(ce.last(), SendMessage | EditMessageText)  # a text screen now
    # an own picture is labelled as such
    await ce.act(OWNER, "md", str(home.id))
    assert await ce.send(_photo_msg())
    await ce.click(OWNER, SCREEN_EDITOR, arg=str(home.id))
    assert "🗑 Убрать медиа" in [t for t, _ in ce.buttons()]
    assert "заглушка" not in ce.last_text()


def _photo_msg() -> Any:
    return msg(OWNER, photo=True)


async def test_home_removes_the_banner_everywhere_and_undo_restores(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    # the owner's own picture on «info»
    await ce.act(OWNER, "md", str(ce.env.content.get_screen("info").id))  # type: ignore[union-attr]
    assert await ce.send(_photo_msg())
    own = ce.env.content.get_screen("info")
    assert own is not None and own.screen.media_id is not None
    own_media = own.screen.media_id
    shown = _banner_codes(ce)
    assert "info" not in shown and "home" in shown

    await ce.click(OWNER, SCREEN_HOME)
    assert f"на экранах — {len(shown)}" in ce.last_text()
    assert "🖼 Вернуть заглушку" not in [t for t, _ in ce.buttons()]  # nowhere to put it
    await ce.press(OWNER, "🖼 Заглушка: убрать со всех экранов")
    assert ce.toasts[-1] == "⚡ Применено"
    assert f"заглушка убрана с {len(shown)} экр." in ce.last_text()
    assert _banner_codes(ce) == set()
    info = ce.env.content.get_screen("info")
    assert info is not None and info.screen.media_id == own_media  # never touched
    texts = [t for t, _ in ce.buttons()]
    assert "🖼 Заглушка: убрать со всех экранов" not in texts and "🖼 Вернуть заглушку" in texts

    await ce.press(OWNER, "↩️ Отменить")
    assert ce.toasts[-1] == "↩️ Отменено"
    assert _banner_codes(ce) == shown
    assert "↩️ Отменено" in ce.last_text() and "🖼 Заглушка" in ce.last_text()  # back on the constructor home

    # take it off again and put it back with «🖼 Вернуть заглушку»: own screens without a picture get it too
    await ce.press(OWNER, "🖼 Заглушка: убрать со всех экранов")
    res = await ce.editor.create_screen("Акция", code="promo_x", actor=None)
    assert res.screen_id is not None
    await ce.click(OWNER, SCREEN_HOME)
    await ce.press(OWNER, "🖼 Вернуть заглушку")
    assert ce.toasts[-1] == "⚡ Применено"
    assert _banner_codes(ce) == shown | {"promo_x"}
    info = ce.env.content.get_screen("info")
    assert info is not None and info.screen.media_id == own_media


async def test_users_need_the_right_to_remove_the_banner(ce: CEnv) -> None:
    await ce.add(USER, "user")
    shown = _banner_codes(ce)
    await ce.act(USER, "bnx")
    assert ce.toasts[-1] == "Нет прав"
    assert _banner_codes(ce) == shown


async def test_restore_reinstalls_the_banner_file(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.editor.remove_banner(actor=None)
    for path in ce.library.root.rglob("*.jpg"):
        path.unlink()
    await ce.click(OWNER, SCREEN_HOME)
    await ce.press(OWNER, "🖼 Вернуть заглушку")
    assert ce.toasts[-1] == "⚡ Применено"
    assert "home" in _banner_codes(ce)
    assert any(ce.library.root.rglob("*.jpg"))
