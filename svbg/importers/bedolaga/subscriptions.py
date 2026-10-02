"""Subscriptions and the link to Remnawave 3.x (06 §2.2–2.3, 02 §7.3).

The panel is read once (``users/stream``, 500 per page) into an in-memory index by ``id`` / ``shortUuid`` /
``telegramId`` — the importer **never writes** to it. For each Bedolaga subscription the key is, in order:
``subscriptions.remnawave_id`` or ``users.remnawave_id`` → ``remnawave_short_uuid`` or the last path
segment of
``subscription_url`` → ``telegramId`` with exactly one match (owner override ``link`` first). Checks: the
panel's ``telegramId`` equals the user's, the ``shortUuid`` equals the stored one, and ``wlq_subjects``
agrees;
any mismatch is a **conflict** (``link_state='panel_missing'`` without a panel id: never linked automatically,
never re-created by the writer).

Values (ids kept): ``paid_until`` = the panel's ``expireAt`` when linked (a later ``expireAt`` — renewal past
the bot — is marked ``overrides._import_expire``; an earlier one raises «срок в панели меньше оплаченного»),
otherwise ``end_date``; limits, strategy, tag and external squad from the panel (truth); ``desired_squads`` =
the logical set ``connected_squads ∪ subscription_servers`` mapped twin → base (never a twin, R4; empty → the
panel's set reversed, then the plan's); ``DISABLED`` in the panel → ``disabled_reason`` ``ip_guard`` (an
active
IP Guard block) / ``channel_left`` (a trial whose user left the channel) / ``admin`` (reported);
``last_revoke_at`` → ``cooldowns.reissue``; ``extra_devices`` = devices above the plan's limit (paid add-on).

Panel accounts without a bot subscription become **unclaimed** subscriptions (``user_id NULL``,
``plan_snapshot.origin='panel'``) unless the owner skipped them. The panel account of a bot subscription the
import leaves out (owner rule ``skip_subscription_ids``, or its user in conflict) is **not** unclaimed: it
has an owner; the pair is reported (``skipped_subscription_panel``).

An active IP Guard block (06 §2.8) also sets the hold: ``hold_kind='ip_guard'``, ``hold_since=blocked_at``,
``hold_frozen_seconds = (0 if zeroed else max(0, E0 − blocked_at)) + credited_seconds`` (``E0`` =
``end_date_at_block`` for a bot subscription, ``panel_expire_at_block`` for a panel-only account),
``hold_zeroed`` — the writer then never writes ``expireAt`` of the frozen account; ``paid_until`` is not
moved. The ``disabled_reason='channel_left'`` decision reads the membership cache of the **required**
channel(s) only; active trials disabled in the panel are listed for a ``getChatMember`` re-check
(``trial_membership_unverified``: the importer has no Bot API).

A re-run (shadow) refreshes a subscription — unclaimed ones included — only if the bot has not changed it
since the last import (fingerprint in ``legacy_id_map.data``); otherwise it is reported and left alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from svbg.importers import legacy_id_map
from svbg.importers.bedolaga.catalog import GIB
from svbg.importers.bedolaga.plan import SOURCE, chunks, id_floor, reserve_ids, uniform
from svbg.remnawave.contributors import reverse
from svbg.remnawave.models import PanelUser
from svbg.remnawave.projection import snapshot_values
from svbg.remnawave.transport import Lane
from svbg.subscriptions.tables import subscription_events, subscriptions

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "ApiPanelReader",
    "PanelIndex",
    "PanelReader",
    "StaticPanelReader",
    "check",
    "run",
    "short_from_url",
]

EXPIRE_SLACK: Final = timedelta(minutes=5)
COLUMNS: Final = (
    "id",
    "user_id",
    "status",
    "is_trial",
    "start_date",
    "end_date",
    "traffic_limit_gb",
    "device_limit",
    "connected_squads",
    "created_at",
    "updated_at",
    "remnawave_short_uuid",
    "remnawave_id",
    "subscription_url",
    "last_revoke_at",
    "tariff_id",
    "autopay_enabled",
)
#: Columns the bot may change after an import; a re-run only refreshes a row whose values are still the
#: importer's (fingerprint).
GUARDED: Final = (
    "user_id",
    "plan_id",
    "link_state",
    "panel_user_id",
    "paid_until",
    "desired_expire_at",
    "desired_status",
    "disabled_reason",
    "desired_device_limit",
    "desired_squads",
    "extra_devices",
    "is_trial",
)

#: The IP Guard hold the import keeps in step with the source (06 §2.8); not part of the fingerprint.
HOLD_COLUMNS: Final = ("hold_kind", "hold_since", "hold_frozen_seconds", "hold_zeroed")

# ------------------------------------------------------------------------------------------------ panel


class PanelReader(Protocol):
    def users(self) -> AsyncIterator[PanelUser]: ...


class ApiPanelReader:
    """``users/stream`` through the bot's Remnawave client (reads only, background lane)."""

    def __init__(self, api: RemnawaveApi, *, page_size: int = 500) -> None:
        self._api = api
        self._size = page_size

    async def users(self) -> AsyncIterator[PanelUser]:
        async for page in self._api.iter_users(self._size, lane=Lane.BACKGROUND):
            for user in page.users:
                yield user


