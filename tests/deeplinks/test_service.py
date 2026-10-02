"""DeeplinkService on a real database: resolution order, hits, onboarding, promo, short links, stats."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from svbg.core import clock
from svbg.deeplinks.model import Intent
from svbg.deeplinks.service import TEXTS, Landing
from svbg.tg.ui.context import UserCtx
from tests.dbkit import CountingDatabase
from tests.deeplinks.kit import FakeCatalog, Kit, build_kit, plan

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def kit(db: CountingDatabase) -> Kit:
    clock.set_clock(NOW)
    return build_kit(db)


class Gate:
    """The user path's start hook: shows the channel screen while ``closed``."""

    def __init__(self) -> None:
        self.closed = False
        self.links: list[Any] = []

    async def __call__(self, user: UserCtx, chat_id: int, link: Any) -> tuple[str, Any] | None:
        self.links.append(link)
        return ("chan", None) if self.closed else None


# ------------------------------------------------------------------------------------------- direct


async def test_direct_targets(kit: Kit) -> None:
    u = await kit.user(100)
    assert await kit.service.on_start(u, 100, "s_buy") == ("buy", None)
    assert await kit.service.on_start(u, 100, "p_std") == ("buy_plan", "1")
    assert await kit.service.on_start(u, 100, "p_1") == ("buy_plan", "1")
    assert await kit.service.on_start(u, 100, "t_500") == ("topup", "50000")
    assert await kit.service.on_start(u, 100, None) is None
    hits = await kit.hits()
    assert [(h["kind"], h["payload"], h["is_new"]) for h in hits] == [
        ("screen", "s_buy", True),
        ("plan", "p_std", True),
        ("plan", "p_1", True),
        ("topup", "t_500", True),
    ]
    assert not kit.sent.messages


async def test_stage2_deeplink_object_is_accepted(kit: Kit) -> None:
    from svbg.tg.user.deeplink import parse_start_payload

    u = await kit.user(101)
    assert await kit.service.on_start(u, 101, parse_start_payload("plan_std")) == ("buy_plan", "1")


async def test_screens_users_may_not_open_land_on_home(kit: Kit) -> None:
    u = await kit.user(102)
    assert await kit.service.on_start(u, 102, "s_settings_root") is None
    assert kit.sent.messages == [(102, TEXTS["screen_gone"])]


async def test_plan_unavailable_and_link_only_plans(kit: Kit) -> None:
    u = await kit.user(103)
    assert await kit.service.on_start(u, 103, "p_old") == ("buy", None)
    assert await kit.service.on_start(u, 103, "p_nope") == ("buy", None)
    assert kit.sent.messages[-1] == (103, TEXTS["plan_gone"])
    assert kit.service.granted_plan_code(u.user_id) is None
    assert await kit.service.on_start(u, 103, "p_vip") == ("buy_plan", "2")
    assert kit.service.granted_plan_code(u.user_id) == "vip"
    clock.set_clock(NOW + timedelta(hours=25))
    assert kit.service.granted_plan_code(u.user_id) is None


async def test_link_only_plan_is_not_opened_by_its_numeric_id(kit: Kit) -> None:
    u = await kit.user(111)
    # ids are sequential: p_1, p_2, … must not reveal hidden plans
    assert await kit.service.on_start(u, 111, "p_2") == ("buy", None)
    assert await kit.service.on_start(u, 111, "plan_2") == ("buy", None)
    assert kit.sent.messages == [(111, TEXTS["plan_gone"])] * 2
    assert kit.service.granted_plan_code(u.user_id) is None
    # a public plan still opens by id
    assert await kit.service.on_start(u, 111, "p_1") == ("buy_plan", "1")
    assert kit.service.granted_plan_code(u.user_id) is None


async def test_short_link_grants_a_link_only_plan_even_by_id(kit: Kit) -> None:
    link = await kit.service.create_link(Intent(plan="2"), title="Партнёр", actor=None)
    u = await kit.user(112)
    assert await kit.service.on_start(u, 112, "l_" + link.code) == ("buy_plan", "2")
    assert kit.service.granted_plan_code(u.user_id) == "vip"


async def test_ignored_payloads(kit: Kit) -> None:
    u = await kit.user(104)
    for payload in ("setup_abc", "unknown_code", "t_abc", "привет", "x" * 70):
        assert await kit.service.on_start(u, 104, payload) is None
    assert await kit.hits() == []


