"""Promo engine (01 §1.4, 07 §2.4.4): activation, pending discounts at checkout, redemption, the owner's CRUD.

* :meth:`PromoService.activate` — a code typed in the bot or carried by a deep link (``source='link'``). One
  transaction: the **user's** row is locked (taps of one user run one after another — «once per user» is
  exact), the promo and the user's facts are read in **one** statement, then either the effect is applied
  right there (days / wallet / trial / plan gift, idempotent by the use id: ``wallet_ledger`` key
  ``bonus/promo_use/<id>``, ``subscription_events`` ref ``promo_use/<id>``) or — for a discount — a use
  is **reserved** (``promo_uses`` row without an order, ``effect.reserved``) and the user gets a
  ``promo_pending`` row (one per user, a newer one replaces it and frees the older reservation). The last
  statement takes the use: ``UPDATE promocodes SET uses = uses + 1 WHERE uses < max_uses`` — the promo row
  is locked only from there to the commit, so a burst of taps on a popular code does not queue behind one
  transaction, and the total limit stays exact (a lost race rolls the whole activation back). Unknown codes
  are rate-limited per user (brute force). The creator of a code cannot activate it.
* :meth:`PromoService.checkout_discounts` — for ``CheckoutService.draft_plan(discounts=…)``: **0 SQL** for a
  user without a pending discount (in-memory index), one read otherwise. The holder of a reservation is not
  refused for «exhausted»: the use is already theirs.
* :meth:`PromoService.claim` — for ``CheckoutService.pay`` (in its transaction): binds the reservation to
  the order, so one activated discount pays for one order only; ``None`` or the refusal text.
* :meth:`PromoService.redeem_order` — after ``order.fulfilled`` (bus): the order's claimed or reserved use
  becomes the checkout use (``effect.discount_minor``); without a reservation (it expired, or the discount
  went into a second order before :meth:`claim` was wired) a use is taken anyway — the money is already
  paid — and marked ``over_limit``. :meth:`PromoService.sweep` repeats missed redemptions, frees claims of
  canceled / expired orders and reservations whose pending discount is gone (``uses − 1``).
* Owner CRUD re-reads the actor's role in the transaction (:func:`svbg.services.roles.load_actor`). Codes
  of :data:`~svbg.promo.rules.VALUE_KINDS` from an admin are held to ``ADMIN_WALLET_ADJUST_MAX`` /
  ``ADMIN_GRANT_DAYS_MAX`` per use, need a reason, and ``admin_audit.amount_minor`` = amount × max uses.

The process keeps the pending index in memory; every write of this service updates it after its commit.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.billing import wallet
from svbg.billing.tables import orders
from svbg.core.clock import now
from svbg.core.money import format_money
from svbg.core.tables import users
from svbg.promo.legacy import LegacyPromo
from svbg.promo.rules import (
    KIND_FIELDS,
    REFUSALS,
    REFUSALS_EN,
    VALUE_KINDS,
    Facts,
    Promo,
    PromoDiscount,
    PromoError,
    check_code,
    describe,
    discount_of,
    generate_code,
    normalize_input,
    pending_until,
    promo_ids_of,
    refusal,
    validate,
    validate_limits,
)
from svbg.promo.tables import promo_pending, promo_uses, promocodes
from svbg.services import roles
from svbg.services.roles import Act, Limits, RoleError
from svbg.subscriptions.lifecycle import LIVE_STATES, SubscriptionError, SubscriptionLifecycle
from svbg.subscriptions.tables import subscriptions, trial_grants
from svbg.subscriptions.trial import TrialRefused

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.bus import Event, EventBus
    from svbg.db.engine import Database

__all__ = [
    "Activation",
    "Actor",
    "PendingEntry",
    "PromoService",
    "PromoStats",
    "TrialGranter",
]

log = logging.getLogger("svbg.promo")

DAY_S: Final = 86_400
#: Unknown codes a user may try per window before «Слишком много попыток».
MAX_FAILED_ATTEMPTS: Final = 5
ATTEMPTS_WINDOW_S: Final = 600.0
_LIMITER_USERS: Final = 10_000
#: Refusals after which a pending discount can never apply again (it is dropped).
_FINAL_REFUSALS: Final = frozenset(
    {"inactive", "expired", "exhausted", "used", "not_new", "banned", "currency"}
)
#: Columns the owner may change with :meth:`PromoService.update`.
_LIMIT_FIELDS: Final = frozenset(
    {"title", "enabled", "max_uses", "once_per_user", "new_users_only", "starts_at", "expires_at", "code"}
)
_VALUE_FIELDS: Final = frozenset().union(*KIND_FIELDS.values())
#: Changes of a :data:`VALUE_KINDS` code that change what it gives away (limit check + reason).
_EXPOSURE_FIELDS: Final = frozenset({"days", "amount_minor", "max_uses"})
#: The admin right of 04 §9.1 that opens the promo editor.
PERM: Final = "promo"
DENIED: Final = "Нет прав"
_BIGINT_MAX: Final = 2**63 - 1
REDEEM_WINDOW: Final = timedelta(days=3)
#: A use reserved by an activated discount and not bound to an order yet.
_RESERVED: Final = sa.and_(promo_uses.c.order_id.is_(None), promo_uses.c.effect["reserved"].is_not(None))
#: An order snapshot with a promo discount (a constant: never built from input).
_PROMO_PATH: Final = """'$.discounts[*].source ? (@ starts with "promo:")'::jsonpath"""


class TrialGranter(Protocol):
    """``svbg.subscriptions.trial.TrialService.grant`` (raises ``TrialRefused``)."""

    async def grant(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        days: int,
        source: str = "bot",
        caused_by: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Actor:
    """Who presses (from the cached context): the service re-reads the role from the database."""

    user_id: int | None
    role: str | None = None
    telegram_id: int | None = None


@dataclass(frozen=True, slots=True)
class PendingEntry:
    """A discount waiting for checkout (in memory: shown on screens without SQL)."""

    promo_id: int
    code: str
    until: datetime
    label: str  # «−20 % на покупку»
    label_en: str = ""  # «−20% off your purchase»


@dataclass(frozen=True, slots=True)
class Activation:
    """``applied``: the effect is there. ``pending``: a discount waits for checkout. ``refused``: ``text``."""

    outcome: Literal["applied", "pending", "refused"]
    text: str
    promo: Promo | None = None
    reason: str | None = None
    until: datetime | None = None
    use_id: int | None = None


@dataclass(frozen=True, slots=True)
class PromoStats:
    uses: int
    users: int
    uses_7d: int
    discount_minor: int  # given as discounts at checkout
    pending: int  # discounts activated and waiting


class _Refuse(Exception):
    def __init__(self, reason: str, promo: Promo | None = None, text: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.promo = promo
        self.text = text or REFUSALS.get(reason, REFUSALS["inactive"])


class _Attempts:
    """Failed lookups per user in a sliding window (in memory, bounded)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self._limit = limit
        self._window = window_s
        self._by_user: OrderedDict[int, deque[float]] = OrderedDict()

    def _recent(self, user_id: int) -> deque[float]:
        q = self._by_user.get(user_id)
        if q is None:
            q = deque()
            self._by_user[user_id] = q
            while len(self._by_user) > _LIMITER_USERS:
                self._by_user.popitem(last=False)
        edge = time.monotonic() - self._window
        while q and q[0] < edge:
            q.popleft()
        return q

    def blocked(self, user_id: int) -> bool:
        return len(self._recent(user_id)) >= self._limit

    def fail(self, user_id: int) -> None:
        self._recent(user_id).append(time.monotonic())