class StaticPanelReader:
    """A fixed list of panel users (tests, an exported snapshot)."""

    def __init__(self, items: Iterable[PanelUser]) -> None:
        self._items = list(items)

    async def users(self) -> AsyncIterator[PanelUser]:
        for user in self._items:
            yield user


@dataclass(slots=True)
class PanelIndex:
    by_id: dict[int, PanelUser] = field(default_factory=dict)
    by_short: dict[str, PanelUser] = field(default_factory=dict)
    by_tg: dict[int, list[PanelUser]] = field(default_factory=dict)

    @classmethod
    async def load(cls, reader: PanelReader) -> PanelIndex:
        idx = cls()
        async for u in reader.users():
            idx.by_id[u.id] = u
            if u.short_uuid:
                idx.by_short[u.short_uuid] = u
            if u.telegram_id is not None:
                idx.by_tg.setdefault(u.telegram_id, []).append(u)
        return idx


def short_from_url(url: str | None) -> str | None:
    if not url:
        return None
    tail = url.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail or None


# ------------------------------------------------------------------------------------------------ helpers


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.timestamp()
    return value


def frozen_seconds(block: Mapping[str, Any]) -> int:
    """06 §2.8 / 05 §2.2.4: ``(0 if zeroed else max(0, E0 − blocked_at)) + credited_seconds``."""
    credited = int(block.get("credited_seconds") or 0)
    if block.get("zeroed_during_block"):
        return credited
    key = "end_date_at_block" if block.get("owner_kind") == "bot_sub" else "panel_expire_at_block"
    e0, at = block.get(key), block.get("blocked_at")
    if e0 is None or at is None:
        return credited
    return max(0, int((e0 - at).total_seconds())) + credited


def _hold(block: Mapping[str, Any] | None) -> dict[str, Any]:
    """``hold_*`` columns for an active IP Guard block (the cleared hold without one)."""
    if block is None or block.get("blocked_at") is None:
        return {"hold_kind": None, "hold_since": None, "hold_frozen_seconds": 0, "hold_zeroed": False}
    return {
        "hold_kind": "ip_guard",
        "hold_since": block["blocked_at"],
        "hold_frozen_seconds": frozen_seconds(block),
        "hold_zeroed": bool(block.get("zeroed_during_block")),
    }


def fingerprint(row: Mapping[str, Any]) -> str:
    data = {k: _norm(row.get(k)) for k in GUARDED}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:32]


@dataclass(slots=True)
class _Link:
    state: str  # linked | panel_missing | conflict | unverified
    user: PanelUser | None = None
    via: str | None = None
    reason: str | None = None


