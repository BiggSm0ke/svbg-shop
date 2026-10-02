"""«Применить к N текущим подписчикам» — new squads of a plan for its live subscriptions (04 §7, 02 §3.3).

One durable job ``catalog.apply_squads`` (``jobs(queue='panel', lane='background')``) walks the plan's
subscriptions by id in batches of :data:`BATCH`. For each batch, **one transaction**:

* ``desired_squads`` (and ``desired_ext_squad`` when the plan's external squad changed) and the ``squads`` of
  ``plan_snapshot`` are set to the absolute target carried by the job (so renewals keep the new squads);
* a ``panel.update`` job is enqueued through the writer for every subscription whose value actually changed
  (``ordering_key='sub:<id>'``, background lane, coalesced by the writer) — the projection and the sync then
  see the expected value and never record a false ``overrides``;
* subscriptions with a manual squads edit in the panel (``overrides.squads``) are skipped and listed in the
  report;
* the cursor and the counters are written into the job's own payload, fenced by the lease (``status =
  'running' AND locked_by = <worker>``): a worker that lost its lease rolls the whole batch back, and a
  restarted job continues from the cursor — no batch is applied twice, none is lost.

Progress is shown to the admin in one message that is edited in place (throttled); delivery problems of that
message never fail the job.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa

from svbg.jobs.queue import Job, enqueue
from svbg.jobs.tables import jobs
from svbg.jobs.worker import Handler, JobContext, PermanentJobError, RetryJob
from svbg.remnawave.writer import QUEUE as PANEL_QUEUE
from svbg.remnawave.writer import enqueue_update
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.tg.notifier import Notifier

__all__ = [
    "BATCH",
    "KIND",
    "ApplySquadsJob",
    "NotifierProgress",
    "ProgressSink",
    "enqueue_apply_squads",
]

log = logging.getLogger("svbg.catalog.squads")

KIND: Final = "catalog.apply_squads"
BATCH: Final = 100
MAX_ATTEMPTS: Final = 50
SKIPPED_KEEP: Final = 20  # ids of skipped subscriptions listed in the report
PROGRESS_EVERY_S: Final = 3.0

_T: Final = {
    "progress": "📍 Тариф «{name}»: новые сквады для текущих подписчиков\n"
    "Обработано {seen} из {total} · изменено {changed} · пропущено {skipped}",
    "done": "✅ Тариф «{name}»: сквады применены\n"
    "Обработано {seen} · изменено {changed} · без изменений {same} · пропущено {skipped}",
    "skipped": "Пропущены подписки с ручной правкой сквадов в панели: {ids}{more}",
    "more": " и ещё {n}",
    "panel_note": "Панель получает изменения в фоне, по очереди.",
}


class ProgressSink(Protocol):
    """Where the job reports its progress (an admin's private chat)."""

    async def send(self, chat_id: int, text: str) -> int | None: ...

    async def edit(self, chat_id: int, message_id: int, text: str) -> None: ...


class NotifierProgress:
    """:class:`ProgressSink` over the notifier (rate limits, retries); failures are only logged."""

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def send(self, chat_id: int, text: str) -> int | None:
        from svbg.tg.notifier import Priority

        try:
            msg = await self._notifier.send(chat_id, text, priority=Priority.LOW)
        except Exception as e:  # noqa: BLE001 - progress is best effort
            log.warning("squads job: progress message not sent: %s", type(e).__name__)
            return None
        return None if msg is None else int(msg.message_id)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        from aiogram.methods import EditMessageText

        from svbg.tg.notifier import Priority

        try:
            await self._notifier.call(
                EditMessageText(chat_id=chat_id, message_id=message_id, text=text),
                chat_id=chat_id,
                priority=Priority.LOW,
                coalesce_key=f"squads:{message_id}",
            )
        except Exception as e:  # noqa: BLE001 - progress is best effort («message is not modified» too)
            log.debug("squads job: progress edit failed: %s", type(e).__name__)


async def enqueue_apply_squads(
    conn: AsyncConnection,
    *,
    plan_id: int,
    plan_version: int,
    plan_name: str,
    squads: Sequence[str],
    ext_squad: str | None = None,
    set_ext: bool = False,
    total: int = 0,
    chat_id: int | None = None,
    caused_by: str | None = None,
) -> int | None:
    """Schedule the job in the caller's transaction (together with the plan change and its audit row).

    ``plan_version`` is the version the change produced: the dedup key makes a repeated enqueue of the same
    change a no-op. Runs of one plan execute in order (``ordering_key='catalog.plan:<id>'``).
    """
    target = list(dict.fromkeys(squads))
    if not target:
        raise ValueError("squads must not be empty")
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "name": plan_name[:64],
        "squads": target,
        "total": max(0, total),
        "cursor": 0,
        "seen": 0,
        "changed": 0,
        "same": 0,
        "skipped": 0,
        "skipped_ids": [],
    }
    if set_ext:
        payload["ext_squad"] = ext_squad
    if chat_id is not None:
        payload["chat_id"] = chat_id
    return await enqueue(
        conn,
        KIND,
        payload,
        queue=PANEL_QUEUE,
        lane="background",
        ordering_key=f"catalog.plan:{plan_id}",
        dedup_key=f"catalog.squads:{plan_id}:{plan_version}",
        max_attempts=MAX_ATTEMPTS,
        caused_by=caused_by,
    )


