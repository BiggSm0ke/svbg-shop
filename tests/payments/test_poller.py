"""Reconciler and presence polling (D14): budgets per invoice, batches, rate limits, «Я оплатил»."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from svbg.core.crypto import Crypto
from svbg.payments import poller as poller_mod
from svbg.payments.poller import (
    DOMAIN_NEXT_S,
    ERROR_RETRY_S,
    EXPIRY_GRACE_S,
    FREQUENT_MAX,
    HARD_CAP,
    I_PAID_FETCH_TIMEOUT_S,
    MAX_PHASES,
    Poller,
    TokenBucket,
)
from svbg.payments.registry import InstanceSpec
from svbg.payments.testkit import CoreHarness, CountingHttp, check_poll_budget
from tests.dbkit import CountingDatabase
from tests.payments.conftest import STUB_CONFIG, BatchPay, Env, FakeStubServer, StubPay, build_env


class FakeTime:
    def __init__(self) -> None:
        self.wall = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        self.mono = 10_000.0
        self.slept = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.advance(seconds)


async def _env(db: CountingDatabase, crypto: Crypto, *, has_domain: bool) -> tuple[Env, FakeTime, Poller]:
    env = await build_env(db, crypto, has_domain=has_domain)
    t = FakeTime()
    env.core._clock = t.now
    poller = Poller(env.core, env.db, clock=t.now, monotonic=t.monotonic, sleep=t.sleep)
    return env, t, poller


async def _run(poller: Poller, t: FakeTime, seconds: float, step: float = 2.0) -> None:
    elapsed = 0.0
    while elapsed < seconds:
        await poller.tick()
        t.advance(step)
        elapsed += step


def _status_requests(env: Env) -> int:
    return sum(1 for c in env.http.calls if c.method == "GET" and c.url.endswith("/invoices"))


# ------------------------------------------------------------------------------------------ budgets


@pytest.mark.parametrize(("domain", "budget"), [(True, 3), (False, 24)], ids=["domain", "no-domain"])
async def test_testkit_budget_for_an_abandoned_checkout(
    db: CountingDatabase, crypto: Crypto, domain: bool, budget: int
) -> None:
    server = FakeStubServer()
    harness = await CoreHarness.create(db, crypto, StubPay, STUB_CONFIG, http=CountingHttp(server))
    try:
        used = await check_poll_budget(harness, domain=domain)
    finally:
        await harness.close()
    assert 0 < used <= budget
    assert used == (3 if domain else FREQUENT_MAX - 1 + 6)  # 17 frequent + 6 decay


async def test_batch_provider_makes_one_request_per_tick(db: CountingDatabase, crypto: Crypto) -> None:
    server = FakeStubServer()
    harness = await CoreHarness.create(db, crypto, BatchPay, STUB_CONFIG, http=CountingHttp(server))
    try:
        used = await check_poll_budget(harness, domain=False, invoices=7)
    finally:
        await harness.close()
    assert used <= 24
    assert max(len(ids) for ids in server.status_calls) == 7  # all invoices in one request


async def test_check_poll_budget_fails_a_greedy_poller(db: CountingDatabase, crypto: Crypto) -> None:
    from svbg.payments import poller as poller_mod
    from svbg.payments.testkit import KitFailure

    server = FakeStubServer()
    harness = await CoreHarness.create(db, crypto, StubPay, STUB_CONFIG, http=CountingHttp(server))
    original = poller_mod.DOMAIN_NEXT_S
    poller_mod._NEXT["domain"] = (60,) * 10  # a regression: checks every minute
    try:
        with pytest.raises(KitFailure, match="budget 3"):
            await check_poll_budget(harness, domain=True)
    finally:
        poller_mod._NEXT["domain"] = original
        await harness.close()


async def test_no_domain_schedule_frequent_then_decay(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()
    assert poller.present(pid)
    await _run(poller, t, 180)
    assert _status_requests(env) == FREQUENT_MAX - 1
    assert not poller.present(pid)
    row = await env.payment(pid)
    assert row["checks_used"] == FREQUENT_MAX - 1 and row["check_step"] == 0
    await _run(poller, t, 3 * 3600)
    row = await env.payment(pid)
    assert row["check_step"] == 6 and row["next_check_at"] is None and row["status"] == "pending"
    assert _status_requests(env) == FREQUENT_MAX - 1 + 6


async def test_domain_schedule_is_three_safety_checks(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    assert not poller.present(pid)
    await _run(poller, t, 9 * 60, step=10)
    assert _status_requests(env) == 0
    await _run(poller, t, 3 * 3600, step=10)
    assert _status_requests(env) == 3
    assert (await env.payment(pid))["checks_used"] == 3


# ------------------------------------------------------------------------------------------ settling


async def test_poll_settles_a_paid_invoice_and_stops(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()
    await _run(poller, t, 30)
    env.server.set("inv-1", "paid", "179.00")
    await _run(poller, t, 12)
    assert (await env.payment(pid))["status"] == "paid" and len(env.credited) == 1
    assert not poller.present(pid)  # forgotten after settling
    before = _status_requests(env)
    await _run(poller, t, 3 * 3600, step=10)
    assert _status_requests(env) == before


async def test_poll_applies_expiry_and_mismatch(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    p1 = await env.pending()
    p2 = await env.pending(user_id=await env.add_user(555_000_009))  # another user's screen
    env.server.set("inv-1", "expired", "179.00")
    env.server.set("inv-2", "paid", "100.00")
    await _run(poller, t, 12)
    assert (await env.payment(p1))["status"] == "expired"
    assert (await env.payment(p2))["status"] == "mismatch" and env.credited == []


async def test_provider_errors_do_not_break_the_tick(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    env.server.fail_status = True
    t.advance(601)
    stats = await poller.tick()
    assert stats.errors == 1 and stats.requests == 1 and poller.last_error
    row = await env.payment(pid)
    # The request counts towards the hard cap, but the step of the schedule is not used up: retry in 5 min.
    assert row["check_step"] == 0 and row["checks_used"] == 1
    assert row["next_check_at"] == t.now() + timedelta(seconds=ERROR_RETRY_S)
    env.server.fail_status = False
    env.server.raise_network = True
    t.advance(ERROR_RETRY_S + 1)
    stats = await poller.tick()
    assert stats.errors == 1 and (await env.payment(pid))["check_step"] == 0
    env.server.raise_network = False  # the desk is back: the schedule goes on where it stopped
    t.advance(ERROR_RETRY_S + 1)
    stats = await poller.tick()
    assert stats.errors == 0 and stats.requests == 1
    row = await env.payment(pid)
    assert row["check_step"] == 1 and row["checks_used"] == 3
    assert row["next_check_at"] == t.now() + timedelta(seconds=DOMAIN_NEXT_S[0])


# ------------------------------------------------------------------------------------------ presence


async def test_leave_stops_the_frequent_phase(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()
    await _run(poller, t, 30)
    used = _status_requests(env)
    assert used == 2
    poller.leave(env.user_id)
    assert not poller.present(pid)
    await _run(poller, t, 150)
    assert _status_requests(env) == used  # nothing until the decay (first check at +210 s)


async def test_presence_restarts_are_limited(db: CountingDatabase, crypto: Crypto) -> None:
    env, _t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()  # phase 1
    inst = env.inst()
    for _ in range(MAX_PHASES + 2):
        poller.leave(env.user_id)
        poller.touch(pid, env.user_id, inst.id)
    assert poller._phases[pid] == MAX_PHASES
    poller.leave(env.user_id)
    poller.touch(pid, env.user_id, inst.id)
    assert not poller.present(pid)
    poller.forget(pid)
    assert pid not in poller._phases


async def test_touch_keeps_a_running_phase(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()
    await _run(poller, t, 20)
    poller.touch(pid, env.user_id, env.inst().id)  # back on the screen while the phase runs
    assert poller._phases[pid] == 1


async def test_rate_limit_two_requests_per_second_per_instance(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    for _ in range(8):
        await env.pending()
    t.advance(601)
    stats = await poller.tick()
    assert stats.requests == 8
    # 2 tokens of burst, then 2 per second: the last 6 requests waited ≥ 3 s in total.
    assert t.slept >= 2.9


def test_token_bucket() -> None:
    now = [0.0]
    bucket = TokenBucket(2.0, clock=lambda: now[0])
    assert bucket.reserve() == 0 and bucket.reserve() == 0
    assert bucket.reserve() == pytest.approx(0.5)
    now[0] = 10.0
    assert bucket.reserve() == 0
    with pytest.raises(ValueError):
        TokenBucket(0, clock=lambda: 0.0)


async def test_at_most_four_provider_calls_at_once(db: CountingDatabase, crypto: Crypto) -> None:
    env = await build_env(db, crypto, has_domain=True)
    for n in range(6):
        await env.registry.save(
            InstanceSpec(slug=f"stub{n}", provider="stubpay", enabled=True, config=STUB_CONFIG)
        )
    for n in range(6):
        await env.pending(f"stub{n}")
    await env.db.raw("update payments set next_check_at = now() - interval '1 second'")
    env.server.delay = 0.05
    poller = Poller(env.core, env.db, rps=100.0)
    stats = await poller.tick()
    assert stats.requests == 6
    assert 1 < env.server.max_concurrent <= 4


# ------------------------------------------------------------------------------------------ «Я оплатил»


async def test_i_paid_checks_now_with_cooldown(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and rec.status == "pending" and _status_requests(env) == 1
    env.server.set("inv-1", "paid", "179.00")
    rec = await poller.check_now(pid, env.user_id)  # within the cooldown: no request
    assert rec is not None and rec.status == "pending" and _status_requests(env) == 1
    t.advance(11)
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and rec.status == "paid" and len(env.credited) == 1
    t.advance(11)
    await poller.check_now(pid, env.user_id)  # settled: no more requests
    assert _status_requests(env) == 2
    assert (await env.payment(pid))["checks_used"] == 2


async def test_i_paid_without_domain_restarts_presence(db: CountingDatabase, crypto: Crypto) -> None:
    env, _t, poller = await _env(db, crypto, has_domain=False)
    pid = await env.pending()
    poller.leave(env.user_id)
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and poller.present(pid)


async def test_i_paid_is_scoped_to_the_owner_and_capped(db: CountingDatabase, crypto: Crypto) -> None:
    env, _t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    other = await env.add_user(555_000_002)
    assert await poller.check_now(pid, other) is None
    await env.db.raw("update payments set checks_used = $1 where id = $2", HARD_CAP, pid)
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and _status_requests(env) == 0
    manual = await env.pending("manualpay", amount_minor=50_000)
    assert (await poller.check_now(manual, env.user_id)) is not None and _status_requests(env) == 0


async def test_run_once_is_a_scheduler_task(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    await env.pending()
    t.advance(11)
    await poller.run_once()
    assert poller.requests_total == 1
    await asyncio.sleep(0)


# ------------------------------------------------------------------------------------------ review fixes


async def test_new_invoice_ends_the_users_other_frequent_phases(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=False)
    first = await env.pending()
    await _run(poller, t, 20)
    second = await env.pending()  # «Другой способ»: a new invoice, the old screen is gone
    assert poller.present(second) and not poller.present(first)
    before = len(env.server.status_calls)
    await _run(poller, t, 60)
    polled = env.server.status_calls[before:]
    assert polled and all(ids == ["inv-2"] for ids in polled)


async def test_last_check_waits_for_the_invoice_expiry(db: CountingDatabase, crypto: Crypto) -> None:
    """A 24-hour invoice paid on the 5th hour without a webhook is still found by the reconciler."""
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    expires = t.now() + timedelta(hours=24)
    await env.db.raw(
        "update payments set created_at = $1, expires_at = $2 where id = $3", t.now(), expires, pid
    )
    t.advance(601)
    await poller.tick()
    t.advance(DOMAIN_NEXT_S[0] + 1)
    await poller.tick()
    assert _status_requests(env) == 2
    row = await env.payment(pid)
    assert row["check_step"] == 2 and row["next_check_at"] == expires + timedelta(seconds=EXPIRY_GRACE_S)
    t.advance(5 * 3600)
    env.server.set("inv-1", "paid", "179.00")
    await poller.tick()
    assert _status_requests(env) == 2  # nothing before the expiry: the budget stays 3
    t.advance(20 * 3600)
    await poller.tick()
    assert _status_requests(env) == 3 and (await env.payment(pid))["status"] == "paid"


async def test_last_check_after_a_far_expiry_is_bounded(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    await env.db.raw(
        "update payments set created_at = $1, expires_at = $2 where id = $3",
        t.now(),
        t.now() + timedelta(days=60),
        pid,
    )
    t.advance(601)
    await poller.tick()
    t.advance(DOMAIN_NEXT_S[0] + 1)
    await poller.tick()
    row = await env.payment(pid)
    assert row["next_check_at"] == row["created_at"] + timedelta(days=7)


async def test_i_paid_rereads_an_expired_invoice_within_a_day(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()
    await env.db.raw("update payments set expires_at = $1 where id = $2", t.now(), pid)
    env.server.set("inv-1", "expired", "179.00")
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and rec.status == "expired"
    env.server.set("inv-1", "paid", "179.00")  # the bank was slow: paid after the invoice expired
    t.advance(11)
    rec = await poller.check_now(pid, env.user_id)
    assert rec is not None and rec.status == "paid" and len(env.credited) == 1
    assert _status_requests(env) == 2


async def test_i_paid_does_not_reread_long_expired_or_settled(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    old = await env.pending()
    await env.db.raw(
        "update payments set status = $1, expires_at = $2 where id = $3",
        "expired",
        t.now() - timedelta(hours=25),
        old,
    )
    mismatch = await env.pending()
    await env.db.raw("update payments set status = $1 where id = $2", "mismatch", mismatch)
    for pid in (old, mismatch):
        rec = await poller.check_now(pid, env.user_id)
        assert rec is not None
    assert _status_requests(env) == 0


async def test_slow_provider_does_not_block_other_instances(
    db: CountingDatabase, crypto: Crypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await build_env(db, crypto, has_domain=False)
    t = FakeTime()
    env.core._clock = t.now
    poller = Poller(env.core, env.db, clock=t.now, monotonic=t.monotonic, sleep=t.sleep, tick_wait=0.05)
    slow, fast = env.inst("stubpay"), env.inst("weakpay")
    release = asyncio.Event()

    async def hang(_ids: object) -> list[object]:
        await release.wait()
        return []

    monkeypatch.setattr(slow.provider, "fetch_status", hang)
    p_slow = await env.pending("stubpay")
    await env.pending("weakpay", user_id=await env.add_user(555_000_003))
    t.advance(11)
    stats = await poller.tick()  # returns although the stub desk hangs
    assert stats.by_instance == {slow.id: 1, fast.id: 1}
    t.advance(10)
    stats = await poller.tick()
    assert stats.by_instance == {fast.id: 1}  # the hanging lane is skipped, the other desk goes on
    assert poller._presence[p_slow].count == 1  # the skipped check did not spend the phase budget
    release.set()
    await asyncio.wait(list(poller._lanes.values()))
    t.advance(10)
    stats = await poller.tick()
    assert stats.by_instance == {slow.id: 1, fast.id: 1}
    await poller.close()


async def test_reconciler_backlog_is_taken_in_portions(db: CountingDatabase, crypto: Crypto) -> None:
    env, t, poller = await _env(db, crypto, has_domain=True)
    for _ in range(13):
        await env.pending()
    t.advance(601)
    stats = await poller.tick()
    assert stats.requests == int(poller_mod.RPS_PER_INSTANCE * poller_mod.DB_LANE_S)  # 10
    t.advance(2)
    assert (await poller.tick()).requests == 0  # the next scan is 15 s later
    t.advance(15)
    assert (await poller.tick()).requests == 3


async def test_i_paid_does_not_wait_behind_a_slow_desk(
    db: CountingDatabase, crypto: Crypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svbg.tg.user.shop import I_PAID_TIMEOUT_S

    assert I_PAID_FETCH_TIMEOUT_S < I_PAID_TIMEOUT_S - 2  # room for the two SQL statements
    env, _t, poller = await _env(db, crypto, has_domain=True)
    pid = await env.pending()

    async def hang(_ids: object) -> list[object]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(env.inst().provider, "fetch_status", hang)
    monkeypatch.setattr(poller_mod, "I_PAID_FETCH_TIMEOUT_S", 0.05)
    for _ in range(poller_mod.MAX_CONCURRENCY):  # every background slot is busy
        await poller._sem.acquire()
    rec = await asyncio.wait_for(poller.check_now(pid, env.user_id), timeout=5)
    assert rec is not None and rec.status == "pending" and rec.id == pid
    assert (await env.payment(pid))["checks_used"] == 1
