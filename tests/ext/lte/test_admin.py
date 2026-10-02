"""LTE minimal admin (05 §2.1.6): rights in the service, server-side refusals, audit, limits with a preview,
the user card section and the screens (forged arguments, code word)."""

from __future__ import annotations

from typing import Any

import pytest

from svbg.ext.api import SlotCall
from svbg.ext.lte import admin as a
from svbg.ext.lte.admin import LteAdmin, can, parse_limit
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.forms import ValidationError
from svbg.tg.ui.view import Toast, View
from tests.ext.lte.rkit import GB, LteEnv, lte_env
from tests.ext.lte.test_ui import Ctx, Module, Router, buttons

OWNER = UserCtx(user_id=1, role="owner")
ADMIN = UserCtx(user_id=2, role="admin", perms=frozenset({"lte.users", "lte.view"}))
STAR = UserCtx(user_id=3, role="admin", perms=frozenset({"*"}))
SUPPORT = UserCtx(user_id=4, role="support")
USER = UserCtx(user_id=5)


@pytest.mark.parametrize(
    ("user", "view", "users", "config"),
    [
        (OWNER, True, True, True),
        (ADMIN, True, True, False),
        (STAR, True, True, False),  # «*» never grants the owner's configuration
        (UserCtx(user_id=6, role="admin", perms=frozenset({"lte.config"})), True, False, True),
        (SUPPORT, True, False, False),
        (USER, False, False, False),
    ],
)
def test_rights_matrix(user: UserCtx, view: bool, users: bool, config: bool) -> None:
    assert (can(user, a.PERM_VIEW), can(user, a.PERM_USERS), can(user, a.PERM_CONFIG)) == (
        view,
        users,
        config,
    )


def test_parse_limit() -> None:
    assert parse_limit("50") == 50 and parse_limit(" 0 ") == 0
    assert parse_limit("∞") is None and parse_limit("без лимита") is None
    for bad in ("-5", "1.5", "abc", "9999999"):
        with pytest.raises(ValidationError):
            parse_limit(bad)


async def _blocked(env: LteEnv, tg: int, used: int = 11 * GB) -> int:
    sid = await env.linked_sub(tg)
    await env.open_period(sid, used=used)
    await env.service.process_subscription(sid)
    await env.drain()
    return sid


@pytest.mark.pg
async def test_add_gb_releases_and_respects_the_admin_cap(pg_dsn: str) -> None:
    async with lte_env(pg_dsn, LTE_ADMIN_GB_MAX=5) as env:
        sid = await _blocked(env, 701)
        adm = LteAdmin(env.service)
        assert not (await adm.add_gb(sid, 6, actor=ADMIN)).ok  # above the cap: owner only
        assert not (await adm.add_gb(sid, 2, actor=SUPPORT)).ok
        res = await adm.add_gb(sid, 2, actor=ADMIN)
        assert res.ok and "Блок снят" in res.text
        await env.drain()
        assert await env.panel_squads(sid) == [env.base]
        assert (await adm.add_gb(sid, 6, actor=OWNER)).ok
        credits = await env.db.raw("select bytes, source from lte_credits order by id")
        assert [(c["bytes"], c["source"]) for c in credits] == [(2 * GB, "admin"), (6 * GB, "admin")]
        audit = await env.db.raw("select actor_id, details from admin_audit where action = 'lte.add_gb'")
        assert [r["actor_id"] for r in audit] == [ADMIN.user_id, OWNER.user_id]
        assert all(r["details"]["domain"] == "lte" for r in audit)
        assert not (await adm.add_gb(sid, 0, actor=OWNER)).ok
        assert not (await adm.add_gb(999_999, 1, actor=OWNER)).ok


