"""Webhook inbox: fast intake (< 50 ms) and background processing with GET confirmation (02 §5.3–5.6).

Intake (:func:`store`, called by the ``/webhooks/remnawave`` route after the HMAC check): a slim, secret-free
copy of the event goes into ``rw_inbox`` keyed by ``sha256(raw body)`` with ``ON CONFLICT DO NOTHING`` — the
panel's retries resend the same bytes, so a body is processed once however often it arrives.

Processing (:class:`InboxProcessor`, background):

* events of one panel user are handled in ``timestamp`` order; a batch of events of one user costs one
  ``GET``;
* state-changing ``user.*`` events are **confirmed** with ``GET /users/{id}`` and projected from the fresh
  state (the webhook body is a hint: delivery order is not guaranteed and ``updatedAt`` is not a version);
  only when the panel is unreachable is the webhook's own data used, and only if it is newer than the stored
  snapshot;
* unknown events are stored and marked ``skipped``; events of panel users the bot does not know are skipped
  and announced on the bus (claims / import live in stage 2);
* owner-relevant events (nodes, API token deleted, panel restarted, CRM, errors) are published on the bus for
  the admin chat; user-facing ones carry ``echo=True`` when they are the echo of our own recent write (02
  §5.5);
* more than ``mass_threshold`` pending state-changing ``user.*`` events → one reconciliation pass instead of
  N GETs; the events are marked done only when the pass finished ``ok`` and only those received before it
  started — a busy/aborted/failed pass leaves them to the normal path (and mass mode pauses for a while);
* ``service.login_attempt_*`` are **security** events, not owner chat events: the login is stored masked
  (``ad…(12)``: admins type passwords into the login field) and they are never relayed to the admin group;
* ``service.api_token_deleted`` for **our** token (uuid from the JWT) opens the breaker and raises a critical
  «Требует внимания» item;
* processed rows are kept (hash only, ``slim`` emptied after 72 h) for longer than the accepted webhook age,
  so a replayed signed body inside the acceptance window is still a duplicate;
* a failing item is retried with backoff and isolated through the error hub; it never stops the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import msgspec
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.bus import Event
from svbg.core.clock import now
from svbg.core.component import fix_setting
from svbg.jobs.tables import jobs
from svbg.remnawave.capabilities import jwt_claims
from svbg.remnawave.contributors import SquadContributors
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.models import PanelUser
from svbg.remnawave.projection import apply, compute, mark_missing, publish, select_subscriptions
from svbg.remnawave.tables import rw_inbox
from svbg.remnawave.transport import Lane
from svbg.remnawave.webhooks import MAX_AGE, MAX_FUTURE, WebhookEnvelope
from svbg.remnawave.writer import QUEUE, ordering_key
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.jobs._support import ErrorCapture
    from svbg.jobs.scheduler import Scheduler
    from svbg.remnawave.api import RemnawaveApi

__all__ = [
    "ATTENTION_API_GONE",
    "CONFIRM_EVENTS",
    "KNOWN_EVENTS",
    "OWNER_EVENTS",
    "RETENTION",
    "SECURITY_EVENTS",
    "SLIM_TTL",
    "InboxProcessor",
    "mask_login",
    "slim_payload",
    "store",
]

log = logging.getLogger("svbg.remnawave.inbox")

#: The panel's event catalogue (3.4.x, ``RWC/constants/events/events.ts``).
KNOWN_EVENTS: Final = frozenset(
    {
        "user.created",
        "user.modified",
        "user.deleted",
        "user.revoked",
        "user.disabled",
        "user.enabled",
        "user.limited",
        "user.expired",
        "user.traffic_reset",
        "user.first_connected",
        "user.bandwidth_usage_threshold_reached",
        "user.not_connected",
        "user.expiration",
        "user_hwid_devices.added",
        "user_hwid_devices.deleted",
        "node.created",
        "node.modified",
        "node.disabled",
        "node.enabled",
        "node.deleted",
        "node.connection_lost",
        "node.connection_restored",
        "node.traffic_notify",
        "service.panel_started",
        "service.login_attempt_failed",
        "service.login_attempt_success",
        "service.subpage_config_changed",
        "service.api_token_created",
        "service.api_token_deleted",
        "errors.bandwidth_usage_threshold_reached_max_notifications",
        "crm.infra_billing_node_payment_in_7_days",
        "crm.infra_billing_node_payment_in_48hrs",
        "crm.infra_billing_node_payment_in_24hrs",
        "crm.infra_billing_node_payment_due_today",
        "crm.infra_billing_node_payment_overdue_24hrs",
        "crm.infra_billing_node_payment_overdue_48hrs",
        "crm.infra_billing_node_payment_overdue_7_days",
        "torrent_blocker.report",
    }
)
#: ``user.*`` events whose state is confirmed by ``GET /users/{id}`` before projection (02 §5.6).
CONFIRM_EVENTS: Final = frozenset(
    {
        "user.created",
        "user.modified",
        "user.deleted",
        "user.revoked",
        "user.disabled",
        "user.enabled",
        "user.limited",
        "user.expired",
        "user.traffic_reset",
        "user.first_connected",
    }
)
#: Events the owner cares about (admin chat topics «Панель и ноды» / «Ошибки»).
OWNER_EVENTS: Final = frozenset(
    {
        "node.connection_lost",
        "node.connection_restored",
        "node.traffic_notify",
        "service.panel_started",
        "service.api_token_deleted",
        "errors.bandwidth_usage_threshold_reached_max_notifications",
        "torrent_blocker.report",
    }
)
#: Optional security alerts (02 §5.4, off by default): never posted to the admin group (its members are not
#: necessarily bot admins, and the «login» is often a mistyped password). Published with ``security=True``.
SECURITY_EVENTS: Final = frozenset({"service.login_attempt_failed", "service.login_attempt_success"})
#: Our operation kinds whose echo each event is (02 §5.5).
ECHO_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "user.created": ("panel.create",),
    "user.modified": ("panel.update", "panel.renew"),
    "user.revoked": ("panel.revoke",),
    "user.traffic_reset": ("panel.reset_traffic",),
    "user.enabled": ("panel.enable", "panel.reset_traffic"),
    "user.disabled": ("panel.disable",),
    "user.deleted": ("panel.delete",),
}
ECHO_WINDOW: Final = timedelta(minutes=2)
MAX_ATTEMPTS: Final = 5
LEASE: Final = timedelta(seconds=60)
#: Processed rows are deleted after this age. It must exceed the oldest timestamp the route accepts
#: (``MAX_AGE`` + ``MAX_FUTURE``): the hash is the only replay protection of a signed body (02 §0.1 п.4).
RETENTION: Final = MAX_AGE + MAX_FUTURE + timedelta(days=1)
#: After this age a processed row keeps only its hash: ``slim``/``note`` are emptied (02 §5.3 п.5).
SLIM_TTL: Final = timedelta(hours=72)
#: After a mass reconciliation attempt (whatever its result) events are handled one by one for a while.
MASS_COOLDOWN_S: Final = 300.0
#: Webhook data replaces the snapshot (panel down) only when it is newer than it by more than this: the
#: snapshot time is the bot's clock, the payload timestamp the panel's (02 §5.6).
FALLBACK_SKEW: Final = timedelta(seconds=5)
ATTENTION_API_GONE: Final = "rw:token_deleted"
_TXT_API_GONE_TITLE = "API-токен бота удалён в панели"
_TXT_API_GONE_BODY = (
    "В панели удалён API-токен «{name}», которым пользуется бот. Запросы к панели остановлены, изменения "
    "подписок ждут в очереди. Создайте новый токен в панели и вставьте его: «⚙️ Настройки → Remnawave» или "
    "/setup."
)
_EVENT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")

_USER_KEYS: Final = (
    "id",
    "shortUuid",
    "username",
    "status",
    "trafficLimitBytes",
    "trafficLimitStrategy",
    "expireAt",
    "telegramId",
    "tag",
    "hwidDeviceLimit",
    "externalSquadUuid",
    "lastTriggeredThreshold",
    "subRevokedAt",
    "lastTrafficResetAt",
    "createdAt",
    "subscriptionUrl",
)
_TRAFFIC_KEYS: Final = ("usedTrafficBytes", "onlineAt", "firstConnectedAt")
_NODE_KEYS: Final = (
    "uuid",
    "name",
    "countryCode",
    "isConnected",
    "isDisabled",
    "lastStatusMessage",
    "trafficUsedBytes",
)
_DEVICE_KEYS: Final = ("hwid", "platform", "osVersion", "deviceModel", "createdAt")


def mask_login(value: Any) -> str | None:
    """``ad…(12)``: enough to recognise one's own login, useless as a leaked password (02 §5.4)."""
    if not isinstance(value, str) or not value:
        return None
    n = len(value)
    head = value[:2] if n >= 6 else ""
    return f"{head}…({n})"


