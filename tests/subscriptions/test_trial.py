"""Trial: one per user / Telegram id, audience, catalog trial plan, rollback on refusal (02 §4.1, 06 M2)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from svbg.subscriptions.channel import ChannelService, Membership
from svbg.subscriptions.lifecycle import SubscriptionLifecycle
from svbg.subscriptions.trial import TRIAL_TEXTS, TrialResult, TrialService
from tests.subscriptions.kit import FakeCatalog, SyncEnv, frozen_clock, plan_row, sync_env

CHANNEL = -100_123


class Lookup:
    def __init__(self, status: str = "member", *, fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.calls = 0

    async def __call__(self, chat_id: int, telegram_id: int) -> Membership:
        self.calls += 1
        if self.fail:
            raise ConnectionError("telegram is down")
        return Membership(self.status, self.status in ("member", "creator", "administrator"))


def make(
    env: SyncEnv, cfg: dict[str, Any] | None = None, *, lookup: Lookup | None = None
) -> tuple[TrialService, FakeCatalog, dict[str, Any]]:
    config: dict[str, Any] = {"TRIAL_DAYS": 3, "TRIAL_AUDIENCE": "all", "REQUIRED_CHANNEL_ID": None}
    config.update(cfg or {})
    catalog = FakeCatalog(
        plan_row(env.squad, 7, is_trial=True, panel_tag="TRIAL", device_addon=None, code="trial")
    )

    def snapshot() -> Mapping[str, Any]:
        return config

    channel = ChannelService(env.db, config=snapshot, lookup=lookup)
    return TrialService(env.db, catalog, config=snapshot, channel=channel), catalog, config


async def counts(env: SyncEnv) -> tuple[int, int, int]:
    subs = await env.db.raw("select count(*) as n from subscriptions")
    grants = await env.db.raw("select count(*) as n from trial_grants")
    jobs = await env.db.raw("select count(*) as n from jobs")
    return subs[0]["n"], grants[0]["n"], jobs[0]["n"]


async def test_trial_activation_end_to_end(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        trial, _, _ = make(env)
        with frozen_clock() as clock:
            uid = await env.add_user(800)
            assert (await trial.check(uid)).ok
            res = await trial.activate(uid, caused_by="tg:800")
            assert res.ok and res.days == 3 and res.paid_until == clock() + timedelta(days=3)
            await env.drain()
            row = await env.sub(res.subscription_id or 0)
            assert row["is_trial"] and row["link_state"] == "linked" and row["plan_id"] == 7
            assert row["plan_snapshot"]["is_trial"] is True
            user = env.panel_user(row["panel_user_id"])
            assert user["expireAt"] == clock() + timedelta(days=3)
            assert user["tag"] == "TRIAL" and user["hwidDeviceLimit"] == 5 and user["telegramId"] == 800
            grant = (await env.db.raw("select * from trial_grants"))[0]
            assert grant["user_id"] == uid and grant["telegram_id"] == 800
            assert grant["subscription_id"] == res.subscription_id
            ev = await env.db.raw("select kind, ref_type from subscription_events order by id")
            assert (ev[0]["kind"], ev[0]["ref_type"]) == ("trial_started", "trial")
            assert any(e.name == "trial.activated" for e in env.events)
            again = await trial.activate(uid)
            assert not again.ok and again.reason == "used" and again.text == TRIAL_TEXTS["used"]


async def test_trial_refusals_leave_nothing_behind(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        trial, catalog, cfg = make(env)
        uid = await env.add_user(801)
        cfg["TRIAL_DAYS"] = 0
        assert (await trial.activate(uid)).reason == "disabled"
        cfg["TRIAL_DAYS"] = 3
        catalog.trial = None
        assert (await trial.activate(uid)).reason == "no_plan"
        catalog.trial = {"squads": "not-a-list"}
        assert (await trial.activate(uid)).reason == "no_plan"
        assert await counts(env) == (0, 0, 0)  # the created subscription was rolled back
        assert (await trial.activate(10**9)).reason == "no_user"
        await env.db.raw("update users set banned_at = now() where id = $1", uid)
        assert (await trial.activate(uid)).reason == "banned"


async def test_one_trial_per_telegram_id_and_not_for_customers(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        trial, _, _ = make(env)
        # Imported from Bedolaga: this Telegram account already had its trial (no bot user yet).
        await env.db.raw("insert into trial_grants (telegram_id, source) values (802, 'import')")
        uid = await env.add_user(802)
        assert (await trial.check(uid)).reason == "used"
        buyer = await env.add_user(803)
        async with env.db.tx() as conn:
            await SubscriptionLifecycle().purchase(
                conn, user_id=buyer, terms=plan_row(env.squad), days=30, ref_id="o1"
            )
        res = await trial.activate(buyer)
        assert res.reason == "has_subscription" and "уже есть подписка" in res.text


async def test_concurrent_clicks_give_one_trial(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        trial, _, _ = make(env)
        uid = await env.add_user(804)
        results: list[TrialResult] = await asyncio.gather(*(trial.activate(uid) for _ in range(5)))
        assert sum(r.ok for r in results) == 1
        assert {r.reason for r in results if not r.ok} <= {"used", "has_subscription"}
        assert (await counts(env))[:2] == (1, 1)


async def test_channel_audience(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        lookup = Lookup("left")
        trial, _, cfg = make(env, {"TRIAL_AUDIENCE": "channel_members"}, lookup=lookup)
        uid = await env.add_user(805)
        assert (await trial.activate(uid)).reason == "channel_not_configured"
        cfg["REQUIRED_CHANNEL_ID"] = CHANNEL
        res = await trial.activate(uid)
        assert res.reason == "not_member" and "Подпишитесь" in res.text
        lookup.status = "member"
        assert (await trial.check(uid)).reason == "not_member"  # a cached "left" is trusted for a minute
        assert (await trial.check(uid, fresh_channel=True)).ok  # «Проверить» asks Telegram again
        down = Lookup(fail=True)
        trial2, _, _ = make(
            env, {"TRIAL_AUDIENCE": "channel_members", "REQUIRED_CHANNEL_ID": CHANNEL}, lookup=down
        )
        other = await env.add_user(806)
        assert (await trial2.activate(other)).reason == "channel_unknown"
        ok = await trial.activate(uid)
        assert ok.ok


async def test_screen_check_budget(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        lookup = Lookup("member")
        trial, catalog, _ = make(
            env, {"TRIAL_AUDIENCE": "channel_members", "REQUIRED_CHANNEL_ID": CHANNEL}, lookup=lookup
        )
        uid = await env.add_user(807)
        assert (await trial.check(uid)).ok  # first time: Telegram is asked and the answer cached
        mark, calls, http = env.db.queries, lookup.calls, len(env.panel.requests)
        assert (await trial.check(uid)).ok
        assert env.db.queries - mark <= 2 and lookup.calls == calls and catalog.reads == 0
        assert len(env.panel.requests) == http  # no HTTP to the panel on the click
