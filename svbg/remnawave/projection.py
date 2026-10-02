"""Projection: panel state → ``subscriptions.panel_*`` snapshot, manual panel edits → ``overrides`` (02 §6.3).

Rules (who owns what):

* runtime (status LIMITED/EXPIRED, traffic used, online, first connection, shortUuid/URL) — the panel: copied;
* ``expireAt`` — the bot (``paid_until``), but manual panel edits are accepted: a *longer* term is adopted
  with an audit event, a *shorter* one is adopted too and raises «Требует внимания» (paid term reduced in
  panel);
* plan fields (traffic limit, strategy, device limit, squads, external squad, tag) — the bot; a difference
  that the bot did not cause is a manual edit and goes to ``overrides[field]`` (the bot never fights it);
* ``status=DISABLED`` without our ``disabled_reason`` → ``overrides.status`` (admin of the panel disabled it);
* squads are compared **after** ``reverse`` (twin → base, 07 §2.4.3), so a module substitution never looks
  like a manual edit; an empty squad list (webhooks of some events) means "unknown" and is ignored; while
  the owner module is down (fail-closed freeze) or the writer marked a frozen change
  (``overrides._squads_pending``) a difference is the bot's own pending change, not a manual edit: no
  override, and once the module is back it is reported as drift (the writer re-sends the squads);
* while the subscription has a pending panel operation the desired side is not compared at all (02 §6.1);
* ``updatedAt`` of the panel is never used as a version (02 §0.1): stale data is detected by
  ``panel_state_ts`` (payload timestamp / our read time).

:func:`compute` is pure (row + panel user → :class:`Projection`); :func:`apply` writes it with optimistic
concurrency on ``updated_at`` (a row changed meanwhile is left for the next pass).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.bus import Event
from svbg.core.clock import now
from svbg.core.component import fix_screen
from svbg.core.tables import users
from svbg.remnawave.contributors import Substitution, forward, reverse, same_squads
from svbg.remnawave.models import PanelUser
from svbg.subscriptions.tables import EVENT_REF_PREDICATE, subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database

__all__ = [
    "EXPIRE_TOLERANCE",
    "SQUADS_PENDING",
    "Alert",
    "Projection",
    "apply",
    "compute",
    "mark_missing",
    "publish",
    "select_subscriptions",
    "snapshot_values",
]

log = logging.getLogger("svbg.remnawave.projection")

#: The panel keeps milliseconds; anything within a second is "the same date".
EXPIRE_TOLERANCE = timedelta(seconds=1)
#: Internal ``overrides`` marker (keys starting with ``_`` are bookkeeping, never manual edits): the writer
#: could not send a squads change because the owner module was down (fail-closed, 07 §2.4.3).
SQUADS_PENDING = "_squads_pending"

_TXT_REDUCED_TITLE = "Срок оплаченной подписки уменьшен в панели"
_TXT_REDUCED_BODY = (
    "Подписка №{id} ({username}): в панели срок изменён вручную с {old} на {new}. Бот принял новое значение. "
    "Если это ошибка, верните срок в карточке подписки."
)
_TXT_MISSING_TITLE = "Пользователь панели удалён"
_TXT_MISSING_BODY = (
    "Подписка №{id} ({username}): пользователя нет в панели. Бот не пересоздаёт его автоматически: выберите "
    "«Пересоздать» (срок и ссылка сохранятся) или «Закрыть» в карточке подписки."
)
_TXT_TG_TITLE = "В панели сменили Telegram-владельца подписки"
_TXT_TG_BODY = (
    "Подписка №{id} ({username}): в панели указан другой telegramId ({new}), а в боте подписка принадлежит "
    "{owner}. Бот ничего не переносит автоматически — проверьте, была ли это передача подписки."
)


@dataclass(frozen=True, slots=True)
class Alert:
    dedup_key: str
    title: str
    body: str
    fix_action: str | None = None
    severity: str = "warn"


@dataclass(slots=True)
class Projection:
    """What a panel snapshot changes for one subscription."""

    subscription_id: int
    values: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    bus: list[Event] = field(default_factory=list)
    #: Fields the bot must re-send (a module substitution changed while the plan did not): writer's job.
    drift: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.values or self.events)


def select_subscriptions() -> sa.Select[Any]:
    """``subscriptions.*`` + the owner's ``telegram_id`` (as ``owner_telegram_id``)."""
    return sa.select(subscriptions, users.c.telegram_id.label("owner_telegram_id")).select_from(
        subscriptions.outerjoin(users, users.c.id == subscriptions.c.user_id)
    )