def _fence(job: Job, ctx: JobContext) -> sa.ColumnElement[bool]:
    return sa.and_(jobs.c.id == job.id, jobs.c.status == "running", jobs.c.locked_by == ctx.worker_id)


class ApplySquadsJob:
    """Handler of :data:`KIND`. Register :meth:`handlers` with the job worker."""

    def __init__(
        self,
        db: Database,
        *,
        progress: ProgressSink | None = None,
        batch: int = BATCH,
        progress_every: float = PROGRESS_EVERY_S,
    ) -> None:
        if not 1 <= batch <= 1000:
            raise ValueError("batch must be 1..1000")
        self._db = db
        self._progress = progress
        self._batch = batch
        self._every = progress_every

    def handlers(self) -> dict[str, Handler]:
        return {KIND: self.run}

    async def run(self, job: Job, ctx: JobContext) -> None:
        state = dict(job.payload)
        try:
            plan_id = int(state["plan_id"])
            squads = [str(s) for s in state["squads"]]
        except (KeyError, TypeError, ValueError) as e:
            raise PermanentJobError(f"bad payload: {type(e).__name__}") from None
        if not squads:
            raise PermanentJobError("bad payload: empty squads")
        fields = ["squads", "ext_squad"] if "ext_squad" in state else ["squads"]
        await self._open_progress(job, ctx, state)
        last_report = time.monotonic()
        while True:
            more = await self._batch_once(job, ctx, state, plan_id=plan_id, squads=squads, fields=fields)
            if not more:
                break
            if time.monotonic() - last_report >= self._every:
                await self._report(state, done=False)
                last_report = time.monotonic()
        await self._report(state, done=True)
        log.info(
            "plan %s squads applied: seen %s, changed %s, skipped %s",
            plan_id,
            state["seen"],
            state["changed"],
            state["skipped"],
        )

    async def _open_progress(self, job: Job, ctx: JobContext, state: dict[str, Any]) -> None:
        chat_id = state.get("chat_id")
        if self._progress is None or not isinstance(chat_id, int) or state.get("msg_id"):
            return
        msg_id = await self._progress.send(chat_id, self._text(state, done=False))
        if msg_id is None:
            return
        state["msg_id"] = msg_id
        async with self._db.tx() as conn:
            await self._save(conn, job, ctx, {"msg_id": msg_id})

    async def _save(
        self, conn: AsyncConnection, job: Job, ctx: JobContext, values: Mapping[str, Any]
    ) -> None:
        patch = sa.cast(sa.literal(dict(values), jobs.c.payload.type), jobs.c.payload.type)
        found = await conn.scalar(
            sa.update(jobs)
            .where(_fence(job, ctx))
            .values(payload=jobs.c.payload.op("||")(patch), updated_at=sa.func.now())
            .returning(jobs.c.id)
        )
        if found is None:
            raise RetryJob(5, "аренда задачи потеряна — партия откатывается и будет повторена")

    async def _batch_once(
        self,
        job: Job,
        ctx: JobContext,
        state: dict[str, Any],
        *,
        plan_id: int,
        squads: list[str],
        fields: list[str],
    ) -> bool:
        set_ext = "ext_squad" in state
        ext = state.get("ext_squad")
        manual = subscriptions.c.overrides.has_key("squads")
        async with self._db.tx() as conn:
            rows = (
                await conn.execute(
                    sa.select(subscriptions.c.id, manual.label("manual"))
                    .where(
                        subscriptions.c.plan_id == plan_id,
                        subscriptions.c.id > int(state["cursor"]),
                        subscriptions.c.link_state != "closed",
                    )
                    .order_by(subscriptions.c.id)
                    .limit(self._batch)
                    .with_for_update(of=subscriptions)
                )
            ).all()
            if not rows:
                return False
            skipped = [int(r.id) for r in rows if r.manual]
            todo = [int(r.id) for r in rows if not r.manual]
            changed: list[int] = []
            if todo:
                target = sa.cast(
                    sa.literal(squads, subscriptions.c.desired_squads.type),
                    subscriptions.c.desired_squads.type,
                )
                differs = subscriptions.c.desired_squads.is_distinct_from(target)
                snap: dict[str, Any] = {"squads": squads}
                values: dict[str, Any] = {"desired_squads": target}
                if set_ext:
                    values["desired_ext_squad"] = ext
                    snap["ext_squad"] = ext
                    differs = sa.or_(differs, subscriptions.c.desired_ext_squad.is_distinct_from(ext))
                patch = sa.cast(
                    sa.literal(snap, subscriptions.c.plan_snapshot.type), subscriptions.c.plan_snapshot.type
                )
                changed = [
                    int(i)
                    for i in (
                        await conn.execute(
                            sa.update(subscriptions)
                            .where(subscriptions.c.id.in_(todo), differs)
                            .values(**values, updated_at=sa.func.now())
                            .returning(subscriptions.c.id)
                        )
                    ).scalars()
                ]
                # the squads of the frozen plan description follow (renewals use it) — also for unchanged rows
                await conn.execute(
                    sa.update(subscriptions)
                    .where(subscriptions.c.id.in_(todo))
                    .values(plan_snapshot=subscriptions.c.plan_snapshot.op("||")(patch))
                )
                for sid in sorted(changed):
                    await enqueue_update(conn, sid, fields, lane="background", caused_by=f"job:{job.id}")
            kept = list(state.get("skipped_ids") or [])
            kept = (kept + skipped)[:SKIPPED_KEEP]
            state.update(
                cursor=int(rows[-1].id),
                seen=int(state["seen"]) + len(rows),
                changed=int(state["changed"]) + len(changed),
                same=int(state["same"]) + len(todo) - len(changed),
                skipped=int(state["skipped"]) + len(skipped),
                skipped_ids=kept,
            )
            await self._save(
                conn,
                job,
                ctx,
                {k: state[k] for k in ("cursor", "seen", "changed", "same", "skipped", "skipped_ids")},
            )
        return len(rows) == self._batch

    def _text(self, state: Mapping[str, Any], *, done: bool) -> str:
        args = {
            "name": state.get("name") or f"#{state.get('plan_id')}",
            "seen": state.get("seen", 0),
            "total": max(int(state.get("total") or 0), int(state.get("seen") or 0)),
            "changed": state.get("changed", 0),
            "same": state.get("same", 0),
            "skipped": state.get("skipped", 0),
        }
        if not done:
            return _T["progress"].format(**args)
        lines = [_T["done"].format(**args)]
        ids = list(state.get("skipped_ids") or [])
        if ids:
            extra = int(state.get("skipped") or 0) - len(ids)
            more = _T["more"].format(n=extra) if extra > 0 else ""
            lines.append(_T["skipped"].format(ids=", ".join(f"№{i}" for i in ids), more=more))
        if int(state.get("changed") or 0):
            lines.append(_T["panel_note"])
        return "\n\n".join(lines)

    async def _report(self, state: Mapping[str, Any], *, done: bool) -> None:
        chat_id, msg_id = state.get("chat_id"), state.get("msg_id")
        if self._progress is None or not isinstance(chat_id, int):
            return
        text = self._text(state, done=done)
        if isinstance(msg_id, int):
            await self._progress.edit(chat_id, msg_id, text)
        elif done:
            await self._progress.send(chat_id, text)