# ------------------------------------------------------------------------------------------- order


async def test_exact_ad_code_wins_over_prefixes(kit: Kit) -> None:
    u = await kit.user(105)
    # "p_test" is an old campaign code: an ad, not the plan "test"
    assert await kit.service.on_start(u, 105, "p_test") is None
    assert await kit.service.on_start(u, 105, "summer2025") is None
    assert kit.ads.attached == [(u.user_id, 13, True), (u.user_id, 11, True)]
    assert [h["kind"] for h in await kit.hits()] == ["ad_code", "ad_code"]


async def test_old_campaign_codes_that_look_like_bad_prefixes(kit: Kit) -> None:
    # Bedolaga campaigns "t_tiktok" / "p_VK2024": a known prefix with a value it would reject
    kit.ads.links.update({"t_tiktok": 21, "p_VK2024": 22})
    u = await kit.user(113)
    assert await kit.service.on_start(u, 113, "t_tiktok") is None
    assert await kit.service.on_start(u, 113, "p_VK2024") is None
    assert kit.ads.attached == [(u.user_id, 21, True), (u.user_id, 22, True)]
    hits = await kit.hits()
    assert [(h["kind"], h["payload"]) for h in hits] == [("ad_code", "t_tiktok"), ("ad_code", "p_VK2024")]
    assert not kit.sent.messages
    # an unknown one is still ignored, and setup_ never reaches the ads module
    assert await kit.service.on_start(u, 113, "t_nothing") is None
    assert await kit.service.on_start(u, 113, "setup_abc") is None
    assert "setup_abc" not in kit.ads.finds
    assert len(await kit.hits()) == 2


async def test_ad_lookups_are_cached(kit: Kit) -> None:
    u = await kit.user(106)
    for _ in range(3):
        await kit.service.on_start(u, 106, "s_buy")
        await kit.service.on_start(u, 106, "a_tiktok")
    assert kit.ads.finds.count("s_buy") == 1
    assert kit.ads.finds.count("a_tiktok") == 1 and kit.ads.finds.count("tiktok") == 1
    assert kit.ads.attached.count((u.user_id, 12, True)) == 3


async def test_referral_codes(kit: Kit) -> None:
    new = await kit.user(107)
    old = await kit.user(108, is_new=False)
    await kit.service.on_start(new, 107, "r_abc123")
    await kit.service.on_start(old, 108, "r_abc123")  # no retro-binding
    assert kit.referral.calls == [(new.user_id, "abc123")]
    kit.referral.calls.clear()
    legacy = await kit.user(109)
    await kit.service.on_start(legacy, 109, "refA1b2C3d4")  # stored with the prefix: one call
    stripped = await kit.user(110)
    await kit.service.on_start(stripped, 110, "refZ9y8X7w6")  # stored without it: second try
    assert kit.referral.calls == [
        (legacy.user_id, "refA1b2C3d4"),
        (stripped.user_id, "refZ9y8X7w6"),
        (stripped.user_id, "Z9y8X7w6"),
    ]
    assert [h["kind"] for h in await kit.hits()] == ["ref", "ref", "legacy_ref", "legacy_ref"]


async def test_works_without_optional_modules(db: CountingDatabase) -> None:
    clock.set_clock(NOW)
    kit = build_kit(db, promo=False, ads=False, referral=False)
    u = await kit.user(111)
    assert await kit.service.on_start(u, 111, "r_abc") is None
    assert await kit.service.on_start(u, 111, "summer2025") is None
    assert await kit.service.on_start(u, 111, "pr_AUTUMN") is None
    assert kit.sent.messages == [(111, TEXTS["promo_unavailable"])]
    assert await kit.service.on_start(u, 111, "p_std") == ("buy_plan", "1")


# ------------------------------------------------------------------------------------------- onboarding


