"""Typed methods of the panel API used by the bot (02 §2.2). All paths are under ``/api``.

Conventions:

* user ids are guarded before the network: a non-positive id or a non-int (a 2.x UUID) raises
  ``validation`` instead of letting the panel answer 400 and the caller take the "create" branch;
* mutating calls first make sure the panel version allows writes (:meth:`RemnawaveApi.ensure_gate`);
* idempotent calls (GET, absolute PATCH, enable/disable, DELETE, hwid delete) may be retried by the
  transport; ``create_user``, ``revoke``, ``reset_traffic``, ``bulk_extend`` are never retried blindly —
  the writer repeats them through its outbox after checking whether they were applied (02 §4);
* ``enable``/``disable`` return ``None`` when the panel says ``A029``/``A030`` (already in that state);
  ``delete_user`` returns ``False`` when the user is already gone (404 after a retry is success);
* ``activeInternalSquads=[]`` is never sent (it removes the user from every node).

Only ``svbg.remnawave.writer`` may call mutating methods (AST rule owned by the writer's tests).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Final, TypeVar
from urllib.parse import quote

import msgspec
from msgspec import UNSET, UnsetType

from svbg.remnawave.capabilities import VersionGate, gate_version
from svbg.remnawave.errors import ErrorKind, RemnawaveError, WriteBlockedError
from svbg.remnawave.models import (
    AccessibleNodes,
    ConnectionKeys,
    ExternalSquad,
    HwidDevices,
    InternalSquad,
    Metadata,
    Node,
    PanelUser,
    RequestHistory,
    ResolvedUser,
    SubpageConfig,
    SubpagePageConfig,
    SubscriptionSettings,
    SystemConfig,
    SystemStats,
    UserMetadata,
    UsersPage,
    _ExternalSquads,
    _InternalSquads,
)
from svbg.remnawave.transport import Lane, RawResponse, Transport

__all__ = ["MUTATING_METHODS", "NodeUsage", "RemnawaveApi", "iso_utc"]

T = TypeVar("T")

USERNAME_RE: Final = re.compile(r"^[A-Za-z0-9_-]{3,36}$")
TAG_RE: Final = re.compile(r"^[A-Z0-9_]{1,16}$")
BULK_MAX: Final = 500
STREAM_MAX: Final = 1000

#: Methods that change panel state — callable only from the writer.
MUTATING_METHODS: Final = frozenset(
    {
        "create_user",
        "update_user",
        "enable",
        "disable",
        "reset_traffic",
        "revoke",
        "delete_user",
        "bulk_update_squads",
        "bulk_extend",
        "delete_device",
        "delete_all_devices",
        "drop_connections",
        "patch_subscription_settings",
        "put_meta",
    }
)


class _Envelope[R](msgspec.Struct):
    response: R


_decoders: dict[Any, msgspec.json.Decoder[Any]] = {}


def _decoder(tp: Any) -> msgspec.json.Decoder[Any]:
    dec = _decoders.get(tp)
    if dec is None:
        dec = _decoders[tp] = msgspec.json.Decoder(_Envelope[tp])  # type: ignore[valid-type]
    return dec


def iso_utc(value: datetime) -> str:
    """ISO-8601 in UTC with milliseconds and ``Z`` (what the panel itself emits)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _bad(message: str, *, method: str | None = None, path: str | None = None) -> RemnawaveError:
    return RemnawaveError(
        ErrorKind.VALIDATION,
        None,
        "CLIENT_GUARD",
        message,
        "Бот отказался отправлять запрос с неверными данными. Это ошибка в данных бота — передайте "
        "технические детали разработчику.",
        method=method,
        path=path,
    )


def _user_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _bad(f"неверный id пользователя панели: {type(value).__name__}")
    return value


