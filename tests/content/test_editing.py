"""ContentEditor: one transaction per change, CAS by screens.version, content_audit batches, undo, reload."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from svbg.content.editing import CAPTION_LIMIT, ContentEditor, EditError, StaleError
from svbg.content.store import ContentStore
from svbg.core import clock
from tests.dbkit import CountingDatabase

SHA = "a" * 64


@pytest.fixture
async def store(db: CountingDatabase) -> ContentStore:
    s = ContentStore(db)
    await s.load()
    return s


@pytest.fixture
def editor(db: CountingDatabase, store: ContentStore) -> ContentEditor:
    return ContentEditor(db, store)


@pytest.fixture
def frozen() -> Iterator[clock.FrozenClock]:
    fc = clock.FrozenClock()
    clock.set_clock(fc)
    try:
        yield fc
    finally:
        clock.reset_clock()


@pytest.fixture
async def custom(editor: ContentEditor, store: ContentStore) -> AsyncIterator[int]:
    res = await editor.create_screen("Акция", code="promo_may", actor=None)
    assert res.screen_id is not None
    yield res.screen_id


def home(store: ContentStore) -> tuple[int, int]:
    entry = store.get_screen("home")
    assert entry is not None
    return entry.id, entry.screen.version


async def test_set_text_applies_immediately_with_entities_and_audit(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore
) -> None:
    sid, ver = home(store)
    entities = [
        {"type": "bold", "offset": 0, "length": 6},
        {"type": "custom_emoji", "offset": 7, "length": 2, "custom_emoji_id": "5368324170671202286"},
    ]
    started = time.perf_counter()
    res = await editor.set_text(sid, "ru", "Привет 🔥 мир", entities, expected_version=ver, actor=None)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0  # transaction + snapshot reload: everyone sees it on the next click
    entry = store.get_screen("home")
    assert entry is not None
    assert entry.screen.version == ver + 1 == res.version
    block = entry.text("ru")
    assert block.text == "Привет 🔥 мир"
    assert [e["type"] for e in block.entities] == ["bold", "custom_emoji"]
    rows = await db.raw(
        "select entity, old, new from content_audit where batch_id = $1 order by id", res.batch_id
    )
    assert [r["entity"] for r in rows] == ["screen", "note"]
    assert rows[0]["old"]["version"] == ver and rows[0]["new"]["version"] == ver + 1
    assert rows[1]["new"]["summary"] == "Текст (ru)"


async def test_two_admins_do_not_overwrite_each_other(editor: ContentEditor, store: ContentStore) -> None:
    sid, ver = home(store)
    await editor.set_text(sid, "ru", "Первый админ", None, expected_version=ver, actor=None)
    with pytest.raises(StaleError):
        await editor.set_text(sid, "ru", "Второй админ", None, expected_version=ver, actor=None)
    entry = store.get_screen(sid)
    assert entry is not None and entry.text("ru").text == "Первый админ"


async def test_undo_restores_previous_text_and_is_audited(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore
) -> None:
    sid, ver = home(store)
    before = store.get_screen(sid)
    assert before is not None
    old_text = before.text("ru").text
    res = await editor.set_text(sid, "ru", "Новый текст", None, expected_version=ver, actor=None)
    undo = await editor.undo(res.batch_id, actor=None)
    after = store.get_screen(sid)
    assert after is not None
    assert after.text("ru").text == old_text
    assert after.screen.version == ver + 2 == undo.version
    with pytest.raises(EditError, match="уже отменено"):
        await editor.undo(res.batch_id, actor=None)
    history = await editor.history(sid)
    assert history[0].is_undo and history[0].summary.startswith("↩️ Отменено")
    assert history[1].undone and history[1].summary == "Текст (ru)"
    # the undo itself can be undone (redo)
    await editor.undo(undo.batch_id, actor=None)
    again = store.get_screen(sid)
    assert again is not None and again.text("ru").text == "Новый текст"


async def test_undo_window_is_ten_minutes(
    editor: ContentEditor, store: ContentStore, frozen: clock.FrozenClock
) -> None:
    sid, ver = home(store)
    res = await editor.set_text(sid, "ru", "Текст", None, expected_version=ver, actor=None)
    frozen.advance(minutes=11)
    with pytest.raises(EditError, match="10 минут"):
        await editor.undo(res.batch_id, actor=None)
    assert await editor.last_undoable(sid) is None


async def test_undo_refused_after_a_later_change(editor: ContentEditor, store: ContentStore) -> None:
    sid, ver = home(store)
    first = await editor.set_text(sid, "ru", "Раз", None, expected_version=ver, actor=None)
    await editor.set_text(sid, "ru", "Два", None, expected_version=ver + 1, actor=None)
    with pytest.raises(EditError, match="уже меняли"):
        await editor.undo(first.batch_id, actor=None)


async def test_add_button_validates_and_checks_row_width(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    entry = store.get_screen(custom)
    assert entry is not None
    ver = entry.screen.version
    for i in range(8):
        res = await editor.add_button(
            custom,
            label={"ru": f"B{i}"},
            action="url:https://example.com",
            row=0,
            expected_version=ver,
            actor=None,
        )
        assert res.version is not None
        ver = res.version
    with pytest.raises(EditError, match="не больше 8"):
        await editor.add_button(
            custom,
            label={"ru": "B9"},
            action="url:https://example.com",
            row=0,
            expected_version=ver,
            actor=None,
        )
    with pytest.raises(EditError, match="Не сохранено"):
        await editor.add_button(
            custom, label={"ru": "X"}, action="url:ftp://bad", expected_version=ver, actor=None
        )
    with pytest.raises(EditError, match="Не сохранено"):
        await editor.add_button(
            custom,
            label={"ru": "X"},
            action="screen:home",
            visible_if={"sub": "paid"},
            expected_version=ver,
            actor=None,
        )
    entry = store.get_screen(custom)
    assert entry is not None
    assert len([b for b in entry.screen.buttons if b.row == 0]) == 8


async def test_new_button_goes_above_the_menu_row(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    entry = store.get_screen(custom)
    assert entry is not None
    res = await editor.add_button(
        custom,
        label={"ru": "Купить"},
        action="system:buy",
        style="success",
        expected_version=entry.screen.version,
        actor=None,
    )
    entry = store.get_screen(custom)
    assert entry is not None
    new = next(b for b in entry.screen.buttons if b.id == res.created_id)
    menu = next(b for b in entry.screen.buttons if b.id != res.created_id)
    assert new.row < menu.row
    assert new.style == "success"


async def test_system_button_keeps_its_action(editor: ContentEditor, store: ContentStore) -> None:
    entry = store.get_screen("home")
    assert entry is not None
    sys_btn = next(b for b in entry.screen.buttons if b.system_key)
    assert sys_btn.id is not None
    ver = entry.screen.version
    with pytest.raises(EditError, match="действие менять нельзя"):
        await editor.update_button(sys_btn.id, action="url:https://x.org", expected_version=ver, actor=None)
    with pytest.raises(EditError, match="удалить нельзя"):
        await editor.delete_button(sys_btn.id, expected_version=ver, actor=None)
    res = await editor.update_button(
        sys_btn.id,
        label={"ru": "Новое имя"},
        style="danger",
        icon_custom_emoji_id="5368324170671202286",
        enabled=False,
        expected_version=ver,
        actor=None,
    )
    entry = store.get_screen("home")
    assert entry is not None
    b = next(b for b in entry.screen.buttons if b.id == sys_btn.id)
    assert (b.label["ru"], b.style, b.icon_custom_emoji_id, b.enabled) == (
        "Новое имя",
        "danger",
        "5368324170671202286",
        False,
    )
    assert b.label.get("en") == sys_btn.label.get("en")  # other languages are kept
    await editor.undo(res.batch_id, actor=None)
    entry = store.get_screen("home")
    assert entry is not None
    restored = next(b for b in entry.screen.buttons if b.id == sys_btn.id)
    assert restored == sys_btn


async def test_move_button_renumbers_and_undo_restores(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    entry = store.get_screen(custom)
    assert entry is not None
    ver = entry.screen.version
    ids = []
    for name in ("A", "B", "C"):
        res = await editor.add_button(
            custom, label={"ru": name}, action="copy:x", row=0, expected_version=ver, actor=None
        )
        assert res.version is not None and res.created_id is not None
        ver, _ = res.version, ids.append(res.created_id)

    def layout() -> list[tuple[str, int, int]]:
        e = store.get_screen(custom)
        assert e is not None
        return sorted((b.label["ru"], b.row, b.sort) for b in e.screen.buttons if b.row < 9)

    res = await editor.move_button(ids[2], "left", expected_version=ver, actor=None)
    assert layout() == [("A", 0, 0), ("B", 0, 2), ("C", 0, 1)]
    with pytest.raises(EditError, match="с краю"):
        await editor.move_button(ids[0], "left", expected_version=res.version, actor=None)
    res2 = await editor.move_button(ids[0], "down", expected_version=res.version, actor=None)
    assert layout() == [("A", 1, 0), ("B", 0, 1), ("C", 0, 0)]
    await editor.undo(res2.batch_id, actor=None)
    assert layout() == [("A", 0, 0), ("B", 0, 2), ("C", 0, 1)]


async def test_delete_screen_checks_references_and_undo_restores(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    sid, ver = home(store)
    link = await editor.add_button(
        sid, label={"ru": "🔥 Акция"}, action="screen:promo_may", expected_version=ver, actor=None
    )
    entry = store.get_screen(custom)
    assert entry is not None
    with pytest.raises(EditError, match="На экран ведут"):
        await editor.delete_screen(custom, expected_version=entry.screen.version, actor=None)
    assert link.created_id is not None
    await editor.delete_button(link.created_id, expected_version=link.version, actor=None)
    with pytest.raises(EditError, match="Системный экран"):
        await editor.delete_screen(sid, expected_version=None, actor=None)
    buttons_before = entry.screen.buttons
    res = await editor.delete_screen(custom, expected_version=entry.screen.version, actor=None)
    assert store.get_screen(custom) is None
    await editor.undo(res.batch_id, actor=None)
    back = store.get_screen("promo_may")
    assert back is not None and back.id == custom
    assert back.screen.buttons == buttons_before


async def test_create_screen_validates_code_and_undo_removes_it(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    with pytest.raises(EditError, match="уже занят"):
        await editor.create_screen("Ещё", code="promo_may", actor=None)
    with pytest.raises(EditError, match="Код экрана"):
        await editor.create_screen("Ещё", code="sys", actor=None)
    res = await editor.create_screen("Без кода", actor=None)
    assert res.screen_id is not None and store.get_screen(res.screen_id) is not None
    await editor.undo(res.batch_id, actor=None)
    assert store.get_screen(res.screen_id) is None
    assert store.get_screen(custom) is not None


async def test_media_caption_limit_and_mode(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    rows = await db.raw(
        "insert into media (kind, sha256, path) values ('photo', $1, 'aa/x.jpg') returning id", SHA
    )
    media_id = int(rows[0]["id"])
    entry = store.get_screen(custom)
    assert entry is not None
    long_text = "я" * (CAPTION_LIMIT + 1)
    res = await editor.set_text(
        custom, "ru", long_text, None, expected_version=entry.screen.version, actor=None
    )
    with pytest.raises(EditError, match="превью-ссылку"):
        await editor.set_media(custom, media_id, expected_version=res.version, actor=None)
    res = await editor.set_media_mode(custom, "preview", expected_version=res.version, actor=None)
    res = await editor.set_media(custom, media_id, expected_version=res.version, actor=None)
    with pytest.raises(EditError, match="не поместится"):
        await editor.set_media_mode(custom, "attach", expected_version=res.version, actor=None)
    entry = store.get_screen(custom)
    assert entry is not None and entry.screen.media_id == media_id and entry.screen.media_mode == "preview"
    with pytest.raises(EditError, match="не найден"):
        await editor.set_media(custom, 999_999, expected_version=res.version, actor=None)


async def test_condition_is_compiled_and_visible_only_to_matching_users(
    editor: ContentEditor, store: ContentStore
) -> None:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.renderer import build_keyboard

    sid, ver = home(store)
    await editor.add_button(
        sid,
        label={"ru": "Продлить со скидкой"},
        action="system:renew",
        visible_if={"sub": "expired"},
        expected_version=ver,
        actor=None,
    )
    entry = store.get_screen(sid)
    assert entry is not None

    def labels(state: str) -> list[str]:
        kb = build_keyboard(entry.keyboard("ru"), UserCtx(1, sub_state=state), "ru")
        return [b.text for row in kb.inline_keyboard for b in row]

    assert "Продлить со скидкой" in labels("expired")
    for state in ("none", "trial", "active", "frozen"):
        assert "Продлить со скидкой" not in labels(state)


async def test_undo_of_a_creation_is_refused_while_something_links_to_it(
    editor: ContentEditor, store: ContentStore
) -> None:
    created = await editor.create_screen("Промо", code="promo_x", actor=None)
    sid, ver = home(store)
    link = await editor.add_button(
        sid, label={"ru": "Промо"}, action="screen:promo_x", expected_version=ver, actor=None
    )
    with pytest.raises(EditError, match="уже ведут: кнопка «Промо» на экране home") as e:
        await editor.undo(created.batch_id, actor=None)
    assert e.value.code == "referenced"
    assert store.get_screen("promo_x") is not None
    assert link.created_id is not None
    await editor.delete_button(link.created_id, expected_version=link.version, actor=None)
    await editor.undo(created.batch_id, actor=None)  # nothing links there any more
    assert store.get_screen("promo_x") is None


async def test_undo_of_a_deletion_when_the_code_is_taken_or_twice_at_once(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore
) -> None:
    import asyncio

    import sqlalchemy as sa

    first = await editor.create_screen("Дубль", code="dup_x", actor=None)
    assert first.screen_id is not None and first.version is not None
    deleted = await editor.delete_screen(first.screen_id, expected_version=first.version, actor=None)
    second = await editor.create_screen("Новый дубль", code="dup_x", actor=None)
    with pytest.raises(EditError, match="уже занят") as e:
        await editor.undo(deleted.batch_id, actor=None)
    assert e.value.code == "code_taken"
    entry = store.get_screen("dup_x")
    assert entry is not None and entry.id == second.screen_id

    # two undos of one deletion at the same time: one restores, the other is a clean refusal
    assert second.screen_id is not None and second.version is not None
    gone = await editor.delete_screen(second.screen_id, expected_version=second.version, actor=None)

    async def hold() -> None:
        async with db.read() as conn:
            await conn.execute(sa.text("select pg_sleep(0.05)"))

    await asyncio.gather(hold(), hold(), hold())  # warm connections: both undos really run side by side
    results = await asyncio.gather(
        editor.undo(gone.batch_id, actor=None),
        editor.undo(gone.batch_id, actor=None),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], EditError), results
    assert store.get_screen("dup_x") is not None


async def test_at_most_max_buttons_per_screen(
    editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    from svbg.content.editing import MAX_BUTTONS

    entry = store.get_screen(custom)
    assert entry is not None
    ver = entry.screen.version
    count = len(entry.screen.buttons)
    while count < MAX_BUTTONS:
        res = await editor.add_button(
            custom,
            label={"ru": f"B{count}"},
            action="copy:x",
            row=10 + count // 8,
            enabled=count % 2 == 0,
            expected_version=ver,
            actor=None,
        )
        assert res.version is not None
        ver, count = res.version, count + 1
    with pytest.raises(EditError, match=f"больше {MAX_BUTTONS}") as e:
        await editor.add_button(custom, label={"ru": "X"}, action="copy:x", expected_version=ver, actor=None)
    assert e.value.code == "too_many"


class FlakyStore(ContentStore):
    """A store whose next ``fail`` reloads fail (the database blinked after the commit)."""

    fail = 0

    async def reload(self) -> Any:
        if self.fail:
            self.fail -= 1
            raise OSError("connection reset")
        return await super().reload()


async def test_a_failed_reload_after_commit_is_not_an_edit_error(db: CountingDatabase) -> None:
    import asyncio

    store = FlakyStore(db)
    await store.load()
    editor = ContentEditor(db, store, reload_retry=(0.01,))
    sid, ver = home(store)
    store.fail = 2
    res = await editor.set_text(sid, "ru", "Сохранено", None, expected_version=ver, actor=None)
    assert not res.live and res.version == ver + 1  # committed; the snapshot is behind for a moment
    assert editor.reload_pending
    for _ in range(300):
        if not editor.reload_pending:
            break
        await asyncio.sleep(0.01)
    entry = store.get_screen(sid)
    assert entry is not None and entry.text("ru").text == "Сохранено"  # applied without a restart
    res2 = await editor.set_text(sid, "ru", "Ещё", None, expected_version=res.version, actor=None)
    assert res2.live
    await editor.aclose()


async def test_preview_mode_without_public_url_keeps_caption_limits(
    db: CountingDatabase, store: ContentStore, custom: int
) -> None:
    public_url: list[str] = []
    editor = ContentEditor(db, store, preview_available=lambda: bool(public_url))
    rows = await db.raw(
        "insert into media (kind, sha256, path) values ('photo', $1, 'aa/y.jpg') returning id", "b" * 64
    )
    media_id = int(rows[0]["id"])
    entry = store.get_screen(custom)
    assert entry is not None
    res = await editor.set_media_mode(custom, "preview", expected_version=entry.screen.version, actor=None)
    res = await editor.set_media(custom, media_id, expected_version=res.version, actor=None)
    long_text = "я" * (CAPTION_LIMIT + 1)
    # without PUBLIC_URL the media goes as an attachment: the text must fit the caption
    with pytest.raises(EditError, match="PUBLIC_URL") as e:
        await editor.set_text(custom, "ru", long_text, None, expected_version=res.version, actor=None)
    assert e.value.code == "caption_too_long"
    public_url.append("https://shop.example")
    res = await editor.set_text(custom, "ru", long_text, None, expected_version=res.version, actor=None)
    public_url.clear()
    with pytest.raises(EditError, match="PUBLIC_URL"):
        await editor.set_media(custom, media_id, expected_version=res.version, actor=None)
    with pytest.raises(EditError, match="PUBLIC_URL"):
        await editor.set_media_mode(custom, "preview", expected_version=res.version, actor=None)


async def test_screen_references_cover_broadcasts_and_disabled_links(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore, custom: int
) -> None:
    from svbg.broadcasts.tables import broadcasts
    from svbg.db.meta import metadata
    from svbg.deeplinks.tables import deeplinks

    async with db.tx() as conn:  # not in every test schema; production has them from the migrations
        await conn.run_sync(lambda c: metadata.create_all(c, tables=[deeplinks, broadcasts]))
    button = {"label": {"ru": "Акция"}, "action": {"type": "screen", "target": "promo_may"}, "row": 0}
    rows = await db.raw(
        "insert into broadcasts (status, content, buttons) values ('draft', $1, $2) returning id",
        {"type": "text", "text": "x"},
        [button],
    )
    cast = int(rows[0]["id"])
    await db.raw(
        "insert into deeplinks (code, title, intent, enabled) values ('may', 'Май', $1, false)",
        {"screen": "promo_may"},
    )
    entry = store.get_screen(custom)
    assert entry is not None
    refs, warnings = await editor.references(custom, "promo_may")
    assert refs == [f"кнопка рассылки #{cast}"]
    assert warnings == ["выключенная ссылка l_may"]
    with pytest.raises(EditError, match=f"кнопка рассылки #{cast}"):
        await editor.delete_screen(custom, expected_version=entry.screen.version, actor=None)
    # a broadcast finished long ago no longer holds the screen; the disabled link is only a warning
    await db.raw(
        "update broadcasts set status = 'done', finished_at = now() - interval '40 days' where id = $1", cast
    )
    res = await editor.delete_screen(custom, expected_version=entry.screen.version, actor=None)
    assert res.warnings == ("выключенная ссылка l_may",)


async def test_undos_of_one_batch_wait_for_each_other(
    db: CountingDatabase, editor: ContentEditor, store: ContentStore
) -> None:
    import asyncio

    import asyncpg

    sid, ver = home(store)
    res = await editor.set_text(sid, "ru", "Текст", None, expected_version=ver, actor=None)
    other = await asyncpg.connect(db.pg_dsn)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("select pg_advisory_xact_lock(hashtext($1))", "content_undo:" + res.batch_id)
        task = asyncio.create_task(editor.undo(res.batch_id, actor=None))
        await asyncio.sleep(0.2)
        assert not task.done()  # another undo of the same batch is in progress
        await tx.rollback()
        await asyncio.wait_for(task, 5)
    finally:
        await other.close()
    with pytest.raises(EditError, match="уже отменено"):
        await editor.undo(res.batch_id, actor=None)