def snapshot_values(user: PanelUser) -> dict[str, Any]:
    """``panel_*`` columns from a panel user (squads stored as the panel has them, not reversed)."""
    traffic = user.user_traffic
    values: dict[str, Any] = {
        "panel_status": user.status,
        "panel_expire_at": user.expire_at,
        "panel_traffic_limit": user.traffic_limit_bytes,
        "panel_used_traffic": traffic.used_traffic_bytes,
        "panel_reset_strategy": user.traffic_limit_strategy,
        "panel_device_limit": user.hwid_device_limit,
        "panel_ext_squad": user.external_squad_uuid,
        "panel_tag": user.tag,
        "panel_telegram_id": user.telegram_id,
        "panel_online_at": traffic.online_at,
        "panel_first_connected_at": traffic.first_connected_at,
        "panel_last_traffic_reset_at": user.last_traffic_reset_at,
        "panel_short_uuid": user.short_uuid,
        "panel_username": user.username,
    }
    if user.subscription_url:
        values["subscription_url"] = user.subscription_url
    squads = user.squad_uuids
    if squads:  # empty = "unknown" in some webhook payloads; never overwrite a known list with it
        values["panel_squads"] = squads
    return values


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fmt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M UTC") if value is not None else "—"


# (overrides key, desired column, value from the panel user, compare even when desired is NULL)
_PLAN_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("traffic_bytes", "desired_traffic_bytes", "traffic_limit_bytes", False),
    ("reset_strategy", "desired_reset_strategy", "traffic_limit_strategy", False),
    ("device_limit", "desired_device_limit", "hwid_device_limit", True),
    ("ext_squad", "desired_ext_squad", "external_squad_uuid", True),
    ("tag", "desired_tag", "tag", False),
)


def compute(
    sub: Mapping[str, Any],
    user: PanelUser,
    *,
    twins: Mapping[str, str],
    substitutions: Sequence[Substitution] = (),
    frozen: bool = False,
    pending: bool = False,
    state_ts: datetime | None = None,
    source: str = "sync",
    at: datetime | None = None,
) -> Projection:
    """Pure: what ``user`` (fresh panel state) changes for ``sub`` (a :func:`select_subscriptions` row)."""
    sid = int(sub["id"])
    result = Projection(sid)
    current = at or now()
    ts = state_ts or current
    prev_ts = sub.get("panel_state_ts")
    if prev_ts is not None and ts < prev_ts:
        result.stale = True  # an older picture than the one we already have (out-of-order webhook)
        return result

    values: dict[str, Any] = {}
    for column, value in snapshot_values(user).items():
        if sub.get(column) != value:
            values[column] = value
    if sub.get("link_state") == "panel_missing":
        values["link_state"] = "linked"
        result.events.append(_event(sid, "panel_reappeared", source))
    overrides = dict(sub.get("overrides") or {})
    original_overrides = dict(overrides)

    # --- panel-owned facts that other parts of the bot react to (bus only; notifications are stage 2)
    old_status = sub.get("panel_status")
    if old_status is not None and old_status != user.status:
        result.bus.append(
            Event(
                "subscription.panel_status",
                {"subscription_id": sid, "old": old_status, "new": user.status, "source": source},
            )
        )
    if sub.get("panel_first_connected_at") is None and user.user_traffic.first_connected_at is not None:
        result.bus.append(Event("subscription.first_connected", {"subscription_id": sid, "source": source}))
    old_short = sub.get("panel_short_uuid")
    if old_short is not None and old_short != user.short_uuid:
        result.events.append(_event(sid, "link_changed_in_panel", source))
        result.bus.append(Event("subscription.link_changed", {"subscription_id": sid, "source": source}))
    old_tg = sub.get("panel_telegram_id")
    owner_tg = sub.get("owner_telegram_id")
    if (
        old_tg is not None
        and user.telegram_id != old_tg
        and owner_tg is not None
        and user.telegram_id != owner_tg
    ):
        result.events.append(_event(sid, "telegram_changed_in_panel", source))
        result.alerts.append(
            Alert(
                f"rw:tg_changed:{sid}",
                _TXT_TG_TITLE,
                _TXT_TG_BODY.format(
                    id=sid, username=user.username, new=user.telegram_id or "—", owner=owner_tg
                ),
                fix_screen("subscription", str(sid)),
            )
        )

    if not pending:
        _project_expire(sub, user, values, result, source)
        _project_plan_fields(sub, user, overrides)
        _project_squads(sub, user, overrides, result, twins, substitutions, frozen)
        _project_status(sub, user, overrides, result, source)
    if overrides != original_overrides:
        values["overrides"] = overrides
        added = sorted(
            k for k in overrides if not k.startswith("_") and original_overrides.get(k) != overrides[k]
        )
        if added:
            result.events.append(
                _event(
                    sid,
                    "override",
                    source,
                    details={"fields": added, "values": {k: overrides[k] for k in added}},
                )
            )
    if values:
        values["panel_state_ts"] = ts
    result.values = values
    return result


