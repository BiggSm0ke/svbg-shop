"""LTE quotas: applying decisions to the panel through the core (05 §2.1.10, 07 §2.4.3 fail-closed).

The module never writes the panel's squads itself. A decision is applied in **one transaction** that changes
``lte_blocks`` and the core table ``panel_squad_substitutions`` together (block → a row "base → twin" appears,
release → the row disappears) and queues the core writer (``panel.update`` of ``squads``, FIFO per
subscription). The writer applies the substitutions fail-closed: while the module is degraded or switched off
it does not touch ``activeInternalSquads`` of subscriptions with LTE rows at all.

After the writer's job (same ``ordering_key``) the module's own ``lte.confirm`` job checks the panel snapshot:

* a block is ``applied`` once the twin shows in ``panel_squads``; after :data:`APPLY_ATTEMPTS` failures it is
  cancelled with an alert («блок неприменим»);
* a release is ``released`` once no twin of the block is left; it is retried for ever (never cancelled) and a
  control re-send is queued: ``lte.resend`` in 150 s (``POST internal-squads/{first}/bulk-actions/
  add-many-users`` — membership unchanged, the panel re-publishes the event to the nodes, 05 §2.1.10).

Shadow decisions are journaled as ``mode='shadow'`` rows and never reach the panel. ``release_all`` is the
"снять" path of switching the module off and of the emergency button / CLI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

import msgspec
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.ext.lte.decide import (
    MODE_ENFORCE,
    MODE_SHADOW,
    REASON_MANUAL,
    BlockCandidate,
    Release,
    Twin,
    substitutions_for,
)
from svbg.ext.lte.tables import lte_blocks, lte_twins
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.remnawave.errors import (
    ErrorKind,
    PanelNotConfiguredError,
    PanelUnavailableError,
    RemnawaveError,
    WriteBlockedError,
)
from svbg.remnawave.transport import Lane
from svbg.remnawave.writer import enqueue_update, ordering_key
from svbg.subscriptions.tables import panel_squad_substitutions, panel_squad_twins, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database
    from svbg.ext.lte.planner import Plan
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "APPLY_ATTEMPTS",
    "CONFIRM_KIND",
    "MODULE",
    "RESEND_DELAY",
    "RESEND_KIND",
    "AppliedPlan",
    "Enforcer",
    "PlacedBlock",
    "block_ref",
    "enqueue_resend",
    "fallback_substitutions",
    "manual_candidate",
    "sync_twins",
]

log = logging.getLogger("svbg.ext.lte.enforce")

MODULE: Final = "lte"
CONFIRM_KIND: Final = "lte.confirm"
RESEND_KIND: Final = "lte.resend"
#: Control re-send after a release (05 §2.1.7 code constant).
RESEND_DELAY: Final = timedelta(seconds=150)
APPLY_ATTEMPTS: Final = 20
CONFIRM_RETRY_S: Final = 30.0
UNLINKED_RETRY_S: Final = 300.0
UNREACHABLE_RETRY_S: Final = 60.0
RELEASE_BATCH: Final = 200
_LIVE: Final = ("active", "releasing")

_T: Final = {
    "unenforceable_title": "LTE: блок неприменим",
    "unenforceable_body": "Подписка №{sid}: лимит LTE исчерпан, но у её сквада нет годного двойника "
    "(нет в карте, пустой или с ошибкой). Блок не поставлен, LTE у пользователя работает. Проверьте карту "
    "«база → двойник» на экране «🌐 Трафик LTE».",
    "apply_failed_title": "LTE: блок не применился",
    "apply_failed_body": "Подписка №{sid}: двойник так и не появился в панели после {n} проверок. Блок снят, "
    "чтобы не держать пользователя в неизвестном состоянии. Проверьте очередь панели в «Состояние».",
    "scopes_title": "LTE: нет прав на переотправку",
    "scopes_body": "Панель отклонила add-many-users. Выдайте токену скоуп internal-squads:add-many-users.",
}


def block_ref(block_id: int) -> str:
    """``source_ref`` of the substitution rows of a block."""
    return f"lte:block:{int(block_id)}"


@dataclass(frozen=True, slots=True)
class PlacedBlock:
    block_id: int
    subscription_id: int
    group_id: int
    period_id: int
    mode: str
    reason: str
    used_bytes: int
    limit_bytes: int


@dataclass(slots=True)
class AppliedPlan:
    """What :meth:`Enforcer.apply_plan` did (for notifications, cards and attention after the commit)."""

    placed: list[PlacedBlock] = field(default_factory=list)
    released: list[tuple[int, int, int, str]] = field(default_factory=list)  # (block, sub, group, reason)
    restored: list[int] = field(default_factory=list)
    rebound: list[int] = field(default_factory=list)
    unenforceable: list[tuple[int, int]] = field(default_factory=list)  # (sub, group)

    @property
    def panel_writes(self) -> int:
        return sum(1 for b in self.placed if b.mode == MODE_ENFORCE) + len(self.restored) + len(self.released)


def fallback_substitutions(
    desired: Sequence[str], group_ids: Collection[int], twins: Mapping[str, Twin]
) -> dict[str, str]:
    """Without the panel topology: every desired base with a healthy twin of a blocked group."""
    out: dict[str, str] = {}
    for base in desired:
        twin = twins.get(base)
        if twin is not None and twin.group_id in group_ids and twin.problem is None:
            out[base] = twin.twin_squad_uuid
    return out


def _substitutions(
    desired: Sequence[str],
    group_id: int,
    twins: Mapping[str, Twin],
    squad_inbounds: Mapping[str, Collection[str]] | None,
    group_tags: Mapping[int, Collection[str]] | None,
) -> dict[str, str]:
    if squad_inbounds and group_tags and group_tags.get(group_id):
        subs, _missing = substitutions_for(
            desired, {group_id}, twins, squad_inbounds=squad_inbounds, group_tags=group_tags
        )
        return subs
    return fallback_substitutions(desired, {group_id}, twins)


async def sync_twins(conn: AsyncConnection, twins: Iterable[Twin] | None = None) -> int:
    """Mirror ``lte_twins`` into the core map ``panel_squad_twins`` (owner ``lte``) for ``reverse``.

    A core row is removed only when no live substitution still uses its twin (fail-closed).
    """
    if twins is None:
        rows = (await conn.execute(sa.select(lte_twins))).mappings().all()
        twins = [
            Twin(r["base_squad_uuid"], int(r["group_id"]), r["twin_squad_uuid"], r["problem"]) for r in rows
        ]
    items = list(twins)
    t = panel_squad_twins
    if items:
        stmt = pg_insert(t).values(
            [
                {
                    "substitute_squad_uuid": x.twin_squad_uuid,
                    "base_squad_uuid": x.base_squad_uuid,
                    "owner_module": MODULE,
                }
                for x in items
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.substitute_squad_uuid],
            set_={"base_squad_uuid": stmt.excluded.base_squad_uuid},
            where=t.c.owner_module == MODULE,
        )
        await conn.execute(stmt)
    keep = [x.twin_squad_uuid for x in items]
    used = sa.select(panel_squad_substitutions.c.substitute_squad_uuid).where(
        panel_squad_substitutions.c.owner_module == MODULE
    )
    gone = sa.delete(t).where(
        t.c.owner_module == MODULE,
        t.c.substitute_squad_uuid.not_in(used),
        t.c.substitute_squad_uuid.not_in(keep or [""]),
    )
    result = await conn.execute(gone)
    return len(items) + int(result.rowcount or 0)


class Enforcer:
    """Writes decisions (in the caller's transaction) and runs the confirm / re-send jobs."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi] | None = None,
        *,
        attention: AttentionService | None = None,
    ) -> None:
        self._db = db
        self._api = api
        self._attention = attention

    # ---------------------------------------------------------------------------------- plan → rows

    async def apply_plan(
        self,
        conn: AsyncConnection,
        plan: Plan,
        *,
        desired: Mapping[int, Sequence[str]],
        twins: Mapping[str, Twin],
        squad_inbounds: Mapping[str, Collection[str]] | None = None,
        group_tags: Mapping[int, Collection[str]] | None = None,
        at: datetime | None = None,
    ) -> AppliedPlan:
        """Releases first (never cancelled), then restores, re-binds and new blocks (05 §2.1.10)."""
        at = at or now()
        out = AppliedPlan()
        for rel in plan.releases:
            if await self.release(conn, rel, at=at):
                out.released.append((rel.block_id, rel.subscription_id, rel.group_id, rel.release_reason))
        for res in plan.restores:
            subs = _substitutions(
                desired.get(res.subscription_id, ()), res.group_id, twins, squad_inbounds, group_tags
            )
            if res.mode == MODE_ENFORCE and not subs:
                out.unenforceable.append((res.subscription_id, res.group_id))
                continue
            done = (
                await conn.execute(
                    sa.update(lte_blocks)
                    .where(lte_blocks.c.id == res.block_id, lte_blocks.c.status == "releasing")
                    .values(status="active", release_reason=None, period_id=res.period_id, reason=res.reason)
                    .returning(lte_blocks.c.id)
                )
            ).first()
            if done is None:
                continue
            if res.mode == MODE_ENFORCE:
                await self._substitute(conn, res.subscription_id, res.block_id, subs, at=at, op="apply")
            out.restored.append(res.block_id)
        for reb in plan.rebinds:
            await conn.execute(
                sa.update(lte_blocks)
                .where(lte_blocks.c.id == reb.block_id, lte_blocks.c.status.in_(_LIVE))
                .values(period_id=reb.period_id)
            )
            out.rebound.append(reb.block_id)
        for cand in plan.blocks:
            placed = await self.block(
                conn,
                cand,
                desired=desired.get(cand.subscription_id, ()),
                twins=twins,
                squad_inbounds=squad_inbounds,
                group_tags=group_tags,
                at=at,
            )
            if placed is None:
                if cand.mode == MODE_ENFORCE:
                    out.unenforceable.append((cand.subscription_id, cand.group_id))
                continue
            out.placed.append(placed)
        return out

    async def block(
        self,
        conn: AsyncConnection,
        cand: BlockCandidate,
        *,
        desired: Sequence[str],
        twins: Mapping[str, Twin],
        squad_inbounds: Mapping[str, Collection[str]] | None = None,
        group_tags: Mapping[int, Collection[str]] | None = None,
        at: datetime | None = None,
    ) -> PlacedBlock | None:
        """Insert a live block (+ substitutions and the writer job for ``enforce``). ``None``: a live block
        exists already, or an enforce block cannot be applied (no healthy twin: never an empty set)."""
        at = at or now()
        subs: dict[str, str] = {}
        if cand.mode == MODE_ENFORCE:
            subs = _substitutions(desired, cand.group_id, twins, squad_inbounds, group_tags)
            if not subs:
                return None
        stmt = (
            pg_insert(lte_blocks)
            .values(
                subscription_id=cand.subscription_id,
                group_id=cand.group_id,
                period_id=cand.period_id,
                reason=cand.reason,
                mode=cand.mode,
                status="active",
                used_at_block=cand.used_bytes,
                limit_at_block=cand.limit_bytes,
                created_at=at,
                applied_at=at if cand.mode == MODE_SHADOW else None,
            )
            .on_conflict_do_nothing(
                index_elements=["subscription_id", "group_id"],
                index_where=sa.text("status IN ('active', 'releasing')"),
            )
            .returning(lte_blocks.c.id)
        )
        block_id = (await conn.execute(stmt)).scalar()
        if block_id is None:
            return None
        if cand.mode == MODE_ENFORCE:
            await self._substitute(conn, cand.subscription_id, int(block_id), subs, at=at, op="apply")
        return PlacedBlock(
            int(block_id),
            cand.subscription_id,
            cand.group_id,
            cand.period_id,
            cand.mode,
            cand.reason,
            cand.used_bytes,
            cand.limit_bytes,
        )

    async def release(self, conn: AsyncConnection, rel: Release, *, at: datetime | None = None) -> bool:
        """``active → releasing`` (enforce: substitution rows deleted + writer job + confirm) or straight to
        ``released`` (shadow). Returns ``False`` when the block is not live any more."""
        at = at or now()
        if rel.immediate:
            done = (
                await conn.execute(
                    sa.update(lte_blocks)
                    .where(lte_blocks.c.id == rel.block_id, lte_blocks.c.status.in_(_LIVE))
                    .values(status="released", release_reason=rel.release_reason, released_at=at)
                    .returning(lte_blocks.c.id)
                )
            ).first()
            return done is not None
        done = (
            await conn.execute(
                sa.update(lte_blocks)
                .where(lte_blocks.c.id == rel.block_id, lte_blocks.c.status == "active")
                .values(status="releasing", release_reason=rel.release_reason)
                .returning(lte_blocks.c.id)
            )
        ).first()
        if done is None:
            return False
        twins = (
            (
                await conn.execute(
                    sa.delete(panel_squad_substitutions)
                    .where(
                        panel_squad_substitutions.c.subscription_id == rel.subscription_id,
                        panel_squad_substitutions.c.owner_module == MODULE,
                        panel_squad_substitutions.c.source_ref == block_ref(rel.block_id),
                    )
                    .returning(panel_squad_substitutions.c.substitute_squad_uuid)
                )
            )
            .scalars()
            .all()
        )
        await enqueue_update(
            conn, rel.subscription_id, ["squads"], lane="background", caused_by=f"lte:release:{rel.block_id}"
        )
        await self._confirm(conn, rel.subscription_id, rel.block_id, "release", list(twins), at=at)
        return True

    async def _substitute(
        self,
        conn: AsyncConnection,
        sid: int,
        block_id: int,
        subs: Mapping[str, str],
        *,
        at: datetime,
        op: str,
    ) -> None:
        t = panel_squad_substitutions
        stmt = pg_insert(t).values(
            [
                {
                    "subscription_id": sid,
                    "base_squad_uuid": base,
                    "substitute_squad_uuid": twin,
                    "owner_module": MODULE,
                    "source_ref": block_ref(block_id),
                }
                for base, twin in sorted(subs.items())
            ]
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_panel_squad_substitutions_sub_base",
            set_={
                "substitute_squad_uuid": stmt.excluded.substitute_squad_uuid,
                "owner_module": stmt.excluded.owner_module,
                "source_ref": stmt.excluded.source_ref,
            },
        )
        await conn.execute(stmt)
        await enqueue_update(conn, sid, ["squads"], lane="background", caused_by=f"lte:block:{block_id}")
        await self._confirm(conn, sid, block_id, op, sorted(set(subs.values())), at=at)

    @staticmethod
    async def _confirm(
        conn: AsyncConnection, sid: int, block_id: int, op: str, twins: Sequence[str], *, at: datetime
    ) -> None:
        await enqueue(
            conn,
            CONFIRM_KIND,
            {"sub_id": sid, "block_id": block_id, "op": op, "twins": list(twins)},
            queue="panel",
            lane="background",
            ordering_key=ordering_key(sid),  # after the writer's panel.update of the same subscription
            max_attempts=10_000 if op == "release" else APPLY_ATTEMPTS,
            caused_by=f"lte:{op}:{block_id}",
            run_at=at,
        )

    # ------------------------------------------------------------------------------ release everything

    async def release_all(self, reason: str, *, due_only: bool = False, at: datetime | None = None) -> int:
        """Release every live enforce block (``due_only``: only blocks of periods whose planned end passed)
        in batches; shadow blocks are closed at once. Returns the number of blocks released."""
        at = at or now()
        total = 0
        last_id = 0
        while True:
            async with self._db.tx() as conn:
                q = sa.select(
                    lte_blocks.c.id, lte_blocks.c.subscription_id, lte_blocks.c.group_id, lte_blocks.c.mode
                ).where(lte_blocks.c.status == "active", lte_blocks.c.id > last_id)
                if due_only:
                    from svbg.ext.lte.tables import lte_periods

                    q = q.where(
                        sa.exists().where(
                            lte_periods.c.id == lte_blocks.c.period_id,
                            sa.or_(lte_periods.c.state == "closed", lte_periods.c.planned_end_at <= at),
                        )
                    )
                rows = (
                    await conn.execute(
                        q.order_by(lte_blocks.c.id).limit(RELEASE_BATCH).with_for_update(skip_locked=True)
                    )
                ).all()
                if not rows:
                    return total
                for r in rows:
                    rel = Release(
                        int(r.id), int(r.subscription_id), 0, int(r.group_id), reason, r.mode == MODE_SHADOW
                    )
                    if await self.release(conn, rel, at=at):
                        total += 1
                last_id = int(rows[-1].id)

    # ------------------------------------------------------------------------------------------ jobs

    async def confirm_job(self, job: Job, ctx: JobContext) -> None:
        """``lte.confirm``: did the panel take the block / the release? (runs after the writer's job)."""
        del ctx
        block_id = int(job.payload["block_id"])
        op = str(job.payload.get("op") or "")
        twins = {str(x) for x in job.payload.get("twins") or ()}
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        lte_blocks.c.status,
                        lte_blocks.c.subscription_id,
                        lte_blocks.c.applied_at,
                        subscriptions.c.link_state,
                        subscriptions.c.panel_squads,
                    )
                    .select_from(
                        lte_blocks.join(subscriptions, subscriptions.c.id == lte_blocks.c.subscription_id)
                    )
                    .where(lte_blocks.c.id == block_id)
                )
            ).first()
        if row is None:
            return
        sid = int(row.subscription_id)
        squads = {str(x) for x in (row.panel_squads or ())}
        linked = row.link_state == "linked"
        at = now()
        if op == "release":
            if row.status != "releasing":
                return  # restored meanwhile (or already released)
            if linked and squads & twins:
                raise RetryJob(CONFIRM_RETRY_S, "двойник ещё в панели")
            async with self._db.tx() as conn:
                done = (
                    await conn.execute(
                        sa.update(lte_blocks)
                        .where(lte_blocks.c.id == block_id, lte_blocks.c.status == "releasing")
                        .values(status="released", released_at=at)
                        .returning(lte_blocks.c.id)
                    )
                ).first()
                if done is not None and linked:
                    await enqueue_resend(conn, sid, block_id, at=at)
            return
        if row.status != "active" or row.applied_at is not None:
            return
        if linked and squads & twins:
            await self._mark_applied(block_id, at)
            return
        if job.attempts < APPLY_ATTEMPTS:
            raise RetryJob(UNLINKED_RETRY_S if not linked else CONFIRM_RETRY_S, "двойник ещё не в панели")
        await self._cancel(sid, block_id, at)
        await self._raise(
            f"lte:apply_failed:{sid}",
            _T["apply_failed_title"],
            _T["apply_failed_body"].format(sid=sid, n=APPLY_ATTEMPTS),
        )

    async def _mark_applied(self, block_id: int, at: datetime) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(lte_blocks)
                .where(lte_blocks.c.id == block_id, lte_blocks.c.applied_at.is_(None))
                .values(applied_at=at)
            )

    async def _cancel(self, sid: int, block_id: int, at: datetime) -> None:
        async with self._db.tx() as conn:
            done = (
                await conn.execute(
                    sa.update(lte_blocks)
                    .where(lte_blocks.c.id == block_id, lte_blocks.c.status == "active")
                    .values(status="cancelled", release_reason="apply_failed", released_at=at)
                    .returning(lte_blocks.c.id)
                )
            ).first()
            if done is None:
                return
            await conn.execute(
                sa.delete(panel_squad_substitutions).where(
                    panel_squad_substitutions.c.subscription_id == sid,
                    panel_squad_substitutions.c.source_ref == block_ref(block_id),
                )
            )
            await enqueue_update(conn, sid, ["squads"], lane="background", caused_by=f"lte:cancel:{block_id}")

    async def resend_job(self, job: Job, ctx: JobContext) -> None:
        """``lte.resend``: re-publish the user's squad event to the nodes (membership unchanged)."""
        del ctx
        sid = int(job.payload["sub_id"])
        block_id = job.payload.get("block_id")
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(
                        subscriptions.c.panel_user_id,
                        subscriptions.c.panel_squads,
                        subscriptions.c.link_state,
                    ).where(subscriptions.c.id == sid)
                )
            ).first()
        if row is None or row.link_state != "linked" or not row.panel_user_id or not row.panel_squads:
            return
        if self._api is None:
            raise RetryJob(UNREACHABLE_RETRY_S, "панель не подключена")
        squad = str(next(iter(row.panel_squads)))
        try:
            api = self._api()
            await api.transport.request(
                "POST",
                f"/internal-squads/{_path(squad)}/bulk-actions/add-many-users",
                json_body=msgspec.json.encode({"userIds": [int(row.panel_user_id)]}),
                idempotent=True,  # the membership does not change: a repeat is harmless
                scope="internal-squads:add-many-users",
                lane=Lane.BACKGROUND,
            )
        except PanelNotConfiguredError as err:
            raise RetryJob(UNREACHABLE_RETRY_S, "панель не подключена") from err
        except (PanelUnavailableError, WriteBlockedError) as err:
            raise RetryJob(max(err.retry_after or 0.0, 5.0), str(err)) from err
        except RemnawaveError as err:
            log.warning("lte resend for subscription %s failed: %s", sid, err.kind.value)
            if err.kind in (
                ErrorKind.AUTH,
                ErrorKind.FORBIDDEN_SCOPE,
                ErrorKind.VALIDATION,
                ErrorKind.NOT_FOUND,
            ):
                if err.kind is not ErrorKind.NOT_FOUND:
                    await self._raise("lte:resend_scopes", _T["scopes_title"], _T["scopes_body"])
                raise PermanentJobError(str(err)) from err
            if err.retry_after:
                raise RetryJob(err.retry_after, str(err)) from err
            raise
        at = now()
        async with self._db.tx() as conn:
            stmt = sa.update(lte_blocks).values(resend_done_at=at)
            if isinstance(block_id, int):
                stmt = stmt.where(lte_blocks.c.id == block_id)
            else:
                stmt = stmt.where(lte_blocks.c.subscription_id == sid, lte_blocks.c.status == "active")
            await conn.execute(stmt)

    async def raise_unenforceable(self, items: Iterable[tuple[int, int]]) -> None:
        for sid, _group in sorted(set(items)):
            await self._raise(
                f"lte:unenforceable:{sid}",
                _T["unenforceable_title"],
                _T["unenforceable_body"].format(sid=sid),
            )

    async def _raise(self, key: str, title: str, body: str) -> None:
        if self._attention is None:
            return
        try:
            await self._attention.raise_item(key, "warn", title, body)
        except Exception:  # attention is advisory
            log.exception("lte: could not raise %s", key)


