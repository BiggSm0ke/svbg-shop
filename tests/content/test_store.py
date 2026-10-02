from __future__ import annotations

import asyncio
import time

import pytest
import sqlalchemy as sa

from svbg.content import defaults
from svbg.content.store import ContentStore, build_snapshot, seed_system_screens
from svbg.content.tables import media, screen_buttons, screens
from svbg.tg.ui.context import UserCtx
from tests.dbkit import CountingDatabase


async def _insert_screen(db: CountingDatabase, code: str | None, body: dict, **extra: object) -> int:
    async with db.tx() as conn:
        row = (
            await conn.execute(
                sa.insert(screens)
                .values(code=code, kind="custom", body=body, **extra)
                .returning(screens.c.id)
            )
        ).first()
    assert row is not None
    return int(row.id)


async def _insert_button(db: CountingDatabase, screen_id: int, **values: object) -> int:
    values.setdefault("label", {"ru": "Кнопка"})
    values.setdefault("action", {"type": "screen", "target": "home"})
    async with db.tx() as conn:
        row = (
            await conn.execute(
                sa.insert(screen_buttons).values(screen_id=screen_id, **values).returning(screen_buttons.c.id)
            )
        ).first()
    assert row is not None
    return int(row.id)


async def test_load_seeds_system_screens(db: CountingDatabase) -> None:
    store = ContentStore(db)
    snap = await store.load()
    for code in (defaults.HOME, defaults.MENU_FALLBACK, defaults.ERROR, defaults.SETTINGS_ROOT):
        entry = store.get_screen(code)
        assert entry is not None, code
        assert entry.screen.kind == "system"
    assert snap.problems == ()
    assert store.version == snap.version == 1
    home = store.get_screen("home")
    assert home is not None
    assert home.text("ru").entities[0]["type"] == "bold"
    # lookup by numeric id works for both int and str
    assert store.get_screen(home.id) is home
    assert store.get_screen(str(home.id)) is home
    assert store.get_screen("99999999999999999999") is None


async def test_seeding_is_idempotent_and_keeps_owner_edits(db: CountingDatabase) -> None:
    store = ContentStore(db)
    await store.load()
    await db.raw("update screens set body = $1::jsonb where code = 'home'", {"ru": {"text": "Моё"}})
    # owner hid the settings button and deleted the menu button of the error screen
    await db.raw("update screen_buttons set enabled = false where system_key = 'settings'")
    await db.raw(
        "delete from screen_buttons where system_key = 'home' "
        "and screen_id = (select id from screens where code = 'error')"
    )
    async with db.tx() as conn:
        added = await seed_system_screens(conn)
    assert added == 1  # only the missing system button came back
    snap = await store.reload()
    home = snap.get_screen("home")
    assert home is not None and home.text("ru").text == "Моё"
    labels = [b.label for row in home.keyboard("ru").rows for b in row]
    assert labels and not any("Настройки" in label for label in labels)  # the hidden button stays hidden
    rows = await db.raw("select count(*) as n from screens")
    assert rows[0]["n"] == len(defaults.SYSTEM_SCREENS)
    async with db.tx() as conn:
        assert await seed_system_screens(conn) == 0


async def test_keyboard_templates_per_language_and_conditions(db: CountingDatabase) -> None:
    store = ContentStore(db)
    await store.load()
    sid = await _insert_screen(db, "shop", {"ru": {"text": "Магазин"}, "en": {"text": "Shop"}})
    await _insert_button(db, sid, label={"ru": "Купить", "en": "Buy"}, row=0, sort=2, style="success")
    await _insert_button(db, sid, label={"ru": "Тарифы"}, row=0, sort=1)
    await _insert_button(db, sid, label={"ru": "Админ"}, row=1, visible_if={"role": "owner"})
    await _insert_button(db, sid, label={"ru": "Выкл"}, row=1, enabled=False)
    snap = await store.reload()
    entry = snap.get_screen("shop")
    assert entry is not None
    ru = entry.keyboard("ru")
    assert [[t.label for t in row] for row in ru.rows] == [["Тарифы", "Купить"], ["Админ"]]
    en = entry.keyboard("en")
    assert [[t.label for t in row] for row in en.rows] == [["Тарифы", "Buy"], ["Админ"]]
    assert entry.keyboard("de") is entry.keyboard("ru")  # unknown language → default
    admin_btn = ru.rows[1][0]
    assert admin_btn.condition is not None
    assert admin_btn.condition(UserCtx(1, role="owner"))
    assert not admin_btn.condition(UserCtx(1, role="admin"))