def _path_part(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "/" in value or value in (".", ".."):
        raise _bad(f"неверный {what}")
    return quote(value, safe="")


def _squads(value: Sequence[str]) -> list[str]:
    items = [str(s) for s in value]
    if not items:
        raise _bad("пустой список activeInternalSquads снял бы пользователя со всех нод — не отправляем")
    return items


class RemnawaveApi:
    """Thin typed client over one :class:`Transport`. Cheap; recreated on every hot swap."""

    def __init__(
        self,
        transport: Transport,
        *,
        gate: VersionGate | None = None,
        confirmed_major: int | None = None,
    ) -> None:
        self.transport = transport
        self.gate: VersionGate | None = gate
        self.confirmed_major = confirmed_major
        self._gate_lock = asyncio.Lock()

    # ------------------------------------------------------------------------------------- plumbing

    async def _call(
        self,
        method: str,
        path: str,
        tp: Any,
        *,
        body: Any = None,
        params: Mapping[str, str] | None = None,
        idempotent: bool,
        user_scoped: bool = False,
        scope: str,
        lane: Lane = Lane.INTERACTIVE,
        budget: float | None = None,
        write: bool = False,
    ) -> Any:
        if write:
            await self.ensure_gate(lane=lane)
            if self.gate is not None and not self.gate.writes_allowed:
                raise WriteBlockedError(self.gate.version, method=method, path=path)
        raw = await self.transport.request(
            method,
            path,
            json_body=msgspec.json.encode(body) if body is not None else None,
            params=params,
            idempotent=idempotent,
            user_scoped=user_scoped,
            scope=scope,
            lane=lane,
            budget=budget,
        )
        if tp is None:
            return raw
        return self._decode(raw, tp, method, path)

    @staticmethod
    def _decode(raw: RawResponse, tp: Any, method: str, path: str) -> Any:
        if not raw.body:
            raise RemnawaveError(
                ErrorKind.SERVER,
                raw.status,
                "EMPTY_RESPONSE",
                "панель вернула пустой ответ",
                method=method,
                path=path,
            )
        try:
            return _decoder(tp).decode(raw.body).response
        except msgspec.ValidationError as exc:
            raise RemnawaveError(
                ErrorKind.SERVER,
                raw.status,
                "BAD_RESPONSE",
                f"ответ панели не по контракту: {exc}",
                "Ответ панели не совпал с ожидаемым форматом. Проверьте версию панели на экране «Состояние» "
                "и обновите бота.",
                method=method,
                path=path,
            ) from None
        except msgspec.DecodeError:
            raise RemnawaveError(
                ErrorKind.SERVER,
                raw.status,
                "BAD_RESPONSE",
                "ответ не похож на API панели (не JSON)",
                "Адрес отвечает не как API Remnawave: возможно, это страница входа Cloudflare/Caddy или "
                "другой сайт. Проверьте REMNAWAVE_URL и заголовки доступа.",
                method=method,
                path=path,
            ) from None

    async def ensure_gate(self, *, lane: Lane = Lane.INTERACTIVE) -> VersionGate:
        """Detect the panel version once (single flight) so writes respect the version gate (02 §1.1).

        A 403 on metadata (no ``system:metadata`` scope) yields an ``unknown`` gate that allows writes;
        transport failures propagate (the writer retries later).
        """
        if self.gate is not None:
            return self.gate
        async with self._gate_lock:
            if self.gate is not None:
                return self.gate
            try:
                meta = await self.metadata(lane=lane)
            except RemnawaveError as err:
                if err.kind is not ErrorKind.FORBIDDEN_SCOPE:
                    raise
                self.gate = gate_version(None, confirmed_major=self.confirmed_major)
            else:
                self.gate = gate_version(meta.version, confirmed_major=self.confirmed_major)
            return self.gate

    # --------------------------------------------------------------------------------------- system

    async def metadata(self, *, lane: Lane = Lane.INTERACTIVE, budget: float | None = None) -> Metadata:
        return await self._call(
            "GET",
            "/system/metadata",
            Metadata,
            idempotent=True,
            scope="system:metadata",
            lane=lane,
            budget=budget,
        )

    async def configuration(self, *, lane: Lane = Lane.INTERACTIVE) -> SystemConfig:
        return await self._call(
            "GET",
            "/system/configuration",
            SystemConfig,
            idempotent=True,
            scope="system:configuration",
            lane=lane,
        )

    async def stats(self, *, lane: Lane = Lane.INTERACTIVE) -> SystemStats:
        return await self._call(
            "GET", "/system/stats", SystemStats, idempotent=True, scope="system:stats", lane=lane
        )

    # ---------------------------------------------------------------------------------------- users

    async def create_user(
        self,
        *,
        username: str,
        expire_at: datetime,
        telegram_id: int | UnsetType | None = UNSET,
        email: str | UnsetType | None = UNSET,
        description: str | UnsetType = UNSET,
        tag: str | UnsetType | None = UNSET,
        traffic_limit_bytes: int | UnsetType = UNSET,
        traffic_limit_strategy: str | UnsetType = UNSET,
        active_internal_squads: Sequence[str] | UnsetType = UNSET,
        external_squad_uuid: str | UnsetType | None = UNSET,
        hwid_device_limit: int | UnsetType = UNSET,
        short_uuid: str | UnsetType = UNSET,
        created_at: datetime | UnsetType = UNSET,
        lane: Lane = Lane.INTERACTIVE,
        budget: float | None = None,
    ) -> PanelUser:
        """``POST /users`` → 201. Never retried here. ``hwidDeviceLimit=None`` cannot be sent on create
        (omit it to inherit the panel's fallback)."""
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise _bad("username: 3..36 символов, только A-Z a-z 0-9 _ -")
        if isinstance(tag, str) and not TAG_RE.fullmatch(tag):
            raise _bad("tag: до 16 символов A-Z 0-9 _")
        if hwid_device_limit is None:  # type: ignore[comparison-overlap]
            raise _bad("hwidDeviceLimit=null нельзя передать при создании — не указывайте поле")
        if isinstance(hwid_device_limit, int) and hwid_device_limit < 0:
            raise _bad("hwidDeviceLimit должен быть ≥ 0")
        if isinstance(traffic_limit_bytes, int) and traffic_limit_bytes < 0:
            raise _bad("trafficLimitBytes должен быть ≥ 0")
        body: dict[str, Any] = {"username": username, "expireAt": iso_utc(expire_at)}
        _put(body, "telegramId", telegram_id)
        _put(body, "email", email)
        _put(body, "description", description)
        _put(body, "tag", tag)
        _put(body, "trafficLimitBytes", traffic_limit_bytes)
        _put(body, "trafficLimitStrategy", traffic_limit_strategy)
        if not isinstance(active_internal_squads, UnsetType):
            body["activeInternalSquads"] = _squads(active_internal_squads)
        _put(body, "externalSquadUuid", external_squad_uuid)
        _put(body, "hwidDeviceLimit", hwid_device_limit)
        _put(body, "shortUuid", short_uuid)
        if not isinstance(created_at, UnsetType):
            body["createdAt"] = iso_utc(created_at)
        return await self._call(
            "POST",
            "/users",
            PanelUser,
            body=body,
            idempotent=False,
            user_scoped=True,
            scope="users:create",
            lane=lane,
            budget=budget,
            write=True,
        )

    async def update_user(
        self,
        id: int,
        *,
        expire_at: datetime | UnsetType = UNSET,
        traffic_limit_bytes: int | UnsetType = UNSET,
        traffic_limit_strategy: str | UnsetType = UNSET,
        active_internal_squads: Sequence[str] | UnsetType = UNSET,
        external_squad_uuid: str | UnsetType | None = UNSET,
        hwid_device_limit: int | UnsetType | None = UNSET,
        tag: str | UnsetType | None = UNSET,
        description: str | UnsetType | None = UNSET,
        telegram_id: int | UnsetType | None = UNSET,
        email: str | UnsetType | None = UNSET,
        lane: Lane = Lane.INTERACTIVE,
        budget: float | None = None,
    ) -> PanelUser:
        """``PATCH /users {id, …}`` with **absolute** values (idempotent). ``status`` is never sent:
        enable/disable go through actions (02 §3.2)."""
        uid = _user_id(id)
        if isinstance(tag, str) and not TAG_RE.fullmatch(tag):
            raise _bad("tag: до 16 символов A-Z 0-9 _")
        if isinstance(hwid_device_limit, int) and hwid_device_limit < 0:
            raise _bad("hwidDeviceLimit должен быть ≥ 0")
        body: dict[str, Any] = {"id": uid}
        if not isinstance(expire_at, UnsetType):
            body["expireAt"] = iso_utc(expire_at)
        _put(body, "trafficLimitBytes", traffic_limit_bytes)
        _put(body, "trafficLimitStrategy", traffic_limit_strategy)
        if not isinstance(active_internal_squads, UnsetType):
            body["activeInternalSquads"] = _squads(active_internal_squads)
        _put(body, "externalSquadUuid", external_squad_uuid)
        _put(body, "hwidDeviceLimit", hwid_device_limit)
        _put(body, "tag", tag)
        _put(body, "description", description)
        _put(body, "telegramId", telegram_id)
        _put(body, "email", email)
        if len(body) == 1:
            raise _bad("PATCH без изменяемых полей")
        return await self._call(
            "PATCH",
            "/users",
            PanelUser,
            body=body,
            idempotent=True,
            user_scoped=True,
            scope="users:update",
            lane=lane,
            budget=budget,
            write=True,
        )

    async def get_user(
        self, id: int, *, lane: Lane = Lane.INTERACTIVE, budget: float | None = None
    ) -> PanelUser:
        uid = _user_id(id)
        return await self._call(
            "GET",
            f"/users/{uid}",
            PanelUser,
            idempotent=True,
            user_scoped=True,
            scope="users:by-id",
            lane=lane,
            budget=budget,
        )

    async def get_by_short_uuid(self, short_uuid: str, *, lane: Lane = Lane.INTERACTIVE) -> PanelUser:
        part = _path_part(short_uuid, "shortUuid")
        return await self._call(
            "GET",
            f"/users/by-short-uuid/{part}",
            PanelUser,
            idempotent=True,
            user_scoped=True,
            scope="users:by-short-uuid",
            lane=lane,
        )

    async def get_by_username(self, username: str, *, lane: Lane = Lane.INTERACTIVE) -> PanelUser:
        part = _path_part(username, "username")
        return await self._call(
            "GET",
            f"/users/by-username/{part}",
            PanelUser,
            idempotent=True,
            user_scoped=True,
            scope="users:by-username",
            lane=lane,
        )

    async def resolve(
        self,
        *,
        id: int | None = None,
        short_uuid: str | None = None,
        username: str | None = None,
        lane: Lane = Lane.INTERACTIVE,
    ) -> ResolvedUser:
        """``POST /users/resolve`` with exactly one key (a read: safe to retry)."""
        given = [v for v in (id, short_uuid, username) if v is not None]
        if len(given) != 1:
            raise _bad("resolve: нужен ровно один из id, shortUuid, username")
        body: dict[str, Any]
        if id is not None:
            body = {"id": _user_id(id)}
        elif short_uuid is not None:
            body = {"shortUuid": short_uuid}
        else:
            body = {"username": username}
        return await self._call(
            "POST",
            "/users/resolve",
            ResolvedUser,
            body=body,
            idempotent=True,
            user_scoped=True,
            scope="users:resolve",
            lane=lane,
        )

    async def stream(
        self,
        cursor: str | int | None = None,
        size: int = 500,
        *,
        status: str | None = None,
        telegram_id: int | None = None,
        tag: str | None = None,
        email: str | None = None,
        traffic_limit_strategy: str | None = None,
        external_squad_uuid: str | None = None,
        lane: Lane = Lane.BACKGROUND,
    ) -> UsersPage:
        """One keyset page of ``GET /users/stream`` (``id ASC``). Pass ``page.cursor`` to continue."""
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= STREAM_MAX:
            raise _bad(f"size: 1..{STREAM_MAX}")
        params: dict[str, str] = {"size": str(size)}
        if cursor is not None and cursor != "":
            params["cursor"] = str(cursor)
        for key, value in (
            ("status", status),
            ("telegramId", telegram_id),
            ("tag", tag),
            ("email", email),
            ("trafficLimitStrategy", traffic_limit_strategy),
            ("externalSquadUuid", external_squad_uuid),
        ):
            if value is not None:
                params[key] = str(value)
        return await self._call(
            "GET", "/users/stream", UsersPage, params=params, idempotent=True, scope="users:stream", lane=lane
        )

    async def iter_users(
        self, size: int = 500, *, lane: Lane = Lane.BACKGROUND, **filters: Any
    ) -> AsyncIterator[UsersPage]:
        """Every page of ``users/stream`` with bounded memory (one page at a time)."""
        cursor: str | None = None
        while True:
            page = await self.stream(cursor, size, lane=lane, **filters)
            yield page
            nxt = page.cursor
            if not page.has_more or nxt is None or nxt == cursor:
                return
            cursor = nxt

    async def enable(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> PanelUser | None:
        """``actions/enable``; ``None`` when already enabled (A030)."""
        return await self._action(id, "enable", "users:enable", lane)

    async def disable(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> PanelUser | None:
        """``actions/disable``; ``None`` when already disabled (A029)."""
        return await self._action(id, "disable", "users:disable", lane)

    async def _action(self, id: int, action: str, scope: str, lane: Lane) -> PanelUser | None:
        uid = _user_id(id)
        try:
            return await self._call(
                "POST",
                f"/users/{uid}/actions/{action}",
                PanelUser,
                idempotent=True,
                user_scoped=True,
                scope=scope,
                lane=lane,
                write=True,
            )
        except RemnawaveError as err:
            if err.kind is ErrorKind.ALREADY:
                return None
            raise

    async def reset_traffic(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> PanelUser:
        """``actions/reset-traffic`` — not idempotent for the writer (check ``lastTrafficResetAt`` first)."""
        uid = _user_id(id)
        return await self._call(
            "POST",
            f"/users/{uid}/actions/reset-traffic",
            PanelUser,
            idempotent=False,
            user_scoped=True,
            scope="users:reset-traffic",
            lane=lane,
            write=True,
        )

    async def revoke(
        self,
        id: int,
        only_passwords: bool = False,
        *,
        short_uuid: str | None = None,
        lane: Lane = Lane.INTERACTIVE,
    ) -> PanelUser:
        """``actions/revoke``: new shortUuid/URL (or only passwords). Not retried: check ``subRevokedAt``."""
        uid = _user_id(id)
        body: dict[str, Any] = {}
        if only_passwords:
            body["revokeOnlyPasswords"] = True
        if short_uuid is not None:
            if not 16 <= len(short_uuid) <= 64:
                raise _bad("shortUuid при перевыпуске: 16..64 символа")
            body["shortUuid"] = short_uuid
        return await self._call(
            "POST",
            f"/users/{uid}/actions/revoke",
            PanelUser,
            body=body,
            idempotent=False,
            user_scoped=True,
            scope="users:revoke-subscription",
            lane=lane,
            write=True,
        )

    async def delete_user(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> bool:
        """``DELETE /users/{id}`` → 204. ``False`` when the user was already gone (that is success too)."""
        uid = _user_id(id)
        try:
            await self._call(
                "DELETE",
                f"/users/{uid}",
                None,
                idempotent=True,
                user_scoped=True,
                scope="users:delete",
                lane=lane,
                write=True,
            )
        except RemnawaveError as err:
            if err.kind is ErrorKind.NOT_FOUND:
                return False
            raise
        return True

    async def accessible_nodes(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> AccessibleNodes:
        uid = _user_id(id)
        return await self._call(
            "GET",
            f"/users/{uid}/accessible-nodes",
            AccessibleNodes,
            idempotent=True,
            user_scoped=True,
            scope="users:accessible-nodes",
            lane=lane,
        )

    async def request_history(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> RequestHistory:
        uid = _user_id(id)
        return await self._call(
            "GET",
            f"/users/{uid}/subscription-request-history",
            RequestHistory,
            idempotent=True,
            user_scoped=True,
            scope="users:subscription-request-history",
            lane=lane,
        )

    async def bulk_update_squads(
        self, ids: Sequence[int], squads: Sequence[str], *, lane: Lane = Lane.BACKGROUND
    ) -> None:
        """``POST /users/bulk/update-squads`` → 204, no webhooks. ≤500 ids, absolute (idempotent)."""
        user_ids = self._bulk_ids(ids)
        body = {"userIds": user_ids, "activeInternalSquads": _squads(squads)}
        await self._call(
            "POST",
            "/users/bulk/update-squads",
            None,
            body=body,
            idempotent=True,
            scope="users:bulk-update-squads",
            lane=lane,
            write=True,
        )

    async def bulk_extend(self, ids: Sequence[int], days: int, *, lane: Lane = Lane.BACKGROUND) -> None:
        """``POST /users/bulk/extend-expiration-date`` → 204 — adds to ``expire_at``, never retried."""
        user_ids = self._bulk_ids(ids)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 9999:
            raise _bad("days: 1..9999")
        await self._call(
            "POST",
            "/users/bulk/extend-expiration-date",
            None,
            body={"userIds": user_ids, "extendDays": days},
            idempotent=False,
            scope="users:bulk-extend-expiration-date",
            lane=lane,
            write=True,
        )

    @staticmethod
    def _bulk_ids(ids: Sequence[int]) -> list[int]:
        user_ids = [_user_id(i) for i in ids]
        if not 1 <= len(user_ids) <= BULK_MAX:
            raise _bad(f"bulk: от 1 до {BULK_MAX} id")
        return user_ids

    # ----------------------------------------------------------------------------------------- hwid

    async def devices(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> HwidDevices:
        uid = _user_id(id)
        return await self._call(
            "GET",
            f"/hwid/devices/{uid}",
            HwidDevices,
            idempotent=True,
            scope="hwid-user-devices:list-by-user",
            lane=lane,
        )

    async def delete_device(self, id: int, hwid: str, *, lane: Lane = Lane.INTERACTIVE) -> HwidDevices:
        """Remove one device; the answer lists the **remaining** devices (check ``not result.has(hwid)``)."""
        uid = _user_id(id)
        if not isinstance(hwid, str) or not hwid:
            raise _bad("пустой hwid")
        return await self._call(
            "POST",
            "/hwid/devices/delete",
            HwidDevices,
            body={"userId": uid, "hwid": hwid},
            idempotent=True,
            scope="hwid-user-devices:delete",
            lane=lane,
            write=True,
        )

    async def delete_all_devices(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> HwidDevices:
        """Remove every device (no webhooks); success is ``result.total == 0``."""
        uid = _user_id(id)
        return await self._call(
            "POST",
            "/hwid/devices/delete-all",
            HwidDevices,
            body={"userId": uid},
            idempotent=True,
            scope="hwid-user-devices:delete-all",
            lane=lane,
            write=True,
        )

    async def drop_connections(self, ids: Sequence[int], *, lane: Lane = Lane.INTERACTIVE) -> None:
        """``POST /connections/drop`` (202) for the given users on all nodes."""
        user_ids = [_user_id(i) for i in ids]
        if not user_ids:
            raise _bad("drop_connections: пустой список")
        body = {"dropBy": {"by": "userIds", "userIds": user_ids}, "targetNodes": {"target": "allNodes"}}
        await self._call(
            "POST",
            "/connections/drop",
            None,
            body=body,
            idempotent=True,
            scope="connections:drop",
            lane=lane,
            write=True,
        )

    # --------------------------------------------------------------------------------- squads, nodes

    async def internal_squads(self, *, lane: Lane = Lane.INTERACTIVE) -> list[InternalSquad]:
        res: _InternalSquads = await self._call(
            "GET",
            "/internal-squads",
            _InternalSquads,
            idempotent=True,
            scope="internal-squads:list",
            lane=lane,
        )
        return res.internal_squads

    async def external_squads(self, *, lane: Lane = Lane.INTERACTIVE) -> list[ExternalSquad]:
        res: _ExternalSquads = await self._call(
            "GET",
            "/external-squads",
            _ExternalSquads,
            idempotent=True,
            scope="external-squads:list",
            lane=lane,
        )
        return res.external_squads

    async def nodes(self, *, lane: Lane = Lane.INTERACTIVE) -> list[Node]:
        return await self._call("GET", "/nodes", list[Node], idempotent=True, scope="nodes:list", lane=lane)

    # ------------------------------------------------------------------------- subscription settings

    async def subscription_settings(self, *, lane: Lane = Lane.INTERACTIVE) -> SubscriptionSettings:
        return await self._call(
            "GET",
            "/subscription-settings",
            SubscriptionSettings,
            idempotent=True,
            scope="subscription-settings:get",
            lane=lane,
        )

    async def patch_subscription_settings(
        self, uuid: str, *, lane: Lane = Lane.INTERACTIVE, **fields: Any
    ) -> SubscriptionSettings:
        """``PATCH /subscription-settings`` with camelCase ``fields`` (absolute values)."""
        if not uuid:
            raise _bad("uuid настроек подписки пуст")
        if not fields:
            raise _bad("PATCH без изменяемых полей")
        return await self._call(
            "PATCH",
            "/subscription-settings",
            SubscriptionSettings,
            body={"uuid": uuid, **fields},
            idempotent=True,
            scope="subscription-settings:update",
            lane=lane,
            write=True,
        )

    async def connection_keys(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> ConnectionKeys:
        uid = _user_id(id)
        return await self._call(
            "GET",
            f"/subscriptions/connection-keys/{uid}",
            ConnectionKeys,
            idempotent=True,
            user_scoped=True,
            scope="subscriptions:connection-keys",
            lane=lane,
        )

    async def subpage_config(
        self, short_uuid: str, headers: Mapping[str, str] | None = None, *, lane: Lane = Lane.INTERACTIVE
    ) -> SubpageConfig:
        """``GET /subscriptions/subpage-config/{shortUuid}`` — a GET **with a body** ``{requestHeaders}``."""
        part = _path_part(short_uuid, "shortUuid")
        return await self._call(
            "GET",
            f"/subscriptions/subpage-config/{part}",
            SubpageConfig,
            body={"requestHeaders": dict(headers or {})},
            idempotent=True,
            scope="subscriptions:subpage-config",
            lane=lane,
        )

    async def subpage_page_config(self, uuid: str, *, lane: Lane = Lane.INTERACTIVE) -> SubpagePageConfig:
        part = _path_part(uuid, "uuid")
        return await self._call(
            "GET",
            f"/subscription-page-configs/{part}",
            SubpagePageConfig,
            idempotent=True,
            scope="subscription-page-configs:get",
            lane=lane,
        )

    # ------------------------------------------------------------------------------------- metadata

    async def get_meta(self, id: int, *, lane: Lane = Lane.INTERACTIVE) -> dict[str, Any]:
        uid = _user_id(id)
        res: UserMetadata = await self._call(
            "GET",
            f"/metadata/user/{uid}",
            UserMetadata,
            idempotent=True,
            user_scoped=True,
            scope="metadata:get-user",
            lane=lane,
        )
        return dict(res.metadata)

    async def put_meta(
        self, id: int, metadata: Mapping[str, Any], *, lane: Lane = Lane.INTERACTIVE
    ) -> dict[str, Any]:
        """``PUT /metadata/user/{id}`` **replaces** the whole object — do read-merge-write of your key."""
        uid = _user_id(id)
        res: UserMetadata = await self._call(
            "PUT",
            f"/metadata/user/{uid}",
            UserMetadata,
            body={"metadata": dict(metadata)},
            idempotent=True,
            user_scoped=True,
            scope="metadata:upsert-user",
            lane=lane,
            write=True,
        )
        return dict(res.metadata)

    # ------------------------------------------------------------------------------ bandwidth stats

    async def node_usage(
        self, usage_date: date, node_uuids: Sequence[str], *, lane: Lane = Lane.BACKGROUND
    ) -> NodeUsage:
        """``POST /bandwidth-stats/nodes/usage?start=D&end=D&minTotalBytes=0`` ``{nodesUuids}`` (a read).

        One UTC date per call (a range would glue the cumulative sums). ``payload`` is the raw ``response``
        (``{nodes: [{uuid, users: [{id, totalBytes}]}]}``), ``date`` the panel clock (``Date`` header).
        """
        if not isinstance(usage_date, date) or isinstance(usage_date, datetime):
            raise _bad("usage_date: ожидается дата")
        nodes = [str(u) for u in node_uuids]
        for node in nodes:
            _path_part(node, "uuid ноды")
        if not 1 <= len(nodes) <= BULK_MAX:
            raise _bad(f"nodes/usage: от 1 до {BULK_MAX} нод")
        day = usage_date.isoformat()
        raw: RawResponse = await self._call(
            "POST",
            "/bandwidth-stats/nodes/usage",
            None,
            body={"nodesUuids": nodes},
            params={"start": day, "end": day, "minTotalBytes": "0"},
            idempotent=True,
            scope="bandwidth-stats:node-usage",
            lane=lane,
        )
        try:
            data = msgspec.json.decode(raw.body) if raw.body else None
        except msgspec.DecodeError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict | list):
            raise RemnawaveError(
                ErrorKind.SERVER,
                raw.status,
                "BAD_RESPONSE",
                "ответ nodes/usage не по контракту",
                method="POST",
                path="/bandwidth-stats/nodes/usage",
            )
        return NodeUsage(payload=data["response"], date=raw.date)


class NodeUsage(msgspec.Struct, frozen=True):
    """Answer of :meth:`RemnawaveApi.node_usage`: the raw ``response`` and the panel's ``Date``."""

    payload: Any
    date: datetime | None


def _put(body: dict[str, Any], key: str, value: object) -> None:
    if not isinstance(value, UnsetType):
        body[key] = value
