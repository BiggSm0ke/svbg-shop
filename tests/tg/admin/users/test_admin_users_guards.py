"""Review fixes of the admin user card: no grants to oneself, 24 h totals per admin, admin days seen by the
LTE engine as «admin», module rights granted through the role editor (never by «все права»)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from svbg.core.clock import now
from svbg.ext.ip_guard.tg import PERM_CONFIG as IPG_CONFIG
from svbg.ext.ip_guard.tg import PERM_UNBLOCK as IPG_UNBLOCK
from svbg.ext.ip_guard.tg import CardActions
from svbg.ext.lte import admin as lte_admin
from svbg.ext.lte import periods
from svbg.ext.lte.service import engine_event
from svbg.services import roles
from svbg.services.roles import ADMIN_PERMS, RoleError
from svbg.tg.admin import deeplinks
from svbg.tg.admin.roles import ACTIONS as ROLE_ACTIONS
from svbg.tg.admin.roles import MODULE_BIT0, SCREEN_EDIT, catalogue, mask_of
from svbg.tg.admin.users.screens import SCREEN_CARD
from svbg.tg.ui.codec import encode
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.view import View
from tests.catalog.kit import add_plan
from tests.dbkit import CountingDatabase
from tests.ext.lte.test_periods import state_at
from tests.tg.admin.users.kit import ADMIN, OTHER, OWNER, USER, UEnv, add_sub, build_uenv

MODULES = (
    ("ip_guard.view", "IP Guard: просмотр"),
    ("ip_guard.unblock", "IP Guard: снять блок"),
    ("ip_guard.config", "IP Guard: настройки"),
    ("lte.config", "LTE: настройки"),
)


async def audit_actions(env: UEnv) -> list[str]:
    return [r["action"] for r in await env.audit()]


# ------------------------------------------------------------------------------------------- self


async def test_admin_cannot_grant_days_plans_or_money_to_themselves(env: UEnv) -> None:
    me = env.ids[ADMIN]
    await add_sub(env.db, me, days=5)
    std = await add_plan(env.db, "std", prices=((30, 17900),))
    await env.catalog.reload()
    for result in (
        await env.ops.grant_days(ADMIN, me, 3, "себе на тест", "s1" * 16),
        await env.ops.give_plan(ADMIN, me, std, 3, "себе на тест", "s2" * 16),
        await env.ops.adjust_wallet(ADMIN, me, 100, "себе на тест", "s3" * 16),
    ):
        assert not result.ok and "Себе" in result.text
    assert await audit_actions(env) == []
    # the owner may (the owner is not limited at all)
    owner = env.ids[OWNER]
    await add_sub(env.db, owner, days=5)
    assert (await env.ops.grant_days(OWNER, owner, 3, "себе на тест", "s4" * 16)).ok


async def test_own_card_has_no_money_buttons(env: UEnv) -> None:
    await add_sub(env.db, env.ids[ADMIN], days=5)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(env.ids[ADMIN])))
    labels = " ".join(env.labels())
    assert "➕ Дни" not in labels and "Выдать тариф" not in labels and "💰 Баланс" not in labels
    await add_sub(env.db, env.ids[USER], days=5)
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(env.ids[USER])))
    labels = " ".join(env.labels())
    assert "➕ Дни" in labels and "Выдать тариф" in labels and "💰 Баланс" in labels
    await add_sub(env.db, env.ids[OWNER], days=5)
    await env.click(OWNER, encode(SCREEN_CARD, arg=str(env.ids[OWNER])))
    assert "➕ Дни" in " ".join(env.labels())


# ------------------------------------------------------------------------------------------- 24 h totals


async def test_days_have_a_rolling_daily_total_per_admin(env: UEnv) -> None:
    other = await env.add(OTHER)
    await add_sub(env.db, env.ids[USER], days=5)
    await add_sub(env.db, other, days=5)
    env.settings["ADMIN_GRANT_DAYS_DAY_MAX"] = 40
    assert (await env.ops.grant_days(ADMIN, env.ids[USER], 31, "компенсация", "d1" * 16)).ok
    # a new op_id per submit no longer resets anything; both signs count; any target counts
    refused = await env.ops.grant_days(ADMIN, other, 10, "компенсация", "d2" * 16)
    assert not refused.ok and "осталось 9 дн." in refused.text
    assert (await env.ops.grant_days(ADMIN, other, -9, "ошибка", "d3" * 16)).ok
    refused = await env.ops.grant_days(ADMIN, other, 1, "компенсация", "d4" * 16)
    assert not refused.ok and "осталось 0 дн." in refused.text
    # plans count too
    std = await add_plan(env.db, "std", prices=((30, 17900),))
    await env.catalog.reload()
    assert not (await env.ops.give_plan(ADMIN, other, std, 1, "подарок", "d5" * 16)).ok
    # the owner is not limited
    assert (await env.ops.grant_days(OWNER, other, 300, "подарок", "d6" * 16)).ok
    # a day later the window has moved
    await env.db.raw(
        "update admin_audit set ts = ts - interval '25 hours' where actor_id = $1", env.ids[ADMIN]
    )
    assert (await env.ops.grant_days(ADMIN, other, 30, "компенсация", "d7" * 16)).ok
    assert (await env.ops.give_plan(ADMIN, other, std, 10, "подарок", "d8" * 16)).ok


async def test_default_daily_days_total_is_three_maximal_grants(env: UEnv) -> None:
    await add_sub(env.db, env.ids[USER], days=5)
    for i in range(3):
        assert (await env.ops.grant_days(ADMIN, env.ids[USER], 31, "компенсация", f"e{i}" * 16)).ok
    refused = await env.ops.grant_days(ADMIN, env.ids[USER], 1, "компенсация", "e9" * 16)
    assert not refused.ok and "Лимит на сутки" in refused.text


async def test_wallet_has_a_rolling_daily_total_per_admin(env: UEnv) -> None:
    uid = env.ids[USER]
    env.settings["ADMIN_WALLET_ADJUST_MAX"] = 100  # whole rubles per operation
    env.settings["ADMIN_WALLET_ADJUST_DAY_MAX"] = 150  # whole rubles per 24 h
    assert (await env.ops.adjust_wallet(ADMIN, uid, 10_000, "компенсация", "w1" * 16)).ok
    assert (await env.ops.adjust_wallet(ADMIN, uid, -5_000, "ошибка", "w2" * 16)).ok
    refused = await env.ops.adjust_wallet(ADMIN, uid, 1, "компенсация", "w3" * 16)
    assert not refused.ok and "Суточный лимит" in refused.text
    assert (await env.ops.adjust_wallet(OWNER, uid, 1_000_000, "бонус", "w4" * 16)).ok
    del env.settings["ADMIN_WALLET_ADJUST_DAY_MAX"]  # default: 3 × the per-operation limit
    assert (await env.ops.adjust_wallet(ADMIN, uid, 10_000, "компенсация", "w5" * 16)).ok
    assert (await env.ops.adjust_wallet(ADMIN, uid, 5_000, "компенсация", "w6" * 16)).ok
    assert not (await env.ops.adjust_wallet(ADMIN, uid, 1, "компенсация", "w7" * 16)).ok


async def test_parallel_submits_cannot_both_pass_the_daily_total(env: UEnv) -> None:
    other = await env.add(OTHER)
    await add_sub(env.db, env.ids[USER], days=5)
    await add_sub(env.db, other, days=5)
    env.settings["ADMIN_GRANT_DAYS_DAY_MAX"] = 20
    results = await asyncio.gather(
        env.ops.grant_days(ADMIN, env.ids[USER], 15, "компенсация", "p1" * 16),
        env.ops.grant_days(ADMIN, other, 15, "компенсация", "p2" * 16),
    )
    assert sorted(r.ok for r in results) == [False, True]
    assert (await audit_actions(env)).count("subs.grant") == 1


def test_limits_from_settings_daily_keys() -> None:
    lim = roles.Limits.from_settings(
        {"ADMIN_GRANT_DAYS_DAY_MAX": 50, "ADMIN_WALLET_ADJUST_DAY_MAX": 700},
        currency_exponent=2,
        max_plan_price_minor=1000,
    )
    assert lim.grant_days_per_day == 50 and lim.wallet_adjust_per_day == 70_000
    lim = roles.Limits.from_settings(
        {"ADMIN_GRANT_DAYS_DAY_MAX": -1, "ADMIN_WALLET_ADJUST_DAY_MAX": "x"},
        currency_exponent=2,
        max_plan_price_minor=1000,
    )
    assert lim.grant_days_per_day == 93 and lim.wallet_adjust_per_day == 3000
    assert roles.Limits(31, None, wallet_adjust_day_max_minor=5).wallet_adjust_per_day is None


# ------------------------------------------------------------------------------------------- LTE kind


async def test_admin_days_on_a_trial_are_an_admin_event_for_lte(env: UEnv) -> None:
    sid = await add_sub(env.db, env.ids[USER], days=3, is_trial=True)
    assert (await env.ops.grant_days(ADMIN, env.ids[USER], 30, "продлили триал", "l1" * 16)).ok
    row = (await env.db.raw("select * from subscription_events where subscription_id = $1", sid))[0]
    row = dict(row)
    if isinstance(row["details"], str):
        row["details"] = json.loads(row["details"])
    event = engine_event(row)
    assert event is not None and event.kind == "admin" and event.is_trial is True
    started = now() - timedelta(days=4)
    state = state_at(started, kind="trial", coverage_end=row["old_expire"], is_trial=True, at=started)
    out = periods.apply(state, event)
    assert "E4" in out.rules and out.state.anchor_kind == "admin"
    assert out.state.is_trial is True
    assert not out.actions_of("revoke_exemption")
    assert not out.actions_of("needs_review")


# ------------------------------------------------------------------------------------------- rights


def test_core_column_has_the_07_rights_and_star_never_covers_modules() -> None:
    for perm in ("content.edit", "deeplinks", "tickets", "broadcast.send"):
        assert perm in ADMIN_PERMS
        assert roles.clean_perms("admin", [perm]) == [perm]
    modules = [code for code, _ in MODULES]
    assert roles.clean_perms("admin", ["*"], modules) == list(ADMIN_PERMS)
    assert roles.clean_perms("admin", ["*", "ip_guard.unblock"], modules) == [
        *ADMIN_PERMS,
        "ip_guard.unblock",
    ]
    try:
        roles.clean_perms("admin", ["ip_guard.unblock"])  # module not loaded
    except RoleError as e:
        assert "Неизвестные права" in e.text
    else:
        raise AssertionError("an unknown module right was accepted")
    star = roles.Actor(1, 1, "admin", frozenset({"*"}))
    assert star.has_perm("deeplinks") and star.has_perm("stats")
    assert not star.has_perm("ip_guard.unblock") and not star.has_perm("lte.config")
    assert roles.stored_perms(["*", "lte.config", "junk", 5]) == (*ADMIN_PERMS, "lte.config")


def test_editor_catalogue_bits() -> None:
    items = catalogue([*MODULES, ("ip_guard.view", "dup"), ("BAD", "x"), ("stats", "core twice")])
    modules = [(bit, code) for bit, code, _ in items if bit >= MODULE_BIT0]
    assert modules == [(MODULE_BIT0 + i, code) for i, (code, _) in enumerate(MODULES)]
    assert mask_of(["deeplinks", "ip_guard.unblock"], items) == (1 << ADMIN_PERMS.index("deeplinks")) | (
        1 << (MODULE_BIT0 + 1)
    )


async def test_owner_grants_deeplinks_and_ip_guard_unblock_and_the_screens_open(db: CountingDatabase) -> None:
    env = await build_uenv(db, module_perms=MODULES)
    uid = env.ids[USER]
    await env.click(OWNER, encode(SCREEN_EDIT, arg=str(uid)))
    await env.press(OWNER, "админ")
    assert "🧩 IP Guard: снять блок" in " ".join(env.labels())
    assert "▫️ 🧩 IP Guard: снять блок" in env.labels()  # modules are never on by default
    items = catalogue(MODULES)
    mask = mask_of(["deeplinks", "ip_guard.unblock"], items)
    await env.click(OWNER, encode(ROLE_ACTIONS, "save", f"{uid}:admin:{mask}"))
    assert "✅ Сохранено." in env.text
    row = (await env.db.raw("select role, perms from users where id = $1", uid))[0]
    perms = row["perms"] if isinstance(row["perms"], list) else json.loads(row["perms"])
    assert (row["role"], perms) == ("admin", ["deeplinks", "ip_guard.unblock"])
    audit = [r for r in await env.audit() if r["action"] == "role.set"][-1]
    details = audit["details"] if isinstance(audit["details"], dict) else json.loads(audit["details"])
    assert details["new_perms"] == ["deeplinks", "ip_guard.unblock"]

    # the link builder's router guard (required_role=admin, perm=deeplinks)
    user = UserCtx(uid, telegram_id=USER, role="admin", perms=frozenset(perms))
    assert user.at_least("admin") and user.has_perm(deeplinks.PERM)
    env.users.by_tg[USER] = user
    opened: list[str] = []

    async def probe(_ctx: object, _arg: object) -> View:
        opened.append("links")
        return View(text="links")

    env.router.screen("dl_probe", required_role="admin", perm=deeplinks.PERM)(probe)
    await env.click(USER, encode("dl_probe"))
    assert opened == ["links"]

    # IP Guard re-reads the role from the database for every press
    actions = CardActions(lambda: None, env.db, env.directory.configured_async)  # type: ignore[arg-type,return-value]
    assert (await actions.allowed(USER, IPG_UNBLOCK))[0] is True
    assert (await actions.allowed(USER, IPG_CONFIG))[0] is False
    # an admin with «все права» stored as "*" never gets module rights
    await env.db.raw("update users set perms = '[\"*\"]'::jsonb where id = $1", env.ids[ADMIN])
    assert (await actions.allowed(ADMIN, IPG_UNBLOCK))[0] is False
    assert not lte_admin.can(
        UserCtx(env.ids[ADMIN], telegram_id=ADMIN, role="admin", perms=frozenset({"*"})), "lte.config"
    )
