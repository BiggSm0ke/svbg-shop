"""The wallet (07 §4.5): ``users.wallet_minor`` + ``wallet_ledger``, every movement in the caller's
transaction.

Each operation is **one statement**: a data-modifying CTE that changes ``users.wallet_minor`` and inserts the
ledger row in the same breath, so the balance and the ledger can never disagree:

* :func:`credit` — ``UPDATE users SET wallet_minor = wallet_minor + x … AND NOT EXISTS (same ledger key)``
  then ``INSERT wallet_ledger … FROM upd``. A repeated key is a no-op (returns ``None``); in a race the
  ``UNIQUE(user_id, reason, ref_type, ref_id)`` makes the second transaction fail instead of crediting twice;
* :func:`debit` — the CAS of 07 §4.5: ``… WHERE wallet_minor >= x RETURNING`` — no lock is held across any
  HTTP call, and the balance can never go below zero (also a ``CHECK`` on both tables);
* :func:`take` — a debit of *up to* ``x`` (chargebacks: take what is there, report the rest).

Invariants (property tests in ``tests/billing``): ``Σ wallet_ledger(user) = users.wallet_minor``; every
``balance_after`` equals the running sum; no balance below zero; at most one entry per key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from svbg.billing.tables import users_wallet, wallet_ledger
from svbg.core.tables import admin_audit
from svbg.domain.wallet_rules import check_entry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "Entry",
    "adjust",
    "balance",
    "credit",
    "credit_many",
    "debit",
    "find",
    "lock_user",
    "mismatches",
    "take",
]

_U = users_wallet
_L = wallet_ledger


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    user_id: int
    amount_minor: int
    balance_after: int
    reason: str
    ref_type: str
    ref_id: str


def _key(user_id: int, reason: str, ref_type: str, ref_id: str) -> sa.ColumnElement[bool]:
    return sa.and_(
        _L.c.user_id == user_id, _L.c.reason == reason, _L.c.ref_type == ref_type, _L.c.ref_id == ref_id
    )


async def _move(
    conn: AsyncConnection,
    user_id: int,
    amount_minor: int,
    *,
    reason: str,
    ref_type: str,
    ref_id: str,
    currency: str,
    guard: sa.ColumnElement[bool] | None = None,
    actor_id: int | None = None,
    note: str | None = None,
) -> Entry | None:
    check_entry(reason, amount_minor)
    ref_id = str(ref_id)
    dup = sa.select(sa.literal(1)).where(_key(user_id, reason, ref_type, ref_id)).exists()
    where = [_U.c.id == user_id, ~dup]
    if guard is not None:
        where.append(guard)
    upd = (
        sa.update(_U)
        .where(*where)
        .values(wallet_minor=_U.c.wallet_minor + amount_minor)
        .returning(_U.c.wallet_minor)
        .cte("upd")
    )
    ins = (
        sa.insert(_L)
        .from_select(
            [
                "user_id",
                "amount_minor",
                "currency",
                "balance_after",
                "reason",
                "ref_type",
                "ref_id",
                "actor_id",
                "note",
            ],
            sa.select(
                sa.literal(user_id, sa.BigInteger),
                sa.literal(amount_minor, sa.BigInteger),
                sa.literal(currency),
                upd.c.wallet_minor,
                sa.literal(reason),
                sa.literal(ref_type),
                sa.literal(ref_id),
                sa.literal(actor_id, sa.BigInteger),
                sa.literal(note[:300] if note else None, sa.Text),
            ),
        )
        .returning(_L.c.id, _L.c.balance_after)
    )
    row = (await conn.execute(ins)).first()
    if row is None:
        return None
    return Entry(int(row.id), user_id, amount_minor, int(row.balance_after), reason, ref_type, ref_id)


async def credit(
    conn: AsyncConnection,
    user_id: int,
    amount_minor: int,
    *,
    reason: str,
    ref_type: str,
    ref_id: str | int,
    currency: str,
    actor_id: int | None = None,
    note: str | None = None,
) -> Entry | None:
    """Add ``amount_minor`` (> 0); ``None`` if this ``(reason, ref)`` was already applied (or no such
    user)."""
    return await _move(
        conn,
        user_id,
        amount_minor,
        reason=reason,
        ref_type=ref_type,
        ref_id=str(ref_id),
        currency=currency,
        actor_id=actor_id,
        note=note,
    )


async def debit(
    conn: AsyncConnection,
    user_id: int,
    amount_minor: int,
    *,
    reason: str,
    ref_type: str,
    ref_id: str | int,
    currency: str,
    actor_id: int | None = None,
    note: str | None = None,
) -> Entry | None:
    """Take ``amount_minor`` (> 0) only if the balance covers it (CAS); ``None`` = not enough (or repeated
    key)."""
    if amount_minor <= 0:
        raise ValueError("debit amount must be positive")
    return await _move(
        conn,
        user_id,
        -amount_minor,
        reason=reason,
        ref_type=ref_type,
        ref_id=str(ref_id),
        currency=currency,
        guard=_U.c.wallet_minor >= amount_minor,
        actor_id=actor_id,
        note=note,
    )


async def take(
    conn: AsyncConnection,
    user_id: int,
    amount_minor: int,
    *,
    reason: str,
    ref_type: str,
    ref_id: str | int,
    currency: str,
    note: str | None = None,
) -> int:
    """Debit up to ``amount_minor`` (whatever the balance holds). Returns what was taken (0 = nothing).

    The caller must hold the user's row lock (:func:`lock_user`), so the balance it is based on is current."""
    if amount_minor <= 0:
        raise ValueError("amount must be positive")
    available = await balance(conn, user_id)
    part = min(available, amount_minor)
    if part <= 0:
        return 0
    entry = await debit(
        conn, user_id, part, reason=reason, ref_type=ref_type, ref_id=ref_id, currency=currency, note=note
    )
    return entry.amount_minor * -1 if entry is not None else 0


