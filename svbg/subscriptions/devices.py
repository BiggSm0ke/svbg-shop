"""User actions on the panel user: link reissue, HWID device reset / delete (02 §4.7, §4.8).

Producers (:class:`SubscriptionActions`) run in the caller's transaction with **one** SQL for the guard
(ownership, state, freeze and the per-action cooldown in a single CAS ``UPDATE``) plus the job insert; the
panel is never called on the click.

* Link reissue is the writer's ``panel.revoke`` (it guards against a double revoke on retry).
* Device deletion: jobs ``panel.hwid_delete`` / ``panel.hwid_reset`` in the **same** ``panel`` queue with the
  writer's ``ordering_key`` (strict FIFO with every other change of the subscription). Their handlers belong
  to the single panel writer (``svbg/remnawave/writer.py`` — the only module allowed to call mutating panel
  methods); the reference implementation is ``tests/subscriptions/hwid_jobs.py`` (idempotent: a device that
  is already gone counts as deleted; connections are dropped afterwards, best effort).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.core.clock import now
from svbg.jobs.queue import enqueue
from svbg.remnawave.writer import K_REVOKE, QUEUE, enqueue_action, ordering_key
from svbg.subscriptions import hooks, journal
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ACTION_TEXTS",
    "ACTION_TEXTS_EN",
    "K_HWID_DELETE",
    "K_HWID_RESET",
    "ActionResult",
    "SubscriptionActions",
]

K_HWID_DELETE: Final = "panel.hwid_delete"
K_HWID_RESET: Final = "panel.hwid_reset"
MAX_ATTEMPTS: Final = 100
_HWID_MAX: Final = 256

#: Cooldown keys in ``subscriptions.cooldowns`` and their defaults (settings may override them).
REISSUE: Final = "reissue"
DEVICES_RESET: Final = "devices_reset"
DEVICE_DELETE: Final = "device_delete"

ACTION_TEXTS: Final[Mapping[str, str]] = {
    "ok": "Готово.",
    "not_found": "Подписка не найдена.",
    "pending": "Подписка ещё подключается — попробуйте через минуту.",
    "frozen": "Подписка приостановлена — действие недоступно.",
    "cooldown": "Слишком часто. Попробуйте через {minutes} мин.",
}
#: English of :data:`ACTION_TEXTS` (same keys and placeholders).
ACTION_TEXTS_EN: Final[Mapping[str, str]] = {
    "ok": "Done.",
    "not_found": "Subscription not found.",
    "pending": "The subscription is still connecting — please try again in a minute.",
    "frozen": "Your subscription is on hold — the action is not available.",
    "cooldown": "Too often. Please try again in {minutes} min.",
}


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    reason: str = "ok"  # ok | not_found | pending | frozen | cooldown
    retry_after_s: int = 0
    job_id: int | None = None

    @property
    def text(self) -> str:
        minutes = max(1, -(-self.retry_after_s // 60))
        return ACTION_TEXTS.get(self.reason, "").format(minutes=minutes)

    def localized(self, lang: str | None) -> str:
        """:attr:`text` in ``lang`` (Russian fallback)."""
        if lang != "en" or self.reason not in ACTION_TEXTS_EN:
            return self.text
        return ACTION_TEXTS_EN[self.reason].format(minutes=max(1, -(-self.retry_after_s // 60)))


class SubscriptionActions:
    """User-initiated panel actions with cooldowns. ``config`` supplies the cooldown settings (minutes)."""

    def __init__(
        self,
        *,
        config: Callable[[], Mapping[str, Any]] | None = None,
        reissue_cooldown: timedelta = timedelta(minutes=10),
        devices_reset_cooldown: timedelta = timedelta(minutes=5),
        device_delete_cooldown: timedelta = timedelta(seconds=5),
    ) -> None:
        self._config = config
        self._defaults = {
            REISSUE: (reissue_cooldown, "REISSUE_COOLDOWN_MINUTES"),
            DEVICES_RESET: (devices_reset_cooldown, "DEVICES_RESET_COOLDOWN_MINUTES"),
            DEVICE_DELETE: (device_delete_cooldown, None),
        }

    def cooldown(self, key: str) -> timedelta:
        default, setting = self._defaults[key]
        if setting is not None and self._config is not None:
            value = self._config().get(setting)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return timedelta(minutes=value)
        return default

    async def _guard(
        self, conn: AsyncConnection, subscription_id: int, user_id: int | None, key: str
    ) -> ActionResult | None:
        """One CAS ``UPDATE``: owner, ``linked``, not frozen, cooldown passed → stamp the cooldown."""
        at = now()
        last = subscriptions.c.cooldowns[key].astext
        conds = [
            subscriptions.c.id == subscription_id,
            subscriptions.c.link_state == "linked",
            subscriptions.c.hold_kind.is_(None),
            sa.or_(last.is_(None), sa.cast(last, sa.DateTime(timezone=True)) <= at - self.cooldown(key)),
        ]
        if user_id is not None:
            conds.append(subscriptions.c.user_id == user_id)
        stamp = sa.cast(
            sa.literal({key: at.isoformat()}, subscriptions.c.cooldowns.type), subscriptions.c.cooldowns.type
        )
        done = (
            await conn.execute(
                sa.update(subscriptions)
                .where(*conds)
                .values(cooldowns=subscriptions.c.cooldowns.op("||")(stamp))
                .returning(subscriptions.c.id)
            )
        ).first()
        if done is not None:
            return None
        return await self._why(conn, subscription_id, user_id, key)

    async def _why(
        self, conn: AsyncConnection, subscription_id: int, user_id: int | None, key: str
    ) -> ActionResult:
        """Refusal path only (a second SQL): tell the user why."""
        row = (
            (
                await conn.execute(
                    sa.select(
                        subscriptions.c.user_id,
                        subscriptions.c.link_state,
                        subscriptions.c.hold_kind,
                        subscriptions.c.cooldowns,
                    ).where(subscriptions.c.id == subscription_id)
                )
            )
            .mappings()
            .first()
        )
        if row is None or (user_id is not None and row["user_id"] != user_id):
            return ActionResult(False, "not_found")
        if row["link_state"] == "pending":
            return ActionResult(False, "pending")
        if row["link_state"] != "linked":
            return ActionResult(False, "not_found")
        if row["hold_kind"] is not None:
            return ActionResult(False, "frozen")
        stamp = (row["cooldowns"] or {}).get(key)
        left = 0
        if isinstance(stamp, str):
            left = int(((datetime.fromisoformat(stamp) + self.cooldown(key)) - now()).total_seconds())
        return ActionResult(False, "cooldown", max(1, left))

    async def reissue_link(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        user_id: int | None = None,
        only_passwords: bool = False,
        caused_by: str | None = None,
    ) -> ActionResult:
        """«Перевыпустить ссылку»: the old link stops working on every device (02 §4.8)."""
        refused = await self._guard(conn, subscription_id, user_id, REISSUE)
        if refused is not None:
            return refused
        job = await enqueue_action(
            conn, subscription_id, K_REVOKE, {"only_passwords": only_passwords}, caused_by=caused_by
        )
        await self._note(
            conn, subscription_id, user_id, "reissue_requested", "subscription.reissue_requested"
        )
        return ActionResult(True, job_id=job)

    async def reset_devices(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        user_id: int | None = None,
        caused_by: str | None = None,
    ) -> ActionResult:
        """Delete every HWID device of the subscription (``delete-all``, no panel webhook follows)."""
        refused = await self._guard(conn, subscription_id, user_id, DEVICES_RESET)
        if refused is not None:
            return refused
        job = await _enqueue(conn, K_HWID_RESET, subscription_id, {}, caused_by)
        await self._note(
            conn, subscription_id, user_id, "devices_reset_requested", "subscription.devices_reset_requested"
        )
        return ActionResult(True, job_id=job)

    async def delete_device(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        hwid: str,
        *,
        user_id: int | None = None,
        caused_by: str | None = None,
    ) -> ActionResult:
        """Delete one device by ``hwid`` (from the devices list the user path showed)."""
        if not isinstance(hwid, str) or not hwid or len(hwid) > _HWID_MAX:
            raise ValueError("hwid must be a non-empty string")
        refused = await self._guard(conn, subscription_id, user_id, DEVICE_DELETE)
        if refused is not None:
            return refused
        job = await _enqueue(conn, K_HWID_DELETE, subscription_id, {"hwid": hwid}, caused_by)
        return ActionResult(True, job_id=job)

    @staticmethod
    async def _note(conn: AsyncConnection, sid: int, user_id: int | None, kind: str, event: str) -> None:
        await journal.record(conn, sid, kind, source="bot", details={"user_id": user_id})
        await hooks.emit(conn, event, {"subscription_id": sid, "user_id": user_id})


async def _enqueue(
    conn: AsyncConnection, kind: str, sid: int, payload: Mapping[str, Any], caused_by: str | None
) -> int | None:
    return await enqueue(
        conn,
        kind,
        {"sub_id": sid, **payload},
        queue=QUEUE,
        lane="interactive",
        ordering_key=ordering_key(sid),
        max_attempts=MAX_ATTEMPTS,
        caused_by=caused_by,
    )