def _pick(src: Any, keys: Sequence[str]) -> dict[str, Any]:
    if not isinstance(src, Mapping):
        return {}
    return {k: src[k] for k in keys if k in src}


def _slim_user(src: Any) -> dict[str, Any]:
    user = _pick(src, _USER_KEYS)
    if not user:
        return {}
    traffic = _pick(src.get("userTraffic"), _TRAFFIC_KEYS)
    for key in _TRAFFIC_KEYS:  # older payloads keep traffic fields at the top level
        if key not in traffic and key in src:
            traffic[key] = src[key]
    if traffic:
        user["userTraffic"] = traffic
    squads = src.get("activeInternalSquads")
    if isinstance(squads, list):
        user["activeInternalSquads"] = [
            {"uuid": s["uuid"]} for s in squads if isinstance(s, Mapping) and isinstance(s.get("uuid"), str)
        ]
    return user


def slim_payload(env: WebhookEnvelope) -> dict[str, Any]:
    """The part of ``data``/``meta`` the bot uses — no secrets, no e-mail, no IP addresses."""
    data = env.data
    out: dict[str, Any] = {}
    if env.scope == "user":
        out["user"] = _slim_user(data)
    elif env.scope in ("user_hwid_devices", "torrent_blocker"):
        out["user"] = _slim_user(data.get("user"))
        if env.scope == "user_hwid_devices":
            out["device"] = _pick(data.get("hwidUserDevice"), _DEVICE_KEYS)
        else:
            out["node"] = _pick(data.get("node"), _NODE_KEYS)
            report = data.get("report")
            if isinstance(report, Mapping):
                out["report"] = _pick(report, ("willUnblockAt", "blockedAt"))
    elif env.scope == "node":
        out["node"] = _pick(data, _NODE_KEYS)
    elif env.scope == "service":
        if isinstance(data.get("panelVersion"), str):
            out["panelVersion"] = data["panelVersion"][:64]
        token = data.get("apiToken")
        if isinstance(token, Mapping):
            slim_token = _pick(token, ("uuid",))
            name = token.get("name", token.get("tokenName"))  # 3.4.x: ``name``
            if isinstance(name, str):
                slim_token["tokenName"] = name[:100]
            out["apiToken"] = slim_token
        attempt = data.get("loginAttempt")
        if isinstance(attempt, Mapping):  # the password was stripped by parse_envelope already
            slim_attempt: dict[str, Any] = {}
            masked = mask_login(attempt.get("username"))  # often a mistyped password: never stored clear
            if masked is not None:
                slim_attempt["username"] = masked
            if isinstance(attempt.get("description"), str):
                slim_attempt["description"] = attempt["description"][:200]
            out["loginAttempt"] = slim_attempt
    elif env.scope == "errors":
        if isinstance(data.get("description"), str):
            out["description"] = data["description"][:500]
    elif env.scope == "crm":
        out["crm"] = _pick(data, ("providerName", "nodeName", "nextBillingAt", "loginUrl"))
    if env.meta:
        out["meta"] = _pick(env.meta, ("expiration", "notConnectedAfterHours"))
    return out


