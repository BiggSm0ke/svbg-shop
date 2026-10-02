"""Delivery job: counts, 403 → bot_blocked_at, copy fallback, options, pause/resume/stop, restart, rate."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pytest
from aiogram.methods import CopyMessage, DeleteMessage, EditMessageText, PinChatMessage, SendMessage

from svbg.broadcasts.repo import BroadcastRepo
from svbg.broadcasts.sender import JOB_CLEANUP, JOB_RUN, BroadcastSender
from svbg.broadcasts.service import Actor, BroadcastError, BroadcastService
from svbg.jobs import JobQueue, JobWorker, PermanentJobError
from tests.broadcasts.kit import FakeBot, FastClock, job, make_notifier, mk_many, mk_user
from tests.dbkit import CountingDatabase

ADMIN_TG = 42
TEXT = {
    "type": "text",
    "text": "🔥 Акция",
    "entities": [
        {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "5368324170671202286"},
        {"type": "spoiler", "offset": 3, "length": 5},
    ],
}


@dataclass
class Env:
    db: CountingDatabase
    bot: FakeBot
    clock: FastClock
    sender: BroadcastSender
    service: BroadcastService
    actor: Actor

    async def draft(self, **options: Any) -> int:
        bid = await self.service.create(self.actor, chat_id=ADMIN_TG, message_id=7, content=TEXT)
        if options:
            await self.service.repo.update_draft(bid, options=options)
        return bid

    async def finish(self, bid: int) -> int:
        slices = 1
        while await self.sender.run(bid):
            slices += 1
        return slices

    async def row(self, bid: int) -> dict[str, Any]:
        return dict((await self.db.raw("select * from broadcasts where id = $1", bid))[0])

    def copies(self) -> list[int]:
        return [int(c.chat_id) for c in self.bot.of(CopyMessage)]


async def make_env(db: CountingDatabase, *, limits: dict[str, Any] | None = None, **kw: Any) -> Env:
    admin = await mk_user(db, ADMIN_TG, role="owner")
    bot, clock = FakeBot(), FastClock()
    notifier = make_notifier(bot, clock, **(limits or {}))
    repo = BroadcastRepo(db)
    sender = BroadcastSender(db, notifier, repo=repo, monotonic=clock, **kw)
    return Env(db, bot, clock, sender, BroadcastService(repo, sender), Actor(admin, "owner"))


@pytest.fixture
async def env(db: CountingDatabase) -> Env:
    return await make_env(db)


async def test_full_run_counts_blocked_and_failed(env: Env) -> None:
    await mk_many(env.db, 30)
    env.bot.blocked = {100_003, 100_007}
    env.bot.bad = {100_010}
    bid = await env.draft()
    bc = await env.service.start(bid, env.actor, ADMIN_TG)
    assert bc.total == 31 and bc.progress_msg is not None
    jobs = await env.db.raw("select kind, dedup_key from jobs")
    assert [(j["kind"], j["dedup_key"]) for j in jobs] == [(JOB_RUN, f"broadcast:{bid}")]
    mark = env.db.queries
    await env.finish(bid)
    statements = env.db.queries - mark
    row = await env.row(bid)
    assert row["status"] == "done" and row["finished_at"] is not None
    assert (row["sent"], row["blocked"], row["failed"]) == (28, 2, 1)
    assert sorted(env.copies()) == sorted({ADMIN_TG, *range(100_000, 100_030)})  # each exactly once
    blocked = await env.db.raw("select telegram_id from users where bot_blocked_at is not null order by 1")
    assert [r["telegram_id"] for r in blocked] == [100_003, 100_007]
    copy = env.bot.of(CopyMessage)[0]
    assert (copy.from_chat_id, copy.message_id, copy.reply_markup) == (ADMIN_TG, 7, None)
    edits = [c for c in env.bot.of(EditMessageText) if c.chat_id == ADMIN_TG]
    assert edits and "✅ завершена" in edits[-1].text and "Отправлено: <b>28 из ~" in edits[-1].text
    assert statements <= 6  # 1 batch: get + select + save tx (update, users) + empty select + finish
    audit = await env.db.raw("select action, target from admin_audit")
    assert [(a["action"], a["target"]) for a in audit] == [("broadcast.start", f"broadcast:{bid}")]


async def test_copy_falls_back_to_the_normalized_copy(env: Env) -> None:
    await mk_many(env.db, 5)
    env.bot.source_gone = True
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    await env.finish(bid)
    sends = [c for c in env.bot.of(SendMessage) if c.text == "🔥 Акция"]
    assert sorted(int(c.chat_id) for c in sends) == sorted({ADMIN_TG, *range(100_000, 100_005)})
    assert all(c.parse_mode is None for c in sends)
    assert [e.type for e in sends[0].entities or ()] == ["custom_emoji", "spoiler"]
    row = await env.row(bid)
    assert row["sent"] == 6 and row["source_msg_id"] is None
    # a later broadcast run of the same row goes straight to send*
    assert (await env.service.repo.get(bid)).can_copy is False  # type: ignore[union-attr]


async def test_pin_silent_and_buttons(env: Env) -> None:
    await mk_many(env.db, 3)
    bid = await env.draft(pin=True, silent=True)
    await env.service.set_buttons(
        bid, [{"label": {"ru": "Купить"}, "action": {"type": "screen", "target": "buy"}}]
    )
    await env.service.start(bid, env.actor, ADMIN_TG)
    await env.finish(bid)
    copies = env.bot.of(CopyMessage)
    assert len(copies) == 4 and all(c.disable_notification is True for c in copies)
    markup = copies[0].reply_markup
    assert markup is not None and markup.inline_keyboard[0][0].callback_data == "v1:buy:o"
    pins = env.bot.of(PinChatMessage)
    assert len(pins) == 4 and all(p.disable_notification is True for p in pins)


async def test_pause_resume_never_sends_twice(env: Env) -> None:
    await mk_many(env.db, 200)
    bid = await env.draft()
    paused: list[asyncio.Future[Any]] = []

    async def on_call(n: int, _method: Any) -> None:
        if n == 70 and not paused:
            paused.append(asyncio.ensure_future(env.service.pause(bid, env.actor)))

    env.bot.on_call = on_call
    await env.service.start(bid, env.actor, ADMIN_TG)
    assert await env.sender.run(bid) is False
    await paused[0]
    row = await env.row(bid)
    assert row["status"] == "paused" and 0 < row["sent"] < 201
    first = Counter(env.copies())
    assert max(first.values()) == 1 and row["sent"] == len(first)
    await env.service.resume(bid, env.actor)
    await env.finish(bid)
    every = Counter(env.copies())
    assert set(every) == {ADMIN_TG, *range(100_000, 100_200)} and max(every.values()) == 1
    row = await env.row(bid)
    assert (row["status"], row["sent"]) == ("done", 201)
    actions = [a["action"] for a in await env.db.raw("select action from admin_audit order by id")]
    assert actions == ["broadcast.start", "broadcast.pause", "broadcast.resume"]


def hold_copy(env: Env, nth: int) -> tuple[asyncio.Event, asyncio.Event]:
    """Block the ``nth`` copy in flight (a send stuck behind the rate limit) → (reached, release)."""
    reached, release = asyncio.Event(), asyncio.Event()

    async def on_call(_n: int, method: Any) -> None:
        if isinstance(method, CopyMessage) and len(env.copies()) == nth and not reached.is_set():
            reached.set()
            await release.wait()

    env.bot.on_call = on_call
    return reached, release


async def test_stop_interrupts_the_running_batch_at_once(env: Env) -> None:
    await mk_many(env.db, 400)
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    reached, release = hold_copy(env, 20)
    task = asyncio.create_task(env.sender.run(bid))
    await asyncio.wait_for(reached.wait(), 10.0)
    await env.service.stop(bid, env.actor)  # never released: only the interrupt can end the batch
    mark = len(env.copies())
    assert await asyncio.wait_for(task, 10.0) is False
    assert len(env.copies()) == mark  # nothing is sent after «Остановить»
    row = await env.row(bid)
    assert row["status"] == "canceled" and row["sent"] < env.sender.batch_size
    assert await env.sender.run(bid) is False  # a late job run does nothing
    release.set()


async def test_stop_from_another_process_ends_after_the_batch(env: Env) -> None:
    await mk_many(env.db, 400)
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    reached, release = hold_copy(env, 20)
    task = asyncio.create_task(env.sender.run(bid))
    await asyncio.wait_for(reached.wait(), 10.0)
    # another process changed the status: no interrupt here, the batch-end UPDATE … RETURNING sees it
    await env.db.raw("update broadcasts set status = 'canceled' where id = $1", bid)
    mark = len(env.copies())
    release.set()
    assert await asyncio.wait_for(task, 10.0) is False
    assert len(env.copies()) - mark <= env.sender.batch_size
    assert (await env.row(bid))["sent"] <= env.sender.batch_size


async def test_restart_resumes_from_the_cursor(db: CountingDatabase) -> None:
    env = await make_env(db, batch_size=10, slice_s=1.0)
    await mk_many(db, 120)
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    assert await env.sender.run(bid) is True  # the slice ended, more to do
    before = (await env.row(bid))["cursor"]
    assert before > 0
    # "restart": a new sender (new process) continues from the stored cursor
    restarted = BroadcastSender(db, make_notifier(env.bot, env.clock), monotonic=env.clock, batch_size=10)
    while await restarted.run(bid):
        pass
    every = Counter(env.copies())
    assert set(every) == {ADMIN_TG, *range(100_000, 100_120)} and max(every.values()) == 1
    assert (await env.row(bid))["status"] == "done"


async def test_ten_thousand_recipients_take_about_seven_minutes(env: Env) -> None:
    await mk_many(env.db, 9_999)
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    t0 = env.clock.t
    slices = await env.finish(bid)
    elapsed = env.clock.t - t0
    row = await env.row(bid)
    assert (row["status"], row["sent"]) == ("done", 10_000)
    assert 380 <= elapsed <= 460, elapsed  # 10 000 / 25 per second ≈ 400 s ≈ 7 min
    assert slices >= 12  # 30 s slices: the job re-arms itself instead of holding a worker for minutes
    edits = [c for c in env.bot.of(EditMessageText) if c.chat_id == ADMIN_TG]
    assert 60 <= len(edits) <= 100  # every ~5 s


async def test_delete_after_hours(env: Env) -> None:
    await mk_many(env.db, 4)
    bid = await env.draft(delete_after_h=1)
    await env.service.start(bid, env.actor, ADMIN_TG)
    await env.finish(bid)
    msgs = await env.db.raw("select chat_id, msg_id from broadcast_msgs where broadcast_id = $1", bid)
    assert len(msgs) == 5
    jobs = await env.db.raw(
        "select next_run_at - now() as wait from jobs where kind = $1 and dedup_key = $2",
        JOB_CLEANUP,
        f"broadcast.cleanup:{bid}",
    )
    assert len(jobs) == 1 and 3000 < jobs[0]["wait"].total_seconds() <= 3600
    await env.db.raw(
        "update broadcast_msgs set delete_at = now() - interval '1 minute' where chat_id <> $1", ADMIN_TG
    )
    await env.sender.cleanup_job(job(bid), None)  # type: ignore[arg-type]
    deleted = sorted(int(c.chat_id) for c in env.bot.of(DeleteMessage))
    assert deleted == list(range(100_000, 100_004))
    left = await env.db.raw("select chat_id from broadcast_msgs")
    assert [r["chat_id"] for r in left] == [ADMIN_TG]
    pending = await env.db.raw("select count(*) as n from jobs where kind = $1", JOB_CLEANUP)
    assert pending[0]["n"] == 1  # still one job for the admin's message, due later


async def test_job_worker_runs_slices_until_done(db: CountingDatabase) -> None:
    env = await make_env(db, batch_size=10, slice_s=0.5)
    await mk_many(db, 80)
    bid = await env.draft()
    await env.service.start(bid, env.actor, ADMIN_TG)
    queue = JobQueue(db)
    worker = JobWorker(db, queue, env.sender.handlers(), poll_interval=0.1)
    await worker.start()
    try:
        deadline = time.monotonic() + 20
        while (await env.row(bid))["status"] != "done":
            assert time.monotonic() < deadline, "broadcast did not finish"
            await asyncio.sleep(0.05)
    finally:
        await worker.stop(5)
    every = Counter(env.copies())
    assert len(every) == 81 and max(every.values()) == 1
    states = await db.raw("select status, attempts from jobs where kind = $1", JOB_RUN)
    assert [s["status"] for s in states] == ["done"]


async def test_bad_payload_is_permanent(env: Env) -> None:
    for payload in ({}, {"broadcast_id": "1"}, {"broadcast_id": True}, {"broadcast_id": 0}):
        with pytest.raises(PermanentJobError):
            await env.sender.run_job(type("J", (), {"payload": payload})(), None)  # type: ignore[arg-type]


async def test_transitions_are_guarded(env: Env) -> None:
    bid = await env.draft()
    await env.service.set_segment(bid, {"preset": "trial"})
    with pytest.raises(BroadcastError, match="Нет получателей"):
        await env.service.start(bid, env.actor, ADMIN_TG)
    await env.service.set_segment(bid, {"preset": "all"})
    await env.service.start(bid, env.actor, ADMIN_TG)
    with pytest.raises(BroadcastError, match="уже запущена"):
        await env.service.start(bid, env.actor, ADMIN_TG)
    with pytest.raises(BroadcastError):
        await env.service.resume(bid, env.actor)
    with pytest.raises(BroadcastError):
        await env.service.set_segment(bid, {"preset": "active"})
    assert not await env.service.set_buttons(bid, [])
    await env.service.stop(bid, env.actor)
    with pytest.raises(BroadcastError):
        await env.service.pause(bid, env.actor)
    row = await env.row(bid)
    assert row["status"] == "canceled" and row["finished_at"] is not None
    edits = [c for c in env.bot.of(EditMessageText) if c.chat_id == ADMIN_TG]
    assert edits and "⏹ остановлена" in edits[-1].text  # no live run: the service refreshed it itself


def test_text_broadcast_goes_as_send_message_while_the_banner_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import svbg.tg.banner as banner_mod
    from svbg.broadcasts.sender import _with_banner

    text = {"type": "text", "text": "Привет", "entities": []}
    assert not _with_banner(text)  # no banner installed: a copy, as before
    monkeypatch.setattr(banner_mod, "is_banner_on", lambda: True)
    assert _with_banner(text)
    assert not _with_banner(
        {**text, "entities": [{"type": "custom_emoji", "offset": 0, "length": 1, "custom_emoji_id": "1"}]}
    )
    assert not _with_banner({**text, "preview": {"url": "https://example.org/"}})
    assert _with_banner({**text, "preview": {"is_disabled": True}})
    assert not _with_banner({"type": "photo", "file_id": "F", "text": "x"})
