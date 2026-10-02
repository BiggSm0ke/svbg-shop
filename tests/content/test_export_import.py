from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from PIL import Image

from svbg.content.export_import import (
    ArchiveLimits,
    ContentArchiveError,
    ContentTransfer,
    Section,
    check_no_secrets,
    read_archive,
    register_section,
    sections,
)
from svbg.content.media import MediaLibrary, MediaLimits
from svbg.content.store import ContentStore
from tests.dbkit import CountingDatabase, open_db
from tests.pgcluster import PgCluster


def _png(color: tuple[int, int, int] = (0, 120, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def other_db(pg_cluster: PgCluster) -> AsyncIterator[CountingDatabase]:
    """A second clean install (its own database) for transfer tests."""
    name = f"t_io_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        async with open_db(pg_cluster.dsn(name)) as database:
            yield database
    finally:
        admin = await asyncpg.connect(pg_cluster.dsn("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _populate(db: CountingDatabase, root: Path) -> dict[str, Any]:
    """Owner-edited content: home text + photo, a code-less custom screen and a button to it, plans."""
    store = ContentStore(db)
    await store.load()
    stored = await MediaLibrary(db, root).add(_png(), "photo", bot_id=111, file_id="file-of-bot-111")
    await db.raw(
        'update media set file_ids = \'{"111": "file-of-bot-111"}\'::jsonb where id = $1', stored.media.id
    )
    home = store.get_screen("home")
    assert home is not None
    body = {
        "ru": {
            "text": "Привет ⭐ мир",
            "entities": [
                {"type": "custom_emoji", "offset": 7, "length": 1, "custom_emoji_id": "5368324170671202286"}
            ],
        }
    }
    await db.raw(
        "update screens set body = $1, media_id = $2, media_mode = 'preview', updated_by = 777 where id = $3",
        body,
        stored.media.id,
        home.id,
    )
    [custom] = await db.raw(
        "insert into screens (kind, title, body) values ('custom', $1, $2) returning id",
        {"ru": "Акция"},
        {"ru": {"text": "Скидка 20%"}},
    )
    await db.raw(
        "insert into screen_buttons (screen_id, row, sort, label, action, style, icon_custom_emoji_id,"
        " visible_if) values ($1, 0, 0, $2, $3, 'danger', '5368324170671202286', $4)",
        custom["id"],
        {"ru": "Назад"},
        {"type": "screen", "target": "home"},
        {"sub": "expired"},
    )
    await db.raw(
        "insert into screen_buttons (screen_id, row, sort, label, action) values ($1, 5, 0, $2, $3)",
        home.id,
        {"ru": "🔥 Акция"},
        {"type": "screen", "target": str(custom["id"])},
    )
    [plan] = await db.raw(
        "insert into plans (code, name, enabled, squads, is_trial, traffic_bytes, device_limit, sort)"
        " values ('month', $1, true, $2, false, 107374182400, 3, 1) returning id",
        {"ru": "Месяц"},
        ["sq-1"],
    )
    await db.raw(
        "insert into plan_prices (plan_id, days, currency, amount_minor, highlight)"
        " values ($1, 30, 'RUB', 19900, true), ($1, 90, 'RUB', 49900, false)",
        plan["id"],
    )
    await db.raw(
        "insert into plans (code, name, enabled, squads, is_trial) values ('trial', $1, true, $2, true)",
        {"ru": "Пробный"},
        ["sq-1"],
    )
    await db.raw(
        "insert into locations (squad_uuid, title, flag, sort, panel_name)"
        " values ('sq-1', $1, '🇩🇪', 1, 'DE')",
        {"ru": "Германия"},
    )
    return {"custom_id": int(custom["id"]), "media_sha": stored.media.sha256}


async def _content_view(db: CountingDatabase) -> dict[str, Any]:
    """Comparable content: screens by code (custom ones by title), screen targets by code/title."""
    screens = await db.raw(
        "select s.id, s.code, s.kind, s.title, s.body, s.media_mode, s.enabled, m.sha256"
        " from screens s left join media m on m.id = s.media_id"
    )
    key = {r["id"]: r["code"] or f"custom:{r['title'].get('ru')}" for r in screens}
    buttons = await db.raw(
        "select screen_id, system_key, row, sort, label, icon_custom_emoji_id, style, action, visible_if,"
        " enabled from screen_buttons order by screen_id, row, sort, id"
    )
    out: dict[str, Any] = {}
    for r in screens:
        btns = []
        for b in buttons:
            if b["screen_id"] != r["id"]:
                continue
            action = dict(b["action"])
            if action.get("type") == "screen" and str(action["target"]).isdigit():
                action["target"] = key[int(action["target"])]
            btns.append({**{k: b[k] for k in dict(b) if k != "screen_id"}, "action": action})
        out[key[r["id"]]] = {
            "kind": r["kind"],
            "title": r["title"],
            "body": r["body"],
            "media": r["sha256"],
            "media_mode": r["media_mode"],
            "enabled": r["enabled"],
            "buttons": btns,
        }
    plans = await db.raw(
        "select p.code, p.name, p.enabled, p.is_trial, p.squads, p.traffic_bytes, p.device_limit, p.sort,"
        " coalesce(json_agg(json_build_object('d', pp.days, 'c', pp.currency, 'a', pp.amount_minor,"
        " 'h', pp.highlight) order by pp.days) filter (where pp.id is not null), '[]') as prices"
        " from plans p left join plan_prices pp on pp.plan_id = p.id group by p.id order by p.code"
    )
    locs = await db.raw("select squad_uuid, title, flag, sort from locations order by squad_uuid")
    return {"screens": out, "plans": [dict(p) for p in plans], "locations": [dict(r) for r in locs]}


def _transfer(
    db: CountingDatabase, base: Path, store: ContentStore | None = None, **kw: Any
) -> ContentTransfer:
    hooks = [store.reload] if store is not None else []
    return ContentTransfer(db, base / "media", base / "content-exports", on_applied=hooks, **kw)


def _doc(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read("content.json"))


def _rezip(
    src: Path,
    dest: Path,
    *,
    doc: dict[str, Any] | None = None,
    drop: str = "",
    extra: dict[str, bytes] | None = None,
) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for info in zin.infolist():
            if info.filename == drop:
                continue
            data = zin.read(info)
            if info.filename == "content.json" and doc is not None:
                data = json.dumps(doc).encode()
            zout.writestr(info.filename, data)
        for name, data in (extra or {}).items():
            zout.writestr(name, data)
    return dest


def _scalars(node: Any) -> list[Any]:
    """Every scalar value of a JSON tree (timestamps may contain "777" as a substring)."""
    if isinstance(node, dict):
        return [v for x in node.values() for v in _scalars(x)]
    if isinstance(node, list):
        return [v for x in node for v in _scalars(x)]
    return [node]


# ---------------------------------------------------------------- export → import into another install


async def test_round_trip_into_clean_install_with_another_bot(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    src_dir, dst_dir = tmp_path / "a", tmp_path / "b"
    info = await _populate(db, src_dir / "media")
    exported = await _transfer(db, src_dir).export()
    assert exported.sections == ("screens", "plans") and exported.media == 1
    raw = zipfile.ZipFile(exported.path).read("content.json").decode()
    assert "file-of-bot-111" not in raw and "file_ids" not in raw  # no bot ids
    assert 777 not in _scalars(json.loads(raw)) and "777" not in _scalars(json.loads(raw))  # no editor
    doc = _doc(exported.path)
    assert doc["format"] == "svbg-content" and doc["schema_version"] == 1

    target_store = ContentStore(other_db)
    await target_store.load()
    await other_db.raw("insert into screens (code, kind, body) values ('old_custom', 'custom', '{}')")
    result = await _transfer(other_db, dst_dir, target_store).import_archive(exported.path, actor=5)

    assert result.sections == ("screens", "plans")
    assert result.stats["screens"]["deleted"] == 1  # the target's own custom screen is replaced
    assert await _content_view(other_db) == await _content_view(db)
    # the media file is in the target's media directory, verified by hash; no file_id for the new bot yet
    [m] = await other_db.raw("select path, file_ids, sha256 from media")
    assert m["sha256"] == info["media_sha"] and m["file_ids"] == {}
    assert (dst_dir / "media" / m["path"]).is_file()
    # the store was reloaded by the hook: the new content is visible on the next click
    home = target_store.get_screen("home")
    assert home is not None and home.text("ru").text == "Привет ⭐ мир"
    assert target_store.get_media(home.screen.media_id) is not None
    [audit] = await other_db.raw(
        "select entity, actor, new from content_audit where batch_id = $1", result.batch_id
    )
    assert (
        audit["entity"] == "import"
        and audit["actor"] == 5
        and audit["new"]["sections"] == ["plans", "screens"]
    )
    assert result.backup.is_file() and "Контент загружен" in result.summary()


async def test_reimport_into_same_install_keeps_custom_screen_ids(
    db: CountingDatabase, tmp_path: Path
) -> None:
    info = await _populate(db, tmp_path / "media")
    transfer = _transfer(db, tmp_path)
    exported = await transfer.export()
    await transfer.import_archive(exported.path)
    rows = await db.raw("select id from screens where kind = 'custom'")
    assert [r["id"] for r in rows] == [info["custom_id"]]
    [btn] = await db.raw("select action from screen_buttons where label->>'ru' = '🔥 Акция'")
    assert btn["action"] == {"type": "screen", "target": str(info["custom_id"])}
    [s] = await db.raw("select version from screens where code = 'home'")
    assert s["version"] == 2  # CAS: concurrent editors holding version 1 will be refused


async def test_undo_restores_content_before_import(db: CountingDatabase, tmp_path: Path) -> None:
    store = ContentStore(db)
    await store.load()
    transfer = _transfer(db, tmp_path, store)
    clean = await transfer.export(tmp_path / "clean.zip")
    await _populate(db, tmp_path / "media")
    before = await _content_view(db)

    result = await transfer.import_archive(clean.path, actor=1)
    assert await db.raw("select id from screens where kind = 'custom'") == []
    [trial] = await db.raw("select enabled, is_trial from plans where code = 'month'")
    assert trial["enabled"] is False  # plans absent from the archive are switched off, not deleted

    undone = await transfer.undo(result.batch_id, actor=1)
    assert await _content_view(db) == before
    home = store.get_screen("home")
    assert home is not None and home.text("ru").text == "Привет ⭐ мир"
    # undo is itself undoable
    await transfer.undo(undone.batch_id)
    assert await db.raw("select id from screens where kind = 'custom'") == []


async def test_undo_unknown_or_bad_batch(db: CountingDatabase, tmp_path: Path) -> None:
    transfer = _transfer(db, tmp_path)
    for batch in ("0" * 32, "../../etc", "x"):
        with pytest.raises(ContentArchiveError):
            await transfer.undo(batch)


async def test_dangling_screen_reference_is_switched_off(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(only=["screens"])
    doc = _doc(exported.path)
    doc["sections"]["screens"]["screens"] = [s for s in doc["sections"]["screens"]["screens"] if s["code"]]
    archive = _rezip(exported.path, tmp_path / "dangling.zip", doc=doc)
    await ContentStore(other_db).load()
    result = await _transfer(other_db, tmp_path / "b").import_archive(archive)
    [btn] = await other_db.raw("select enabled from screen_buttons where label->>'ru' = '🔥 Акция'")
    assert btn["enabled"] is False
    assert any("которого нет" in w for w in result.warnings)
    assert result.stats["screens"]["buttons_off"] == 1
    assert "plans" not in result.stats  # sections absent from the archive are left as they are


async def test_missing_system_screens_and_buttons_are_reseeded(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    await ContentStore(db).load()
    exported = await _transfer(db, tmp_path).export(only=["screens"])
    doc = _doc(exported.path)
    screens = [s for s in doc["sections"]["screens"]["screens"] if s["code"] != "error"]
    for s in screens:
        s["buttons"] = []
    doc["sections"]["screens"]["screens"] = screens
    archive = _rezip(exported.path, tmp_path / "x.zip", doc=doc)
    await ContentStore(other_db).load()
    result = await _transfer(other_db, tmp_path / "b").import_archive(archive)
    assert result.stats["screens"]["reseeded"] > 0
    assert await other_db.raw("select id from screens where code = 'error'")
    assert await other_db.raw("select id from screen_buttons where system_key = 'home'")


# ---------------------------------------------------------------- refusals: nothing changes


async def _assert_untouched(db: CountingDatabase, before: dict[str, Any], exports: Path) -> None:
    assert await _content_view(db) == before
    assert await db.raw("select id from content_audit") == []
    assert not exports.exists() or not any(p.name.startswith("backup-") for p in exports.iterdir())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].append(
                {"label": {"ru": "x"}, "action": "nope:1"}
            ),
            "кнопка",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].append(
                {"label": {"ru": "x"}, "action": "url:javascript:alert(1)"}
            ),
            "URL",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].append(
                {"label": {"ru": "x"}, "action": "screen:home", "visible_if": {"bogus": 1}}
            ),
            "кнопка",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].append(
                {"label": {"ru": "x"}, "action": "screen:home", "style": "pink"}
            ),
            "цвет",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].append(
                {"label": {"ru": "x"}, "action": "screen:home", "icon_custom_emoji_id": "abc"}
            ),
            "emoji",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"][0]["buttons"].extend(
                [{"label": {"ru": "x"}, "action": "screen:home", "row": 4}] * 9
            ),
            "больше 8",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"].append(
                {"id": 999, "code": "sys", "kind": "custom"}
            ),
            "зарезервирован",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"].append(
                {"id": 998, "code": "Bad Code", "kind": "custom"}
            ),
            "код",
        ),
        (
            lambda d: d["sections"]["screens"]["screens"].append(
                dict(d["sections"]["screens"]["screens"][0])
            ),
            "повторяется",
        ),
        (
            lambda d: d["sections"]["plans"]["plans"].append(
                {**d["sections"]["plans"]["plans"][0], "code": "x2", "is_trial": True}
            ),
            "пробный",
        ),
        (
            lambda d: d["sections"]["plans"]["plans"][0]["prices"].append(
                {"days": 0, "currency": "RUB", "amount_minor": -1}
            ),
            "цена",
        ),
        (lambda d: d["sections"]["plans"]["plans"][0].update(enabled=True, squads=[]), "сквад"),
        (lambda d: d.update(schema_version=99), "новой версией"),
        (lambda d: d.update(format="other"), "не архив"),
        (lambda d: d.update(sections={}), "нет разделов"),
        (lambda d: d["media"].append({"sha256": "zz", "kind": "photo"}), "некорректная"),
    ],
)
async def test_invalid_archive_changes_nothing(
    db: CountingDatabase, tmp_path: Path, mutate: Any, message: str
) -> None:
    await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(tmp_path / "good.zip")
    doc = _doc(exported.path)
    mutate(doc)
    bad = _rezip(exported.path, tmp_path / "bad.zip", doc=doc)
    before = await _content_view(db)
    transfer = _transfer(db, tmp_path / "dst")
    with pytest.raises(ContentArchiveError) as exc:
        await transfer.import_archive(bad)
    assert message.lower() in exc.value.text().lower()
    await _assert_untouched(db, before, tmp_path / "dst" / "content-exports")


