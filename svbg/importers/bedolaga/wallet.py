"""Wallets (06 §2.4.1, 05 §2.3.6 п.7): the balance is carried over **as is**, never rebuilt from history.

* one ``wallet_ledger(reason='import_opening', ref_type='import_run', ref_id=<run>)`` per user with a positive
  ``balance_kopeks`` (zero balances get no row; a negative one is reported and waits for a manual decision);
* a repeated run (shadow on a fresh dump) never writes a second opening: it moves the wallet by the difference
  between the source balance and the sum of the import's own entries (``import_adjust``, one per run) — so
  ``Σ import entries(user) = balance_kopeks`` holds after every run while the bot's own movements are kept;
* a user imported by an earlier run and left out now (``deleted`` without money, gone from the source) is
  brought to the source balance (0) the same way — no stale opening survives on a wallet nobody reconciles;
* every movement is one statement that changes ``users.wallet_minor`` and inserts the ledger row together
  (``Σ ledger = wallet_minor``); a decrease that would take the wallet below zero is not applied and blocks
  the cut-over (``wallet_adjust_blocked``).

Check С2: ``Σ wallet_minor = Σ balance_kopeks`` and per user, for every imported user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.billing import wallet as billing_wallet
from svbg.billing.tables import users_wallet, wallet_ledger
from svbg.importers.bedolaga.plan import chunks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["ADJUST", "IMPORT_REASONS", "OPENING", "REF_TYPE", "check", "run"]

OPENING: Final = "import_opening"
#: Signed correction of the opening on a repeated (shadow) run. Needs registering in
#: ``svbg.domain.wallet_rules.SIGNED_REASONS`` (integration request); written by :func:`_adjust` meanwhile.
ADJUST: Final = "import_adjust"
IMPORT_REASONS: Final = (OPENING, ADJUST)
REF_TYPE: Final = "import_run"


async def _imported_sums(ctx: Ctx, user_ids: list[int]) -> tuple[dict[int, int], set[int]]:
    sums: dict[int, int] = {}
    opened: set[int] = set()
    L = wallet_ledger
    for chunk in chunks(user_ids, 5000):
        rows = (
            await ctx.conn.execute(
                sa.select(L.c.user_id, L.c.reason, sa.func.sum(L.c.amount_minor))
                .where(L.c.user_id.in_(chunk), L.c.reason.in_(IMPORT_REASONS), L.c.ref_type == REF_TYPE)
                .group_by(L.c.user_id, L.c.reason)
            )
        ).all()
        for uid, reason, total in rows:
            sums[int(uid)] = sums.get(int(uid), 0) + int(total)
            if reason == OPENING:
                opened.add(int(uid))
    return sums, opened


async def _adjust(conn: AsyncConnection, user_id: int, delta: int, *, ref_id: str, currency: str) -> bool:
    """``wallet_minor += delta`` and the ``import_adjust`` row in one statement; ``False`` if it would go
    below
    zero (or was already applied for this run)."""
    U, L = users_wallet, wallet_ledger
    dup = (
        sa.select(sa.literal(1))
        .where(L.c.user_id == user_id, L.c.reason == ADJUST, L.c.ref_type == REF_TYPE, L.c.ref_id == ref_id)
        .exists()
    )
    upd = (
        sa.update(U)
        .where(U.c.id == user_id, ~dup, U.c.wallet_minor + delta >= 0)
        .values(wallet_minor=U.c.wallet_minor + delta)
        .returning(U.c.wallet_minor)
        .cte("upd")
    )
    ins = (
        sa.insert(L)
        .from_select(
            ["user_id", "amount_minor", "currency", "balance_after", "reason", "ref_type", "ref_id", "note"],
            sa.select(
                sa.literal(user_id, sa.BigInteger),
                sa.literal(delta, sa.BigInteger),
                sa.literal(currency),
                upd.c.wallet_minor,
                sa.literal(ADJUST),
                sa.literal(REF_TYPE),
                sa.literal(ref_id),
                sa.literal("Bedolaga: изменение баланса между прогонами импорта"),
            ),
        )
        .returning(L.c.id)
    )
    return (await conn.execute(ins)).first() is not None


async def _orphans(ctx: Ctx) -> dict[int, int]:
    """Users imported by an earlier run that this run no longer carries over (``deleted`` without money now,
    vanished from the source, or in conflict): ``user id → the balance the import's entries must sum to``
    — the source balance if the row is still there, else 0. Without this an old opening would stay on a
    wallet nobody reconciles any more."""
    mapped = await ctx.mapped("user")
    ids = sorted(int(k) for k in mapped if k.isdigit() and int(k) not in ctx.users)
    if not ids:
        return {}
    source = {
        int(r["id"]): int(r["balance_kopeks"] or 0)
        for r in await ctx.src.rows(
            "users", ["id", "balance_kopeks"], where="id = ANY($1::int[])", args=(ids,)
        )
    }
    return {uid: max(0, source.get(uid, 0)) for uid in ids}


async def run(ctx: Ctx) -> None:
    rep, cur = ctx.report, ctx.cfg.currency
    balances: dict[int, int] = {}
    for uid, row in ctx.users.items():
        bal = int(row["balance_kopeks"] or 0)
        if bal < 0:
            rep.issue("negative_balance", user_id=uid, balance_kopeks=bal)
            continue
        balances[uid] = bal
    orphans = await _orphans(ctx)
    if orphans:
        existing: set[int] = set()
        for chunk in chunks(list(orphans), 5000):
            existing.update(
                int(x)
                for x in (
                    await ctx.conn.execute(sa.select(users_wallet.c.id).where(users_wallet.c.id.in_(chunk)))
                ).scalars()
            )
        for uid, bal in orphans.items():
            if uid in existing:
                balances[uid] = bal
                rep.inc("wallet", "orphans")
    sums, opened = await _imported_sums(ctx, list(balances))
    ref = str(ctx.run_id)
    for uid, bal in balances.items():
        have = sums.get(uid, 0)
        if bal == have:
            if bal:
                rep.inc("wallet", "unchanged")
            continue
        if uid not in opened and have == 0:
            entry = await billing_wallet.credit(
                ctx.conn,
                uid,
                bal,
                reason=OPENING,
                ref_type=REF_TYPE,
                ref_id=ref,
                currency=cur,
                note="Bedolaga: остаток на момент переезда",
            )
            if entry is None:
                rep.issue("wallet_opening_failed", user_id=uid, balance_kopeks=bal)
                continue
            rep.inc("wallet", "openings")
            rep.inc("wallet", "opening_minor", bal)
            continue
        delta = bal - have
        if await _adjust(ctx.conn, uid, delta, ref_id=ref, currency=cur):
            rep.inc("wallet", "adjusted")
            rep.inc("wallet", "adjust_minor", delta)
        else:
            rep.issue("wallet_adjust_blocked", user_id=uid, balance_kopeks=bal, imported=have, delta=delta)
    rep.set("wallet", "source_total_minor", sum(b for uid, b in balances.items() if uid in ctx.users))


async def check(ctx: Ctx) -> None:
    """С2: per user the import's ledger entries equal the source balance, and so does ``wallet_minor``
    (until the bot itself moves money after T0)."""
    expected = {uid: max(0, int(r["balance_kopeks"] or 0)) for uid, r in ctx.users.items()}
    expected.update(await _orphans(ctx))
    sums, _ = await _imported_sums(ctx, list(expected))
    wallets: dict[int, int] = {}
    for chunk in chunks(list(expected), 5000):
        for uid, w in (
            await ctx.conn.execute(
                sa.select(users_wallet.c.id, users_wallet.c.wallet_minor).where(users_wallet.c.id.in_(chunk))
            )
        ).all():
            wallets[int(uid)] = int(w)
    bad: list[dict[str, Any]] = []
    for uid, bal in expected.items():
        if sums.get(uid, 0) != bal or wallets.get(uid, 0) != bal:
            bad.append(
                {
                    "user_id": uid,
                    "balance_kopeks": bal,
                    "imported": sums.get(uid, 0),
                    "wallet": wallets.get(uid),
                }
            )
    total_src = sum(expected.values())
    total_dst = sum(wallets.get(uid, 0) for uid in expected)
    ctx.report.check(
        "C2",
        not bad and total_src == total_dst,
        source_total=total_src,
        wallet_total=total_dst,
        mismatches=bad[:50],
    )
