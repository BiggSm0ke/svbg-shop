"""Stage 3 end to end — the constructor and the content transfer (stage3-contracts «Acceptance»).

* the owner turns «✏️» on and changes the home screen's picture, a button (colour, Premium emoji icon,
  visibility condition) and the text; another user sees every change on the next click without a restart;
  «↩️ Отменить» brings the text back;
* ``content.zip`` export → import into a clean install (another database, another bot) → the same screens,
  media and plans.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from svbg.app import App
from svbg.boot.envfile import EnvDocument, write_atomic
from svbg.content.banner import banner_sha256
from svbg.core.crypto import generate_key
from tests.e2e.conftest import CAPTCHA_OFF_SQL, OWNER_ID, AppEnv, StartApp, apply_sql, schema_sql
from tests.e2e.test_stage2_kit import FAST, open_shop, until
from tests.e2e.test_stage3_kit import (  # noqa: F401 - the ``tg`` fixture
    EMOJI,
    chat,
    create_database,
    drop_database,
    jpeg_bytes,
    tg,
)
from tests.pgcluster import PgCluster

pytestmark = pytest.mark.pg

CLEAN_DB = f"clean_{uuid.uuid4().hex[:10]}"


def home_buttons(msg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b for row in rows for b in row]


async def test_owner_edits_home_in_place_others_see_it_and_undo(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        anna = chat(shop, 5_101)  # no subscription: sees the conditional button
        boris = chat(shop, 5_102)  # trial: does not
        await anna.start()
        await boris.start()
        await boris.press("Попробовать бесплатно")
        await boris.wait_text("Готово! Пробный период до", timeout=20)
        old_text = anna.text()

        owner = chat(shop, OWNER_ID)
        await owner.start()
        await owner.say("/edit", expect="✏️ Экран")
        await owner.tap("✏️ Экран", expect="✏️ Экран «")

        # 🖼 picture: downloaded from Telegram, stored by sha256, re-sent by file_id
        await owner.tap("🖼 Медиа", expect="Пришлите фото")
        await owner.send_photo(jpeg_bytes(), expect="⚡ Применено")
        media = await shop.rows("select kind, path, sha256 from media order by id")
        # the default banner (installed on start) and the owner's own picture that replaced it on home
        assert [m["kind"] for m in media] == ["photo", "photo"]
        own = [m for m in media if m["sha256"] != banner_sha256()]
        assert len(own) == 1 and (app_env.data_dir / "media" / own[0]["path"]).is_file()

        # ➕ a link button: green, a Premium emoji icon, visible only without a subscription
        await owner.tap("➕ Кнопка", expect="шаг 1 из 2")
        await owner.say("🔥 Скидка новичкам", expect="шаг 2 из 2")
        await owner.tap("🔗 Ссылка", expect="Пришлите ссылку")
        await owner.say("https://example.com/sale", expect="кнопка добавлена")
        await owner.tap("😀 Иконка", expect="премиум-эмодзи")
        icon = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": EMOJI}]
        await owner.say("🔥", entities=icon, expect=f"Иконка: {EMOJI}")
        await owner.tap("🎨 Цвет", expect="зелёный")
        await owner.tap("зелёный", expect="Цвет: зелёный")
        await owner.tap("👁 Условие", expect="Нет подписки")
        await owner.tap("Нет подписки", expect="Видна: Нет подписки")
        assert shop.tg.calls_for("getCustomEmojiStickers"), "the icon is validated by Telegram"

        # 📝 the text (with entities) — the last change, the one «Отменить» takes back
        await owner.tap("⬅️ К экрану", expect="📝 Текст RU")
        await owner.tap("📝 Текст RU", expect="Пришлите новый текст")
        entities = [{"type": "bold", "offset": 0, "length": 5}, {"type": "spoiler", "offset": 6, "length": 6}]
        await owner.say("Акция скидки до мая", entities=entities, expect="⚡ Применено")

        # another user, the next click, no restart
        await anna.start()
        shown = anna.message()
        assert shown.get("photo") and shown.get("caption") == "Акция скидки до мая"
        assert [e["type"] for e in shown.get("caption_entities") or []] == ["bold", "spoiler"]
        sale = next(b for b in home_buttons(shown) if "Скидка новичкам" in b["text"])
        assert sale["url"] == "https://example.com/sale"
        assert sale.get("style") == "success" and sale.get("icon_custom_emoji_id") == EMOJI
        await boris.start()
        assert not any("Скидка новичкам" in b["text"] for b in home_buttons(boris.message()))
        assert shop.app.running  # the same process all along: no restart in between

        # «↩️ Отменить» brings the old text back for everybody; the picture and the button stay
        await owner.tap("↩️ Отменить", expect="Отменено")
        await anna.start()
        assert anna.text() == old_text
        assert any("Скидка новичкам" in b["text"] for b in home_buttons(anna.message()))


async def _second_install(pg_cluster: PgCluster, app_env: AppEnv, tmp_path: Path) -> tuple[App, Path, str]:
    """A clean install: a new database with the migrated schema, another bot token, another data dir."""
    dsn = await create_database(pg_cluster, CLEAN_DB)
    await apply_sql(dsn, schema_sql())
    await apply_sql(dsn, CAPTCHA_OFF_SQL)
    data = tmp_path / "clean"
    token = app_env.tg.add_bot(username="svbg_clean_bot")
    env = AppEnv(
        data_dir=data,
        env_path=data / ".env",
        dsn=dsn,
        token=token,
        tg=app_env.tg,
        values={
            "BOT_TOKEN": token,
            "TELEGRAM_API_URL": app_env.tg.url,
            "DATABASE_URL": dsn,
            "SECRET_KEY": generate_key(),
            "OWNER_IDS": str(OWNER_ID),
            "DATA_DIR": str(data),
        },
    )
    doc = EnvDocument.parse("")
    for key, value in env.values.items():
        doc.set(key, value)
    data.mkdir(parents=True)
    write_atomic(env.env_path, doc.render())
    app = App(env.options(**FAST))
    await app.start()
    return app, data, token


def _content(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(r.values()) for r in rows]


SCREENS_SQL = "select code, title, body, media_mode from screens where code is not null order by code"
BUTTONS_SQL = (
    "select s.code, b.label, b.action, b.style, b.visible_if, b.icon_custom_emoji_id, b.row, b.sort"
    " from screen_buttons b join screens s on s.id = b.screen_id where s.code is not null"
    " order by s.code, b.row, b.sort, b.label::text"
)
PLANS_SQL = (
    "select p.code, p.name, p.is_trial, p.traffic_bytes, p.device_limit, pp.days, pp.amount_minor"
    " from plans p left join plan_prices pp on pp.plan_id = p.id order by p.code, pp.days"
)


async def test_content_zip_moves_to_a_clean_install(
    start_app: StartApp, app_env: AppEnv, pg_cluster: PgCluster, tmp_path: Path
) -> None:
    async with open_shop(start_app, app_env) as shop:
        app = shop.app
        assert app.content_transfer is not None and app.media is not None
        owner = chat(shop, OWNER_ID)
        await owner.start()
        await owner.say("/edit", expect="✏️ Экран")
        await owner.tap("✏️ Экран", expect="✏️ Экран «")
        await owner.tap("🖼 Медиа", expect="Пришлите фото")
        await owner.send_photo(jpeg_bytes(color=(10, 120, 200)), expect="⚡ Применено")
        await owner.tap("📝 Текст RU", expect="Пришлите новый текст")
        await owner.say("Перенесённая главная", expect="⚡ Применено")
        exported = await app.content_transfer.export(tmp_path / "content.zip")
        assert Path(exported.path).is_file()
        src = shop.db
        screens = _content([dict(r) for r in await src.raw(SCREENS_SQL)])
        buttons = _content([dict(r) for r in await src.raw(BUTTONS_SQL)])
        plans = _content([dict(r) for r in await src.raw(PLANS_SQL)])
        media = [dict(r) for r in await src.raw("select sha256, kind from media order by sha256")]

    clean, data, token = await _second_install(pg_cluster, app_env, tmp_path)
    try:
        assert clean.content_transfer is not None and clean.content is not None
        result = await clean.content_transfer.import_archive(Path(exported.path), actor=None)
        assert "screens" in result.sections and "plans" in result.sections, result.summary()
        db: Any = clean.db
        assert _content([dict(r) for r in await db.raw(SCREENS_SQL)]) == screens
        assert _content([dict(r) for r in await db.raw(BUTTONS_SQL)]) == buttons
        assert _content([dict(r) for r in await db.raw(PLANS_SQL)]) == plans
        moved = [dict(r) for r in await db.raw("select sha256, kind, path from media order by sha256")]
        assert [(m["sha256"], m["kind"]) for m in moved] == [(m["sha256"], m["kind"]) for m in media]
        assert all((data / "media" / m["path"]).is_file() for m in moved)
        # the new bot shows it at once (another token: the file is uploaded, not re-sent by a foreign id)
        home = clean.content.get_screen("home")
        assert home is not None and home.text("ru").text == "Перенесённая главная"
        begin = len(app_env.tg.calls)
        app_env.tg.push_message(5_201, "/start", bot_id=app_env.tg.bot_id(token))
        await until(
            lambda: any(
                c.ok and c.method == "sendPhoto" and c.params.get("chat_id") == 5_201
                for c in app_env.tg.calls[begin:]
            ),
            what="the imported home screen with its picture",
        )
    finally:
        await clean.stop()
        await drop_database(pg_cluster, CLEAN_DB)