def _link(
    ctx: Ctx, s: Mapping[str, Any], u: Mapping[str, Any], wlq: Mapping[int, int], claimed: set[int]
) -> _Link:
    idx = ctx.panel
    if idx is None:
        return _Link("unverified")
    sid = int(s["id"])
    cand: PanelUser | None = None
    via = None
    if sid in ctx.overrides.link:
        cand, via = idx.by_id.get(ctx.overrides.link[sid]), "override"
        if cand is None:
            return _Link("conflict", reason="override_panel_user_not_found")
    rid = s["remnawave_id"] or u.get("remnawave_id")
    if cand is None and rid:
        cand, via = idx.by_id.get(int(rid)), "id"
    stored_short = s["remnawave_short_uuid"] or short_from_url(s["subscription_url"])
    if cand is None and stored_short:
        cand, via = idx.by_short.get(str(stored_short)), "short_uuid"
    tg = u.get("telegram_id")
    if cand is None and tg is not None:
        found = idx.by_tg.get(int(tg), [])
        if len(found) == 1:
            cand, via = found[0], "telegram_id"
        elif len(found) > 1:
            return _Link("conflict", reason="several_panel_users_for_telegram_id")
    if cand is None:
        return _Link("panel_missing")
    if via != "override":
        if cand.telegram_id is not None and tg is not None and int(cand.telegram_id) != int(tg):
            return _Link("conflict", cand, via, "telegram_id_differs")
        if s["remnawave_short_uuid"] and cand.short_uuid != s["remnawave_short_uuid"]:
            return _Link("conflict", cand, via, "short_uuid_differs")
        if sid in wlq and wlq[sid] != cand.id:
            return _Link("conflict", cand, via, "wlq_subjects_differs")
    if cand.id in claimed:
        return _Link("conflict", cand, via, "panel_user_claimed_twice")
    return _Link("linked", cand, via)


