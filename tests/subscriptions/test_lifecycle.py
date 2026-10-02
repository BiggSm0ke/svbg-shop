"""Purchase / renew / plan change / device addon / extend on a real PG + the fake panel (X2, X4 items)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from svbg.subscriptions.lifecycle import SubscriptionError, SubscriptionLifecycle
from svbg.subscriptions.terms import PlanTerms
from tests.subscriptions.kit import SyncEnv, frozen_clock, plan_row, sync_env

DAY = timedelta(days=1)
LIFE = SubscriptionLifecycle()


async def buy(env: SyncEnv, user_id: int, order: str, days: int = 30, **kw: Any) -> Any:
    terms = kw.pop("terms", None) or plan_row(env.squad)
    async with env.db.tx() as conn:
        return await LIFE.purchase(conn, user_id=user_id, terms=terms, days=days, ref_id=order, **kw)


async def events(env: SyncEnv, sid: int) -> list[dict[str, Any]]:
    rows = await env.db.raw("select * from subscription_events where subscription_id = $1 order by id", sid)
    return [dict(r) for r in rows]


def names(env: SyncEnv) -> list[str]:
    return [e.name for e in env.events]


async def test_first_purchase_creates_and_links(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(555)
            res = await buy(env, uid, "o1", 30)
            assert res.action == "created" and not res.duplicate
            assert res.new_paid_until == clock() + 30 * DAY
            await env.drain()
            row = await env.sub(res.subscription_id)
            assert row["link_state"] == "linked" and row["panel_username"] == "sv_555"
            assert row["paid_until"] == clock() + 30 * DAY and not row["is_trial"]
            assert row["plan_id"] == 1 and row["plan_snapshot"]["max_devices"] == 15
            user = env.panel_user(row["panel_user_id"])
            assert user["expireAt"] == clock() + 30 * DAY
            assert user["hwidDeviceLimit"] == 5 and user["telegramId"] == 555
            ev = await events(env, res.subscription_id)
            assert [e["kind"] for e in ev][:1] == ["purchase_new"]
            assert ev[0]["ref_type"] == "order" and ev[0]["ref_id"] == "o1"
            assert ev[0]["delta_seconds"] == 30 * 86_400
            assert "subscription.term_changed" in names(env)


async def test_same_order_twice_is_applied_once(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid = await env.add_user(556)
        first = await buy(env, uid, "o1")
        again = await buy(env, uid, "o1")
        assert again.duplicate and again.subscription_id == first.subscription_id
        assert again.new_paid_until == first.new_paid_until and again.action == "created"
        assert len(await env.db.raw("select 1 from subscriptions")) == 1
        assert len(await env.jobs("panel.create")) == 1
        # Renew applied once as well, even when retried after the first one committed.
        r1 = await buy(env, uid, "o2")
        r2 = await buy(env, uid, "o2")
        assert r2.duplicate and r2.action == "renewed" and r2.new_paid_until == r1.new_paid_until
        assert (await env.sub(first.subscription_id))["paid_until"] == r1.new_paid_until


async def test_renew_adds_to_the_bots_paid_until_and_pushes_absolute_date(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(557)
            sid = (await buy(env, uid, "o1", 30)).subscription_id
            await env.drain()
            res = await buy(env, uid, "o2", 90, extra_devices=2)
            assert res.action == "renewed" and res.old_paid_until == clock() + 30 * DAY
            assert res.new_paid_until == clock() + 120 * DAY
            await env.drain()
            row = await env.sub(sid)
            assert row["extra_devices"] == 2 and row["desired_device_limit"] == 7
            user = env.panel_user(row["panel_user_id"])
            assert user["expireAt"] == clock() + 120 * DAY and user["hwidDeviceLimit"] == 7
            # An expired subscription renews from now, not from the past date.
            await env.db.raw("update subscriptions set paid_until = $1 where id = $2", clock() - 5 * DAY, sid)
            res = await buy(env, uid, "o3", 30)
            assert res.new_paid_until == clock() + 30 * DAY


async def test_trial_conversion_keeps_the_remainder_and_the_link(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            trial_squad = env.panel.add_internal_squad("TRIAL")
            uid = await env.add_user(558)
            trial = PlanTerms.from_snapshot(
                plan_row(trial_squad, 9, is_trial=True, panel_tag="TRIAL", device_addon=None)
            )
            from svbg.subscriptions.service import Desired

            async with env.db.tx() as conn:
                sid = await LIFE.service.create(
                    conn,
                    user_id=uid,
                    telegram_id=558,
                    desired=Desired(expire_at=clock() + 3 * DAY, squads=[trial_squad], tag="TRIAL"),
                    plan_id=9,
                    plan_snapshot=trial.to_snapshot(),
                    is_trial=True,
                )
            await env.drain()
            short = (await env.sub(sid))["panel_short_uuid"]
            res = await buy(env, uid, "o1", 30)
            assert res.action == "converted" and res.subscription_id == sid
            assert res.is_trial_before and not res.is_trial_after
            assert res.new_paid_until == clock() + 33 * DAY  # 06 M2: the rest of the trial is added
            await env.drain()
            row = await env.sub(sid)
            assert not row["is_trial"] and row["plan_id"] == 1
            assert row["panel_short_uuid"] == short  # same panel user, the link did not change
            user = env.panel_user(row["panel_user_id"])
            assert user["activeInternalSquads"] == [env.squad]
            assert user["tag"] is None and user["hwidDeviceLimit"] == 5
            assert user["expireAt"] == clock() + 33 * DAY
            ev = await events(env, sid)
            converted = next(e for e in ev if e["kind"] == "trial_converted")
            assert converted["details"]["is_trial_before"] is True
            assert converted["details"]["is_trial_after"] is False


async def test_plan_change_keeps_or_replaces_the_term(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            other = env.panel.add_internal_squad("DE")
            uid = await env.add_user(559)
            await buy(env, uid, "o1", 30)
            kept = await buy(env, uid, "o2", 30, terms=plan_row(other, 2))
            assert kept.action == "changed" and kept.new_paid_until == clock() + 60 * DAY
            replaced = await buy(
                env, uid, "o3", 30, terms=plan_row(env.squad, 3), replace_term=True, extra_seconds=3600
            )
            assert replaced.new_paid_until == clock() + 30 * DAY + timedelta(hours=1)
            await env.drain()
            row = await env.sub(replaced.subscription_id)
            assert row["plan_id"] == 3
            assert env.panel_user(row["panel_user_id"])["expireAt"] == replaced.new_paid_until


async def test_admin_extension_in_panel_is_the_base_and_then_cleared(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(560)
            sid = (await buy(env, uid, "o1", 30)).subscription_id
            await env.drain()
            manual = clock() + 50 * DAY
            await env.db.raw(
                "update subscriptions set overrides = '{\"expire\": true}'::jsonb, panel_expire_at = $1 "
                "where id = $2",
                manual,
                sid,
            )
            res = await buy(env, uid, "o2", 30)
            assert res.new_paid_until == manual + 30 * DAY
            await env.drain()
            row = await env.sub(sid)
            assert "expire" not in row["overrides"]
            assert env.panel_user(row["panel_user_id"])["expireAt"] == manual + 30 * DAY


async def test_purchase_while_frozen_goes_to_the_frozen_balance(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock():
            uid = await env.add_user(561)
            sid = (await buy(env, uid, "o1", 30)).subscription_id
            await env.drain()
            await env.db.raw(
                "update subscriptions set hold_kind = 'ip_guard', hold_since = now(), "
                "hold_frozen_seconds = 100 where id = $1",
                sid,
            )
            before = await env.sub(sid)
            jobs_before = len(await env.jobs())
            res = await buy(env, uid, "o2", 30)
            assert res.frozen and res.new_paid_until == before["paid_until"]
            row = await env.sub(sid)
            assert row["hold_frozen_seconds"] == 100 + 30 * 86_400
            assert row["paid_until"] == before["paid_until"]
            assert len(await env.jobs()) == jobs_before  # same plan, nothing to push while frozen


async def test_concurrent_orders_one_subscription_both_terms(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(562)
            a, b = await asyncio.gather(buy(env, uid, "oa", 30), buy(env, uid, "ob", 30))
            assert {a.action, b.action} == {"created", "renewed"}
            assert a.subscription_id == b.subscription_id
            assert len(await env.db.raw("select 1 from subscriptions")) == 1
            assert (await env.sub(a.subscription_id))["paid_until"] == clock() + 60 * DAY
            same = await asyncio.gather(*(buy(env, uid, "oc", 30) for _ in range(4)))
            assert sum(not r.duplicate for r in same) == 1
            assert (await env.sub(a.subscription_id))["paid_until"] == clock() + 90 * DAY


async def test_closed_or_missing_subscription_gets_a_new_one(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid = await env.add_user(563)
        first = (await buy(env, uid, "o1")).subscription_id
        await env.drain()
        await env.db.raw("update subscriptions set link_state = 'panel_missing' where id = $1", first)
        res = await buy(env, uid, "o2")
        assert res.action == "created" and res.subscription_id != first
        assert (await env.sub(res.subscription_id))["panel_username"] == "sv_563_2"


async def test_device_addon(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid = await env.add_user(564)
        sid = (await buy(env, uid, "o1")).subscription_id
        await env.drain()
        await env.db.raw(
            "update subscriptions set overrides = '{\"device_limit\": true}'::jsonb where id = $1", sid
        )
        async with env.db.tx() as conn:
            res = await LIFE.add_devices(conn, sid, 3, ref_id="d1")
        assert res.action == "devices_added"
        async with env.db.tx() as conn:
            dup = await LIFE.add_devices(conn, sid, 3, ref_id="d1")
        assert dup.duplicate
        await env.drain()
        row = await env.sub(sid)
        assert row["extra_devices"] == 3 and row["desired_device_limit"] == 8
        assert "device_limit" not in row["overrides"]
        assert env.panel_user(row["panel_user_id"])["hwidDeviceLimit"] == 8
        with pytest.raises(SubscriptionError) as err:
            async with env.db.tx() as conn:
                await LIFE.add_devices(conn, sid, 8, ref_id="d2")  # 5 + 3 + 8 > 15
        assert err.value.code == "addon_limit" and "15" in err.value.text
        assert (await env.sub(sid))["extra_devices"] == 3  # rolled back
        assert "subscription.devices_changed" in names(env)


async def test_device_addon_refused_without_addon_or_with_unlimited(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        for tg, terms in (
            (565, plan_row(env.squad, device_addon=None)),
            (566, plan_row(env.squad, device_limit=0)),
            (567, plan_row(env.squad, device_limit=None)),
        ):
            uid = await env.add_user(tg)
            sid = (await buy(env, uid, f"o{tg}", terms=terms)).subscription_id
            with pytest.raises(SubscriptionError) as err:
                async with env.db.tx() as conn:
                    await LIFE.add_devices(conn, sid, 1, ref_id=f"d{tg}")
            assert err.value.code == "addon_unavailable"
            with pytest.raises(SubscriptionError):
                await buy(env, uid, f"x{tg}", terms=terms, extra_devices=1)


async def test_extend_grant_shorten_and_frozen_credit(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(568)
            sid = (await buy(env, uid, "o1", 30)).subscription_id
            async with env.db.tx() as conn:
                res = await LIFE.extend(
                    conn,
                    sid,
                    7 * 86_400,
                    source="referral",
                    kind="referral_days",
                    ref_type="ref",
                    ref_id="r1",
                )
                dup = await LIFE.extend(
                    conn,
                    sid,
                    7 * 86_400,
                    source="referral",
                    kind="referral_days",
                    ref_type="ref",
                    ref_id="r1",
                )
            assert res.new_paid_until == clock() + 37 * DAY and dup.duplicate
            async with env.db.tx() as conn:
                cut = await LIFE.extend(conn, sid, -100 * 86_400, source="admin", reason="возврат")
            assert cut.new_paid_until == clock()  # never below now: the panel expires the user itself
            await env.drain()
            row = await env.sub(sid)
            assert env.panel_user(row["panel_user_id"])["expireAt"] >= clock() + timedelta(minutes=2)
            await env.db.raw(
                "update subscriptions set hold_kind = 'admin', hold_since = now(), hold_frozen_seconds = 50 "
                "where id = $1",
                sid,
            )
            async with env.db.tx() as conn:
                credit = await LIFE.extend(conn, sid, 3600, source="admin")
                negative = await LIFE.extend(conn, sid, -10_000, source="admin")
            assert credit.action == "frozen_credit" and negative.frozen
            assert (await env.sub(sid))["hold_frozen_seconds"] == 0


async def test_refusals_and_validation(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with pytest.raises(SubscriptionError) as err:
            await buy(env, 999_999, "o1")
        assert err.value.code == "no_user"
        uid = await env.add_user(569)
        for days in (0, -1, 40_000):
            with pytest.raises(ValueError):
                await buy(env, uid, "o2", days)
        with pytest.raises(ValueError):
            await buy(env, uid, "o2", extra_seconds=-1)
        with pytest.raises(ValueError):
            await buy(env, uid, "o2", terms={"squads": []})
        sid = (await buy(env, uid, "o3")).subscription_id
        await env.db.raw("update subscriptions set link_state = 'closed' where id = $1", sid)
        with pytest.raises(SubscriptionError) as err:
            async with env.db.tx() as conn:
                await LIFE.add_devices(conn, sid, 1, ref_id="d")
        assert err.value.code == "no_subscription"
        for bad in (0, True):
            with pytest.raises(ValueError):
                async with env.db.tx() as conn:
                    await LIFE.extend(conn, sid, bad, source="admin")
        assert not await env.db.raw("select 1 from jobs where queue = 'hook' and status = 'dead'")


async def test_panel_down_purchase_is_kept_and_delivered_after_recovery(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid = await env.add_user(570)
            sid = (await buy(env, uid, "o1", 30)).subscription_id
            await env.drain()
            env.panel.inject("503", method="PATCH", times=None)
            res = await buy(env, uid, "o2", 30)  # the money side commits regardless of the panel
            assert res.new_paid_until == clock() + 60 * DAY
            outcomes = await env.drain()
            assert [o for j, o in outcomes if j.kind == "panel.update"] in (["retry"], ["failed"])  # not dead
            row = await env.sub(sid)
            assert row["paid_until"] == clock() + 60 * DAY
            assert env.panel_user(row["panel_user_id"])["expireAt"] == clock() + 30 * DAY
            env.panel.clear_faults()
            await env.drain(make_due=True)
            assert env.panel_user(row["panel_user_id"])["expireAt"] == clock() + 60 * DAY
            again = await buy(env, uid, "o2", 30)  # a retried fulfill after the outage changes nothing
            assert again.duplicate and (await env.sub(sid))["paid_until"] == clock() + 60 * DAY
