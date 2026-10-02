"""Referral days markers → ``referral_rewards(kind='days')`` (06 §2.5, 05 §2.3.6 пп.2, 3, 5, 6).

Bedolaga keeps the fact of a days reward as a zero ``referral_earnings`` row (``user_id`` = inviter,
``referral_id`` = invitee) with ``reason``:

* ``referral_days_inviter`` / ``referral_days_invitee`` → ``status='granted'`` (``granted_at = created_at``)
  for the side's recipient; the exact number of days is not stored, so the current
  ``REFERRAL_DAYS_INVITER_DAYS`` / ``REFERRAL_DAYS_INVITEE_DAYS`` (14 / 7) are written with
  ``reason='estimated'``. A money pair (``legacy`` from the referral stage) with a days marker becomes
  ``granted`` (the days really were given; the money stays in ``amount_minor``);
* ``*_skipped`` → ``deferred`` with ``retry_until = created_at + REFERRAL_DAYS_RETRY_SKIPPED_HOURS`` (168)
  when that is after T0, otherwise ``expired``; a granted marker of the same side wins; a legacy money side
  stays legacy (``SKIP_ALREADY_PAID``);
* pairs without markers: below the cut-off (``REFERRAL_DAYS_MIN_USER_ID`` on the invitee id, or attached
  before ``REFERRAL_DAYS_START_AFTER``) → ``legacy`` on both sides (never rewarded); above it — the «живой
  хвост»: nothing is written, the number is reported (``referral_days_live_tail``) so the owner pushes them
  by hand after T0;
* self markers and ``referral_registration_pending`` are not carried over.

A missing ``referrals`` row of a marker pair is created (``source='import'``). Re-runs (shadow) refresh a row
only while it is still the importer's (fingerprint in ``legacy_id_map``); a row the bot changed is reported.
The 30-day inviter cap is computed by the module from ``granted_at``, so the imported markers keep it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.referral.tables import referral_rewards, referrals

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["MARKERS", "check", "run"]

INVITER: Final = "referral_days_inviter"
INVITEE: Final = "referral_days_invitee"
MARKERS: Final = (INVITER, f"{INVITER}_skipped", INVITEE, f"{INVITEE}_skipped")
ENTITY: Final = "referral_days"


def _fp(status: str, granted_at: datetime | None, retry_until: datetime | None, days: int | None) -> str:
    g = granted_at.isoformat() if granted_at else ""
    r = retry_until.isoformat() if retry_until else ""
    return f"{status}|{g}|{r}|{days or ''}"


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    if not await ctx.src.has("referral_earnings"):
        return
    days = {
        "inviter": ctx.settings.int("REFERRAL_DAYS_INVITER_DAYS", 14) or None,
        "invitee": ctx.settings.int("REFERRAL_DAYS_INVITEE_DAYS", 7) or None,
    }
    retry = timedelta(hours=max(0, ctx.settings.int("REFERRAL_DAYS_RETRY_SKIPPED_HOURS", 168) or 0))
    rows = await ctx.src.rows(
        "referral_earnings",
        ["id", "user_id", "referral_id", "reason", "created_at"],
        where="reason = ANY($1::text[])",
        args=(list(MARKERS),),
    )
    # (invitee, side) → {"inviter", "granted": first granted marker time, "skipped": last skipped time}
    sides: dict[tuple[int, str], dict[str, Any]] = {}
    for r in rows:
        inviter, invitee = int(r["user_id"]), int(r["referral_id"])
        if inviter == invitee:
            rep.inc("referral_days", "self_markers")
            continue
        reason = str(r["reason"])
        side = "inviter" if reason.startswith(INVITER) else "invitee"
        at = r["created_at"] or ctx.t0
        cur = sides.setdefault((invitee, side), {"inviter": inviter, "granted": None, "skipped": None})
        if cur["inviter"] != inviter:
            rep.issue("referral_days_marker_conflict", invitee=invitee, inviter=inviter, other=cur["inviter"])
            continue
        if reason.endswith("_skipped"):
            cur["skipped"] = at if cur["skipped"] is None else max(cur["skipped"], at)
        else:
            cur["granted"] = at if cur["granted"] is None else min(cur["granted"], at)
    rep.set("referral_days", "markers", len(rows))

    existing_ref = {
        int(a): int(b)
        for a, b in (
            await ctx.conn.execute(sa.select(referrals.c.referred_user_id, referrals.c.referrer_id))
        ).all()
    }
    current = {
        (int(r["referred_user_id"]), str(r["side"])): dict(r)
        for r in (
            await ctx.conn.execute(sa.select(referral_rewards).where(referral_rewards.c.kind == "days"))
        ).mappings()
    }
    mapped = await ctx.mapped(ENTITY)
    remember: list[tuple[str, int, Mapping[str, Any]]] = []
    for (invitee, side), m in sorted(sides.items()):
        inviter = int(m["inviter"])
        if invitee not in ctx.users or inviter not in ctx.users:
            rep.issue("referral_days_pair_skipped", invitee=invitee, inviter=inviter, side=side)
            continue
        ref = existing_ref.get(invitee)
        if ref is None:
            attached = ctx.users[invitee].get("created_at") or m["granted"] or m["skipped"] or ctx.t0
            await ctx.conn.execute(
                pg_insert(referrals)
                .values(referred_user_id=invitee, referrer_id=inviter, attached_at=attached, source="import")
                .on_conflict_do_nothing()
            )
            existing_ref[invitee] = inviter
            rep.inc("referral_days", "pairs_created")
        elif ref != inviter:
            rep.issue("referral_days_other_referrer", invitee=invitee, inviter=inviter, current=ref)
            continue
        recipient = inviter if side == "inviter" else invitee
        if m["granted"] is not None:
            status, granted_at, retry_until = "granted", m["granted"], None
        else:
            until = m["skipped"] + retry
            status, granted_at, retry_until = ("deferred" if until > ctx.t0 else "expired"), None, until
        values = {
            "status": status,
            "days": days[side] if status == "granted" else None,
            "granted_at": granted_at,
            "retry_until": retry_until,
            "reason": "estimated" if status == "granted" else "import_skipped",
        }
        fp = _fp(status, granted_at, retry_until, values["days"])
        key = f"{invitee}:{side}"
        row = current.get((invitee, side))
        if row is None:
            new_id = await ctx.conn.scalar(
                pg_insert(referral_rewards)
                .values(
                    referred_user_id=invitee,
                    user_id=recipient,
                    side=side,
                    kind="days",
                    created_at=granted_at or m["skipped"] or ctx.t0,
                    **values,
                )
                .on_conflict_do_nothing(
                    index_elements=[referral_rewards.c.referred_user_id, referral_rewards.c.side],
                    index_where=sa.text("kind = 'days'"),
                )
                .returning(referral_rewards.c.id)
            )
            if new_id is not None:
                remember.append((key, int(new_id), {"fp": fp}))
                rep.inc("referral_days", status)
            continue
        prev = mapped.get(key)
        mine = prev is not None and int(prev[0]) == int(row["id"])
        if mine:
            if _fp(row["status"], row["granted_at"], row["retry_until"], row["days"]) != prev[1].get("fp"):
                rep.issue("referral_days_changed_by_bot", invitee=invitee, side=side, status=row["status"])
                continue
        elif row["status"] == "legacy":
            if status != "granted":
                rep.inc("referral_days", "legacy_kept")
                continue
            rep.inc("referral_days", "legacy_upgraded")
        else:
            rep.issue("referral_days_changed_by_bot", invitee=invitee, side=side, status=row["status"])
            continue
        await ctx.conn.execute(
            sa.update(referral_rewards).where(referral_rewards.c.id == row["id"]).values(**values)
        )
        remember.append((key, int(row["id"]), {"fp": fp}))
        rep.inc("referral_days", status)
    await ctx.remember(ENTITY, remember)
    await _unmarked(ctx, {inv for inv, _s in sides})


async def _unmarked(ctx: Ctx, marked: set[int]) -> None:
    """Pairs without markers: below the cut-off → legacy on both sides; above it → the live tail."""
    rep = ctx.report
    min_id = ctx.settings.int("REFERRAL_DAYS_MIN_USER_ID", 0) or 0
    start_after = _parse_dt(ctx.settings.get("REFERRAL_DAYS_START_AFTER"))
    have = {
        int(r[0])
        for r in (
            await ctx.conn.execute(
                sa.select(referral_rewards.c.referred_user_id).where(referral_rewards.c.kind == "days")
            )
        ).all()
    }
    pairs = (
        await ctx.conn.execute(
            sa.select(referrals.c.referred_user_id, referrals.c.referrer_id, referrals.c.attached_at).where(
                referrals.c.source == "import"
            )
        )
    ).all()
    legacy: list[dict[str, Any]] = []
    tail = 0
    for raw_invitee, raw_inviter, attached in pairs:
        invitee, inviter = int(raw_invitee), int(raw_inviter)
        if invitee in marked or invitee in have or invitee not in ctx.users:
            continue
        below = (min_id and invitee < min_id) or (start_after is not None and attached < start_after)
        if not below:
            tail += 1
            continue
        for side, uid in (("inviter", inviter), ("invitee", invitee)):
            legacy.append(
                {
                    "referred_user_id": invitee,
                    "user_id": uid,
                    "side": side,
                    "kind": "days",
                    "status": "legacy",
                    "reason": "legacy_cutoff",
                    "created_at": attached,
                }
            )
    if legacy:
        res = await ctx.conn.execute(
            pg_insert(referral_rewards)
            .values(legacy)
            .on_conflict_do_nothing(
                index_elements=[referral_rewards.c.referred_user_id, referral_rewards.c.side],
                index_where=sa.text("kind = 'days'"),
            )
            .returning(referral_rewards.c.id)
        )
        rep.inc("referral_days", "legacy_cutoff", len(res.all()))
    rep.set("referral_days", "live_tail", tail)
    if tail:
        rep.issue("referral_days_live_tail", pairs=tail, min_user_id=min_id or None)


async def check(ctx: Ctx) -> None:
    """С9 as far as the importer can prove it: every days side the importer wrote still holds the state the
    markers give (granted / deferred / expired); the shadow run compares the 30-day caps with the source."""
    rep = ctx.report
    if not await ctx.src.has("referral_earnings"):
        return
    mapped = await ctx.mapped(ENTITY)
    rows = {}
    ids = [int(v[0]) for v in mapped.values()]
    if ids:
        rows = {
            int(r["id"]): r
            for r in (
                await ctx.conn.execute(sa.select(referral_rewards).where(referral_rewards.c.id.in_(ids)))
            ).mappings()
        }
    bad: list[str] = []
    by_status: dict[str, int] = {}
    for key, (new, data) in sorted(mapped.items()):
        row = rows.get(int(new))
        if row is None or _fp(row["status"], row["granted_at"], row["retry_until"], row["days"]) != data.get(
            "fp"
        ):
            bad.append(key)
            continue
        by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + 1
    rep.check("C9", not bad, sides=len(mapped), by_status=by_status, mismatched=bad[:20])
