"""LTE term events at runtime (05 §2.1.9): money vs gifts, holds with an end, timers after pending events,
late events re-simulated, the catch-up's high-water mark and backoff, the term hook never waits on a cycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.ext.lte import periods as p
from svbg.ext.lte.model import engine_event_kind
from svbg.ext.lte.service import (
    CATCH_UP_KEY,
    FAILED_ALERT_AFTER,
    HWM_LAG,
    TERM_RETRY_S,
    hold_intervals,
    kv_get,
)
from svbg.jobs.worker import RetryJob
from tests.ext.lte.rkit import LteEnv, lte_env

pg = pytest.mark.pg


def d(month: int, day: int, hour: int = 9) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


# ------------------------------------------------------------------------------------------- pure parts


@pytest.mark.parametrize(
    ("core", "source", "kind"),
    [
        ("purchase_renew", "bot", "paid"),
        ("purchase_new", "admin", "admin"),  # «Выдать тариф» in the admin panel
        ("plan_changed", "admin", "admin"),
        ("purchase_new", "promo", "bonus"),  # a plan_gift promo code
        ("purchase_renew", "referral", "bonus"),
        ("trial_converted", "bonus", "bonus"),
    ],
)
def test_purchase_kinds_are_paid_only_for_money(core: str, source: str, kind: str) -> None:
    assert engine_event_kind(core, source=source) == kind


def test_hold_intervals_keep_past_holds_with_their_end() -> None:
    marks = [
        ("frozen", d(10, 10)),
        ("frozen", d(10, 11)),  # a repeated freeze does not restart the hold
        ("unfrozen", d(11, 15)),
        ("unfrozen", d(11, 16)),  # an unmatched unfreeze is ignored
        ("frozen", d(12, 1)),
    ]
    assert hold_intervals(marks) == (
        p.HoldInterval(d(10, 10), d(11, 15)),
        p.HoldInterval(d(12, 1), None),
    )
    assert hold_intervals([], frozen_since=d(9, 1)) == (p.HoldInterval(d(9, 1), None),)
    assert hold_intervals([("unfrozen", d(9, 1))]) == ()


def test_plan_recompute_never_reinserts_old_periods_of_a_full_replay() -> None:
    old = p.PeriodState(d(1, 1), 0, d(1, 1), d(2, 1), state="closed", ended_at=d(2, 1), period_id=1)
    lost = p.PeriodState(d(2, 1), 1, d(2, 1), d(3, 1), state="closed", ended_at=d(3, 1))  # never stored
    live = p.PeriodState(d(9, 1), 0, d(9, 1), d(10, 1), state="open")
    diff = p.plan_recompute([old], [replace_id(old), lost, live], since=d(8, 1))
    assert diff.insert == (live,) and diff.stale == () and diff.keep == ()


def replace_id(period: p.PeriodState) -> p.PeriodState:
    from dataclasses import replace

    return replace(period, period_id=None)


# -------------------------------------------------------------------------------------------- database


async def _fresh(env: LteEnv, tg: int) -> int:
    """A linked subscription without journal rows: each test writes its own term history."""
    sid = await env.linked_sub(tg)
    await env.db.raw("delete from subscription_events where subscription_id = $1", sid)
    return sid


async def _event(
    env: LteEnv,
    sid: int,
    kind: str,
    ts: datetime,
    *,
    old: datetime | None = None,
    new: datetime | None = None,
    source: str = "bot",
    details: dict[str, Any] | None = None,
) -> int:
    rows = await env.db.raw(
        "insert into subscription_events "
        "(subscription_id, kind, source, old_expire, new_expire, details, ts) "
        "values ($1, $2, $3, $4, $5, $6, $7) returning id",
        sid,
        kind,
        source,
        old,
        new,
        details or {},
        ts,
    )
    return int(rows[0]["id"])


async def _apply(env: LteEnv, sid: int, at: datetime) -> Any:
    async with env.db.tx() as conn:
        return await env.service.apply_events(conn, sid, at=at)


async def _anchor(env: LteEnv, sid: int) -> dict[str, Any]:
    rows = await env.db.raw("select * from lte_anchors where subscription_id = $1", sid)
    return dict(rows[0])


async def _periods(env: LteEnv, sid: int) -> list[dict[str, Any]]:
    rows = await env.db.raw(
        "select id, idx, anchor_at, starts_at, state, end_cause, ended_at from lte_periods "
        "where subscription_id = $1 order by starts_at, id",
        sid,
    )
    return [dict(r) for r in rows]


async def _launch_trial(env: LteEnv, sid: int) -> None:
    await env.db.raw(
        "insert into lte_overrides (subscription_id, kind, exempt_kind) "
        "values ($1, 'exempt', 'launch_trial')",
        sid,
    )


async def _revoked(env: LteEnv, sid: int) -> str | None:
    rows = await env.db.raw(
        "select revoke_reason from lte_overrides where subscription_id = $1 and exempt_kind = 'launch_trial'",
        sid,
    )
    return rows[0]["revoke_reason"]


@pg
async def test_an_admin_plan_or_a_gift_does_not_revoke_launch_trial_money_does(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _fresh(env, 501)
        await _launch_trial(env, sid)
        await _event(env, sid, "trial_started", d(9, 1), new=d(9, 4), details={"is_trial": True})
        await _event(env, sid, "purchase_new", d(9, 2), old=d(9, 4), new=d(10, 2), source="admin")
        await _event(env, sid, "purchase_renew", d(9, 3), old=d(10, 2), new=d(11, 2), source="promo")
        await _apply(env, sid, d(9, 3, 10))
        assert await _revoked(env, sid) is None
        assert (await _anchor(env, sid))["anchor_kind"] == "admin"
        assert await env.service.daily(at=d(9, 3, 11)) == 0  # the safety net ignores them too
        assert await _revoked(env, sid) is None
        await _event(env, sid, "purchase_renew", d(9, 4), old=d(11, 2), new=d(12, 2))
        await _apply(env, sid, d(9, 4, 10))
        assert await _revoked(env, sid) == "converted_to_paid"


@pg
async def test_unfreeze_after_a_long_hold_does_not_end_the_series_inside_the_hold(pg_dsn: str) -> None:
    """Owner scenario 29: hold 10.10–15.11, coverage until 30.10 — E8 is held, the unfreeze extends."""
    async with lte_env(pg_dsn) as env:
        sid = await _fresh(env, 502)
        await _event(env, sid, "purchase_new", d(9, 30), new=d(10, 30))
        await _event(env, sid, "frozen", d(10, 10), old=d(10, 30), source="system")
        await _apply(env, sid, d(10, 10, 10))
        await _event(env, sid, "unfrozen", d(11, 15), old=d(10, 30), new=d(12, 5), source="system")
        await _apply(env, sid, d(11, 15, 10))
        anchor = await _anchor(env, sid)
        assert anchor["series_open"] and anchor["anchor_at"] == d(9, 30)
        assert anchor["coverage_end"] == d(12, 5)
        periods = await _periods(env, sid)
        assert all(x["end_cause"] != "series_end" for x in periods)
        assert [x["state"] for x in periods].count("closed") == len(periods) - 1
        assert all(x["ended_at"] is None or x["ended_at"] >= d(11, 15) for x in periods)


@pg
async def test_timers_apply_pending_events_before_firing(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _fresh(env, 503)
        await _event(env, sid, "purchase_new", d(9, 1), new=d(10, 1))
        await _apply(env, sid, d(9, 1, 10))
        # A renewal committed after the catch-up of this cycle, before its timers.
        await _event(env, sid, "purchase_renew", d(9, 30), old=d(10, 1), new=d(10, 31))
        assert await env.service._timers(d(10, 3)) == 1
        anchor = await _anchor(env, sid)
        assert (
            anchor["series_open"] and anchor["anchor_at"] == d(9, 1) and anchor["coverage_end"] == d(10, 31)
        )
        assert all(x["end_cause"] != "series_end" for x in await _periods(env, sid))


@pg
async def test_a_late_renewal_paid_before_the_series_end_is_re_simulated(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await _fresh(env, 504)
        await _event(env, sid, "purchase_new", d(9, 1), new=d(10, 1))
        await _apply(env, sid, d(9, 1, 10))
        await env.service._timers(d(10, 3))  # E8 at 02.10 09:00 (coverage 01.10 + 24 h)
        assert not (await _anchor(env, sid))["series_open"]
        # Paid 01.10 20:00 (inside the grace), delivered only on 03.10.
        await _event(
            env,
            sid,
            "purchase_renew",
            d(10, 3, 12),
            old=d(10, 1),
            new=d(10, 31),
            details={"paid_at": d(10, 1, 20).isoformat()},
        )
        written = await _apply(env, sid, d(10, 3, 13))
        assert written is not None and written.reviews == []
        anchor = await _anchor(env, sid)
        assert anchor["series_open"] and anchor["anchor_at"] == d(9, 1)  # same series, same reset day
        periods = await _periods(env, sid)
        assert all(x["end_cause"] != "series_end" for x in periods)
        assert [x["state"] for x in periods if x["state"] != "closed"] == ["open"]


@pg
async def test_catch_up_parks_a_failing_subscription_and_moves_the_mark(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with lte_env(pg_dsn) as env:
        poison = await _fresh(env, 505)
        good = await _fresh(env, 506)
        await _event(env, poison, "purchase_new", d(9, 1), new=d(10, 1))
        last = await _event(env, good, "purchase_new", d(9, 1), new=d(10, 1))
        real = env.service.apply_events

        async def apply_events(conn: Any, sid: int, *, at: datetime | None = None) -> Any:
            if sid == poison:
                raise RuntimeError("poison event")
            return await real(conn, sid, at=at)

        monkeypatch.setattr(env.service, "apply_events", apply_events)
        at = datetime.now(UTC) + HWM_LAG + timedelta(minutes=1)
        assert await env.service._catch_up(at) == 1
        async with env.db.read() as conn:
            state = await kv_get(conn, CATCH_UP_KEY)
        assert state["hwm"] >= last and set(state["failed"]) == {str(poison)}
        assert await env.service._catch_up(at) == 0  # parked: not retried before its backoff
        for n in range(2, FAILED_ALERT_AFTER + 1):
            at += timedelta(days=2)
            await env.service._catch_up(at)
            async with env.db.read() as conn:
                assert (await kv_get(conn, CATCH_UP_KEY))["failed"][str(poison)]["n"] == n
        assert "lte:term_failed" in await env.rw.attention_keys()
        monkeypatch.setattr(env.service, "apply_events", real)
        at += timedelta(days=2)
        assert await env.service._catch_up(at) == 1  # healed: processed and forgotten
        async with env.db.read() as conn:
            assert (await kv_get(conn, CATCH_UP_KEY))["failed"] == {}
        assert len(await _periods(env, poison)) == 1


@pg
async def test_term_hook_retries_instead_of_waiting_for_a_long_cycle(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(507)
        async with env.service.lock:
            with pytest.raises(RetryJob) as caught:
                await env.service.process_subscription(sid, wait=0.01)
        assert caught.value.delay == TERM_RETRY_S
        assert await env.service.process_subscription(sid, wait=0.01) is not None
        assert not env.service.lock.locked()


def test_a_base_squad_belongs_to_one_group_so_two_blocks_never_share_a_substitution() -> None:
    """Review finding 5: ``lte_twins`` is keyed by the base squad — a block of another group gets no row
    for that base (it is «неприменим» instead), so a release never drops a row another block needs."""
    from svbg.ext.lte.decide import Twin
    from svbg.ext.lte.enforce import fallback_substitutions
    from svbg.ext.lte.tables import lte_twins

    assert [c.name for c in lte_twins.primary_key.columns] == ["base_squad_uuid"]
    twins = {"NL": Twin("NL", 1, "NL noLTE")}
    assert fallback_substitutions(["NL"], {1}, twins) == {"NL": "NL noLTE"}
    assert fallback_substitutions(["NL"], {2}, twins) == {}
