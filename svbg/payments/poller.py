"""Reconciler and presence polling (04 D14): payments without a public webhook URL still settle in seconds,
and the provider is never hammered.

Two schedules per pending payment that the provider can report on (``Capabilities.fetch_status``):

* **with a domain** (webhooks reach us): no frequent phase, ≤ 3 safety checks at 10 min, 30 min and 2 h after
  the invoice (the webhook is the main path, the checks only catch a lost one);
* **without a domain**: a *frequent phase* — a check every 10 s while the user is on the payment screen (after
  «Оплатить», until another button is pressed), at most 3 minutes; returning to the screen or «Я оплатил»
  restarts it (at most :data:`MAX_PHASES` phases), opening another invoice ends the phases of the user's
  other invoices. Then a *decay*: 30 s → 1 → 3 → 10 → 30 min → 2 h, stop. An abandoned checkout therefore
  costs ≤ 18 + 6 = **24 requests** for its whole life.

The last scheduled check of either plan never comes before the invoice's own expiry (``expires_at`` + 2 min,
at most 7 days after the invoice): a CryptoBot invoice living 24 h is still looked at once after it could
have been paid. A check the provider failed to answer (error, timeout) does not use up a step of the
schedule: it is retried in 5 minutes (every request still counts towards :data:`HARD_CAP`). Without a domain a
payment the poller saw expire is re-read once more 6 h later by the core (a late ``expired → paid`` has no
webhook to bring it), and «Я оплатил» re-reads an expired / canceled invoice within a day of its expiry.

Global limits: ≤ 2 requests per second per instance (token bucket), ≤ 4 provider calls at once per process,
and a batch provider (``batch_status``, e.g. CryptoBot ``getInvoices``) gets one request per tick for all its
due invoices. Every instance is polled by its own *lane* (a background task, at most one per instance): a slow
or hanging provider delays only its own checks, never the tick or the other providers; a reconciler backlog
is taken in portions (≈ 5 s of the instance's rate per scan), so frequent checks never wait behind it.

The decay schedule lives in the database (``next_check_at``, ``check_step``, ``checks_used``), the frequent
phase in memory: a restart only loses the frequent phase, never a payment.

The poller only *reads* statuses; every state change goes through :meth:`PaymentCore.apply_statuses`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.core import clock as core_clock
from svbg.db.meta import UtcDateTime
from svbg.payments.core import PaymentRecord
from svbg.payments.tables import LATE_PAYABLE, payments
from svbg.sdk.payments import ProviderError

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.payments.core import PaymentCore
    from svbg.payments.registry import LiveInstance

__all__ = [
    "DOMAIN_NEXT_S",
    "ERROR_RETRY_S",
    "FREQUENT_EVERY_S",
    "FREQUENT_FOR_S",
    "HARD_CAP",
    "I_PAID_FETCH_TIMEOUT_S",
    "MAX_PHASES",
    "NODOMAIN_NEXT_S",
    "Poller",
    "TokenBucket",
]

log = logging.getLogger("svbg.payments.poller")

FREQUENT_EVERY_S: Final = 10.0
FREQUENT_FOR_S: Final = 180.0
FREQUENT_MAX: Final = int(FREQUENT_FOR_S // FREQUENT_EVERY_S)  # 18
MAX_PHASES: Final = 3
#: Interval after each database-scheduled check (the first one is set by the core at invoice creation).
DOMAIN_NEXT_S: Final = (1200, 5400)  # 10 min → 30 min → 2 h, stop: 3 checks
NODOMAIN_NEXT_S: Final = (60, 180, 600, 1800, 7200)  # +30 s → 1 → 3 → 10 → 30 min → 2 h, stop: 6 checks
#: The last scheduled check comes after the invoice's expiry plus this, but at most this long after creation.
EXPIRY_GRACE_S: Final = 120
LAST_CHECK_MAX_S: Final = 7 * 86_400
#: A scheduled check the provider failed to answer is retried after this (the step is not used up).
ERROR_RETRY_S: Final = 300
#: Absolute ceiling of status requests per payment (frequent phases + decay + retries + «Я оплатил»).
HARD_CAP: Final = 60
I_PAID_COOLDOWN_S: Final = 10.0
#: «Я оплатил» waits for the provider at most this long (the button's own timeout is 8 s).
I_PAID_FETCH_TIMEOUT_S: Final = 5.0
#: «Я оплатил» on an expired / canceled invoice re-reads it until this long after its expiry.
I_PAID_LATE_S: Final = 86_400
DB_SCAN_EVERY_S: Final = 15.0
DUE_LIMIT: Final = 500
#: Reconciler portion per instance and scan: this many seconds of the instance's request rate.
DB_LANE_S: Final = 5.0
#: How long a tick waits for its lanes before returning (they go on in the background).
TICK_WAIT_S: Final = 1.5
RPS_PER_INSTANCE: Final = 2.0
MAX_CONCURRENCY: Final = 4

_NEXT: Final = {"domain": DOMAIN_NEXT_S, "nodomain": NODOMAIN_NEXT_S}
_LATE: Final = frozenset(LATE_PAYABLE) - {"pending"}


class TokenBucket:
    """Reservation token bucket: ``reserve()`` returns how long to wait before the request may go."""

    def __init__(self, rate: float, burst: float | None = None, *, clock: Callable[[], float]) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = max(1.0, burst if burst is not None else rate)
        self._tokens = self.burst
        self._clock = clock
        self._stamp = clock()

    def reserve(self) -> float:
        t = self._clock()
        self._tokens = min(self.burst, self._tokens + (t - self._stamp) * self.rate)
        self._stamp = t
        self._tokens -= 1.0
        return 0.0 if self._tokens >= 0 else -self._tokens / self.rate


@dataclass(slots=True)
class _Presence:
    user_id: int
    instance_id: int
    until: float
    next_due: float
    count: int = 0
    external_id: str | None = None


@dataclass(slots=True)
class _Item:
    payment_id: str
    external_id: str | None
    plan: str | None = None
    step: int | None = None  # set for database-scheduled checks
    created_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(slots=True)
class TickStats:
    requests: int = 0
    checked: int = 0
    errors: int = 0
    by_instance: dict[int, int] = field(default_factory=dict)


class Poller:
    """See module docstring. ``run_once`` is the scheduler task (every ~2 s)."""

    def __init__(
        self,
        core: PaymentCore,
        db: Database,
        *,
        rps: float = RPS_PER_INSTANCE,
        max_concurrency: int = MAX_CONCURRENCY,
        hard_cap: int = HARD_CAP,
        db_scan_every: float = DB_SCAN_EVERY_S,
        tick_wait: float = TICK_WAIT_S,
        clock: Callable[[], datetime] = core_clock.now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._core = core
        self._db = db
        self._rps = rps
        self._sem = asyncio.Semaphore(max_concurrency)
        self._hard_cap = hard_cap
        self._db_scan_every = db_scan_every
        self._tick_wait = tick_wait
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._buckets: dict[int, TokenBucket] = {}
        self._presence: dict[str, _Presence] = {}
        self._by_user: dict[int, set[str]] = defaultdict(set)
        self._phases: dict[str, int] = {}
        self._i_paid_at: dict[str, float] = {}
        self._lanes: dict[int, asyncio.Task[None]] = {}
        self._last_scan: float | None = None
        self.requests_total = 0
        self.last_error: str | None = None
        core.presence = self

    # ------------------------------------------------------------------------------- presence (D14)

    def touch(self, payment_id: str, user_id: int, instance_id: int, external_id: str | None = None) -> None:
        """The user is looking at the payment screen of ``payment_id``: (re)start the frequent phase. The
        user's other invoices lose theirs (one payment screen at a time: «Другой способ» opened a new one)."""
        now = self._monotonic()
        cur = self._presence.get(payment_id)
        if cur is not None and cur.until > now and cur.count < FREQUENT_MAX:
            return  # a running phase keeps its budget
        for other in self._by_user.get(user_id, set()) - {payment_id}:
            self._presence.pop(other, None)
        self._by_user[user_id] &= {payment_id}
        phases = self._phases.get(payment_id, 0)
        if phases >= MAX_PHASES:
            return
        self._phases[payment_id] = phases + 1
        self._presence[payment_id] = _Presence(
            user_id=user_id,
            instance_id=instance_id,
            until=now + FREQUENT_FOR_S,
            next_due=now + FREQUENT_EVERY_S,
            external_id=external_id or (cur.external_id if cur else None),
        )
        self._by_user[user_id].add(payment_id)

    def leave(self, user_id: int) -> None:
        """The user pressed something else: end the frequent phases of their payments (decay continues)."""
        for payment_id in self._by_user.pop(user_id, set()):
            self._presence.pop(payment_id, None)

    def forget(self, payment_id: str) -> None:
        """The payment settled: drop all in-memory state of it."""
        cur = self._presence.pop(payment_id, None)
        if cur is not None:
            self._by_user.get(cur.user_id, set()).discard(payment_id)
        self._phases.pop(payment_id, None)
        self._i_paid_at.pop(payment_id, None)

    def present(self, payment_id: str) -> bool:
        cur = self._presence.get(payment_id)
        return cur is not None and cur.until > self._monotonic() and cur.count < FREQUENT_MAX

    # ------------------------------------------------------------------------------------- ticking

    async def run_once(self) -> None:
        """Scheduler task."""
        await self.tick()

    async def tick(self) -> TickStats:
        """One pass: due frequent checks (memory) + due reconciler checks (database, every ~15 s), one lane
        per instance. Waits for the lanes at most ``tick_wait`` seconds; a lane still running (a slow
        provider) keeps going in the background and its instance is skipped until it ends."""
        stats = TickStats()
        busy = {iid for iid, task in self._lanes.items() if not task.done()}
        groups: dict[int, list[_Item]] = defaultdict(list)
        active = await self._due_presence(groups, busy)
        mono = self._monotonic()
        if self._last_scan is None or mono - self._last_scan >= self._db_scan_every:
            self._last_scan = mono
            await self._due_db(groups, exclude=active, busy=busy)
        started: list[asyncio.Task[None]] = []
        for iid, items in groups.items():
            inst = self._core.instances.get(iid)
            if inst is None or not inst.caps.fetch_status or not items:
                continue
            task = asyncio.get_running_loop().create_task(self._lane(inst, items, stats))
            self._lanes[iid] = task
            task.add_done_callback(self._lane_done)
            started.append(task)
        if started:
            await asyncio.wait(started, timeout=self._tick_wait)
        return stats

    def _lane_done(self, task: asyncio.Task[None]) -> None:
        for iid, lane in list(self._lanes.items()):
            if lane is task:
                del self._lanes[iid]
        if not task.cancelled() and task.exception() is not None:
            log.error("payments: poll lane crashed", exc_info=task.exception())

    async def close(self) -> None:
        """Stop the running lanes (shutdown)."""
        lanes = list(self._lanes.values())
        for task in lanes:
            task.cancel()
        if lanes:
            await asyncio.gather(*lanes, return_exceptions=True)

    async def _due_presence(self, groups: dict[int, list[_Item]], busy: set[int]) -> set[str]:
        """Queue due frequent checks; returns every payment in an active phase (the database waits). A check
        of an instance whose lane is still running waits for the next tick (its budget is not spent)."""
        now = self._monotonic()
        active: set[str] = set()
        due: list[tuple[str, _Presence]] = []
        for payment_id, cur in list(self._presence.items()):
            if cur.until <= now or cur.count >= FREQUENT_MAX:
                self._presence.pop(payment_id, None)
                self._by_user.get(cur.user_id, set()).discard(payment_id)
                continue
            active.add(payment_id)
            if cur.next_due <= now and cur.instance_id not in busy:
                due.append((payment_id, cur))
        missing = [pid for pid, cur in due if cur.external_id is None]
        if missing:
            async with self._db.read() as conn:
                rows = await conn.execute(
                    sa.select(payments.c.id, payments.c.external_id).where(payments.c.id.in_(missing))
                )
                for pid, ext in rows:
                    if pid in self._presence:
                        self._presence[pid].external_id = ext
        for payment_id, cur in due:
            if cur.external_id is None:
                continue  # the invoice is still being created
            cur.count += 1
            cur.next_due = now + FREQUENT_EVERY_S
            groups[cur.instance_id].append(_Item(payment_id, cur.external_id))
        return active

    def _db_cap(self, inst: LiveInstance, queued: int) -> int:
        """Reconciler items an instance takes per scan: one batch request for a batch provider (minus the
        frequent checks already queued for it), else ≈ ``DB_LANE_S`` seconds of its request rate."""
        if inst.caps.batch_status:
            return max(0, inst.caps.batch_limit - queued)
        return max(1, int(self._rps * DB_LANE_S))

    async def _due_db(self, groups: dict[int, list[_Item]], *, exclude: set[str], busy: set[int]) -> None:
        caps = {
            inst.id: self._db_cap(inst, len(groups.get(inst.id, ())))
            for inst in self._core.instances.all()
            if inst.caps.fetch_status and inst.id not in busy
        }
        caps = {iid: cap for iid, cap in caps.items() if cap > 0}
        if not caps:
            return
        now = self._clock()
        rank = (
            sa.func.row_number()
            .over(partition_by=payments.c.instance_id, order_by=payments.c.next_check_at)
            .label("rn")
        )
        due = (
            sa.select(
                payments.c.id,
                payments.c.instance_id,
                payments.c.external_id,
                payments.c.poll_plan,
                payments.c.check_step,
                payments.c.created_at,
                payments.c.expires_at,
                payments.c.next_check_at,
                rank,
            )
            .where(
                payments.c.status == "pending",
                payments.c.next_check_at.is_not(None),
                payments.c.next_check_at <= now,
                payments.c.poll_plan.is_not(None),
                payments.c.checks_used < self._hard_cap,
                payments.c.instance_id.in_(sorted(caps)),
            )
            .subquery()
        )
        async with self._db.read() as conn:
            rows = (
                await conn.execute(
                    sa.select(
                        due.c.id,
                        due.c.instance_id,
                        due.c.external_id,
                        due.c.poll_plan,
                        due.c.check_step,
                        due.c.created_at,
                        due.c.expires_at,
                    )
                    .where(due.c.rn <= max(caps.values()) + len(exclude))
                    .order_by(due.c.next_check_at)
                    .limit(DUE_LIMIT)
                )
            ).all()
        taken: dict[int, int] = defaultdict(int)
        for pid, iid, ext, plan, step, created, expires in rows:
            if pid in exclude or taken[iid] >= caps[iid]:
                continue
            taken[iid] += 1
            groups[iid].append(_Item(pid, ext, plan, step, created, expires))

    def _bucket(self, instance_id: int) -> TokenBucket:
        bucket = self._buckets.get(instance_id)
        if bucket is None:
            bucket = self._buckets[instance_id] = TokenBucket(self._rps, clock=self._monotonic)
        return bucket

    async def _lane(self, inst: LiveInstance, items: list[_Item], stats: TickStats) -> None:
        """Poll one instance (frequent checks first, then the reconciler portion), then book the schedule:
        only checks the provider actually answered advance it."""
        ready = [i for i in items if i.external_id]
        size = inst.caps.batch_limit if inst.caps.batch_status else 1
        answered: set[str] = set()
        for k in range(0, len(ready), size):
            chunk = ready[k : k + size]
            if await self._request(inst, [i.external_id for i in chunk if i.external_id], stats):
                answered.update(i.payment_id for i in chunk)
        await self._bookkeeping(items, answered)

    async def _request(
        self, inst: LiveInstance, ids: Sequence[str], stats: TickStats, *, quick: bool = False
    ) -> bool:
        """One status request; ``True`` when the provider answered and the statuses were applied. ``quick``
        («Я оплатил»): no queueing behind background checks and a short timeout (the user is waiting)."""
        if quick:
            self._bucket(inst.id).reserve()  # accounted, never waited for: one press per 10 s per payment
            return await self._fetch(inst, ids, stats, min(self._core.fetch_timeout, I_PAID_FETCH_TIMEOUT_S))
        async with self._sem:
            wait = self._bucket(inst.id).reserve()
            if wait > 0:
                await self._sleep(wait)
            return await self._fetch(inst, ids, stats, self._core.fetch_timeout)

    async def _fetch(self, inst: LiveInstance, ids: Sequence[str], stats: TickStats, timeout: float) -> bool:  # noqa: ASYNC109
        stats.requests += 1
        stats.by_instance[inst.id] = stats.by_instance.get(inst.id, 0) + 1
        self.requests_total += 1
        try:
            async with asyncio.timeout(timeout):
                statuses = await inst.provider.fetch_status(list(ids))
        except (ProviderError, TimeoutError) as exc:
            stats.errors += 1
            self.last_error = getattr(exc, "human", "timeout")
            log.warning("payments: status check of %s failed: %s", inst.slug, self.last_error)
            return False
        except Exception:
            stats.errors += 1
            log.exception("payments: %s fetch_status crashed", inst.slug)
            return False
        stats.checked += len(ids)
        try:
            await self._core.apply_statuses(inst, statuses, source="poll")
        except Exception:
            stats.errors += 1
            log.exception("payments: statuses of %s could not be applied", inst.slug)
            return False
        return True

    @staticmethod
    def _next_at(item: _Item, now: datetime) -> datetime | None:
        intervals = _NEXT.get(item.plan or "", ())
        step = item.step or 0
        if step >= len(intervals):
            return None
        nxt = now + timedelta(seconds=intervals[step])
        if step == len(intervals) - 1 and item.expires_at is not None:
            start = item.created_at or now
            after_expiry = min(
                item.expires_at + timedelta(seconds=EXPIRY_GRACE_S),
                start + timedelta(seconds=LAST_CHECK_MAX_S),
            )
            nxt = max(nxt, after_expiry)
        return nxt

    async def _bookkeeping(self, items: list[_Item], answered: set[str]) -> None:
        """Advance the decay schedule of answered checks, retry the failed ones in :data:`ERROR_RETRY_S`
        without using up a step; count the requests (only of still pending payments)."""
        if not items:
            return
        now = self._clock()
        scheduled = [i for i in items if i.step is not None]
        frequent = [i.payment_id for i in items if i.step is None]
        async with self._db.tx() as conn:
            if frequent:
                await conn.execute(
                    sa.update(payments)
                    .where(payments.c.id.in_(frequent), payments.c.status == "pending")
                    .values(checks_used=payments.c.checks_used + 1, last_checked_at=now)
                )
            if scheduled:
                params: list[dict[str, Any]] = []
                for item in scheduled:
                    step = item.step or 0
                    if item.external_id is None or item.payment_id in answered:
                        nxt, new_step = self._next_at(item, now), step + 1
                    else:  # the provider did not answer: same step again, a little later
                        nxt, new_step = now + timedelta(seconds=ERROR_RETRY_S), step
                    params.append(
                        {
                            "b_id": item.payment_id,
                            "b_step": new_step,
                            "b_next": nxt,
                            "b_used": 1 if item.external_id else 0,
                        }
                    )
                await conn.execute(
                    sa.update(payments)
                    .where(payments.c.id == sa.bindparam("b_id"), payments.c.status == "pending")
                    .values(
                        checks_used=payments.c.checks_used + sa.bindparam("b_used", type_=sa.Integer),
                        last_checked_at=now,
                        check_step=sa.bindparam("b_step"),
                        next_check_at=sa.bindparam("b_next", type_=UtcDateTime),
                    ),
                    params,
                )

    # ------------------------------------------------------------------------------- «Я оплатил»

    async def check_now(self, payment_id: str, user_id: int) -> PaymentRecord | None:
        """«Я оплатил»: an immediate status check (cooldown 10 s, budget-capped, ≤ 5 s for the provider) and
        a new frequent phase without a domain. An expired / canceled invoice is re-read too, up to a day
        after its expiry (late payment wins). Returns the payment as it is now (``None`` if it is not this
        user's)."""
        async with self._db.read() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(payments).where(payments.c.id == payment_id, payments.c.user_id == user_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        record = PaymentRecord.from_row(row)
        inst = self._core.instances.get(record.instance_id)
        mono = self._monotonic()
        last = self._i_paid_at.get(payment_id)
        if (
            not self._checkable(record, row["expires_at"])
            or row["poll_plan"] is None
            or inst is None
            or not inst.caps.fetch_status
            or not record.external_id
            or row["checks_used"] >= self._hard_cap
            or (last is not None and mono - last < I_PAID_COOLDOWN_S)
        ):
            return record
        self._i_paid_at[payment_id] = mono
        await self._request(inst, [record.external_id], TickStats(), quick=True)
        async with self._db.tx() as conn:
            fresh = (
                (
                    await conn.execute(
                        sa.update(payments)
                        .where(payments.c.id == payment_id)
                        .values(checks_used=payments.c.checks_used + 1, last_checked_at=self._clock())
                        .returning(*payments.c)
                    )
                )
                .mappings()
                .first()
            )
        record = PaymentRecord.from_row(fresh) if fresh is not None else record
        if record.status == "pending" and row["poll_plan"] == "nodomain":
            self.touch(payment_id, user_id, record.instance_id, record.external_id)
        return record

    def _checkable(self, record: PaymentRecord, expires_at: datetime | None) -> bool:
        if record.status == "pending":
            return True
        if record.status not in _LATE:
            return False
        return self._clock() <= (expires_at or record.created_at) + timedelta(seconds=I_PAID_LATE_S)
