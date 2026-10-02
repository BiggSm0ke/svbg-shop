"""«Применить к N подписчикам»: batches through the writer, cursor + lease fence, report, invariants."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from svbg.catalog.squads_job import KIND, ApplySquadsJob, NotifierProgress, enqueue_apply_squads
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError, RetryJob
from tests.catalog.kit import SQ_DE, SQ_FI, SQ_NL, add_plan, add_sub
from tests.dbkit import CountingDatabase
from tests.subscriptions.kit import sync_env

TARGET = [SQ_NL, SQ_DE]


@dataclass
class Sink:
    sent: list[tuple[int, str]] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    msg_id: int | None = 555

    async def send(self, chat_id: int, text: str) -> int | None:
        self.sent.append((chat_id, text))
        return self.msg_id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    @property
    def last(self) -> str:
        return (self.edits[-1][2] if self.edits else self.sent[-1][1]) if (self.edits or self.sent) else ""


async def enqueue(db: CountingDatabase, plan_id: int, *, version: int = 2, **kw: Any) -> int | None:
    async with db.tx() as conn:
        return await enqueue_apply_squads(
            conn,
            plan_id=plan_id,
            plan_version=version,
            plan_name="Стандарт",
            squads=kw.pop("squads", TARGET),
            chat_id=kw.pop("chat_id", 777),
            **kw,
        )


async def claim(db: CountingDatabase, worker: str) -> tuple[Job, JobContext]:
    queue = JobQueue(db)
    claimed = [j for j in await queue.claim("background", worker, 50) if j.kind == KIND]
    assert len(claimed) == 1, claimed
    return claimed[0], JobContext(db=db, queue=queue, worker_id=worker)


async def run_to_end(db: CountingDatabase, handler: ApplySquadsJob, worker: str = "w1") -> Job:
    job, ctx = await claim(db, worker)
    await handler.run(job, ctx)
    assert await ctx.queue.complete(job.id, worker_id=worker, attempt=job.attempts)
    return job


async def sub_row(db: CountingDatabase, sid: int) -> dict[str, Any]:
    return dict((await db.raw("select * from subscriptions where id = $1", sid))[0])


async def update_jobs(db: CountingDatabase, sid: int) -> int:
    rows = await db.raw(
        "select count(*) as n from jobs where kind = 'panel.update' and (payload->>'sub_id')::bigint = $1",
        sid,
    )
    return int(rows[0]["n"])


# ------------------------------------------------------------------------------------------- end to end


async def test_apply_through_writer_to_the_panel(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        de = env.panel.add_internal_squad("DE")
        target = [env.squad, de]
        pid = await add_plan(env.db, "std", squads=(env.squad,))
        other = await add_plan(env.db, "other", squads=(env.squad,))
        subs = [await env.linked_sub(100 + i) for i in range(3)]
        manual = await env.linked_sub(200)
        closed = await env.linked_sub(201)
        foreign = await env.linked_sub(300)
        await env.db.raw(
            "update subscriptions set plan_id = $1 where id = any($2::bigint[])", pid, [*subs, manual, closed]
        )
        await env.db.raw("update subscriptions set plan_id = $1 where id = $2", other, foreign)
        await env.db.raw(
            """update subscriptions set overrides = '{"squads": true}'::jsonb where id = $1""", manual
        )
        await env.db.raw("update subscriptions set link_state = 'closed' where id = $1", closed)
        assert await enqueue(env.db, pid, squads=target, total=4) is not None
        sink = Sink()
        await run_to_end(env.db, ApplySquadsJob(env.db, progress=sink, batch=2))
        outcomes = await env.drain()
        assert outcomes and all(o == "done" for _, o in outcomes), outcomes
        for sid in subs:
            row = await env.sub(sid)
            assert row["desired_squads"] == target and row["plan_snapshot"]["squads"] == target
            user = env.panel_user(row["panel_user_id"])
            assert user["activeInternalSquads"] == target
        for sid in (manual, closed, foreign):
            row = await env.sub(sid)
            assert row["desired_squads"] == [env.squad]
            assert env.panel_user(row["panel_user_id"])["activeInternalSquads"] == [env.squad]
        assert sink.sent == [(777, sink.sent[0][1])] and "Обработано 0 из 4" in sink.sent[0][1]
        final = sink.last
        assert "сквады применены" in final and f"№{manual}" in final and "изменено 3" in final
        assert "пропущено 1" in final


async def test_ext_squad_is_applied_only_when_requested(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std")
    sid = await add_sub(db, pid)
    await db.raw("update subscriptions set desired_ext_squad = 'ext-old' where id = $1", sid)
    await enqueue(db, pid, squads=[SQ_NL], ext_squad=None, set_ext=True)
    await run_to_end(db, ApplySquadsJob(db))
    row = await sub_row(db, sid)
    assert row["desired_ext_squad"] is None and row["desired_squads"] == [SQ_NL]
    job = (await db.raw("select payload from jobs where kind = 'panel.update'"))[0]
    assert job["payload"]["fields"] == ["ext_squad", "squads"]


async def test_purchase_during_mass_apply_is_not_parked_behind_background(db: CountingDatabase) -> None:
    """Review finding: the per-key FIFO must not make a buyer wait for the background drain of a mass apply.

    The interactive ``panel.update`` of a purchase promotes the earlier background job of the same
    subscription, so the interactive lane serves both (in order) while the other subscribers' jobs stay in
    the background lane.
    """
    from svbg.remnawave.writer import enqueue_update

    pid = await add_plan(db, "std")
    subs = [await add_sub(db, pid) for _ in range(3)]
    await enqueue(db, pid)
    await run_to_end(db, ApplySquadsJob(db))
    assert [await update_jobs(db, s) for s in subs] == [1, 1, 1]
    buyer = subs[1]
    async with db.tx() as conn:  # lifecycle.purchase → the writer's interactive update
        new_id = await enqueue_update(conn, buyer, ["expire"], caused_by="order:1")
    assert new_id is not None
    lanes = await db.raw(
        "select (payload->>'sub_id')::bigint as sid, lane from jobs where kind = 'panel.update' order by id"
    )
    assert [(r["sid"], r["lane"]) for r in lanes] == [
        (subs[0], "background"),
        (buyer, "interactive"),  # promoted
        (subs[2], "background"),
        (buyer, "interactive"),
    ]
    queue = JobQueue(db)
    first = await queue.claim("interactive", "wi", 10)
    assert [(j.payload["sub_id"], j.id < new_id) for j in first] == [(buyer, True)]  # FIFO per key kept
    assert await queue.claim("interactive", "wi", 10) == []  # the purchase waits for its predecessor only
    assert await queue.complete(first[0].id, worker_id="wi", attempt=first[0].attempts)
    second = await queue.claim("interactive", "wi", 10)
    assert [j.id for j in second] == [new_id]
    background = await queue.claim("background", "wb", 10)
    assert sorted(j.payload["sub_id"] for j in background) == [subs[0], subs[2]]


# ------------------------------------------------------------------------------------------- robustness


class Stealing(ApplySquadsJob):
    """Loses its lease right before batch ``steal_at`` (another worker took the job)."""

    def __init__(self, db: CountingDatabase, steal_at: int, **kw: Any) -> None:
        super().__init__(db, **kw)
        self.db = db
        self.steal_at = steal_at
        self.calls = 0

    async def _batch_once(self, job: Job, ctx: JobContext, state: dict[str, Any], **kw: Any) -> bool:
        self.calls += 1
        if self.calls == self.steal_at:
            await self.db.raw("update jobs set locked_by = 'thief' where id = $1", job.id)
        return await super()._batch_once(job, ctx, state, **kw)


async def test_lost_lease_rolls_the_batch_back_and_resumes_from_the_cursor(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std")
    subs = [await add_sub(db, pid) for _ in range(5)]
    await enqueue(db, pid)
    job, ctx = await claim(db, "w1")
    with pytest.raises(RetryJob):
        await Stealing(db, steal_at=2, batch=2).run(job, ctx)
    payload = (await db.raw("select payload from jobs where id = $1", job.id))[0]["payload"]
    assert payload["cursor"] == subs[1] and payload["changed"] == 2  # batch 1 committed, batch 2 rolled back
    assert [await update_jobs(db, s) for s in subs] == [1, 1, 0, 0, 0]
    assert (await sub_row(db, subs[2]))["desired_squads"] == [SQ_NL]
    await db.raw(
        "update jobs set status = 'ready', locked_by = null, next_run_at = now() where id = $1", job.id
    )
    await run_to_end(db, ApplySquadsJob(db, batch=2), worker="w2")
    assert [await update_jobs(db, s) for s in subs] == [1] * 5
    for s in subs:
        assert (await sub_row(db, s))["desired_squads"] == TARGET


async def test_dedup_bad_payload_and_empty_plan(db: CountingDatabase) -> None:
    pid = await add_plan(db, "std")
    assert await enqueue(db, pid, version=7) is not None
    assert await enqueue(db, pid, version=7) is None  # the same change twice: one job
    sink = Sink(msg_id=None)
    await run_to_end(db, ApplySquadsJob(db, progress=sink))
    assert len(sink.sent) == 2 and "сквады применены" in sink.sent[-1][1]  # no message id: the report is sent
    with pytest.raises(ValueError, match="empty"):
        await enqueue(db, pid, squads=[])
    async with db.tx() as conn:
        from svbg.jobs.queue import enqueue as raw_enqueue

        await raw_enqueue(conn, KIND, {"plan_id": "x"}, queue="panel", lane="background")
    job, ctx = await claim(db, "w3")
    with pytest.raises(PermanentJobError):
        await ApplySquadsJob(db).run(job, ctx)


async def test_invariants_property(db: CountingDatabase) -> None:
    """Random populations, batch sizes, lease losses: every live non-manual subscription of the plan ends on
    the target exactly once; nothing else changes; counters add up."""
    rnd = random.Random(20261001)
    pool = [SQ_NL, SQ_DE, SQ_FI]
    for round_no in range(12):
        target = rnd.sample(pool, rnd.randint(1, 3))
        pid = await add_plan(db, f"p{round_no}", squads=(SQ_NL,))
        other = await add_plan(db, f"o{round_no}", squads=(SQ_NL,))
        expect: dict[int, tuple[bool, list[str]]] = {}  # sid -> (should change, initial squads)
        for _ in range(rnd.randint(0, 25)):
            squads = rnd.sample(pool, rnd.randint(1, 3))
            state = rnd.choice(["linked", "linked", "pending", "closed"])
            manual = rnd.random() < 0.2
            plan = pid if rnd.random() < 0.85 else other
            sid = await add_sub(db, plan, squads=squads, link_state=state, manual=manual)
            live = plan == pid and state != "closed" and not manual
            expect[sid] = (live and squads != target, squads)
        await enqueue(db, pid, squads=target, total=len(expect))
        batch = rnd.randint(1, 7)
        job, ctx = await claim(db, f"w{round_no}")
        steal_at = rnd.choice([None, 1, 2, 3])
        handler: ApplySquadsJob = (
            Stealing(db, steal_at, batch=batch) if steal_at else ApplySquadsJob(db, batch=batch)
        )
        try:
            await handler.run(job, ctx)
            await ctx.queue.complete(job.id, worker_id=ctx.worker_id, attempt=job.attempts)
        except RetryJob:
            await db.raw(
                "update jobs set status = 'ready', locked_by = null, next_run_at = now() where id = $1",
                job.id,
            )
            await run_to_end(db, ApplySquadsJob(db, batch=batch), worker=f"x{round_no}")
        payload = (await db.raw("select payload from jobs where id = $1", job.id))[0]["payload"]
        live = [
            s
            for s, (_, initial) in expect.items()
            if (await sub_row(db, s))["plan_id"] == pid and (await sub_row(db, s))["link_state"] != "closed"
        ]
        assert payload["seen"] == len(live)
        assert payload["changed"] == sum(1 for c, _ in expect.values() if c)
        for sid, (should_change, initial) in expect.items():
            row = await sub_row(db, sid)
            manual = bool(row["overrides"].get("squads"))
            on_plan = row["plan_id"] == pid and row["link_state"] != "closed" and not manual
            assert row["desired_squads"] == (target if on_plan else initial), (round_no, sid)
            assert await update_jobs(db, sid) == (1 if should_change else 0), (round_no, sid)
            if on_plan:
                assert row["plan_snapshot"]["squads"] == target
        assert payload["skipped"] + payload["changed"] + payload["same"] == payload["seen"]
        await db.raw("delete from jobs where kind = 'panel.update'")
        await db.raw("delete from subscriptions")


# ------------------------------------------------------------------------------------------- progress sink


@dataclass
class _Msg:
    message_id: int


@dataclass
class FakeNotifier:
    fail: bool = False
    calls: list[Any] = field(default_factory=list)

    async def send(self, chat_id: int, text: str, **kw: Any) -> _Msg | None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("send", chat_id, text))
        return _Msg(42)

    async def call(self, method: Any, **kw: Any) -> Any:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(("call", method, kw))
        return True


async def test_notifier_progress_is_best_effort() -> None:
    ok = FakeNotifier()
    sink = NotifierProgress(ok)  # type: ignore[arg-type]
    assert await sink.send(5, "hi") == 42
    await sink.edit(5, 42, "more")
    method = ok.calls[-1][1]
    assert (
        method.message_id == 42 and method.text == "more" and ok.calls[-1][2]["coalesce_key"] == "squads:42"
    )
    broken = NotifierProgress(FakeNotifier(fail=True))  # type: ignore[arg-type]
    assert await broken.send(5, "hi") is None
    await broken.edit(5, 42, "x")  # no exception
