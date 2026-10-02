"""The owner's 14 freeze scenarios (05 §2.2.4) on the **real path** on PostgreSQL: IP Guard block (core
``hold.freeze``) → purchases / grants / «🗑 Закрыть блок» (``zero_hold``) during the block → unblock (core
``hold.unfreeze``), checking the outcome and the new ``paid_until``.

Mapping of the owner's ``credited_seconds`` to SvBG: a purchase while frozen goes to the frozen balance
(``SubscriptionLifecycle.purchase``); any other extension (admin, panel date credited by the admin, nightly
import) is ``SubscriptionLifecycle.extend`` of exactly the granted seconds; zeroing is the block's «Закрыть».
``owner_kind=panel`` / ``bot_nosub`` collapse to "the subscription's ``paid_until`` at the block" (``None`` =
no paid term).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from svbg.core.clock import now
from svbg.subscriptions.lifecycle import SubscriptionLifecycle
from tests.ext.ip_guard.kit import GuardEnv, guard_env
from tests.subscriptions.kit import frozen_clock, plan_row

DAY = 86_400
LIFE = SubscriptionLifecycle()

# (at day, kind, value): kind "purchase" (days of a paid plan), "grant" (± seconds), "zero" (close the block)
Event = tuple[float, str, int]

CASES: list[tuple[str, float | None, float, tuple[Event, ...], str, float]] = [
    # name, paid_until at block (days from B), unblock at (days), events, outcome, paid_until after (days)
    ("1 short block returns the rest", 10, 3, (), "active", 13),
    ("2 block longer than the rest", 2, 20, (), "active", 22),
    ("3 purchase before the rest ran out", 10, 20, ((5, "purchase", 30),), "active", 60),
    ("4 purchase after the rest ran out", 2, 20, ((10, "purchase", 30),), "active", 52),
    ("5 expired before the block", -1, 3, (), "expired", 3),
    ("6 zeroed during the block", 10, 3, ((1, "zero", 0),), "zeroed", 3),
    ("7 zeroed then granted", 10, 3, ((1, "zero", 0), (2, "grant", 7 * DAY)), "active", 10),
    ("8 deduction", 10, 2, ((1, "grant", -3 * DAY),), "active", 9),
    ("9 panel extension credited once", 10, 2, ((1, "grant", 30 * DAY),), "active", 42),
    ("10 panel-only payment credited", 5, 1, ((0.5, "grant", 30 * DAY),), "active", 36),
    ("11 no paid term, granted during block", None, 2, ((1, "grant", 30 * DAY),), "active", 32),
    ("12 nightly import changes the date", 10, 3, ((1, "grant", 2 * DAY),), "active", 15),
    ("13 panel ahead at the block not doubled", 40, 5, (), "active", 45),
    ("14 panel extended seconds before click", 10, 3, ((2.999, "grant", 30 * DAY),), "active", 43),
]


async def blocked(g: GuardEnv, paid_days: float | timedelta | None) -> tuple[int, int, int, datetime]:
    """A bought, linked subscription whose ``paid_until`` is set to B + ``paid_days`` and then blocked at B.

    Returns ``(user id, subscription id, block id, B)``."""
    uid = await g.env.add_user(4242)
    async with g.db.tx() as conn:
        res = await LIFE.purchase(conn, user_id=uid, terms=plan_row(g.env.squad), days=30, ref_id="o1")
    await g.env.drain()
    sid = res.subscription_id
    at = now()
    rest = paid_days if isinstance(paid_days, timedelta) else timedelta(days=paid_days or 0)
    paid = None if paid_days is None else at + rest
    await g.db.raw("update subscriptions set paid_until = $1 where id = $2", paid, sid)
    assert (await g.service.manual_block(sid, actor_id=None)).ok
    block_id = int((await g.one("select id from ip_guard_blocks where subscription_id = $1", sid))["id"])
    return uid, sid, block_id, at


async def apply_event(g: GuardEnv, uid: int, sid: int, block_id: int, kind: str, value: int, n: int) -> None:
    if kind == "zero":
        res = await g.service.close(block_id, actor_id=None, reason="тест: обнулить")
        assert res.ok, res
        return
    async with g.db.tx() as conn:
        if kind == "purchase":
            await LIFE.purchase(conn, user_id=uid, terms=plan_row(g.env.squad), days=value, ref_id=f"e{n}")
        else:
            await LIFE.extend(conn, sid, value, source="admin", reason="тест")


@pytest.mark.parametrize(
    ("name", "paid", "unblock_at", "events", "outcome", "after"), CASES, ids=[c[0] for c in CASES]
)
async def test_freeze_table_on_the_real_path(
    pg_dsn: str,
    name: str,
    paid: float | None,
    unblock_at: float,
    events: tuple[Event, ...],
    outcome: str,
    after: float,
) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            uid, sid, block_id, b = await blocked(g, paid)
            frozen = max(0, int(paid * DAY)) if paid is not None else 0
            assert (await g.env.sub(sid))["hold_frozen_seconds"] == frozen, name
            for n, (day, kind, value) in enumerate(events):
                clock.set(b + timedelta(seconds=round(day * DAY)))
                await apply_event(g, uid, sid, block_id, kind, value, n)
                assert (await g.env.sub(sid))["paid_until"] == (
                    None if paid is None else b + timedelta(days=paid)
                ), f"{name}: the frozen date never moves"
            clock.set(b + timedelta(days=unblock_at))
            res = await g.service.unblock(block_id, actor_id=None)
            assert (res.ok, res.code) == (True, outcome), name
            sub = await g.env.sub(sid)
            assert sub["hold_kind"] is None and sub["hold_frozen_seconds"] == 0, name
            assert sub["paid_until"] == b + timedelta(days=after), name
            row = await g.one("select outcome, new_paid_until from ip_guard_blocks where id = $1", block_id)
            assert (row["outcome"], row["new_paid_until"]) == (outcome, b + timedelta(days=after)), name


async def test_owner_scenario_10_matches_panel_reference(pg_dsn: str) -> None:
    # the owner computed it from the panel date E0 = B + 5 d: T = now + 5 d + 30 d (now = B + 1 d)
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            uid, sid, block_id, b = await blocked(g, 5)
            await apply_event(g, uid, sid, block_id, "grant", 30 * DAY, 0)
            clock.set(b + timedelta(days=1))
            await g.service.unblock(block_id, actor_id=None)
            assert (await g.env.sub(sid))["paid_until"] == clock() + timedelta(days=35)


async def test_closed_subscription_is_gone(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            _, sid, block_id, _ = await blocked(g, 10)
            await g.db.raw("update subscriptions set link_state = 'closed' where id = $1", sid)
            res = await g.service.unblock(block_id, actor_id=None)
            assert res.code == "gone"


@pytest.mark.parametrize(
    ("left", "outcome"), [(timedelta(minutes=4), "expired"), (timedelta(minutes=5), "active")]
)
async def test_remainder_below_five_minutes_is_expired(pg_dsn: str, left: timedelta, outcome: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            _, sid, block_id, b = await blocked(g, left)
            clock.set(b + timedelta(days=1))
            assert (await g.service.unblock(block_id, actor_id=None)).code == outcome
            assert (await g.env.sub(sid))["paid_until"] == clock() + left


async def test_deduction_never_goes_below_zero(pg_dsn: str) -> None:
    async with guard_env(pg_dsn) as g:
        with frozen_clock() as clock:
            uid, sid, block_id, b = await blocked(g, 1)
            await apply_event(g, uid, sid, block_id, "grant", -5 * DAY, 0)
            assert (await g.env.sub(sid))["hold_frozen_seconds"] == 0  # clamped, not −4 days
            await apply_event(g, uid, sid, block_id, "grant", 3 * DAY, 1)
            clock.set(b + timedelta(days=2))
            await g.service.unblock(block_id, actor_id=None)
            assert (await g.env.sub(sid))["paid_until"] == clock() + timedelta(days=3)


@pytest.mark.parametrize(("paid", "frozen"), [(timedelta(hours=-3), 0), (timedelta(seconds=90), 90)])
async def test_only_paid_rest_is_frozen_never_grace(pg_dsn: str, paid: timedelta, frozen: int) -> None:
    # lesson 5: a date in the past (grace overlay, "closing" date) is never frozen as paid days
    async with guard_env(pg_dsn) as g:
        with frozen_clock():
            _, sid, _, _ = await blocked(g, paid)
            assert (await g.env.sub(sid))["hold_frozen_seconds"] == frozen
            block: Any = await g.one(
                "select frozen_seconds from ip_guard_blocks where subscription_id = $1", sid
            )
            assert block["frozen_seconds"] == frozen
