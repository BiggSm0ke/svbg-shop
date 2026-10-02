"""LTE user UI (05 §2.1.3–2.1.4): slot lines and the pack button over one read model (≤ 1 SQL, 0 HTTP), the
«докупка» screens down to the core checkout, forged callback arguments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.ext.api import SlotCall, SlotResult
from svbg.ext.lte import ui
from svbg.ext.lte.decide import EffectiveLimit
from svbg.ext.lte.packs import Pack, PackFacts
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.view import Redirect, View
from tests.dbkit import SqlCounter
from tests.ext.lte.rkit import GB, lte_env

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USER = UserCtx(user_id=1)


@dataclass
class Module:
    cfg: dict[str, Any] = field(
        default_factory=lambda: {"LTE_ENABLED": True, "LTE_ENFORCE": "on", "LTE_TOPUP_ENABLED": True}
    )

    def config(self) -> dict[str, Any]:
        return self.cfg


def facts(**kw: Any) -> PackFacts:
    base: dict[str, Any] = {
        "subscription_id": 1,
        "user_id": 1,
        "group_id": 1,
        "group_name": {"ru": "LTE"},
        "group_state": "active",
        "group_enforce": True,
        "panel_user_id": 7,
        "paid_until": NOW + timedelta(days=20),
        "period_id": 3,
        "period_state": "open",
        "planned_end_at": datetime(2026, 10, 6, 21, 0, tzinfo=UTC),
        "anchor_kind": "paid",
        "rights": True,
        "used_bytes": 12_400_000_000,
        "limit": EffectiveLimit(found=True, base=50 * GB),
        "packs": (Pack(1, 10, 10_000, "RUB"),),
    }
    base.update(kw)
    return PackFacts(**base)


def call(*items: PackFacts, module: Module | None = None) -> SlotCall:
    return SlotCall(USER, {}, ui.UiModel(items, NOW), module or Module())  # type: ignore[arg-type]


def lines(result: SlotResult | None) -> list[str]:
    return list(result.lines) if result is not None else []


def test_status_line_and_subscription_block() -> None:
    assert lines(ui.render_status_line(call(facts()))) == ["🌐 LTE: 12,4 / 50 ГБ"]
    assert lines(ui.render_blocks(call(facts()))) == ["🌐 <b>Трафик LTE</b>", "└ 12,4 из 50 ГБ · сброс 07.10"]
    bought = facts(credit_bytes=10 * GB, limit=EffectiveLimit(found=True, base=50 * GB, credits=10 * GB))
    assert lines(ui.render_blocks(call(bought)))[1] == "└ 12,4 из 60 ГБ · сброс 07.10 (+10 ГБ докуплено)"
    warn = facts(used_bytes=45 * GB)
    assert lines(ui.render_blocks(call(warn)))[1].startswith("└ ⚠️ 45 из 50 ГБ")


@pytest.mark.parametrize(
    ("kw", "line"),
    [
        ({"block_reason": "quota", "block_status": "active"}, "└ 🚫 лимит исчерпан · доступ вернётся 07.10"),
        (
            {"block_reason": "quota", "block_status": "active", "period_state": "deferred"},
            "└ 🚫 лимит исчерпан · доступ вернётся после продления",
        ),
        (
            {"limit": EffectiveLimit(found=True, base=0), "period_is_trial": True},
            "└ 🔒 недоступно на пробном периоде",
        ),
        ({"exempt": True}, "└ ♾ без ограничения · израсходовано 12,4 ГБ"),
    ],
)
def test_block_variants(kw: dict[str, Any], line: str) -> None:
    assert lines(ui.render_blocks(call(facts(**kw))))[1] == line


@pytest.mark.parametrize(
    ("kw", "cfg"),
    [
        ({}, {"LTE_ENFORCE": "shadow"}),  # the shadow is invisible to users
        ({"group_enforce": False}, {}),
        ({"frozen": True}, {}),  # IP Guard: no LTE line at all
        ({"rights": False}, {}),
        ({"period_id": None}, {}),
        ({}, {"LTE_ENFORCE_LIST": [99]}),  # outside the pilot
    ],
)
def test_hidden(kw: dict[str, Any], cfg: dict[str, Any]) -> None:
    module = Module()
    module.cfg.update(cfg)
    assert ui.render_status_line(call(facts(**kw), module=module)) is None
    assert ui.render_blocks(call(facts(**kw), module=module)) is None


def test_status_line_hides_exempt_zero_and_unlimited_but_shows_numbers_when_blocked() -> None:
    for kw in (
        {"exempt": True},
        {"limit": EffectiveLimit(found=True, base=0)},
        {"limit": EffectiveLimit(True, None)},
    ):
        assert ui.render_status_line(call(facts(**kw))) is None
    blocked = facts(used_bytes=51 * GB, block_reason="quota", block_status="active")
    assert lines(ui.render_status_line(call(blocked))) == ["🌐 LTE: 51 / 50 ГБ"]


def test_pack_button_only_when_needed_and_available() -> None:
    assert ui.render_buttons(call(facts())) is None  # 25 %: no button
    warn = ui.render_buttons(call(facts(used_bytes=41 * GB)))
    assert warn is not None
    (button,) = warn.buttons
    assert button.action == ui.ACTION_TOPUP and button.after == "connect" and button.style == "success"
    blocked = facts(used_bytes=60 * GB, block_reason="quota", block_status="active")
    assert ui.render_buttons(call(blocked)) is not None
    off = Module()
    off.cfg["LTE_TOPUP_ENABLED"] = False
    assert ui.render_buttons(call(blocked, module=off)) is None
    assert ui.render_buttons(call(facts(used_bytes=41 * GB, packs=()))) is None
    trial = facts(used_bytes=41 * GB, period_is_trial=True)
    assert ui.render_buttons(call(trial)) is None


def test_renderers_survive_a_missing_model_or_broken_config() -> None:
    class Broken:
        def config(self) -> Any:
            raise RuntimeError("settings not loaded")

    empty = SlotCall(USER, {}, None, Module())  # type: ignore[arg-type]
    assert ui.render_status_line(empty) is None and ui.render_buttons(empty) is None
    broken = SlotCall(USER, {}, ui.UiModel((facts(),), NOW), Broken())  # type: ignore[arg-type]
    assert ui.render_blocks(broken) is None  # defaults: enforcement off → hidden, never an exception


# ----------------------------------------------------------------------------------------- the screens


@dataclass
class Router:
    screens: dict[str, Any] = field(default_factory=dict)
    actions: dict[tuple[str, str], Any] = field(default_factory=dict)
    forms: dict[str, Any] = field(default_factory=dict)

    def screen(self, code: str, **kw: Any) -> Any:
        def deco(fn: Any) -> Any:
            self.screens[code] = fn
            return fn

        return deco

    def action(self, screen: str, action: str, **kw: Any) -> Any:
        def deco(fn: Any) -> Any:
            self.actions[(screen, action)] = fn
            return fn

        return deco

    def form(self, form: Any) -> Any:
        self.forms[form.name] = form
        return form


@dataclass
class Ctx:
    user: UserCtx
    lang: str = "ru"
    started: list[tuple[str, Any]] = field(default_factory=list)

    async def start_form(self, name: str, initial: Any = None) -> str:
        self.started.append((name, initial))
        return f"form:{name}"


def buttons(view: View) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data or b.url or "") for row in view.keyboard or () for b in row]  # type: ignore[union-attr]


@pytest.mark.pg
async def test_load_view_is_one_query(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(601)
        await env.open_period(sid, used=3 * GB)
        uid = await env.user_of(sid)
        with SqlCounter(env.db.engine) as counter:
            async with env.db.read() as conn:
                model = await ui.load_view(conn, UserCtx(user_id=uid), {})
        assert counter.count == 1, counter.recent
        assert model is not None and model.facts[0].used_bytes == 3 * GB
        async with env.db.read() as conn:
            assert await ui.load_view(conn, UserCtx(user_id=uid + 1000), {}) is None
            assert await ui.load_view(conn, object(), {}) is None


@pytest.mark.pg
async def test_topup_flow_ends_in_the_core_checkout(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        p10 = await env.add_pack(10, 9_900)
        p50 = await env.add_pack(50, 39_000)
        sid = await env.linked_sub(602)
        await env.open_period(sid, used=22 * GB)
        await env.service.process_subscription(sid)  # blocked: over by 12 GB
        uid = await env.user_of(sid)
        router = Router()
        ui.install(router, lambda: env.service)
        ctx = Ctx(UserCtx(user_id=uid))
        with SqlCounter(env.db.engine) as counter:
            view = await router.screens[ui.SCREEN_TOPUP](ctx, None)
        assert counter.count <= 2, counter.recent
        assert "превышен на 12 ГБ" in view.text
        labels = [text for text, _ in buttons(view)]
        assert labels[0].startswith("✅ +50 ГБ") and not any(t.startswith("+10") for t in labels)
        # the small pack is not enough: a warning and «Всё равно купить»
        confirm = await router.actions[(ui.SCREEN_TOPUP, "pick")](ctx, f"{env.group_id}:{p10}")
        assert "не хватит" in confirm.text and "Сгорит" in confirm.text
        assert buttons(confirm)[0][1].endswith(f"{env.group_id}:{p10}:1")
        ok = await router.actions[(ui.SCREEN_TOPUP, "pick")](ctx, f"{env.group_id}:{p50}")
        assert "не хватит" not in ok.text
        done = await router.actions[(ui.SCREEN_TOPUP, "buy")](ctx, f"{env.group_id}:{p50}:0")
        assert isinstance(done, Redirect) and done.screen == "co"
        rows = await env.db.raw("select kind, status, user_id from orders where id = $1", int(done.arg))
        assert (rows[0]["kind"], rows[0]["status"], rows[0]["user_id"]) == ("addon_lte", "draft", uid)
        assert isinstance(await router.actions[("mod", ui.ACTION_TOPUP)](ctx, None), Redirect)


@pytest.mark.pg
@pytest.mark.parametrize("arg", ["x", "1:2:3:4", "-1", "1:a", "1" * 20])
async def test_forged_arguments_are_rejected(pg_dsn: str, arg: str) -> None:
    async with lte_env(pg_dsn) as env:
        router = Router()
        ui.install(router, lambda: env.service)
        ctx = Ctx(UserCtx(user_id=1))
        for key in ((ui.SCREEN_TOPUP, "pick"), (ui.SCREEN_TOPUP, "buy")):
            res = await router.actions[key](ctx, arg)
            assert isinstance(res, Redirect)
        assert await env.db.raw("select 1 from orders") == []


@pytest.mark.pg
async def test_other_users_pack_and_refusal_texts(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        p10 = await env.add_pack(10, 9_900)
        sid = await env.linked_sub(603)
        await env.open_period(sid, used=1 * GB, is_trial=True, anchor_kind="trial")
        router = Router()
        ui.install(router, lambda: env.service)
        ctx = Ctx(UserCtx(user_id=await env.user_of(sid)))
        view = await router.screens[ui.SCREEN_TOPUP](ctx, None)
        assert "пробном" in view.text
        stranger = Ctx(UserCtx(user_id=424242))
        res = await router.actions[(ui.SCREEN_TOPUP, "buy")](stranger, f"{env.group_id}:{p10}:0")
        assert isinstance(res, View) and "оплаченной подписки" in res.text
        assert await env.db.raw("select 1 from orders") == []
