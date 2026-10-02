"""LTE quotas (WLQ) → the ``lte_*`` tables of the LTE module (06 §2.9, 05 §2.1.17).

Only the **live** state of the snapshot is carried over; the link is ``wlq_subjects.panel_user_id`` ↔
``subscriptions.panel_user_id`` (one subscription = one panel account; panel-only subjects land on the
unclaimed subscriptions of the subscriptions stage):

* ``wlq_groups`` + ``wlq_group_limits`` → ``lte_groups`` (limit triple: no row → ``has_*=false``, ``NULL`` →
  unlimited, ``0`` → unavailable; an active group without a default limit is imported ``suspended``),
  ``wlq_group_nodes`` → ``lte_group_nodes`` (``pending_add`` → a draft span ``counted_to = counted_from``),
  ``wlq_squads`` ``kind='twin'`` → ``lte_twins`` + the core map ``panel_squad_twins`` (owner ``lte``; the
  writer's ``reverse`` needs it even for a twin without a group), ``kind='group'`` →
  ``lte_groups.squad_uuid``;
* ``wlq_anchors`` → ``lte_anchors``; ``wlq_periods`` ``open|deferred`` + ``wlq_period_usage`` →
  ``lte_periods`` + ``lte_period_usage`` (``estimated_bytes`` includes the gap estimate);
* ``wlq_user_limits`` (live) → ``lte_overrides(kind='limit')``; ``wlq_exemptions`` (live) →
  ``lte_overrides(kind='exempt', exempt_kind)`` (``owner_personal`` → ``owner``); a ``launch_trial`` whose
  subject already has a ``paid`` billing event (processed or not) is carried over **revoked**
  (``converted_to_paid``, 05 §2.1.17) — the rest are revoked by the module on the first payment after T0;
  ``wlq_period_overrides`` → ``kind='no_block'``;
* ``wlq_topups`` ``active`` → ``lte_credits(source='import')``;
* ``wlq_blocks`` ``pending_apply|active`` with ``mode='enforce'`` → ``lte_blocks(status='active')`` + the
  substitution «base → twin» (``panel_squad_substitutions``, owner ``lte``, ``source_ref='lte:block:<id>'``)
  for every desired base squad of the subscription that has a twin of the block's group — the writer then
  keeps the twin in the panel (С6 = 0 operations, С7);
* ``wlq_notifications`` ``sent`` (``warn`` / ``exhausted``) of the imported periods → ``notification_log``
  (anchors of :mod:`svbg.ext.lte.notify`) so they are not sent again;
* ``wlq_counters`` of D−1/D (Moscow dates) on group nodes + ``wlq_node_status`` read marks →
  ``lte_counters`` / ``lte_node_state`` (the accounting continues without a new baseline).

Re-runs (shadow) follow the source: imported rows are found through ``legacy_id_map`` (a placeholder
subscription dropped by the subscriptions stage takes its rows with it — the row, not the map, decides);
an imported block / period / override / credit that is no longer live in the source is released / closed /
revoked / expired. Rows the stand created itself are never touched (a clash is reported).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.ext.lte.model import msk_date
from svbg.ext.lte.tables import (
    lte_anchors,
    lte_blocks,
    lte_counters,
    lte_credits,
    lte_group_nodes,
    lte_groups,
    lte_node_state,
    lte_overrides,
    lte_period_usage,
    lte_periods,
    lte_twins,
)
from svbg.services.notify_user import notification_log
from svbg.subscriptions.tables import panel_squad_substitutions, panel_squad_twins, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.importers.bedolaga.plan import Ctx

__all__ = [
    "MODULE",
    "TABLES",
    "block_ref",
    "check",
    "missing_tables",
    "run",
    "subs_by_panel",
    "upsert_mapped",
]

MODULE: Final = "lte"
TABLES: Final = (
    "lte_groups",
    "lte_group_nodes",
    "lte_twins",
    "lte_anchors",
    "lte_periods",
    "lte_period_usage",
    "lte_overrides",
    "lte_blocks",
    "lte_credits",
    "lte_counters",
    "lte_node_state",
    "panel_squad_substitutions",
    "panel_squad_twins",
)
_EXEMPT_KIND: Final = {"owner_personal": "owner", "launch_trial": "launch_trial", "manual": "manual"}
_BLOCK_REASONS: Final = ("quota", "unavailable", "manual")
_NOTIFY_KIND: Final = {"warn": "lte_warn", "exhausted": "lte_exhausted"}
_GROUP_STATES: Final = ("draft", "active", "suspended")
_SLUG_OK: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def block_ref(block_id: int) -> str:
    """``source_ref`` of a block's substitution rows (same as ``svbg.ext.lte.enforce.block_ref``)."""
    return f"lte:block:{int(block_id)}"


# ------------------------------------------------------------------------------------------- helpers


async def missing_tables(conn: AsyncConnection, names: Iterable[str]) -> list[str]:
    """Target tables of ``names`` that do not exist (the module's migration has not run)."""
    out = []
    for name in names:
        if await conn.scalar(sa.text("SELECT to_regclass(:n) IS NULL"), {"n": f"public.{name}"}):
            out.append(name)
    return out


