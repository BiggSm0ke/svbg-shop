"""IP Guard → the tables of the IP Guard module (06 §2.8, 05 §2.2.10).

* ``ip_guard_blocks`` ``status='active'`` → ``ip_guard_blocks(status='active')`` of the subscription linked
  to the block's panel account: ``frozen_seconds = (0 if zeroed_during_block else max(0, E0 − blocked_at))
  + credited_seconds`` (``E0`` = ``end_date_at_block`` for ``owner_kind='bot_sub'``, ``panel_expire_at_block``
  otherwise), ``zeroed``, ``reason`` (``anomaly_manual`` → ``anomaly``), metrics, ``events``, the top-50 of
  ``ips`` as ``evidence`` (the module's format: ``{"top": [{key, ips[≤8], nodes, seen}], "total"}``),
  ``confirmed_by`` (Telegram id → ``users.id``). The hold itself (``hold_kind='ip_guard'``, ``paid_until``
  **not** moved) is set by the subscriptions stage; this stage reports a block whose subscription has no hold;
* ``status='unblocked' AND panel_restored IS NOT TRUE`` → ``status='unblocked'`` with ``new_paid_until =
  new_end_date`` + the owner item «дожать кнопкой в Bedolaga до T0» (``ip_guard_unblock_pending``);
* other unblocked / closed blocks → ``status='closed'`` archive rows (the «повторный нарушитель» mark), no
  evidence (personal data);
* ``ip_guard_warnings`` → **one** acknowledged ``ip_guard_alerts(kind='warn')`` per account with the count
  (``metrics.legacy_warnings``) — the rows themselves are not carried over;
* ``IP_GUARD_WHITELIST_PANEL_USER_IDS`` (``.env``, else ``system_settings``; split by ``[\\s,;]+`` like
  Bedolaga) → ``ip_guard_exempt(subscription_id, reason='import', until=NULL)`` through the subscription with
  that ``panel_user_id``; bad tokens and ids without a subscription are listed for the owner. The key itself
  is not a setting here: the white list is data.

Re-runs follow the source: an imported active block that is no longer active in Bedolaga is closed; an
imported white-list row whose id left the list is removed. Rows the stand created itself are left alone (an
active block of the stand on the same subscription is reported).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.tables import users
from svbg.ext.ip_guard.tables import ip_guard_alerts, ip_guard_blocks, ip_guard_exempt
from svbg.importers.bedolaga.lte import missing_tables, stale_ids, subs_by_panel, upsert_mapped
from svbg.importers.bedolaga.subscriptions import frozen_seconds

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["MODULE", "WHITELIST_KEY", "check", "evidence", "parse_whitelist", "run"]

MODULE: Final = "ip_guard"
WHITELIST_KEY: Final = "IP_GUARD_WHITELIST_PANEL_USER_IDS"
TABLES: Final = ("ip_guard_blocks", "ip_guard_alerts", "ip_guard_exempt")
EVIDENCE_TOP: Final = 50
EVIDENCE_TTL: Final = timedelta(days=180)  # ip_guard.evidence_ttl_days default
EVENTS_MAX: Final = 100
_SPLIT: Final = re.compile(r"[\s,;]+")
_REASON: Final = {"auto": "auto", "manual": "manual", "anomaly_manual": "anomaly", "anomaly": "anomaly"}
_COLS: Final = (
    "id",
    "status",
    "reason",
    "panel_user_id",
    "user_id",
    "telegram_id",
    "subscription_id",
    "owner_kind",
    "blocked_at",
    "ip_count",
    "live_ip_count",
    "subnet_count",
    "ips",
    "end_date_at_block",
    "panel_expire_at_block",
    "credited_seconds",
    "zeroed_during_block",
    "events",
    "pinned",
    "confirmed_by",
    "confirmed_at",
    "unblock_mode",
    "unblocked_by",
    "unblocked_at",
    "unblock_outcome",
    "new_end_date",
    "panel_restored",
    "closed_by",
    "closed_at",
)


def parse_whitelist(raw: str | None) -> tuple[list[int], list[str]]:
    """Bedolaga's parsing (``[\\s,;]+``): positive integers, the rest is reported."""
    ids: list[int] = []
    bad: list[str] = []
    for token in _SPLIT.split(raw or ""):
        if not token:
            continue
        if token.isdigit() and int(token) > 0:
            if int(token) not in ids:
                ids.append(int(token))
        else:
            bad.append(token)
    return ids, bad