async def enqueue_resend(
    conn: AsyncConnection, sid: int, block_id: int | None, *, at: datetime | None = None
) -> int | None:
    """Control re-send of the squad event in :data:`RESEND_DELAY` (one pending per subscription)."""
    return await enqueue(
        conn,
        RESEND_KIND,
        {"sub_id": int(sid), "block_id": block_id},
        queue="panel",
        lane="background",
        ordering_key=ordering_key(sid),
        dedup_key=f"lte:resend:{int(sid)}",
        run_at=(at or now()) + RESEND_DELAY,
        max_attempts=20,
        caused_by=f"lte:resend:{block_id}",
    )


def _path(value: str) -> str:
    from urllib.parse import quote

    if not value or len(value) > 128 or "/" in value or value in (".", ".."):
        raise PermanentJobError("bad squad uuid")
    return quote(value, safe="")


def manual_candidate(
    *,
    subscription_id: int,
    panel_user_id: int,
    group_id: int,
    period_id: int,
    used: int,
    limit: int,
    mode: str,
) -> BlockCandidate:
    """A manual block «Заблокировать до сброса» as a regular candidate (released at the next reset)."""
    return BlockCandidate(
        subscription_id=subscription_id,
        panel_user_id=panel_user_id,
        group_id=group_id,
        period_id=period_id,
        mode=mode,  # type: ignore[arg-type]
        reason=REASON_MANUAL,
        used_bytes=used,
        limit_bytes=limit,
    )