# -------------------------------------------------------------------------------------------------- stage


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    rows = await ctx.src.rows("subscriptions", COLUMNS)
    servers: dict[int, list[str]] = {}
    for r in await ctx.src.rows("subscription_servers", ["id", "subscription_id", "server_squad_id"]):
        uuid = ctx.squad_by_server.get(int(r["server_squad_id"]))
        if uuid:
            servers.setdefault(int(r["subscription_id"]), []).append(uuid)
    wlq = {
        int(r["subscription_id"]): int(r["panel_user_id"])
        for r in await ctx.src.rows("wlq_subjects", ["id", "subscription_id", "panel_user_id", "state"])
        if r["subscription_id"] is not None and (r["state"] or "active") == "active"
    }
    ipg = await ctx.src.rows(
        "ip_guard_blocks",
        [
            "id",
            "status",
            "subscription_id",
            "panel_user_id",
            "owner_kind",
            "blocked_at",
            "end_date_at_block",
            "panel_expire_at_block",
            "credited_seconds",
            "zeroed_during_block",
        ],
    )
    active = [r for r in ipg if r["status"] == "active"]
    ipg_subs = {int(r["subscription_id"]): r for r in active if r["subscription_id"]}
    ipg_panel = {int(r["panel_user_id"]): r for r in active if r["panel_user_id"]}
    members = await _members(ctx)

    ids = [int(r["id"]) for r in rows]
    existing: dict[int, dict[str, Any]] = {}
    for chunk in chunks(ids, 5000):
        for row in (
            await ctx.conn.execute(sa.select(subscriptions).where(subscriptions.c.id.in_(chunk)))
        ).mappings():
            existing[int(row["id"])] = dict(row)
    taken_panel: dict[int, int] = {}
    if ctx.panel is not None:
        pids = list(ctx.panel.by_id)
        for chunk in chunks(pids, 5000):
            for sid, pid in (
                await ctx.conn.execute(
                    sa.select(subscriptions.c.id, subscriptions.c.panel_user_id).where(
                        subscriptions.c.panel_user_id.in_(chunk)
                    )
                )
            ).all():
                taken_panel[int(pid)] = int(sid)
    mapped = await ctx.mapped("subscription")

    claimed: set[int] = set()
    touched: set[int] = set()  # panel users seen by a bot subscription (linked or in conflict)
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    remember: list[tuple[int, int, dict[str, Any]]] = []
    for s in rows:
        sid = int(s["id"])
        rep.inc("subscriptions", "source")
        if sid in ctx.overrides.skip_subscription_ids:
            rep.inc("subscriptions", "skipped_override")
            rep.skip("subscriptions", sid)
            _owned_elsewhere(ctx, s, wlq, touched, "skip_subscription_ids")
            continue
        u = ctx.users.get(int(s["user_id"]))
        if u is None:
            rep.issue("subscription_user_missing", subscription_id=sid, user_id=s["user_id"])
            _owned_elsewhere(ctx, s, wlq, touched, "subscription_user_missing")
            continue
        link = _link(ctx, s, u, wlq, claimed)
        if link.user is not None:
            touched.add(link.user.id)
        if link.state == "linked" and link.user is not None:
            owner = taken_panel.get(link.user.id)
            if owner is not None and owner != sid and await _claim_unowned(ctx, link.user.id, owner):
                taken_panel.pop(link.user.id)
                owner = None
            if owner is not None and owner != sid:
                link = _Link("conflict", link.user, link.via, "panel_user_taken_in_bot")
                rep.issue("panel_user_taken", subscription_id=sid, panel_user_id=link.user.id, by=owner)
        if link.state == "linked" and link.user is not None:
            claimed.add(link.user.id)
        rep.inc("subscriptions", link.state)
        if link.state == "conflict":
            rep.issue(
                "subscription_conflict",
                subscription_id=sid,
                user_id=u["id"],
                reason=link.reason,
                panel_user_id=link.user.id if link.user else None,
            )
        elif link.state == "panel_missing":
            rep.issue("panel_missing", subscription_id=sid, user_id=u["id"])
        values = _values(
            ctx,
            s,
            u,
            link,
            server_squads=servers.get(sid, []),
            ipg_subs=ipg_subs,
            ipg_panel=ipg_panel,
            members=members,
        )
        values["id"] = sid
        cur = existing.get(sid)
        prev = mapped.get(str(sid))
        if cur is not None:
            if prev is None:
                rep.issue("subscription_id_taken", subscription_id=sid)
                continue
            if prev[1].get("fp") != fingerprint(cur):
                rep.issue("subscription_changed_locally", subscription_id=sid)
                ctx.subs[sid] = sid
                continue
            same_hold = all(_norm(cur.get(k)) == _norm(values.get(k)) for k in HOLD_COLUMNS)
            if fingerprint(values) == prev[1].get("fp") and same_hold:
                rep.inc("subscriptions", "unchanged")
            else:
                updates.append(values)
                rep.inc("subscriptions", "updated")
        else:
            inserts.append(values)
            rep.inc("subscriptions", "created")
        ctx.subs[sid] = sid
        remember.append((sid, sid, {"fp": fingerprint(values), "link": link.state, "via": link.via}))

    for chunk in chunks(uniform(inserts), 500):
        await ctx.conn.execute(sa.insert(subscriptions).values(chunk))
    for values in updates:
        sid = values.pop("id")
        await ctx.conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == sid)
            .values(**values, updated_at=sa.func.now())
        )
        values["id"] = sid
    await ctx.remember("subscription", remember)
    await _events(ctx, [int(v["id"]) for v in inserts])
    await _unowned(ctx, claimed | touched, taken_panel, ipg_panel)
    await _count_expired(ctx, rows)


async def _members(ctx: Ctx) -> dict[int, bool]:
    """``telegram_id → member of every required channel`` from the cache ``user_channel_subscriptions``
    (one row per user **and channel**: rows of other channels never decide). Without an active required
    channel every cached row counts."""
    required = {
        str(r["channel_id"])
        for r in await ctx.src.rows("required_channels", ["id", "channel_id", "is_active"])
        if r["channel_id"] and r["is_active"] is not False
    }
    out: dict[int, bool] = {}
    for r in await ctx.src.rows(
        "user_channel_subscriptions", ["id", "telegram_id", "channel_id", "is_member"]
    ):
        if r["telegram_id"] is None or (required and str(r["channel_id"]) not in required):
            continue
        tg = int(r["telegram_id"])
        out[tg] = out.get(tg, True) and bool(r["is_member"])
    return out