async def test_intent_survives_the_gate(kit: Kit) -> None:
    gate = Gate()
    gate.closed = True
    u = await kit.user(120)
    hook = kit.service.start_hook(gate)
    assert await hook(u, 120, "pr_AUTUMN") == ("chan", None)
    assert gate.links == [None]  # the user path never sees (and never stores) the link itself
    assert kit.promo.calls == []  # not applied before the channel check
    state = await kit.ui_state.get(u.user_id)
    assert state.pending_intent is not None and state.pending_intent["promo"] == "AUTUMN"
    landing = await kit.service.resume(u)
    assert landing == Landing("home", None, TEXTS["promo_pending"].format(code="AUTUMN"))
    assert kit.promo.calls == [(u.user_id, "AUTUMN")]
    assert await kit.service.resume(u) is None  # consumed


async def test_intent_after_restart_and_plain_start(kit: Kit, db: CountingDatabase) -> None:
    gate = Gate()
    gate.closed = True
    u = await kit.user(121)
    await kit.service.on_start(u, 121, "p_std", gate=gate)
    # restart: a cold ui_state cache; the user joined the channel and sends a plain /start
    fresh = build_kit(db)
    gate.closed = False
    assert await fresh.service.on_start(u, 121, None, gate=gate) == ("buy_plan", "1")
    assert await fresh.service.resume(u) is None


async def test_a_newer_link_replaces_the_pending_one(kit: Kit) -> None:
    gate = Gate()
    gate.closed = True
    u = await kit.user(122)
    await kit.service.on_start(u, 122, "p_std", gate=gate)
    await kit.service.on_start(u, 122, "t_300", gate=gate)
    gate.closed = False
    assert await kit.service.on_start(u, 122, "s_buy", gate=gate) == ("buy", None)
    assert await kit.service.resume(u) is None


async def test_expired_intent_is_dropped(kit: Kit) -> None:
    gate = Gate()
    gate.closed = True
    u = await kit.user(123)
    kit.config["DEEPLINK_INTENT_TTL_HOURS"] = 2
    await kit.service.on_start(u, 123, "p_std", gate=gate)
    clock.set_clock(NOW + timedelta(hours=3))
    assert await kit.service.resume(u) is None


async def test_stage2_pending_intent_is_executed(kit: Kit) -> None:
    u = await kit.user(124)
    old = {"kind": "deeplink", "v": 1, "type": "legacy_code", "value": "plan_std", "raw": "plan_std"}
    await kit.ui_state.set_pending_intent(u.user_id, old)
    assert await kit.service.resume(u) == Landing("buy_plan", "1")


async def test_resume_without_intent_costs_no_sql(kit: Kit) -> None:
    u = await kit.user(125)
    await kit.ui_state.get(u.user_id)  # cached, as after any click
    before = kit.db.queries
    assert await kit.service.resume(u) is None
    assert kit.db.queries == before


# ------------------------------------------------------------------------------------------- promo


async def test_promo_outcomes(kit: Kit) -> None:
    u = await kit.user(130)
    await kit.service.on_start(u, 130, "pr_WEEK")
    await kit.service.on_start(u, 130, "pr_WEEK")
    await kit.service.on_start(u, 130, "pr_NOPE")
    assert [m for _, m in kit.sent.messages] == [
        TEXTS["promo_applied"].format(code="WEEK"),
        TEXTS["promo_refused"].format(code="WEEK"),
        TEXTS["promo_refused"].format(code="NOPE"),
    ]


async def test_promo_module_failures_are_isolated(kit: Kit) -> None:
    u = await kit.user(131)
    kit.promo.fail = RuntimeError("promo is down")
    assert await kit.service.on_start(u, 131, "p_std") == ("buy_plan", "1")
    link = await kit.service.create_link(Intent(plan="std", promo="WEEK"), title="x", actor=await kit.actor())
    assert await kit.service.on_start(u, 131, "l_" + link.code) == ("buy_plan", "1")
    assert kit.sent.messages[-1] == (131, TEXTS["promo_refused"].format(code="WEEK"))
    assert [place for _, place, _ in kit.hub.captured] == ["deeplinks:promo.apply"]
    kit.promo.fail = None
    kit.promo.hang = True  # the timeout bounds /start
    assert await kit.service.on_start(u, 131, "pr_WEEK") is None
    assert kit.hub.captured[-1][1] == "deeplinks:promo.apply"


async def test_broken_dependencies_never_break_start(kit: Kit) -> None:
    u = await kit.user(132)

    def boom(_user: UserCtx, _code: str) -> bool:
        raise RuntimeError("probe")

    kit.service.can_open = boom
    assert await kit.service.on_start(u, 132, "s_buy") is None

    async def bad_find(_code: str) -> Any:
        raise OSError("db")

    kit.ads.find = bad_find  # type: ignore[method-assign]
    kit.service.forget_ads()
    assert await kit.service.on_start(u, 132, "t_100") == ("topup", "10000")