async def test_corrupted_media_changes_nothing(db: CountingDatabase, tmp_path: Path) -> None:
    info = await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(tmp_path / "good.zip")
    name = next(n for n in zipfile.ZipFile(exported.path).namelist() if n.startswith("media/"))
    bad = _rezip(exported.path, tmp_path / "bad.zip", drop=name, extra={name: _png((1, 2, 3))})
    before = await _content_view(db)
    dst = tmp_path / "dst"
    with pytest.raises(ContentArchiveError) as exc:
        await _transfer(db, dst).import_archive(bad)
    assert "контрольная сумма" in exc.value.text()
    assert await _content_view(db) == before
    assert await db.raw("select id from content_audit") == []
    assert not (dst / "media" / info["media_sha"][:2]).exists() or not any((dst / "media").rglob("*.png"))


async def test_media_of_wrong_type_is_refused(db: CountingDatabase, tmp_path: Path) -> None:
    await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(tmp_path / "good.zip")
    doc = _doc(exported.path)
    payload = b"MZ\x90\x00 definitely not a picture"
    sha = hashlib.sha256(payload).hexdigest()
    doc["media"].append({"sha256": sha, "kind": "photo"})
    bad = _rezip(exported.path, tmp_path / "bad.zip", doc=doc, extra={f"media/{sha}.jpg": payload})
    with pytest.raises(ContentArchiveError) as exc:
        await _transfer(db, tmp_path / "dst").import_archive(bad)
    assert "тип файла" in exc.value.text()


