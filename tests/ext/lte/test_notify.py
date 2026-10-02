"""LTE notifications (05 §2.1.5): formats, quiet hours, ``notification_log`` dedup, rendering at sending time
(the pack button only when ``availability`` passes then), skips, and best-effort admin cards."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

import pytest

from svbg.ext.lte import notify
from svbg.ext.lte.decide import NotifyRequest
from tests.ext.lte.rkit import GB, FakeAdminChat, lte_env


@pytest.mark.parametrize(
    ("value", "text"),
    [(0, "0"), (12_400_000_000, "12,4"), (50 * GB, "50"), (None, "∞"), (-5, "0"), (2**30, "1,1")],
)
def test_fmt_gb(value: int | None, text: str) -> None:
    assert notify.fmt_gb(value) == text


def test_fmt_date_is_moscow_time() -> None:
    assert notify.fmt_date(datetime(2026, 10, 6, 21, 30, tzinfo=UTC)) == "07.10"
    assert notify.fmt_date(None) == "—"


def test_group_name_never_leaks_internal_words() -> None:
    assert notify.group_name({"ru": "LTE", "en": "Mobile"}, "en") == "Mobile"
    assert notify.group_name({"en": "Mobile"}, "ru") == "Mobile"
    assert notify.group_name({}) == "LTE" and notify.group_name(None) == "LTE"


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("00:00-09:00", (time(0), time(9))), ("23-7", (time(23), time(7))), ("", None), ("off", None)],
)
def test_parse_quiet(raw: str, parsed: Any) -> None:
    assert notify.parse_quiet(raw) == parsed


def test_parse_quiet_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        notify.parse_quiet("ночью")


@pytest.mark.parametrize(
    ("utc", "until"),
    [
        (datetime(2026, 10, 2, 22, 0, tzinfo=UTC), datetime(2026, 10, 3, 6, 0, tzinfo=UTC)),  # 01:00 MSK
        (datetime(2026, 10, 2, 7, 0, tzinfo=UTC), None),  # 10:00 MSK — outside
    ],
)
def test_quiet_until(utc: datetime, until: datetime | None) -> None:
    assert notify.quiet_until(utc, (time(0), time(9))) == until
    assert notify.quiet_until(utc, None) is None


def test_quiet_window_across_midnight() -> None:
    at = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)  # 23:30 MSK
    assert notify.quiet_until(at, (time(23), time(7))) == datetime(2026, 10, 3, 4, 0, tzinfo=UTC)


@pytest.mark.pg
async def test_warning_is_logged_once_per_period_and_sent_with_the_pack_button(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        await env.add_pack(10, 9_900)
        sid = await env.linked_sub(301)
        await env.open_period(sid, used=int(8.5 * GB))
        await env.service.process_subscription(sid)
        await env.service.process_subscription(sid)  # a second decision: no second warning
        rows = await env.db.raw(
            "select kind, anchor, status from notification_log where subscription_id = $1", sid
        )
        assert [(r["kind"], r["status"]) for r in rows] == [("lte_warn", "pending")]
        await env.drain()
        (sent,) = env.notifier.sent
        assert "осталось 1,5 ГБ из 10 ГБ" in sent.text and sent.kwargs["parse_mode"] == "HTML"
        buttons = [b.text for row in sent.kwargs["reply_markup"].inline_keyboard for b in row]
        assert buttons == ["⚡ Докупить трафик LTE", "🔗 Подключиться"]
        rows = await env.db.raw("select status from notification_log where subscription_id = $1", sid)
        assert rows[0]["status"] == "sent"


@pytest.mark.pg
async def test_button_is_decided_at_sending_time(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        await env.add_pack(10, 9_900)
        sid = await env.linked_sub(302)
        await env.open_period(sid, used=9 * GB)
        await env.service.process_subscription(sid)
        env.config["LTE_TOPUP_ENABLED"] = False  # switched off before the job ran
        await env.drain()
        (sent,) = env.notifier.sent
        buttons = [b.text for row in sent.kwargs["reply_markup"].inline_keyboard for b in row]
        assert buttons == ["🔗 Подключиться"]


@pytest.mark.pg
async def test_quiet_hours_postpone_the_warning(pg_dsn: str) -> None:
    async with lte_env(pg_dsn, LTE_QUIET_HOURS="00:00-23:59") as env:
        sid = await env.linked_sub(303)
        await env.open_period(sid, used=9 * GB)
        await env.service.process_subscription(sid)
        await env.drain()
        assert env.notifier.sent == []
        rows = await env.db.raw("select next_run_at > now() as later from jobs where kind = 'lte.notify'")
        assert rows and rows[0]["later"]


@pytest.mark.pg
@pytest.mark.parametrize("why", ["frozen", "banned", "off", "send_failed", "blocked_bot"])
async def test_unreachable_or_frozen_users_are_skipped(pg_dsn: str, why: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(304)
        await env.open_period(sid, used=9 * GB)
        await env.service.process_subscription(sid)
        uid = await env.user_of(sid)
        if why == "frozen":
            await env.db.raw(
                "update subscriptions set hold_kind = 'ip_guard', hold_since = now() where id = $1", sid
            )
        elif why == "banned":
            await env.db.raw("update users set banned_at = now() where id = $1", uid)
        elif why == "off":
            env.config["LTE_NOTIFY_USER"] = False
        elif why == "send_failed":
            env.notifier.fail = RuntimeError("network")
        else:
            env.notifier.blocked = True
        await env.drain()
        assert env.notifier.sent == []
        rows = await env.db.raw("select status, reason from notification_log where subscription_id = $1", sid)
        assert rows[0]["status"] == "skipped" and rows[0]["reason"]


@pytest.mark.pg
async def test_record_needs_a_user_and_dedups(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(305)
        pid = await env.open_period(sid)
        uid = await env.user_of(sid)
        req = NotifyRequest(sid, 1, env.group_id, pid, "exhausted", used_bytes=GB, limit_bytes=GB)
        cfg = notify.NotifyConfig(quiet=None)
        async with env.db.tx() as conn:
            assert await notify.record(conn, req, user_id=None, cfg=cfg) is None
            first = await notify.record(conn, req, user_id=uid, block_id=5, cfg=cfg)
            assert first is not None
            assert await notify.record(conn, req, user_id=uid, block_id=5, cfg=cfg) is None
            # a new block in the same period (after a pack) is a new notification
            assert await notify.record(conn, req, user_id=uid, block_id=6, cfg=cfg) is not None
            assert await notify.record(conn, req, user_id=uid, cfg=notify.NotifyConfig(enabled=False)) is None


async def test_admin_cards_are_best_effort() -> None:
    chat = FakeAdminChat(fail=True)
    await notify.post_cards(chat, ["a"])  # never raises
    await notify.post_cards(None, ["a"])
    ok = FakeAdminChat()
    await notify.post_cards(ok, ["x", "y"])
    assert ok.posts == [("lte", "x"), ("lte", "y")]


async def test_admin_cards_are_reports() -> None:
    chat = FakeAdminChat()
    block = notify.card_report("block", sid=7, used="10", limit="10", reason="лимит")
    await notify.post_cards(chat, [block, notify.card_report("release_all", n=3, reason="emergency")])
    assert chat.reports[0] is block
    assert chat.posts[0] == (
        "lte",
        "🚫 <b>LTE: блок</b>\n\nПодписка: <b>№7</b>\nИзрасходовано: <b>10 из 10 ГБ</b>\n"
        "Причина: <b>лимит</b>",
    )
    assert "Снято: <b>3 шт.</b>" in chat.posts[1][1]
    # a table-like rich message where the chat takes them
    rich = block.rich().model_dump(exclude_none=True)
    assert [b["type"] for b in rich["blocks"]] == ["heading", "table"]
    # the job payload: structured fields, or the text of a job queued before the upgrade
    topup = notify.card_from_payload({"card": "topup", "sid": 5, "gb": "10", "text": "old"})
    assert isinstance(topup, notify.Report) and "Добавлено: +10 ГБ" in topup.plain_text()
    assert notify.card_from_payload({"text": "⚡ old"}) == "⚡ old"
    assert notify.card_from_payload({"card": "nope"}) is None
    # an admin chat without post_report gets the HTML text
    old = type("OldChat", (), {"post": FakeAdminChat.post, "posts": [], "fail": False})()
    await notify.post_cards(old, [block])
    assert old.posts == [("lte", block.html())]
