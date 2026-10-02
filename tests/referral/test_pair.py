"""Days mode end to end: grants through the lifecycle, deferred sides, caps, idempotency, reports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from svbg.core.bus import Event
from svbg.referral.rules import RETRY_WINDOW
from tests.referral.kit import (
    Env,
    add_purchase,
    add_sub,
    add_trial,
    add_user,
    bind,
    grant_rows,
    jobs,
    paid_until,
    rewards,
)

DAY = timedelta(days=1)


async def _pair(env: Env, *, inviter_sub: bool = True) -> tuple[int, int, int | None, int]:
    inviter = await add_user(env.db, username="anya", first_name="Аня")
    invitee = await add_user(env.db, first_name="Боб <b>")
    isub = await add_sub(env.db, inviter, days_left=10) if inviter_sub else None
    await bind(env.db, invitee, inviter)
    tsub = await add_trial(env.db, invitee, days_left=3)
    return inviter, invitee, isub, tsub


def _close(a: datetime, b: datetime, slack: timedelta = timedelta(minutes=2)) -> bool:
    return abs(a - b) <= slack


async def test_both_sides_granted_once(env: Env) -> None:
    inviter, invitee, isub, tsub = await _pair(env)
    assert isub is not None
    before_i, before_t = await paid_until(env.db, isub), await paid_until(env.db, tsub)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {"inviter": 14, "invitee": 7}
    assert _close(await paid_until(env.db, isub), before_i + 14 * DAY)
    assert _close(await paid_until(env.db, tsub), before_t + 7 * DAY)
    rows = await rewards(env.db, invitee)
    assert {s: (r["status"], r["days"], r["user_id"]) for s, r in rows.items()} == {
        "inviter": ("granted", 14, inviter),
        "invitee": ("granted", 7, invitee),
    }
    events = await env.db.raw(
        "select subscription_id, kind, source, delta_seconds from subscription_events "
        "where kind = 'referral' order by subscription_id"
    )
    assert [(e["subscription_id"], e["source"], e["delta_seconds"]) for e in events] == sorted(
        [(isub, "referral", 14 * 86_400), (tsub, "referral", 7 * 86_400)]
    )
    trial = await env.db.raw("select is_trial from subscriptions where id = $1", tsub)
    assert trial[0]["is_trial"] is True  # a bonus never converts the trial
    assert len(await jobs(env.db, "referral.notify")) == 3  # two users + one admin report
    # Second pass: structural idempotency — nothing changes, nothing is said.
    again = await env.svc.process_pair(invitee)
    assert again.granted == {}
    assert len(await jobs(env.db, "referral.notify")) == 3
    await env.drain()
    texts = [t for _, t, _ in env.sender.sent]
    assert any(t.startswith("🎁 +14 дн. подписки за приглашённого Боб &lt;b&gt;!") for t in texts)
    assert any(t.startswith("🎁 +7 дн. подписки в подарок по приглашению @anya!") for t in texts)
    [(kind, report, html)] = env.poster.posts
    assert kind == "partners" and html
    assert "Реферальная награда (дни)" in report and "+14 дн." in report and "+7 дн." in report
    assert "Всего наград у пригласившего: 1" in report
    assert "Боб &lt;b&gt;" in report


async def test_paid_trigger_waits_for_a_purchase(env: Env) -> None:
    env.cfg["REFERRAL_TRIGGER"] = "paid"
    _inviter, invitee, _isub, tsub = await _pair(env)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {} and await rewards(env.db, invitee) == {}
    await add_purchase(env.db, tsub)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {"inviter": 14, "invitee": 7}


async def test_paid_trigger_ignores_admin_and_gift_plans(env: Env) -> None:
    env.cfg["REFERRAL_TRIGGER"] = "paid"
    _inviter, invitee, _isub, tsub = await _pair(env)
    for source in ("admin", "promo", "gift"):
        await env.db.raw(
            "insert into subscription_events (subscription_id, kind, source, ref_type, ref_id) "
            "values ($1, 'purchase_new', $2, 'order', $3)",
            tsub,
            source,
            source,
        )
    out = await env.svc.process_pair(invitee)
    assert out.granted == {} and await rewards(env.db, invitee) == {}
    await add_purchase(env.db, tsub)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {"inviter": 14, "invitee": 7}


async def test_inviter_without_subscription_waits_then_gets_days(env: Env) -> None:
    inviter, invitee, _isub, _tsub = await _pair(env, inviter_sub=False)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {"invitee": 7}
    rows = await rewards(env.db, invitee)
    assert rows["inviter"]["status"] == "deferred"
    assert rows["inviter"]["reason"] == "no_subscription"
    assert _close(rows["inviter"]["retry_until"], datetime.now(UTC) + RETRY_WINDOW)
    await env.drain()
    [(_, report, _)] = env.poster.posts
    assert "Реферальная награда (дни)" in report
    assert "не выдано: нет подписки, ждём до" in report
    # Re-checks while waiting: no writes, no messages.
    notes = len(await jobs(env.db, "referral.notify", "done"))
    assert (await env.svc.process_pair(invitee)).granted == {}
    assert len(await jobs(env.db, "referral.notify")) == 0
    # The inviter subscribes → the hook finds the deferred side → granted.
    isub = await add_sub(env.db, inviter, days_left=30)
    await env.bus.publish(
        Event(
            "subscription.term_changed", {"user_id": inviter, "subscription_id": isub, "kind": "purchase_new"}
        )
    )
    assert len(await jobs(env.db, "referral.pair")) == 1
    await env.drain()
    rows = await rewards(env.db, invitee)
    assert (rows["inviter"]["status"], rows["inviter"]["days"], rows["inviter"]["retry_until"]) == (
        "granted",
        14,
        None,
    )
    assert len(await jobs(env.db, "referral.notify", "done")) == notes + 2  # user + admin
    assert "+14 дн." in env.poster.posts[-1][1]


async def test_only_deferral_is_one_warning(env: Env) -> None:
    """Nothing granted (invitee settled earlier, inviter capped) → one «не выдана» warning, once."""
    inviter, invitee, _isub, _tsub = await _pair(env)
    await grant_rows(env.db, inviter, 20)
    out = await env.svc.process_pair(invitee)
    assert out.granted == {"invitee": 7}
    rows = await rewards(env.db, invitee)
    assert (rows["inviter"]["status"], rows["inviter"]["reason"]) == ("deferred", "cap_30d")
    await env.drain()
    assert "лимит 20 наград за 30 дней" in env.poster.posts[-1][1]
    # A second invitee of the same capped inviter: their side granted, inviter deferred → report again
    # for the new pair (different pair), still one per pair.
    second = await add_user(env.db)
    await bind(env.db, second, inviter)
    await add_trial(env.db, second)
    await env.svc.process_pair(second)
    await env.svc.process_pair(second)
    await env.drain()
    assert len(env.poster.posts) == 2


async def test_cap_frees_within_the_window(env: Env) -> None:
    inviter, invitee, _isub, _tsub = await _pair(env)
    await grant_rows(env.db, inviter, 20, ago=timedelta(days=29, hours=23))
    await env.svc.process_pair(invitee)
    assert (await rewards(env.db, invitee))["inviter"]["status"] == "deferred"
    # Two hours later the oldest rewards leave the 30-day window: the hourly sweep re-checks the pair.
    env.svc._clock = lambda: datetime.now(UTC) + timedelta(hours=2)  # type: ignore[method-assign]
    expired, queued = await env.svc.sweep_once()
    assert (expired, queued) == (0, 1)
    await env.drain()
    assert (await rewards(env.db, invitee))["inviter"]["status"] == "granted"


async def test_deferred_side_expires_silently(env: Env) -> None:
    _inviter, invitee, _isub, _tsub = await _pair(env, inviter_sub=False)
    await env.svc.process_pair(invitee)
    await env.drain()
    posts = len(env.poster.posts)
    env.svc._clock = lambda: datetime.now(UTC) + RETRY_WINDOW + timedelta(minutes=1)  # type: ignore[method-assign]
    expired, _queued = await env.svc.sweep_once()
    assert expired == 1
    await env.drain()
    rows = await rewards(env.db, invitee)
    assert rows["inviter"]["status"] == "expired"
    assert len(env.poster.posts) == posts
    assert (await env.svc.process_pair(invitee)).granted == {}


async def test_expired_subscription_revives(env: Env) -> None:
    inviter = await add_user(env.db)
    invitee = await add_user(env.db)
    isub = await add_sub(env.db, inviter, days_left=-20)  # expired long ago, still linked
    await bind(env.db, invitee, inviter)
    await add_trial(env.db, invitee)
    await env.svc.process_pair(invitee)
    assert _close(await paid_until(env.db, isub), datetime.now(UTC) + 14 * DAY)


async def test_frozen_subscription_keeps_the_days_frozen(env: Env) -> None:
    inviter = await add_user(env.db)
    invitee = await add_user(env.db)
    isub = await add_sub(env.db, inviter, days_left=5, hold=True)
    before = await paid_until(env.db, isub)
    await bind(env.db, invitee, inviter)
    await add_trial(env.db, invitee)
    out = await env.svc.process_pair(invitee)
    assert out.granted["inviter"] == 14
    row = (await env.db.raw("select paid_until, hold_frozen_seconds from subscriptions where id = $1", isub))[
        0
    ]
    assert row["paid_until"] == before
    assert row["hold_frozen_seconds"] == 14 * 86_400


async def test_trial_event_end_to_end(env: Env) -> None:
    inviter, invitee, _isub, tsub = await _pair(env)
    await env.bus.publish(Event("trial.activated", {"user_id": invitee, "subscription_id": tsub, "days": 3}))
    assert len(await jobs(env.db, "referral.pair")) == 1
    await env.drain()
    rows = await rewards(env.db, invitee)
    assert {s: r["status"] for s, r in rows.items()} == {"inviter": "granted", "invitee": "granted"}
    assert inviter


async def test_own_referral_event_is_ignored(env: Env) -> None:
    inviter, invitee, isub, _tsub = await _pair(env)
    await env.bus.publish(
        Event("subscription.term_changed", {"user_id": inviter, "subscription_id": isub, "kind": "referral"})
    )
    assert await jobs(env.db, "referral.pair") == []
    assert invitee


async def test_events_ignored_when_off(env: Env) -> None:
    _inviter, invitee, _isub, tsub = await _pair(env)
    env.cfg["REFERRAL_ENABLED"] = False
    await env.bus.publish(Event("trial.activated", {"user_id": invitee, "subscription_id": tsub}))
    assert await jobs(env.db, "referral.pair") == []
    assert (await env.svc.process_pair(invitee)).skipped == "off"


async def test_concurrent_passes_grant_once(env: Env) -> None:
    inviter, invitee, isub, _tsub = await _pair(env)
    assert isub is not None
    before = await paid_until(env.db, isub)
    outs = await asyncio.gather(*(env.svc.process_pair(invitee) for _ in range(4)))
    assert sum(len(o.granted) for o in outs) == 2
    assert _close(await paid_until(env.db, isub), before + 14 * DAY)
    assert inviter


async def test_pair_with_concurrent_purchase_has_no_deadlock(env: Env) -> None:
    """Lock order users → subscriptions → referrals, the same as the purchase path (lesson 2 of 05 §2.3.5)."""
    from svbg.subscriptions.lifecycle import SubscriptionLifecycle

    inviter, invitee, _isub, _tsub = await _pair(env)
    lifecycle = SubscriptionLifecycle()
    terms = {"plan_id": 1, "squads": ["11111111-1111-4111-8111-111111111111"], "device_limit": 5}

    async def buy(user: int, ref: str) -> None:
        async with env.db.tx() as conn:
            await lifecycle.purchase(conn, user_id=user, terms=terms, days=30, ref_id=ref)

    await asyncio.wait_for(
        asyncio.gather(
            env.svc.process_pair(invitee),
            buy(inviter, "o-1"),
            buy(invitee, "o-2"),
            env.svc.process_pair(invitee),
        ),
        timeout=30,
    )
    rows = await rewards(env.db, invitee)
    assert {s: r["status"] for s, r in rows.items()} == {"inviter": "granted", "invitee": "granted"}


async def test_no_pair_and_percent_mode(env: Env) -> None:
    lonely = await add_user(env.db)
    assert (await env.svc.process_pair(lonely)).skipped == "no_pair"
    _inviter, invitee, _isub, _tsub = await _pair(env)
    env.cfg["REFERRAL_MODE"] = "percent"
    assert (await env.svc.process_pair(invitee)).skipped == "off"
    assert await rewards(env.db, invitee) == {}


async def test_sweep_catches_a_lost_hook(env: Env) -> None:
    _inviter, invitee, _isub, _tsub = await _pair(env)
    expired, queued = await env.svc.sweep_once()
    assert (expired, queued) == (0, 1)
    await env.drain()
    assert {r["status"] for r in (await rewards(env.db, invitee)).values()} == {"granted"}
    # Settled pairs are not queued again.
    assert await env.svc.sweep_once() == (0, 0)
