"""Minimal subscription service (stage 1): the business side of the panel primitives.

Every method takes the caller's open transaction (``conn``): the subscription change and the panel operation
(``jobs`` row via :mod:`svbg.remnawave.writer`) commit together or not at all. Purchase flows (stage 2) build
on these primitives; nothing here talks to the panel directly.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from svbg.remnawave.models import FOREVER
from svbg.remnawave.writer import (
    K_DELETE,
    K_DISABLE,
    K_ENABLE,
    enqueue_action,
    enqueue_create,
    enqueue_renew,
    enqueue_update,
)
from svbg.subscriptions.tables import subscription_events, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "DEFAULT_PREFIX",
    "FOREVER",
    "Desired",
    "SubscriptionService",
    "configure_username_prefix",
    "make_username",
]

DEFAULT_PREFIX: Final = "sv_"
_PREFIX_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,12}$")

#: desired column → writer update field.
_FIELD_OF: Final[Mapping[str, str]] = {
    "desired_expire_at": "expire",
    "desired_traffic_bytes": "traffic",
    "desired_reset_strategy": "strategy",
    "desired_device_limit": "device_limit",
    "desired_squads": "squads",
    "desired_ext_squad": "ext_squad",
    "desired_tag": "tag",
}


def make_username(prefix: str, telegram_id: int | None, n: int = 1, *, fallback: str | None = None) -> str:
    """``{prefix}{telegram_id}`` for the first subscription, ``…_{n}`` for the next ones (02 §3.2)."""
    if not _PREFIX_RE.match(prefix):
        raise ValueError("префикс имени: 1–12 символов A-Z a-z 0-9 _ -")
    core = str(telegram_id) if telegram_id is not None else (fallback or "")
    if not core or not re.fullmatch(r"[A-Za-z0-9_-]+", core):
        raise ValueError("нужен telegram_id или безопасный fallback для имени пользователя панели")
    name = f"{prefix}{core}" if n <= 1 else f"{prefix}{core}_{n}"
    if not 3 <= len(name) <= 36:
        raise ValueError("имя пользователя панели должно быть от 3 до 36 символов")
    return name


@dataclass(frozen=True, slots=True)
class Desired:
    """The bot's intent for one subscription (bytes, not GB; ``device_limit`` None = panel fallback)."""

    expire_at: datetime
    squads: Sequence[str]
    traffic_bytes: int = 0
    reset_strategy: str = "NO_RESET"
    device_limit: int | None = None
    ext_squad: str | None = None
    tag: str | None = None

    def __post_init__(self) -> None:
        if self.expire_at.tzinfo is None:
            raise ValueError("expire_at must be timezone-aware")
        if not list(self.squads):
            raise ValueError(
                "нужен хотя бы один сквад: пустой activeInternalSquads снимает пользователя с нод"
            )
        if self.traffic_bytes < 0 or (self.device_limit is not None and self.device_limit < 0):
            raise ValueError("лимиты не могут быть отрицательными")

    def columns(self) -> dict[str, Any]:
        return {
            "desired_expire_at": min(self.expire_at, FOREVER),
            "paid_until": min(self.expire_at, FOREVER),
            "desired_squads": list(dict.fromkeys(self.squads)),
            "desired_traffic_bytes": self.traffic_bytes,
            "desired_reset_strategy": self.reset_strategy,
            "desired_device_limit": self.device_limit,
            "desired_ext_squad": self.ext_squad,
            "desired_tag": self.tag,
        }


#: The app's live ``PANEL_USERNAME_PREFIX`` (set once at start, reset at stop): services built without an
#: explicit prefix follow the setting without a restart (new subscriptions only; nothing is renamed).
_prefix_source: list[Callable[[], object]] = []


def configure_username_prefix(source: Callable[[], object] | None) -> None:
    """Set (or clear with ``None``) where :class:`SubscriptionService` reads the default username prefix."""
    _prefix_source[:] = [] if source is None else [source]


def _configured_prefix() -> str:
    if not _prefix_source:
        return DEFAULT_PREFIX
    try:
        value = _prefix_source[0]()
    except Exception:  # noqa: BLE001 - a broken settings snapshot never blocks a purchase
        return DEFAULT_PREFIX
    return value if isinstance(value, str) and _PREFIX_RE.match(value) else DEFAULT_PREFIX