def _owned_elsewhere(
    ctx: Ctx, s: Mapping[str, Any], wlq: Mapping[int, int], touched: set[int], why: str
) -> None:
    """A bot subscription left out of the import still owns its panel account: that account is never
    imported as an unclaimed subscription (``sitepay`` / the writer would manage somebody's account)."""
    owner = ctx.source_users.get(int(s["user_id"])) if s["user_id"] is not None else None
    link = _link(ctx, s, owner or {}, wlq, set())
    if link.user is None:
        return
    touched.add(link.user.id)
    ctx.report.issue(
        "skipped_subscription_panel",
        subscription_id=int(s["id"]),
        panel_user_id=link.user.id,
        why=why,
        link=link.state,
    )


async def _claim_unowned(ctx: Ctx, panel_user_id: int, sub_id: int) -> bool:
    """A panel account imported earlier as unclaimed (the dump was older than the panel read) now has its
    Bedolaga subscription: drop the importer's placeholder so the real one (with the Bedolaga id) takes the
    account. Only an untouched placeholder (no user, ``origin='panel'``) is dropped, inside a savepoint."""
    prev = (await ctx.mapped("panel_user")).get(str(panel_user_id))
    if prev is None or int(prev[0]) != sub_id:
        return False
    try:
        async with ctx.conn.begin_nested():
            row = (
                await ctx.conn.execute(
                    sa.select(subscriptions.c.user_id, subscriptions.c.plan_snapshot)
                    .where(subscriptions.c.id == sub_id)
                    .with_for_update()
                )
            ).first()
            if row is None or row[0] is not None or (row[1] or {}).get("origin") != "panel":
                return False
            await ctx.conn.execute(sa.delete(subscriptions).where(subscriptions.c.id == sub_id))
            await ctx.conn.execute(
                sa.delete(legacy_id_map).where(
                    legacy_id_map.c.source == SOURCE,
                    legacy_id_map.c.entity == "panel_user",
                    legacy_id_map.c.old_id == str(panel_user_id),
                )
            )
    except DBAPIError:
        return False
    (await ctx.mapped("panel_user")).pop(str(panel_user_id), None)
    ctx.report.inc("subscriptions", "unowned_claimed")
    return True