def _money(amount: int, currency: str, lang: str = "ru") -> str:
    try:
        return format_money(amount, currency, lang)
    except (ValueError, KeyError):
        return f"{amount} {currency}"


def _money_en(amount: int, currency: str) -> str:
    return _money(amount, currency, "en")


class PromoService:
    def __init__(
        self,
        db: Database,
        *,
        catalog: Any | None = None,
        lifecycle: SubscriptionLifecycle | None = None,
        trial: TrialGranter | None = None,
        currency: Callable[[], str] = lambda: "RUB",
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        attempts_window_s: float = ATTEMPTS_WINDOW_S,
        limits: Callable[[], Limits] | None = None,
        owner_ids: Callable[[], Awaitable[Iterable[int]]] | None = None,
    ) -> None:
        self.db = db
        self.catalog = catalog
        self.lifecycle = lifecycle or SubscriptionLifecycle()
        self.trial = trial
        self._currency = currency
        self._timezone = timezone
        # Absent → the safe defaults: 31 days per use, no wallet codes from admins (owners are not limited).
        self._limits = limits or Limits
        self._owner_ids = owner_ids
        self._attempts = _Attempts(max_attempts, attempts_window_s)
        self._pending: dict[int, PendingEntry] = {}

    # ------------------------------------------------------------------------------------------ helpers

    @property
    def currency(self) -> str:
        return (self._currency() or "RUB").upper()

    @property
    def timezone(self) -> str:
        return self._timezone() or "Europe/Moscow"

    def plan_title(self, plan_id: int, lang: str = "ru") -> str | None:
        snap = getattr(self.catalog, "snapshot", None)
        plan = snap.plan(plan_id) if snap is not None else None
        return plan.title(lang) if plan is not None else None

    def describe(self, promo: Promo, lang: str = "ru") -> str:
        if lang == "en":
            return describe(
                promo, money=_money_en, plan_title=lambda plan_id: self.plan_title(plan_id, "en"), lang="en"
            )
        return describe(promo, money=_money, plan_title=self.plan_title)

    def localize(self, act: Activation, lang: str | None) -> str:
        """The text of an activation result in ``lang`` (Russian — the built one — otherwise)."""
        if lang != "en":
            return act.text
        if act.outcome == "refused":
            return REFUSALS_EN.get(act.reason or "", act.text)
        if act.promo is None:
            return act.text
        what = self.describe(act.promo, "en")
        if act.outcome == "pending" and act.until is not None:
            return (
                f"✅ Promo code {act.promo.code} accepted: {what}.\n"
                f"The discount applies at checkout until {_fmt_until(act.until, self.timezone)}."
            )
        return f"🎁 Promo code {act.promo.code} applied: {what}."

    async def load(self) -> int:
        """Fill the pending index (start-up); expired rows are deleted."""
        at = now()
        async with self.db.tx() as conn:
            await conn.execute(sa.delete(promo_pending).where(promo_pending.c.until <= at))
            rows = (
                (
                    await conn.execute(
                        sa.select(promo_pending.c.user_id, promo_pending.c.until, promocodes).join(
                            promocodes, promocodes.c.id == promo_pending.c.promo_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        self._pending = {int(r["user_id"]): self._entry(Promo.from_row(r), r["until"]) for r in rows}
        return len(self._pending)

    def _entry(self, promo: Promo, until: datetime) -> PendingEntry:
        return PendingEntry(promo.id, promo.code, until, self.describe(promo), self.describe(promo, "en"))

    def pending(self, user_id: int) -> PendingEntry | None:
        """The user's waiting discount (memory, 0 SQL)."""
        entry = self._pending.get(user_id)
        if entry is not None and entry.until <= now():
            self._pending.pop(user_id, None)
            return None
        return entry

    # --------------------------------------------------------------------------------------- activation

    @staticmethod
    def _activation_query(user_id: int, code: str) -> sa.Select[Any]:
        """The promo (by code, case-insensitive) and everything :func:`refusal` needs — one statement."""
        live = (
            sa.select(subscriptions.c.id, subscriptions.c.is_trial, subscriptions.c.plan_id)
            .where(subscriptions.c.user_id == users.c.id, subscriptions.c.link_state.in_(LIVE_STATES))
            .order_by(subscriptions.c.id.desc())
            .limit(1)
            .correlate(users)
            .lateral("live")
        )
        has_paid = (
            sa.select(sa.literal(1))
            .where(
                orders.c.user_id == users.c.id,
                orders.c.kind != "topup",
                orders.c.status.in_(("paid", "fulfilled")),
            )
            .correlate(users)
            .exists()
        )
        mine = sa.and_(promo_uses.c.promo_id == promocodes.c.id, promo_uses.c.user_id == users.c.id)
        used = sa.select(sa.literal(1)).where(mine, ~_RESERVED).correlate(users, promocodes).exists()
        reserved = sa.select(sa.literal(1)).where(mine, _RESERVED).correlate(users, promocodes).exists()
        had_trial = (
            sa.select(sa.literal(1))
            .where(
                sa.or_(
                    trial_grants.c.user_id == users.c.id,
                    sa.and_(
                        users.c.telegram_id.is_not(None), trial_grants.c.telegram_id == users.c.telegram_id
                    ),
                )
            )
            .correlate(users)
            .exists()
        )
        had_sub = (
            sa.select(sa.literal(1)).where(subscriptions.c.user_id == users.c.id).correlate(users).exists()
        )
        return (
            sa.select(
                promocodes,
                users.c.banned_at,
                has_paid.label("has_paid"),
                used.label("used"),
                reserved.label("reserved"),
                sa.or_(had_trial, had_sub).label("trial_used"),
                live.c.id.label("sub_id"),
                live.c.is_trial.label("live_trial"),
                live.c.plan_id.label("live_plan_id"),
            )
            .select_from(
                users.join(promocodes, sa.func.lower(promocodes.c.code) == code.lower()).outerjoin(
                    live, sa.true()
                )
            )
            .where(users.c.id == user_id)
        )

    async def activate(
        self, user_id: int, raw_code: Any, *, source: str = "bot", caused_by: str | None = None
    ) -> Activation:
        """Apply a code for the user (see the module docstring). Never raises for a user mistake."""
        if self._attempts.blocked(user_id):
            return Activation("refused", REFUSALS["too_many"], reason="too_many")
        code = normalize_input(raw_code)
        if code is None:
            self._attempts.fail(user_id)
            return Activation("refused", REFUSALS["not_found"], reason="not_found")
        try:
            async with self.db.tx() as conn:
                result, pending = await self._activate_tx(
                    conn, user_id=user_id, code=code, source=source, caused_by=caused_by
                )
        except _Refuse as r:
            if r.reason == "not_found":
                self._attempts.fail(user_id)
            return Activation("refused", r.text, r.promo, reason=r.reason)
        except SubscriptionError as e:
            return Activation("refused", e.text, reason=e.code)
        except TrialRefused as e:
            reason = "trial_used" if e.reason in ("used", "has_subscription") else "no_trial"
            return Activation("refused", REFUSALS[reason], reason=reason)
        if pending is not None:
            self._pending[user_id] = pending
        return result

    async def _activate_tx(
        self, conn: AsyncConnection, *, user_id: int, code: str, source: str, caused_by: str | None
    ) -> tuple[Activation, PendingEntry | None]:
        """The transactional part of :meth:`activate`; raises :class:`_Refuse` to roll back.

        Lock order ``users`` → ``promocodes`` (as everywhere): the user's row first (one activation per user
        at a time), the promo row only by the last statement (:meth:`_take`)."""
        at = now()
        if await conn.scalar(sa.select(users.c.id).where(users.c.id == user_id).with_for_update()) is None:
            raise _Refuse("not_found")
        # A new statement after the lock: it sees what the user's previous tap committed.
        f = (await conn.execute(self._activation_query(user_id, code))).mappings().first()
        if f is None:
            raise _Refuse("not_found")
        promo = Promo.from_row(f)
        if promo.created_by is not None and promo.created_by == user_id:
            raise _Refuse("own", promo)
        facts = Facts(
            has_paid=bool(f["has_paid"]),
            used=bool(f["used"]),
            live_sub=f["sub_id"] is not None,
            live_trial=bool(f["live_trial"]),
            live_plan_id=f["live_plan_id"],
            trial_used=bool(f["trial_used"]),
            banned=f["banned_at"] is not None,
        )
        reserved = promo.is_discount and bool(f["reserved"])
        # The holder of a reservation typing the code again: the use is theirs, «exhausted» does not apply.
        checked = replace(promo, max_uses=None) if reserved else promo
        reason = refusal(checked, facts, at, currency=self.currency)
        if reason is not None:
            raise _Refuse(reason, promo)
        if promo.is_discount:
            until = pending_until(promo, at)
            if not reserved:
                freed = await self._release(conn, user_id, keep=promo.id)
                await conn.execute(
                    sa.insert(promo_uses).values(
                        promo_id=promo.id,
                        user_id=user_id,
                        source=_use_source(source),
                        effect={"reserved": True},
                    )
                )
            values = {"promo_id": promo.id, "until": until, "source": _pending_source(source)}
            await conn.execute(
                pg_insert(promo_pending)
                .values(user_id=user_id, **values)
                .on_conflict_do_update(
                    index_elements=[promo_pending.c.user_id], set_={**values, "created_at": sa.func.now()}
                )
            )
            if not reserved:
                await self._recount(conn, freed, take=promo)
            text = (
                f"✅ Промокод {promo.code} принят: {self.describe(promo)}.\n"
                f"Скидка применится при оплате до {_fmt_until(until, self.timezone)}."
            )
            return Activation("pending", text, promo, until=until), self._entry(promo, until)
        use_id = int(
            (
                await conn.execute(
                    sa.insert(promo_uses)
                    .values(promo_id=promo.id, user_id=user_id, source=_use_source(source))
                    .returning(promo_uses.c.id)
                )
            ).scalar_one()
        )
        effect = await self._apply(conn, promo, user_id=user_id, facts=f, use_id=use_id, caused_by=caused_by)
        await conn.execute(sa.update(promo_uses).where(promo_uses.c.id == use_id).values(effect=effect))
        await self._recount(conn, Counter(), take=promo)
        text = f"🎁 Промокод {promo.code} применён: {self.describe(promo)}."
        return Activation("applied", text, promo, use_id=use_id), None

    @staticmethod
    async def _release(conn: AsyncConnection, user_id: int, *, keep: int | None = None) -> Counter[int]:
        """Delete the user's unbound reservations (except of ``keep``); returns ``promo_id → count``."""
        stmt = sa.delete(promo_uses).where(promo_uses.c.user_id == user_id, _RESERVED)
        if keep is not None:
            stmt = stmt.where(promo_uses.c.promo_id != keep)
        return Counter(int(p) for p in (await conn.execute(stmt.returning(promo_uses.c.promo_id))).scalars())

    @staticmethod
    async def _recount(conn: AsyncConnection, freed: Mapping[int, int], *, take: Promo | None = None) -> None:
        """``uses − n`` for freed reservations and — the last write of an activation — ``uses + 1`` for
        ``take`` only while under ``max_uses`` (a lost race raises ``exhausted``). Rows in id order: two
        users swapping codes never deadlock."""
        ids = sorted({*freed, *((take.id,) if take is not None else ())})
        for pid in ids:
            if take is not None and pid == take.id:
                taken = await conn.scalar(
                    sa.update(promocodes)
                    .where(
                        promocodes.c.id == pid,
                        promocodes.c.enabled.is_(True),
                        sa.or_(promocodes.c.max_uses.is_(None), promocodes.c.uses < promocodes.c.max_uses),
                    )
                    .values(uses=promocodes.c.uses + 1)
                    .returning(promocodes.c.id)
                )
                if taken is None:
                    raise _Refuse("exhausted", take)
            elif freed.get(pid):
                await conn.execute(
                    sa.update(promocodes)
                    .where(promocodes.c.id == pid)
                    .values(uses=sa.func.greatest(promocodes.c.uses - int(freed[pid]), 0))
                )

    async def _apply(
        self,
        conn: AsyncConnection,
        promo: Promo,
        *,
        user_id: int,
        facts: Mapping[str, Any],
        use_id: int,
        caused_by: str | None,
    ) -> dict[str, Any]:
        """The effect of an immediate kind, in the activation transaction; returns ``promo_uses.effect``."""
        ref = str(use_id)
        caused_by = caused_by or f"promo_use:{use_id}"
        effect: dict[str, Any] = {}
        if promo.kind in ("wallet", "wallet_days"):
            entry = await wallet.credit(
                conn,
                user_id,
                int(promo.amount_minor or 0),
                reason="bonus",
                ref_type="promo_use",
                ref_id=ref,
                currency=promo.currency or self.currency,
                note=f"Промокод {promo.code}"[:200],
            )
            if entry is None:
                raise _Refuse("not_found")
            effect["wallet_minor"] = promo.amount_minor
        if promo.kind in ("days", "wallet_days") or (promo.kind == "trial_extend" and facts["sub_id"]):
            applied = await self.lifecycle.extend(
                conn,
                int(facts["sub_id"]),
                int(promo.days or 0) * DAY_S,
                source="promo",
                ref_type="promo_use",
                ref_id=ref,
                reason=f"Промокод {promo.code}"[:200],
                caused_by=caused_by,
            )
            effect.update(days=promo.days, subscription_id=applied.subscription_id)
        elif promo.kind == "trial_extend":
            if self.trial is None:
                raise _Refuse("no_trial")
            granted = await self.trial.grant(
                conn, user_id=user_id, days=int(promo.days or 0), source="promo", caused_by=caused_by
            )
            effect.update(
                days=promo.days, trial=True, subscription_id=getattr(granted, "subscription_id", None)
            )
        elif promo.kind == "plan_gift":
            snap = getattr(self.catalog, "snapshot", None)
            plan = snap.plan(promo.plan_id) if snap is not None and promo.plan_id is not None else None
            if plan is None:
                raise _Refuse("plan_missing")
            try:
                applied = await self.lifecycle.purchase(
                    conn,
                    user_id=user_id,
                    terms=plan,
                    days=int(promo.days or 0),
                    ref_id=ref,
                    ref_type="promo_use",
                    source="promo",
                    caused_by=caused_by,
                )
            except (TypeError, ValueError) as e:
                log.warning("promo %s: the gift plan is unusable: %s", promo.id, e)
                raise _Refuse("plan_missing") from e
            effect.update(days=promo.days, plan_id=promo.plan_id, subscription_id=applied.subscription_id)
        return effect

    # --------------------------------------------------------------------------------------- checkout

    async def checkout_discounts(
        self, user_id: int, plan_id: int | None, lang: str = "ru"
    ) -> list[PromoDiscount]:
        """Discounts for a draft of ``plan_id`` (``CheckoutService.draft_plan(discounts=…)``)."""
        entry = self.pending(user_id)
        if entry is None:
            return []
        has_paid = (
            sa.select(sa.literal(1))
            .where(
                orders.c.user_id == user_id,
                orders.c.kind != "topup",
                orders.c.status.in_(("paid", "fulfilled")),
            )
            .exists()
        )
        mine = sa.and_(promo_uses.c.promo_id == promocodes.c.id, promo_uses.c.user_id == user_id)
        used = sa.select(sa.literal(1)).where(mine, ~_RESERVED).exists()
        reserved = sa.select(sa.literal(1)).where(mine, _RESERVED).exists()
        stmt = (
            sa.select(
                promocodes,
                has_paid.label("has_paid"),
                used.label("used"),
                reserved.label("reserved"),
                users.c.banned_at,
            )
            .select_from(promocodes.join(users, users.c.id == user_id))
            .where(promocodes.c.id == entry.promo_id)
        )
        async with self.db.read() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        if row is None:
            await self.drop_pending(user_id)
            return []
        promo = Promo.from_row(row)
        facts = Facts(
            has_paid=bool(row["has_paid"]),
            used=bool(row["used"]),
            live_sub=True,
            banned=row["banned_at"] is not None,
        )
        # The reservation holder already owns a use: the total limit was checked when the code was typed.
        checked = replace(promo, max_uses=None) if row["reserved"] else promo
        reason = refusal(checked, facts, now(), currency=self.currency)
        if reason is not None:
            if reason in _FINAL_REFUSALS:
                await self.drop_pending(user_id)
            return []
        found = discount_of(promo, plan_id)
        return [replace(found, lang=lang)] if found is not None else []

    async def drop_pending(self, user_id: int) -> None:
        """Forget the user's waiting discount and free its reserved use."""
        self._pending.pop(user_id, None)
        async with self.db.tx() as conn:
            await conn.execute(sa.select(users.c.id).where(users.c.id == user_id).with_for_update())
            await conn.execute(sa.delete(promo_pending).where(promo_pending.c.user_id == user_id))
            await self._recount(conn, await self._release(conn, user_id))

    async def claim(
        self,
        conn: AsyncConnection,
        *,
        order_id: int,
        user_id: int,
        snapshot: Mapping[str, Any] | None,
        lang: str = "ru",
    ) -> str | None:
        """Bind the user's reserved promo uses to ``order_id`` — in ``CheckoutService.pay``'s transaction,
        before the order leaves ``draft`` (the buyer's row is locked there). ``None``: fine (or no promo in
        the order); otherwise the refusal text — the discount already went into another order. Idempotent
        for the same order. :meth:`sweep` frees the claim if the order is canceled or expires."""
        for pid in promo_ids_of(snapshot):
            mine = sa.and_(promo_uses.c.promo_id == pid, promo_uses.c.order_id == order_id)
            if await conn.scalar(sa.select(promo_uses.c.id).where(mine)):
                continue
            pick = (
                sa.select(promo_uses.c.id)
                .where(promo_uses.c.promo_id == pid, promo_uses.c.user_id == user_id, _RESERVED)
                .order_by(promo_uses.c.id)
                .limit(1)
                .with_for_update()
                .scalar_subquery()
            )
            bound = await conn.scalar(
                sa.update(promo_uses)
                .where(promo_uses.c.id == pick)
                .values(order_id=order_id, effect={"claimed": True})
                .returning(promo_uses.c.id)
            )
            if bound is None:
                return (REFUSALS_EN if lang == "en" else REFUSALS)["claim_gone"]
        return None

    async def redeem_order(self, order_id: int) -> int:
        """Record the promo uses of a fulfilled order (idempotent). Returns the number of new uses."""
        recorded = 0
        async with self.db.tx() as conn:
            row = (
                await conn.execute(
                    sa.select(orders.c.user_id, orders.c.status, orders.c.snapshot).where(
                        orders.c.id == order_id
                    )
                )
            ).first()
            if row is None or row.status != "fulfilled":
                return 0
            snapshot = row.snapshot or {}
            ids = promo_ids_of(snapshot)
            if not ids:
                return 0
            user_id = int(row.user_id)
            amounts = {
                str(d.get("source")): d.get("amount_minor")
                for d in snapshot.get("discounts") or ()
                if isinstance(d, Mapping)
            }
            # Same lock order as an activation (users → promocodes): the reservation cannot move meanwhile.
            await conn.execute(sa.select(users.c.id).where(users.c.id == user_id).with_for_update())
            for pid in ids:
                effect = {"discount_minor": amounts.get(f"promo:{pid}")}
                if await self._redeem_one(conn, pid, user_id=user_id, order_id=order_id, effect=effect):
                    recorded += 1
            await conn.execute(
                sa.delete(promo_pending).where(
                    promo_pending.c.user_id == user_id, promo_pending.c.promo_id.in_(ids)
                )
            )
        entry = self._pending.get(user_id)
        if entry is not None and entry.promo_id in ids:
            self._pending.pop(user_id, None)
        return recorded

    async def _redeem_one(
        self, conn: AsyncConnection, pid: int, *, user_id: int, order_id: int, effect: dict[str, Any]
    ) -> bool:
        """One promo of a fulfilled order: claimed use → final; else the reservation; else a new use."""
        have = (
            await conn.execute(
                sa.select(promo_uses.c.id, promo_uses.c.effect).where(
                    promo_uses.c.promo_id == pid, promo_uses.c.order_id == order_id
                )
            )
        ).first()
        if have is not None:
            if not (isinstance(have.effect, Mapping) and have.effect.get("claimed")):
                return False  # redeemed before
            final = {"source": "checkout", "effect": effect}
            await conn.execute(sa.update(promo_uses).where(promo_uses.c.id == have.id).values(**final))
            return True
        pick = (
            sa.select(promo_uses.c.id)
            .where(promo_uses.c.promo_id == pid, promo_uses.c.user_id == user_id, _RESERVED)
            .order_by(promo_uses.c.id)
            .limit(1)
            .scalar_subquery()
        )
        bound = await conn.scalar(
            sa.update(promo_uses)
            .where(promo_uses.c.id == pick)
            .values(order_id=order_id, source="checkout", effect=effect)
            .returning(promo_uses.c.id)
        )
        if bound is not None:
            return True
        # No reservation: it expired before the payment, or the discount went into a second order. The money
        # is paid at the discounted price — the use is recorded truthfully and marked for the owner.
        counted = (
            await conn.execute(
                sa.update(promocodes)
                .where(promocodes.c.id == pid)
                .values(uses=promocodes.c.uses + 1)
                .returning(promocodes.c.uses, promocodes.c.max_uses, promocodes.c.once_per_user)
            )
        ).first()
        if counted is None:
            return False  # the promo is gone
        repeat = bool(counted.once_per_user) and bool(
            await conn.scalar(
                sa.select(
                    sa.select(sa.literal(1))
                    .where(promo_uses.c.promo_id == pid, promo_uses.c.user_id == user_id, ~_RESERVED)
                    .exists()
                )
            )
        )
        over = repeat or (counted.max_uses is not None and counted.uses > counted.max_uses)
        if over:
            effect = {**effect, "over_limit": True}
            log.warning("promo %s: order %s got the discount over the limit", pid, order_id)
        await conn.execute(
            pg_insert(promo_uses)
            .values(promo_id=pid, user_id=user_id, order_id=order_id, source="checkout", effect=effect)
            .on_conflict_do_nothing(
                index_elements=[promo_uses.c.promo_id, promo_uses.c.order_id],
                index_where=promo_uses.c.order_id.is_not(None),
            )
        )
        return True

    def install(self, bus: EventBus) -> Callable[[], None]:
        """Redeem on ``order.fulfilled`` (durable through the hook queue; :meth:`sweep` catches misses)."""

        async def on_fulfilled(event: Event) -> None:
            order_id = event.payload.get("order_id")
            if isinstance(order_id, bool) or not isinstance(order_id, int):
                return
            try:
                await self.redeem_order(order_id)
            except Exception:
                log.exception("promo redemption of order %s failed (the sweep retries)", order_id)

        return bus.subscribe("order.fulfilled", on_fulfilled)

    async def sweep(self, at: datetime | None = None) -> int:
        """Periodic: redeem fulfilled discounted orders that were missed, free claims of dead orders, drop
        expired pending discounts and free their reservations."""
        at = at or now()
        has_promo = sa.func.jsonb_path_exists(orders.c.snapshot, sa.literal_column(_PROMO_PATH))
        redeemed = (
            sa.select(sa.literal(1))
            .where(promo_uses.c.order_id == orders.c.id, promo_uses.c.effect["claimed"].is_(None))
            .correlate(orders)
            .exists()
        )
        async with self.db.read() as conn:
            ids = (
                (
                    await conn.execute(
                        sa.select(orders.c.id)
                        .where(
                            orders.c.status == "fulfilled",
                            orders.c.fulfilled_at >= at - REDEEM_WINDOW,
                            has_promo,
                            ~redeemed,
                        )
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
        done = 0
        for oid in ids:
            done += await self.redeem_order(int(oid))
        dead = sa.select(orders.c.id).where(orders.c.status.in_(("canceled", "expired")))
        live_pending = (
            sa.select(sa.literal(1))
            .where(
                promo_pending.c.user_id == promo_uses.c.user_id,
                promo_pending.c.promo_id == promo_uses.c.promo_id,
                promo_pending.c.until > at,
            )
            .exists()
        )
        async with self.db.tx() as conn:
            # A claimed use of an order that will never be paid is a reservation again.
            await conn.execute(
                sa.update(promo_uses)
                .where(promo_uses.c.effect["claimed"].is_not(None), promo_uses.c.order_id.in_(dead))
                .values(order_id=None, effect={"reserved": True})
            )
            gone = (
                (
                    await conn.execute(
                        sa.delete(promo_pending)
                        .where(promo_pending.c.until <= at)
                        .returning(promo_pending.c.user_id)
                    )
                )
                .scalars()
                .all()
            )
            freed = Counter(
                int(p)
                for p in (
                    await conn.execute(
                        sa.delete(promo_uses).where(_RESERVED, ~live_pending).returning(promo_uses.c.promo_id)
                    )
                ).scalars()
            )
            await self._recount(conn, freed)
        for uid in gone:
            entry = self._pending.get(int(uid))
            if entry is not None and entry.until <= at:
                self._pending.pop(int(uid), None)
        return done

    # -------------------------------------------------------------------------------------------- owner

    async def get(self, promo_id: int) -> Promo | None:
        async with self.db.read() as conn:
            row = (
                (await conn.execute(sa.select(promocodes).where(promocodes.c.id == promo_id)))
                .mappings()
                .first()
            )
        return Promo.from_row(row) if row is not None else None

    async def find(self, code: str) -> Promo | None:
        key = normalize_input(code)
        if key is None:
            return None
        async with self.db.read() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(promocodes).where(sa.func.lower(promocodes.c.code) == key.lower())
                    )
                )
                .mappings()
                .first()
            )
        return Promo.from_row(row) if row is not None else None

    async def page(self, offset: int = 0, limit: int = 10) -> tuple[list[Promo], int]:
        """Newest first, with the total count (one statement)."""
        stmt = (
            sa.select(promocodes, sa.func.count().over().label("total"))
            .order_by(promocodes.c.id.desc())
            .offset(max(0, offset))
            .limit(max(1, limit))
        )
        async with self.db.read() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        total = int(rows[0]["total"]) if rows else 0
        if not rows and offset > 0:
            async with self.db.read() as conn:
                total = int(await conn.scalar(sa.select(sa.func.count()).select_from(promocodes)) or 0)
        return [Promo.from_row(r) for r in rows], total

    async def stats(self, promo_id: int) -> PromoStats:
        week = now() - timedelta(days=7)
        u = promo_uses
        stmt = sa.select(
            sa.select(sa.func.count()).where(u.c.promo_id == promo_id).scalar_subquery(),
            sa.select(sa.func.count(sa.distinct(u.c.user_id)))
            .where(u.c.promo_id == promo_id)
            .scalar_subquery(),
            sa.select(sa.func.count()).where(u.c.promo_id == promo_id, u.c.used_at >= week).scalar_subquery(),
            sa.select(
                sa.func.coalesce(sa.func.sum(sa.cast(u.c.effect["discount_minor"].astext, sa.BigInteger)), 0)
            )
            .where(u.c.promo_id == promo_id, u.c.order_id.is_not(None))
            .scalar_subquery(),
            sa.select(sa.func.count())
            .where(promo_pending.c.promo_id == promo_id, promo_pending.c.until > now())
            .scalar_subquery(),
        )
        async with self.db.read() as conn:
            row = (await conn.execute(stmt)).one()
        return PromoStats(*(int(v or 0) for v in row))

    async def _staff(self, conn: AsyncConnection, actor: Actor, *, lock: bool = True) -> roles.Actor:
        """The actor's role read now, in this transaction (``FOR SHARE``): owner, or admin with ``promo``.
        A role revoked after the screen was drawn wins; the cached context is never trusted for a write."""
        owners = await self._owner_ids() if self._owner_ids is not None else ()
        fresh: roles.Actor | None = None
        if actor.telegram_id is not None:
            fresh = await roles.load_actor(conn, telegram_id=actor.telegram_id, owner_ids=owners, lock=lock)
        elif actor.user_id is not None:
            fresh = await roles.load_actor(conn, user_id=actor.user_id, owner_ids=owners, lock=lock)
        if fresh is None or not (fresh.is_owner or fresh.has_perm(PERM)):
            raise PromoError(DENIED)
        return fresh

    def _guard_value(
        self, staff: roles.Actor, values: Mapping[str, Any], max_uses: int | None, reason: str | None
    ) -> tuple[str, int | None]:
        """A :data:`VALUE_KINDS` code: the admin limits per use (owners are not limited) and a reason.
        Returns the reason and ``admin_audit.amount_minor`` — the money the code can give away in total
        (amount × max uses; one use when unlimited, ``max_uses`` goes to the details)."""
        limits = self._limits()
        amount, days = values.get("amount_minor"), values.get("days")
        try:
            if amount is not None:
                roles.check_limit(staff, Act.WALLET_ADJUST, int(amount), limits)
            if days is not None:
                roles.check_limit(staff, Act.SUBS_GRANT, int(days), limits)
            why = roles.require_reason(reason)
        except RoleError as e:
            raise PromoError(e.text) from None
        exposure = min(int(amount) * (max_uses or 1), _BIGINT_MAX) if amount is not None else None
        return why, exposure

    @staticmethod
    async def _audit(
        conn: AsyncConnection,
        staff: roles.Actor,
        action: str,
        promo_id: int,
        details: Any,
        *,
        amount_minor: int | None = None,
        reason: str | None = None,
    ) -> None:
        await roles.audit(
            conn,
            staff,
            action,
            target=f"promo:{promo_id}",
            amount_minor=amount_minor,
            reason=reason,
            details=_plain(details),
        )

    async def create(
        self,
        actor: Actor,
        *,
        kind: str,
        code: str | None,
        values: Mapping[str, Any],
        limits: Mapping[str, Any] | None = None,
        legacy: bool = False,
        reason: str | None = None,
    ) -> Promo:
        """A new promo (``code=None`` → generated). Raises :class:`PromoError` with an owner-facing text.
        A :data:`VALUE_KINDS` code needs ``reason`` and stays within the admin limits per use."""
        clean = validate(kind, {**values, "currency": values.get("currency") or self.currency})
        lim = dict(limits or {})
        unknown = set(lim) - _LIMIT_FIELDS
        if unknown:
            raise PromoError(f"Неизвестные поля: {', '.join(sorted(unknown))}")
        lim.pop("code", None)
        validate_limits(lim)
        for attempt in range(5):
            the_code = check_code(code, legacy=legacy) if code is not None else generate_code()
            try:
                async with self.db.tx() as conn:
                    staff = await self._staff(conn, actor)
                    why, exposure = (
                        self._guard_value(staff, clean, lim.get("max_uses"), reason)
                        if kind in VALUE_KINDS
                        else (None, None)
                    )
                    row = (
                        (
                            await conn.execute(
                                sa.insert(promocodes)
                                .values(code=the_code, kind=kind, created_by=staff.user_id, **clean, **lim)
                                .returning(promocodes)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    promo = Promo.from_row(row)
                    await self._audit(
                        conn,
                        staff,
                        "promo.create",
                        promo.id,
                        {"code": promo.code, "kind": kind, **clean, **lim},
                        amount_minor=exposure,
                        reason=why,
                    )
                return promo
            except IntegrityError as e:
                if "uq_promocodes_code_lower" not in str(e.orig):
                    raise
                if code is not None or attempt == 4:
                    raise PromoError("Такой код уже есть") from None
        raise PromoError("Такой код уже есть")  # pragma: no cover - loop always returns or raises

    async def update(
        self,
        promo_id: int,
        actor: Actor,
        *,
        expected_version: int | None = None,
        reason: str | None = None,
        **changes: Any,
    ) -> Promo:
        """Change limits or values (values are re-validated for the promo's kind). The code can be changed
        only before the first use. ``expected_version`` — compare-and-set (a stale edit raises). A change of
        what a :data:`VALUE_KINDS` code gives (amount, days, max uses) is checked like a new code."""
        unknown = set(changes) - _LIMIT_FIELDS - _VALUE_FIELDS
        if unknown:
            raise PromoError(f"Неизвестные поля: {', '.join(sorted(unknown))}")
        async with self.db.tx() as conn:
            staff = await self._staff(conn, actor)
            row = (
                (
                    await conn.execute(
                        # NO KEY: activations inserting ``promo_uses`` (foreign key) are not held up
                        sa.select(promocodes)
                        .where(promocodes.c.id == promo_id)
                        .with_for_update(key_share=True)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PromoError("Промокод не найден")
            promo = Promo.from_row(row)
            if expected_version is not None and promo.version != expected_version:
                raise PromoError("Промокод уже изменили — откройте его заново")
            values: dict[str, Any] = {}
            value_changes = {k: v for k, v in changes.items() if k in _VALUE_FIELDS}
            current = {k: row[k] for k in _VALUE_FIELDS}
            if value_changes:
                current.update(value_changes)
                values.update(validate(promo.kind, current))
            lim = {k: v for k, v in changes.items() if k in _LIMIT_FIELDS}
            why: str | None = None
            exposure: int | None = None
            if promo.kind in VALUE_KINDS and _EXPOSURE_FIELDS & set(changes):
                max_uses = lim.get("max_uses", promo.max_uses)
                why, exposure = self._guard_value(staff, values or current, max_uses, reason)
            if "code" in lim:
                new_code = check_code(lim["code"])
                if new_code != promo.code and promo.uses > 0:
                    raise PromoError("Код уже использовали — его нельзя переименовать")
            merged = {"starts_at": promo.starts_at, "expires_at": promo.expires_at, **lim}
            validate_limits(merged)
            if "title" in lim and lim["title"] is not None:
                lim["title"] = str(lim["title"]).strip()[:200] or None
            values.update(lim)
            try:
                async with conn.begin_nested():
                    new = (
                        (
                            await conn.execute(
                                sa.update(promocodes)
                                .where(promocodes.c.id == promo_id)
                                .values(**values, version=promocodes.c.version + 1, updated_at=sa.func.now())
                                .returning(promocodes)
                            )
                        )
                        .mappings()
                        .one()
                    )
            except IntegrityError as e:
                if "uq_promocodes_code_lower" in str(e.orig):
                    raise PromoError("Такой код уже есть") from None
                raise
            await self._audit(
                conn, staff, "promo.update", promo_id, changes, amount_minor=exposure, reason=why
            )
        updated = Promo.from_row(new)
        self._refresh_pending_labels(updated)
        return updated

    def _refresh_pending_labels(self, promo: Promo) -> None:
        for uid, entry in list(self._pending.items()):
            if entry.promo_id == promo.id:
                self._pending[uid] = PendingEntry(
                    promo.id, promo.code, entry.until, self.describe(promo), self.describe(promo, "en")
                )

    async def delete(self, promo_id: int, actor: Actor) -> None:
        """Only an unused promo can be deleted; a used one is switched off instead (history stays)."""
        async with self.db.tx() as conn:
            staff = await self._staff(conn, actor)
            row = (
                await conn.execute(
                    sa.select(promocodes.c.code, promocodes.c.uses)
                    .where(promocodes.c.id == promo_id)
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise PromoError("Промокод не найден")
            used = await conn.scalar(
                sa.select(sa.func.count()).select_from(promo_uses).where(promo_uses.c.promo_id == promo_id)
            )
            if used or row.uses:
                raise PromoError("Промокод уже использовали — его можно только выключить")
            await conn.execute(sa.delete(promocodes).where(promocodes.c.id == promo_id))
            await self._audit(conn, staff, "promo.delete", promo_id, {"code": row.code})
        for uid, entry in list(self._pending.items()):
            if entry.promo_id == promo_id:
                self._pending.pop(uid, None)

    async def toggle_plan(self, promo_id: int, plan_id: int, actor: Actor) -> Promo:
        """Add / remove ``plan_id`` in a discount's allowed plans (empty = all plans)."""
        promo = await self.get(promo_id)
        if promo is None:
            raise PromoError("Промокод не найден")
        plans = set(promo.plan_ids)
        plans.symmetric_difference_update({plan_id})
        return await self.update(promo_id, actor, plan_ids=sorted(plans))

    # ------------------------------------------------------------------------------------------- import

    @staticmethod
    async def import_legacy(conn: AsyncConnection, legacy: LegacyPromo) -> int:
        """Insert or refresh an imported promo (keyed by ``(source='import', legacy_id)``); returns its id.
        Uses go to ``promo_uses`` with ``source='import'``; call :meth:`recount_uses` afterwards."""
        values = {
            "code": legacy.code,
            "kind": legacy.kind,
            "source": "import",
            "legacy_id": legacy.legacy_id,
            **legacy.values,
            **legacy.limits,
        }
        stmt = pg_insert(promocodes).values(**values)
        if legacy.legacy_id is not None:
            update = {k: v for k, v in values.items() if k not in ("source", "legacy_id")}
            stmt = stmt.on_conflict_do_update(
                index_elements=[promocodes.c.source, promocodes.c.legacy_id],
                index_where=promocodes.c.legacy_id.is_not(None),
                set_={**update, "updated_at": sa.func.now()},
            )
        return int((await conn.execute(stmt.returning(promocodes.c.id))).scalar_one())

    async def recount_uses(self, conn: AsyncConnection) -> None:
        """After an import: ``uses = COUNT(promo_uses)`` (06 §2.6)."""
        counted = (
            sa.select(sa.func.count())
            .where(promo_uses.c.promo_id == promocodes.c.id)
            .correlate(promocodes)
            .scalar_subquery()
        )
        await conn.execute(sa.update(promocodes).values(uses=counted))


def _pending_source(source: str) -> str:
    return source if source in ("bot", "link", "admin", "import") else "bot"


def _use_source(source: str) -> str:
    return source if source in ("bot", "link", "checkout", "admin", "import", "site") else "bot"


def _fmt_until(at: datetime, tz: str) -> str:
    try:
        local = at.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return at.strftime("%d.%m.%Y %H:%M UTC")
    return local.strftime("%d.%m.%Y %H:%M")


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value