class SubscriptionService:
    """Stateless; every write happens in the caller's transaction."""

    def __init__(self, *, username_prefix: str | None = None) -> None:
        if username_prefix is not None and not _PREFIX_RE.match(username_prefix):
            raise ValueError("префикс имени: 1–12 символов A-Z a-z 0-9 _ -")
        self._prefix = username_prefix

    @property
    def prefix(self) -> str:
        """The explicit prefix, else the live ``PANEL_USERNAME_PREFIX`` (``sv_`` by default)."""
        return self._prefix if self._prefix is not None else _configured_prefix()

    async def create(
        self,
        conn: AsyncConnection,
        *,
        user_id: int | None,
        telegram_id: int | None,
        desired: Desired,
        plan_id: int | None = None,
        plan_snapshot: Mapping[str, Any] | None = None,
        caused_by: str | None = None,
        lane: str = "interactive",
        is_trial: bool = False,
        extra_devices: int = 0,
    ) -> int:
        """A ``pending`` subscription + ``panel.create``. Username: next free ``{prefix}{tg}[_{n}]``."""
        if extra_devices < 0:
            raise ValueError("extra_devices must be >= 0")
        public_id_fallback = secrets.token_hex(6) if telegram_id is None else None
        n = 1
        base_prefix = self.prefix  # one read: the setting may change meanwhile
        if telegram_id is not None:
            prefix = make_username(base_prefix, telegram_id)
            taken = set(
                (
                    await conn.execute(
                        sa.select(subscriptions.c.panel_username).where(
                            sa.or_(
                                subscriptions.c.panel_username == prefix,
                                subscriptions.c.panel_username.like(prefix.replace("_", r"\_") + r"\_%"),
                            )
                        )
                    )
                ).scalars()
            )
            while make_username(base_prefix, telegram_id, n) in taken:
                n += 1
        username = make_username(base_prefix, telegram_id, n, fallback=public_id_fallback)
        sid = await conn.scalar(
            sa.insert(subscriptions)
            .values(
                user_id=user_id,
                plan_id=plan_id,
                plan_snapshot=dict(plan_snapshot or {}),
                link_state="pending",
                panel_username=username,
                is_trial=is_trial,
                extra_devices=extra_devices,
                **desired.columns(),
            )
            .returning(subscriptions.c.id)
        )
        await enqueue_create(conn, int(sid), lane=lane, caused_by=caused_by)
        return int(sid)

    async def change(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        clear_overrides: Iterable[str] = (),
        caused_by: str | None = None,
        lane: str = "interactive",
        **desired: Any,
    ) -> int | None:
        """Set ``desired_*`` columns (pass them as ``desired_squads=[…]`` etc.) and PATCH those fields."""
        unknown = set(desired) - set(_FIELD_OF)
        if not desired or unknown:
            raise ValueError(f"change() accepts only {sorted(_FIELD_OF)}")
        if "desired_squads" in desired and not list(desired["desired_squads"] or ()):
            raise ValueError("пустой список сквадов не допускается")
        if "desired_expire_at" in desired:
            desired["paid_until"] = desired["desired_expire_at"]
        await conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == subscription_id)
            .values(**desired, updated_at=sa.func.now())
        )
        fields = [_FIELD_OF[k] for k in desired if k in _FIELD_OF]
        return await enqueue_update(
            conn, subscription_id, fields, clear_overrides=clear_overrides, lane=lane, caused_by=caused_by
        )

    async def set_forever(
        self, conn: AsyncConnection, subscription_id: int, *, caused_by: str | None = None
    ) -> None:
        """«Бессрочно» = 2099-12-31T00:00:00Z (the panel shows ``expire=0`` for year 2099)."""
        await self.change(conn, subscription_id, desired_expire_at=FOREVER, caused_by=caused_by)

    async def renew(
        self, conn: AsyncConnection, subscription_id: int, days: int, *, caused_by: str | None = None
    ) -> int | None:
        return await enqueue_renew(conn, subscription_id, days, caused_by=caused_by)

    async def disable(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        reason: str = "admin",
        caused_by: str | None = None,
    ) -> int | None:
        return await enqueue_action(conn, subscription_id, K_DISABLE, {"reason": reason}, caused_by=caused_by)

    async def enable(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        only_reason: str | None = None,
        caused_by: str | None = None,
    ) -> int | None:
        payload = {"only_reason": only_reason} if only_reason else {}
        return await enqueue_action(conn, subscription_id, K_ENABLE, payload, caused_by=caused_by)

    async def close(
        self,
        conn: AsyncConnection,
        subscription_id: int,
        *,
        delete_in_panel: bool = False,
        caused_by: str | None = None,
    ) -> int | None:
        """``delete_mode``: disable (default, the panel user stays for history) or delete (02 §4.10)."""
        if delete_in_panel:
            return await enqueue_action(conn, subscription_id, K_DELETE, caused_by=caused_by)
        await conn.execute(
            sa.update(subscriptions)
            .where(subscriptions.c.id == subscription_id)
            .values(
                link_state=sa.case(
                    (subscriptions.c.link_state == "pending", "closed"), else_=subscriptions.c.link_state
                )
            )
        )
        await conn.execute(
            sa.insert(subscription_events).values(
                subscription_id=subscription_id, kind="close_requested", source="bot"
            )
        )
        return await enqueue_action(
            conn, subscription_id, K_DISABLE, {"reason": "closed"}, caused_by=caused_by
        )

    async def get(self, conn: AsyncConnection, subscription_id: int) -> Mapping[str, Any] | None:
        return (
            (await conn.execute(sa.select(subscriptions).where(subscriptions.c.id == subscription_id)))
            .mappings()
            .first()
        )