def _values(
    ctx: Ctx,
    s: Mapping[str, Any],
    u: Mapping[str, Any],
    link: _Link,
    *,
    server_squads: Sequence[str],
    ipg_subs: Mapping[int, Mapping[str, Any]],
    ipg_panel: Mapping[int, Mapping[str, Any]],
    members: Mapping[int, bool],
) -> dict[str, Any]:
    rep, t0 = ctx.report, ctx.t0
    sid = int(s["id"])
    plan = ctx.plan
    trial = bool(s["is_trial"])
    tg = u.get("telegram_id")
    pu = link.user if link.state == "linked" else None
    end = s["end_date"]
    logical = [x for x in (_json(s["connected_squads"]) or []) if isinstance(x, str)] + list(server_squads)
    squads = reverse(logical, ctx.twins)
    if pu is not None:
        panel_set = reverse(pu.squad_uuids, ctx.twins)
        if squads and sorted(set(squads)) != sorted(set(panel_set)):
            rep.issue("squads_differ", subscription_id=sid, bedolaga=squads, panel=panel_set)
        if not squads:
            squads = panel_set
    if not squads and plan is not None:
        squads = list(plan.squads)
        rep.issue("empty_squads", subscription_id=sid)
    overrides: dict[str, Any] = {}
    paid_until = end
    if pu is not None and pu.expire_at is not None:
        paid_until = pu.expire_at
        if end is not None and pu.expire_at - end > EXPIRE_SLACK:
            overrides["_import_expire"] = "panel_later"
            rep.issue("expire_later_in_panel", subscription_id=sid, end_date=end, expire_at=pu.expire_at)
        elif end is not None and end - pu.expire_at > EXPIRE_SLACK:
            overrides["_import_expire"] = "panel_earlier"
            rep.issue("expire_earlier_in_panel", subscription_id=sid, end_date=end, expire_at=pu.expire_at)
    devices = pu.hwid_device_limit if pu is not None else s["device_limit"]
    if pu is not None and s["device_limit"] is not None and pu.hwid_device_limit != s["device_limit"]:
        rep.issue(
            "devices_differ", subscription_id=sid, bedolaga=s["device_limit"], panel=pu.hwid_device_limit
        )
    if pu is not None and s["traffic_limit_gb"] is not None:
        bedolaga_bytes = int(s["traffic_limit_gb"]) * GIB
        if bedolaga_bytes != int(pu.traffic_limit_bytes or 0):
            rep.issue(
                "traffic_differs",
                subscription_id=sid,
                bedolaga_bytes=bedolaga_bytes,
                panel_bytes=pu.traffic_limit_bytes,
            )
    extra = 0
    if not trial and plan is not None and plan.device_limit and devices:
        extra = max(0, int(devices) - int(plan.device_limit))
    disabled_reason: str | None = None
    desired_status = "active"
    block = ipg_subs.get(sid) or (ipg_panel.get(pu.id) if pu is not None else None)
    blocked_ipg = block is not None
    if pu is not None and pu.status == "DISABLED":
        desired_status = "disabled"
        if blocked_ipg:
            disabled_reason = "ip_guard"
        elif trial and tg is not None and members.get(int(tg)) is False:
            disabled_reason = "channel_left"
        else:
            disabled_reason = "admin"
            rep.issue("disabled_admin", subscription_id=sid, panel_user_id=pu.id)
        if trial and not blocked_ipg and tg is not None and end is not None and end > t0:
            # 06 §2.10: an active trial's membership is re-checked with getChatMember (Bot API, not here).
            rep.issue(
                "trial_membership_unverified",
                subscription_id=sid,
                telegram_id=int(tg),
                cached=members.get(int(tg)),
                reason=disabled_reason,
            )
    elif blocked_ipg:
        # An active block must never be lifted by the import (R5): keep it disabled and report the drift.
        desired_status, disabled_reason = "disabled", "ip_guard"
        rep.issue("ip_guard_panel_not_disabled", subscription_id=sid)
    snapshot: dict[str, Any] = {
        "legacy": True,
        "source": "bedolaga",
        "is_trial": trial,
        "bedolaga": {
            "status": s["status"],
            "tariff_id": s["tariff_id"],
            "autopay": bool(s["autopay_enabled"]),
            "short_uuid": s["remnawave_short_uuid"],
            "link": link.state,
            "via": link.via,
        },
    }
    if trial:
        st = ctx.settings
        snapshot.update(days=st.int("TRIAL_DURATION_DAYS", 3), devices=st.int("TRIAL_DEVICE_LIMIT"))
    cooldowns = {"reissue": s["last_revoke_at"].isoformat()} if s["last_revoke_at"] else {}
    values: dict[str, Any] = {
        "user_id": int(u["id"]) if tg is not None else None,
        "plan_id": None if trial or plan is None else plan.id,
        "plan_snapshot": snapshot,
        "link_state": "linked" if pu is not None else "panel_missing",
        "panel_user_id": pu.id if pu is not None else None,
        "paid_until": paid_until,
        "desired_expire_at": paid_until,
        "desired_traffic_bytes": pu.traffic_limit_bytes
        if pu is not None
        else int(s["traffic_limit_gb"] or 0) * GIB,
        "desired_reset_strategy": pu.traffic_limit_strategy if pu is not None else None,
        "desired_device_limit": devices,
        "desired_squads": squads,
        "desired_ext_squad": pu.external_squad_uuid if pu is not None else None,
        "desired_tag": pu.tag if pu is not None else None,
        "desired_status": desired_status,
        "disabled_reason": disabled_reason,
        "overrides": overrides,
        "is_trial": trial,
        "extra_devices": extra,
        "cooldowns": cooldowns,
        "created_at": s["created_at"] or s["start_date"] or t0,
        **_hold(block),
    }
    if pu is not None:
        values.update(snapshot_values(pu))
        values["panel_squads"] = pu.squad_uuids
        values["panel_state_ts"] = t0
        values["subscription_url"] = pu.subscription_url or None
    else:
        values.update(
            {k: None for k in ("panel_short_uuid", "panel_username", "panel_status", "panel_expire_at")},
            subscription_url=None,
        )
    return values