async def test_missing_media_file_imports_screen_without_media(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(tmp_path / "good.zip")
    name = next(n for n in zipfile.ZipFile(exported.path).namelist() if n.startswith("media/"))
    archive = _rezip(exported.path, tmp_path / "nomedia.zip", drop=name)
    await ContentStore(other_db).load()
    result = await _transfer(other_db, tmp_path / "dst").import_archive(archive)
    [home] = await other_db.raw("select media_id from screens where code = 'home'")
    assert home["media_id"] is None
    assert any("медиа" in w for w in result.warnings)


def test_read_archive_refuses_garbage_and_bombs(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(ContentArchiveError, match="zip"):
        read_archive(junk)
    with pytest.raises(ContentArchiveError, match="не найден"):
        read_archive(tmp_path / "absent.zip")
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(ContentArchiveError, match=r"content\.json"):
        read_archive(empty)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", b"{" + b" " * (8 * 1024 * 1024) + b"}")
    with pytest.raises(ContentArchiveError, match="сжат"):
        read_archive(bomb)
    with pytest.raises(ContentArchiveError, match="больше"):
        read_archive(bomb, ArchiveLimits(max_archive_bytes=10))
    many = tmp_path / "many.zip"
    with zipfile.ZipFile(many, "w") as zf:
        for i in range(5):
            zf.writestr(f"f{i}", b"x")
    with pytest.raises(ContentArchiveError, match="много"):
        read_archive(many, ArchiveLimits(max_entries=3))
    notjson = tmp_path / "notjson.zip"
    with zipfile.ZipFile(notjson, "w") as zf:
        zf.writestr("content.json", b"\xff\xfe garbage")
    with pytest.raises(ContentArchiveError, match="JSON"):
        read_archive(notjson)


async def test_backups_are_pruned(db: CountingDatabase, tmp_path: Path) -> None:
    await ContentStore(db).load()
    transfer = _transfer(db, tmp_path, keep_backups=lambda: 2)
    exported = await transfer.export(tmp_path / "x.zip", only=["screens"])
    for _ in range(4):
        await transfer.import_archive(exported.path)
    backups = sorted(p.name for p in (tmp_path / "content-exports").iterdir() if p.name.startswith("backup-"))
    assert len(backups) == 2


async def test_reload_hook_failure_does_not_hide_the_import(db: CountingDatabase, tmp_path: Path) -> None:
    await ContentStore(db).load()

    async def broken() -> None:
        raise RuntimeError("store down")

    transfer = _transfer(db, tmp_path)
    transfer.add_reload_hook(broken)
    exported = await transfer.export(tmp_path / "x.zip", only=["screens"])
    result = await transfer.import_archive(exported.path)
    assert result.batch_id


# ---------------------------------------------------------------- sections and secrets


def test_check_no_secrets() -> None:
    check_no_secrets({"screens": [{"label": {"ru": "x"}, "action": {"type": "copy", "text": "token"}}]})
    for bad in ({"api_key": "x"}, {"a": [{"file_ids": {}}]}, {"b": {"WEBHOOK_SECRET": 1}}, {"password": "p"}):
        with pytest.raises(ValueError, match="secret-like"):
            check_no_secrets(bad)


async def test_registered_section_round_trip_and_unknown_section_warning(
    db: CountingDatabase, tmp_path: Path
) -> None:
    store: dict[str, Any] = {"value": {"greeting": "hi"}}

    async def export(_conn: Any, _ctx: Any) -> Any:
        return store["value"]

    def validate(data: Any, ctx: Any) -> Any:
        if not isinstance(data, dict):
            ctx.problem("demo", "bad")
        return data

    async def apply(_conn: Any, data: Any, _ctx: Any) -> dict[str, int]:
        store["value"] = data
        return {"items": 1}

    register_section(Section("zz_demo", "Демо", export, validate, apply, order=999))
    try:
        with pytest.raises(ValueError, match="already"):
            register_section(Section("zz_demo", "Демо", export, validate, apply))
        assert list(sections())[-1] == "zz_demo"
        await ContentStore(db).load()
        transfer = _transfer(db, tmp_path)
        exported = await transfer.export(tmp_path / "x.zip", only=["zz_demo"])
        store["value"] = {"greeting": "changed"}
        result = await transfer.import_archive(exported.path)
        assert store["value"] == {"greeting": "hi"} and result.stats == {"zz_demo": {"items": 1}}
        doc = _doc(exported.path)
        doc["sections"]["from_the_future"] = {}
        archive = _rezip(exported.path, tmp_path / "future.zip", doc=doc)
        result = await transfer.import_archive(archive)
        assert any("from_the_future" in w for w in result.warnings)
    finally:
        from svbg.content import export_import

        export_import._SECTIONS.pop("zz_demo", None)
    with pytest.raises(ValueError, match="unknown"):
        await _transfer(db, tmp_path).export(only=["zz_demo"])


# ---------------------------------------------------------------- imported media = uploads; export off-loop

_SECRET = b"55.7558N 37.6173E Ivan Petrov"


def _dirty_jpeg() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = _SECRET.decode()  # Make
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (9, 9, 9)).save(
        buf, "JPEG", exif=exif.tobytes(), xmp=b"<x>" + _SECRET + b"</x>"
    )
    return buf.getvalue()


def _with_media(src: Path, dest: Path, payload: bytes, kind: str, *, on_home: bool) -> tuple[Path, str]:
    doc = _doc(src)
    sha = hashlib.sha256(payload).hexdigest()
    doc["media"].append({"sha256": sha, "kind": kind, "width": 4000, "height": 3000})
    if on_home:
        home = next(s for s in doc["sections"]["screens"]["screens"] if s["code"] == "home")
        home["media"] = sha
    return _rezip(src, dest, doc=doc, extra={f"media/{sha}.bin": payload}), sha


async def test_imported_photo_is_cleaned_like_an_upload(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    await _populate(db, tmp_path / "a" / "media")
    exported = await _transfer(db, tmp_path / "a").export(tmp_path / "good.zip")
    dirty = _dirty_jpeg()
    archive, dirty_sha = _with_media(exported.path, tmp_path / "dirty.zip", dirty, "photo", on_home=True)
    store = ContentStore(other_db)
    await store.load()
    dst = tmp_path / "b"
    await _transfer(other_db, dst, store).import_archive(archive)

    [row] = await other_db.raw(
        "select m.sha256, m.path, m.width, m.height, m.size from screens s join media m on m.id = s.media_id"
        " where s.code = 'home'"
    )
    assert row["sha256"] != dirty_sha  # stored under the hash of the cleaned file
    stored = (dst / "media" / row["path"]).read_bytes()
    assert _SECRET not in stored and hashlib.sha256(stored).hexdigest() == row["sha256"]
    assert (row["width"], row["height"], row["size"]) == (
        40,
        30,
        len(stored),
    )  # real, not the archive's hints
    assert not any(dirty_sha in p.name for p in (dst / "media").rglob("*"))
    assert not list((dst / "media").rglob("*.tmp"))
    home = store.get_screen("home")
    assert home is not None and store.get_media(home.screen.media_id) is not None


async def test_imported_photo_over_the_pixel_limit_changes_nothing(
    db: CountingDatabase, tmp_path: Path
) -> None:
    await _populate(db, tmp_path / "media")
    exported = await _transfer(db, tmp_path).export(tmp_path / "good.zip")
    buf = io.BytesIO()
    Image.new("RGB", (300, 300)).save(buf, "PNG")
    archive, _ = _with_media(exported.path, tmp_path / "big.zip", buf.getvalue(), "photo", on_home=False)
    before = await _content_view(db)
    limits = ArchiveLimits(media=MediaLimits(photo_decode_max_pixels=10_000))
    with pytest.raises(ContentArchiveError) as exc:
        await _transfer(db, tmp_path / "dst", limits=limits).import_archive(archive)
    assert "пиксел" in exc.value.text()
    await _assert_untouched(db, before, tmp_path / "dst" / "content-exports")


async def test_imported_video_metadata_is_blanked(
    db: CountingDatabase, other_db: CountingDatabase, tmp_path: Path
) -> None:
    from tests.content.test_media_privacy import SECRET, _mp4

    await _populate(db, tmp_path / "a" / "media")
    exported = await _transfer(db, tmp_path / "a").export(tmp_path / "good.zip")
    video = _mp4(dirty=True)
    archive, sha = _with_media(exported.path, tmp_path / "v.zip", video, "video", on_home=False)
    await ContentStore(other_db).load()
    dst = tmp_path / "b"
    await _transfer(other_db, dst).import_archive(archive)
    [row] = await other_db.raw("select sha256, path, size, mime from media where kind = 'video'")
    data = (dst / "media" / row["path"]).read_bytes()
    assert SECRET not in data and len(data) == len(video) == row["size"] and row["sha256"] != sha
    assert row["mime"] == "video/mp4"


async def test_reimporting_own_export_keeps_media_hashes(db: CountingDatabase, tmp_path: Path) -> None:
    info = await _populate(db, tmp_path / "media")
    transfer = _transfer(db, tmp_path)
    exported = await transfer.export(tmp_path / "x.zip")
    await transfer.import_archive(exported.path)
    rows = await db.raw("select sha256 from media")
    assert [r["sha256"] for r in rows] == [info["media_sha"]]  # our own clean JPEG is not re-encoded


async def test_auto_named_exports_are_pruned_explicit_ones_kept(db: CountingDatabase, tmp_path: Path) -> None:
    await ContentStore(db).load()
    transfer = _transfer(db, tmp_path, keep_exports=2)
    exports = tmp_path / "content-exports"
    mine = await transfer.export(exports / "mine.zip", only=["screens"])
    paths = [(await transfer.export(only=["screens"])).path for _ in range(4)]
    assert len(set(paths)) == 4  # unique names even within one second
    left = sorted(p.name for p in exports.iterdir())
    assert len([n for n in left if n.startswith("content-")]) == 2
    assert paths[-1].is_file() and mine.path.is_file()
    await transfer.import_archive(paths[-1])  # the backup taken here is not an export: not pruned by it
    await transfer.export(only=["screens"])
    assert any(p.name.startswith("backup-") for p in exports.iterdir())


async def test_export_builds_json_and_zip_off_the_event_loop(
    db: CountingDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from svbg.content import export_import

    await _populate(db, tmp_path / "media")
    seen: list[bool] = []
    real_check = export_import.check_no_secrets
    real_media_path = export_import.resolve_media_path

    def check(value: Any, path: str = "$") -> None:
        seen.append(threading.current_thread() is threading.main_thread())
        real_check(value, path)

    def media_path(root: Path, rel: str | None) -> Path | None:
        seen.append(threading.current_thread() is threading.main_thread())
        return real_media_path(root, rel)

    monkeypatch.setattr(export_import, "check_no_secrets", check)
    monkeypatch.setattr(export_import, "resolve_media_path", media_path)
    result = await _transfer(db, tmp_path).export(tmp_path / "x.zip")
    assert result.media == 1 and seen and not any(seen)


async def test_export_refuses_a_section_with_secrets(db: CountingDatabase, tmp_path: Path) -> None:
    async def export(_conn: Any, _ctx: Any) -> Any:
        return {"api_key": "sk-live"}

    def validate(data: Any, _ctx: Any) -> Any:
        return data

    async def apply(_conn: Any, _data: Any, _ctx: Any) -> dict[str, int]:
        return {}

    register_section(Section("zz_leaky", "Утечка", export, validate, apply, order=998))
    try:
        with pytest.raises(ValueError, match="secret-like"):
            await _transfer(db, tmp_path).export(tmp_path / "leak.zip", only=["zz_leaky"])
        assert not (tmp_path / "leak.zip").exists()
    finally:
        from svbg.content import export_import

        export_import._SECTIONS.pop("zz_leaky", None)
