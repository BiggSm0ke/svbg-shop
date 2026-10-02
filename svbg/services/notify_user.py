"""User notifications with deduplication (07 §5 stage 2, 02 §5.4–5.5, 06 M10).

One fact reaches the user once, whoever reported it — the panel's webhook or the bot's own fallback scheduler:
every notification is a row of ``notification_log`` with ``UNIQUE(target, kind, anchor)`` inserted with
``ON CONFLICT DO NOTHING`` **in the same transaction** as its delivery job (``notify.user``), so a duplicate
never even gets a job and a rolled back change never notifies.

Kinds (``target`` is ``sub:<id>``; ``anchor`` pins the fact):

========================  =====================================================  ==========================
kind                      source                                                 anchor
========================  =====================================================  ==========================
``expiring_<H>h``         ``user.expiration`` (``meta.expiration < 0``), scanner  ``paid_until`` (epoch)
``trial_ending``          scanner (``NOTIFY_TRIAL_ENDING_HOURS`` before the end)  ``paid_until``
``expired``               ``user.expired``, ``user.expiration > 0``, scanner      ``paid_until``
``traffic``               ``user.bandwidth_usage_threshold_reached``             ``<percent>:<last reset>``
``limited``               ``user.limited``                                       last traffic reset
``first_connected``       ``user.first_connected``                               ``1`` (once per subscription)
``device_added``          ``user_hwid_devices.added``                            device fingerprint + time
``revoked``               ``user.revoked`` (not the echo of the bot's own revoke) event time
========================  =====================================================  ==========================

Both sources share the anchor of the expiry reminders, so the panel and the scanner never double a reminder;
a renewal moves ``paid_until`` and so starts a fresh set. Each kind has an owner toggle
``NOTIFY_USER_<KIND>`` (default on) checked when the fact is recorded **and** before sending. Blocked, banned
and frozen users are skipped. The scanner is one ``INSERT … SELECT`` per tick.

Rendering and sending belong to the Telegram layer (:class:`NotificationSender`); this module never imports
aiogram.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

import svbg.subscriptions.tables  # noqa: F401 - ``subscriptions`` must be on the metadata for the foreign key
from svbg.core.clock import now
from svbg.core.tables import users
from svbg.db.meta import JSONB, UtcDateTime, metadata, now_default
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import PermanentJobError
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.bus import Event, EventBus
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import Handler, JobContext

__all__ = [
    "BASE_KINDS",
    "JOB_KIND",
    "STATUSES",
    "TOGGLES",
    "Notification",
    "NotificationSender",
    "NotifyUser",
    "base_kind",
    "notification_log",
]

log = logging.getLogger("svbg.services.notify_user")

STATUSES: Final = ("pending", "sent", "skipped")
BASE_KINDS: Final = (
    "expiring",
    "trial_ending",
    "expired",
    "traffic",
    "limited",
    "first_connected",
    "device_added",
    "revoked",
)
#: Base kind → its owner toggle (registry key, bool, default ``True``).
TOGGLES: Final[Mapping[str, str]] = {
    "expiring": "NOTIFY_USER_EXPIRING",
    "trial_ending": "NOTIFY_USER_TRIAL_ENDING",
    "expired": "NOTIFY_USER_EXPIRED",
    "traffic": "NOTIFY_USER_TRAFFIC",
    "limited": "NOTIFY_USER_TRAFFIC",
    "first_connected": "NOTIFY_USER_FIRST_CONNECTED",
    "device_added": "NOTIFY_USER_DEVICES",
    "revoked": "NOTIFY_USER_REVOKED",
}
JOB_KIND: Final = "notify.user"
JOB_QUEUE: Final = "notify"
DEFAULT_EXPIRING_HOURS: Final = (72, 24)
DEFAULT_TRIAL_HOURS: Final = 2
#: Expired reminders are sent only for a subscription that ended within this window (no old spam).
EXPIRED_WINDOW: Final = timedelta(days=1)
#: Expiry facts whose ``paid_until`` moved further than this from the panel's report are stale (renewed).
RENEWED_SLACK: Final = timedelta(hours=1)
PURGE_DAYS: Final = 180
_TRAFFIC_STEP: Final = 10

notification_log = sa.Table(
    "notification_log",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("target", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("anchor", sa.Text, nullable=False),
    sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
    sa.Column(
        "subscription_id",
        sa.BigInteger,
        sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=True,
    ),
    sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    sa.Column("reason", sa.Text, nullable=True),
    sa.Column("created_at", UtcDateTime, nullable=False, server_default=now_default()),
    sa.Column("sent_at", UtcDateTime, nullable=True),
    sa.UniqueConstraint("target", "kind", "anchor", name="uq_notification_log_target_kind_anchor"),
    sa.CheckConstraint("status IN ('pending', 'sent', 'skipped')", name="status"),
    sa.CheckConstraint("length(target) > 0 AND length(kind) > 0 AND length(anchor) > 0", name="not_empty"),
    sa.Index("ix_notification_log_created_at", "created_at"),
)


def base_kind(kind: str) -> str:
    """``expiring_24h`` → ``expiring``; other kinds are their own base."""
    return "expiring" if kind.startswith("expiring_") else kind


def epoch_anchor(value: datetime | None) -> str:
    return "0" if value is None else str(int(value.timestamp()))


@dataclass(frozen=True, slots=True)
class Notification:
    """Everything the sender needs (no further SQL)."""

    id: int
    kind: str
    base: str
    user_id: int
    telegram_id: int
    lang: str | None
    subscription_id: int | None = None
    paid_until: datetime | None = None
    is_trial: bool = False
    subscription_url: str | None = None
    plan_snapshot: Mapping[str, Any] = field(default_factory=dict)
    used_traffic: int | None = None
    traffic_limit: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> bool:
        """Render and send. ``False``: not delivered for good (the user blocked the bot)."""
        ...


Config = Callable[[], Mapping[str, Any]]

_SCAN_SQL: Final = sa.text(
    """
    WITH cand AS (
        SELECT s.id AS sid, s.user_id, s.is_trial,
               floor(extract(epoch FROM s.paid_until))::bigint AS ts,
               extract(epoch FROM (s.paid_until - :now)) AS left_s
        FROM subscriptions s JOIN users u ON u.id = s.user_id
        WHERE s.link_state = 'linked' AND s.hold_kind IS NULL AND s.paid_until IS NOT NULL
          AND u.banned_at IS NULL AND u.bot_blocked_at IS NULL AND u.telegram_id IS NOT NULL
          AND s.paid_until > :expired_since AND s.paid_until <= :horizon
    ), kinds AS (
        SELECT sid, user_id, ts,
               CASE
                 WHEN left_s <= 0 THEN 'expired'
                 WHEN is_trial THEN CASE WHEN left_s <= :trial_s THEN 'trial_ending' END
                 ELSE (SELECT 'expiring_' || min(h) || 'h' FROM unnest(:hours) AS h WHERE h * 3600 >= left_s)
               END AS kind
        FROM cand
    )
    INSERT INTO notification_log (target, kind, anchor, user_id, subscription_id)
    SELECT 'sub:' || sid, kind, ts::text, user_id, sid FROM kinds
    WHERE (kind = 'expired' AND :expired_on)
       OR (kind = 'trial_ending' AND :trial_on)
       OR (kind LIKE 'expiring\\_%' AND :expiring_on)
    ON CONFLICT (target, kind, anchor) DO NOTHING
    RETURNING id
    """
).bindparams(sa.bindparam("hours", type_=ARRAY(sa.Integer)), sa.bindparam("now", type_=UtcDateTime))


class NotifyUser:
    """See module docstring."""

    def __init__(
        self,
        db: Database,
        *,
        config: Config,
        sender: NotificationSender | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self._db = db
        self._config = config
        self.sender = sender
        self._clock = clock

    # ------------------------------------------------------------------------------------------ settings

    def _cfg(self, key: str) -> Any:
        try:
            return self._config().get(key)
        except (RuntimeError, AttributeError):
            return None

    def enabled(self, base: str) -> bool:
        value = self._cfg(TOGGLES.get(base, ""))
        return value is not False and str(value).lower() not in ("false", "0", "off", "no")

    def expiring_hours(self) -> list[int]:
        raw = self._cfg("NOTIFY_EXPIRING_HOURS")
        if isinstance(raw, str):
            raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
        if not isinstance(raw, (list, tuple)):
            return sorted(DEFAULT_EXPIRING_HOURS)
        out: list[int] = []
        for item in raw:
            try:
                h = int(str(item).strip())
            except ValueError:
                continue
            if 1 <= h <= 24 * 60 and h not in out:
                out.append(h)
        return sorted(out)

    def trial_hours(self) -> int:
        raw = self._cfg("NOTIFY_TRIAL_ENDING_HOURS")
        try:
            value = int(raw) if raw is not None and not isinstance(raw, bool) else DEFAULT_TRIAL_HOURS
        except (TypeError, ValueError):
            value = DEFAULT_TRIAL_HOURS
        return max(0, min(value, 72))

    # ------------------------------------------------------------------------------------------ recording

    async def record(
        self,
        conn: AsyncConnection,
        *,
        target: str,
        kind: str,
        anchor: str,
        user_id: int | None,
        subscription_id: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> int | None:
        """Log the fact and queue its delivery (caller's transaction). ``None``: duplicate or switched off."""
        base = base_kind(kind)
        if base not in BASE_KINDS:
            raise ValueError(f"unknown notification kind {kind!r}")
        if not self.enabled(base):
            return None
        stmt = (
            pg_insert(notification_log)
            .values(
                target=target,
                kind=kind,
                anchor=anchor[:200],
                user_id=user_id,
                subscription_id=subscription_id,
                payload=dict(payload or {}),
            )
            .on_conflict_do_nothing(index_elements=["target", "kind", "anchor"])
            .returning(notification_log.c.id)
        )
        log_id = (await conn.execute(stmt)).scalar()
        if log_id is None:
            return None
        await self._enqueue(conn, int(log_id))
        return int(log_id)

    async def notify(self, **kwargs: Any) -> int | None:
        """:meth:`record` in its own transaction."""
        async with self._db.tx() as conn:
            return await self.record(conn, **kwargs)

    @staticmethod
    async def _enqueue(conn: AsyncConnection, log_id: int) -> None:
        await enqueue(
            conn,
            JOB_KIND,
            {"id": log_id},
            queue=JOB_QUEUE,
            lane="background",
            dedup_key=f"{JOB_KIND}:{log_id}",
            max_attempts=8,
        )

    # ------------------------------------------------------------------------------------------ scanner

    async def scan(self) -> int:
        """Fallback reminders (expiring / trial ending / expired) in one ``INSERT … SELECT``. Returns how many
        new notifications were queued."""
        hours = self.expiring_hours() if self.enabled("expiring") else []
        trial_h = self.trial_hours() if self.enabled("trial_ending") else 0
        at = self._clock()
        horizon = at + timedelta(hours=max([*hours, trial_h, 0]))
        params = {
            "now": at,
            "expired_since": at - EXPIRED_WINDOW,
            "horizon": horizon,
            "trial_s": trial_h * 3600,
            "hours": hours,
            "expired_on": self.enabled("expired"),
            "trial_on": self.enabled("trial_ending") and trial_h > 0,
            "expiring_on": bool(hours),
        }
        async with self._db.tx() as conn:
            ids = [int(r[0]) for r in (await conn.execute(_SCAN_SQL, params)).all()]
            for log_id in ids:
                await self._enqueue(conn, log_id)
        return len(ids)

    async def purge(self, older_than_days: int = PURGE_DAYS) -> int:
        cutoff = self._clock() - timedelta(days=older_than_days)
        async with self._db.tx() as conn:
            result = await conn.execute(
                sa.delete(notification_log).where(notification_log.c.created_at < cutoff)
            )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------------------------------ panel events

    def install(self, bus: EventBus) -> Callable[[], None]:
        offs = [
            bus.subscribe("remnawave.user.*", self.on_event),
            bus.subscribe("remnawave.user_hwid_devices.*", self.on_event),
        ]

        def uninstall() -> None:
            for off in offs:
                off()

        return uninstall

    async def _sub(self, sid: int) -> Mapping[str, Any] | None:
        async with self._db.read() as conn:
            return (
                (
                    await conn.execute(
                        sa.select(
                            subscriptions.c.user_id,
                            subscriptions.c.paid_until,
                            subscriptions.c.is_trial,
                            subscriptions.c.link_state,
                            subscriptions.c.panel_used_traffic,
                            subscriptions.c.panel_traffic_limit,
                            subscriptions.c.panel_last_traffic_reset_at,
                            subscriptions.c.created_at,
                        ).where(subscriptions.c.id == sid)
                    )
                )
                .mappings()
                .first()
            )

    async def on_event(self, event: Event) -> None:
        """Bus handler for the panel's user events (published by the webhook inbox)."""
        name = event.name.removeprefix("remnawave.")
        payload = event.payload
        sid = payload.get("subscription_id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            return
        row = await self._sub(sid)
        if row is None or row["user_id"] is None or row["link_state"] == "closed":
            return
        fact = self._fact(name, payload, row)
        if fact is None:
            return
        kind, anchor, extra = fact
        await self.notify(
            target=f"sub:{sid}",
            kind=kind,
            anchor=anchor,
            user_id=int(row["user_id"]),
            subscription_id=sid,
            payload=extra,
        )

    def _fact(
        self, name: str, payload: Mapping[str, Any], row: Mapping[str, Any]
    ) -> tuple[str, str, dict[str, Any]] | None:
        at = self._clock()
        paid_until: datetime | None = row["paid_until"]
        if name == "user.expiration":
            return self._expiration(payload, row, at)
        if name == "user.expired":
            if paid_until is None or paid_until > at:
                return None  # renewed meanwhile (the panel's job may run after our renewal)
            return "expired", epoch_anchor(paid_until), {}
        if name == "user.first_connected":
            return "first_connected", "1", {}
        if name == "user.bandwidth_usage_threshold_reached":
            used, limit = row["panel_used_traffic"], row["panel_traffic_limit"]
            if not used or not limit:
                return None
            percent = min(100, int(used * 100 // limit) // _TRAFFIC_STEP * _TRAFFIC_STEP)
            if percent <= 0:
                return None
            reset = row["panel_last_traffic_reset_at"] or row["created_at"]
            return "traffic", f"{percent}:{epoch_anchor(reset)}", {"percent": percent}
        if name == "user.limited":
            reset = row["panel_last_traffic_reset_at"] or row["created_at"]
            return "limited", epoch_anchor(reset), {}
        if name == "user_hwid_devices.added":
            device = payload.get("device")
            if not isinstance(device, Mapping) or not isinstance(device.get("hwid"), str):
                return None
            fp = hashlib.sha256(device["hwid"].encode()).hexdigest()[:16]
            parts = [str(device[k]) for k in ("deviceModel", "platform", "osVersion") if device.get(k)]
            return (
                "device_added",
                f"{fp}:{device.get('createdAt') or ''}",
                {"device": " · ".join(parts)[:120]},
            )
        if name == "user.revoked":
            if payload.get("echo"):
                return None  # the bot's own reissue: the user path shows the new link itself
            return "revoked", str(payload.get("ts") or epoch_anchor(at)), {}
        return None

    def _expiration(
        self, payload: Mapping[str, Any], row: Mapping[str, Any], at: datetime
    ) -> tuple[str, str, dict[str, Any]] | None:
        meta = payload.get("meta")
        raw = meta.get("expiration") if isinstance(meta, Mapping) else None
        try:
            hours = int(raw) if raw is not None and not isinstance(raw, bool) else None
        except (TypeError, ValueError):
            hours = None
        paid_until: datetime | None = row["paid_until"]
        if hours is None or paid_until is None:
            return None
        anchor = epoch_anchor(paid_until)
        if hours >= 0:
            return None if paid_until > at else ("expired", anchor, {})
        before = -hours
        if paid_until - at > timedelta(hours=before) + RENEWED_SLACK:
            return None  # the bot's paid_until is later than the panel's expiry: renewed (02 §5.4)
        if paid_until <= at:
            return None
        if row["is_trial"]:
            trial_h = self.trial_hours()
            if trial_h and before <= trial_h:
                return "trial_ending", anchor, {}
            return None
        offsets = self.expiring_hours()
        bucket = next((h for h in offsets if h >= before), None)
        if bucket is None:
            return None
        return f"expiring_{bucket}h", anchor, {}

    # ------------------------------------------------------------------------------------------ delivery

    def handlers(self) -> dict[str, Handler]:
        return {JOB_KIND: self.send_job}

    async def send_job(self, job: Job, ctx: JobContext) -> None:
        log_id = job.payload.get("id")
        if isinstance(log_id, bool) or not isinstance(log_id, int):
            raise PermanentJobError("bad payload: id")
        row = await self._load(log_id)
        if row is None or row["status"] != "pending":
            return
        kind = str(row["kind"])
        base = base_kind(kind)
        reason = self._skip_reason(row, base)
        if reason is not None:
            await self._mark(log_id, "skipped", reason)
            return
        if self.sender is None:
            await self._mark(log_id, "skipped", "no sender")
            return
        snap = row["plan_snapshot"]
        note = Notification(
            id=log_id,
            kind=kind,
            base=base,
            user_id=int(row["user_id"]),
            telegram_id=int(row["telegram_id"]),
            lang=row["language"],
            subscription_id=row["subscription_id"],
            paid_until=row["paid_until"],
            is_trial=bool(row["is_trial"]),
            subscription_url=row["subscription_url"],
            plan_snapshot=snap if isinstance(snap, Mapping) else {},
            used_traffic=row["panel_used_traffic"],
            traffic_limit=row["panel_traffic_limit"],
            payload=row["payload"] if isinstance(row["payload"], Mapping) else {},
        )
        delivered = await self.sender.send(note)
        await self._mark(log_id, "sent" if delivered else "skipped", None if delivered else "not delivered")

    def _skip_reason(self, row: Mapping[str, Any], base: str) -> str | None:
        if row["user_id"] is None or row["telegram_id"] is None:
            return "no telegram"
        if row["banned_at"] is not None:
            return "banned"
        if row["bot_blocked_at"] is not None:
            return "blocked the bot"
        if not self.enabled(base):
            return "switched off"
        if row["subscription_id"] is not None and row["link_state"] in (None, "closed"):
            return "subscription closed"
        if row["hold_kind"] is not None and base in ("expiring", "trial_ending", "expired"):
            return "frozen"
        if base in ("expiring", "trial_ending", "expired"):
            paid_until: datetime | None = row["paid_until"]
            if epoch_anchor(paid_until) != row["anchor"]:
                return "renewed"
            at = self._clock()
            if base == "expired" and paid_until is not None and paid_until > at:
                return "renewed"
            if base != "expired" and (paid_until is None or paid_until <= at):
                return "too late"
        return None

    async def _load(self, log_id: int) -> Mapping[str, Any] | None:
        stmt = (
            sa.select(
                notification_log.c.kind,
                notification_log.c.anchor,
                notification_log.c.status,
                notification_log.c.user_id,
                notification_log.c.subscription_id,
                notification_log.c.payload,
                users.c.telegram_id,
                users.c.language,
                users.c.banned_at,
                users.c.bot_blocked_at,
                subscriptions.c.link_state,
                subscriptions.c.paid_until,
                subscriptions.c.is_trial,
                subscriptions.c.subscription_url,
                subscriptions.c.plan_snapshot,
                subscriptions.c.hold_kind,
                subscriptions.c.panel_used_traffic,
                subscriptions.c.panel_traffic_limit,
            )
            .select_from(
                notification_log.outerjoin(users, users.c.id == notification_log.c.user_id).outerjoin(
                    subscriptions, subscriptions.c.id == notification_log.c.subscription_id
                )
            )
            .where(notification_log.c.id == log_id)
        )
        async with self._db.read() as conn:
            return (await conn.execute(stmt)).mappings().first()

    async def _mark(self, log_id: int, status: str, reason: str | None) -> None:
        values: dict[str, Any] = {"status": status, "reason": reason}
        if status == "sent":
            values["sent_at"] = sa.func.now()
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(notification_log)
                .where(notification_log.c.id == log_id, notification_log.c.status == "pending")
                .values(**values)
            )