async def _events(ctx: Ctx, ids: Sequence[int]) -> None:
    for chunk in chunks(list(ids), 2000):
        await ctx.conn.execute(
            pg_insert(subscription_events)
            .from_select(
                ["subscription_id", "kind", "source", "new_expire", "ref_type", "ref_id"],
                sa.select(
                    subscriptions.c.id,
                    sa.literal("imported"),
                    sa.literal("import"),
                    subscriptions.c.paid_until,
                    sa.literal("import"),
                    sa.literal("bedolaga"),
                ).where(subscriptions.c.id.in_(chunk)),
            )
            .on_conflict_do_nothing()
        )


def _unowned_values(ctx: Ctx, pu: PanelUser, block: Mapping[str, Any] | None) -> dict[str, Any]:
    """An unclaimed subscription mirrors its panel account (the panel is the truth: 06 §2.2 п.4)."""
    blocked = block is not None  # an IP Guard block of a panel-only account (owner_kind='panel')
    values = snapshot_values(pu)
    values.update(
        user_id=None,
        plan_id=None,
        plan_snapshot={"legacy": True, "source": "bedolaga", "origin": "panel"},
        link_state="linked",
        panel_user_id=pu.id,
        panel_state_ts=ctx.t0,
        panel_squads=pu.squad_uuids,
        subscription_url=pu.subscription_url or None,
        paid_until=pu.expire_at,
        desired_expire_at=pu.expire_at,
        desired_traffic_bytes=pu.traffic_limit_bytes,
        desired_reset_strategy=pu.traffic_limit_strategy,
        desired_device_limit=pu.hwid_device_limit,
        desired_squads=reverse(pu.squad_uuids, ctx.twins),
        desired_ext_squad=pu.external_squad_uuid,
        desired_tag=pu.tag,
        desired_status="disabled" if blocked else "active",
        disabled_reason="ip_guard" if blocked else None,
        overrides={"status": "DISABLED"} if pu.status == "DISABLED" and not blocked else {},
        is_trial=False,
        extra_devices=0,
        **_hold(block),
    )
    return values


async def _unowned(
    ctx: Ctx,
    seen: set[int],
    taken_panel: Mapping[int, int],
    ipg_panel: Mapping[int, Mapping[str, Any]],
) -> None:
    """Panel users without a bot subscription → unclaimed subscriptions (06 §2.2 п.4). A re-run follows the
    panel (clients «only in the panel» renew through the site, past the bot) while the bot has not changed
    the row since the import; a row the bot changed is reported and left alone."""
    if ctx.panel is None:
        return
    rep = ctx.report
    mapped = await ctx.mapped("panel_user")
    rows: list[dict[str, Any]] = []
    kept: dict[int, int] = {}  # panel user id → subscription id, refreshed or unchanged
    for pid, pu in sorted(ctx.panel.by_id.items()):
        if pid in seen:
            if str(pid) in mapped:
                rep.issue("unowned_placeholder_owned", panel_user_id=pid, subscription_id=mapped[str(pid)][0])
            continue
        rep.inc("subscriptions", "unowned_panel")
        if pid in ctx.overrides.skip_panel_user_ids:
            rep.inc("subscriptions", "unowned_skipped")
            rep.skip("panel_users", pid)
            continue
        values = _unowned_values(ctx, pu, ipg_panel.get(pid))
        if pid in taken_panel:
            prev = mapped.get(str(pid))
            if prev is None or int(prev[0]) != taken_panel[pid]:
                rep.issue("unowned_already_in_bot", panel_user_id=pid, subscription_id=taken_panel[pid])
                continue
            if await _refresh_unowned(ctx, taken_panel[pid], values, prev[1]):
                kept[pid] = taken_panel[pid]
            continue
        rep.issue("unowned_panel", panel_user_id=pid, username=pu.username, telegram_id=pu.telegram_id)
        rows.append(values)
    if rows:
        if ctx.dry:  # the counter is not transactional: a dry run numbers the rows itself
            top = await ctx.conn.scalar(sa.select(sa.func.coalesce(sa.func.max(subscriptions.c.id), 0)))
            first = max(int(top), id_floor(ctx, "subscriptions")) + 1
            for i, values in enumerate(rows):
                values["id"] = first + i
        else:
            # Ids above the source's (+ the shadow gap): Bedolaga keeps numbering its subscriptions meanwhile.
            await reserve_ids(ctx.conn, "subscriptions", id_floor(ctx, "subscriptions"))
        created: dict[int, int] = {}
        for chunk in chunks(uniform(rows), 500):
            res = await ctx.conn.execute(
                sa.insert(subscriptions)
                .values(chunk)
                .returning(subscriptions.c.id, subscriptions.c.panel_user_id)
            )
            created.update({int(pid): int(sid) for sid, pid in res.all()})
        rep.inc("subscriptions", "unowned_created", len(created))
        await _events(ctx, sorted(created.values()))
        kept.update(created)
    if kept:
        fps: dict[int, str] = {}
        for chunk in chunks(sorted(kept.values()), 5000):
            for row in (
                await ctx.conn.execute(sa.select(subscriptions).where(subscriptions.c.id.in_(chunk)))
            ).mappings():
                fps[int(row["id"])] = fingerprint(row)
        await ctx.remember(
            "panel_user",
            [(pid, sid, {"origin": "panel", "fp": fps.get(sid)}) for pid, sid in sorted(kept.items())],
        )


