"""Message jobs: Russian only, overrides with fallback, skipped users, failures, admin topic."""

from __future__ import annotations

import pytest

from svbg.core.settings.registry import core_registry
from svbg.jobs.worker import PermanentJobError
from svbg.referral import texts
from svbg.referral.config import SETTINGS, register_settings
from svbg.referral.wiring import PARTNERS_TOPIC, build
from tests.referral.kit import Env, add_user, job


def _msg(user_id: int, key: str = "granted_inviter", **values: object) -> dict[str, object]:
    return {"user_id": user_id, "key": key, "values": {"days": 14, "name": "@bob", **values}}


async def _run(env: Env, payload: dict[str, object]) -> None:
    await env.svc.handlers()["referral.notify"](job("referral.notify", payload), None)  # type: ignore[arg-type]


async def test_messages_are_russian_whatever_the_stored_language(env: Env) -> None:
    uid = await add_user(env.db, language="en", tg=777)  # left from the old bot
    await _run(env, _msg(uid))
    assert env.sender.sent == [(777, "🎁 +14 дн. подписки за приглашённого @bob!", "HTML")]


async def test_override_and_broken_override(env: Env) -> None:
    uid = await add_user(env.db, tg=779)
    env.svc.overrides = lambda lang, key: (
        "Спасибо! +{days} дн. за {name}" if key == "referral.granted_inviter" else None
    )
    await _run(env, _msg(uid))
    assert env.sender.sent[-1][1] == "Спасибо! +14 дн. за @bob"
    env.svc.overrides = lambda lang, key: "сломано {nope}"
    await _run(env, _msg(uid))
    assert env.sender.sent[-1][1] == "🎁 +14 дн. подписки за приглашённого @bob!"

    def boom(lang: str, key: str) -> str:
        raise RuntimeError("store down")

    env.svc.overrides = boom
    await _run(env, _msg(uid))
    assert env.sender.sent[-1][1] == "🎁 +14 дн. подписки за приглашённого @bob!"


async def test_blocked_banned_and_missing_users_are_skipped(env: Env) -> None:
    blocked = await add_user(env.db)
    await env.db.raw("update users set bot_blocked_at = now() where id = $1", blocked)
    banned = await add_user(env.db)
    await env.db.raw("update users set banned_at = now() where id = $1", banned)
    for uid in (blocked, banned, 999_999):
        await _run(env, _msg(uid))
    assert env.sender.sent == []


async def test_send_failure_propagates_for_a_retry(env: Env) -> None:
    uid = await add_user(env.db)
    env.sender.fail = ConnectionError("telegram down")
    with pytest.raises(ConnectionError):
        await _run(env, _msg(uid))


async def test_no_sender_or_poster_drops_quietly(env: Env) -> None:
    uid = await add_user(env.db)
    env.svc.sender = None
    env.svc.poster = None
    await _run(env, _msg(uid))
    await _run(env, {"admin": "report"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_id": 1},
        {"user_id": 1, "key": "granted_inviter", "values": []},
        {"user_id": 1, "key": "nope", "values": {}},
    ],
)
async def test_broken_payload_is_permanent(env: Env, payload: dict[str, object]) -> None:
    with pytest.raises(PermanentJobError):
        await _run(env, payload)


async def test_admin_report_goes_to_partners(env: Env) -> None:
    await _run(env, {"admin": "🎁 report"})
    assert env.poster.posts == [("partners", "🎁 report", True)]


def test_mention_order_and_escaping() -> None:
    assert texts.mention("bob", "Боб", 1, 2) == "@bob"
    assert texts.mention(None, "<Боб>", 1, 2) == "&lt;Боб&gt;"
    assert texts.mention(None, "  ", 123, 2) == "ID 123"
    assert texts.mention(None, None, None, 2) == "#2"
    assert texts.admin_who("a&b", "<x>", 5, 1) == "&lt;x&gt; (@a&amp;b, id <code>5</code>)"
    assert texts.fmt_date("garbage") == "—"


def test_settings_register_into_the_core_registry() -> None:
    registry = core_registry()
    register_settings(registry)
    register_settings(registry)  # idempotent
    for defn in SETTINGS:
        assert registry.get(defn.key).section == "referral"
    assert registry.get("REFERRAL_TRIGGER").choices == ("paid", "trial_or_paid", "register")
    assert registry.get("REFERRAL_ENABLED").default is False


async def test_wiring_builds_and_registers(env: Env) -> None:
    from svbg.core.bus import EventBus
    from svbg.jobs.scheduler import Scheduler

    handlers: dict[str, object] = {}
    bus = EventBus()
    scheduler = Scheduler(None)
    svc = build(env.db, config=lambda: env.cfg, bus=bus, scheduler=scheduler, job_handlers=handlers)  # type: ignore[arg-type]
    assert set(handlers) == {"referral.attached", "referral.pair", "referral.pct", "referral.notify"}
    assert "referral.sweep" in scheduler.tasks()
    assert bus.handlers_for("trial.activated") == (svc.on_event,)
    assert PARTNERS_TOPIC.kind == "partners" and PARTNERS_TOPIC.label == "🤝 Партнёры"
