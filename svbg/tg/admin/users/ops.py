"""Operations of the user card — always through the services and the panel writer, never straight to the
panel.

Every operation runs in **one** transaction that starts by re-reading the presser's role
(:func:`svbg.services.roles.load_actor`, ``FOR SHARE`` for money) and ends with the ``admin_audit`` row, so
the change and its audit commit together or not at all (04 §9.1):

* ``±N дней`` — :meth:`SubscriptionLifecycle.extend` (absolute ``expireAt`` PATCH job; frozen → frozen
  balance);
* «Выдать тариф» — :meth:`SubscriptionLifecycle.purchase` with a reference ``admin_op/<op>`` (a double submit
  is a no-op);
* «Баланс ±X» — :func:`svbg.billing.wallet.adjust` (ledger + audit in one statement chain);
* «Сбросить устройства», «Новая ссылка» — :class:`SubscriptionActions` jobs (admins skip the user cooldowns);
* block / unblock — ``users.banned_at`` + ``panel.disable`` (``BOT_BAN``) / ``panel.enable`` (lifts only
  ``BOT_BAN``, never an admin's own disable in the panel);
* «Написать» — a plain message through the notifier (bounded by a timeout), audited after the attempt.

Money and term changes need a reason; admins are limited by ``ADMIN_GRANT_DAYS_MAX`` /
``ADMIN_WALLET_ADJUST_MAX`` per operation and by the 24 h totals (:func:`svbg.services.roles.check_daily`),
above that only an owner; an admin never grants days, plans or money to themselves. Admin days are journaled
as ``extended`` with ``source='admin'`` — the LTE period engine classifies them as an ``admin`` event.

Results are :class:`OpResult` with a short Russian text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa

from svbg.billing import wallet
from svbg.core.money import format_money
from svbg.core.tables import users
from svbg.services import roles
from svbg.services.roles import Act, Actor, Limits, RoleError
from svbg.subscriptions.devices import SubscriptionActions
from svbg.subscriptions.lifecycle import MAX_DAYS, SubscriptionError, SubscriptionLifecycle
from svbg.subscriptions.service import SubscriptionService
from svbg.subscriptions.tables import subscriptions
from svbg.subscriptions.terms import PlanTerms

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = ["MESSAGE_MAX", "OpResult", "UserOps"]

log = logging.getLogger("svbg.tg.admin.users")

DAY: Final = 86_400
MESSAGE_MAX: Final = 3500
SEND_TIMEOUT: Final = 10.0
LIVE: Final = ("pending", "linked")
BAN_REASON: Final = "BOT_BAN"

T: Final[dict[str, str]] = {
    "denied": roles.DENIED,
    "not_found": "Пользователь не найден.",
    "no_sub": "У пользователя нет активной подписки — выдайте тариф.",
    "no_linked": "Подписка ещё не подключена к панели — попробуйте позже.",
    "zero": "Нужно число, отличное от нуля.",
    "days_range": f"Число дней: от −{MAX_DAYS} до {MAX_DAYS}.",
    "duplicate": "Уже сделано.",
    "days_done": "Готово: {delta}. Действует до {until}.",
    "days_frozen": "Подписка приостановлена: {delta} добавлены к остатку и начнут идти после разморозки.",
    "plan_done": "Тариф «{plan}» выдан на {days} дн. Действует до {until}.",
    "plan_frozen": "Тариф «{plan}» выдан: подписка приостановлена, срок начнёт идти после разморозки.",
    "no_plan": "Тариф не найден.",
    "trial_plan": "Пробный тариф так выдать нельзя.",
    "wallet_done": "Баланс: {delta}. Теперь {balance}.",
    "wallet_short": "На балансе меньше, чем вы списываете.",
    "banned": "Пользователь заблокирован: бот его игнорирует, подписка отключена в панели.",
    "already_banned": "Пользователь уже заблокирован.",
    "unbanned": "Пользователь разблокирован.",
    "not_banned": "Пользователь не заблокирован.",
    "ban_self": "Себя заблокировать нельзя.",
    "ban_owner": "Владельца заблокировать нельзя.",
    "ban_staff": "Сотрудников блокирует только владелец — сначала снимите роль.",
    "is_banned": "Пользователь заблокирован — сначала разблокируйте.",
    "devices_done": "Устройства сброшены: пользователь подключится заново.",
    "reissue_done": "Ссылка перевыпущена: старая перестала работать.",
    "no_tg": "У пользователя нет Telegram — написать нельзя.",
    "msg_empty": "Пустое сообщение.",
    "msg_sent": "Сообщение отправлено.",
    "msg_blocked": "Не доставлено: пользователь заблокировал бота.",
    "msg_failed": "Не удалось отправить, попробуйте позже.",
    "msg_header": "✉️ <b>Сообщение от поддержки</b>\n\n{text}",
}


class Messenger(Protocol):
    """What :meth:`UserOps.message` needs from :class:`svbg.tg.notifier.Notifier`."""

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = ..., **kw: Any) -> Any: ...


class PlanSource(Protocol):
    def plan(self, plan_id: int | None) -> Any: ...


@dataclass(frozen=True, slots=True)
class OpResult:
    ok: bool
    text: str
    denied: bool = False
    #: Telegram id whose cached context must be dropped (ban / unban).
    invalidate: int | None = None


def _days_text(days: int) -> str:
    sign = "+" if days > 0 else "−"
    return f"{sign}{abs(days)} дн."


def signed_money(amount: int, currency: str) -> str:
    try:
        text = format_money(abs(amount), currency, "ru")
    except (ValueError, KeyError):
        text = f"{abs(amount)} {currency}"
    return ("+" if amount > 0 else "−" if amount < 0 else "") + text


class UserOps:
    """See the module docstring. Dependencies are callables so settings and owners apply without restart."""

    def __init__(
        self,
        db: Database,
        *,
        owner_ids: Callable[[], Awaitable[frozenset[int]]],
        limits: Callable[[], Limits],
        currency: Callable[[], str],
        plans: Callable[[], PlanSource | None] = lambda: None,
        notifier: Messenger | None = None,
        format_date: Callable[[Any], str] = lambda d: d.strftime("%d.%m.%Y") if d else "—",
        lifecycle: SubscriptionLifecycle | None = None,
        service: SubscriptionService | None = None,
    ) -> None:
        self.db = db
        self._owner_ids = owner_ids
        self._limits = limits
        self._currency = currency
        self._plans = plans
        self._notifier = notifier
        self._date = format_date
        self._lifecycle = lifecycle or SubscriptionLifecycle()
        self._service = service or SubscriptionService()
        # Staff act on behalf of the user: no user cooldowns (they are stamped anyway, which is harmless).
        self._actions = SubscriptionActions(
            reissue_cooldown=timedelta(0),
            devices_reset_cooldown=timedelta(0),
            device_delete_cooldown=timedelta(0),
        )

    # ------------------------------------------------------------------------------------------ plumbing

    async def _run(
        self,
        actor_tg: int,
        act: Act,
        target: int,
        body: Callable[[AsyncConnection, Actor], Awaitable[OpResult]],
    ) -> OpResult:
        owners = await self._owner_ids()
        try:
            async with self.db.tx() as conn:
                actor = await roles.load_actor(
                    conn, telegram_id=actor_tg, owner_ids=owners, lock=act in roles.MONEY_ACTS
                )
                if not roles.authorize(actor, act):
                    if actor is not None and actor.role != "user":  # stale cached role: keep the attempt
                        await roles.audit(conn, actor, "access_denied", target=f"{act.value}:user:{target}")
                    return OpResult(False, T["denied"], denied=True)
                assert actor is not None
                roles.check_target(actor, act, target)
                return await body(conn, actor)
        except RoleError as e:
            return OpResult(False, e.text, denied=e.code == "denied")
        except SubscriptionError as e:
            return OpResult(False, e.text)

    @staticmethod
    async def _target(conn: AsyncConnection, user_id: int, *, lock: bool = False) -> Any:
        stmt = sa.select(users.c.id, users.c.telegram_id, users.c.role, users.c.banned_at).where(
            users.c.id == user_id
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await conn.execute(stmt)).first()
        if row is None:
            raise RoleError("not_found", T["not_found"])
        return row

    @staticmethod
    async def _live_sub(conn: AsyncConnection, user_id: int) -> Any:
        return (
            await conn.execute(
                sa.select(subscriptions.c.id, subscriptions.c.link_state)
                .where(subscriptions.c.user_id == user_id, subscriptions.c.link_state.in_(LIVE))
                .order_by(subscriptions.c.id.desc())
                .limit(1)
            )
        ).first()

    # ------------------------------------------------------------------------------------------ term

    async def grant_days(self, actor_tg: int, user_id: int, days: int, reason: str, op_id: str) -> OpResult:
        """``±days`` to the live subscription (``subs.grant``; reason; admin limit)."""

        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            if isinstance(days, bool) or not isinstance(days, int) or days == 0:
                raise RoleError("zero", T["zero"])
            if abs(days) > MAX_DAYS:
                raise RoleError("range", T["days_range"])
            limits = self._limits()
            roles.check_limit(actor, Act.SUBS_GRANT, days, limits)
            why = roles.require_reason(reason)
            await self._target(conn, user_id)
            await roles.check_daily(conn, actor, Act.SUBS_GRANT, days, limits)
            sub = await self._live_sub(conn, user_id)
            if sub is None:
                raise RoleError("no_sub", T["no_sub"])
            applied = await self._lifecycle.extend(
                conn,
                int(sub.id),
                days * DAY,
                source="admin",
                ref_type="admin_op",
                ref_id=op_id,
                reason=why,
                caused_by=f"admin:{actor.user_id}",
            )
            if applied.duplicate:
                return OpResult(True, T["duplicate"])
            await roles.audit(
                conn,
                actor,
                "subs.grant",
                target=f"user:{user_id}",
                reason=why,
                details={
                    "days": days,
                    "subscription_id": int(sub.id),
                    "op_id": op_id,
                    "new_paid_until": applied.new_paid_until.isoformat() if applied.new_paid_until else None,
                    "frozen": applied.frozen,
                },
            )
            if applied.frozen:
                return OpResult(True, T["days_frozen"].format(delta=_days_text(days)))
            return OpResult(
                True, T["days_done"].format(delta=_days_text(days), until=self._date(applied.new_paid_until))
            )

        return await self._run(actor_tg, Act.SUBS_GRANT, user_id, body)

    async def give_plan(  # noqa: PLR0917 - flat operation arguments, mirrors grant_days
        self, actor_tg: int, user_id: int, plan_id: int, days: int, reason: str, op_id: str
    ) -> OpResult:
        """Give (or switch to) a plan for ``days`` without payment (``subs.grant``; reason; admin limit)."""

        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
                raise RoleError("range", T["days_range"])
            limits = self._limits()
            roles.check_limit(actor, Act.SUBS_GRANT, days, limits)
            why = roles.require_reason(reason)
            source = self._plans()
            plan = None if source is None else source.plan(plan_id)
            if plan is None:
                raise RoleError("no_plan", T["no_plan"])
            if getattr(plan, "is_trial", False):
                raise RoleError("trial_plan", T["trial_plan"])
            target = await self._target(conn, user_id)
            if target.banned_at is not None:
                raise RoleError("banned", T["is_banned"])
            await roles.check_daily(conn, actor, Act.SUBS_GRANT, days, limits)
            try:
                terms = PlanTerms.from_snapshot(plan)
            except (TypeError, ValueError) as e:
                raise RoleError("bad_plan", f"Тариф нельзя выдать: {e}") from None
            applied = await self._lifecycle.purchase(
                conn,
                user_id=user_id,
                terms=terms,
                days=days,
                ref_type="admin_op",
                ref_id=op_id,
                source="admin",
                caused_by=f"admin:{actor.user_id}",
            )
            title = plan.title("ru") if callable(getattr(plan, "title", None)) else str(plan_id)
            if applied.duplicate:
                return OpResult(True, T["duplicate"])
            await roles.audit(
                conn,
                actor,
                "subs.give_plan",
                target=f"user:{user_id}",
                reason=why,
                details={
                    "plan_id": plan_id,
                    "days": days,
                    "subscription_id": applied.subscription_id,
                    "action": applied.action,
                    "op_id": op_id,
                },
            )
            if applied.frozen:
                return OpResult(True, T["plan_frozen"].format(plan=title))
            return OpResult(
                True, T["plan_done"].format(plan=title, days=days, until=self._date(applied.new_paid_until))
            )

        return await self._run(actor_tg, Act.SUBS_GRANT, user_id, body)

    # ------------------------------------------------------------------------------------------ wallet

    async def adjust_wallet(
        self, actor_tg: int, user_id: int, delta_minor: int, reason: str, op_id: str
    ) -> OpResult:
        """``±amount`` on the wallet (``wallet.adjust``; reason; admin limit). The ledger row and the audit
        row are written by :func:`svbg.billing.wallet.adjust` in this transaction."""

        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            if isinstance(delta_minor, bool) or not isinstance(delta_minor, int) or delta_minor == 0:
                raise RoleError("zero", T["zero"])
            limits = self._limits()
            roles.check_limit(actor, Act.WALLET_ADJUST, delta_minor, limits)
            why = roles.require_reason(reason)
            await self._target(conn, user_id)
            await roles.check_daily(conn, actor, Act.WALLET_ADJUST, delta_minor, limits)
            currency = self._currency()
            entry = await wallet.adjust(
                conn,
                user_id,
                delta_minor,
                op_id=op_id,
                actor_id=actor.user_id,
                role=actor.role,
                reason=why,
                currency=currency,
            )
            if entry is None:
                done = await wallet.find(conn, user_id, "admin_adjust", "admin_op", op_id)
                if done is not None:
                    return OpResult(True, T["duplicate"])
                raise RoleError("short", T["wallet_short"])
            return OpResult(
                True,
                T["wallet_done"].format(
                    delta=signed_money(delta_minor, currency),
                    balance=signed_money(entry.balance_after, currency),
                ),
            )

        return await self._run(actor_tg, Act.WALLET_ADJUST, user_id, body)

    # ------------------------------------------------------------------------------------------ block

    async def _ban_target(self, conn: AsyncConnection, actor: Actor, user_id: int) -> Any:
        target = await self._target(conn, user_id, lock=True)
        owners = await self._owner_ids()
        if actor.user_id is not None and int(target.id) == actor.user_id:
            raise RoleError("self", T["ban_self"])
        if target.role == "owner" or (target.telegram_id is not None and target.telegram_id in owners):
            raise RoleError("owner", T["ban_owner"])
        if target.role != "user" and not actor.is_owner:
            raise RoleError("staff", T["ban_staff"])
        return target

    async def _linked_subs(self, conn: AsyncConnection, user_id: int) -> Iterable[int]:
        rows = (
            await conn.execute(
                sa.select(subscriptions.c.id).where(
                    subscriptions.c.user_id == user_id, subscriptions.c.link_state == "linked"
                )
            )
        ).all()
        return [int(r.id) for r in rows]

    async def ban(self, actor_tg: int, user_id: int, reason: str) -> OpResult:
        """Block in the bot and disable the panel user (``users.ban``; reason)."""

        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            why = roles.require_reason(reason)
            target = await self._ban_target(conn, actor, user_id)
            if target.banned_at is not None:
                return OpResult(False, T["already_banned"])
            await conn.execute(sa.update(users).where(users.c.id == user_id).values(banned_at=sa.func.now()))
            subs = list(await self._linked_subs(conn, user_id))
            for sid in subs:
                await self._service.disable(conn, sid, reason=BAN_REASON, caused_by=f"admin:{actor.user_id}")
            await roles.audit(
                conn, actor, "user.ban", target=f"user:{user_id}", reason=why, details={"subscriptions": subs}
            )
            return OpResult(True, T["banned"], invalidate=target.telegram_id)

        return await self._run(actor_tg, Act.USERS_BAN, user_id, body)

    async def unban(self, actor_tg: int, user_id: int, reason: str) -> OpResult:
        """Unblock; the panel user is enabled only if the bot's ban disabled it (``users.ban``; reason)."""

        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            why = roles.require_reason(reason)
            target = await self._ban_target(conn, actor, user_id)
            if target.banned_at is None:
                return OpResult(False, T["not_banned"])
            await conn.execute(sa.update(users).where(users.c.id == user_id).values(banned_at=None))
            subs = list(await self._linked_subs(conn, user_id))
            for sid in subs:
                await self._service.enable(
                    conn, sid, only_reason=BAN_REASON, caused_by=f"admin:{actor.user_id}"
                )
            await roles.audit(
                conn,
                actor,
                "user.unban",
                target=f"user:{user_id}",
                reason=why,
                details={"subscriptions": subs},
            )
            return OpResult(True, T["unbanned"], invalidate=target.telegram_id)

        return await self._run(actor_tg, Act.USERS_BAN, user_id, body)

    # -------------------------------------------------------------------------------------- devices / link

    async def _sub_action(self, actor_tg: int, user_id: int, act: Act, kind: str) -> OpResult:
        async def body(conn: AsyncConnection, actor: Actor) -> OpResult:
            await self._target(conn, user_id)
            sub = await self._live_sub(conn, user_id)
            if sub is None:
                raise RoleError("no_sub", T["no_sub"])
            if sub.link_state != "linked":
                raise RoleError("no_linked", T["no_linked"])
            caused_by = f"admin:{actor.user_id}"
            if kind == "devices":
                result = await self._actions.reset_devices(conn, int(sub.id), caused_by=caused_by)
            else:
                result = await self._actions.reissue_link(conn, int(sub.id), caused_by=caused_by)
            if not result.ok:
                return OpResult(False, result.text or T["no_linked"])
            await roles.audit(
                conn,
                actor,
                "user.devices_reset" if kind == "devices" else "user.reissue",
                target=f"user:{user_id}",
                details={"subscription_id": int(sub.id), "job_id": result.job_id},
            )
            return OpResult(True, T["devices_done"] if kind == "devices" else T["reissue_done"])

        return await self._run(actor_tg, act, user_id, body)

    async def reset_devices(self, actor_tg: int, user_id: int) -> OpResult:
        return await self._sub_action(actor_tg, user_id, Act.USERS_DEVICES, "devices")

    async def reissue(self, actor_tg: int, user_id: int) -> OpResult:
        return await self._sub_action(actor_tg, user_id, Act.USERS_REISSUE, "reissue")

    # ------------------------------------------------------------------------------------------ message

    async def message(self, actor_tg: int, user_id: int, text: str) -> OpResult:
        """Send ``text`` to the user (``users.message``): authorise, send (≤ 10 s), audit the outcome."""
        body_text = (text or "").strip()
        if not body_text:
            return OpResult(False, T["msg_empty"])
        body_text = body_text[:MESSAGE_MAX]
        found: dict[str, Any] = {}

        async def check(conn: AsyncConnection, actor: Actor) -> OpResult:
            target = await self._target(conn, user_id)
            if target.telegram_id is None:
                raise RoleError("no_tg", T["no_tg"])
            found["actor"], found["chat"] = actor, int(target.telegram_id)
            return OpResult(True, "")

        allowed = await self._run(actor_tg, Act.USERS_MESSAGE, user_id, check)
        if not allowed.ok:
            return allowed
        if self._notifier is None:
            return OpResult(False, T["msg_failed"])
        outcome = "sent"
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                sent = await self._notifier.send(
                    found["chat"], T["msg_header"].format(text=escape(body_text)), parse_mode="HTML"
                )
            if sent is None:
                outcome = "blocked"
        except Exception as exc:  # noqa: BLE001 - delivery failure is reported to the admin, not raised
            log.warning("message to user %s failed: %s", user_id, type(exc).__name__)
            outcome = "failed"
        try:
            async with self.db.tx() as conn:
                await roles.audit(
                    conn,
                    found["actor"],
                    "user.message",
                    target=f"user:{user_id}",
                    details={"chars": len(body_text), "outcome": outcome},
                )
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot audit a message to user %s: %s", user_id, type(exc).__name__)
        return OpResult(
            outcome == "sent", T[{"sent": "msg_sent", "blocked": "msg_blocked"}.get(outcome, "msg_failed")]
        )