async def _refresh_unowned(ctx: Ctx, sid: int, values: Mapping[str, Any], data: Mapping[str, Any]) -> bool:
    """Bring an unclaimed subscription imported earlier to the panel's current values. ``False`` (reported)
    when the bot changed it meanwhile (claimed by a user, edited, …)."""
    rep = ctx.report
    cur = (
        (await ctx.conn.execute(sa.select(subscriptions).where(subscriptions.c.id == sid).with_for_update()))
        .mappings()
        .first()
    )
    if cur is None:
        rep.issue("unowned_vanished", subscription_id=sid)
        return False
    fp = data.get("fp")
    origin = (cur["plan_snapshot"] or {}).get("origin")
    if cur["user_id"] is not None or origin != "panel" or (fp is not None and fp != fingerprint(cur)):
        rep.issue("unowned_changed_locally", subscription_id=sid, panel_user_id=cur["panel_user_id"])
        return False
    changes = {
        k: v
        for k, v in values.items()
        if k not in ("panel_state_ts", "plan_snapshot") and _norm(cur.get(k)) != _norm(v)
    }
    if not changes:
        rep.inc("subscriptions", "unowned_unchanged")
        return True
    await ctx.conn.execute(
        sa.update(subscriptions)
        .where(subscriptions.c.id == sid)
        .values(**changes, panel_state_ts=ctx.t0, updated_at=sa.func.now())
    )
    rep.inc("subscriptions", "unowned_updated")
    if "paid_until" in changes:
        rep.inc("subscriptions", "unowned_expire_moved")  # renewed past the bot (site fallback, by hand)
    return True


async def _count_expired(ctx: Ctx, rows: Sequence[Mapping[str, Any]]) -> None:
    """Expired Bedolaga subscriptions are kept with ``paid_until`` in the past (06 §2.3: ``expired``)."""
    expired = sum(1 for s in rows if s["end_date"] is not None and s["end_date"] <= ctx.t0)
    ctx.report.set("subscriptions", "expired_in_source", expired)


async def check(ctx: Ctx) -> None:
    """С5: linked / (total − panel_missing − conflicts) = 100 %; and every imported subscription exists."""
    rep = ctx.report
    linked = rep.get("subscriptions", "linked")
    total = len(ctx.subs)
    missing = rep.get("subscriptions", "panel_missing")
    conflicts = rep.get("subscriptions", "conflict")
    present = 0
    for chunk in chunks(list(ctx.subs), 5000):
        present += int(
            await ctx.conn.scalar(sa.select(sa.func.count()).where(subscriptions.c.id.in_(chunk))) or 0
        )
    expected = total - missing - conflicts
    ok = None if ctx.panel is None else (linked == expected and present == total)
    rep.check(
        "C5",
        ok,
        linked=linked,
        expected=expected,
        panel_missing=missing,
        conflicts=conflicts,
        present=present,
    )
    rep.part(
        "C1",
        "subscriptions",
        expected=rep.get("subscriptions", "source") - rep.get("subscriptions", "skipped_override"),
        present=present,
    )
