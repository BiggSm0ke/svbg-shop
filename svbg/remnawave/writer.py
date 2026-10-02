"""The only code that changes panel state (02 §4, §6.1; 07 §2.4.3).

Every write is a durable job in ``jobs`` with ``queue='panel'`` and ``ordering_key='sub:<id>'`` (strict FIFO
per subscription), produced inside the caller's business transaction by the ``enqueue_*`` functions below and
executed by :class:`PanelWriter` handlers. Rules enforced here:

* **absolute targets**: renew computes ``target = max(panel.expireAt, now) + days`` once from a fresh ``GET``,
  stores it in the job payload (committed) and then PATCHes ``expireAt=target`` — a retry after a timeout
  repeats the same target, never extends twice (``actions/extend`` is not used);
* **coalescing**: consecutive ready ``panel.update`` jobs of one subscription are folded into one PATCH of the
  final desired state (desired values are read at execution time, they are absolute);
* **adoption** on ``A019``: a create retried after a lost response finds "its" panel user (same telegramId,
  created within 5 min of the operation) and links it instead of creating a second one;
* ``activeInternalSquads`` is never sent empty; module substitutions are applied by the core
  (:class:`~svbg.remnawave.contributors.SquadContributors`), fail-closed when the owner module is down;
* ``hwidDeviceLimit``: ``NULL`` = panel fallback (omitted on create, ``null`` in PATCH), ``0`` = no limit;
* "forever" is ``2099-12-31T00:00:00Z`` (:data:`~svbg.remnawave.models.FOREVER`);
* HTTP calls are never made inside an open DB transaction.

Failures: ``auth``/``forbidden_scope``/``validation`` go straight to ``dead`` with an owner alert; transient
ones are retried with backoff and alert the owner after :data:`ALERT_AFTER` attempts. While the panel is not
reachable at all (breaker open, not connected, writes blocked by the version gate) a job is postponed without
using up its attempt budget: the outbox guarantees delivery however long the outage lasts (02 §6.5).

Closed subscriptions: the writer touches their panel user only to disable/delete it; a create that raced
with ``close()`` still records the panel user it made, so the queued disable reaches it (no orphans).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.component import fix_screen, fix_setting
from svbg.jobs.queue import Job, enqueue
from svbg.jobs.tables import jobs
from svbg.jobs.worker import Handler, JobContext, PermanentJobError, RetryJob
from svbg.remnawave.contributors import SquadContributors, forward
from svbg.remnawave.errors import (
    USER_NOT_FOUND_CODES,
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
)
from svbg.remnawave.models import FOREVER, PanelUser
from svbg.remnawave.projection import SQUADS_PENDING, mark_missing, select_subscriptions, snapshot_values
from svbg.remnawave.transport import Lane
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.tables import EVENT_REF_PREDICATE, subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "ALERT_AFTER",
    "KINDS",
    "K_HWID_DELETE",
    "K_HWID_RESET",
    "QUEUE",
    "UPDATE_FIELDS",
    "DeviceJobs",
    "PanelWriter",
    "emergency_set_squads",
    "enqueue_action",
    "enqueue_create",
    "enqueue_renew",
    "enqueue_update",
    "next_username",
    "ordering_key",
]

log = logging.getLogger("svbg.remnawave.writer")

QUEUE: Final = "panel"
K_CREATE: Final = "panel.create"
K_UPDATE: Final = "panel.update"
K_RENEW: Final = "panel.renew"
K_ENABLE: Final = "panel.enable"
K_DISABLE: Final = "panel.disable"
K_DELETE: Final = "panel.delete"
K_REVOKE: Final = "panel.revoke"
K_RESET_TRAFFIC: Final = "panel.reset_traffic"
KINDS: Final = (K_CREATE, K_UPDATE, K_RENEW, K_ENABLE, K_DISABLE, K_DELETE, K_REVOKE, K_RESET_TRAFFIC)
#: HWID device jobs (02 §4.7), produced by :class:`svbg.subscriptions.devices.SubscriptionActions` in the
#: same ``panel`` queue and ``ordering_key`` (the producer keeps its own copy of the names).
K_HWID_DELETE: Final = "panel.hwid_delete"
K_HWID_RESET: Final = "panel.hwid_reset"
#: Fields a ``panel.update`` may push; each maps to one ``desired_*`` column.
UPDATE_FIELDS: Final = ("expire", "traffic", "strategy", "device_limit", "squads", "ext_squad", "tag")
#: overrides key of each update field (02 §6.3).
_OVERRIDE_KEY: Final = {
    "expire": "expire",
    "traffic": "traffic_bytes",
    "strategy": "reset_strategy",
    "device_limit": "device_limit",
    "squads": "squads",
    "ext_squad": "ext_squad",
    "tag": "tag",
}
#: Transient failures in a row after which the owner is told (the job keeps retrying).
ALERT_AFTER: Final = 5
MAX_ATTEMPTS: Final = 100
ADOPT_WINDOW: Final = timedelta(minutes=5)
MIN_EXPIRE_AHEAD: Final = timedelta(minutes=2)
SKEW_RETRY_AHEAD: Final = timedelta(minutes=15)
ENABLE_MIN_AHEAD: Final = timedelta(minutes=1)
MAX_USERNAME_SUFFIX: Final = 20
USERNAME_MAX: Final = 36
DESCRIPTION_MAX: Final = 200
_GONE_SQUAD_CODES: Final = frozenset({"A018", "A039", "A118", "A182"})
#: Retry delay while the panel cannot be reached at all (no attempt is used up meanwhile).
UNREACHABLE_RETRY_S: Final = 60.0
WRITE_BLOCKED_RETRY_S: Final = 300.0

_TXT = {
    "dead_title": "Изменение в панели остановлено: {what}",
    "dead_body": "Подписка №{sid}: панель отклонила «{what}». {hint} После исправления нажмите «Повторить» "
    "в «Состояние → Очередь».",
    "stuck_title": "Панель не принимает изменения",
    "stuck_body": "«{what}» для подписки №{sid} не удаётся выполнить уже {n} раз подряд: {error}. Бот "
    "продолжает повторять сам. {hint}",
    "squad_gone_title": "Сквад тарифа удалён в панели",
    "squad_gone_body": "Подписка №{sid} не создана: в панели нет сквадов {squads}. Выберите существующие "
    "сквады в тарифе и нажмите «Повторить» в «Состояние → Очередь».",
    "squad_gone_update_body": "Подписка №{sid}: изменение не применено — в панели нет сквадов {squads} "
    "(тариф ссылается на удалённый сквад). Выберите существующие сквады в тарифе и нажмите «Повторить» в "
    "«Состояние → Очередь».",
    "ext_gone_title": "Внешний сквад удалён в панели",
    "ext_gone_body": "Внешний сквад {ext} не найден в панели. Подписка №{sid} записана без него — проверьте "
    "настройки тарифа.",
}
_WHAT = {
    K_CREATE: "создание пользователя",
    K_UPDATE: "изменение параметров",
    K_RENEW: "продление",
    K_ENABLE: "включение",
    K_DISABLE: "отключение",
    K_DELETE: "удаление",
    K_REVOKE: "перевыпуск ссылки",
    K_RESET_TRAFFIC: "сброс трафика",
}


# ----------------------------------------------------------------------------------------------- producers


def ordering_key(subscription_id: int) -> str:
    return f"sub:{int(subscription_id)}"


async def _enqueue(
    conn: AsyncConnection,
    kind: str,
    subscription_id: int,
    payload: Mapping[str, Any],
    *,
    lane: str,
    caused_by: str | None,
    run_at: datetime | None = None,
) -> int | None:
    if isinstance(subscription_id, bool) or not isinstance(subscription_id, int) or subscription_id <= 0:
        raise ValueError("subscription_id must be a positive int")
    return await enqueue(
        conn,
        kind,
        {"sub_id": subscription_id, **payload},
        queue=QUEUE,
        lane=lane,
        ordering_key=ordering_key(subscription_id),
        max_attempts=MAX_ATTEMPTS,
        caused_by=caused_by,
        run_at=run_at,
    )


async def enqueue_create(
    conn: AsyncConnection, subscription_id: int, *, lane: str = "interactive", caused_by: str | None = None
) -> int | None:
    """Create the panel user of a ``pending`` subscription from its ``desired_*`` columns."""
    return await _enqueue(conn, K_CREATE, subscription_id, {}, lane=lane, caused_by=caused_by)


async def enqueue_update(
    conn: AsyncConnection,
    subscription_id: int,
    fields: Iterable[str],
    *,
    clear_overrides: Iterable[str] = (),
    lane: str = "interactive",
    caused_by: str | None = None,
) -> int | None:
    """PATCH the given ``fields`` from the current ``desired_*`` values (absolute, coalescible)."""
    wanted = sorted(set(fields))
    unknown = set(wanted) - set(UPDATE_FIELDS)
    if not wanted or unknown:
        raise ValueError(f"fields must be a non-empty subset of {UPDATE_FIELDS}")
    clear = sorted(set(clear_overrides))
    payload: dict[str, Any] = {"fields": wanted}
    if clear:
        payload["clear_overrides"] = clear
    return await _enqueue(conn, K_UPDATE, subscription_id, payload, lane=lane, caused_by=caused_by)


async def enqueue_renew(
    conn: AsyncConnection,
    subscription_id: int,
    days: int,
    *,
    lane: str = "interactive",
    caused_by: str | None = None,
) -> int | None:
    """Extend by ``days`` from ``max(panel.expireAt, now)`` with an absolute, persisted target (02 §4.3)."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 36_500:
        raise ValueError("days must be an int in 1..36500")
    return await _enqueue(conn, K_RENEW, subscription_id, {"days": days}, lane=lane, caused_by=caused_by)


