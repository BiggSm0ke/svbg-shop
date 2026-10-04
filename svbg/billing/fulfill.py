"""Fulfill a paid order and show the result in the same message (07 §4.5 step 5).

Jobs (register :meth:`Fulfiller.handlers` with the worker):

* ``billing.fulfill`` (``dedup_key = fulfill:order:<id>``, lane ``interactive``) — one transaction: lock the
  user and the order, ``paid → fulfilled`` (CAS) after :mod:`svbg.subscriptions.lifecycle` applied the term
  (``paid_until``, ``subscription_events`` with ``ref order/<id>``, the panel job through the writer), module
  items (X4) fulfilled after the term, ``order.fulfilled`` announced (durable), and the UI jobs queued. A
  repeated or concurrent run finds the order ``fulfilled`` (or the journal row) and changes nothing — exactly
  once. A frozen or banned user's order goes ``held`` (X5): the user is told the money is safe, the owner gets
  «Требует внимания» with a button to the decision (:meth:`Fulfiller.resolve_held`: «Зачесть в заморозку» /
  «Вернуть на кошелёк», rights re-checked, ``admin_audit``); an undecided ``held`` purchase is refunded to the
  wallet after :data:`HELD_TTL`. A refusal of the subscription service refunds the money.
* ``billing.ui`` — runs **after the panel jobs of the subscription** (same ``ordering_key`` ``sub:<id>``:
  strict FIFO): edits ``ui_ref`` into «✅ Оплачено, подписка до …» + «🔗 Подключиться»; a message older than
  48 h or gone → a new message. If the panel user is still not there, the message says «подключаем…» and the
  job ends — a **keyless** re-check job (``billing.ui_check:<id>``) waits for the panel user, so a dead panel
  job never parks the subscription's queue behind a retrying UI job.
* ``billing.ui_progress`` — a few seconds after fulfill: if the result is not shown yet (the panel is slow or
  down), shows «✅ Оплачено, подключаем…».
* ``billing.notice`` — «💰 Зачислено…» and other user notices (edit the payment message or send a new one).
* ``billing.attention`` — «Требует внимания» items raised after the business transaction committed.

**Telegram is never called inside a transaction.** The UI jobs are optimistic: read the order (its
``updated_at`` is the version), call Telegram, then ``UPDATE … WHERE updated_at = <version>``. Whoever loses
that CAS bumps the version (so a job still in flight re-checks too) and re-shows what the order's stage says —
the progress job can never leave «подключаем…» over the final message, and nothing holds a row lock or a pool
connection while Telegram answers (a payment webhook touching the same order is never kept waiting).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa

from svbg.billing import texts, wallet
from svbg.billing.config import BillingConfig, ConfigSource
from svbg.billing.crediting import held_fix_action
from svbg.billing.kinds import (
    ATTENTION_JOB,
    FULFILL_JOB,
    JOB_QUEUE,
    NOTICE_JOB,
    UI_JOB,
    UI_PROGRESS_JOB,
    enqueue_attention,
    enqueue_fulfill,
    enqueue_notice,
)
from svbg.billing.ports import AttentionPort, Messenger, Notice, UiRef
from svbg.billing.tables import order_items, orders, users_wallet
from svbg.core.clock import now
from svbg.domain.pricing import ITEM_DEVICES, ITEM_PLAN
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import Handler, PermanentJobError, RetryJob
from svbg.remnawave.writer import ordering_key
from svbg.services import roles
from svbg.subscriptions import hooks
from svbg.subscriptions.hold import spend_check
from svbg.subscriptions.lifecycle import SubscriptionError, SubscriptionLifecycle
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext

__all__ = [
    "BUILTIN_ITEMS",
    "EDIT_MAX_AGE",
    "HELD_PERM",
    "HELD_TTL",
    "PROGRESS_DELAY",
    "Fulfiller",
    "HeldResolution",
    "OrderItemHandler",
]

log = logging.getLogger("svbg.billing")

#: Telegram does not reliably edit older messages; a new message is sent instead (07 §4.5 step 5).
EDIT_MAX_AGE: Final = timedelta(hours=48)
#: How long the user may look at «⏳ Оформляю…» before the message says «подключаем…».
PROGRESS_DELAY: Final = timedelta(seconds=4)
#: Re-check of a purchase whose panel user is not there yet (panel job dead / panel user missing).
UI_RECHECK_S: Final = 60.0
UI_MAX_ATTEMPTS: Final = 1440  # ~1 day of re-checks at UI_RECHECK_S
MESSENGER_TIMEOUT_S: Final = 20.0
#: Optimistic UI rounds before the job retries (each lost round means another job changed the message).
UI_CAS_ROUNDS: Final = 4
BUILTIN_ITEMS: Final = (ITEM_PLAN, ITEM_DEVICES)
#: An undecided ``held`` purchase goes back to the wallet after this long (money is never stuck).
HELD_TTL: Final = timedelta(days=7)
#: The admin right needed to decide a ``held`` purchase (owners have every right).
HELD_PERM: Final = "wallet.adjust"
_HELD_BATCH: Final = 100

_Mode = Literal["final", "progress"]
_BUILTIN_KINDS: Final = ("new", "renew", "change", "addon_devices")
_TICK: Final = sa.literal(timedelta(microseconds=1), sa.Interval)


class OrderItemHandler(Protocol):
    """X4: a module's order position. ``fulfill`` runs in the fulfill transaction **after** the term was
    applied; raising :class:`SubscriptionError` refunds the whole order."""

    async def fulfill(
        self, conn: AsyncConnection, order: Mapping[str, Any], item: Mapping[str, Any]
    ) -> None: ...


class OrderKindHandler(Protocol):
    """X4: a module order kind (``addon_lte``): applied in the fulfill transaction instead of a plan term;
    returns the subscription id. Raising :class:`SubscriptionError` refunds the whole order."""

    async def apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class HeldResolution:
    order_id: int
    action: str  # "credit_hold" | "refund" | "noop" | "insufficient"
    refunded_minor: int = 0


class Fulfiller:
    def __init__(
        self,
        db: Database,
        *,
        config: ConfigSource,
        lifecycle: SubscriptionLifecycle | None = None,
        messenger: Messenger | None = None,
        attention: AttentionPort | None = None,
        item_handlers: Mapping[str, OrderItemHandler] | None = None,
        progress_delay: timedelta = PROGRESS_DELAY,
    ) -> None:
        self._db = db
        self._config = config
        self.lifecycle = lifecycle or SubscriptionLifecycle()
        self.messenger = messenger
        self._attention = attention
        self._items = dict(item_handlers or {})
        self._kinds: dict[str, OrderKindHandler] = {}
        self._progress_delay = progress_delay

    @property
    def config(self) -> BillingConfig:
        return BillingConfig.from_mapping(self._config())

    def register_item(self, item_type: str, handler: OrderItemHandler) -> None:
        if item_type in BUILTIN_ITEMS:
            raise ValueError(f"{item_type} is a built-in item type")
        self._items[item_type] = handler

    def register_kind(self, kind: str, handler: OrderKindHandler) -> None:
        """A module order kind (``ORDER_KINDS`` must allow it: ``addon_lte``)."""
        if kind in (*_BUILTIN_KINDS, "topup"):
            raise ValueError(f"{kind} is a built-in order kind")
        self._kinds[kind] = handler

    def handlers(self) -> dict[str, Handler]:
        return {
            FULFILL_JOB: self.fulfill_job,
            UI_JOB: self.ui_job,
            UI_PROGRESS_JOB: self.progress_job,
            NOTICE_JOB: self.notice_job,
            ATTENTION_JOB: self.attention_job,
        }

    # ------------------------------------------------------------------------------------------ fulfill

    async def fulfill_job(self, job: Job, ctx: JobContext) -> None:
        order_id = _order_id(job)
        await self.fulfill(order_id, allow_frozen=bool(job.payload.get("allow_frozen")))

    async def fulfill(self, order_id: int, *, allow_frozen: bool = False) -> str:
        """Apply a paid order. Returns the order status afterwards (``fulfilled``, ``held``,
        ``canceled``…)."""
        async with self._db.read() as conn:
            user_id = await conn.scalar(sa.select(orders.c.user_id).where(orders.c.id == order_id))
        if user_id is None:
            raise PermanentJobError(f"order {order_id} does not exist")
        async with self._db.tx() as conn:
            if await wallet.lock_user(conn, int(user_id)) is None:
                raise PermanentJobError(f"user {user_id} does not exist")
            order = (
                (await conn.execute(sa.select(orders).where(orders.c.id == order_id).with_for_update()))
                .mappings()
                .one()
            )
            if order["status"] != "paid" or order["kind"] == "topup":
                return str(order["status"])  # exactly once: fulfilled before, or held / canceled meanwhile
            if not allow_frozen and not await self._may_spend(conn, int(user_id)):
                await self._hold(conn, order)
                return "held"
            items = (
                (
                    await conn.execute(
                        sa.select(order_items)
                        .where(order_items.c.order_id == order_id)
                        .order_by(order_items.c.position)
                    )
                )
                .mappings()
                .all()
            )
            try:
                async with conn.begin_nested():  # a refusal undoes only the partial subscription change
                    sub_id = await self._apply(conn, order, items)
            except SubscriptionError as e:
                await self._refund(conn, order, e.text)
                return "canceled"
            at = now()
            await conn.execute(
                sa.update(order_items)
                .where(order_items.c.order_id == order_id, order_items.c.status == "pending")
                .values(status="fulfilled")
            )
            await conn.execute(
                sa.update(orders)
                .where(orders.c.id == order_id)
                .values(status="fulfilled", fulfilled_at=at, subscription_id=sub_id, updated_at=at)
            )
            await hooks.emit(
                conn,
                "order.fulfilled",
                {
                    "order_id": order_id,
                    "user_id": int(user_id),
                    "kind": order["kind"],
                    "subscription_id": sub_id,
                    "total_minor": int(order["total_minor"]),
                    "currency": order["currency"],
                },
                lane="interactive",
                caused_by=f"order:{order_id}",
            )
            await enqueue(
                conn,
                UI_JOB,
                {"order_id": order_id},
                queue=JOB_QUEUE,
                lane="interactive",
                ordering_key=ordering_key(sub_id),  # after the panel jobs of this subscription (FIFO)
                dedup_key=f"billing.ui:{order_id}",
                max_attempts=20,
                caused_by=f"order:{order_id}",
            )
            await enqueue(
                conn,
                UI_PROGRESS_JOB,
                {"order_id": order_id},
                queue=JOB_QUEUE,
                lane="interactive",
                run_at=at + self._progress_delay,
                dedup_key=f"billing.ui_progress:{order_id}",
                max_attempts=5,
                caused_by=f"order:{order_id}",
            )
        return "fulfilled"

    async def _may_spend(self, conn: AsyncConnection, user_id: int) -> bool:
        held = (
            sa.select(subscriptions.c.hold_kind)
            .where(subscriptions.c.user_id == user_id, subscriptions.c.hold_kind.is_not(None))
            .limit(1)
            .scalar_subquery()
        )
        row = (
            await conn.execute(
                sa.select(users_wallet.c.banned_at, held.label("hold_kind")).where(
                    users_wallet.c.id == user_id
                )
            )
        ).first()
        return row is not None and bool(spend_check(banned_at=row.banned_at, hold_kind=row.hold_kind))

    async def _apply(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: list[Mapping[str, Any]]
    ) -> int:
        snap = order["snapshot"] or {}
        kind = order["kind"]
        ref = str(order["id"])
        caused_by = f"order:{order['id']}"
        if kind in ("new", "renew", "change"):
            plan = snap.get("plan")
            days = snap.get("days")
            if not isinstance(plan, Mapping) or not isinstance(days, int):
                raise SubscriptionError("bad_order", "Заказ повреждён.")
            try:
                applied = await self.lifecycle.purchase(
                    conn,
                    user_id=int(order["user_id"]),
                    terms=plan,
                    days=days,
                    ref_id=ref,
                    extra_devices=int(snap.get("extra_devices") or 0),
                    caused_by=caused_by,
                )
            except (TypeError, ValueError) as e:
                raise SubscriptionError("bad_order", "Тариф заказа повреждён.") from e
        elif kind == "addon_devices":
            sid = order["subscription_id"] or snap.get("subscription_id")
            if not isinstance(sid, int):
                raise SubscriptionError("bad_order", "Заказ повреждён.")
            applied = await self.lifecycle.add_devices(
                conn, sid, int(snap.get("extra_devices") or 0), ref_id=ref, caused_by=caused_by
            )
        else:
            kind_handler = self._kinds.get(str(kind))
            if kind_handler is None:
                raise SubscriptionError("bad_order", "Неизвестный вид заказа.")
            sub_id = await kind_handler.apply(conn, order, items)
            await self._fulfill_items(conn, order, items)
            return sub_id
        await self._fulfill_items(conn, order, items)
        return applied.subscription_id

    async def _fulfill_items(
        self, conn: AsyncConnection, order: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
    ) -> None:
        for item in items:
            if item["type"] in BUILTIN_ITEMS:
                continue
            handler = self._items.get(str(item["type"]))
            if handler is None:
                raise SubscriptionError("bad_order", "Позиция заказа сейчас недоступна.")
            await handler.fulfill(conn, order, item)

    async def _hold(self, conn: AsyncConnection, order: Mapping[str, Any]) -> None:
        """A paid (money taken) purchase of a frozen / banned user waits for the owner's decision: the user
        learns the money is safe, the owner gets the item with the decision button."""
        at = now()
        await conn.execute(
            sa.update(orders)
            .where(orders.c.id == order["id"])
            .values(status="held", note="frozen", updated_at=at)
        )
        snap = order["snapshot"] or {}
        title = str(snap.get("title") or "")
        await enqueue_notice(
            conn,
            {
                "type": "held",
                "user_id": order["user_id"],
                "ui_ref": order["ui_ref"],
                "title": title,
                "currency": order["currency"],
            },
            dedup_key=f"notice:held:{order['id']}",
        )
        await enqueue_attention(
            conn,
            f"billing:held:{order['id']}",
            "warn",
            texts.ATTENTION["fulfill_held_title"],
            texts.ATTENTION["fulfill_held_body"].format(
                order=order["id"],
                title=title,
                amount=texts.money(int(order["total_minor"]), str(order["currency"])),
                user=order["user_id"],
            ),
            fix_action=held_fix_action(int(order["id"])),
        )

    async def _refund(
        self, conn: AsyncConnection, order: Mapping[str, Any], reason: str, *, alert: bool = True
    ) -> int:
        """Give the money of a paid order back to the wallet (refused by the subscription service; a ``held``
        purchase canceled — ``alert=False``: the owner decided it or is told separately)."""
        at = now()
        amount = int(order["total_minor"])
        balance = 0
        if (
            amount > 0
            and await wallet.find(conn, order["user_id"], "purchase", "order", order["id"]) is not None
        ):
            entry = await wallet.credit(
                conn,
                int(order["user_id"]),
                amount,
                reason="purchase_refund",
                ref_type="order",
                ref_id=order["id"],
                currency=str(order["currency"]),
                note=reason,
            )
            balance = (
                entry.balance_after
                if entry is not None
                else await wallet.balance(conn, int(order["user_id"]))
            )
        else:
            amount = 0
            balance = await wallet.balance(conn, int(order["user_id"]))
        await conn.execute(
            sa.update(orders)
            .where(orders.c.id == order["id"])
            .values(status="canceled", note=f"refused: {reason}"[:300], updated_at=at)
        )
        snap = order["snapshot"] or {}
        if amount > 0:
            await enqueue_notice(
                conn,
                {
                    "type": "refunded",
                    "user_id": order["user_id"],
                    "ui_ref": order["ui_ref"],
                    "title": snap.get("title") or "",
                    "reason": reason,
                    "amount_minor": amount,
                    "balance_minor": balance,
                    "currency": order["currency"],
                },
                dedup_key=f"notice:refund:{order['id']}",
            )
        if amount > 0 and alert:
            await enqueue_attention(
                conn,
                f"billing:fulfill_failed:{order['id']}",
                "warn",
                texts.ATTENTION["fulfill_failed_title"],
                texts.ATTENTION["fulfill_failed_body"].format(
                    order=order["id"],
                    reason=reason,
                    amount=texts.money(amount, str(order["currency"])),
                    user=order["user_id"],
                ),
            )
        return amount

    # ------------------------------------------------------------------------------------ held orders

    async def resolve_held(
        self,
        order_id: int,
        action: str,
        *,
        actor_telegram_id: int,
        owner_ids: Iterable[int] = (),
        reason: str | None = None,
    ) -> HeldResolution:
        """The owner's decision on a ``held`` purchase (05 §2.4.2): ``credit_hold`` — buy it anyway (the term
        goes to the frozen balance and arrives on unfreeze), ``refund`` — cancel it (money back on the wallet
        if it was taken). The presser's rights are read **now** (owner, or an admin with :data:`HELD_PERM`);
        a refusal is audited and raises :class:`svbg.services.roles.RoleError` («Нет прав»); a decision is
        written to ``admin_audit`` in the same transaction. A second press is a ``noop``."""
        if action not in ("credit_hold", "refund"):
            raise ValueError("action must be credit_hold or refund")
        async with self._db.read() as conn:
            user_id = await conn.scalar(sa.select(orders.c.user_id).where(orders.c.id == order_id))
        if user_id is None:
            raise ValueError("no such order")
        denied = False
        result = HeldResolution(order_id, "noop")
        async with self._db.tx() as conn:
            actor = await roles.load_actor(
                conn, telegram_id=actor_telegram_id, owner_ids=owner_ids, lock=True
            )
            if actor is None or not actor.has_perm(HELD_PERM):
                denied = True
                await roles.audit(
                    conn,
                    actor,
                    "billing.held.denied",
                    target=f"order:{order_id}",
                    details={"telegram_id": actor_telegram_id, "decision": action},
                )
            else:
                result = await self._decide_held(
                    conn, order_id, int(user_id), action, "заказ отменён администратором"
                )
                if result.action in ("refund", "credit_hold"):
                    await roles.audit(
                        conn,
                        actor,
                        f"billing.held.{result.action}",
                        target=f"order:{order_id}",
                        amount_minor=await self._order_total(conn, order_id),
                        reason=roles.require_reason(reason) if reason else "решение по замороженному заказу",
                        details={"user_id": int(user_id), "refunded_minor": result.refunded_minor},
                    )
        if denied:
            raise roles.RoleError("denied", roles.DENIED)
        if result.action in ("refund", "credit_hold"):
            await self._resolve_attention(order_id)
        return result

    async def _decide_held(
        self, conn: AsyncConnection, order_id: int, user_id: int, action: str, why: str
    ) -> HeldResolution:
        await wallet.lock_user(conn, user_id)
        order = (
            (await conn.execute(sa.select(orders).where(orders.c.id == order_id).with_for_update()))
            .mappings()
            .one()
        )
        if order["status"] != "held":
            return HeldResolution(order_id, "noop")
        debited = await wallet.find(conn, user_id, "purchase", "order", order_id) is not None
        price = int(order["total_minor"])
        if action == "refund":
            refunded = await self._refund(conn, order, why, alert=False) if debited else 0
            if not debited:
                await conn.execute(
                    sa.update(orders)
                    .where(orders.c.id == order_id)
                    .values(status="canceled", note="held: canceled", updated_at=now())
                )
            return HeldResolution(order_id, "refund", refunded)
        if not debited and price > 0:
            entry = await wallet.debit(
                conn,
                user_id,
                price,
                reason="purchase",
                ref_type="order",
                ref_id=order_id,
                currency=str(order["currency"]),
            )
            if entry is None:
                return HeldResolution(order_id, "insufficient")
        await conn.execute(
            sa.update(orders)
            .where(orders.c.id == order_id)
            .values(status="paid", paid_at=order["paid_at"] or now(), updated_at=now())
        )
        await enqueue_fulfill(conn, order_id, allow_frozen=True)
        return HeldResolution(order_id, "credit_hold")

    @staticmethod
    async def _order_total(conn: AsyncConnection, order_id: int) -> int | None:
        value = await conn.scalar(sa.select(orders.c.total_minor).where(orders.c.id == order_id))
        return int(value) if value else None

    async def _resolve_attention(self, order_id: int) -> None:
        resolve = getattr(self._attention, "resolve", None)
        if resolve is None:
            return
        try:
            await resolve(f"billing:held:{order_id}")
        except Exception:  # the decision is committed; a stale item is only noise
            log.warning("billing: attention item of held order %s not resolved", order_id, exc_info=True)

    async def expire_held(self, at: datetime | None = None) -> int:
        """Safety net (sweeper): a ``held`` purchase nobody decided for :data:`HELD_TTL` is refunded to the
        wallet (taken money) or canceled (not taken), with a notice to the user and a note to the owner."""
        at = at or now()
        async with self._db.read() as conn:
            rows = (
                await conn.execute(
                    sa.select(orders.c.id, orders.c.user_id)
                    .where(orders.c.status == "held", orders.c.updated_at < at - HELD_TTL)
                    .order_by(orders.c.id)
                    .limit(_HELD_BATCH)
                )
            ).all()
        done = 0
        for row in rows:
            async with self._db.tx() as conn:
                order = (
                    (await conn.execute(sa.select(orders).where(orders.c.id == row.id))).mappings().first()
                )
                result = await self._decide_held(
                    conn, int(row.id), int(row.user_id), "refund", f"нет решения {HELD_TTL.days} дн."
                )
                if result.action != "refund" or order is None:
                    continue
                await roles.audit(
                    conn,
                    None,
                    "billing.held.expired",
                    target=f"order:{row.id}",
                    amount_minor=result.refunded_minor or None,
                    reason=f"нет решения {HELD_TTL.days} дн." if result.refunded_minor else None,
                    details={"user_id": int(row.user_id)},
                )
                await enqueue_attention(
                    conn,
                    f"billing:held_expired:{row.id}",
                    "info",
                    texts.ATTENTION["held_expired_title"],
                    texts.ATTENTION["held_expired_body"].format(
                        order=row.id,
                        title=(order["snapshot"] or {}).get("title") or "",
                        user=row.user_id,
                        days=HELD_TTL.days,
                        amount=texts.money(result.refunded_minor, str(order["currency"])),
                    ),
                )
            done += 1
            await self._resolve_attention(int(row.id))
        return done

    # ------------------------------------------------------------------------------------------ UI

    def _ui_query(self, order_id: int) -> sa.Select[Any]:
        return (
            sa.select(
                orders.c.id,
                orders.c.status,
                orders.c.ui_ref,
                orders.c.ui_stage,
                orders.c.snapshot,
                orders.c.currency,
                orders.c.subscription_id,
                orders.c.updated_at,
                users_wallet.c.telegram_id,
                users_wallet.c.wallet_minor,
                subscriptions.c.link_state,
                subscriptions.c.subscription_url,
                subscriptions.c.paid_until,
                subscriptions.c.hold_kind,
            )
            .select_from(
                orders.join(users_wallet, users_wallet.c.id == orders.c.user_id).outerjoin(
                    subscriptions, subscriptions.c.id == orders.c.subscription_id
                )
            )
            .where(orders.c.id == order_id)
        )

    async def ui_job(self, job: Job, ctx: JobContext) -> None:
        """Final message after the panel jobs (FIFO by ``sub:<id>``); a not-ready panel user hands over to a
        keyless re-check (``payload.recheck``) instead of retrying under the subscription's ordering key."""
        order_id = _order_id(job)
        shown = await self._show(order_id, "final")
        if shown != "waiting":
            return
        if job.payload.get("recheck"):
            raise RetryJob(UI_RECHECK_S, "пользователь панели ещё не готов")
        async with self._db.tx() as conn:
            await enqueue(
                conn,
                UI_JOB,
                {"order_id": order_id, "recheck": True},
                queue=JOB_QUEUE,
                lane="interactive",
                run_at=now() + timedelta(seconds=UI_RECHECK_S),
                dedup_key=f"billing.ui_check:{order_id}",
                max_attempts=UI_MAX_ATTEMPTS,
                caused_by=f"order:{order_id}",
            )

    async def progress_job(self, job: Job, ctx: JobContext) -> None:
        """«✅ Оплачено, подключаем…» when the final message is late (never over the final one)."""
        await self._show(_order_id(job), "progress")

    def _target(self, row: Mapping[str, Any], mode: _Mode, repairing: bool) -> tuple[Notice, str] | None:
        """What the message must show now and the stage that records it; ``None`` — nothing to change.
        ``repairing``: our last edit may have overwritten another job's — re-show what the stage says."""
        cfg = self.config
        snap = row["snapshot"] or {}
        title = str(snap.get("title") or "")
        final = None
        if row["link_state"] == "linked" and row["subscription_url"]:
            final = texts.paid_notice(
                title=title,
                until=row["paid_until"],
                subscription_url=str(row["subscription_url"]),
                balance_minor=int(row["wallet_minor"] or 0),
                currency=str(row["currency"]),
                tz=cfg.timezone,
                devices=int(snap.get("extra_devices") or 0) if snap.get("kind") == "addon_devices" else None,
            )
        stage = row["ui_stage"]
        connecting = texts.connecting_notice(title=title)
        if stage == "done":
            return (final, "done") if repairing and final is not None else None
        if mode == "final" and final is not None:
            return final, "done"
        if stage is None:
            return connecting, "connecting"
        return (connecting, "connecting") if repairing else None

    async def _show(self, order_id: int, mode: _Mode) -> str:
        """Optimistic edit (see the module docstring). Returns ``done`` (final message shown), ``waiting``
        (the panel user is not ready: «подключаем…» shown), ``progress`` or ``gone`` (nothing to show)."""
        repairing = False
        for _ in range(UI_CAS_ROUNDS):
            async with self._db.read() as conn:
                row = (await conn.execute(self._ui_query(order_id))).mappings().first()
            if row is None or row["status"] != "fulfilled":
                return "gone"
            target = self._target(row, mode, repairing)
            if target is None:
                if row["ui_stage"] == "done":
                    return "done"
                return "waiting" if mode == "final" else "progress"
            notice, stage = target
            ref = await self._deliver(row["ui_ref"], row["telegram_id"], notice)  # no transaction open
            if await self._commit_ui(order_id, row["updated_at"], stage, ref):
                if stage == "done":
                    return "done"
                return "waiting" if mode == "final" else "progress"
            repairing = True
        raise RetryJob(2.0, "сообщение о покупке меняется параллельно")

    async def _commit_ui(self, order_id: int, version: datetime, stage: str, ref: UiRef | None) -> bool:
        """CAS on the version read before the edit; on a lost CAS the version is bumped anyway, so a job
        still in flight re-checks its own edit too."""
        bump = sa.func.greatest(sa.func.clock_timestamp(), orders.c.updated_at + _TICK)
        values: dict[str, Any] = {"ui_stage": stage, "updated_at": bump}
        if ref is not None:
            values["ui_ref"] = ref.as_json()
        async with self._db.tx() as conn:
            won = (
                await conn.execute(
                    sa.update(orders)
                    .where(orders.c.id == order_id, orders.c.updated_at == version)
                    .values(**values)
                    .returning(orders.c.id)
                )
            ).first()
            if won is None:
                await conn.execute(sa.update(orders).where(orders.c.id == order_id).values(updated_at=bump))
        return won is not None

    async def _deliver(self, raw_ref: Any, telegram_id: int | None, notice: Notice) -> UiRef | None:
        """Edit the message in place, or send a new one (too old / gone / no message). ``None``: not shown
        (no messenger, no Telegram id, the user blocked the bot)."""
        if self.messenger is None:
            log.info("billing: no messenger wired, %s not shown", notice.screen)
            return None
        ref = UiRef.from_json(raw_ref)
        async with asyncio.timeout(MESSENGER_TIMEOUT_S):
            if ref is not None and now() - ref.at < EDIT_MAX_AGE and await self.messenger.edit(ref, notice):
                return ref
            if telegram_id is None:
                return None
            return await self.messenger.send(int(telegram_id), notice)

    # ------------------------------------------------------------------------------------- notices

    async def notice_job(self, job: Job, ctx: JobContext) -> None:
        p = job.payload
        try:
            user_id = int(p["user_id"])
            currency = str(p["currency"])
        except (KeyError, TypeError, ValueError) as e:
            raise PermanentJobError(f"bad notice payload: {type(e).__name__}") from None
        async with self._db.read() as conn:
            user = (
                await conn.execute(sa.select(users_wallet.c.telegram_id).where(users_wallet.c.id == user_id))
            ).first()
        notice = _notice(p, currency)
        await self._deliver(p.get("ui_ref"), user.telegram_id if user is not None else None, notice)

    async def attention_job(self, job: Job, ctx: JobContext) -> None:
        if self._attention is None:
            log.warning(
                "billing: attention item %s not raised (no attention service)", job.payload.get("key")
            )
            return
        p = job.payload
        await self._attention.raise_item(
            str(p["key"]),
            str(p.get("severity") or "warn"),
            str(p["title"]),
            str(p.get("body") or ""),
            fix_action=p.get("fix_action"),
        )