async def test_bad_rows_are_isolated(db: CountingDatabase) -> None:
    store = ContentStore(db)
    await store.load()
    sid = await _insert_screen(db, "promo", {"ru": "Промо"})
    good = await _insert_button(db, sid, label={"ru": "Ок"})
    await _insert_button(db, sid, label={"ru": "Плохое действие"}, action={"type": "rocket"})
    hidden = await _insert_button(db, sid, label={"ru": "Плохое условие"}, visible_if={"weather": "sunny"})
    await _insert_screen(
        db, "broken", {"ru": {"text": "x", "entities": [{"type": "bold", "offset": 5, "length": 9}]}}
    )
    snap = await store.reload()
    assert len(snap.problems) == 3
    assert any("broken" in p for p in snap.problems)
    entry = snap.get_screen("promo")
    assert entry is not None
    ids = [t.button.id for row in entry.keyboard("ru").rows for t in row]
    assert ids == [good, hidden]
    bad_cond = entry.keyboard("ru").rows[0][1]
    assert bad_cond.condition is not None and not bad_cond.condition(UserCtx(1, role="owner"))  # fail closed
    assert snap.get_screen("broken") is None
    assert snap.get_screen("home") is not None  # everything else still loads


async def test_reserved_codes_are_skipped() -> None:
    snap = build_snapshot([{"id": 1, "code": "sys", "kind": "custom", "body": {}}], [], [], version=1)
    assert snap.get_screen("sys") is None
    assert snap.problems


async def test_reload_swaps_atomically(db: CountingDatabase) -> None:
    store = ContentStore(db)
    old = await store.load()
    await _insert_screen(db, "fresh", {"ru": "новый"})
    observed: list[int] = []

    def read() -> None:
        snap = store.snapshot
        # a snapshot is internally consistent: either it has "fresh" (new) or not (old)
        assert (snap.get_screen("fresh") is not None) == (snap.version != old.version)
        observed.append(snap.version)

    task = asyncio.create_task(store.reload())
    while not task.done():
        read()
        await asyncio.sleep(0)
    read()
    new = await task
    assert new.version == old.version + 1
    assert old.get_screen("fresh") is None  # readers holding the old snapshot are unaffected
    assert store.get_screen("fresh") is not None
    assert observed[0] == old.version and observed[-1] == new.version


async def test_concurrent_reloads_are_serialized(db: CountingDatabase) -> None:
    store = ContentStore(db)
    await store.load()
    snaps = await asyncio.gather(*(store.reload() for _ in range(5)))
    assert sorted(s.version for s in snaps) == [2, 3, 4, 5, 6]
    assert store.version == 6


async def test_remember_file_id(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        mid = (
            await conn.execute(
                sa.insert(media)
                .values(kind="photo", sha256="a" * 64, path="a.jpg", file_ids={"7": "OLD"})
                .returning(media.c.id)
            )
        ).first()
    assert mid is not None
    store = ContentStore(db, seed=False)
    await store.load()
    assert store.file_id(mid.id, 7) == "OLD"
    assert store.file_id(mid.id, 8) is None
    await store.remember_file_id(mid.id, 8, "NEW8")
    assert store.file_id(mid.id, 8) == "NEW8"
    rows = await db.raw("select file_ids from media where id = $1", mid.id)
    assert rows[0]["file_ids"] == {"7": "OLD", "8": "NEW8"}
    before = db.queries
    await store.remember_file_id(mid.id, 8, "NEW8")  # unchanged → no SQL
    assert db.queries == before


async def test_db_constraints(db: CountingDatabase) -> None:
    with pytest.raises(Exception, match="code_format"):
        await db.raw("insert into screens (code) values ('Bad-Code')")
    with pytest.raises(Exception, match="system_has_code"):
        await db.raw("insert into screens (kind) values ('system')")
    sid = await _insert_screen(db, "c1", {})
    with pytest.raises(Exception, match="style"):
        await db.raw(
            "insert into screen_buttons (screen_id, label, action, style) values ($1, '{}', '{}', 'pink')",
            sid,
        )
    with pytest.raises(Exception, match="sha256"):
        await db.raw("insert into media (kind, sha256) values ('photo', 'nothex')")


async def test_reload_500_screens_under_200ms(db: CountingDatabase) -> None:
    store = ContentStore(db)
    await store.load()
    body = {"ru": {"text": "Экран {days_left}", "entities": [{"type": "bold", "offset": 0, "length": 5}]}}
    await db.raw(
        "insert into screens (code, kind, body) "
        "select 's' || g, 'custom', $1::jsonb from generate_series(1, 500) g",
        body,
    )
    label = {"ru": "Кнопка", "en": "Button"}
    cond = {"all": [{"sub": "active"}, {"days_left": {"lte": 3}}]}
    await db.raw(
        "insert into screen_buttons (screen_id, row, sort, label, action, visible_if) "
        'select s.id, b % 3, b, $1::jsonb, \'{"type": "system", "name": "buy"}\'::jsonb, $2::jsonb '
        "from screens s cross join generate_series(1, 5) b where s.code like 's%'",
        label,
        cond,
    )
    await store.reload()  # warm-up (prepared statements, imports)
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        snap = await store.reload()
        timings.append((time.perf_counter() - started) * 1000)
    assert len(snap.by_id) >= 500
    assert snap.problems == ()
    assert min(timings) <= 200, timings
