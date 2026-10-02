"""Default banner: a packaged placeholder picture on every screen until the owner takes it off.

New installation → every system screen shows it; an existing installation gets it once (flag in
``config_meta``); a long text without ``PUBLIC_URL`` stays without it; «убрать со всех» / «вернуть» never
touch the owner's pictures and «↩️ Отменить» brings everything back.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from svbg.content import defaults
from svbg.content.banner import (
    ASSET,
    CAPTION_LIMIT,
    META_KEY,
    asset_bytes,
    banner_mode,
    banner_sha256,
    ensure_banner_media,
    is_banner,
)
from svbg.content.editing import BANNER_REMOVED, BANNER_SEEDED, ContentEditor, EditError
from svbg.content.media import MediaLibrary, prepare_media
from svbg.content.store import ContentStore
from tests.dbkit import CountingDatabase

SYSTEM_CODES = [s.code for s in defaults.SYSTEM_SCREENS]


def _jpeg(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _banner_codes(store: ContentStore) -> set[str]:
    snap = store.snapshot
    return {str(e.code or e.id) for e in snap.by_id.values() if is_banner(snap.get_media(e.screen.media_id))}


async def _versions(db: CountingDatabase) -> dict[str, int]:
    rows = await db.raw("select coalesce(code, id::text) as k, version from screens")
    return {r["k"]: r["version"] for r in rows}


async def _audit_count(db: CountingDatabase) -> int:
    return int((await db.raw("select count(*) as n from content_audit"))[0]["n"])


async def _flag(db: CountingDatabase) -> dict | None:
    rows = await db.raw("select value from config_meta where key = $1", META_KEY)
    return rows[0]["value"] if rows else None


def _store(db: CountingDatabase, root: Path, *, preview: bool = False) -> ContentStore:
    return ContentStore(db, media_root=root, preview_available=lambda: preview)


# ---------------------------------------------------------------- the asset


def test_asset_ships_in_the_package_and_is_stored_byte_for_byte() -> None:
    import svbg

    assert ASSET.is_file()
    assert ASSET.is_relative_to(Path(svbg.__file__).resolve().parent)  # inside the package (wheel / image)
    data = asset_bytes()
    assert data is not None
    prepared = prepare_media(data, "photo")
    # kept as is: the stored file has the asset's hash — that is how the banner is recognised
    assert not prepared.reencoded
    assert prepared.sha256 == banner_sha256() == hashlib.sha256(data).hexdigest()
    assert (prepared.width, prepared.height) == (2000, 1027)


def test_mode_attach_when_every_language_fits_a_caption() -> None:
    short = {"ru": {"text": "я" * CAPTION_LIMIT}, "en": {"text": "ok"}}
    long = {"ru": {"text": "ok"}, "en": {"text": "😀" * (CAPTION_LIMIT // 2 + 1)}}  # UTF-16: 2 units each
    assert banner_mode(short, preview_ok=False) == "attach"
    assert banner_mode(long, preview_ok=True) == "preview"
    assert banner_mode(long, preview_ok=False) is None


# ---------------------------------------------------------------- installation


async def test_new_installation_shows_the_banner_on_every_system_screen(
    db: CountingDatabase, tmp_path: Path
) -> None:
    root = tmp_path / "media"
    store = _store(db, root)
    snap = await store.load()
    assert snap.problems == ()
    assert _banner_codes(store) == set(SYSTEM_CODES)
    for code in SYSTEM_CODES:
        entry = store.get_screen(code)
        assert entry is not None and entry.screen.media_mode == "attach", code
    media_rows = await db.raw("select id, sha256, path, kind from media")
    assert len(media_rows) == 1 and media_rows[0]["sha256"] == banner_sha256()
    assert media_rows[0]["kind"] == "photo"
    assert (root / media_rows[0]["path"]).read_bytes() == asset_bytes()
    flag = await _flag(db)
    assert flag is not None and flag["media_id"] == media_rows[0]["id"] and flag["skipped"] == []

    # a restart changes nothing: no new rows, versions, audit or files
    versions, audit = await _versions(db), await _audit_count(db)
    again = _store(db, root)
    await again.load()
    assert await _versions(db) == versions
    assert await _audit_count(db) == audit
    assert len(await db.raw("select id from media")) == 1


async def test_existing_installation_gets_the_banner_once(db: CountingDatabase, tmp_path: Path) -> None:
    root = tmp_path / "media"
    old = ContentStore(db)  # the previous version: no banner
    await old.load()
    editor = ContentEditor(db, old)
    own = await MediaLibrary(db, root).add(_jpeg(), "photo")
    home = old.get_screen("home")
    assert home is not None
    await editor.set_media(home.id, own.media.id, expected_version=home.screen.version, actor=None)
    custom = await editor.create_screen("Свой экран", code="my_page", actor=None)
    before = await _versions(db)

    store = _store(db, root)
    await store.load()
    assert _banner_codes(store) == set(SYSTEM_CODES) - {"home"}
    entry = store.get_screen("home")
    assert entry is not None and entry.screen.media_id == own.media.id  # the owner's picture stays
    mine = store.get_screen("my_page")
    assert mine is not None and mine.screen.media_id is None  # own screens of the past are not touched
    after = await _versions(db)
    for code in SYSTEM_CODES:
        assert after[code] == before[code] + (0 if code == "home" else 1), code
    notes = await db.raw(
        "select count(*) as n from content_audit where new ->> 'summary' = $1", BANNER_SEEDED
    )
    assert notes[0]["n"] == len(SYSTEM_CODES) - 1
    flag = await _flag(db)
    assert flag is not None and flag["screens"] == len(SYSTEM_CODES) - 1

    # a second run changes nothing — not even a screen the owner cleared since
    error = store.get_screen("error")
    assert error is not None
    await ContentEditor(db, store).set_media(
        error.id, None, expected_version=error.screen.version, actor=None
    )
    versions, audit = await _versions(db), await _audit_count(db)
    again = _store(db, root)
    await again.load()
    assert await _versions(db) == versions
    assert await _audit_count(db) == audit
    assert "error" not in _banner_codes(again)
    assert custom.screen_id is not None


@pytest.mark.parametrize("preview", [False, True])
async def test_long_text_gets_a_link_preview_or_no_banner(
    db: CountingDatabase, tmp_path: Path, preview: bool
) -> None:
    old = ContentStore(db)
    await old.load()
    long = "я" * (CAPTION_LIMIT + 1)
    await db.raw(
        "update screens set body = jsonb_set(body, '{ru,text}', to_jsonb($1::text)) where code = 'info'", long
    )
    store = _store(db, tmp_path / "media", preview=preview)
    await store.load()
    info = store.get_screen("info")
    assert info is not None
    flag = await _flag(db)
    assert flag is not None
    if preview:  # PUBLIC_URL set: the banner goes as a link preview above the long text
        assert is_banner(store.get_media(info.screen.media_id))
        assert info.screen.media_mode == "preview"
        assert flag["skipped"] == []
    else:  # no PUBLIC_URL: a caption cannot hold the text — the screen stays without the banner
        assert info.screen.media_id is None
        assert flag["skipped"] == ["info"]
    assert _banner_codes(store) >= set(SYSTEM_CODES) - {"info"}


async def test_banner_reinstalls_a_missing_file_and_keeps_cached_file_ids(
    db: CountingDatabase, tmp_path: Path
) -> None:
    root = tmp_path / "media"
    media_id = await ensure_banner_media(db, root)
    assert media_id is not None
    await db.raw("""update media set file_ids = '{"42": "CACHED"}'::jsonb where id = $1""", media_id)
    rel = (await db.raw("select path from media where id = $1", media_id))[0]["path"]
    (root / rel).unlink()
    assert await ensure_banner_media(db, root) == media_id
    assert (root / rel).is_file()
    row = (await db.raw("select file_ids from media where id = $1", media_id))[0]
    assert row["file_ids"] == {"42": "CACHED"}


# ---------------------------------------------------------------- the owner takes it off / puts it back


@pytest.fixture
async def banner_store(db: CountingDatabase, tmp_path: Path) -> ContentStore:
    store = _store(db, tmp_path / "media")
    await store.load()
    return store


async def test_remove_everywhere_keeps_own_pictures_and_undo_brings_it_back(
    db: CountingDatabase, tmp_path: Path, banner_store: ContentStore
) -> None:
    store = banner_store
    editor = ContentEditor(db, store)
    own = await MediaLibrary(db, tmp_path / "media").add(_jpeg(), "photo")
    info = store.get_screen("info")
    assert info is not None
    await editor.set_media(info.id, own.media.id, expected_version=info.screen.version, actor=None)
    shown = _banner_codes(store)
    assert "info" not in shown and len(shown) == len(SYSTEM_CODES) - 1

    res = await editor.remove_banner(actor=7)
    assert res.bulk and res.changed == len(shown) and res.screen_id is None
    assert _banner_codes(store) == set()  # the snapshot is reloaded: users see it on the next click
    info = store.get_screen("info")
    assert info is not None and info.screen.media_id == own.media.id  # the owner's picture is untouched
    rows = await db.raw("select entity, new from content_audit where batch_id = $1", res.batch_id)
    assert sum(r["entity"] == "screen" for r in rows) == len(shown)
    assert {r["new"]["summary"] for r in rows if r["entity"] == "note"} == {BANNER_REMOVED}
    history = await editor.history(store.get_screen("home").id)  # type: ignore[union-attr]
    assert history[0].summary == BANNER_REMOVED
    with pytest.raises(EditError, match="ни на одном"):
        await editor.remove_banner(actor=7)

    undo = await editor.undo(res.batch_id, actor=7)
    assert undo.bulk and undo.changed == len(shown)
    assert _banner_codes(store) == shown
    info = store.get_screen("info")
    assert info is not None and info.screen.media_id == own.media.id
    with pytest.raises(EditError, match="уже отменено"):
        await editor.undo(res.batch_id, actor=7)


async def test_restore_fills_only_screens_without_a_picture(
    db: CountingDatabase, tmp_path: Path, banner_store: ContentStore
) -> None:
    store = banner_store
    editor = ContentEditor(db, store)
    removed = await editor.remove_banner(actor=None)
    own = await MediaLibrary(db, tmp_path / "media").add(_jpeg((10, 10, 200)), "photo")
    home = store.get_screen("home")
    assert home is not None
    await editor.set_media(home.id, own.media.id, expected_version=home.screen.version, actor=None)
    created = await editor.create_screen("Акция", code="promo_may", actor=None)
    assert created.screen_id is not None
    new_screen = store.get_screen(created.screen_id)
    assert new_screen is not None and new_screen.screen.media_id is None  # banner is off: none for new ones

    media_id = await ensure_banner_media(db, tmp_path / "media")
    assert media_id is not None
    res = await editor.restore_banner(media_id, actor=None)
    assert res.bulk and res.changed == len(SYSTEM_CODES)  # every system screen but home + the own one
    assert _banner_codes(store) == (set(SYSTEM_CODES) - {"home"}) | {"promo_may"}
    home = store.get_screen("home")
    assert home is not None and home.screen.media_id == own.media.id
    with pytest.raises(EditError, match="некуда"):
        await editor.restore_banner(media_id, actor=None)
    # the banner is on again: a new own screen gets it by default
    third = await editor.create_screen("Ещё экран", actor=None)
    assert third.screen_id is not None
    assert is_banner(store.get_media(store.get_screen(third.screen_id).screen.media_id))  # type: ignore[union-attr]

    await editor.undo(res.batch_id, actor=None)
    assert _banner_codes(store) == {str(third.screen_id)}
    with pytest.raises(EditError, match="уже отменено"):
        await editor.undo(res.batch_id, actor=None)
    assert removed.changed == len(SYSTEM_CODES)


async def test_new_own_screen_gets_the_banner_by_default(
    db: CountingDatabase, banner_store: ContentStore
) -> None:
    editor = ContentEditor(db, banner_store, preview_available=lambda: False)
    res = await editor.create_screen("Новый экран", code="fresh", actor=None)
    entry = banner_store.get_screen("fresh")
    assert entry is not None and entry.screen.media_mode == "attach"
    assert is_banner(banner_store.get_media(entry.screen.media_id))
    # «🗑 Убрать картинку» works for the banner like for any picture, and the undo puts it back
    rm = await editor.set_media(entry.id, None, expected_version=entry.screen.version, actor=None)
    entry = banner_store.get_screen("fresh")
    assert entry is not None and entry.screen.media_id is None
    await editor.undo(rm.batch_id, actor=None)
    entry = banner_store.get_screen("fresh")
    assert entry is not None and is_banner(banner_store.get_media(entry.screen.media_id))
    assert res.screen_id == entry.id


@pytest.mark.parametrize("preview", [False, True])
async def test_banner_never_blocks_a_long_text(
    db: CountingDatabase, banner_store: ContentStore, preview: bool
) -> None:
    editor = ContentEditor(db, banner_store, preview_available=lambda: preview)
    home = banner_store.get_screen("home")
    assert home is not None
    long = "я" * (CAPTION_LIMIT + 10)
    res = await editor.set_text(home.id, "ru", long, None, expected_version=home.screen.version, actor=None)
    entry = banner_store.get_screen("home")
    assert entry is not None and entry.text("ru").text == long
    assert len(res.warnings) == 1
    if preview:
        assert is_banner(banner_store.get_media(entry.screen.media_id))
        assert entry.screen.media_mode == "preview" and "превью-ссылкой" in res.warnings[0]
    else:
        assert entry.screen.media_id is None and "убрана" in res.warnings[0]


async def test_owner_picture_still_limits_the_caption(
    db: CountingDatabase, tmp_path: Path, banner_store: ContentStore
) -> None:
    editor = ContentEditor(db, banner_store, preview_available=lambda: False)
    own = await MediaLibrary(db, tmp_path / "media").add(_jpeg(), "photo")
    home = banner_store.get_screen("home")
    assert home is not None
    res = await editor.set_media(home.id, own.media.id, expected_version=home.screen.version, actor=None)
    with pytest.raises(EditError, match="подпись"):
        await editor.set_text(
            home.id, "ru", "я" * (CAPTION_LIMIT + 1), None, expected_version=res.version, actor=None
        )


async def test_bulk_undo_is_refused_after_a_screen_changed(
    db: CountingDatabase, banner_store: ContentStore
) -> None:
    editor = ContentEditor(db, banner_store)
    res = await editor.remove_banner(actor=None)
    home = banner_store.get_screen("home")
    assert home is not None
    await editor.set_text(
        home.id, "ru", "Новый текст", None, expected_version=home.screen.version, actor=None
    )
    with pytest.raises(EditError, match="«home» уже меняли"):
        await editor.undo(res.batch_id, actor=None)
    assert _banner_codes(banner_store) == set()  # nothing was half-undone


async def test_restore_skips_a_long_text_without_public_url_and_says_so(
    db: CountingDatabase, tmp_path: Path, banner_store: ContentStore
) -> None:
    editor = ContentEditor(db, banner_store, preview_available=lambda: False)
    await editor.remove_banner(actor=None)
    long = "я" * (CAPTION_LIMIT + 1)
    await db.raw(
        "update screens set body = jsonb_set(body, '{ru,text}', to_jsonb($1::text)) where code = 'info'", long
    )
    await banner_store.reload()
    media_id = await ensure_banner_media(db, tmp_path / "media")
    assert media_id is not None
    res = await editor.restore_banner(media_id, actor=None)
    assert res.changed == len(SYSTEM_CODES) - 1
    assert "info" not in _banner_codes(banner_store)
    assert len(res.warnings) == 1 and res.warnings[0].startswith("Без заглушки остались info")
    assert "PUBLIC_URL" in res.warnings[0]
