"""Locations = the panel's internal squads with a display title and flag set in the bot (02 §3.3).

:func:`sync_locations` reads ``GET /internal-squads`` (outside any transaction), then in one transaction:

* new squads get a row (title empty → the panel name is shown until the admin sets a title);
* known squads get the fresh panel name and member count, and lose ``missing_since`` if they came back;
* squads gone from the panel keep their row with ``missing_since`` (titles are not lost on a panel hiccup);
* plans referring to a squad that is not in the panel any more are marked broken (``broken_reason``: the
  purchase is hidden) and the owner gets a «Требует внимания» item; when the squads are back (or the admin
  picked others) the mark and the item go away.

Guard: an empty list from a panel where squads were known is treated as «wrong panel / not ready» — nothing is
marked missing (02 §6 «предохранитель»).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.catalog.tables import locations, plans
from svbg.core.component import fix_screen
from svbg.remnawave.transport import Lane

if TYPE_CHECKING:
    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database
    from svbg.remnawave.models import InternalSquad

__all__ = [
    "BROKEN_PREFIX",
    "PLAN_SCREEN",
    "SquadSource",
    "SyncResult",
    "attention_key",
    "sync_locations",
]

log = logging.getLogger("svbg.catalog.locations")

#: ``broken_reason`` written by the sync starts with this (other reasons are never touched by it).
BROKEN_PREFIX: Final = "В панели нет сквадов: "
#: Screen of the plan card in the admin editor (``fix_action`` of the alert).
PLAN_SCREEN: Final = "pl"
SYNC_TIMEOUT: Final = 15.0

_T: Final = {
    "title": "Тариф «{name}» скрыт из продажи",
    "body": "В панели больше нет сквадов тарифа: {squads}. Покупка и продление скрыты, пока вы не выберете "
    "существующие сквады в тарифе (или сквады не вернутся в панель).",
}


class SquadSource(Protocol):
    """``RemnawaveApi`` satisfies it."""

    async def internal_squads(self, *, lane: Lane = ...) -> list[InternalSquad]: ...


@dataclass(frozen=True, slots=True)
class SyncResult:
    total: int = 0
    added: tuple[str, ...] = ()
    gone: tuple[str, ...] = ()  # newly missing in the panel
    returned: tuple[str, ...] = ()  # were missing, are back
    broken: tuple[int, ...] = ()  # plans newly marked broken
    repaired: tuple[int, ...] = ()  # plans whose mark was removed
    guarded: bool = False  # the panel returned nothing: no squad was marked missing

    @property
    def changed(self) -> bool:
        return bool(self.added or self.gone or self.returned or self.broken or self.repaired)


def attention_key(plan_id: int) -> str:
    return f"catalog.plan_broken:{plan_id}"


def _label(row: Mapping[str, Any] | None, uuid: str) -> str:
    if row is None:
        return uuid[:8]
    title = row["title"] if isinstance(row["title"], dict) else {}
    name = next((v for v in title.values() if v), "") or row["panel_name"] or uuid[:8]
    return f"{row['flag']} {name}" if row["flag"] else str(name)


async def sync_locations(
    db: Database,
    source: SquadSource,
    *,
    attention: AttentionService | None = None,
    lane: Lane = Lane.BACKGROUND,
    timeout: float = SYNC_TIMEOUT,  # noqa: ASYNC109 - bounds the panel call only, not the DB transaction
) -> SyncResult:
    """Refresh ``locations`` from the panel and mark/unmark broken plans. Raises the panel's errors."""
    async with asyncio.timeout(timeout):
        squads: Sequence[InternalSquad] = await source.internal_squads(lane=lane)
    seen = {s.uuid: s for s in squads}
    async with db.tx() as conn:
        rows = {
            r["squad_uuid"]: r
            for r in (await conn.execute(sa.select(locations).with_for_update())).mappings().all()
        }
        guarded = not seen and any(r["missing_since"] is None for r in rows.values())
        added = tuple(u for u in seen if u not in rows)
        returned = tuple(u for u in seen if u in rows and rows[u]["missing_since"] is not None)
        gone = (
            ()
            if guarded
            else tuple(u for u, r in rows.items() if u not in seen and r["missing_since"] is None)
        )
        for squad in squads:
            stmt = pg_insert(locations).values(
                squad_uuid=squad.uuid,
                panel_name=squad.name or "",
                members=squad.info.members_count,
                sort=squad.view_position * 10,
                synced_at=sa.func.now(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[locations.c.squad_uuid],
                set_={
                    "panel_name": stmt.excluded.panel_name,
                    "members": stmt.excluded.members,
                    "missing_since": None,
                    "synced_at": sa.func.now(),
                    "updated_at": sa.func.now(),
                },
            )
            await conn.execute(stmt)
        if gone:
            await conn.execute(
                sa.update(locations)
                .where(locations.c.squad_uuid.in_(gone))
                .values(missing_since=sa.func.now(), updated_at=sa.func.now())
            )
        broken: list[int] = []
        repaired: list[int] = []
        alerts: list[tuple[int, str, list[str]]] = []
        if not guarded:
            plan_rows = (
                await conn.execute(sa.select(plans.c.id, plans.c.name, plans.c.squads, plans.c.broken_reason))
            ).all()
            for p in plan_rows:
                missing = [s for s in (p.squads or []) if s not in seen]
                ours = p.broken_reason is not None and p.broken_reason.startswith(BROKEN_PREFIX)
                if missing:
                    labels = [_label(rows.get(s), s) for s in missing]
                    reason = BROKEN_PREFIX + ", ".join(labels)
                    if p.broken_reason is None or (ours and p.broken_reason != reason):
                        await _mark(conn, p.id, reason)
                        if p.broken_reason is None:
                            broken.append(p.id)
                            name = next((v for v in (p.name or {}).values() if v), f"#{p.id}")
                            alerts.append((p.id, str(name), labels))
                elif ours:
                    await _mark(conn, p.id, None)
                    repaired.append(p.id)
    result = SyncResult(len(seen), added, gone, returned, tuple(broken), tuple(repaired), guarded)
    if guarded:
        log.warning("locations: the panel returned no internal squads; nothing marked missing")
    await _alerts(attention, alerts, repaired)
    return result


async def _mark(conn: Any, plan_id: int, reason: str | None) -> None:
    await conn.execute(
        sa.update(plans)
        .where(plans.c.id == plan_id)
        .values(broken_reason=reason, version=plans.c.version + 1, updated_at=sa.func.now())
    )


async def _alerts(
    attention: AttentionService | None,
    broken: Sequence[tuple[int, str, list[str]]],
    repaired: Sequence[int],
) -> None:
    if attention is None:
        return
    for plan_id, name, labels in broken:
        try:
            await attention.raise_item(
                attention_key(plan_id),
                "error",
                _T["title"].format(name=name),
                _T["body"].format(squads=", ".join(labels)),
                fix_action=fix_screen(PLAN_SCREEN, str(plan_id)),
            )
        except Exception:  # an alert must never undo the sync
            log.exception("locations: cannot raise the broken plan alert")
    for plan_id in repaired:
        try:
            await attention.resolve(attention_key(plan_id))
        except Exception:
            log.exception("locations: cannot resolve the broken plan alert")
