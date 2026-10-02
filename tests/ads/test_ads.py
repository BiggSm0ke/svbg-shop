"""Ad links: Bedolaga codes as is, first-touch attribution in one statement, the funnel, the owner's
screens."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from svbg.ads.admin import ACTIONS, SCREEN_CARD, SCREEN_LIST, AdAdminScreens
from svbg.ads.service import AdError, AdService, check_new_code, from_bedolaga_campaign
from svbg.tg.ui.codec import encode
from tests.dbkit import CountingDatabase
from tests.promo.kit import add_paid_order
from tests.promo.ui_kit import ADMIN, ADMIN_NOPERM, OWNER, SUPPORT, USER, UiEnv, build_ui
from tests.tg.ui.ui_harness import text_message

ACTOR = (1, "owner")
BEDOLAGA = {
    "id": 3,
    "name": "Канал Ивана",
    "start_parameter": "ivan_channel",
    "bonus_type": "balance",
    "balance_bonus_kopeks": 5000,
    "subscription_duration_days": None,
    "is_active": True,
    "partner_user_id": 77,
    "created_at": datetime(2025, 5, 1, 10, 0),
}


async def new_user(db: CountingDatabase, tg_id: int) -> int:
    return int((await db.raw("insert into users (telegram_id) values ($1) returning id", tg_id))[0]["id"])


def test_codes_and_bedolaga_mapping() -> None:
    assert check_new_code(" spring_tg ") == "spring_tg"
    for bad in ("ab", "p_123", "pr_x12", "REF123", "setup_x", "l_abc", "a_tag", "with space", "x" * 33, None):
        with pytest.raises(AdError):
            check_new_code(bad)
    values = from_bedolaga_campaign(BEDOLAGA)
    assert values["code"] == "ivan_channel" and values["title"] == "Канал Ивана"
    assert values["bonus"] == {"type": "balance", "balance_bonus_kopeks": 5000}
    assert values["owner_user_id"] == 77 and values["legacy_id"] == "3"
    assert values["created_at"] == datetime(2025, 5, 1, 10, 0, tzinfo=UTC)
    with pytest.raises(AdError):
        from_bedolaga_campaign({**BEDOLAGA, "start_parameter": "bad code"})


async def test_import_attribution_and_funnel(db: CountingDatabase, ads: AdService) -> None:
    async with db.tx() as conn:
        link_id = await AdService.import_campaign(conn, BEDOLAGA)
        assert await AdService.import_campaign(conn, {**BEDOLAGA, "name": "Иван"}) == link_id
    await ads.load()
    link = ads.by_code("ivan_channel")
    assert link is not None and link.title == "Иван" and link.has_bonus and link.source == "import"
    assert ads.by_code("IVAN_CHANNEL") is None  # exact match, like Telegram's start parameter

    old = await new_user(db, 1)
    async with db.tx() as conn:
        assert await AdService.import_registration(conn, user_id=old, ad_link_id=link_id)
        assert not await AdService.import_registration(conn, user_id=old, ad_link_id=link_id)
    fresh, returning = await new_user(db, 2), await new_user(db, 3)
    mark = db.queries
    assert await ads.record_start(link, fresh, is_new=True)
    assert db.queries - mark == 1
    assert not await ads.record_start(link, returning, is_new=False)  # counted, not attributed
    assert not await ads.record_start(link, fresh, is_new=True)  # first touch stays
    other = await ads.create(ACTOR, title="Другая", code="other_one")
    assert not await ads.record_start(other, fresh, is_new=True)
    assert (await ads.link_of(fresh)).id == link_id  # type: ignore[union-attr]
    assert await ads.link_of(returning) is None
    clicks = (await db.raw("select clicks from ad_links where id = $1", link_id))[0]["clicks"]
    assert clicks == 3
    await add_paid_order(db, fresh, 17_900)
    await db.raw("insert into trial_grants (user_id, telegram_id) values ($1, 2)", fresh)
    stats = await ads.stats(link_id)
    assert (stats.users, stats.users_30d, stats.trials, stats.payers, stats.revenue_minor) == (
        2,
        2,
        1,
        1,
        17_900,
    )


async def test_owner_crud(db: CountingDatabase, ads: AdService) -> None:
    link = await ads.create(ACTOR, title=" Весна ")
    assert link.title == "Весна" and len(link.code) == 8 and ads.by_code(link.code) is not None
    with pytest.raises(AdError, match="уже есть"):
        await ads.create(ACTOR, title="x", code=link.code)
    with pytest.raises(AdError):
        await ads.create(ACTOR, title=" ")
    off = await ads.update(link.id, ACTOR, enabled=False)
    assert not off.enabled and ads.by_code(link.code) is None
    renamed = await ads.update(link.id, ACTOR, code="spring2026")
    assert ads.get(link.id).code == "spring2026" and ads.by_code(link.code) is None  # type: ignore[union-attr]
    assert renamed.code == "spring2026"
    await ads.update(link.id, ACTOR, enabled=True)
    uid = await new_user(db, 10)
    await ads.record_start(ads.by_code("spring2026"), uid, is_new=True)  # type: ignore[arg-type]
    await ads.load()
    with pytest.raises(AdError, match="менять нельзя"):
        await ads.update(link.id, ACTOR, code="spring2027")
    with pytest.raises(AdError, match="только выключить"):
        await ads.delete(link.id, ACTOR)
    with pytest.raises(AdError):
        await ads.update(link.id, ACTOR, clicks=0)
    empty = await ads.create(ACTOR, title="Пустая", code="empty_one")
    await ads.delete(empty.id, ACTOR)
    assert ads.get(empty.id) is None
    actions = [r["action"] for r in await db.raw("select action from admin_audit order by id")]
    assert actions[0] == "ad_link.create" and actions[-1] == "ad_link.delete"


# ------------------------------------------------------------------------------------------- screens


@pytest.fixture
async def env(db: CountingDatabase, ads: AdService) -> tuple[UiEnv, AdAdminScreens]:
    ui = await build_ui(db)
    screens = AdAdminScreens(ui.router, ads)
    screens.install()
    await ui.staff("promo")
    return ui, screens


@pytest.mark.parametrize("who", [ADMIN_NOPERM, SUPPORT, USER], ids=["admin-without-promo", "support", "user"])
async def test_denied(env: tuple[UiEnv, AdAdminScreens], ads: AdService, who: int) -> None:
    ui, screens = env
    link = await ads.create(ACTOR, title="X", code="xx_link")
    for data in (
        encode(SCREEN_LIST),
        encode(SCREEN_CARD, arg=str(link.id)),
        encode(ACTIONS, "en", str(link.id)),
    ):
        await ui.click(who, data)
        assert ui.toasts[-1] == "Нет прав"
    assert ads.get(link.id).enabled  # type: ignore[union-attr]
    assert not await screens.handle_command(text_message(who, "/ads"))


async def test_screens(env: tuple[UiEnv, AdAdminScreens], ads: AdService, db: CountingDatabase) -> None:
    ui, screens = env
    assert await screens.handle_command(text_message(OWNER, "/ads"))
    assert "Ссылок пока нет" in ui.text
    await ui.press(OWNER, "Новая ссылка")
    await ui.type(OWNER, "Канал Ивана")
    await ui.type(OWNER, "p_bad")
    assert "Код не может начинаться" in ui.text
    await ui.type(OWNER, "ivan_tg")
    assert "Ссылка создана" in ui.text and "https://t.me/svbg_bot?start=ivan_tg" in ui.text
    copy = ui.buttons()[0]
    assert copy.copy_text is not None and copy.copy_text.text == "https://t.me/svbg_bot?start=ivan_tg"
    link = ads.by_code("ivan_tg")
    assert link is not None
    await ui.click(ADMIN, encode(SCREEN_CARD, arg=str(link.id)))
    await ui.press(ADMIN, "Выключить")
    assert "⏸ выключена" in ui.text
    await ui.press(ADMIN, "Название")
    await ui.type(ADMIN, "Иван")
    assert "<b>Иван</b>" in ui.text and "Сохранено" in ui.text
    await ui.press(ADMIN, "Код")
    await ui.type(ADMIN, "ivan2")
    assert "<code>ivan2</code>" in ui.text
    await ui.click(ADMIN, encode(SCREEN_LIST))
    assert "⏸ Иван · 0 перех." in ui.labels()
    uid = await new_user(db, 99)
    await ui.click(ADMIN, encode(ACTIONS, "en", str(link.id)))
    await ads.record_start(ads.by_code("ivan2"), uid, is_new=True)  # type: ignore[arg-type]
    await ads.load()
    await ui.click(ADMIN, encode(SCREEN_CARD, arg=str(link.id)))
    assert "Пришло новых: 1" in ui.text and "Удалить" not in " ".join(ui.labels())
    await ui.click(ADMIN, encode(ACTIONS, "del", str(link.id)))
    assert "только выключить" in str(ui.toasts[-1])
    await ui.click(OWNER, encode(ACTIONS, "new"))
    await ui.type(OWNER, "Временная")
    await ui.press(OWNER, "Пропустить")
    tmp = next(x for x in ads.all() if x.title == "Временная")
    await ui.press(OWNER, "Удалить")
    await ui.press(OWNER, "Да, удалить")
    assert ui.toasts[-1] == "🗑 Удалено" and ads.get(tmp.id) is None
    for data in (encode(ACTIONS, "en", "abc"), encode(ACTIONS, "title", "999"), encode(ACTIONS, "del", "")):
        await ui.click(OWNER, data)
        assert ui.toasts[-1] == "Ссылка не найдена"