def _event(
    sid: int,
    kind: str,
    source: str,
    *,
    old: datetime | None = None,
    new: datetime | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    delta = int((new - old).total_seconds()) if old is not None and new is not None else None
    return {
        "subscription_id": sid,
        "kind": kind,
        "source": source,
        "delta_seconds": delta,
        "old_expire": old,
        "new_expire": new,
        "details": dict(details or {}),
    }


def _project_expire(
    sub: Mapping[str, Any], user: PanelUser, values: dict[str, Any], result: Projection, source: str
) -> None:
    panel = user.expire_at
    desired = sub.get("desired_expire_at") or sub.get("paid_until")
    if panel is None or desired is None:
        return
    if abs(panel - desired) <= EXPIRE_TOLERANCE:
        return
    sid = result.subscription_id
    values["desired_expire_at"] = panel
    values["paid_until"] = panel
    if panel > desired:
        result.events.append(_event(sid, "expire_extended_in_panel", source, old=desired, new=panel))
        return
    result.events.append(_event(sid, "expire_reduced_in_panel", source, old=desired, new=panel))
    result.alerts.append(
        Alert(
            f"rw:expire_reduced:{sid}",
            _TXT_REDUCED_TITLE,
            _TXT_REDUCED_BODY.format(id=sid, username=user.username, old=_fmt(desired), new=_fmt(panel)),
            fix_screen("subscription", str(sid)),
        )
    )
    result.bus.append(
        Event(
            "subscription.expire_reduced",
            {"subscription_id": sid, "old": _iso(desired), "new": _iso(panel), "source": source},
        )
    )


def _project_plan_fields(sub: Mapping[str, Any], user: PanelUser, overrides: dict[str, Any]) -> None:
    for key, column, attr, nullable_managed in _PLAN_FIELDS:
        desired = sub.get(column)
        if desired is None and not nullable_managed:
            continue  # the bot does not manage this field for this subscription
        panel = getattr(user, attr)
        if panel == desired:
            overrides.pop(key, None)  # the admin put it back (or our write caught up)
        else:
            overrides[key] = panel


def _project_squads(  # noqa: PLR0917 - internal step of compute()
    sub: Mapping[str, Any],
    user: PanelUser,
    overrides: dict[str, Any],
    result: Projection,
    twins: Mapping[str, str],
    substitutions: Sequence[Substitution],
    frozen: bool,
) -> None:
    panel = user.squad_uuids
    desired = list(sub.get("desired_squads") or [])
    if not panel or not desired:
        return  # empty in the panel = unknown (webhooks) / never sent by us; empty desired = unmanaged
    base = reverse(panel, twins)
    if same_squads(base, desired):
        overrides.pop("squads", None)
        overrides.pop(SQUADS_PENDING, None)  # the plan's squads are in the panel: nothing is waiting
        if not frozen and not same_squads(panel, forward(desired, substitutions)):
            result.drift.append("squads")  # a substitution appeared/disappeared: the writer re-sends squads
        return
    if frozen or overrides.get(SQUADS_PENDING):
        # Our own change held back by a down module (or its state is unknowable while it is down): never a
        # manual edit. Once the module is OK, the writer re-sends the squads.
        if not frozen:
            result.drift.append("squads")
        return
    overrides["squads"] = sorted(set(base))


def _project_status(
    sub: Mapping[str, Any], user: PanelUser, overrides: dict[str, Any], result: Projection, source: str
) -> None:
    if user.status == "DISABLED":
        if sub.get("disabled_reason") is None and sub.get("desired_status") != "disabled":
            if overrides.get("status") != "DISABLED":
                result.bus.append(
                    Event("subscription.disabled_in_panel", {"subscription_id": result.subscription_id})
                )
            overrides["status"] = "DISABLED"
    elif user.status in ("ACTIVE", "LIMITED", "EXPIRED"):
        overrides.pop("status", None)


async def apply(conn: AsyncConnection, sub: Mapping[str, Any], result: Projection) -> bool:
    """Write ``result`` if the row did not change since it was read. Returns whether it was written."""
    if not result.changed:
        return False
    if result.values:
        stmt = (
            sa.update(subscriptions)
            .where(
                subscriptions.c.id == result.subscription_id, subscriptions.c.updated_at == sub["updated_at"]
            )
            .values(**result.values, updated_at=sa.func.now())
            .returning(subscriptions.c.id)
        )
        if (await conn.execute(stmt)).first() is None:
            log.info(
                "subscription %s changed during projection; left for the next pass", result.subscription_id
            )
            return False
    if result.events:
        await conn.execute(sa.insert(subscription_events), result.events)
    return True


async def publish(result: Projection, *, attention: AttentionService | None, bus: EventBus | None) -> None:
    """After commit: owner alerts and bus events. Failures are logged and never propagate."""
    if attention is not None:
        for alert in result.alerts:
            try:
                await attention.raise_item(
                    alert.dedup_key,
                    alert.severity,
                    alert.title,
                    alert.body,
                    fix_action=alert.fix_action,  # type: ignore[arg-type]
                )
            except Exception:
                log.exception("could not raise attention item %s", alert.dedup_key)
    if bus is not None:
        for event in result.bus:
            _emit(bus, event)


def _emit(bus: EventBus, event: Event) -> None:
    try:
        bus.publish_nowait(event)
    except Exception:  # a closed bus during shutdown must not fail the caller
        log.exception("could not publish %s", event.name)


async def mark_missing(
    db: Database,
    sid: int,
    *,
    source: str,
    username: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    attention: AttentionService | None = None,
    bus: EventBus | None = None,
) -> bool:
    """``linked`` → ``panel_missing`` (02 §6.6): ``paid_until`` kept, no auto re-creation, owner alerted."""
    event: dict[str, Any] = {"subscription_id": sid, "kind": "panel_missing", "source": source}
    if ref_id is not None:
        event.update(ref_type=ref_type, ref_id=ref_id)
    async with db.tx() as conn:
        row = (
            await conn.execute(
                sa.update(subscriptions)
                .where(subscriptions.c.id == sid, subscriptions.c.link_state == "linked")
                .values(link_state="panel_missing", updated_at=sa.func.now())
                .returning(subscriptions.c.id)
            )
        ).first()
        if row is None:
            return False
        stmt = pg_insert(subscription_events).values(**event)
        if ref_id is not None:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["subscription_id", "kind", "ref_type", "ref_id"],
                index_where=sa.text(EVENT_REF_PREDICATE),
            )
        await conn.execute(stmt)
    result = Projection(sid)
    result.alerts.append(
        Alert(
            f"rw:panel_missing:{sid}",
            _TXT_MISSING_TITLE,
            _TXT_MISSING_BODY.format(id=sid, username=username or "—"),
            fix_screen("subscription", str(sid)),
        )
    )
    result.bus.append(Event("subscription.panel_missing", {"subscription_id": sid, "source": source}))
    await publish(result, attention=attention, bus=bus)
    return True