async def enqueue_action(
    conn: AsyncConnection,
    subscription_id: int,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    lane: str = "interactive",
    caused_by: str | None = None,
) -> int | None:
    """``panel.enable|disable|delete|revoke|reset_traffic``."""
    if kind not in (K_ENABLE, K_DISABLE, K_DELETE, K_REVOKE, K_RESET_TRAFFIC):
        raise ValueError(f"unknown panel action {kind!r}")
    return await _enqueue(conn, kind, subscription_id, dict(payload or {}), lane=lane, caused_by=caused_by)


# ----------------------------------------------------------------------------------------------- helpers


_SUFFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")


def next_username(base: str, n: int) -> str:
    """``base_n`` within 36 characters (the base is shortened, never the suffix)."""
    suffix = f"_{n}"
    return base[: USERNAME_MAX - len(suffix)] + suffix


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def _clamp_expire(target: datetime, at: datetime) -> datetime:
    """The panel refuses a PATCH ``expireAt`` in the past (by its clock): keep ≥ now + 2 min (02 §8.1 #8)."""
    floor = at + MIN_EXPIRE_AHEAD
    return target if target > floor else floor


class _Gone(Exception):
    """The panel user does not exist any more (handled per operation)."""


class PanelWriter:
    """Executes ``queue='panel'`` jobs. Register :meth:`handlers` with the job worker."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        contributors: SquadContributors,
        attention: AttentionService | None = None,
        bus: EventBus | None = None,
        alert_after: int = ALERT_AFTER,
    ) -> None:
        self._db = db
        self._api = api
        self._contributors = contributors
        self._attention = attention
        self._bus = bus
        self._alert_after = alert_after

    def handlers(self) -> dict[str, Handler]:
        return {
            K_CREATE: self._wrap(self._create),
            K_UPDATE: self._wrap(self._update),
            K_RENEW: self._wrap(self._renew),
            K_ENABLE: self._wrap(self._enable),
            K_DISABLE: self._wrap(self._disable),
            K_DELETE: self._wrap(self._delete),
            K_REVOKE: self._wrap(self._revoke),
            K_RESET_TRAFFIC: self._wrap(self._reset_traffic),
            **DeviceJobs(self._db, self._api, attention=self._attention).handlers(),
        }

    # ------------------------------------------------------------------------------------ error policy

    def _wrap(self, fn: Callable[[Job, JobContext], Any]) -> Handler:
        async def handler(job: Job, ctx: JobContext) -> None:
            try:
                await fn(job, ctx)
            except PanelNotConfiguredError as err:
                await self._postpone(job, ctx)
                raise RetryJob(UNREACHABLE_RETRY_S, "панель не подключена") from err
            except (PanelUnavailableError, WriteBlockedError) as err:
                # Nothing reached the panel: wait for it without spending the attempt budget (02 §6.5).
                await self._postpone(job, ctx)
                if job.attempts >= self._alert_after:
                    await self._alert_stuck(job, err)
                if isinstance(err, WriteBlockedError):
                    raise RetryJob(WRITE_BLOCKED_RETRY_S, str(err)) from err
                raise RetryJob(max(err.retry_after or 0.0, 5.0), str(err)) from err
            except RemnawaveError as err:
                if err.kind in (ErrorKind.AUTH, ErrorKind.FORBIDDEN_SCOPE, ErrorKind.VALIDATION):
                    await self._alert_dead(job, err)
                    raise PermanentJobError(str(err)) from err
                if job.attempts >= self._alert_after:
                    await self._alert_stuck(job, err)
                if err.retry_after:
                    raise RetryJob(err.retry_after, str(err)) from err
                raise

        handler.__qualname__ = f"PanelWriter.{fn.__name__.lstrip('_')}"
        return handler

    async def _postpone(self, job: Job, ctx: JobContext) -> None:
        """This attempt does not count: the budget grows by one (fenced by the lease and the attempt number,
        so the worker's own ``fail`` that follows still matches)."""
        stmt = (
            sa.update(jobs)
            .where(
                jobs.c.id == job.id,
                jobs.c.status == "running",
                jobs.c.locked_by == ctx.worker_id,
                jobs.c.attempts == job.attempts,
            )
            .values(max_attempts=jobs.c.max_attempts + 1)
        )
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except Exception:  # the retry itself still happens; at worst this attempt counts
            log.exception("panel job %s: could not postpone without using an attempt", job.id)

    async def _alert_dead(self, job: Job, err: RemnawaveError) -> None:
        what = _WHAT.get(job.kind, job.kind)
        fix = fix_setting("REMNAWAVE_TOKEN") if err.kind is ErrorKind.AUTH else fix_screen("jobs", "dead")
        await self._raise(
            f"rw:op_dead:{job.payload.get('sub_id')}:{job.kind}",
            "error",
            _TXT["dead_title"].format(what=what),
            _TXT["dead_body"].format(sid=job.payload.get("sub_id"), what=what, hint=err.hint_ru),
            fix,
        )

    async def _alert_stuck(self, job: Job, err: RemnawaveError) -> None:
        what = _WHAT.get(job.kind, job.kind)
        await self._raise(
            "rw:op_stuck",
            "warn",
            _TXT["stuck_title"],
            _TXT["stuck_body"].format(
                what=what,
                sid=job.payload.get("sub_id"),
                n=job.attempts,
                error=err.message or err.kind.value,
                hint=err.hint_ru,
            ),
            fix_screen("status"),
        )

    async def _raise(self, key: str, severity: str, title: str, body: str, fix: str | None) -> None:
        if self._attention is None:
            return
        try:
            await self._attention.raise_item(key, severity, title, body, fix_action=fix)  # type: ignore[arg-type]
        except Exception:
            log.exception("could not raise attention item %s", key)

    # -------------------------------------------------------------------------------------- data access

    async def _load(self, sid: int) -> Mapping[str, Any] | None:
        async with self._db.read() as conn:
            return (
                (await conn.execute(select_subscriptions().where(subscriptions.c.id == sid)))
                .mappings()
                .first()
            )

    async def _store(
        self,
        sid: int,
        user: PanelUser | None,
        values: Mapping[str, Any] | None = None,
        *,
        event: Mapping[str, Any] | None = None,
        where_state: str | None = None,
    ) -> bool:
        """Snapshot of ``user`` + ``values`` (+ an audit event) in one transaction."""
        new_values: dict[str, Any] = {}
        if user is not None:
            new_values.update(snapshot_values(user))
            new_values["panel_state_ts"] = now()
        new_values.update(values or {})
        stmt = sa.update(subscriptions).where(subscriptions.c.id == sid)
        if where_state is not None:
            stmt = stmt.where(subscriptions.c.link_state == where_state)
        stmt = stmt.values(**new_values, updated_at=sa.func.now()).returning(subscriptions.c.id)
        async with self._db.tx() as conn:
            updated = (await conn.execute(stmt)).first() is not None
            if updated and event is not None:
                await _insert_event(conn, sid, event)
        return updated

    async def _mark_missing(self, sid: int, job: Job, row: Mapping[str, Any]) -> None:
        """The panel user is gone: ``panel_missing``; never re-created automatically (02 §6.6)."""
        await mark_missing(
            self._db,
            sid,
            source="bot",
            username=row.get("panel_username"),
            ref_type="job",
            ref_id=str(job.id),
            attention=self._attention,
            bus=self._bus,
        )

    def _publish(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            from svbg.core.bus import Event

            self._bus.publish_nowait(Event(name, dict(payload)))
        except Exception:
            log.exception("could not publish %s", name)

    @staticmethod
    def _lane(job: Job) -> Lane:
        return Lane.INTERACTIVE if job.lane == "interactive" else Lane.BACKGROUND

    async def _save_payload(self, job: Job, ctx: JobContext, extra: Mapping[str, Any]) -> None:
        """Persist computed targets into the job (committed before the panel call; fenced by the lease)."""
        stmt = (
            sa.update(jobs)
            .where(jobs.c.id == job.id, jobs.c.status == "running", jobs.c.locked_by == ctx.worker_id)
            .values(
                payload=jobs.c.payload.op("||")(
                    sa.cast(sa.literal(dict(extra), jobs.c.payload.type), jobs.c.payload.type)
                )
            )
            .returning(jobs.c.id)
        )
        async with self._db.tx() as conn:
            if (await conn.execute(stmt)).first() is None:
                raise RetryJob(1, "аренда задачи потеряна до записи цели")

    # ------------------------------------------------------------------------------------------- create

    async def _create(self, job: Job, ctx: JobContext) -> None:
        sid = int(job.payload["sub_id"])
        row = await self._load(sid)
        if row is None or row["panel_user_id"] is not None:
            return  # already linked (adopted / previous attempt committed) or gone: nothing to do
        if row["link_state"] == "closed":
            # Closed before this attempt. An earlier attempt may have created the user with its answer lost:
            # find it, so the queued disable/delete reaches it instead of leaving an orphan with access.
            if job.attempts > 1:
                await self._record_orphan(job, row)
            return
        if row["link_state"] != "pending":
            return
        desired = list(row["desired_squads"] or [])
        if not desired:
            raise PermanentJobError(
                f"подписка {sid}: нет сквадов — пустой activeInternalSquads не отправляем"
            )
        expire = row["desired_expire_at"] or row["paid_until"]
        if expire is None:
            raise PermanentJobError(f"подписка {sid}: не задан срок")
        async with self._db.read() as conn:
            subs = await self._contributors.load(conn, sid)
        # On create there is no panel state to keep: the core's tables are the truth, forward always applies.
        squads = forward(desired, subs)
        lane = self._lane(job)
        base = str(job.payload.get("username_base") or row["panel_username"] or "")
        if not base or not _SUFFIX_RE.match(base):
            raise PermanentJobError(f"подписка {sid}: неверное имя пользователя панели")
        username = str(row["panel_username"] or base)
        n = int(job.payload.get("username_n") or 1)
        ext = row["desired_ext_squad"]
        owner_tg = row["owner_telegram_id"]
        description = f"sv:{row['public_id']}"[:DESCRIPTION_MAX]
        user: PanelUser | None = None
        adopted = False
        for _ in range(MAX_USERNAME_SUFFIX):
            kwargs: dict[str, Any] = {
                "username": username,
                "expire_at": expire,
                "active_internal_squads": squads,
                "description": description,
                "lane": lane,
            }
            if owner_tg is not None:
                kwargs["telegram_id"] = owner_tg
            if row["desired_traffic_bytes"] is not None:
                kwargs["traffic_limit_bytes"] = int(row["desired_traffic_bytes"])
            if row["desired_reset_strategy"] is not None:
                kwargs["traffic_limit_strategy"] = row["desired_reset_strategy"]
            if row["desired_device_limit"] is not None:  # NULL = panel fallback: omit on create
                kwargs["hwid_device_limit"] = int(row["desired_device_limit"])
            if ext is not None:
                kwargs["external_squad_uuid"] = ext
            if row["desired_tag"] is not None:
                kwargs["tag"] = row["desired_tag"]
            api = self._api()  # per attempt: a hot swap closes the previous session
            try:
                user = await api.create_user(**kwargs)
                break
            except RemnawaveError as err:
                if err.kind is ErrorKind.CONFLICT and err.code == "A019":
                    existing = await api.get_by_username(username, lane=lane)
                    if await self._is_our_retry(existing, owner_tg, job):
                        user, adopted = existing, True
                        break
                    n += 1
                    username = next_username(base, n)
                    async with self._db.tx() as conn:
                        await conn.execute(
                            sa.update(subscriptions)
                            .where(subscriptions.c.id == sid)
                            .values(panel_username=username, updated_at=sa.func.now())
                        )
                    await self._save_payload(job, ctx, {"username_base": base, "username_n": n})
                    continue
                if err.code in _GONE_SQUAD_CODES:
                    if await self._check_squads(api, sid, squads, ext, lane, "create"):
                        ext = None  # the external squad is gone: create without it (alert raised)
                        continue
                    raise PermanentJobError(
                        f"подписка {sid}: панель отклонила сквады, но все они существуют"
                    ) from err
                raise
        if user is None:
            raise PermanentJobError(f"подписка {sid}: не удалось подобрать свободное имя пользователя панели")
        values: dict[str, Any] = {
            "link_state": "linked",
            "panel_user_id": user.id,
            "desired_expire_at": user.expire_at or expire,
        }
        if ext != row["desired_ext_squad"]:
            values["desired_ext_squad"] = ext
        linked = await self._store(
            sid,
            user,
            values,
            event={
                "kind": "panel_adopted" if adopted else "panel_created",
                "source": "bot",
                "new_expire": user.expire_at,
                "ref_type": "job",
                "ref_id": str(job.id),
            },
            where_state="pending",
        )
        if linked:
            self._publish("subscription.linked", {"subscription_id": sid, "adopted": adopted})
        else:  # closed while the POST was in flight: keep the panel user known so it gets disabled
            await self._link_closed(sid, user, job, adopted)

    async def _record_orphan(self, job: Job, row: Mapping[str, Any]) -> None:
        username = row["panel_username"]
        if not username:
            return
        try:
            existing = await self._api().get_by_username(str(username), lane=self._lane(job))
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                return  # the earlier attempt did not create anything
            raise
        if await self._is_our_retry(existing, row["owner_telegram_id"], job):
            await self._link_closed(int(row["id"]), existing, job, adopted=True)

    async def _link_closed(self, sid: int, user: PanelUser, job: Job, adopted: bool) -> None:
        """Record the panel user of a subscription closed during its create, and make sure it is disabled."""
        values = {**snapshot_values(user), "panel_user_id": user.id, "panel_state_ts": now()}
        stmt = (
            sa.update(subscriptions)
            .where(
                subscriptions.c.id == sid,
                subscriptions.c.link_state == "closed",
                subscriptions.c.panel_user_id.is_(None),
            )
            .values(**values, updated_at=sa.func.now())
            .returning(subscriptions.c.id)
        )
        async with self._db.tx() as conn:
            if (await conn.execute(stmt)).first() is None:
                return
            await _insert_event(
                conn,
                sid,
                {
                    "kind": "panel_adopted" if adopted else "panel_created",
                    "source": "bot",
                    "new_expire": user.expire_at,
                    "ref_type": "job",
                    "ref_id": str(job.id),
                },
            )
            # close() queued a disable already; one more is harmless (A029) and covers any other path.
            await enqueue_action(conn, sid, K_DISABLE, {"reason": "closed"}, lane="background")
        log.warning(
            "subscription %s was closed during its panel create: the panel user is being disabled", sid
        )

    async def _is_our_retry(self, existing: PanelUser, owner_tg: int | None, job: Job) -> bool:
        """``A019``: is this the user our own earlier attempt created (02 §4.1)?"""
        if existing.telegram_id != owner_tg or existing.created_at is None:
            return False
        if existing.created_at < job.created_at - ADOPT_WINDOW:
            return False
        async with self._db.read() as conn:
            taken = await conn.scalar(
                sa.select(subscriptions.c.id).where(subscriptions.c.panel_user_id == existing.id)
            )
        return taken is None

    async def _check_squads(  # noqa: PLR0917 - internal helper of create/update
        self, api: RemnawaveApi, sid: int, squads: Sequence[str], ext: str | None, lane: Lane, op: str
    ) -> bool:
        """A squad-related failure (02 §3.2, §4.1). A vanished internal squad is fatal (``PermanentJobError``
        with a «тариф сломан» alert); returns ``True`` when the external squad ``ext`` vanished (alert raised,
        the caller retries without it), ``False`` when every squad exists."""
        internal = {s.uuid for s in await api.internal_squads(lane=lane)}
        missing = [s for s in squads if s not in internal]
        if missing:
            body = "squad_gone_body" if op == "create" else "squad_gone_update_body"
            await self._raise(
                f"rw:squad_gone:{sid}",
                "error",
                _TXT["squad_gone_title"],
                _TXT[body].format(sid=sid, squads=", ".join(missing)),
                fix_screen("jobs", "dead"),
            )
            raise PermanentJobError(f"подписка {sid}: в панели нет сквадов {', '.join(missing)}")
        if ext is not None:
            external = {s.uuid for s in await api.external_squads(lane=lane)}
            if ext not in external:
                await self._raise(
                    f"rw:ext_squad_gone:{ext}",
                    "warn",
                    _TXT["ext_gone_title"],
                    _TXT["ext_gone_body"].format(sid=sid, ext=ext),
                    fix_screen("status"),
                )
                return True
        return False

    # ------------------------------------------------------------------------------------------- update

    async def _coalesce(self, job: Job, ctx: JobContext) -> tuple[set[str], set[str]]:
        """Fold the directly following ready ``panel.update`` jobs of this subscription into this one.

        The union of their ``fields``/``clear_overrides`` is written into THIS job's payload in the same
        transaction that marks them done (fenced by the lease), so a retry of this job still sends them.
        Returns ``(fields, clear_overrides)`` of this job after folding.
        """
        later = jobs.alias("later")
        blocker = (
            sa.select(sa.literal(1))
            .where(
                later.c.ordering_key == job.ordering_key,
                later.c.id > job.id,
                later.c.id < jobs.c.id,
                later.c.status.in_(("ready", "running")),
                later.c.kind != K_UPDATE,
            )
            .correlate(jobs)
            .exists()
        )
        picked = (
            sa.select(jobs.c.id, jobs.c.payload)
            .where(
                jobs.c.ordering_key == job.ordering_key,
                jobs.c.id > job.id,
                jobs.c.status == "ready",
                jobs.c.kind == K_UPDATE,
                ~blocker,
            )
            .order_by(jobs.c.id)
            .with_for_update(skip_locked=True)
        )
        fields = {str(f) for f in job.payload.get("fields") or ()}
        clear = {str(f) for f in job.payload.get("clear_overrides") or ()}
        async with self._db.tx() as conn:
            rows = (await conn.execute(picked)).all()
            if not rows:
                return fields, clear
            ids = [r.id for r in rows]
            for r in rows:
                payload = r.payload if isinstance(r.payload, dict) else {}
                fields.update(str(f) for f in payload.get("fields") or ())
                clear.update(str(f) for f in payload.get("clear_overrides") or ())
            merged = {"fields": sorted(fields), "clear_overrides": sorted(clear)}
            own = (
                sa.update(jobs)
                .where(jobs.c.id == job.id, jobs.c.status == "running", jobs.c.locked_by == ctx.worker_id)
                .values(
                    payload=jobs.c.payload.op("||")(
                        sa.cast(sa.literal(merged, jobs.c.payload.type), jobs.c.payload.type)
                    )
                )
                .returning(jobs.c.id)
            )
            if (await conn.execute(own)).first() is None:
                raise RetryJob(1, "аренда задачи потеряна до склейки")  # rolls the folding back
            await conn.execute(
                sa.update(jobs)
                .where(jobs.c.id.in_(ids))
                .values(
                    status="done",
                    done_at=sa.func.now(),
                    updated_at=sa.func.now(),
                    last_error=f"объединено с задачей #{job.id}",
                )
            )
        log.debug("panel.update #%s coalesced %d later update(s)", job.id, len(rows))
        return fields, clear

    async def _update(self, job: Job, ctx: JobContext) -> None:
        sid = int(job.payload["sub_id"])
        fields, clear = await self._coalesce(job, ctx)
        if clear:
            await self._clear_overrides(sid, clear)
        row = await self._load(sid)
        if row is None:
            return
        state = row["link_state"]
        if state == "pending":
            raise RetryJob(30, "пользователь панели ещё не создан")
        if state != "linked":
            log.info("panel.update #%s: subscription %s is %s, skipped", job.id, sid, state)
            return
        async with self._db.read() as conn:
            subs = await self._contributors.load(conn, sid)
            owners = await self._contributors.twin_owners(conn) if "squads" in fields else {}
        overrides = dict(row["overrides"] or {})
        kwargs, frozen, sent_expire = self._patch_args(row, fields, overrides, subs, owners)
        if frozen:
            # Fail-closed: the squads change waits for the module. The marker keeps it the bot's own pending
            # change (projection does not take the panel's squads for a manual edit; sync re-sends it).
            await self._set_squads_pending(sid, overrides, True)
            await self._contributors.raise_frozen(self._db, frozen)
        if not kwargs:
            if "squads" in fields and not frozen:
                await self._set_squads_pending(sid, overrides, False)
            return
        api = self._api()
        lane = self._lane(job)
        uid = int(row["panel_user_id"])
        values: dict[str, Any] = {}
        try:
            user = await api.update_user(uid, lane=lane, **kwargs)
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            if "expire_at" in kwargs and err.is_expire_in_past:
                # Clock skew between the bot and the panel: one retry further in the future (02 §4.11).
                sent_expire = now() + SKEW_RETRY_AHEAD
                kwargs["expire_at"] = sent_expire
                user = await api.update_user(uid, lane=lane, **kwargs)
            elif err.code in _GONE_SQUAD_CODES:
                # 02 §3.2/§4.1: a deleted internal squad is fatal; a deleted external one is dropped.
                ext = kwargs.get("external_squad_uuid")
                squads = list(kwargs.get("active_internal_squads") or ())
                if not await self._check_squads(api, sid, squads, ext, lane, "update"):
                    raise
                del kwargs["external_squad_uuid"]
                values["desired_ext_squad"] = None
                if not kwargs:
                    await self._store(sid, None, values)
                    return
                user = await api.update_user(uid, lane=lane, **kwargs)
            else:
                raise
        if sent_expire is not None:
            values["desired_expire_at"] = sent_expire
        if "squads" in fields and not frozen and overrides.get(SQUADS_PENDING):
            values["overrides"] = subscriptions.c.overrides.op("-")(sa.literal(SQUADS_PENDING))
        await self._store(sid, user, values)

    async def _set_squads_pending(self, sid: int, overrides: Mapping[str, Any], on: bool) -> None:
        if bool(overrides.get(SQUADS_PENDING)) == on:
            return
        expr = (
            subscriptions.c.overrides.op("||")(
                sa.cast(
                    sa.literal({SQUADS_PENDING: True}, subscriptions.c.overrides.type),
                    subscriptions.c.overrides.type,
                )
            )
            if on
            else subscriptions.c.overrides.op("-")(sa.literal(SQUADS_PENDING))
        )
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == sid)
                .values(overrides=expr, updated_at=sa.func.now())
            )

    def _patch_args(
        self,
        row: Mapping[str, Any],
        fields: set[str],
        overrides: Mapping[str, Any],
        subs: Sequence[Any],
        twin_owners: Mapping[str, str],
    ) -> tuple[dict[str, Any], tuple[str, ...], datetime | None]:
        kwargs: dict[str, Any] = {}
        frozen: tuple[str, ...] = ()
        sent_expire: datetime | None = None

        def managed(field: str) -> bool:
            return field in fields and _OVERRIDE_KEY[field] not in overrides

        if managed("expire") and row["desired_expire_at"] is not None:
            sent_expire = _clamp_expire(row["desired_expire_at"], now())
            kwargs["expire_at"] = sent_expire
        if managed("traffic") and row["desired_traffic_bytes"] is not None:
            kwargs["traffic_limit_bytes"] = int(row["desired_traffic_bytes"])
        if managed("strategy") and row["desired_reset_strategy"] is not None:
            kwargs["traffic_limit_strategy"] = row["desired_reset_strategy"]
        if managed("device_limit"):
            limit = row["desired_device_limit"]
            kwargs["hwid_device_limit"] = None if limit is None else int(limit)  # null = panel fallback
        if managed("ext_squad"):
            kwargs["external_squad_uuid"] = row["desired_ext_squad"]  # null removes it
        if managed("tag"):
            kwargs["tag"] = row["desired_tag"]  # explicitly requested: null removes the tag (02 §4.4)
        if managed("squads"):
            plan = self._contributors.plan(
                list(row["desired_squads"] or []),
                subs,
                panel_squads=row["panel_squads"],
                twin_owners=twin_owners,
            )
            if plan.frozen:
                frozen = plan.frozen_modules  # fail-closed: activeInternalSquads untouched
            elif plan.squads:
                kwargs["active_internal_squads"] = plan.squads
        return kwargs, frozen, sent_expire

    async def _clear_overrides(self, sid: int, keys: set[str]) -> None:
        expr = subscriptions.c.overrides
        for key in sorted(keys):
            expr = expr.op("-")(sa.literal(key))
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == sid)
                .values(overrides=expr, updated_at=sa.func.now())
            )

    # -------------------------------------------------------------------------------------------- renew

    async def _renew(self, job: Job, ctx: JobContext) -> None:
        sid = int(job.payload["sub_id"])
        row = await self._load(sid)
        if row is None:
            return
        if row["link_state"] == "pending":
            raise RetryJob(30, "пользователь панели ещё не создан")
        if row["link_state"] != "linked":
            log.info("panel.renew #%s: subscription %s is %s, skipped", job.id, sid, row["link_state"])
            return
        api = self._api()
        lane = self._lane(job)
        uid = int(row["panel_user_id"])
        target = _parse_dt(job.payload.get("target_expire_at"))
        old = _parse_dt(job.payload.get("old_expire_at"))
        if target is None:
            # Step 1 (02 §4.3): the target is computed ONCE from a fresh GET and committed into the job.
            try:
                user = await api.get_user(uid, lane=lane)
            except RemnawaveError as err:
                if err.kind is ErrorKind.NOT_FOUND:
                    await self._mark_missing(sid, job, row)
                    return
                raise
            current = now()
            old = user.expire_at
            base = max(old, current) if old is not None else current
            days = int(job.payload["days"])
            target = FOREVER if base >= FOREVER else min(base + timedelta(days=days), FOREVER)
            await self._save_payload(
                job,
                ctx,
                {"target_expire_at": target.isoformat(), "old_expire_at": old.isoformat() if old else None},
            )
        # Step 2: PATCH the absolute target; a repeat sends the very same date.
        try:
            user = await api.update_user(uid, expire_at=_clamp_expire(target, now()), lane=lane)
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            raise
        values = {"desired_expire_at": target, "paid_until": target}
        delta = int((target - old).total_seconds()) if old is not None else None
        await self._store(
            sid,
            user,
            values,
            event={
                "kind": "renewed",
                "source": "bot",
                "delta_seconds": delta,
                "old_expire": old,
                "new_expire": target,
                "ref_type": "job",
                "ref_id": str(job.id),
            },
        )

    # ------------------------------------------------------------------------------------------ actions

    async def _linked_row(self, job: Job, *, allow_closed: bool = False) -> Mapping[str, Any] | None:
        """The row of a subscription whose panel user exists. A closed one only for disable/delete."""
        sid = int(job.payload["sub_id"])
        row = await self._load(sid)
        if row is None:
            return None
        if row["link_state"] == "pending":
            raise RetryJob(30, "пользователь панели ещё не создан")
        states = ("linked", "closed") if allow_closed else ("linked",)
        if row["panel_user_id"] is None or row["link_state"] not in states:
            log.info("%s #%s: subscription %s is %s, skipped", job.kind, job.id, sid, row["link_state"])
            return None
        return row

    async def _disable(self, job: Job, ctx: JobContext) -> None:
        row = await self._linked_row(job, allow_closed=True)
        if row is None:
            return
        sid = int(row["id"])
        reason = str(job.payload.get("reason") or "admin")
        try:
            user = await self._api().disable(int(row["panel_user_id"]), lane=self._lane(job))
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            raise
        values: dict[str, Any] = {"desired_status": "disabled", "disabled_reason": reason}
        event: dict[str, Any] | None = None
        if reason == "closed" and row["link_state"] != "closed":
            # delete_mode=disable (02 §4.10): the panel user stays for history, the subscription is CLOSED.
            values["link_state"] = "closed"
            event = {"kind": "closed", "source": "bot", "ref_type": "job", "ref_id": str(job.id)}
        await self._store(sid, user, values, event=event)

    async def _enable(self, job: Job, ctx: JobContext) -> None:
        row = await self._linked_row(job)
        if row is None:
            return
        sid = int(row["id"])
        api = self._api()
        lane = self._lane(job)
        only_reason = job.payload.get("only_reason")
        if only_reason is not None and row["disabled_reason"] != only_reason:
            return  # e.g. unban lifts only BOT_BAN, never an admin's disable
        try:
            current = await api.get_user(int(row["panel_user_id"]), lane=lane)
            values: dict[str, Any] = {"desired_status": "active", "disabled_reason": None}
            if current.expire_at is None or current.expire_at <= now() + ENABLE_MIN_AHEAD:
                # Enabling an expired user "bounces" back to EXPIRED within 30 s (02 §4.9): only clear flags.
                await self._store(sid, current, values)
                return
            user = await api.enable(int(row["panel_user_id"]), lane=lane)
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            raise
        await self._store(sid, user or current, values)

    async def _delete(self, job: Job, ctx: JobContext) -> None:
        row = await self._linked_row(job, allow_closed=True)
        if row is None:
            return
        await self._api().delete_user(int(row["panel_user_id"]), lane=self._lane(job))  # 404 = already gone
        await self._store(
            int(row["id"]),
            None,
            {"link_state": "closed", "disabled_reason": "closed"},
            event={"kind": "closed", "source": "bot", "ref_type": "job", "ref_id": str(job.id)},
        )

    async def _revoke(self, job: Job, ctx: JobContext) -> None:
        row = await self._linked_row(job)
        if row is None:
            return
        sid = int(row["id"])
        api = self._api()
        lane = self._lane(job)
        uid = int(row["panel_user_id"])
        try:
            current = await api.get_user(uid, lane=lane)
            if current.sub_revoked_at is not None and current.sub_revoked_at >= job.created_at:
                user = current  # done by our previous attempt: never revoke twice (02 §4.8)
            else:
                user = await api.revoke(uid, bool(job.payload.get("only_passwords")), lane=lane)
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            raise
        await self._store(
            sid, user, event={"kind": "revoked", "source": "bot", "ref_type": "job", "ref_id": str(job.id)}
        )

    async def _reset_traffic(self, job: Job, ctx: JobContext) -> None:
        row = await self._linked_row(job)
        if row is None:
            return
        sid = int(row["id"])
        api = self._api()
        lane = self._lane(job)
        uid = int(row["panel_user_id"])
        try:
            current = await api.get_user(uid, lane=lane)
            reset_at = current.last_traffic_reset_at
            if reset_at is not None and reset_at >= job.created_at:
                user = current  # already reset by our previous attempt
            else:
                user = await api.reset_traffic(uid, lane=lane)
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                await self._mark_missing(sid, job, row)
                return
            raise
        await self._store(
            sid,
            user,
            event={"kind": "traffic_reset", "source": "bot", "ref_type": "job", "ref_id": str(job.id)},
        )


