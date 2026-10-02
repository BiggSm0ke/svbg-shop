"""Binding a referrer by code: no retro-binding, no self/cycles, one SQL + one job; the attached job."""

from __future__ import annotations

import pytest

from svbg.deeplinks.ports import referral_port
from tests.dbkit import CountingDatabase
from tests.referral.kit import Env, add_sub, add_trial, add_user, bind, jobs, set_code


async def _referrer(db: CountingDatabase, code: str = "abc123") -> int:
    uid = await add_user(db, username="anya", first_name="Аня")
    await set_code(db, uid, code)
    return uid


async def test_attach_binds_a_fresh_user_in_two_statements(env: Env) -> None:
    referrer = await _referrer(env.db)
    newbie = await add_user(env.db, first_name="Боб")
    before = env.db.queries
    assert await env.svc.attach_referrer(newbie, "abc123") is True
    assert env.db.queries - before == 1  # binding + job + NOTIFY in one statement
    rows = await env.db.raw("select referrer_id, source from referrals where referred_user_id = $1", newbie)
    assert [(r["referrer_id"], r["source"]) for r in rows] == [(referrer, "link")]
    assert len(await jobs(env.db, "referral.attached")) == 1


async def test_attach_works_through_the_deeplink_port(env: Env) -> None:
    await _referrer(env.db)
    newbie = await add_user(env.db)
    port = referral_port(env.svc)
    assert port is not None
    assert await port.attach_referrer(newbie, "abc123") is True


@pytest.mark.parametrize("code", ["nope", "", "bad code", "x" * 63, "<script>"])
async def test_unknown_or_malformed_code(env: Env, code: str) -> None:
    await _referrer(env.db)
    newbie = await add_user(env.db)
    assert await env.svc.attach_referrer(newbie, code) is False


async def test_program_off_binds_nothing(env: Env) -> None:
    await _referrer(env.db)
    newbie = await add_user(env.db)
    env.cfg["REFERRAL_ENABLED"] = False
    before = env.db.queries
    assert await env.svc.attach_referrer(newbie, "abc123") is False
    assert env.db.queries == before


async def test_own_code_is_refused(env: Env) -> None:
    referrer = await _referrer(env.db)
    assert await env.svc.attach_referrer(referrer, "abc123") is False


async def test_no_rebinding(env: Env) -> None:
    await _referrer(env.db)
    other = await add_user(env.db)
    await set_code(env.db, other, "other1")
    newbie = await add_user(env.db)
    assert await env.svc.attach_referrer(newbie, "abc123") is True
    assert await env.svc.attach_referrer(newbie, "other1") is False
    rows = await env.db.raw("select count(*) as n from referrals where referred_user_id = $1", newbie)
    assert rows[0]["n"] == 1


@pytest.mark.parametrize("history", ["subscription", "trial", "wallet", "closed_subscription"])
async def test_no_retro_binding(env: Env, history: str) -> None:
    """Already paid / subscribed users cannot gift days to a stranger via /start <code> (05 §2.3.1)."""
    await _referrer(env.db)
    old = await add_user(env.db)
    if history == "subscription":
        await add_sub(env.db, old)
    elif history == "closed_subscription":
        await add_sub(env.db, old, link_state="closed")
    elif history == "trial":
        await add_trial(env.db, old)
    else:
        await env.db.raw("update users set wallet_minor = 100 where id = $1", old)
        await env.db.raw(
            "insert into wallet_ledger (user_id, amount_minor, currency, balance_after, reason, ref_type, "
            "ref_id) values ($1, 100, 'RUB', 100, 'topup', 'payment', 'p1')",
            old,
        )
    assert await env.svc.attach_referrer(old, "abc123") is False


async def test_banned_referrer_or_user(env: Env) -> None:
    referrer = await _referrer(env.db)
    newbie = await add_user(env.db)
    await env.db.raw("update users set banned_at = now() where id = $1", referrer)
    assert await env.svc.attach_referrer(newbie, "abc123") is False
    await env.db.raw("update users set banned_at = null where id = $1", referrer)
    await env.db.raw("update users set banned_at = now() where id = $1", newbie)
    assert await env.svc.attach_referrer(newbie, "abc123") is False


async def test_cycle_is_refused(env: Env) -> None:
    a = await _referrer(env.db, "codea1")
    b = await add_user(env.db)
    await set_code(env.db, b, "codeb1")
    await bind(env.db, b, a)  # A invited B
    assert await env.svc.attach_referrer(a, "codeb1") is False


async def test_unknown_user_is_refused(env: Env) -> None:
    await _referrer(env.db)
    assert await env.svc.attach_referrer(987_654_321, "abc123") is False


