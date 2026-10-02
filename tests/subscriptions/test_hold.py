"""Freeze / unfreeze (accumulator, X5) and ``can_spend`` on a real PG + the fake panel."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

import pytest

from svbg.subscriptions.hold import can_spend, freeze, spend_check, unfreeze, zero_hold
from svbg.subscriptions.lifecycle import SubscriptionError, SubscriptionLifecycle
from tests.subscriptions.kit import SyncEnv, frozen_clock, plan_row, sync_env

DAY = timedelta(days=1)
LIFE = SubscriptionLifecycle()


async def bought(env: SyncEnv, tg: int, days: int = 30) -> tuple[int, int]:
    uid = await env.add_user(tg)
    async with env.db.tx() as conn:
        res = await LIFE.purchase(conn, user_id=uid, terms=plan_row(env.squad), days=days, ref_id=f"o{tg}")
    await env.drain()
    return uid, res.subscription_id


async def test_freeze_disables_and_unfreeze_restores_the_rest(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            uid, sid = await bought(env, 700, 30)
            clock.advance(days=10)
            async with env.db.tx() as conn:
                res = await freeze(conn, sid, "ip_guard", reason="12 IP за час", actor_id=1)
                again = await freeze(conn, sid, "admin", reason="ещё раз")
            assert res.frozen_seconds == 20 * 86_400 and again.already
            await env.drain()
            row = await env.sub(sid)
            assert row["hold_kind"] == "ip_guard" and row["disabled_reason"] == "ip_guard"
            assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED"
            async with env.db.read() as conn:
                check = await can_spend(conn, uid)
            assert not check and check.reason == "frozen" and "приостановлена" in check.text
            clock.advance(days=40)  # the frozen days do not burn
            async with env.db.tx() as conn:
                out = await unfreeze(conn, sid, actor_id=1)
                noop = await unfreeze(conn, sid)
            assert out.outcome == "active" and out.paid_until == clock() + 20 * DAY
            assert noop.outcome is None
            await env.drain()
            row = await env.sub(sid)
            assert row["hold_kind"] is None and row["hold_frozen_seconds"] == 0
            assert row["paid_until"] == clock() + 20 * DAY and row["disabled_reason"] is None
            user = env.panel_user(row["panel_user_id"])
            assert user["status"] == "ACTIVE" and user["expireAt"] == clock() + 20 * DAY
            async with env.db.read() as conn:
                assert await can_spend(conn, uid)
            kinds = [r["kind"] for r in await env.db.raw("select kind from subscription_events order by id")]
            assert "frozen" in kinds and "unfrozen" in kinds
            assert {"subscription.frozen", "subscription.unfrozen"} <= {e.name for e in env.events}


async def test_unfreeze_outcomes_and_keep_disabled(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            _, sid = await bought(env, 701, 1)
            clock.advance(timedelta(hours=23, minutes=58))  # 2 minutes left
            async with env.db.tx() as conn:
                await freeze(conn, sid, "admin", reason="пауза")
            async with env.db.tx() as conn:
                out = await unfreeze(conn, sid)
            assert out.outcome == "expired"
            _, sid2 = await bought(env, 702, 30)
            async with env.db.tx() as conn:
                await freeze(conn, sid2, "admin", reason="спор")
                await zero_hold(conn, sid2, reason="чарджбэк", actor_id=1)
            async with env.db.tx() as conn:
                out = await unfreeze(conn, sid2)
            assert out.outcome == "zeroed" and out.paid_until == clock()
            _, sid3 = await bought(env, 703, 30)
            async with env.db.tx() as conn:
                await freeze(conn, sid3, "admin", reason="пауза")
            await env.drain()
            assert (await env.sub(sid3))["disabled_reason"] == "hold"
            async with env.db.tx() as conn:
                await unfreeze(conn, sid3, keep_disabled="channel_left")
            await env.drain()
            row = await env.sub(sid3)
            assert row["disabled_reason"] == "channel_left"
            assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED"


async def test_freeze_refusals(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        _, sid = await bought(env, 704)
        async with env.db.tx() as conn:
            with pytest.raises(ValueError):
                await freeze(conn, sid, "vacation", reason="x")
            with pytest.raises(ValueError):
                await freeze(conn, sid, "admin", reason="  ")
            with pytest.raises(SubscriptionError):
                await zero_hold(conn, sid, reason="x")
            with pytest.raises(SubscriptionError):
                await freeze(conn, 10**9, "admin", reason="x")


async def test_can_spend_is_one_sql_and_covers_every_case(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        uid, sid = await bought(env, 705)
        other = await env.add_user(706)
        async with env.db.read() as conn:
            mark = env.db.queries
            ok = await can_spend(conn, uid, sid)
            assert env.db.queries - mark == 1
            assert ok.ok and ok.reason is None
            assert (await can_spend(conn, 10**9)).reason == "no_user"
            assert (await can_spend(conn, other)).ok
        await env.db.raw("update users set banned_at = now() where id = $1", other)
        await env.db.raw(
            "update subscriptions set hold_kind = 'admin', hold_since = now() where id = $1", sid
        )
        async with env.db.read() as conn:
            assert (await can_spend(conn, other)).reason == "banned"
            frozen = await can_spend(conn, uid)
        assert frozen.reason == "frozen" and frozen.hold_kind == "admin"
        assert spend_check(banned_at=None, hold_kind=None).ok


async def test_property_term_is_conserved_through_any_sequence(pg_dsn: str) -> None:
    """Invariant: remaining time == granted − time elapsed while not frozen, for any sequence of purchases,
    grants, freezes, unfreezes and clock moves; the panel ends on exactly the bot's paid_until."""
    async with sync_env(pg_dsn) as env:
        with frozen_clock() as clock:
            rng = random.Random(20261001)  # fixed seed: deterministic
            uid, sid = await bought(env, 707, 30)
            remaining = 30 * 86_400
            frozen = False
            for step in range(60):
                op = rng.choice(("buy", "grant", "freeze", "unfreeze", "advance", "advance"))
                async with env.db.tx() as conn:
                    if op == "buy":
                        days = rng.choice((30, 90))
                        await LIFE.purchase(
                            conn, user_id=uid, terms=plan_row(env.squad), days=days, ref_id=f"s{step}"
                        )
                        remaining += days * 86_400
                    elif op == "grant":
                        secs = rng.randrange(60, 7 * 86_400)
                        await LIFE.extend(conn, sid, secs, source="admin", ref_type="t", ref_id=str(step))
                        remaining += secs
                    elif op == "freeze":
                        await freeze(conn, sid, "admin", reason="тест")
                        frozen = True
                    elif op == "unfreeze":
                        await unfreeze(conn, sid)
                        frozen = False
                    else:
                        secs = rng.randrange(1, 3 * 86_400)
                        clock.advance(secs)
                        if not frozen:
                            remaining -= secs
                assert remaining > 0, "the sequence must not run the term out"
                row = await env.sub(sid)
                left = row["hold_frozen_seconds"] if frozen else (row["paid_until"] - clock()).total_seconds()
                assert left == remaining, (step, op)
            async with env.db.tx() as conn:
                await unfreeze(conn, sid)
            await env.drain()
            row = await env.sub(sid)
            panel: dict[str, Any] = env.panel_user(row["panel_user_id"])
            assert panel["expireAt"] == row["paid_until"] and panel["status"] == "ACTIVE"
            dups = await env.db.raw(
                "select ref_id from subscription_events where ref_id is not null "
                "group by subscription_id, kind, ref_type, ref_id having count(*) > 1"
            )
            assert not dups
