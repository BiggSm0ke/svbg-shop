"""Referral links and money rewards (06 §2.5, 05 §2.3.6 пп.1, 4, 7).

* ``users.referral_code`` → ``referral_codes(source='import')`` (case kept: old ``?start=ref…`` links);
* ``users.referred_by_id`` (not self, inviter imported) → ``referrals(source='import', attached_at =
  users.created_at)``;
* money rows of ``referral_earnings`` (``amount_kopeks > 0``, ``user_id ≠ referral_id``) → ``referral_rewards
  (kind='days', status='legacy')`` on **both** sides: the pair is never rewarded with days again. The money
  itself is **not** written to the wallet — it is already inside ``balance_kopeks`` (the opening); the
  ``referral_reward`` transactions stay in ``legacy_transactions``.

Days markers (granted / deferred / expired), the cut-off and the «живой хвост» are the referral-days importer
(``svbg.importers.bedolaga.referral_days``); its rows and these share ``UNIQUE(referred_user_id, side) WHERE
kind='days'`` — whoever writes first wins, a legacy money pair is never overwritten here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.importers.bedolaga.plan import chunks, uniform
from svbg.referral.tables import DAYS_PREDICATE, referral_codes, referral_rewards, referrals

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["check", "run"]

_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,62}$")


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    codes = []
    owners = {
        str(code): int(uid)
        for uid, code in (
            await ctx.conn.execute(sa.select(referral_codes.c.user_id, referral_codes.c.code))
        ).all()
    }
    for uid, u in ctx.users.items():
        code = u.get("referral_code")
        if not code:
            continue
        code = str(code)
        if not _CODE_RE.fullmatch(code):
            rep.issue("referral_code_invalid", user_id=uid, code=code)
            continue
        if code in owners and owners[code] != uid:
            rep.issue("referral_code_taken", user_id=uid, code=code, owner=owners[code])
            continue
        codes.append({"user_id": uid, "code": code, "source": "import"})
    for chunk in chunks(codes, 1000):
        res = await ctx.conn.execute(
            pg_insert(referral_codes)
            .values(chunk)
            .on_conflict_do_nothing()
            .returning(referral_codes.c.user_id)
        )
        rep.inc("referral", "codes", len(res.all()))

    existing = {
        int(a): int(b)
        for a, b in (
            await ctx.conn.execute(sa.select(referrals.c.referred_user_id, referrals.c.referrer_id))
        ).all()
    }
    pairs: list[dict[str, Any]] = []
    for uid, u in ctx.users.items():
        ref = u.get("referred_by_id")
        if ref is None or int(ref) == uid:
            continue
        rep.inc("referral", "pairs_source")
        if int(ref) not in ctx.users:
            rep.issue("referrer_not_imported", user_id=uid, referrer_id=ref)
            continue
        if uid in existing:
            if existing[uid] != int(ref):
                rep.issue("referrer_differs", user_id=uid, bedolaga=ref, current=existing[uid])
            continue
        pairs.append(
            {
                "referred_user_id": uid,
                "referrer_id": int(ref),
                "attached_at": u["created_at"] or ctx.t0,
                "source": "import",
            }
        )
        existing[uid] = int(ref)
    for chunk in chunks(pairs, 1000):
        await ctx.conn.execute(pg_insert(referrals).values(chunk).on_conflict_do_nothing())
    rep.inc("referral", "pairs_imported", len(pairs))

    earnings = await ctx.src.rows(
        "referral_earnings",
        ["id", "user_id", "referral_id", "amount_kopeks", "reason", "created_at", "reward_type"],
    )
    money: dict[int, dict[str, Any]] = {}
    for e in earnings:
        amount = int(e["amount_kopeks"] or 0)
        if amount <= 0 or int(e["user_id"]) == int(e["referral_id"]):
            continue
        rep.inc("referral", "money_rows")
        pair = money.setdefault(
            int(e["referral_id"]), {"inviter": int(e["user_id"]), "sum": 0, "at": e["created_at"]}
        )
        pair["sum"] += amount
        if e["created_at"] and (pair["at"] is None or e["created_at"] < pair["at"]):
            pair["at"] = e["created_at"]
    rewards: list[dict[str, Any]] = []
    for referred, pair in money.items():
        inviter = pair["inviter"]
        if referred not in ctx.users or inviter not in ctx.users:
            rep.issue("money_pair_skipped", referred_user_id=referred, inviter_id=inviter)
            continue
        if existing.get(referred) is None:
            await ctx.conn.execute(
                pg_insert(referrals)
                .values(
                    referred_user_id=referred,
                    referrer_id=inviter,
                    attached_at=pair["at"] or ctx.t0,
                    source="import",
                )
                .on_conflict_do_nothing()
            )
            existing[referred] = inviter
            rep.inc("referral", "pairs_from_earnings")
        elif existing[referred] != inviter:
            rep.issue(
                "money_pair_other_referrer",
                referred_user_id=referred,
                inviter_id=inviter,
                current=existing[referred],
            )
        common = {
            "referred_user_id": referred,
            "kind": "days",
            "status": "legacy",
            "reason": "legacy_money",
            "created_at": pair["at"] or ctx.t0,
        }
        rewards.append(
            {
                **common,
                "user_id": inviter,
                "side": "inviter",
                "amount_minor": pair["sum"],
                "currency": ctx.cfg.currency,
            }
        )
        rewards.append({**common, "user_id": referred, "side": "invitee"})
    for chunk in chunks(uniform(rewards), 1000):
        res = await ctx.conn.execute(
            pg_insert(referral_rewards)
            .values(chunk)
            .on_conflict_do_nothing(
                index_elements=[referral_rewards.c.referred_user_id, referral_rewards.c.side],
                index_where=sa.text(DAYS_PREDICATE),
            )
            .returning(referral_rewards.c.id)
        )
        rep.inc("referral", "legacy_rewards", len(res.all()))
    rep.set("referral", "money_pairs", len(money))


async def check(ctx: Ctx) -> None:
    """С1 (referral part): every eligible pair is in ``referrals``; every money pair has both legacy sides."""
    rep = ctx.report
    expected_pairs = rep.get("referral", "pairs_source") - rep.issue_totals.get("referrer_not_imported", 0)
    want = [
        uid
        for uid, u in ctx.users.items()
        if u.get("referred_by_id")
        and int(u["referred_by_id"]) != uid
        and int(u["referred_by_id"]) in ctx.users
    ]
    present = 0
    for chunk in chunks(want, 5000):
        present += int(
            await ctx.conn.scalar(sa.select(sa.func.count()).where(referrals.c.referred_user_id.in_(chunk)))
            or 0
        )
    rep.part("C1", "referrals", expected=expected_pairs, present=present)