def _json(value: Any) -> Any:
    """``jsonb`` of the source arrives as text (the source connection has no JSON codec)."""
    if isinstance(value, str | bytes):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def evidence(ips: Any, total: int | None = None) -> dict[str, Any]:
    """Bedolaga ``ips`` (freshest first: ``{key, raw, nodes: {uuid: seen}, seen_at}``) → module evidence."""
    ips = _json(ips)
    items = ips if isinstance(ips, list) else []
    top = []
    for e in items[:EVIDENCE_TOP]:
        if not isinstance(e, Mapping):
            continue
        nodes = e.get("nodes") or {}
        top.append(
            {
                "key": str(e.get("key") or ""),
                "ips": sorted(str(i) for i in e.get("raw") or [])[:8],
                "nodes": sorted(str(n) for n in (nodes if isinstance(nodes, Mapping | list) else [])),
                "seen": e.get("seen_at"),
            }
        )
    return {"top": top, "total": int(total if total is not None else len(items)), "import": "bedolaga"}


def _events(raw: Any, extra: Mapping[str, Any]) -> list[Any]:
    raw = _json(raw)
    items = [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []
    return [*items[-(EVENTS_MAX - 1) :], dict(extra)]


async def _source_has_state(ctx: Ctx) -> bool:
    if ctx.settings.get(WHITELIST_KEY):
        return True
    for table in ("ip_guard_blocks", "ip_guard_warnings"):
        if await ctx.src.has(table) and int(
            await ctx.src.scalar(f"SELECT count(*) FROM public.{table}") or 0
        ):
            return True
    return False


async def _admins(ctx: Ctx, telegram_ids: set[int]) -> dict[int, int]:
    """Telegram id → ``users.id`` of the admins named in the blocks."""
    if not telegram_ids:
        return {}
    rows = (
        await ctx.conn.execute(
            sa.select(users.c.telegram_id, users.c.id).where(users.c.telegram_id.in_(sorted(telegram_ids)))
        )
    ).all()
    return {int(t): int(i) for t, i in rows}


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    missing = await missing_tables(ctx.conn, TABLES)
    if missing:
        if await _source_has_state(ctx):
            rep.issue("module_missing", module=MODULE, state=[f"нет таблиц в боте: {', '.join(missing)}"])
        return
    rows = await ctx.src.rows("ip_guard_blocks", list(_COLS))
    warnings = await ctx.src.rows("ip_guard_warnings", ["id", "panel_user_id", "created_at"])
    wl_ids, wl_bad = parse_whitelist(ctx.settings.get(WHITELIST_KEY))
    panel_ids = {int(r["panel_user_id"]) for r in rows} | {int(r["panel_user_id"]) for r in warnings}
    subs = await subs_by_panel(ctx, panel_ids | set(wl_ids))
    admin_tg = {int(r[k]) for r in rows for k in ("confirmed_by", "unblocked_by", "closed_by") if r[k]}
    admins = await _admins(ctx, admin_tg)
    await _blocks(ctx, rows, subs, admins)
    await _warnings(ctx, warnings, subs)
    await _whitelist(ctx, wl_ids, wl_bad, subs)


def _status(r: Mapping[str, Any]) -> str:
    if r["status"] == "active":
        return "active"
    if r["status"] == "unblocked" and r["panel_restored"] is not True:
        return "unblocked"
    return "closed"


async def _blocks(
    ctx: Ctx,
    rows: Sequence[Mapping[str, Any]],
    subs: Mapping[int, Mapping[str, Any]],
    admins: Mapping[int, int],
) -> None:
    rep = ctx.report
    t0 = ctx.t0
    live = [int(r["id"]) for r in rows if r["status"] == "active"]
    for bid in await stale_ids(ctx, "ip_guard_block", [r["id"] for r in rows]):
        # The source row is gone altogether (never happens in Bedolaga, but a re-run must stay sane).
        await ctx.conn.execute(
            sa.update(ip_guard_blocks)
            .where(ip_guard_blocks.c.id == bid, ip_guard_blocks.c.status == "active")
            .values(status="closed", closed_at=t0, frozen_seconds=0)
        )
    mapped = await ctx.mapped("ip_guard_block")
    for r in rows:
        status = _status(r)
        pid = int(r["panel_user_id"])
        sub = subs.get(pid)
        if sub is None:
            if status != "closed":
                rep.issue("ip_guard_block_unlinked", block_id=r["id"], panel_user_id=pid, status=r["status"])
            else:
                rep.inc("ip_guard", "archive_unlinked")
            continue
        sid = int(sub["id"])
        mine = mapped.get(str(r["id"]))
        if status == "active":
            clash = await ctx.conn.scalar(
                sa.select(ip_guard_blocks.c.id).where(
                    ip_guard_blocks.c.subscription_id == sid, ip_guard_blocks.c.status == "active"
                )
            )
            if clash is not None and (mine is None or int(mine[0]) != int(clash)):
                rep.issue("ip_guard_block_exists", subscription_id=sid, block_id=r["id"])
                continue
            if sub["hold_kind"] != "ip_guard" or sub["disabled_reason"] != "ip_guard":
                rep.issue("ip_guard_hold_missing", subscription_id=sid, block_id=r["id"])
        frozen = frozen_seconds(r) if status == "active" else 0
        values: dict[str, Any] = {
            "subscription_id": sid,
            "panel_user_id": pid,
            "user_id": sub["user_id"],
            "status": status,
            "reason": _REASON.get(str(r["reason"]), "auto"),
            "blocked_at": r["blocked_at"] or t0,
            "ip_count": int(r["ip_count"] or 0),
            "live_ip_count": int(r["live_ip_count"] or 0),
            "subnet_count": int(r["subnet_count"] or 0),
            "evidence": evidence(r["ips"], r["ip_count"]) if status != "closed" else {"archived": True},
            "evidence_purge_at": (r["blocked_at"] or t0) + EVIDENCE_TTL if status != "closed" else None,
            "frozen_seconds": frozen,
            "zeroed": bool(r["zeroed_during_block"]),
            "pinned": False,
            "confirmed_by": admins.get(int(r["confirmed_by"])) if r["confirmed_by"] else None,
            "confirmed_at": r["confirmed_at"],
            "unblock_mode": r["unblock_mode"] if r["unblock_mode"] in ("plain", "revoke") else None,
            "unblocked_by": admins.get(int(r["unblocked_by"])) if r["unblocked_by"] else None,
            "unblocked_at": r["unblocked_at"],
            "outcome": r["unblock_outcome"],
            "new_paid_until": r["new_end_date"] if status == "unblocked" else None,
            "closed_by": admins.get(int(r["closed_by"])) if r["closed_by"] else None,
            "closed_at": (r["closed_at"] or r["unblocked_at"] or t0) if status == "closed" else None,
            "events": _events(
                r["events"],
                {"kind": "imported", "at": t0.isoformat(), "source_id": int(r["id"]), "status": r["status"]},
            ),
        }
        await upsert_mapped(ctx, "ip_guard_block", ip_guard_blocks, r["id"], values)
        rep.inc("ip_guard", f"blocks_{status}")
        if status == "unblocked":
            rep.issue(
                "ip_guard_unblock_pending",
                block_id=r["id"],
                subscription_id=sid,
                new_paid_until=r["new_end_date"],
            )
    rep.set("ip_guard", "blocks_source_active", len(live))


async def _warnings(
    ctx: Ctx, rows: Sequence[Mapping[str, Any]], subs: Mapping[int, Mapping[str, Any]]
) -> None:
    """One acknowledged ``warn`` alert per account with the number of Bedolaga warnings (06 §2.8)."""
    by_pid: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pid[int(r["panel_user_id"])].append(r)
    n = 0
    for pid, items in sorted(by_pid.items()):
        sub = subs.get(pid)
        if sub is None:
            continue
        times = [r["created_at"] for r in items if r["created_at"] is not None] or [ctx.t0]
        values = {
            "kind": "warn",
            "reason": None,
            "subscription_id": int(sub["id"]),
            "panel_user_id": pid,
            "metrics": {
                "legacy_warnings": len(items),
                "first_at": min(times).isoformat(),
                "last_at": max(times).isoformat(),
                "import": "bedolaga",
            },
            "acked_at": ctx.t0,
            "created_at": max(times),
            "updated_at": ctx.t0,
        }
        await upsert_mapped(ctx, "ip_guard_warnings", ip_guard_alerts, pid, values)
        n += 1
    ctx.report.set("ip_guard", "warnings_source", len(rows))
    ctx.report.set("ip_guard", "warning_accounts", n)


async def _whitelist(
    ctx: Ctx, ids: Sequence[int], bad: Sequence[str], subs: Mapping[int, Mapping[str, Any]]
) -> None:
    rep = ctx.report
    for token in bad:
        rep.issue("ip_guard_whitelist_invalid", value=token)
    entries: dict[int, int] = {}
    for pid in ids:
        sub = subs.get(pid)
        if sub is None:
            rep.issue("ip_guard_whitelist_unresolved", panel_user_id=pid)
            continue
        entries[pid] = int(sub["id"])
    # Ids that left the list: the import's own rows go (an admin's own exemption is never touched).
    prev = await ctx.mapped("ip_guard_exempt")
    gone = [int(new) for old, (new, _d) in prev.items() if int(old) not in entries]
    if gone:
        await ctx.conn.execute(
            sa.delete(ip_guard_exempt).where(
                ip_guard_exempt.c.subscription_id.in_(gone), ip_guard_exempt.c.reason == "import"
            )
        )
    for sid in entries.values():
        stmt = pg_insert(ip_guard_exempt).values(
            subscription_id=sid, reason="import", actor_id=None, until=None
        )
        await ctx.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[ip_guard_exempt.c.subscription_id],
                # A «ложная тревога» exemption (until set) becomes the permanent white list.
                set_={
                    "until": None,
                    "reason": sa.case(
                        (ip_guard_exempt.c.until.is_(None), ip_guard_exempt.c.reason),
                        else_=sa.literal("import"),
                    ),
                },
            )
        )
    await ctx.remember("ip_guard_exempt", [(pid, sid, {}) for pid, sid in entries.items()])
    rep.set("ip_guard", "whitelist", len(ids))
    rep.set("ip_guard", "whitelist_imported", len(entries))


