"""Reconciliation with the panel through ``GET /users/stream`` keyset pages (02 §6.2, §6.4).

* **full pass** — every panel user page by page (size 500, O(page) memory: only the ids seen are kept), each
  linked subscription projected (:mod:`svbg.remnawave.projection`); afterwards linked subscriptions the panel
  did not return are confirmed one by one with ``GET`` and only a ``404`` makes them ``panel_missing``;
* **fast pass** — only ``status=LIMITED`` users (without webhooks ``LIMITED`` is the one surprise the bot
  cannot predict), every 5 min;
* schedule — :meth:`Reconciler.tick` every 5 min: fast pass when webhooks are off, full pass when due (60 min
  with webhooks, 15 min without);
* **safety fuse** («не та панель»): the panel returned no users, the configured subscription domain changed,
  or less than half of the linked subscriptions were found with the same id **and** username → the pass stops,
  nothing is marked missing, nothing is written to the panel, and the owner gets an «Требует внимания» item.
  Per-page projection only ever touches users whose id and username both match (the same panel user), so even
  an aborted pass never applies a foreign panel's data;
* **no writes** unless a real drift needs the writer (a module substitution changed while the plan did not):
  manual edits are overrides, not drift; rows whose projection changes nothing are not even updated;
* one pass at a time: a lease row in ``rw_sync_state`` (works across processes);
* a page is projected with the time taken **before** its request as ``state_ts``: a subscription written by
  the writer meanwhile (its ``panel_state_ts`` is later) is skipped as stale instead of being rolled back;
* the API object is taken per request (a hot swap closes the old session between pages); another panel
  address mid-pass ends the pass as ``busy`` (it runs again on the next tick), without an error;
* a legitimate change of the subscription domain is confirmed by the owner (:meth:`Reconciler.confirm_domain`,
  button of the fuse item), which accepts exactly the domain the owner saw;
* squads changes held back by a down module (``overrides._squads_pending``) are re-sent on the first tick
  after the module is OK again.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.bus import Event
from svbg.core.clock import now
from svbg.core.component import fix_screen
from svbg.core.tables import users
from svbg.jobs.tables import jobs
from svbg.remnawave.contributors import SquadContributors
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.importer import same_panel
from svbg.remnawave.models import PanelUser, UsersPage
from svbg.remnawave.projection import (
    SQUADS_PENDING,
    Projection,
    apply,
    compute,
    mark_missing,
    publish,
    select_subscriptions,
)
from svbg.remnawave.tables import rw_sync_state
from svbg.remnawave.transport import Lane
from svbg.remnawave.writer import QUEUE, enqueue_update, ordering_key
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from svbg.core.attention import AttentionService
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.jobs.scheduler import Scheduler
    from svbg.remnawave.api import RemnawaveApi

__all__ = ["CONFIRM_DOMAIN_SCREEN", "FUSE_KEY", "PAGE_SIZE", "Reconciler", "SyncReport"]

log = logging.getLogger("svbg.remnawave.sync")

PAGE_SIZE: Final = 500
TICK_S: Final = 300.0
FULL_WITH_WEBHOOKS: Final = timedelta(minutes=60)
FULL_WITHOUT_WEBHOOKS: Final = timedelta(minutes=15)
LEASE: Final = timedelta(minutes=30)
MISSING_BATCH: Final = 1000
RESUME_BATCH: Final = 500
FUSE_KEY: Final = "rw:sync_fuse"
#: ``fix_action`` screen of a domain fuse: «Это та же панель — подтвердить» → ``Reconciler.confirm_domain``.
CONFIRM_DOMAIN_SCREEN: Final = "rw_sync_confirm"

_TXT_FUSE_TITLE = "Сверка с панелью остановлена: похоже, подключена другая панель"
_TXT_FUSE = {
    "empty": "Панель вернула 0 пользователей, а в боте {linked} связанных подписок.",
    "coverage": "В панели найдено только {matched} из {linked} связанных подписок (нужно не меньше {need}).",
    "domain": "Изменился домен подписок панели: было «{old}», стало «{new}».",
}
_TXT_FUSE_TAIL = (
    " Бот ничего не изменил: подписки не помечены удалёнными, в панель ничего не записано. Проверьте адрес и "
    "токен панели в «Настройки → Remnawave»."
)
_TXT_FUSE_CONFIRM = (
    " Если панель та же и домен подписок сменили намеренно, нажмите «Это та же панель — подтвердить»: бот "
    "запомнит новый домен и выполнит сверку."
)
_TXT_RESTART = "панель переподключена во время сверки; повтор на следующем проходе"


@dataclass(slots=True)
class SyncReport:
    kind: str
    trigger: str
    status: str = "ok"  # ok | aborted | busy | error
    reason: str | None = None
    pages: int = 0
    seen: int = 0
    linked: int = 0
    matched: int = 0
    foreign: int = 0
    unknown: int = 0
    unlinked_known: int = 0
    updated: int = 0
    overrides: int = 0
    drift: int = 0
    missing: int = 0
    #: The panel's current subscription domain (on a ``domain`` abort: the one the owner may confirm).
    domain: str | None = None
    started_at: datetime = field(default_factory=now)
    finished_at: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        return data


class _Abort(Exception):
    def __init__(self, reason: str, text: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.text = text


class _Restart(Exception):
    """The panel address changed mid-pass (hot swap): stop quietly, the next tick starts over."""


class Reconciler:
    """Full and fast reconciliation passes (see module docstring)."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        contributors: SquadContributors,
        attention: AttentionService | None = None,
        bus: EventBus | None = None,
        webhooks_enabled: Callable[[], bool] = lambda: False,
        page_size: int = PAGE_SIZE,
        min_coverage: float = 0.5,
        full_with_webhooks: timedelta = FULL_WITH_WEBHOOKS,
        full_without_webhooks: timedelta = FULL_WITHOUT_WEBHOOKS,
        holder: str | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be in 1..1000")
        self._db = db
        self._api = api
        self._contributors = contributors
        self._attention = attention
        self._bus = bus
        self._webhooks = webhooks_enabled
        self._page = page_size
        self._coverage = min_coverage
        self._full_with = full_with_webhooks
        self._full_without = full_without_webhooks
        self._holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.last: dict[str, SyncReport] = {}

    def register(self, scheduler: Scheduler) -> None:
        scheduler.every("remnawave.sync", TICK_S, self.tick, jitter_s=15, run_at_start=True)

    async def tick(self) -> None:
        """Every 5 min: frozen-squad items (+ re-send held squads), fast pass without webhooks, full pass when
        due."""
        await self._contributors.refresh_attention(self._db)
        await self.resume_frozen_squads()
        webhooks = self._webhooks()
        if not webhooks:
            await self.fast_pass("schedule")
        interval = self._full_with if webhooks else self._full_without
        async with self._db.read() as conn:
            last = await conn.scalar(
                sa.select(rw_sync_state.c.last_ok_at).where(rw_sync_state.c.name == "full")
            )
        if last is None or now() - last >= interval:
            await self.full_pass("schedule")

    # --------------------------------------------------------------------------------------------- lease

    async def _acquire(self, name: str) -> Mapping[str, Any] | None:
        async with self._db.tx() as conn:
            await conn.execute(pg_insert(rw_sync_state).values(name=name).on_conflict_do_nothing())
            row = (
                (
                    await conn.execute(
                        sa.update(rw_sync_state)
                        .where(
                            rw_sync_state.c.name == name,
                            sa.or_(
                                rw_sync_state.c.locked_until.is_(None),
                                rw_sync_state.c.locked_until < sa.func.now(),
                            ),
                        )
                        .values(
                            holder=self._holder,
                            locked_until=sa.func.now() + LEASE,
                            last_started_at=sa.func.now(),
                        )
                        .returning(rw_sync_state.c.sub_domain)
                    )
                )
                .mappings()
                .first()
            )
        return row

    async def _release(self, name: str, report: SyncReport, *, ok: bool, domain: str | None = None) -> None:
        values: dict[str, Any] = {"holder": None, "locked_until": None, "report": report.as_json()}
        if ok:
            values["last_ok_at"] = sa.func.now()
            if domain:
                values["sub_domain"] = domain
        async with self._db.tx() as conn:
            await conn.execute(sa.update(rw_sync_state).where(rw_sync_state.c.name == name).values(**values))

    # ------------------------------------------------------------------------------------------- passes

    async def confirm_domain(self, domain: str) -> SyncReport:
        """The owner confirmed «это та же панель» for the subscription domain ``domain`` (the one shown in the
        fuse item): a full pass that accepts exactly this domain and remembers it. Any other domain still
        aborts the pass."""
        if not domain:
            raise ValueError("domain must not be empty")
        return await self.full_pass("manual", accept_domain=domain)

    async def full_pass(self, trigger: str = "manual", *, accept_domain: str | None = None) -> SyncReport:
        report = SyncReport("full", trigger)
        state = await self._acquire("full")
        if state is None:
            report.status, report.reason = "busy", "сверка уже идёт"
            return report
        domain: str | None = None
        ok = False
        try:
            domain = await self._run_full(report, state.get("sub_domain"), accept_domain)
            ok = report.status == "ok"
        except _Abort as abort:
            report.status, report.reason = "aborted", abort.reason
            await self._fuse(abort.text, domain=abort.reason == "domain")
        except _Restart:
            report.status, report.reason = "busy", _TXT_RESTART
        except Exception as exc:
            report.status, report.reason = "error", type(exc).__name__
            raise
        finally:
            report.finished_at = now()
            self.last["full"] = report
            await self._release("full", report, ok=ok, domain=domain)
            log.info("remnawave full sync %s: %s", report.status, report.as_json())
        if ok and self._attention is not None:
            try:
                await self._attention.resolve(FUSE_KEY)
            except Exception:
                log.exception("could not resolve %s", FUSE_KEY)
        return report

    async def _run_full(
        self, report: SyncReport, old_domain: str | None, accept_domain: str | None = None
    ) -> str | None:
        api = self._api()
        domain: str | None = None
        try:
            config = await api.configuration(lane=Lane.BACKGROUND)
            domain = config.misc.sub_public_domain or None
        except RemnawaveError as err:
            if err.kind is not ErrorKind.FORBIDDEN_SCOPE:
                raise
        report.domain = domain
        if old_domain and domain and domain not in (old_domain, accept_domain):
            raise _Abort(
                "domain",
                _TXT_FUSE["domain"].format(old=old_domain, new=domain) + _TXT_FUSE_TAIL + _TXT_FUSE_CONFIRM,
            )
        async with self._db.read() as conn:
            report.linked = int(
                await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(subscriptions)
                    .where(subscriptions.c.link_state == "linked")
                )
                or 0
            )
            twins = await self._contributors.twins(conn)
            owners = await self._contributors.twin_owners(conn)
        seen: set[int] = set()
        drift: list[int] = []
        first = True
        async for page, fetched_at in self._pages(api):
            report.pages += 1
            if first and not page.users and report.linked > 0:
                raise _Abort("empty", _TXT_FUSE["empty"].format(linked=report.linked) + _TXT_FUSE_TAIL)
            first = False
            seen.update(u.id for u in page.users)
            report.seen += len(page.users)
            drift.extend(
                await self._project_page(
                    page.users, twins, owners, report, source="sync", state_ts=fetched_at
                )
            )
        need = int(report.linked * self._coverage + 0.999999)
        if report.linked > 0 and report.matched < need:
            raise _Abort(
                "coverage",
                _TXT_FUSE["coverage"].format(matched=report.matched, linked=report.linked, need=need)
                + _TXT_FUSE_TAIL,
            )
        if drift:
            async with self._db.tx() as conn:
                for sid in drift:
                    await enqueue_update(conn, sid, ["squads"], lane="background", caused_by="sync")
            report.drift = len(drift)
        report.missing = await self._confirm_missing(api, seen)
        if report.unlinked_known:
            self._emit("remnawave.sync.unlinked", {"count": report.unlinked_known})
        return domain

    async def fast_pass(self, trigger: str = "manual") -> SyncReport:
        """Only ``LIMITED`` users (no-webhook mode). No fuse/missing logic: it never marks anything gone."""
        report = SyncReport("fast", trigger)
        if await self._acquire("fast") is None:
            report.status, report.reason = "busy", "проход уже идёт"
            return report
        ok = False
        try:
            api = self._api()
            async with self._db.read() as conn:
                twins = await self._contributors.twins(conn)
                owners = await self._contributors.twin_owners(conn)
            async for page, fetched_at in self._pages(api, status="LIMITED"):
                report.pages += 1
                report.seen += len(page.users)
                await self._project_page(
                    page.users, twins, owners, report, source="sync_fast", state_ts=fetched_at
                )
            ok = True
        except _Restart:
            report.status, report.reason = "busy", _TXT_RESTART
        except Exception as exc:
            report.status, report.reason = "error", type(exc).__name__
            raise
        finally:
            report.finished_at = now()
            self.last["fast"] = report
            await self._release("fast", report, ok=ok)
        return report

    # ---------------------------------------------------------------------------------------------- page

    def _current(self, first: RemnawaveApi) -> RemnawaveApi:
        """The API for the next request; another panel address than at the start → :class:`_Restart`."""
        api = self._api()
        if not same_panel(api, first):
            raise _Restart
        return api

    async def _pages(self, first: RemnawaveApi, **filters: Any) -> AsyncIterator[tuple[UsersPage, datetime]]:
        """``users/stream`` keyset pages with the time taken right BEFORE each request (``state_ts``)."""
        cursor: str | None = None
        while True:
            api = self._current(first)
            fetched_at = now()
            page = await api.stream(cursor, self._page, lane=Lane.BACKGROUND, **filters)
            yield page, fetched_at
            nxt = page.cursor
            if not page.has_more or nxt is None or nxt == cursor:
                return
            cursor = nxt

    async def _project_page(
        self,
        page: Sequence[PanelUser],
        twins: Mapping[str, str],
        owners: Mapping[str, str],
        report: SyncReport,
        *,
        source: str,
        state_ts: datetime,
    ) -> list[int]:
        """Project one page; returns subscriptions with squad drift (enqueued later, after the fuse).

        ``state_ts`` is when the page was requested: rows the writer stored after it are stale and skipped.
        """
        if not page:
            return []
        ids = [u.id for u in page]
        async with self._db.read() as conn:
            rows = {
                int(r["panel_user_id"]): r
                for r in (
                    await conn.execute(select_subscriptions().where(subscriptions.c.panel_user_id.in_(ids)))
                ).mappings()
            }
            sub_ids = [int(r["id"]) for r in rows.values()]
            pending = await _pending(conn, sub_ids)
            substitutions = await self._contributors.load_many(conn, sub_ids)
            unknown_tg = [u.telegram_id for u in page if u.id not in rows and u.telegram_id is not None]
            known_tg: set[int] = set()
            if unknown_tg:
                known_tg = {
                    int(t)
                    for t in (
                        await conn.execute(
                            sa.select(users.c.telegram_id).where(users.c.telegram_id.in_(unknown_tg))
                        )
                    ).scalars()
                }
        changes: list[tuple[Mapping[str, Any], Projection]] = []
        drift: list[int] = []
        for user in page:
            row = rows.get(user.id)
            if row is None:
                report.unknown += 1
                if user.telegram_id is not None and user.telegram_id in known_tg:
                    report.unlinked_known += 1
                continue
            if row["panel_username"] is not None and row["panel_username"] != user.username:
                report.foreign += 1  # same id, different user: another panel (never projected)
                continue
            if row["link_state"] == "closed":
                continue  # the panel user of a closed subscription is history: not projected
            report.matched += 1
            sid = int(row["id"])
            subs = substitutions.get(sid, [])
            frozen = self._contributors.frozen_modules(
                subs, panel_squads=user.squad_uuids, twin_owners=owners
            )
            result = compute(
                row,
                user,
                twins=twins,
                substitutions=subs,
                frozen=bool(frozen),
                pending=sid in pending,
                state_ts=state_ts,
                source=source,
            )
            if result.stale:
                continue
            if result.changed:
                changes.append((row, result))
            if result.drift and sid not in pending:
                drift.append(sid)
        if changes:
            written: list[Projection] = []
            async with self._db.tx() as conn:
                for row, result in changes:
                    if await apply(conn, row, result):
                        written.append(result)
            report.updated += len(written)
            for result in written:
                if "overrides" in result.values:
                    report.overrides += 1
                await publish(result, attention=self._attention, bus=self._bus)
        return drift

    async def _confirm_missing(self, first: RemnawaveApi, seen: set[int]) -> int:
        """Linked subscriptions the stream did not return: ``GET`` each; only a 404 means "deleted"."""
        missing = 0
        last_id = 0
        while True:
            async with self._db.read() as conn:
                batch = (
                    (
                        await conn.execute(
                            sa.select(
                                subscriptions.c.id,
                                subscriptions.c.panel_user_id,
                                subscriptions.c.panel_username,
                            )
                            .where(subscriptions.c.link_state == "linked", subscriptions.c.id > last_id)
                            .order_by(subscriptions.c.id)
                            .limit(MISSING_BATCH)
                        )
                    )
                    .mappings()
                    .all()
                )
            if not batch:
                return missing
            last_id = int(batch[-1]["id"])
            for row in batch:
                uid = int(row["panel_user_id"])
                if uid in seen:
                    continue
                try:
                    await self._current(first).get_user(uid, lane=Lane.BACKGROUND)
                    continue  # created after the stream passed its id, or a transient gap: not missing
                except RemnawaveError as err:
                    if err.kind is not ErrorKind.NOT_FOUND:
                        raise
                if await mark_missing(
                    self._db,
                    int(row["id"]),
                    source="sync",
                    username=row["panel_username"],
                    attention=self._attention,
                    bus=self._bus,
                ):
                    missing += 1

    # ----------------------------------------------------------------------------------- frozen squads

    async def resume_frozen_squads(self) -> int:
        """Squads changes the writer held back while their module was down (``overrides._squads_pending``):
        once no module blocks them, queue ``panel.update(['squads'])``. Returns the number queued."""
        async with self._db.read() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(subscriptions.c.id, subscriptions.c.panel_squads)
                        .where(
                            subscriptions.c.link_state == "linked",
                            subscriptions.c.overrides.has_key(SQUADS_PENDING),
                        )
                        .order_by(subscriptions.c.id)
                        .limit(RESUME_BATCH)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return 0
            ids = [int(r["id"]) for r in rows]
            pending = await _pending(conn, ids)
            subs = await self._contributors.load_many(conn, ids)
            owners = await self._contributors.twin_owners(conn)
        ready = [
            int(r["id"])
            for r in rows
            if int(r["id"]) not in pending
            and not self._contributors.frozen_modules(
                subs.get(int(r["id"]), []), panel_squads=r["panel_squads"], twin_owners=owners
            )
        ]
        if ready:
            async with self._db.tx() as conn:
                for sid in ready:
                    await enqueue_update(conn, sid, ["squads"], lane="background", caused_by="squads_resume")
            log.info("remnawave: %d held squads change(s) re-queued after module recovery", len(ready))
        return len(ready)

    # ---------------------------------------------------------------------------------------------- misc

    async def _fuse(self, text: str, *, domain: bool = False) -> None:
        log.warning("remnawave sync aborted by the safety fuse: %s", text)
        if self._attention is not None:
            fix = fix_screen(CONFIRM_DOMAIN_SCREEN) if domain else fix_screen("status")
            try:
                await self._attention.raise_item(FUSE_KEY, "error", _TXT_FUSE_TITLE, text, fix_action=fix)
            except Exception:
                log.exception("could not raise %s", FUSE_KEY)
        self._emit("remnawave.sync.fuse", {"text": text})

    def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish_nowait(Event(name, dict(payload)))
        except Exception:
            log.exception("could not publish %s", name)


async def _pending(conn: Any, sub_ids: Sequence[int]) -> set[int]:
    """Subscriptions with a ready/running panel operation: their desired fields are not compared (02 §6.1)."""
    if not sub_ids:
        return set()
    keys = [ordering_key(s) for s in sub_ids]
    rows = (
        await conn.execute(
            sa.select(jobs.c.ordering_key)
            .where(
                jobs.c.ordering_key.in_(keys),
                jobs.c.queue == QUEUE,
                jobs.c.status.in_(("ready", "running")),
            )
            .distinct()
        )
    ).scalars()
    return {int(k.split(":", 1)[1]) for k in rows}