async def lock_user(conn: AsyncConnection, user_id: int) -> tuple[int, int | None] | None:
    """Lock the user's row (lock order everywhere in billing: ``payments`` → ``users`` → ``orders`` →
    ``subscriptions``). Returns ``(wallet_minor, telegram_id)`` or ``None`` for an unknown user."""
    row = (
        await conn.execute(
            sa.select(_U.c.wallet_minor, _U.c.telegram_id).where(_U.c.id == user_id).with_for_update()
        )
    ).first()
    return None if row is None else (int(row.wallet_minor), row.telegram_id)


async def balance(conn: AsyncConnection, user_id: int) -> int:
    value = await conn.scalar(sa.select(_U.c.wallet_minor).where(_U.c.id == user_id))
    return int(value or 0)


async def find(
    conn: AsyncConnection, user_id: int, reason: str, ref_type: str, ref_id: str | int
) -> Entry | None:
    row = (
        (await conn.execute(sa.select(_L).where(_key(user_id, reason, ref_type, str(ref_id)))))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Entry(
        int(row["id"]),
        user_id,
        int(row["amount_minor"]),
        int(row["balance_after"]),
        reason,
        ref_type,
        str(ref_id),
    )


async def credit_many(
    conn: AsyncConnection,
    user_ids: Iterable[int],
    amount_minor: int,
    *,
    reason: str,
    ref_type: str,
    ref_id: str | int,
    currency: str,
    note: str | None = None,
) -> list[Entry]:
    """One operation for many users (``bonus`` to a list, ``import_opening``): each user gets one entry;
    a repeated run with the same reference adds nothing. Users are processed in id order (stable lock
    order)."""
    out: list[Entry] = []
    for uid in sorted(set(user_ids)):
        entry = await credit(
            conn,
            uid,
            amount_minor,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
            currency=currency,
            note=note,
        )
        if entry is not None:
            out.append(entry)
    return out


async def adjust(
    conn: AsyncConnection,
    user_id: int,
    delta_minor: int,
    *,
    op_id: str,
    actor_id: int | None,
    role: str | None,
    reason: str,
    currency: str,
) -> Entry | None:
    """Admin correction (04 §9.1 ``wallet.adjust``): ± amount with a mandatory reason and an ``admin_audit``
    row
    in the same transaction. ``op_id`` (e.g. the callback's UUID) makes a double tap a no-op. A negative
    correction never takes the balance below zero (``None`` then). Rights are checked by the caller."""
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    guard = _U.c.wallet_minor >= -delta_minor if delta_minor < 0 else None
    entry = await _move(
        conn,
        user_id,
        delta_minor,
        reason="admin_adjust",
        ref_type="admin_op",
        ref_id=op_id,
        currency=currency,
        guard=guard,
        actor_id=actor_id,
        note=reason.strip(),
    )
    if entry is not None:
        await conn.execute(
            sa.insert(admin_audit).values(
                actor_id=actor_id,
                role=role,
                action="wallet.adjust",
                target=f"user:{user_id}",
                amount_minor=delta_minor,
                reason=reason.strip()[:500],
                details={"op_id": op_id, "balance_after": entry.balance_after},
            )
        )
    return entry


async def mismatches(conn: AsyncConnection, user_ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
    """Users whose balance differs from the sum of their ledger (should always be empty; «Состояние»)."""
    sums = (
        sa.select(_L.c.user_id, sa.func.sum(_L.c.amount_minor).label("total"))
        .group_by(_L.c.user_id)
        .subquery("sums")
    )
    stmt = (
        sa.select(_U.c.id, _U.c.wallet_minor, sa.func.coalesce(sums.c.total, 0).label("ledger"))
        .select_from(_U.outerjoin(sums, sums.c.user_id == _U.c.id))
        .where(_U.c.wallet_minor != sa.func.coalesce(sums.c.total, 0))
    )
    if user_ids is not None:
        stmt = stmt.where(_U.c.id.in_(list(user_ids)))
    rows = (await conn.execute(stmt)).all()
    return [
        {"user_id": int(r.id), "wallet_minor": int(r.wallet_minor), "ledger": int(r.ledger)} for r in rows
    ]