# ------------------------------------------------------------------------------------------- short links


async def test_short_link_counts_unique_users_and_records_every_hit(kit: Kit) -> None:
    link = await kit.service.create_link(
        Intent(plan="std", promo="AUTUMN", ad="tiktok"),
        title="Канал · осень",
        actor=await kit.actor(),
        max_uses=2,
    )
    a, b, c = await kit.user(140), await kit.user(141), await kit.user(142)
    payload = "l_" + link.code
    assert await kit.service.on_start(a, 140, payload) == ("buy_plan", "1")
    assert await kit.service.on_start(a, 140, payload) == ("buy_plan", "1")  # the same user again
    assert await kit.service.on_start(b, 141, payload) == ("buy_plan", "1")
    assert await kit.service.on_start(c, 142, payload) is None  # the limit is reached
    assert kit.sent.messages[-1] == (142, TEXTS["link_gone"])
    row = await kit.service.get_link(link.id)
    assert row is not None and row.uses == 2
    hits = await kit.hits()
    assert [(h["link_id"], h["user_id"]) for h in hits] == [
        (link.id, a.user_id),
        (link.id, a.user_id),
        (link.id, b.user_id),
    ]
    assert (a.user_id, 12, True) in kit.ads.attached
    assert kit.promo.calls == [(a.user_id, "AUTUMN"), (a.user_id, "AUTUMN"), (b.user_id, "AUTUMN")]


async def test_short_link_costs_at_most_two_statements(kit: Kit) -> None:
    link = await kit.service.create_link(Intent(screen="buy"), title="x", actor=None)
    u = await kit.user(143)
    await kit.service.find_ad("l_" + link.code)  # warm the ad cache (one lookup per payload per minute)
    before = kit.db.queries
    accepted = await kit.service.accept(u, "l_" + link.code)
    assert accepted.intent is not None and accepted.intent.link_id == link.id
    assert kit.db.queries - before <= 2


async def test_expired_disabled_and_unknown_links(kit: Kit) -> None:
    u = await kit.user(144)
    expiring = await kit.service.create_link(
        Intent(screen="buy"), title="x", actor=None, expires_at=NOW + timedelta(hours=1)
    )
    assert await kit.service.on_start(u, 144, "l_" + expiring.code) == ("buy", None)
    clock.set_clock(NOW + timedelta(hours=2))
    assert await kit.service.on_start(u, 144, "l_" + expiring.code) is None
    off = await kit.service.create_link(Intent(screen="buy"), title="y", actor=None)
    await kit.service.set_enabled(off.id, False, actor=await kit.actor())
    assert await kit.service.on_start(u, 144, "l_" + off.code) is None
    assert await kit.service.on_start(u, 144, "l_NoSuchCode") is None
    assert [m for _, m in kit.sent.messages] == [TEXTS["link_gone"], TEXTS["link_gone"]]


async def test_link_with_referral_code(kit: Kit) -> None:
    link = await kit.service.create_link(Intent(ref="abc123"), title="Партнёр", actor=None)
    u = await kit.user(145)
    assert await kit.service.on_start(u, 145, "l_" + link.code) is None
    assert kit.referral.calls == [(u.user_id, "abc123")]


async def test_create_link_validation_and_audit(kit: Kit) -> None:
    with pytest.raises(ValueError, match="Название"):
        await kit.service.create_link(Intent(screen="buy"), title="  ", actor=None)
    with pytest.raises(ValueError, match="цель"):
        await kit.service.create_link(Intent(), title="x", actor=None)
    with pytest.raises(ValueError, match="Лимит"):
        await kit.service.create_link(Intent(screen="buy"), title="x", actor=None, max_uses=0)
    with pytest.raises(ValueError, match="Код"):
        await kit.service.create_link(Intent(screen="buy"), title="x", actor=None, code="bad code")
    link = await kit.service.create_link(
        Intent(plan="std", promo="AUTUMN", ad="tiktok"), title="A  b", actor=await kit.actor(), code="autumn"
    )
    assert (link.title, link.promo_id, link.ad_link_id, link.payload) == ("A b", 6, 12, "l_autumn")
    with pytest.raises(ValueError, match="занят"):
        await kit.service.create_link(Intent(screen="buy"), title="x", actor=await kit.actor(), code="autumn")
    await kit.service.set_enabled(link.id, False, actor=await kit.actor())
    assert await kit.service.set_enabled(10**9, True, actor=await kit.actor()) is None
    rows = await kit.db.raw("select action, target, details from admin_audit order by id")
    assert [(r["action"], r["target"]) for r in rows] == [
        ("deeplink.create", "l_autumn"),
        ("deeplink.disable", f"deeplink:{link.id}"),
    ]
    assert rows[0]["details"]["promo"] == "AUTUMN"


