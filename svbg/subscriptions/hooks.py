"""Durable domain events of subscriptions (05 §3.2 X2/X3).

A business change enqueues its event **in the same transaction** (``jobs(queue='hook')``), so a rolled back
change never announces itself and a committed one is always announced — after the commit, even across a
restart. The job handler re-publishes it on the in-process :class:`~svbg.core.bus.EventBus`; subscribers that
must not lose it (admin topics, referral, LTE) enqueue their own jobs from there.

Events: ``trial.activated``, ``subscription.term_changed`` (``kind``: purchase_new, purchase_renew,
trial_converted, plan_changed, extended, unfrozen…),
``subscription.devices_changed``, ``subscription.frozen``, ``subscription.unfrozen``,
``subscription.channel_left``, ``subscription.channel_returned``, ``subscription.reissue_requested``,
``subscription.devices_reset_requested``, ``subscription.devices_reset``, ``subscription.device_deleted``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from svbg.core.bus import Event, EventBus
from svbg.jobs.queue import Job, enqueue
from svbg.jobs.worker import Handler, JobContext, PermanentJobError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["HOOK_KIND", "HOOK_QUEUE", "EventRelay", "emit"]

log = logging.getLogger("svbg.subscriptions.hooks")

HOOK_QUEUE: Final = "hook"
HOOK_KIND: Final = "subscriptions.event"


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


async def emit(
    conn: AsyncConnection,
    name: str,
    payload: Mapping[str, Any],
    *,
    lane: str = "background",
    caused_by: str | None = None,
) -> None:
    """Announce ``name`` after the caller's transaction commits (validated now, delivered by the relay)."""
    Event(name, {})  # validates the name early: a typo fails the business transaction, not the job
    await enqueue(
        conn,
        HOOK_KIND,
        {"event": name, "payload": _plain(payload)},
        queue=HOOK_QUEUE,
        lane=lane,
        max_attempts=5,
        caused_by=caused_by,
    )


class EventRelay:
    """Job handler that publishes queued subscription events on the bus. Register :meth:`handlers`."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def handlers(self) -> dict[str, Handler]:
        return {HOOK_KIND: self._relay}

    async def _relay(self, job: Job, ctx: JobContext) -> None:
        name = job.payload.get("event")
        payload = job.payload.get("payload")
        if not isinstance(name, str) or not isinstance(payload, Mapping):
            raise PermanentJobError("повреждённое событие подписки")
        failed = await self._bus.publish(Event(name, payload))
        if failed:
            # Subscribers own their reliability (they enqueue their own jobs); the relay never loops.
            log.warning("event %s: %d subscriber(s) failed", name, failed)