async def _insert_event(conn: AsyncConnection, sid: int, event: Mapping[str, Any]) -> None:
    stmt = pg_insert(subscription_events).values(subscription_id=sid, **event)
    if event.get("ref_id") is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["subscription_id", "kind", "ref_type", "ref_id"],
            index_where=sa.text(EVENT_REF_PREDICATE),
        )
    await conn.execute(stmt)


# ------------------------------------------------------------------------------------------- HWID devices

HWID_UNREACHABLE_RETRY_S: Final = 60.0
_DEVICE_GONE_CODES: Final = frozenset({"A204"})


class DeviceJobs:
    """Handlers of ``panel.hwid_delete`` / ``panel.hwid_reset`` (02 §4.7), part of the single panel writer
    (:meth:`PanelWriter.handlers` registers them).

    Same conventions as the writer: idempotent calls (a device that is already gone counts as deleted), a
    ``pending`` subscription waits, a user gone from the panel → ``panel_missing``, ``auth``/``scope``/
    ``validation`` → dead, an unreachable panel → retry without using up attempts. Connections are dropped
    afterwards, best effort; the result is journaled once (``subscription_events`` by job id) and announced.
    """

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        attention: AttentionService | None = None,
    ) -> None:
        self._db = db
        self._api = api
        self._attention = attention

    def handlers(self) -> dict[str, Handler]:
        return {K_HWID_DELETE: self._wrap(self._delete), K_HWID_RESET: self._wrap(self._reset)}

    def _wrap(self, fn: Callable[[Job, Mapping[str, Any]], Any]) -> Handler:
        async def handler(job: Job, ctx: JobContext) -> None:
            sid = int(job.payload["sub_id"])
            async with self._db.read() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.select(
                                subscriptions.c.id,
                                subscriptions.c.link_state,
                                subscriptions.c.panel_user_id,
                                subscriptions.c.panel_username,
                            ).where(subscriptions.c.id == sid)
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                return
            if row["link_state"] == "pending":
                raise RetryJob(30, "пользователь панели ещё не создан")
            if row["link_state"] != "linked" or row["panel_user_id"] is None:
                return
            try:
                await fn(job, row)
            except (PanelNotConfiguredError, PanelUnavailableError, WriteBlockedError) as err:
                raise RetryJob(HWID_UNREACHABLE_RETRY_S, str(err)) from err
            except RemnawaveError as err:
                if err.code in USER_NOT_FOUND_CODES:
                    await mark_missing(
                        self._db,
                        sid,
                        source="bot",
                        username=row["panel_username"],
                        ref_type="job",
                        ref_id=str(job.id),
                        attention=self._attention,
                    )
                    return
                if err.kind in (ErrorKind.AUTH, ErrorKind.FORBIDDEN_SCOPE, ErrorKind.VALIDATION):
                    raise PermanentJobError(str(err)) from err
                if err.retry_after:
                    raise RetryJob(err.retry_after, str(err)) from err
                raise

        handler.__qualname__ = f"DeviceJobs.{fn.__name__.lstrip('_')}"
        return handler

    @staticmethod
    def _lane(job: Job) -> Lane:
        return Lane.INTERACTIVE if job.lane == "interactive" else Lane.BACKGROUND

    async def _delete(self, job: Job, row: Mapping[str, Any]) -> None:
        hwid = str(job.payload.get("hwid") or "")
        if not hwid:
            raise PermanentJobError("не указан hwid")
        uid = int(row["panel_user_id"])
        lane = self._lane(job)
        try:
            left = await self._api().delete_device(uid, hwid, lane=lane)
        except RemnawaveError as err:
            if err.code not in _DEVICE_GONE_CODES and not (
                err.kind is ErrorKind.NOT_FOUND and err.code not in USER_NOT_FOUND_CODES
            ):
                raise
            left = None  # already deleted (by our previous attempt or by the user elsewhere)
        if left is not None and left.has(hwid):
            raise RetryJob(5, "панель не удалила устройство")
        await self._drop(uid, lane)
        await self._done(job, int(row["id"]), "device_deleted", {"hwid_tail": hwid[-6:]})

    async def _reset(self, job: Job, row: Mapping[str, Any]) -> None:
        uid = int(row["panel_user_id"])
        lane = self._lane(job)
        left = await self._api().delete_all_devices(uid, lane=lane)
        if left.total:
            raise RetryJob(5, "панель удалила не все устройства")
        await self._drop(uid, lane)
        await self._done(job, int(row["id"]), "devices_reset", {})

    async def _drop(self, uid: int, lane: Lane) -> None:
        try:
            await self._api().drop_connections([uid], lane=lane)
        except RemnawaveError as err:  # optional step (02 §4.7 p.3): a missing scope is not a failure
            log.info("drop connections for panel user %s skipped: %s", uid, err.kind.value)

    async def _done(self, job: Job, sid: int, kind: str, details: Mapping[str, Any]) -> None:
        async with self._db.tx() as conn:
            if await journal.record(
                conn, sid, kind, source="bot", ref_type="job", ref_id=str(job.id), details=details
            ):
                await hooks.emit(conn, f"subscription.{kind}", {"subscription_id": sid, **details})


# ----------------------------------------------------------------------------------- out of band


async def emergency_set_squads(api: RemnawaveApi, panel_user_id: int, squads: Sequence[str]) -> None:
    """The only panel write allowed outside the outbox: ``svbg lte release --panel-only`` when the bot and
    its database are down (05 §2.1, the owner's emergency switch). One absolute ``PATCH`` of
    ``activeInternalSquads``; an empty list is refused (it would take the user off every node)."""
    items = [str(s) for s in squads if str(s)]
    if not items:
        raise ValueError("пустой список сквадов снял бы пользователя со всех нод — не отправляем")
    await api.update_user(int(panel_user_id), active_internal_squads=items, lane=Lane.BACKGROUND)