async def test_check_spec(kit: Kit) -> None:
    assert await kit.service.check_spec(Intent(plan="std", promo="AUTUMN", ad="tiktok")) == []
    problems = await kit.service.check_spec(Intent(plan="zzz", promo="NOPE", ad="nope"))
    assert problems == ["Тарифа «zzz» нет", "Промокода «NOPE» нет", "Рекламной метки «nope» нет"]
    assert await kit.service.check_spec(Intent()) == ["Выберите цель или добавьте промокод"]


async def test_list_links_pages(kit: Kit) -> None:
    for i in range(12):
        await kit.service.create_link(Intent(topup=100 + i), title=f"L{i}", actor=None)
    first, total = await kit.service.list_links(offset=0, limit=10)
    assert total == 12 and [r.title for r in first][:2] == ["L11", "L10"]
    second, _ = await kit.service.list_links(offset=10, limit=10)
    assert [r.title for r in second] == ["L1", "L0"]
    again, _ = await kit.service.list_links(offset=100, limit=10)  # past the end → first page
    assert again[0].title == "L11"


async def test_stats_and_daily_aggregates(kit: Kit) -> None:
    link = await kit.service.create_link(Intent(screen="buy"), title="x", actor=None)
    a, b = await kit.user(150), await kit.user(151, is_new=False)
    clock.set_clock(NOW - timedelta(days=5))
    await kit.service.on_start(a, 150, "l_" + link.code)
    clock.set_clock(NOW)
    await kit.service.on_start(a, 150, "l_" + link.code)
    await kit.service.on_start(b, 151, "l_" + link.code)
    await kit.service.on_start(b, 151, "s_buy")
    stats = await kit.service.stats(link.id)
    assert stats.hits == (2, 3, 3)
    assert stats.users == (2, 2, 2)
    assert stats.new_users == (1, 1, 1)
    assert await kit.service.aggregate_day(NOW.date()) == 2
    assert await kit.service.aggregate_day(NOW.date()) == 2  # idempotent
    rows = await kit.db.raw("select link_key, link_id, hits, users, new_users from deeplink_daily order by 1")
    assert [tuple(r) for r in rows] == [(f"l:{link.id}", link.id, 2, 2, 1), ("s_buy", None, 1, 1, 0)]
    await kit.service.aggregate_recent()
    assert await kit.service.aggregate_day(date(2020, 1, 1)) == 0


async def test_plans_without_catalog(db: CountingDatabase) -> None:
    clock.set_clock(NOW)
    kit = build_kit(db, catalog=FakeCatalog([plan(5, "solo")]))
    kit.service.catalog = None
    u = await kit.user(160)
    assert await kit.service.on_start(u, 160, "p_solo") == ("buy", None)


async def test_channel_post_link_flow(kit: Kit) -> None:
    """07 stage 3b: ``l_`` with a promo from a channel → the gate → discount pending → hit recorded."""
    link = await kit.service.create_link(
        Intent(plan="std", promo="AUTUMN"), title="Канал", actor=await kit.actor()
    )
    gate = Gate()
    gate.closed = True
    u = await kit.user(170)
    assert await kit.service.on_start(u, 170, "l_" + link.code, gate=gate) == ("chan", None)
    assert [(h["link_id"], h["is_new"]) for h in await kit.hits()] == [(link.id, True)]
    gate.closed = False
    landing = await kit.service.resume(u)
    assert landing == Landing("buy_plan", "1", TEXTS["promo_pending"].format(code="AUTUMN"))
    assert kit.promo.calls == [(u.user_id, "AUTUMN")]