async def check(ctx: Ctx) -> None:
    """С8 as far as the importer can prove it: every active source block of a linked account is an active
    block with the source's frozen seconds and the subscription holds; the white list is exempt."""
    rep = ctx.report
    if await missing_tables(ctx.conn, TABLES):
        return
    rows = [r for r in await ctx.src.rows("ip_guard_blocks", list(_COLS)) if r["status"] == "active"]
    mapped = await ctx.mapped("ip_guard_block")
    bad: list[int] = []
    ok_n = 0
    for r in rows:
        if int(r["panel_user_id"]) in ctx.overrides.skip_panel_user_ids:
            continue
        m = mapped.get(str(r["id"]))
        row = None
        if m is not None:
            row = (
                await ctx.conn.execute(
                    sa.select(ip_guard_blocks.c.status, ip_guard_blocks.c.frozen_seconds).where(
                        ip_guard_blocks.c.id == int(m[0])
                    )
                )
            ).first()
        if row is None or row[0] != "active" or int(row[1]) != frozen_seconds(r):
            bad.append(int(r["id"]))
        else:
            ok_n += 1
    wl_ids, _bad = parse_whitelist(ctx.settings.get(WHITELIST_KEY))
    subs = await subs_by_panel(ctx, wl_ids)
    exempt = {
        int(x[0])
        for x in (
            await ctx.conn.execute(
                sa.select(ip_guard_exempt.c.subscription_id).where(ip_guard_exempt.c.until.is_(None))
            )
        ).all()
    }
    wl_missing = [pid for pid, s in subs.items() if int(s["id"]) not in exempt]
    ok: bool | None = not bad and not wl_missing
    if ctx.panel is None:
        ok = None
    rep.check("C8", ok, blocks=len(rows), matched=ok_n, mismatched=bad, whitelist_missing=wl_missing)