async def test_attached_job_sends_welcomes_and_publishes(env: Env) -> None:
    referrer = await _referrer(env.db)
    newbie = await add_user(env.db, first_name="Боб", language="en")
    assert await env.svc.attach_referrer(newbie, "abc123")
    await env.drain()
    by_chat = await _chats(env)
    assert by_chat[referrer][0].startswith("👥 Новый друг по вашей ссылке: Боб!")
    assert "+14 дн." in by_chat[referrer][0]
    assert by_chat[newbie][0].startswith("🎉 You were invited by @anya.")
    assert "+7 days" in by_chat[newbie][0]
    assert [(e.payload["user_id"], e.payload["referrer_id"]) for e in env.events] == [(newbie, referrer)]
    assert await jobs(env.db, "referral.pair", "done") == []  # trial_or_paid: nothing to grant yet


async def test_register_trigger_queues_the_pair(env: Env) -> None:
    referrer = await _referrer(env.db)
    inviter_sub = await add_sub(env.db, referrer)
    env.cfg["REFERRAL_TRIGGER"] = "register"
    newbie = await add_user(env.db)
    assert await env.svc.attach_referrer(newbie, "abc123")
    await env.drain()
    rows = await env.db.raw(
        "select side, status, reason from referral_rewards where referred_user_id = $1 order by side", newbie
    )
    # The inviter gets the days at once; the newbie has no subscription yet → deferred (no free emission).
    assert [(r["side"], r["status"], r["reason"]) for r in rows] == [
        ("invitee", "deferred", "no_subscription"),
        ("inviter", "granted", None),
    ]
    assert inviter_sub


async def test_percent_mode_welcome(env: Env) -> None:
    referrer = await _referrer(env.db)
    env.cfg["REFERRAL_MODE"] = "percent"
    newbie = await add_user(env.db)
    assert await env.svc.attach_referrer(newbie, "abc123")
    await env.drain()
    by_chat = await _chats(env)
    assert "10% с его покупок" in by_chat[referrer][0]


async def _chats(env: Env) -> dict[int, list[str]]:
    ids = await env.db.raw("select id, telegram_id from users")
    by_tg = {r["telegram_id"]: r["id"] for r in ids}
    out: dict[int, list[str]] = {}
    for chat, text, mode in env.sender.sent:
        assert mode == "HTML"
        out.setdefault(by_tg[chat], []).append(text)
    return out


async def test_register_days_and_welcomes_wait_for_the_captcha(env: Env) -> None:
    """A bot that opens a referral link but never passes the entry captcha brings its inviter nothing: no
    «Новый друг», no days «за переход». The right tap releases both, once."""
    referrer = await _referrer(env.db)
    await add_sub(env.db, referrer)
    env.cfg["REFERRAL_TRIGGER"] = "register"
    env.cfg["CAPTCHA_ENABLED"] = True
    bot = await add_user(env.db, first_name="Бот")
    assert await env.svc.attach_referrer(bot, "abc123")
    await env.drain()
    assert env.sender.sent == [] and env.events == []
    assert await env.db.raw("select 1 from referral_rewards where referred_user_id = $1", bot) == []
    # the hourly catch-up sweep does not pay for it either
    outcome = await env.svc.process_pair(bot)
    assert not outcome.granted
    assert await env.db.raw("select 1 from referral_rewards where referred_user_id = $1", bot) == []

    await env.db.raw("update users set captcha_passed_at = now() where id = $1", bot)
    assert await env.svc.captcha_passed(bot) is True
    assert await env.svc.captcha_passed(bot) is True  # a repeat queues nothing new (one dedup key)
    await env.drain()
    by_chat = await _chats(env)
    welcome, granted = by_chat[referrer]  # once each, despite the repeat
    assert welcome.startswith("👥 Новый друг по вашей ссылке: Бот!") and granted.startswith("🎁 +14 дн.")
    assert len(by_chat[bot]) == 1
    rows = await env.db.raw(
        "select side, status from referral_rewards where referred_user_id = $1 order by side", bot
    )
    assert [(r["side"], r["status"]) for r in rows] == [("invitee", "deferred"), ("inviter", "granted")]
    assert await env.svc.captcha_passed(referrer) is False  # nobody invited the inviter


async def test_staff_and_the_switched_off_captcha_do_not_hold_the_welcome(env: Env) -> None:
    await _referrer(env.db)
    env.cfg["CAPTCHA_ENABLED"] = False
    newbie = await add_user(env.db, first_name="Боб")
    assert await env.svc.attach_referrer(newbie, "abc123")
    await env.drain()
    assert len((await _chats(env))[newbie]) == 1
    env.cfg["CAPTCHA_ENABLED"] = True
    helper = await add_user(env.db, first_name="Саппорт")
    await env.db.raw("update users set role = 'support' where id = $1", helper)  # staff never see it
    assert await env.svc.attach_referrer(helper, "abc123")
    await env.drain()
    assert len((await _chats(env))[helper]) == 1
