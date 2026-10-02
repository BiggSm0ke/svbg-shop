"""Percent mode (basic): a share of every paid purchase of the invited user goes to the inviter's wallet."""

from __future__ import annotations

import pytest

from svbg.core.bus import Event
from svbg.jobs.worker import PermanentJobError
from tests.referral.kit import Env, add_user, bind, job, jobs


@pytest.fixture
def percent(env: Env) -> Env:
    env.cfg["REFERRAL_MODE"] = "percent"
    env.cfg["REFERRAL_PERCENT"] = 10
    return env


def _fulfilled(order_id: int, user_id: int, total: int, kind: str = "new") -> Event:
    return Event(
        "order.fulfilled",
        {"order_id": order_id, "user_id": user_id, "kind": kind, "total_minor": total, "currency": "RUB"},
    )


async def _wallet(env: Env, user_id: int) -> int:
    return int((await env.db.raw("select wallet_minor from users where id = $1", user_id))[0]["wallet_minor"])


async def test_purchase_credits_the_inviter_once(percent: Env) -> None:
    env = percent
    inviter = await add_user(env.db, username="anya")
    invitee = await add_user(env.db, first_name="Боб")
    await bind(env.db, invitee, inviter)
    await env.bus.publish(_fulfilled(41, invitee, 17_900))
    await env.bus.publish(_fulfilled(41, invitee, 17_900))  # a repeated event while the job waits
    assert len(await jobs(env.db, "referral.pct")) == 1
    await env.drain()
    assert await _wallet(env, inviter) == 1_790
    ledger = await env.db.raw(
        "select amount_minor, reason, ref_type, ref_id from wallet_ledger where user_id = $1", inviter
    )
    assert [tuple(r.values()) for r in ledger] == [(1_790, "bonus", "referral", "order:41")]
    rows = await env.db.raw("select kind, status, amount_minor, payment_id, user_id from referral_rewards")
    assert [tuple(r.values()) for r in rows] == [("wallet_pct", "granted", 1_790, "order:41", inviter)]
    # The same order once more (a late duplicate job): nothing is credited twice.
    assert await env.svc.reward_percent(41, invitee, 17_900, "RUB") == 0
    assert await _wallet(env, inviter) == 1_790
    texts = [t for _, t, _ in env.sender.sent]
    assert texts == ["💰 +17,90 ₽ на баланс за покупку приглашённого Боб."]
    [(kind, report, _)] = env.poster.posts
    assert kind == "partners" and "+17,90 ₽" in report and "Заказ №41" in report


async def test_no_referrer_topup_and_days_mode(percent: Env) -> None:
    env = percent
    lonely = await add_user(env.db)
    assert await env.svc.reward_percent(1, lonely, 10_000, "RUB") == 0
    inviter, invitee = await add_user(env.db), await add_user(env.db)
    await bind(env.db, invitee, inviter)
    await env.bus.publish(_fulfilled(2, invitee, 10_000, kind="topup"))
    await env.bus.publish(_fulfilled(3, invitee, 0))
    assert await jobs(env.db, "referral.pct") == []
    env.cfg["REFERRAL_MODE"] = "days"
    await env.bus.publish(_fulfilled(4, invitee, 10_000))
    assert await jobs(env.db, "referral.pct") == []
    assert await env.svc.reward_percent(4, invitee, 10_000, "RUB") == 0


async def test_small_amount_rounds_to_nothing(percent: Env) -> None:
    env = percent
    inviter, invitee = await add_user(env.db), await add_user(env.db)
    await bind(env.db, invitee, inviter)
    assert await env.svc.reward_percent(5, invitee, 9, "RUB") == 0
    assert await env.db.raw("select * from referral_rewards") == []


async def test_broken_job_payload_is_permanent(percent: Env) -> None:
    with pytest.raises(PermanentJobError):
        await percent.svc.handlers()["referral.pct"](job("referral.pct", {"order_id": "x"}), None)  # type: ignore[arg-type]
