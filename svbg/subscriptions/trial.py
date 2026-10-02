"""Trial (02 §4.1, 06 M2): one per bot user **and** per Telegram id, audience ``all | channel_members``.

* ``TRIAL_DAYS`` (0 = off), ``TRIAL_AUDIENCE``, ``REQUIRED_CHANNEL_ID`` come from the settings snapshot;
  the panel terms (squads, devices, traffic, tag) from the catalog's trial plan (:class:`TrialPlanSource`).
* :meth:`TrialService.check` is the screen check: one SQL (+ one for the channel cache), no HTTP to the panel.
* :meth:`TrialService.activate` re-checks under a row lock on ``users`` and creates the ``pending``
  subscription + ``panel.create`` + ``trial_grants`` + audit + ``trial.activated`` in one transaction. A race
  of two clicks or two accounts of one Telegram id ends with exactly one trial (``UNIQUE`` on the grants).
* A user who ever had a subscription (paid, imported, closed) is not offered a trial.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.remnawave.models import FOREVER
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.service import Desired, SubscriptionService
from svbg.subscriptions.tables import subscriptions, trial_grants
from svbg.subscriptions.terms import PlanTerms, TrialPlanSource

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.subscriptions.channel import ChannelService

__all__ = ["TRIAL_TEXTS", "TRIAL_TEXTS_EN", "TrialRefused", "TrialResult", "TrialService", "trial_text"]

log = logging.getLogger("svbg.subscriptions.trial")

#: Default Russian texts per refusal reason (the user path may override them through content).
TRIAL_TEXTS: Final[Mapping[str, str]] = {
    "disabled": "Пробный период сейчас недоступен.",
    "used": "Пробный период уже был использован.",
    "has_subscription": "Пробный период — только для новых пользователей: у вас уже есть подписка.",
    "not_member": "Пробный период — для подписчиков нашего канала. Подпишитесь и нажмите «Проверить».",
    "channel_unknown": "Не получилось проверить подписку на канал. Попробуйте через минуту.",
    "channel_not_configured": "Пробный период временно недоступен.",
    "no_plan": "Пробный период временно недоступен.",
    "banned": "Доступ ограничен. Напишите в поддержку.",
    "no_user": "Нажмите /start и попробуйте ещё раз.",
}
#: English of :data:`TRIAL_TEXTS` (same keys).
TRIAL_TEXTS_EN: Final[Mapping[str, str]] = {
    "disabled": "The trial is not available right now.",
    "used": "The trial has already been used.",
    "has_subscription": "The trial is for new users only: you already have a subscription.",
    "not_member": "The trial is for our channel subscribers. Join the channel and tap «Check».",
    "channel_unknown": "Could not check the channel subscription. Please try again in a minute.",
    "channel_not_configured": "The trial is temporarily unavailable.",
    "no_plan": "The trial is temporarily unavailable.",
    "banned": "Access is restricted. Please contact support.",
    "no_user": "Press /start and try again.",
}


def trial_text(reason: str | None, lang: str | None = "ru") -> str:
    """The refusal text for ``reason`` in ``lang`` (Russian fallback; empty for an unknown reason)."""
    key = reason or ""
    if lang == "en" and key in TRIAL_TEXTS_EN:
        return TRIAL_TEXTS_EN[key]
    return TRIAL_TEXTS.get(key, "")


@dataclass(frozen=True, slots=True)
class TrialResult:
    ok: bool
    reason: str | None = None
    subscription_id: int | None = None
    paid_until: datetime | None = None
    days: int = 0

    @property
    def text(self) -> str:
        return TRIAL_TEXTS.get(self.reason or "", "")

    def localized(self, lang: str | None) -> str:
        return trial_text(self.reason, lang)


class TrialRefused(Exception):
    """Refusal inside :meth:`TrialService.grant`; rolls the caller's transaction back."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TrialService:
    def __init__(
        self,
        db: Database,
        catalog: TrialPlanSource,
        *,
        config: Callable[[], Mapping[str, Any]],
        channel: ChannelService | None = None,
        service: SubscriptionService | None = None,
    ) -> None:
        self._db = db
        self._catalog = catalog
        self._config = config
        self._channel = channel
        self._service = service or SubscriptionService()

    def days(self) -> int:
        try:
            return max(0, int(self._config().get("TRIAL_DAYS") or 0))
        except (TypeError, ValueError):
            return 0

    def audience(self) -> str:
        return str(self._config().get("TRIAL_AUDIENCE") or "all")

    # ------------------------------------------------------------------------------------------- check

    async def _facts(
        self, conn: AsyncConnection, user_id: int, *, lock: bool = False
    ) -> Mapping[str, Any] | None:
        used = (
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
        had = sa.select(sa.literal(1)).where(subscriptions.c.user_id == users.c.id).correlate(users).exists()
        stmt = sa.select(
            users.c.telegram_id, users.c.banned_at, used.label("used"), had.label("has_subscription")
        ).where(users.c.id == user_id)
        if lock:
            stmt = stmt.with_for_update(of=users)
        return (await conn.execute(stmt)).mappings().first()

    @staticmethod
    def _refusal(facts: Mapping[str, Any] | None) -> str | None:
        if facts is None:
            return "no_user"
        if facts["banned_at"] is not None:
            return "banned"
        if facts["used"]:
            return "used"
        if facts["has_subscription"]:
            return "has_subscription"
        return None

    async def _channel_refusal(self, telegram_id: int | None, *, fresh: bool) -> str | None:
        if self.audience() != "channel_members":
            return None
        if self._channel is None or self._channel.required_chat() is None:
            log.warning("TRIAL_AUDIENCE=channel_members, но обязательный канал не задан: триал не выдаётся")
            return "channel_not_configured"
        if telegram_id is None:
            return "not_member"
        member = await self._channel.is_member(telegram_id, fresh=fresh)
        if member is None:
            return "channel_unknown"
        return None if member else "not_member"

    async def check(self, user_id: int, *, fresh_channel: bool = False) -> TrialResult:
        """May the user take the trial now? (Screen check; :meth:`activate` re-checks under a lock.)"""
        days = self.days()
        if days <= 0:
            return TrialResult(False, "disabled")
        async with self._db.read() as conn:
            facts = await self._facts(conn, user_id)
        reason = self._refusal(facts)
        if reason is None and facts is not None:
            reason = await self._channel_refusal(facts["telegram_id"], fresh=fresh_channel)
        return TrialResult(reason is None, reason, days=days)

    # ---------------------------------------------------------------------------------------- activate

    async def activate(self, user_id: int, *, caused_by: str | None = None) -> TrialResult:
        """Check and grant. The channel is checked before the transaction (an HTTP call to Telegram may be
        needed; never inside an open transaction)."""
        pre = await self.check(user_id)
        if not pre.ok:
            return pre
        try:
            async with self._db.tx() as conn:
                return await self.grant(conn, user_id=user_id, days=pre.days, caused_by=caused_by)
        except TrialRefused as refused:
            return TrialResult(False, refused.reason)

    async def grant(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        days: int,
        source: str = "bot",
        caused_by: str | None = None,
    ) -> TrialResult:
        """The transactional part (no channel check). Raises ``TrialRefused`` to roll the caller back."""
        if days <= 0:
            raise TrialRefused("disabled")
        facts = await self._facts(conn, user_id, lock=True)
        reason = self._refusal(facts)
        if reason is not None or facts is None:
            raise TrialRefused(reason or "no_user")
        raw = await self._catalog.trial_plan(conn)
        if raw is None:
            raise TrialRefused("no_plan")
        try:
            plan = PlanTerms.from_snapshot(raw)
        except (TypeError, ValueError) as err:
            log.warning("trial plan is unusable: %s", err)
            raise TrialRefused("no_plan") from err
        telegram_id = facts["telegram_id"]
        at = now()
        until = min(at + timedelta(days=days), FOREVER)
        sid = await self._service.create(
            conn,
            user_id=user_id,
            telegram_id=telegram_id,
            desired=Desired(
                expire_at=until,
                squads=plan.squads,
                traffic_bytes=plan.traffic_bytes,
                reset_strategy=plan.reset_strategy,
                device_limit=plan.device_limit,
                ext_squad=plan.ext_squad,
                tag=plan.panel_tag,
            ),
            plan_id=plan.plan_id,
            plan_snapshot={**plan.to_snapshot(), "is_trial": True},
            caused_by=caused_by,
            is_trial=True,
        )
        granted = (
            await conn.execute(
                pg_insert(trial_grants)
                .values(user_id=user_id, telegram_id=telegram_id, subscription_id=sid, source=source)
                .on_conflict_do_nothing()
                .returning(trial_grants.c.id)
            )
        ).first()
        if granted is None:
            raise TrialRefused("used")  # another account of this Telegram id won the race
        details = {"is_trial_before": False, "is_trial_after": True, "days": days, "plan_id": plan.plan_id}
        await journal.record(
            conn,
            sid,
            "trial_started",
            source=source,
            new_expire=until,
            delta_seconds=days * 86_400,
            ref_type="trial",
            ref_id=str(user_id),
            details=details,
        )
        await hooks.emit(
            conn,
            "trial.activated",
            {
                "subscription_id": sid,
                "user_id": user_id,
                "telegram_id": telegram_id,
                "days": days,
                "paid_until": until,
            },
            lane="interactive",
            caused_by=caused_by,
        )
        return TrialResult(True, None, sid, until, days)