async def subs_by_panel(ctx: Ctx, panel_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Target subscriptions linked to these panel accounts: ``panel_user_id → row``."""
    ids = sorted({int(i) for i in panel_ids})
    if not ids:
        return {}
    s = subscriptions
    rows = (
        await ctx.conn.execute(
            sa.select(
                s.c.id,
                s.c.user_id,
                s.c.panel_user_id,
                s.c.desired_squads,
                s.c.is_trial,
                s.c.hold_kind,
                s.c.disabled_reason,
            ).where(s.c.panel_user_id.in_(ids))
        )
    ).mappings()
    return {int(r["panel_user_id"]): dict(r) for r in rows}


async def upsert_mapped(
    ctx: Ctx, entity: str, table: sa.Table, old_id: Any, values: Mapping[str, Any]
) -> tuple[int, bool]:
    """Update the row an earlier run created for ``old_id`` (if it still exists) or insert a new one.
    Returns ``(id, inserted)``."""
    prev = (await ctx.mapped(entity)).get(str(old_id))
    if prev is not None:
        new_id = int(prev[0])
        res = await ctx.conn.execute(sa.update(table).where(table.c.id == new_id).values(**values))
        if res.rowcount:
            return new_id, False
    new_id = int(await ctx.conn.scalar(sa.insert(table).values(**values).returning(table.c.id)))
    await ctx.remember(entity, [(old_id, new_id, {})])
    return new_id, True


async def stale_ids(ctx: Ctx, entity: str, live: Iterable[Any]) -> list[int]:
    """Target ids an earlier run mapped for ``entity`` whose source rows are no longer live."""
    keep = {str(x) for x in live}
    return sorted(int(new) for old, (new, _d) in (await ctx.mapped(entity)).items() if old not in keep)


def _lower(value: Any) -> str | None:
    return str(value).lower() if value is not None else None


def _pairs(rows: Iterable[Mapping[str, Any]], key: str) -> dict[int, list[Mapping[str, Any]]]:
    out: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            out[int(r[key])].append(r)
    return out


# ---------------------------------------------------------------------------------------------- run


async def _source_has_state(ctx: Ctx) -> bool:
    for table in ("wlq_groups", "wlq_squads", "wlq_blocks", "wlq_periods", "wlq_exemptions"):
        if await ctx.src.has(table) and int(
            await ctx.src.scalar(f"SELECT count(*) FROM public.{table}") or 0
        ):
            return True
    return False


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    if not await ctx.src.has("wlq_subjects"):
        rep.set("lte", "source", 0)
        return
    missing = await missing_tables(ctx.conn, TABLES)
    if missing:
        if await _source_has_state(ctx):
            rep.issue("module_missing", module=MODULE, state=[f"нет таблиц в боте: {', '.join(missing)}"])
        return
    groups = await _groups(ctx)
    twins = await _twins(ctx, groups)
    subjects = {
        int(r["id"]): r
        for r in await ctx.src.rows(
            "wlq_subjects",
            ["id", "panel_user_id", "subscription_id", "kind", "state"],
            where="state = 'active'",
        )
    }
    subs = await subs_by_panel(ctx, (r["panel_user_id"] for r in subjects.values()))
    sub_of: dict[int, dict[str, Any]] = {}
    for sid, subj in subjects.items():
        row = subs.get(int(subj["panel_user_id"]))
        if row is not None:
            sub_of[sid] = row
    rep.set("lte", "subjects", len(subjects))
    rep.set("lte", "subjects_linked", len(sub_of))
    await _anchors(ctx, sub_of)
    periods = await _periods(ctx, sub_of, groups)
    await _overrides(ctx, sub_of, groups, periods)
    await _credits(ctx, sub_of, groups, periods)
    blocks = await _blocks(ctx, sub_of, groups, twins, periods)
    await _notifications(ctx, sub_of, groups, periods, blocks)
    await _counters(ctx, groups)


async def _groups(ctx: Ctx) -> dict[int, int]:
    """``wlq_groups.id → lte_groups.id`` (deleted groups are left out)."""
    rep = ctx.report
    rows = await ctx.src.rows(
        "wlq_groups",
        [
            "id",
            "slug",
            "name",
            "name_en",
            "state",
            "enforce_enabled",
            "margin_bytes",
            "margin_percent",
            "squad_uuid",
            "sort",
            "created_at",
        ],
    )
    limits: dict[int, dict[str, int | None]] = defaultdict(dict)
    for r in await ctx.src.rows("wlq_group_limits", ["group_id", "scope", "limit_bytes"], order=None):
        limits[int(r["group_id"])][str(r["scope"])] = r["limit_bytes"]
    group_squads = {
        int(r["group_id"]): str(r["panel_uuid"])
        for r in await ctx.src.rows("wlq_squads", ["id", "kind", "group_id", "panel_uuid"])
        if r["kind"] == "group" and r["group_id"] is not None and r["panel_uuid"]
    }
    out: dict[int, int] = {}
    for r in rows:
        gid = int(r["id"])
        state = str(r["state"] or "draft")
        if state not in _GROUP_STATES:
            rep.inc("lte", "groups_skipped")
            continue
        slug = str(r["slug"] or "").strip().lower()
        if not slug or not (slug[0].isalnum() and set(slug) <= _SLUG_OK) or len(slug) > 32:
            rep.issue("lte_group_slug_invalid", group_id=gid, slug=r["slug"])
            continue
        lim = limits.get(gid, {})
        if state == "active" and "default" not in lim:
            rep.issue("lte_group_without_default", group_id=gid, slug=slug)
            state = "suspended"
        name = {"ru": str(r["name"] or slug)}
        if r["name_en"]:
            name["en"] = str(r["name_en"])
        values = {
            "slug": slug,
            "name": name,
            "state": state,
            "enforce": bool(r["enforce_enabled"]),
            "margin_bytes": max(0, int(r["margin_bytes"] or 0)),
            "margin_pct": min(100, max(0, int(r["margin_percent"] or 0))),
            "has_default": "default" in lim,
            "limit_default_bytes": lim.get("default"),
            "has_trial": "trial" in lim,
            "limit_trial_bytes": lim.get("trial"),
            "squad_uuid": group_squads.get(gid) or _lower(r["squad_uuid"]),
            "sort": int(r["sort"] or 0),
        }
        stmt = pg_insert(lte_groups).values(**values, created_at=r["created_at"] or ctx.t0)
        stmt = stmt.on_conflict_do_update(
            index_elements=[lte_groups.c.slug],
            set_={k: stmt.excluded[k] for k in values if k != "slug"} | {"updated_at": sa.func.now()},
        )
        new_id = int(await ctx.conn.scalar(stmt.returning(lte_groups.c.id)))
        out[gid] = new_id
        rep.inc("lte", "groups")
    await ctx.remember("lte_group", [(old, new, {}) for old, new in out.items()])

    nodes = []
    for r in await ctx.src.rows(
        "wlq_group_nodes",
        ["id", "group_id", "node_uuid", "state", "counted_from", "counted_to", "created_at"],
    ):
        gid = out.get(int(r["group_id"]))
        if gid is None or r["state"] == "orphaned":
            continue
        start = r["counted_from"]
        end = r["counted_to"]
        if start is None:  # pending_add: a draft span, never "since the beginning of time"
            start = r["created_at"] or ctx.t0
            end = start
        nodes.append(
            {"group_id": gid, "node_uuid": _lower(r["node_uuid"]), "counted_from": start, "counted_to": end}
        )
    for row in nodes:
        stmt = pg_insert(lte_group_nodes).values(**row)
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                constraint="pk_lte_group_nodes", set_={"counted_to": stmt.excluded.counted_to}
            )
        )
    rep.set("lte", "group_nodes", len(nodes))
    return out


async def _twins(ctx: Ctx, groups: Mapping[int, int]) -> dict[str, tuple[str, int | None]]:
    """``base → (twin, lte group id)``; mirrored into ``lte_twins`` and the core ``panel_squad_twins``."""
    rep = ctx.report
    only = next(iter(groups.values())) if len(groups) == 1 else None
    out: dict[str, tuple[str, int | None]] = {}
    for r in await ctx.src.rows("wlq_squads", ["id", "kind", "base_squad_uuid", "group_id", "panel_uuid"]):
        if r["kind"] != "twin" or not r["panel_uuid"] or not r["base_squad_uuid"]:
            continue
        base, twin = _lower(r["base_squad_uuid"]), _lower(r["panel_uuid"])
        assert base is not None and twin is not None
        gid = groups.get(int(r["group_id"])) if r["group_id"] is not None else only
        out[base] = (twin, gid)
        stmt = pg_insert(panel_squad_twins).values(
            substitute_squad_uuid=twin, base_squad_uuid=base, owner_module=MODULE
        )
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[panel_squad_twins.c.substitute_squad_uuid],
                set_={"base_squad_uuid": stmt.excluded.base_squad_uuid},
                where=panel_squad_twins.c.owner_module == MODULE,
            )
        )
        if gid is None:
            rep.issue("lte_twin_without_group", base=base, twin=twin)
            continue
        stmt = pg_insert(lte_twins).values(base_squad_uuid=base, group_id=gid, twin_squad_uuid=twin)
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[lte_twins.c.base_squad_uuid],
                set_={"group_id": stmt.excluded.group_id, "twin_squad_uuid": stmt.excluded.twin_squad_uuid},
            )
        )
    rep.set("lte", "twins", len(out))
    return out


async def _anchors(ctx: Ctx, sub_of: Mapping[int, Mapping[str, Any]]) -> None:
    rows = []
    for r in await ctx.src.rows(
        "wlq_anchors",
        [
            "subject_id",
            "anchor_at",
            "anchor_kind",
            "anchor_source",
            "streak_started_at",
            "series_state",
            "series_closed_at",
            "coverage_end",
            "is_trial",
            "review_state",
            "review_reason",
            "version",
        ],
        order="subject_id",
    ):
        sub = sub_of.get(int(r["subject_id"]))
        if sub is None:
            continue
        closed = r["series_state"] == "closed"
        review = None
        if r["review_state"] not in (None, "none"):
            review = str(r["review_reason"] or r["review_state"])
        rows.append(
            {
                "subscription_id": int(sub["id"]),
                "anchor_at": r["anchor_at"],
                "anchor_kind": str(r["anchor_kind"]),
                "anchor_source": str(r["anchor_source"] or "")[:200],
                "series_open": not closed,
                "series_started_at": r["streak_started_at"],
                "series_closed_at": (r["series_closed_at"] or ctx.t0) if closed else None,
                "coverage_end": r["coverage_end"],
                "is_trial": bool(r["is_trial"]),
                "review_reason": review,
                "version": max(1, int(r["version"] or 1)),
            }
        )
    for row in rows:
        stmt = pg_insert(lte_anchors).values(**row)
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[lte_anchors.c.subscription_id],
                set_={k: stmt.excluded[k] for k in row if k != "subscription_id"}
                | {"updated_at": sa.func.now()},
            )
        )
    ctx.report.set("lte", "anchors", len(rows))


async def _periods(
    ctx: Ctx, sub_of: Mapping[int, Mapping[str, Any]], groups: Mapping[int, int]
) -> dict[int, int]:
    """Live periods: ``wlq_periods.id → lte_periods.id`` (+ usage). Stale imported ones are closed."""
    rep = ctx.report
    rows = await ctx.src.rows(
        "wlq_periods",
        [
            "id",
            "subject_id",
            "anchor_at",
            "period_index",
            "starts_at",
            "planned_end_at",
            "state",
            "is_trial",
            "start_estimated",
        ],
        where="state IN ('open', 'deferred')",
    )
    live_ids = [int(r["id"]) for r in rows]
    # Close the imported periods that are no longer live first: one live period per subscription.
    for pid in await stale_ids(ctx, "lte_period", live_ids):
        await ctx.conn.execute(
            sa.update(lte_periods)
            .where(lte_periods.c.id == pid, lte_periods.c.state != "closed")
            .values(
                state="closed", ended_at=sa.func.greatest(lte_periods.c.starts_at, ctx.t0), end_cause="import"
            )
        )
    out: dict[int, int] = {}
    mapped = await ctx.mapped("lte_period")
    for r in rows:
        sub = sub_of.get(int(r["subject_id"]))
        if sub is None:
            rep.inc("lte", "periods_unlinked")
            continue
        if r["planned_end_at"] <= r["starts_at"]:
            rep.issue("lte_period_invalid", period_id=r["id"])
            continue
        sid = int(sub["id"])
        mine = mapped.get(str(r["id"]))
        live = await ctx.conn.scalar(
            sa.select(lte_periods.c.id).where(
                lte_periods.c.subscription_id == sid, lte_periods.c.state != "closed"
            )
        )
        if live is not None and (mine is None or int(mine[0]) != int(live)):
            if str(int(live)) in {v[0] for v in mapped.values()}:  # another imported period of this sub
                await ctx.conn.execute(
                    sa.update(lte_periods)
                    .where(lte_periods.c.id == live)
                    .values(
                        state="closed",
                        ended_at=sa.func.greatest(lte_periods.c.starts_at, ctx.t0),
                        end_cause="import",
                    )
                )
            else:
                rep.issue("lte_period_exists", subscription_id=sid, period_id=r["id"])
                continue
        values = {
            "subscription_id": sid,
            "anchor_at": r["anchor_at"],
            "idx": max(0, int(r["period_index"] or 0)),
            "starts_at": r["starts_at"],
            "planned_end_at": r["planned_end_at"],
            "ended_at": None,
            "end_cause": None,
            "state": str(r["state"]),
            "is_trial": bool(r["is_trial"]),
            "start_estimated": bool(r["start_estimated"]),
        }
        new_id, _ = await upsert_mapped(ctx, "lte_period", lte_periods, r["id"], values)
        out[int(r["id"])] = new_id
    rep.set("lte", "periods", len(out))

    usage = 0
    for r in await ctx.src.rows(
        "wlq_period_usage",
        [
            "period_id",
            "group_id",
            "used_bytes",
            "after_block_bytes",
            "estimated_bytes",
            "gap_estimated_bytes",
            "last_delta_at",
        ],
        order=None,
    ):
        pid, gid = out.get(int(r["period_id"])), groups.get(int(r["group_id"]))
        if pid is None or gid is None:
            continue
        values = {
            "used_bytes": max(0, int(r["used_bytes"] or 0)),
            "after_block_bytes": max(0, int(r["after_block_bytes"] or 0)),
            "estimated_bytes": max(0, int(r["estimated_bytes"] or 0) + int(r["gap_estimated_bytes"] or 0)),
            "last_delta_at": r["last_delta_at"],
        }
        stmt = pg_insert(lte_period_usage).values(period_id=pid, group_id=gid, **values)
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                constraint="pk_lte_period_usage", set_={k: stmt.excluded[k] for k in values}
            )
        )
        usage += 1
    rep.set("lte", "period_usage", usage)
    return out


async def _paid_subjects(ctx: Ctx, subjects: Mapping[int, Mapping[str, Any]]) -> set[int]:
    """Subjects with a ``paid`` billing event (processed or not): their ``launch_trial`` is converted."""
    if not await ctx.src.has("wlq_billing_events"):
        return set()
    rows = await ctx.src.rows(
        "wlq_billing_events", ["id", "panel_user_id", "subscription_id", "kind"], where="kind = 'paid'"
    )
    panels = {int(r["panel_user_id"]) for r in rows if r["panel_user_id"] is not None}
    subs = {int(r["subscription_id"]) for r in rows if r["subscription_id"] is not None}
    return {
        sid
        for sid, s in subjects.items()
        if int(s["panel_user_id"]) in panels
        or (s["subscription_id"] is not None and int(s["subscription_id"]) in subs)
    }


async def _overrides(
    ctx: Ctx,
    sub_of: Mapping[int, Mapping[str, Any]],
    groups: Mapping[int, int],
    periods: Mapping[int, int],
) -> None:
    rep = ctx.report
    t0 = ctx.t0
    live: list[tuple[str, dict[str, Any]]] = []
    for r in await ctx.src.rows(
        "wlq_user_limits",
        [
            "id",
            "subject_id",
            "group_id",
            "limit_bytes",
            "applies_to",
            "valid_until",
            "reason",
            "created_at",
            "revoked_at",
        ],
    ):
        if r["revoked_at"] is not None or (r["valid_until"] is not None and r["valid_until"] <= t0):
            continue
        sub, gid = sub_of.get(int(r["subject_id"])), groups.get(int(r["group_id"]))
        if sub is None or gid is None:
            rep.issue("lte_override_unlinked", kind="limit", source_id=r["id"])
            continue
        live.append(
            (
                f"limit:{r['id']}",
                {
                    "subscription_id": int(sub["id"]),
                    "group_id": gid,
                    "kind": "limit",
                    "limit_bytes": r["limit_bytes"],
                    "applies_to": str(r["applies_to"] or "all"),
                    "valid_until": r["valid_until"],
                    "reason": str(r["reason"] or ""),
                    "created_at": r["created_at"] or t0,
                },
            )
        )
    src_subjects = {
        int(r["id"]): r
        for r in await ctx.src.rows("wlq_subjects", ["id", "panel_user_id", "subscription_id"])
    }
    paid = await _paid_subjects(ctx, src_subjects)
    exempt_seen: set[int] = set()
    for r in await ctx.src.rows(
        "wlq_exemptions", ["id", "subject_id", "kind", "reason", "created_at", "revoked_at"]
    ):
        if r["revoked_at"] is not None:
            continue
        sub = sub_of.get(int(r["subject_id"]))
        kind = _EXEMPT_KIND.get(str(r["kind"]))
        if sub is None or kind is None:
            rep.issue("lte_override_unlinked", kind="exempt", source_id=r["id"])
            continue
        if int(sub["id"]) in exempt_seen:
            rep.issue("lte_exempt_duplicate", subscription_id=sub["id"], source_id=r["id"])
            continue
        values: dict[str, Any] = {
            "subscription_id": int(sub["id"]),
            "group_id": None,
            "kind": "exempt",
            "exempt_kind": kind,
            "reason": str(r["reason"] or ""),
            "created_at": r["created_at"] or t0,
            "revoked_at": None,
            "revoke_reason": None,
        }
        if kind == "launch_trial" and int(r["subject_id"]) in paid:
            values.update(revoked_at=t0, revoke_reason="converted_to_paid")
            rep.inc("lte", "launch_trial_converted")
        else:
            exempt_seen.add(int(sub["id"]))
        live.append((f"exempt:{r['id']}", values))
    for r in await ctx.src.rows(
        "wlq_period_overrides", ["period_id", "group_id", "no_block", "reason", "created_at"], order=None
    ):
        pid, gid = periods.get(int(r["period_id"])), groups.get(int(r["group_id"]))
        if not r["no_block"] or pid is None or gid is None:
            continue
        sid = await ctx.conn.scalar(sa.select(lte_periods.c.subscription_id).where(lte_periods.c.id == pid))
        live.append(
            (
                f"no_block:{r['period_id']}:{r['group_id']}",
                {
                    "subscription_id": int(sid),
                    "group_id": gid,
                    "kind": "no_block",
                    "period_id": pid,
                    "reason": str(r["reason"] or ""),
                    "created_at": r["created_at"] or t0,
                },
            )
        )
    for oid in await stale_ids(ctx, "lte_override", [k for k, _ in live]):
        await ctx.conn.execute(
            sa.update(lte_overrides)
            .where(lte_overrides.c.id == oid, lte_overrides.c.revoked_at.is_(None))
            .values(revoked_at=t0, revoke_reason="import")
        )
    for key, values in live:
        await upsert_mapped(ctx, "lte_override", lte_overrides, key, values)
        rep.inc("lte", f"overrides_{values['kind']}")


async def _credits(
    ctx: Ctx, sub_of: Mapping[int, Mapping[str, Any]], groups: Mapping[int, int], periods: Mapping[int, int]
) -> None:
    rep = ctx.report
    rows = await ctx.src.rows(
        "wlq_topups",
        ["id", "subject_id", "group_id", "period_id", "bytes", "price_kopeks", "created_at"],
        where="status = 'active'",
    )
    live = []
    for r in rows:
        sub, gid, pid = (
            sub_of.get(int(r["subject_id"])),
            groups.get(int(r["group_id"])),
            periods.get(int(r["period_id"])),
        )
        if sub is None or gid is None or pid is None or int(r["bytes"] or 0) <= 0:
            rep.issue("lte_topup_unlinked", topup_id=r["id"])
            continue
        live.append(int(r["id"]))
        await upsert_mapped(
            ctx,
            "lte_credit",
            lte_credits,
            r["id"],
            {
                "subscription_id": int(sub["id"]),
                "group_id": gid,
                "period_id": pid,
                "bytes": int(r["bytes"]),
                "source": "import",
                "status": "active",
                "expired_at": None,
                "amount_minor": max(0, int(r["price_kopeks"] or 0)),
                "created_at": r["created_at"] or ctx.t0,
            },
        )
    for cid in await stale_ids(ctx, "lte_credit", live):
        await ctx.conn.execute(
            sa.update(lte_credits)
            .where(lte_credits.c.id == cid, lte_credits.c.status == "active")
            .values(status="expired", expired_at=ctx.t0)
        )
    rep.set("lte", "credits", len(live))


async def _blocks(
    ctx: Ctx,
    sub_of: Mapping[int, Mapping[str, Any]],
    groups: Mapping[int, int],
    twins: Mapping[str, tuple[str, int | None]],
    periods: Mapping[int, int],
) -> dict[tuple[int, int], int]:
    """Live enforce blocks → ``lte_blocks`` + substitutions; ``(wlq period, wlq group) → block id``."""
    rep = ctx.report
    rows = await ctx.src.rows(
        "wlq_blocks",
        [
            "id",
            "subject_id",
            "panel_user_id",
            "group_id",
            "period_id",
            "mode",
            "status",
            "reason",
            "used_bytes_at_block",
            "limit_bytes_at_block",
            "created_at",
            "applied_at",
        ],
        where="status IN ('pending_apply', 'active') AND mode = 'enforce'",
    )
    live = [int(r["id"]) for r in rows]
    for bid in await stale_ids(ctx, "lte_block", live):
        res = await ctx.conn.execute(
            sa.update(lte_blocks)
            .where(lte_blocks.c.id == bid, lte_blocks.c.status.in_(("active", "releasing")))
            .values(status="released", release_reason="import", released_at=ctx.t0)
            .returning(lte_blocks.c.subscription_id)
        )
        for (sid,) in res.all():
            await ctx.conn.execute(
                sa.delete(panel_squad_substitutions).where(
                    panel_squad_substitutions.c.subscription_id == sid,
                    panel_squad_substitutions.c.source_ref == block_ref(bid),
                )
            )
            rep.inc("lte", "blocks_released")
    twin_set = {t for t, _g in twins.values()}
    out: dict[tuple[int, int], int] = {}
    blocked_subs: set[int] = set()
    for r in rows:
        sub = sub_of.get(int(r["subject_id"]))
        gid = groups.get(int(r["group_id"]))
        if sub is None or gid is None:
            rep.issue("lte_block_unlinked", block_id=r["id"], panel_user_id=r["panel_user_id"])
            continue
        sid = int(sub["id"])
        clash = await ctx.conn.scalar(
            sa.select(lte_blocks.c.id).where(
                lte_blocks.c.subscription_id == sid,
                lte_blocks.c.group_id == gid,
                lte_blocks.c.status.in_(("active", "releasing")),
            )
        )
        mine = (await ctx.mapped("lte_block")).get(str(r["id"]))
        if clash is not None and (mine is None or int(mine[0]) != int(clash)):
            rep.issue("lte_block_exists", subscription_id=sid, block_id=r["id"])
            continue
        reason = str(r["reason"]) if r["reason"] in _BLOCK_REASONS else "quota"
        values = {
            "subscription_id": sid,
            "group_id": gid,
            "period_id": periods.get(int(r["period_id"])) if r["period_id"] is not None else None,
            "reason": reason,
            "mode": "enforce",
            "status": "active",
            "used_at_block": r["used_bytes_at_block"],
            "limit_at_block": r["limit_bytes_at_block"],
            "created_at": r["created_at"] or ctx.t0,
            "applied_at": r["applied_at"],
            "release_reason": None,
            "released_at": None,
        }
        bid, _ = await upsert_mapped(ctx, "lte_block", lte_blocks, r["id"], values)
        out[(int(r["period_id"]), int(r["group_id"]))] = bid
        blocked_subs.add(sid)
        subst: dict[str, str] = {}
        for base in sub["desired_squads"] or []:
            pair = twins.get(str(base).lower())
            if pair is not None and pair[1] == gid:
                subst[str(base)] = pair[0]
        if not subst:
            rep.issue("lte_block_no_twin", subscription_id=sid, block_id=r["id"])
        else:
            stmt = pg_insert(panel_squad_substitutions).values(
                [
                    {
                        "subscription_id": sid,
                        "base_squad_uuid": base,
                        "substitute_squad_uuid": twin,
                        "owner_module": MODULE,
                        "source_ref": block_ref(bid),
                    }
                    for base, twin in sorted(subst.items())
                ]
            )
            await ctx.conn.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_panel_squad_substitutions_sub_base",
                    set_={
                        "substitute_squad_uuid": stmt.excluded.substitute_squad_uuid,
                        "owner_module": stmt.excluded.owner_module,
                        "source_ref": stmt.excluded.source_ref,
                    },
                )
            )
        if ctx.panel is not None:
            pu = ctx.panel.by_id.get(int(r["panel_user_id"]))
            if pu is None or not set(s.lower() for s in pu.squad_uuids) & twin_set:
                rep.issue("lte_block_not_in_panel", subscription_id=sid, panel_user_id=r["panel_user_id"])
        rep.inc("lte", "blocks")
    # A twin in the panel without a live block: the writer would put the base squad back (С6 shows it).
    if ctx.panel is not None and twin_set:
        linked = await subs_by_panel(ctx, ctx.panel.by_id)
        for pid, pu in sorted(ctx.panel.by_id.items()):
            row = linked.get(pid)
            if row is None or int(row["id"]) in blocked_subs:
                continue
            if set(s.lower() for s in pu.squad_uuids) & twin_set:
                rep.issue("lte_twin_without_block", subscription_id=row["id"], panel_user_id=pid)
    return out


async def _notifications(
    ctx: Ctx,
    sub_of: Mapping[int, Mapping[str, Any]],
    groups: Mapping[int, int],
    periods: Mapping[int, int],
    blocks: Mapping[tuple[int, int], int],
) -> None:
    rows = await ctx.src.rows(
        "wlq_notifications",
        ["id", "subject_id", "group_id", "period_id", "kind", "state", "sent_at", "created_at"],
        where="state = 'sent'",
    )
    values = []
    for r in rows:
        kind = _NOTIFY_KIND.get(str(r["kind"]))
        sub = sub_of.get(int(r["subject_id"]))
        if kind is None or sub is None or r["period_id"] is None or r["group_id"] is None:
            continue
        pid, gid = periods.get(int(r["period_id"])), groups.get(int(r["group_id"]))
        if pid is None or gid is None:
            continue  # a past period: nothing can repeat
        anchor = f"{pid}:{gid}"
        if kind == "lte_exhausted":
            anchor += f":{blocks.get((int(r['period_id']), int(r['group_id'])), 0)}"
        at = r["sent_at"] or r["created_at"] or ctx.t0
        values.append(
            {
                "target": f"sub:{int(sub['id'])}",
                "kind": kind,
                "anchor": anchor,
                "user_id": sub["user_id"],
                "subscription_id": int(sub["id"]),
                "payload": {"import": "bedolaga", "source_id": int(r["id"])},
                "status": "sent",
                "created_at": at,
                "sent_at": at,
            }
        )
    n = 0
    for row in values:
        res = await ctx.conn.execute(
            pg_insert(notification_log)
            .values(**row)
            .on_conflict_do_nothing(index_elements=["target", "kind", "anchor"])
            .returning(notification_log.c.id)
        )
        n += len(res.all())
    ctx.report.set("lte", "notifications", len(values))
    ctx.report.inc("lte", "notifications_new", n)


async def _counters(ctx: Ctx, groups: Mapping[int, int]) -> None:
    rep = ctx.report
    if not groups:
        return
    nodes = sorted(
        {
            str(r[0])
            for r in (
                await ctx.conn.execute(
                    sa.select(lte_group_nodes.c.node_uuid).where(
                        lte_group_nodes.c.group_id.in_(list(groups.values()))
                    )
                )
            ).all()
        }
    )
    if not nodes:
        return
    today = msk_date(ctx.t0)
    since = today - timedelta(days=1)
    rows = await ctx.src.rows(
        "wlq_counters",
        [
            "node_uuid",
            "panel_user_id",
            "usage_date",
            "total_bytes",
            "baseline_bytes",
            "accounted_bytes",
            "carry_bytes",
            "vanished_at",
            "last_seen_at",
        ],
        where="usage_date >= $1 AND lower(node_uuid::text) = ANY($2::text[])",
        args=(since, nodes),
        order=None,
    )
    n = 0
    for r in rows:
        total, base, acc, carry = (
            max(0, int(r[k] or 0))
            for k in ("total_bytes", "baseline_bytes", "accounted_bytes", "carry_bytes")
        )
        if base + acc != total + carry:
            rep.issue("lte_counter_invalid", node=_lower(r["node_uuid"]), panel_user_id=r["panel_user_id"])
            continue
        values = {
            "total": total,
            "accounted": acc,
            "baseline": base,
            "carry": carry,
            "vanished_at": r["vanished_at"],
            "seen_at": r["last_seen_at"] or ctx.t0,
        }
        stmt = pg_insert(lte_counters).values(
            node_uuid=_lower(r["node_uuid"]),
            usage_date=r["usage_date"],
            panel_user_id=int(r["panel_user_id"]),
            **values,
        )
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                constraint="pk_lte_counters", set_={k: stmt.excluded[k] for k in values}
            )
        )
        n += 1
    rep.set("lte", "counters", n)
    states = await ctx.src.rows(
        "wlq_node_status",
        [
            "node_uuid",
            "first_read_at",
            "last_ok_read_at",
            "last_ok_read_date",
            "gap_anchor_read_at",
            "gap_tail_cycles",
            "disconnected_since",
            "xray_uptime_s",
        ],
        where="lower(node_uuid::text) = ANY($1::text[])",
        args=(nodes,),
        order=None,
    )
    for r in states:
        values = {
            "first_read_at": r["first_read_at"],
            "last_ok_read_at": r["last_ok_read_at"],
            "last_ok_read_date": r["last_ok_read_date"],
            "gap_anchor_at": r["gap_anchor_read_at"],
            "gap_tail": max(0, int(r["gap_tail_cycles"] or 0)),
            "disconnected_since": r["disconnected_since"],
            "xray_uptime_s": r["xray_uptime_s"],
        }
        stmt = pg_insert(lte_node_state).values(node_uuid=_lower(r["node_uuid"]), **values)
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[lte_node_state.c.node_uuid], set_={k: stmt.excluded[k] for k in values}
            )
        )
    rep.set("lte", "node_states", len(states))


# -------------------------------------------------------------------------------------------- check


async def check(ctx: Ctx) -> None:
    """С7 as far as the importer can prove it: every live enforce block of a linked subject is an active
    ``lte_blocks`` row with its substitution; the open periods' usage equals the source's."""
    rep = ctx.report
    if not await ctx.src.has("wlq_blocks") or await missing_tables(ctx.conn, TABLES):
        return
    skipped = ctx.overrides.skip_panel_user_ids
    src_blocks = {
        int(r["id"])
        for r in await ctx.src.rows(
            "wlq_blocks",
            ["id", "panel_user_id"],
            where="status IN ('pending_apply', 'active') AND mode = 'enforce'",
        )
        if int(r["panel_user_id"]) not in skipped
    }
    mapped = await ctx.mapped("lte_block")
    ids = [int(mapped[str(b)][0]) for b in src_blocks if str(b) in mapped]
    active = set()
    with_subst = set()
    if ids:
        active = {
            int(r[0])
            for r in (
                await ctx.conn.execute(
                    sa.select(lte_blocks.c.id).where(
                        lte_blocks.c.id.in_(ids), lte_blocks.c.status == "active"
                    )
                )
            ).all()
        }
        with_subst = {
            int(str(r[0]).rsplit(":", 1)[1])
            for r in (
                await ctx.conn.execute(
                    sa.select(panel_squad_substitutions.c.source_ref).where(
                        panel_squad_substitutions.c.source_ref.in_([block_ref(i) for i in ids])
                    )
                )
            ).all()
        }
    pmap = await ctx.mapped("lte_period")
    src_used = 0
    if pmap and await ctx.src.has("wlq_period_usage"):
        src_used = int(
            await ctx.src.scalar(
                "SELECT COALESCE(sum(u.used_bytes), 0) FROM public.wlq_period_usage u "
                "JOIN public.wlq_periods p ON p.id = u.period_id "
                "WHERE p.state IN ('open', 'deferred') AND p.id = ANY($1::bigint[])",
                [int(k) for k in pmap],
            )
            or 0
        )
    pids = [int(v[0]) for v in pmap.values()]
    dst_used = 0
    if pids:
        dst_used = int(
            await ctx.conn.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(lte_period_usage.c.used_bytes), 0))
                .select_from(
                    lte_period_usage.join(lte_periods, lte_periods.c.id == lte_period_usage.c.period_id)
                )
                .where(lte_periods.c.id.in_(pids), lte_periods.c.state != "closed")
            )
            or 0
        )
    ok: bool | None = len(active) == len(src_blocks) and with_subst >= active and src_used == dst_used
    if ctx.panel is None:
        ok = None  # without the panel nothing is linked: shadow decides
    rep.check(
        "C7",
        ok,
        blocks_source=len(src_blocks),
        blocks_target=len(active),
        blocks_with_twin=len(with_subst & active),
        used_source=src_used,
        used_target=dst_used,
    )