async def store(conn: AsyncConnection, body_hash: str, env: WebhookEnvelope) -> bool:
    """Insert one delivery into the inbox. ``False`` = a duplicate of an already stored body."""
    stmt = (
        pg_insert(rw_inbox)
        .values(
            hash=body_hash,
            ts=env.timestamp,
            scope=env.scope[:64],
            event=env.event[:128],
            panel_user_id=env.panel_user_id,
            slim=slim_payload(env),
        )
        .on_conflict_do_nothing(index_elements=[rw_inbox.c.hash])
        .returning(rw_inbox.c.hash)
    )
    return (await conn.execute(stmt)).first() is not None


@dataclass(slots=True)
class _Item:
    hash: str
    ts: datetime
    scope: str
    event: str
    panel_user_id: int | None
    slim: dict[str, Any]
    attempts: int
    status: str = "done"
    note: str | None = None


@dataclass(slots=True)
class InboxStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    confirmed: int = 0
    fallback: int = 0
    last_batch_at: datetime | None = None
    by_event: dict[str, int] = field(default_factory=dict)


class InboxProcessor:
    """Background consumer of ``rw_inbox``. One per process is enough; several are safe (row leases)."""

    def __init__(
        self,
        db: Database,
        api: Callable[[], RemnawaveApi],
        *,
        contributors: SquadContributors,
        attention: AttentionService | None = None,
        bus: EventBus | None = None,
        hub: ErrorCapture | None = None,
        on_panel_started: Callable[[], Awaitable[None]] | None = None,
        on_mass: Callable[[], Awaitable[Any]] | None = None,
        panel_up: Callable[[], bool] | None = None,
        batch: int = 50,
        poll_interval: float = 5.0,
        mass_threshold: int = 200,
        mass_cooldown: float = MASS_COOLDOWN_S,
    ) -> None:
        """``on_mass`` runs one reconciliation pass and returns its report: the events it replaces are marked
        done only when the report says ``status == "ok"`` (``None``/``True`` also count as success, for
        simple callbacks). ``panel_up`` (optional) vetoes a mass pass while the panel is unreachable."""
        self._db = db
        self._api = api
        self._contributors = contributors
        self._attention = attention
        self._bus = bus
        self._hub = hub
        self._on_panel_started = on_panel_started
        self._on_mass = on_mass
        self._panel_up = panel_up
        self._mass_cooldown = mass_cooldown
        self._mass_paused_until = 0.0
        self._batch = batch
        self._poll = poll_interval
        self._mass_threshold = mass_threshold
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.stats = InboxStats()

    # ---------------------------------------------------------------------------------------- lifecycle

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="remnawave:inbox")

    async def stop(self, timeout: float = 10.0) -> None:  # noqa: ASYNC109 - graceful deadline
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping.set()
        self._wake.set()
        try:
            async with asyncio.timeout(timeout):
                await task
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        delay = 0.5
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                n = await self.run_once()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive a DB outage
                log.warning("remnawave inbox: batch failed: %s", type(exc).__name__)
                await self._capture(
                    exc, "remnawave:inbox", "Обработка вебхуков продолжится через несколько секунд."
                )
                n = 0
                await self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            if n >= self._batch:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _capture(self, exc: BaseException, place: str, handled: str) -> None:
        if self._hub is None:
            return
        try:
            await self._hub.capture(exc, place, module="remnawave", handled=handled)
        except Exception:
            log.exception("error hub failed")

    # ------------------------------------------------------------------------------------------- batch

    async def _claim(self) -> list[_Item]:
        picked = (
            sa.select(rw_inbox.c.hash)
            .where(
                rw_inbox.c.status == "new",
                sa.or_(rw_inbox.c.locked_until.is_(None), rw_inbox.c.locked_until < sa.func.now()),
            )
            .order_by(rw_inbox.c.ts, rw_inbox.c.hash)
            .limit(self._batch)
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        stmt = (
            sa.update(rw_inbox)
            .where(rw_inbox.c.hash.in_(sa.select(picked.c.hash)))
            .values(locked_until=sa.func.now() + LEASE, attempts=rw_inbox.c.attempts + 1)
            .returning(
                rw_inbox.c.hash,
                rw_inbox.c.ts,
                rw_inbox.c.scope,
                rw_inbox.c.event,
                rw_inbox.c.panel_user_id,
                rw_inbox.c.slim,
                rw_inbox.c.attempts,
            )
        )
        async with self._db.tx() as conn:
            rows = (await conn.execute(stmt)).all()
        items = [
            _Item(r.hash, r.ts, r.scope, r.event, r.panel_user_id, dict(r.slim or {}), r.attempts)
            for r in rows
        ]
        items.sort(key=lambda i: (i.ts, i.hash))
        return items

    async def run_once(self) -> int:
        """Process one batch; returns the number of inbox rows handled."""
        if self._on_mass is not None and time.monotonic() >= self._mass_paused_until:
            cutoff = await self._mass_cutoff()
            if cutoff is not None:
                await self._mass_sync(cutoff)
        items = await self._claim()
        if not items:
            return 0
        by_user: dict[int, list[_Item]] = {}
        others: list[_Item] = []
        for item in items:
            if item.panel_user_id is not None and item.event in KNOWN_EVENTS:
                by_user.setdefault(item.panel_user_id, []).append(item)
            else:
                others.append(item)
        for uid, group in by_user.items():
            await self._guarded(group, self._handle_user(uid, group))
        for item in others:
            await self._guarded([item], self._handle_other(item))
        await self._finish(items)
        self.stats.last_batch_at = now()
        return len(items)

    async def _guarded(self, group: list[_Item], coro: Awaitable[None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad event never blocks the inbox
            retry = isinstance(exc, RemnawaveError) and exc.kind is ErrorKind.TRANSIENT
            for item in group:
                item.status = "new" if item.attempts < MAX_ATTEMPTS else "error"
                item.note = f"{type(exc).__name__}: {str(exc)[:300]}"
            if not retry or group[0].attempts >= MAX_ATTEMPTS:
                await self._capture(
                    exc,
                    "remnawave:inbox:event",
                    "Событие панели будет обработано повторно; состояние также восстановит сверка.",
                )

    async def _finish(self, items: Sequence[_Item]) -> None:
        async with self._db.tx() as conn:
            for status in ("done", "skipped", "error", "new"):
                part = [i for i in items if i.status == status]
                if not part:
                    continue
                for note in {i.note for i in part}:
                    hashes = [i.hash for i in part if i.note == note]
                    values: dict[str, Any] = {"status": status, "note": note}
                    if status == "new":
                        backoff = timedelta(seconds=min(2 ** max(i.attempts for i in part), 600))
                        values["locked_until"] = sa.func.now() + backoff
                    else:
                        values["processed_at"] = sa.func.now()
                        values["locked_until"] = None
                    await conn.execute(
                        sa.update(rw_inbox).where(rw_inbox.c.hash.in_(hashes)).values(**values)
                    )
        for i in items:
            if i.status == "done":
                self.stats.processed += 1
            elif i.status == "skipped":
                self.stats.skipped += 1
            elif i.status == "error":
                self.stats.failed += 1
            self.stats.by_event[i.event] = self.stats.by_event.get(i.event, 0) + 1

    # ---------------------------------------------------------------------------------------- mass mode

    @staticmethod
    def _mass_rows() -> list[sa.ColumnElement[bool]]:
        """Rows a reconciliation pass replaces: claimable, state-changing ``user.*`` events (02 §5.5)."""
        return [
            rw_inbox.c.status == "new",
            rw_inbox.c.event.in_(sorted(CONFIRM_EVENTS)),
            sa.or_(rw_inbox.c.locked_until.is_(None), rw_inbox.c.locked_until < sa.func.now()),
        ]

    async def _mass_cutoff(self) -> datetime | None:
        """``received_at`` of the newest replaceable event when there are more than ``mass_threshold``."""
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(sa.func.count(), sa.func.max(rw_inbox.c.received_at)).where(*self._mass_rows())
                )
            ).one()
        n, newest = int(row[0] or 0), row[1]
        return newest if n > self._mass_threshold else None

    async def _mass_sync(self, cutoff: datetime) -> None:
        """Hundreds of user events (bulk action in the panel): one stream pass instead of N GETs (02 §5.5).

        Only a pass that finished ``ok`` replaces events, and only those received before it started (later
        ones may describe changes the stream had already passed). A busy/aborted/failing pass replaces
        nothing: the events take the normal path, owner events are not held up, and mass mode pauses.
        """
        assert self._on_mass is not None
        self._mass_paused_until = time.monotonic() + self._mass_cooldown
        if self._panel_up is not None and not self._panel_up():
            return
        try:
            result = await self._on_mass()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the events are processed one by one instead
            log.warning("remnawave inbox: mass reconciliation failed: %s", type(exc).__name__)
            await self._capture(
                exc, "remnawave:inbox:mass", "События панели будут обработаны по одному (GET на каждое)."
            )
            return
        if not _report_ok(result):
            log.info("remnawave inbox: mass reconciliation not ok: %s", getattr(result, "status", result))
            return
        stmt = (
            sa.update(rw_inbox)
            .where(*self._mass_rows(), rw_inbox.c.received_at <= cutoff)
            .values(status="done", note="обработано сверкой", processed_at=sa.func.now(), locked_until=None)
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)

    # -------------------------------------------------------------------------------------- user events

    async def _handle_user(self, uid: int, group: list[_Item]) -> None:
        async with self._db.read() as conn:
            row = (
                (await conn.execute(select_subscriptions().where(subscriptions.c.panel_user_id == uid)))
                .mappings()
                .first()
            )
        if row is None:
            await self._unknown_user(uid, group)
            return
        if row["link_state"] == "closed":
            for item in group:  # the panel user of a closed subscription: history only, nobody is notified
                item.status, item.note = "skipped", "подписка закрыта"
            return
        sid = int(row["id"])
        state_events = [i for i in group if i.event in CONFIRM_EVENTS]
        if state_events:
            await self._confirm(row, state_events)
        for item in group:
            echo = await self._is_echo(sid, item.event)
            self._emit(
                f"remnawave.{item.event}",
                {
                    "subscription_id": sid,
                    "panel_user_id": uid,
                    "echo": echo,
                    "ts": item.ts.isoformat(),
                    "meta": item.slim.get("meta") or {},
                    "device": item.slim.get("device") or {},
                },
            )

    async def _confirm(self, row: Mapping[str, Any], items: list[_Item]) -> None:
        """GET is the truth; webhook data is used only when the panel is unreachable and it is newer."""
        sid = int(row["id"])
        uid = int(row["panel_user_id"])
        latest = items[-1]
        try:
            # The read time is taken BEFORE the request: a write stored meanwhile (its ``panel_state_ts`` is
            # later) makes this answer stale instead of letting it roll the snapshot back.
            fetched_at = now()
            user = await self._api().get_user(uid, lane=Lane.BACKGROUND)
            state_ts = fetched_at
            self.stats.confirmed += 1
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                if any(i.event == "user.deleted" for i in items) and await self._is_echo(sid, "user.deleted"):
                    return  # our own delete: the writer already closed the subscription
                await mark_missing(
                    self._db,
                    sid,
                    source="webhook",
                    username=row.get("panel_username"),
                    attention=self._attention,
                    bus=self._bus,
                )
                return
            if err.kind not in (ErrorKind.TRANSIENT, ErrorKind.PROXY_CHECK, ErrorKind.SERVER, ErrorKind.AUTH):
                raise
            fallback = self._from_webhook(latest)
            if fallback is None:
                raise
            prev = row.get("panel_state_ts")
            if prev is not None and latest.ts <= prev + FALLBACK_SKEW:
                for item in items:  # not clearly newer than our snapshot (two clocks): keep the snapshot
                    item.note = "панель недоступна: событие не новее сохранённого состояния"
                return
            user, state_ts = fallback, latest.ts
            self.stats.fallback += 1
            for item in items:
                item.note = "панель недоступна: применены данные вебхука"
        if latest.event == "user.deleted":
            return  # deleted in the webhook, but GET found the user: nothing to change
        await self._project(row, user, state_ts, source="webhook")

    @staticmethod
    def _from_webhook(item: _Item) -> PanelUser | None:
        data = item.slim.get("user")
        if not isinstance(data, Mapping) or not data:
            return None
        try:
            return msgspec.convert(data, PanelUser, strict=False)
        except msgspec.ValidationError:
            return None

    async def _project(
        self, row: Mapping[str, Any], user: PanelUser, state_ts: datetime, *, source: str
    ) -> None:
        sid = int(row["id"])
        async with self._db.read() as conn:
            twins = await self._contributors.twins(conn)
            owners = await self._contributors.twin_owners(conn)
            subs = await self._contributors.load(conn, sid)
            pending = await _has_pending(conn, sid)
        result = compute(
            row,
            user,
            twins=twins,
            substitutions=subs,
            frozen=bool(
                self._contributors.frozen_modules(subs, panel_squads=user.squad_uuids, twin_owners=owners)
            ),
            pending=pending,
            state_ts=state_ts,
            source=source,
        )
        if result.stale or not result.changed:
            return
        async with self._db.tx() as conn:
            written = await apply(conn, row, result)
        if written:
            await publish(result, attention=self._attention, bus=self._bus)

    async def _is_echo(self, sid: int, event: str) -> bool:
        kinds = ECHO_KINDS.get(event)
        if not kinds:
            return False
        async with self._db.read() as conn:
            found = await conn.scalar(
                sa.select(sa.literal(1))
                .where(
                    jobs.c.ordering_key == ordering_key(sid),
                    jobs.c.queue == QUEUE,
                    jobs.c.status == "done",
                    jobs.c.kind.in_(kinds),
                    jobs.c.done_at > sa.func.now() - ECHO_WINDOW,
                )
                .limit(1)
            )
        return found is not None

    async def _unknown_user(self, uid: int, group: list[_Item]) -> None:
        tg: Any = None
        for item in group:
            user = item.slim.get("user")
            if isinstance(user, Mapping) and user.get("telegramId") is not None:
                tg = user.get("telegramId")
        for item in group:
            item.status, item.note = "skipped", "пользователь панели не связан с подпиской бота"
        self._emit(
            "remnawave.unknown_user",
            {
                "panel_user_id": uid,
                "telegram_id": tg if isinstance(tg, int) else None,
                "events": [i.event for i in group],
            },
        )

    # ------------------------------------------------------------------------------------- other events

    async def _handle_other(self, item: _Item) -> None:
        if item.event not in KNOWN_EVENTS or not _EVENT_NAME_RE.match(item.event):
            item.status, item.note = "skipped", "неизвестное событие панели"
            return
        if item.scope in ("user", "user_hwid_devices", "torrent_blocker") and item.panel_user_id is None:
            item.status, item.note = "skipped", "в событии нет id пользователя"
            return
        security = item.event in SECURITY_EVENTS
        payload: dict[str, Any] = {
            "event": item.event,
            "ts": item.ts.isoformat(),
            "owner": not security and (item.event in OWNER_EVENTS or item.scope in ("crm", "errors")),
            "security": security,
        }
        payload.update({k: v for k, v in item.slim.items() if k != "user"})
        if item.event == "service.api_token_deleted":
            payload["ours"] = await self._token_deleted(item.slim.get("apiToken"))
        self._emit(f"remnawave.{item.event}", payload)
        if item.event == "service.panel_started" and self._on_panel_started is not None:
            await self._on_panel_started()

    async def _token_deleted(self, token: Any) -> bool:
        """02 §2.3: our token deleted → breaker OPEN at once (not at the first 401) + a critical alert."""
        deleted = token.get("uuid") if isinstance(token, Mapping) else None
        if not isinstance(deleted, str) or not deleted:
            return False
        try:
            transport = self._api().transport
            claims = jwt_claims(transport.config.token)
        except Exception:  # noqa: BLE001 - panel not connected / no token: it cannot be ours
            return False
        if not claims or claims.get("uuid") != deleted:
            return False
        log.warning("remnawave: the bot's API token was deleted in the panel; requests are stopped")
        try:
            transport.breaker.trip()
        except Exception:
            log.exception("could not open the panel breaker")
        if self._attention is not None:
            name = token.get("tokenName") if isinstance(token, Mapping) else None
            try:
                await self._attention.raise_item(
                    ATTENTION_API_GONE,
                    "error",
                    _TXT_API_GONE_TITLE,
                    _TXT_API_GONE_BODY.format(name=str(name or "—")[:100]),
                    fix_action=fix_setting("REMNAWAVE_TOKEN"),
                )
            except Exception:
                log.exception("could not raise %s", ATTENTION_API_GONE)
        return True

    def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._bus is None or not _EVENT_NAME_RE.match(name):
            return
        try:
            self._bus.publish_nowait(Event(name, dict(payload)))
        except Exception:
            log.exception("could not publish %s", name)

    # ----------------------------------------------------------------------------------------- upkeep

    def register(self, scheduler: Scheduler) -> None:
        """Daily purge: ``slim`` emptied after 72 h (02 §5.3 п.5), rows deleted after :data:`RETENTION`."""

        async def purge() -> None:
            await self.purge()

        scheduler.every("remnawave.inbox.purge", 86_400, purge, jitter_s=600)

    async def purge(self) -> int:
        """Delete processed rows older than :data:`RETENTION`, empty ``slim`` after :data:`SLIM_TTL`.

        The hash outlives the webhook acceptance window, so a replayed signed body is always a duplicate.
        Returns the number of deleted rows.
        """
        delete = sa.delete(rw_inbox).where(
            rw_inbox.c.received_at < sa.func.now() - RETENTION, rw_inbox.c.status != "new"
        )
        shrink = (
            sa.update(rw_inbox)
            .where(
                rw_inbox.c.received_at < sa.func.now() - SLIM_TTL,
                rw_inbox.c.status != "new",
                rw_inbox.c.slim != sa.text("'{}'::jsonb"),
            )
            .values(slim=sa.text("'{}'::jsonb"), note=None)
        )
        async with self._db.tx() as conn:
            deleted = (await conn.execute(delete)).rowcount or 0
            await conn.execute(shrink)
        return deleted


def _report_ok(result: Any) -> bool:
    """Did ``on_mass`` finish its pass? A ``SyncReport`` must say ``ok``; ``None``/``True`` mean success."""
    if result is None or result is True:
        return True
    return getattr(result, "status", None) == "ok"


async def _has_pending(conn: AsyncConnection, sid: int) -> bool:
    found = await conn.scalar(
        sa.select(sa.literal(1))
        .where(
            jobs.c.ordering_key == ordering_key(sid),
            jobs.c.queue == QUEUE,
            jobs.c.status.in_(("ready", "running")),
        )
        .limit(1)
    )
    return found is not None