@pytest.mark.pg
async def test_exempt_subscription_gets_no_manual_block_and_no_gb(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _blocked(env, 702)
        adm = LteAdmin(env.service)
        res = await adm.set_exempt(sid, True, actor=ADMIN)
        assert res.ok
        (block,) = await env.blocks(sid)
        assert block["status"] == "releasing" and block["release_reason"] == "exempt"
        assert (await adm.set_exempt(sid, True, actor=ADMIN)).text == a.T["exempt_same"]
        assert not (await adm.block(sid, actor=ADMIN)).ok
        assert not (await adm.add_gb(sid, 1, actor=ADMIN)).ok
        assert (await adm.set_exempt(sid, False, actor=ADMIN)).ok


@pytest.mark.pg
async def test_manual_block_and_unblock_until_reset(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(703)
        await env.open_period(sid, used=1 * GB)
        adm = LteAdmin(env.service)
        res = await adm.block(sid, actor=ADMIN)
        assert res.ok, res.text
        assert (await adm.block(sid, actor=ADMIN)).text == a.T["block_exists"]
        await env.drain()
        assert await env.panel_squads(sid) == [env.twin]
        # a manual block is not released by the cycle although usage is low
        await env.service.process_subscription(sid)
        (block,) = await env.blocks(sid)
        assert block["status"] == "active" and block["reason"] == "manual"
        res = await adm.unblock(sid, actor=ADMIN)
        assert res.ok and "снят" in res.text
        await env.drain()
        assert await env.panel_squads(sid) == [env.base]
        # no new block until the reset even over the limit
        await env.db.raw("update lte_period_usage set used_bytes = $1", 20 * GB)
        await env.service.process_subscription(sid)
        assert [b["status"] for b in await env.blocks(sid)] == ["released"]


@pytest.mark.pg
async def test_manual_block_needs_enforcement_and_a_period(pg_dsn: str) -> None:
    async with lte_env(pg_dsn, LTE_ENFORCE="shadow") as env:
        sid = await env.linked_sub(704)
        adm = LteAdmin(env.service)
        assert "тень" in (await adm.block(sid, actor=ADMIN)).text
        env.config["LTE_ENFORCE"] = "on"
        assert (await adm.block(sid, actor=ADMIN)).text == a.T["no_period"]
        assert (await adm.unblock(sid, actor=ADMIN)).text == a.T["no_period"]


@pytest.mark.pg
async def test_limit_change_has_a_preview_and_an_optimistic_lock(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        for i, used in enumerate((3 * GB, 8 * GB, 12 * GB)):
            sid = await env.linked_sub(710 + i)
            await env.open_period(sid, used=used)
        adm = LteAdmin(env.service)
        pv = await adm.preview_limit(env.group_id, "default", 5)
        assert pv is not None and pv.blocked_now == 0 and pv.new_blocks == 2
        assert [r.action for r in pv.sample] == ["block", "block"]
        assert await adm.preview_limit(999, "default", 5) is None
        rows = await env.db.raw("select version from lte_groups where id = $1", env.group_id)
        version = rows[0]["version"]
        assert not (await adm.apply_limit(env.group_id, "default", 5, version, actor=ADMIN)).ok  # owner only
        assert (await adm.apply_limit(env.group_id, "default", 5, version, actor=OWNER)).ok
        assert (await adm.apply_limit(env.group_id, "default", 7, version, actor=OWNER)).text == a.T[
            "limit_stale"
        ]
        assert (await adm.apply_limit(env.group_id, "trial", None, version + 1, actor=OWNER)).ok
        g = (await env.db.raw("select * from lte_groups where id = $1", env.group_id))[0]
        assert g["limit_default_bytes"] == 5 * GB and g["has_trial"] and g["limit_trial_bytes"] is None


@pytest.mark.pg
async def test_release_all_and_settings_need_the_owner(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _blocked(env, 720)
        adm = LteAdmin(env.service)
        assert not (await adm.release_all(actor=STAR)).ok
        assert (await adm.set_mode("on", actor=OWNER)).text == a.T["no_settings"]
        assert not (await adm.set_mode("weird", actor=OWNER)).ok
        res = await adm.release_all(actor=OWNER)
        assert res.ok and "1" in res.text
        await env.drain()
        assert await env.panel_squads(sid) == [env.base]


@pytest.mark.pg
async def test_user_card_section(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _blocked(env, 730)
        uid = await env.user_of(sid)
        async with env.db.read() as conn:
            card = await a.load_card(conn, OWNER, {"user_id": uid})
            assert await a.load_card(conn, OWNER, {"user_id": "x"}) is None
            assert await a.load_card(conn, OWNER, {}) is None
        assert card is not None
        result = a.render_card(SlotCall(OWNER, {}, card, Module()))  # type: ignore[arg-type]
        assert result is not None
        assert result.lines[0].startswith("🌐 <b>Квота LTE</b>: 11 из 10 ГБ (база 10 + пакеты 0 − запас 0)")
        assert result.lines[1] == "🚫 блок: лимит"
        assert [b.action for b in result.buttons] == ["lte.gb", "lte.unblock", "lte.exon"]
        assert all(b.perm == a.PERM_USERS and b.arg == str(sid) for b in result.buttons)
        assert a.render_card(SlotCall(OWNER, {}, None, Module())) is None  # type: ignore[arg-type]


@pytest.mark.pg
async def test_screens_and_actions(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _blocked(env, 740)
        router = Router()
        a.install(router, lambda: env.service)
        view = await router.screens[a.SCREEN](Ctx(OWNER), None)
        assert "Заблокировано: 1" in view.text
        labels = [t for t, _ in buttons(view)]
        assert "⛔ Снять все блоки LTE" in labels and any(t.startswith("Группа") for t in labels)
        plain = await router.screens[a.SCREEN](Ctx(ADMIN), None)
        assert "⛔ Снять все блоки LTE" not in [t for t, _ in buttons(plain)]
        group = await router.screens[a.SCREEN_GROUP](Ctx(OWNER), str(env.group_id))
        assert "Лимит: 10 ГБ" in group.text and "Двойник:" in group.text
        # the code word form, the limit form
        ctx = Ctx(OWNER)
        assert await router.actions[(a.SCREEN, "rall")](ctx, None) == f"form:{a.FORM_RELEASE}"
        check = router.forms[a.FORM_RELEASE].fields[0].validator
        with pytest.raises(ValidationError):
            check("да")
        assert check("снять") == a.CODE_WORD
        done = await router.forms[a.FORM_RELEASE].on_done(ctx, {"code": a.CODE_WORD})
        assert isinstance(done, View) and "Сняты все блоки" in done.text
        assert isinstance(await router.actions[(a.SCREEN, "rall")](Ctx(ADMIN), None), Toast)
        await router.actions[(a.SCREEN_GROUP, "lim")](ctx, f"{env.group_id}:d:1")
        assert ctx.started[-1] == (a.FORM_LIMIT, {"gid": env.group_id, "which": "d", "ver": 1})
        preview = await router.forms[a.FORM_LIMIT].on_done(
            ctx, {"gid": env.group_id, "which": "d", "ver": 1, "value": 20}
        )
        assert "Превью" in preview.text and buttons(preview)[0][1].endswith(f"{env.group_id}:d:20:1")
        applied = await router.actions[(a.SCREEN_GROUP, "apply")](ctx, f"{env.group_id}:d:20:1")
        assert "Лимит сохранён" in applied.text
        # user card actions: rights and forged ids
        support = Ctx(SUPPORT)
        res = await router.actions[("mod", "lte.unblock")](support, str(sid))
        assert isinstance(res, Toast) and res.text == a.T["denied"]
        res = await router.actions[("mod", "lte.unblock")](Ctx(ADMIN), "1; drop")
        assert isinstance(res, Toast) and res.text == a.T["bad"]
        gb = Ctx(ADMIN)
        assert await router.actions[("mod", "lte.gb")](gb, str(sid)) == f"form:{a.FORM_GB}"
        done = await router.forms[a.FORM_GB].on_done(gb, {"sid": sid, "gb": 1})
        assert "Добавлено 1 ГБ" in done.text
        for bad in ("x", "1:x:2:3", f"{env.group_id}:q:1:1"):
            res: Any = await router.actions[(a.SCREEN_GROUP, "apply")](ctx, bad)
            assert not isinstance(res, View)


@pytest.mark.pg
async def test_card_actions_ask_the_admin_for_a_reason(pg_dsn: str) -> None:
    """Review: «+ГБ» and «Снять блок» write the reason the admin typed, not a hard-coded one."""
    async with lte_env(pg_dsn) as env:
        sid = await _blocked(env, 741)
        router = Router()
        a.install(router, lambda: env.service)
        ctx = Ctx(ADMIN)
        assert await router.actions[("mod", "lte.unblock")](ctx, str(sid)) == f"form:{a.FORM_UNBLOCK}"
        assert ctx.started[-1] == (a.FORM_UNBLOCK, {"sid": sid})
        assert [f.name for f in router.forms[a.FORM_GB].fields] == ["gb", "reason"]
        check = router.forms[a.FORM_UNBLOCK].fields[0].validator
        with pytest.raises(ValidationError):
            check("  ")
        done = await router.forms[a.FORM_UNBLOCK].on_done(ctx, {"sid": sid, "reason": "жалоба  в поддержку"})
        assert isinstance(done, View) and "снят" in done.text
        gb = await router.forms[a.FORM_GB].on_done(ctx, {"sid": sid, "gb": 1, "reason": "компенсация сбоя"})
        assert "Добавлено 1 ГБ" in gb.text
        rows = await env.db.raw(
            "select action, reason from admin_audit where action like 'lte.%' order by id"
        )
        assert [(r["action"], r["reason"]) for r in rows] == [
            ("lte.unblock", "жалоба в поддержку"),
            ("lte.add_gb", "компенсация сбоя"),
        ]
        override = await env.db.raw("select reason from lte_overrides where kind = 'no_block'")
        assert [r["reason"] for r in override] == ["жалоба в поддержку"]
        # a forged press without the right never opens the form
        assert isinstance(await router.actions[("mod", "lte.unblock")](Ctx(SUPPORT), str(sid)), Toast)


def test_admin_home_entry_only_for_admins() -> None:
    entry = a.render_home_entry(SlotCall(ADMIN, {}, None, Module()))  # type: ignore[arg-type]
    assert entry is not None and entry.buttons[0].action == "lte.open"
    assert a.render_home_entry(SlotCall(SUPPORT, {}, None, Module())) is None  # type: ignore[arg-type]