def _notice(p: Mapping[str, Any], currency: str) -> Notice:
    """A ``billing.notice`` payload as a notice (a bad payload goes dead)."""
    kind = p.get("type")
    try:
        if kind == "credited":
            order_id = p.get("order_id")
            return texts.credited_notice(
                amount_minor=int(p["amount_minor"]),
                balance_minor=int(p["balance_minor"]),
                currency=currency,
                reason=p.get("reason"),
                order_id=int(order_id) if order_id is not None else None,
                title=p.get("title"),
                price_minor=int(p["price_minor"]) if p.get("price_minor") is not None else None,
            )
        if kind == "refunded":
            return texts.refunded_notice(
                title=str(p.get("title") or ""),
                reason=str(p.get("reason") or ""),
                amount_minor=int(p["amount_minor"]),
                balance_minor=int(p["balance_minor"]),
                currency=currency,
            )
        if kind == "held":
            return texts.held_notice(title=str(p.get("title") or ""))
    except (KeyError, TypeError, ValueError) as e:
        raise PermanentJobError(f"bad notice payload: {type(e).__name__}") from None
    raise PermanentJobError(f"unknown notice type {kind!r}")


def _order_id(job: Job) -> int:
    try:
        return int(job.payload["order_id"])
    except (KeyError, TypeError, ValueError):
        raise PermanentJobError("bad payload: order_id") from None
