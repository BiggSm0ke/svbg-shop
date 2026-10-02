"""Fake Remnawave 3.4.x panel (aiohttp) for tests — built from our own client contract, not from the panel.

Realistic where the bot depends on it (02 §2, §5):

* ``{"response": …}`` envelope; ``201`` on create, ``204`` on delete / bulk, ``202`` on ``connections/drop``;
* numeric user ``id``, ``shortUuid``, ``subscriptionUrl``; responses carry the secret fields
  (``trojanPassword``, ``ssPassword``, ``vlessUuid``) exactly like the panel, so tests can prove the client
  drops them;
* errors: ``A063`` (get-by-*), ``A025`` (actions/update/delete), ``A019``/``A020`` conflicts,
  ``A029``/``A030``
  already, ``A018`` (unknown internal squad, 500), ``A182`` (unknown external squad, 404), ``A204`` (device),
  zod-like 400 ``{message, statusCode, errors[]}`` (incl. «Expiration date cannot be in the past»), 401 for a
  bad token, 403 ``E000 Forbidden`` for a missing scope (scope rules of ``scopes.guard.ts``: ``*``,
  ``res:*``, ``res:read|write``, ``res:slug``);
* ``users/stream`` keyset pagination by ``id`` with a **string** ``nextCursor``;
* production mode: an ``http`` request without ``X-Forwarded-For`` + ``X-Forwarded-Proto: https`` gets its
  socket closed without any HTTP response (ProxyCheck);
* fault injection (latency, 5xx, 429 with ``Retry-After``, disconnect) and a request log;
* a webhook sender that signs like the panel (HMAC-SHA256 hex of the raw body).

Usage::

    async with FakeRemnawave() as panel:
        token = panel.add_token()
        cfg = TransportConfig(base_url=panel.url, token=token)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import itertools
import json
import re
import secrets
import time
import uuid as uuidlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aiohttp import ClientSession, ClientTimeout, web

FaultKind = Literal["latency", "disconnect", "429", "500", "502", "503", "504"]
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_TAG_RE = re.compile(r"^[A-Z0-9_]+$")
_STATUSES = ("ACTIVE", "DISABLED", "LIMITED", "EXPIRED")
_STRATEGIES = ("NO_RESET", "DAY", "WEEK", "MONTH", "MONTH_ROLLING")
_SHORT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def make_jwt(payload: Mapping[str, Any]) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(dict(payload)).encode())
    return f"{header}.{body}.{b64(secrets.token_bytes(32))}"


def webhook_body(payload: Mapping[str, Any]) -> bytes:
    """Bytes like ``JSON.stringify`` (compact, UTF-8, no ASCII escaping)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def webhook_signature(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any
    raw: bytes
    ts: float = field(default_factory=time.monotonic)


@dataclass
class Fault:
    kind: FaultKind
    path: str | None = None
    method: str | None = None
    times: int | None = 1
    delay: float = 0.0
    retry_after: str | None = None

    def matches(self, method: str, path: str) -> bool:
        if self.times is not None and self.times <= 0:
            return False
        if self.method is not None and self.method.upper() != method:
            return False
        return self.path is None or path.startswith(self.path)


@dataclass
class _Token:
    scopes: tuple[str, ...]
    uuid: str


class _Reply(Exception):
    def __init__(self, response: web.StreamResponse) -> None:
        super().__init__("reply")
        self.response = response


class FakeRemnawave:
    """In-process fake panel. All state is public for direct seeding and assertions."""

    def __init__(
        self,
        *,
        version: str = "3.4.4",
        production: bool = False,
        webhook_enabled: bool = True,
        hwid_enabled: bool = False,
        sub_domain: str = "https://sub.example.com",
    ) -> None:
        self.version = version
        self.production = production
        self.sub_domain = sub_domain
        self.users: dict[int, dict[str, Any]] = {}
        self.internal_squads: dict[str, dict[str, Any]] = {}
        self.external_squads: dict[str, dict[str, Any]] = {}
        self.nodes: list[dict[str, Any]] = []
        self.devices: dict[int, list[dict[str, Any]]] = {}
        self.user_metadata: dict[int, dict[str, Any]] = {}
        self.page_configs: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, _Token] = {}
        self.requests: list[RecordedRequest] = []
        self.faults: list[Fault] = []
        self.dropped: list[dict[str, Any]] = []
        self.configuration: dict[str, Any] = {
            "notifications": {
                "webhook": webhook_enabled,
                "bandwidthUsage": None,
                "notConnectedAfter": None,
                "expirationNotifications": None,
            },
            "service": {
                "cleanUsageHistory": False,
                "disableUserUsageRecords": False,
                "disableSrhRecords": False,
                "exportToRedisStream": False,
            },
            "misc": {
                "shortUuidLength": 16,
                "subPublicDomain": sub_domain.split("://", 1)[-1],
                "userUsageIgnoreBelowBytes": 0,
            },
        }
        self.subscription_settings: dict[str, Any] = {
            "uuid": str(uuidlib.uuid4()),
            "serveJsonAtBaseSubscription": False,
            "isShowCustomRemarks": True,
            "customRemarks": {},
            "customResponseHeaders": None,
            "randomizeHosts": False,
            "responseRules": None,
            "hwidSettings": {"enabled": hwid_enabled, "fallbackDeviceLimit": 3, "maxDevicesAnnounce": None},
            "createdAt": iso(datetime.now(UTC)),
            "updatedAt": iso(datetime.now(UTC)),
        }
        self._ids = itertools.count(1)
        self._runner: web.AppRunner | None = None
        self._port = 0
        self.connections = 0
        # LTE (ext/lte): cumulative totalBytes per (node uuid, UTC date "YYYY-MM-DD") → {user id: bytes},
        # and every add-many-users call as (squad uuid, [user ids]).
        self.node_usage: dict[tuple[str, str], dict[int, int]] = {}
        self.squad_resends: list[tuple[str, list[int]]] = []

    # ------------------------------------------------------------------------------------- lifecycle

    @property
    def url(self) -> str:
        if not self._port:
            raise RuntimeError("FakeRemnawave is not started")
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        app = web.Application(middlewares=[self._middleware], client_max_size=4 * 1024 * 1024)
        self._routes(app)
        self._runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server  # aiohttp does not expose the bound port publicly
        assert server is not None
        self._port = server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> FakeRemnawave:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ---------------------------------------------------------------------------------------- seeding

    def add_token(
        self,
        scopes: Iterable[str] = ("*",),
        *,
        exp_days: float | None = 365,
        exp: datetime | None = None,
    ) -> str:
        token_uuid = str(uuidlib.uuid4())
        payload: dict[str, Any] = {"uuid": token_uuid, "role": "API", "iat": int(time.time())}
        if exp is not None:
            payload["exp"] = int(exp.timestamp())
        elif exp_days is not None:
            payload["exp"] = int(time.time() + exp_days * 86400)
        token = make_jwt(payload)
        self.tokens[token] = _Token(tuple(scopes), token_uuid)
        return token

    def add_internal_squad(self, name: str = "Default", *, squad_uuid: str | None = None) -> str:
        sid = squad_uuid or str(uuidlib.uuid4())
        now_s = iso(datetime.now(UTC))
        self.internal_squads[sid] = {
            "uuid": sid,
            "viewPosition": len(self.internal_squads),
            "name": name,
            "tags": [],
            "info": {"membersCount": 0, "inboundsCount": 1},
            "inbounds": [],
            "createdAt": now_s,
            "updatedAt": now_s,
        }
        return sid

    def add_external_squad(self, name: str = "Brand") -> str:
        sid = str(uuidlib.uuid4())
        now_s = iso(datetime.now(UTC))
        self.external_squads[sid] = {
            "uuid": sid,
            "viewPosition": len(self.external_squads),
            "name": name,
            "tags": [],
            "info": {"membersCount": 0},
            "templates": [],
            "subscriptionSettings": None,
            "hostOverrides": None,
            "responseHeadersAdd": {},
            "responseHeadersRemove": [],
            "hwidSettings": None,
            "customRemarks": None,
            "subpageConfigUuid": None,
            "createdAt": now_s,
            "updatedAt": now_s,
        }
        return sid

    def add_node(self, name: str = "NL-1", *, connected: bool = True) -> str:
        nid = str(uuidlib.uuid4())
        self.nodes.append(
            {
                "uuid": nid,
                "id": len(self.nodes) + 1,
                "name": name,
                "address": f"{name.lower()}.example.com",
                "port": 2222,
                "proxyUrl": None,
                "isConnected": connected,
                "isDisabled": False,
                "isConnecting": False,
                "lastStatusChange": iso(datetime.now(UTC)),
                "lastStatusMessage": None,
                "isTrafficTrackingActive": False,
                "trafficResetDay": None,
                "trafficLimitBytes": None,
                "trafficUsedBytes": 0,
                "notifyPercent": None,
                "viewPosition": len(self.nodes),
                "countryCode": "NL",
                "consumptionMultiplier": 1,
                "nodeConsumptionMultiplier": 1,
                "tags": [],
                "integrationUuids": [],
                "ips": [],
                "createdAt": iso(datetime.now(UTC)),
                "updatedAt": iso(datetime.now(UTC)),
                "configProfile": {"activeConfigProfileUuid": None, "activeInbounds": []},
                "providerUuid": None,
                "provider": None,
                "activePluginUuid": None,
                "system": None,
                "versions": {"xray": "25.9.11", "node": "2.1.0"},
                "xrayUptime": 100,
                "usersOnline": 3,
                "note": None,
            }
        )
        return nid

    def add_user(self, **fields: Any) -> dict[str, Any]:
        """Seed a user directly (camelCase fields override the defaults). Returns the stored dict."""
        uid = next(self._ids)
        now_dt = datetime.now(UTC)
        short = fields.pop("shortUuid", None) or self._short_uuid()
        user: dict[str, Any] = {
            "id": uid,
            "shortUuid": short,
            "username": fields.pop("username", f"user_{uid}"),
            "status": "ACTIVE",
            "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": now_dt + timedelta(days=30),
            "telegramId": None,
            "email": None,
            "description": None,
            "tag": None,
            "hwidDeviceLimit": None,
            "externalSquadUuid": None,
            "trojanPassword": secrets.token_urlsafe(12),
            "vlessUuid": str(uuidlib.uuid4()),
            "ssPassword": secrets.token_urlsafe(12),
            "lastTriggeredThreshold": 0,
            "subRevokedAt": None,
            "lastTrafficResetAt": None,
            "createdAt": now_dt,
            "updatedAt": now_dt,
            "activeInternalSquads": [],
            "usedTrafficBytes": 0,
            "lifetimeUsedTrafficBytes": 0,
            "onlineAt": None,
            "firstConnectedAt": None,
        }
        for key, value in fields.items():
            user[key] = (
                parse_iso(value) if key in ("expireAt", "createdAt") and isinstance(value, str) else value
            )
        self.users[uid] = user
        return user

    def user_json(self, user: Mapping[str, Any]) -> dict[str, Any]:
        """ExtendedUser as the panel serializes it (including the secret fields)."""
        return {
            "id": user["id"],
            "shortUuid": user["shortUuid"],
            "username": user["username"],
            "status": user["status"],
            "trafficLimitBytes": user["trafficLimitBytes"],
            "trafficLimitStrategy": user["trafficLimitStrategy"],
            "expireAt": iso(user["expireAt"]),
            "telegramId": user["telegramId"],
            "email": user["email"],
            "description": user["description"],
            "tag": user["tag"],
            "hwidDeviceLimit": user["hwidDeviceLimit"],
            "externalSquadUuid": user["externalSquadUuid"],
            "trojanPassword": user["trojanPassword"],
            "vlessUuid": user["vlessUuid"],
            "ssPassword": user["ssPassword"],
            "lastTriggeredThreshold": user["lastTriggeredThreshold"],
            "subRevokedAt": iso(user["subRevokedAt"]),
            "lastTrafficResetAt": iso(user["lastTrafficResetAt"]),
            "createdAt": iso(user["createdAt"]),
            "updatedAt": iso(user["updatedAt"]),
            "subscriptionUrl": f"{self.sub_domain}/{user['shortUuid']}",
            "activeInternalSquads": [
                {"uuid": s, "name": self.internal_squads.get(s, {}).get("name", "?")}
                for s in user["activeInternalSquads"]
            ],
            "userTraffic": {
                "usedTrafficBytes": user["usedTrafficBytes"],
                "lifetimeUsedTrafficBytes": user["lifetimeUsedTrafficBytes"],
                "onlineAt": iso(user["onlineAt"]),
                "firstConnectedAt": iso(user["firstConnectedAt"]),
                "lastConnectedNodeUuid": None,
            },
        }

    def _short_uuid(self) -> str:
        return "".join(secrets.choice(_SHORT_ALPHABET) for _ in range(16))

    def add_device(self, user_id: int, hwid: str, platform: str = "Android") -> None:
        now_s = iso(datetime.now(UTC))
        self.devices.setdefault(user_id, []).append(
            {
                "hwid": hwid,
                "userId": user_id,
                "platform": platform,
                "osVersion": "14",
                "deviceModel": "Pixel",
                "userAgent": "Happ/1.0",
                "requestIp": "203.0.113.7",
                "createdAt": now_s,
                "updatedAt": now_s,
            }
        )

    # ----------------------------------------------------------------------------------- assertions

    def calls(self, path: str | None = None, method: str | None = None) -> list[RecordedRequest]:
        """Recorded requests; ``path`` is exact (``/users``) or a prefix ending in ``*`` (``/users/*``)."""

        def wanted(r: RecordedRequest) -> bool:
            if method is not None and r.method != method.upper():
                return False
            if path is None:
                return True
            if path.endswith("*"):
                return r.path.startswith(path[:-1])
            return r.path == path

        return [r for r in self.requests if wanted(r)]

    def settings(self, token: str, **extra: Any) -> dict[str, Any]:
        """A settings mapping for :class:`~svbg.remnawave.transport.TransportConfig.from_settings`."""
        return {"REMNAWAVE_URL": self.url, "REMNAWAVE_TOKEN": token, **extra}

    def inject(
        self,
        kind: FaultKind,
        *,
        path: str | None = None,
        method: str | None = None,
        times: int | None = 1,
        delay: float = 0.0,
        retry_after: str | None = None,
    ) -> Fault:
        fault = Fault(kind, path, method, times, delay, retry_after)
        self.faults.append(fault)
        return fault

    def clear_faults(self) -> None:
        self.faults.clear()

    # ------------------------------------------------------------------------------------ webhooks

    def build_webhook(
        self,
        scope: str,
        event: str,
        data: Mapping[str, Any],
        *,
        secret: str,
        timestamp: datetime | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        ts = iso(timestamp or datetime.now(UTC))
        payload: dict[str, Any] = {"scope": scope, "event": event, "timestamp": ts, "data": dict(data)}
        if meta is not None:
            payload["meta"] = dict(meta)
        raw = webhook_body(payload)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Remnawave",
            "X-Remnawave-Signature": webhook_signature(raw, secret),
            "X-Remnawave-Timestamp": ts or "",
        }
        return raw, headers

    async def send_webhook(
        self,
        url: str,
        scope: str,
        event: str,
        data: Mapping[str, Any],
        *,
        secret: str,
        meta: Mapping[str, Any] | None = None,
    ) -> int:
        raw, headers = self.build_webhook(scope, event, data, secret=secret, meta=meta)
        async with (
            ClientSession(timeout=ClientTimeout(total=10)) as session,
            session.post(url, data=raw, headers=headers) as resp,
        ):
            return resp.status

    # ------------------------------------------------------------------------------- HTTP plumbing

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        raw = await request.read()
        body: Any = None
        if raw:
            with contextlib.suppress(ValueError):
                body = json.loads(raw)
        path = request.path[len("/api") :] if request.path.startswith("/api") else request.path
        self.requests.append(
            RecordedRequest(request.method, path, dict(request.query), dict(request.headers), body, raw)
        )
        if self.production and not (
            request.headers.get("X-Forwarded-For") and request.headers.get("X-Forwarded-Proto") == "https"
        ):
            return self._destroy(request)
        for fault in self.faults:
            if not fault.matches(request.method, path):
                continue
            if fault.times is not None:
                fault.times -= 1
            if fault.kind == "latency":
                await asyncio.sleep(fault.delay)
                break
            if fault.kind == "disconnect":
                return self._destroy(request)
            if fault.kind == "429":
                headers = {"Retry-After": fault.retry_after} if fault.retry_after is not None else {}
                return web.json_response({"message": "Too Many Requests"}, status=429, headers=headers)
            status = int(fault.kind)
            return web.json_response(
                {
                    "timestamp": iso(datetime.now(UTC)),
                    "path": request.path,
                    "message": "Fault",
                    "errorCode": "A000",
                },
                status=status,
            )
        try:
            return await handler(request)
        except _Reply as reply:
            return reply.response

    @staticmethod
    def _destroy(request: web.Request) -> web.StreamResponse:
        transport = request.transport
        if transport is not None:
            transport.close()
        return web.Response(status=500)

    def _routes(self, app: web.Application) -> None:
        r = app.router

        def add(method: str, path: str, handler: Handler, resource: str, slug: str, kind: str) -> None:
            r.add_route(method, "/api" + path, self._guard(handler, resource, slug, kind))

        add("GET", "/system/metadata", self._metadata, "system", "metadata", "read")
        add("GET", "/system/configuration", self._configuration, "system", "configuration", "read")
        add("GET", "/system/stats", self._stats, "system", "stats", "read")
        add("POST", "/users", self._create_user, "users", "create", "write")
        add("PATCH", "/users", self._update_user, "users", "update", "write")
        add("GET", "/users/stream", self._stream, "users", "stream", "read")
        add("POST", "/users/resolve", self._resolve, "users", "resolve", "read")
        add("POST", "/users/bulk/update-squads", self._bulk_squads, "users", "bulk-update-squads", "write")
        add(
            "POST",
            "/users/bulk/extend-expiration-date",
            self._bulk_extend,
            "users",
            "bulk-extend-expiration-date",
            "write",
        )
        add("GET", "/users/by-short-uuid/{value}", self._by_short_uuid, "users", "by-short-uuid", "read")
        add("GET", "/users/by-username/{value}", self._by_username, "users", "by-username", "read")
        add("GET", "/users/{id}", self._get_user, "users", "by-id", "read")
        add("DELETE", "/users/{id}", self._delete_user, "users", "delete", "write")
        add(
            "GET", "/users/{id}/accessible-nodes", self._accessible_nodes, "users", "accessible-nodes", "read"
        )
        add(
            "GET",
            "/users/{id}/subscription-request-history",
            self._request_history,
            "users",
            "subscription-request-history",
            "read",
        )
        add("POST", "/users/{id}/actions/enable", self._enable, "users", "enable", "write")
        add("POST", "/users/{id}/actions/disable", self._disable, "users", "disable", "write")
        add(
            "POST",
            "/users/{id}/actions/reset-traffic",
            self._reset_traffic,
            "users",
            "reset-traffic",
            "write",
        )
        add("POST", "/users/{id}/actions/revoke", self._revoke, "users", "revoke-subscription", "write")
        add("GET", "/hwid/devices/{id}", self._devices, "hwid-user-devices", "list-by-user", "read")
        add("POST", "/hwid/devices/delete", self._delete_device, "hwid-user-devices", "delete", "write")
        add(
            "POST",
            "/hwid/devices/delete-all",
            self._delete_all_devices,
            "hwid-user-devices",
            "delete-all",
            "write",
        )
        add("POST", "/connections/drop", self._drop, "connections", "drop", "write")
        add("GET", "/internal-squads", self._internal_squads, "internal-squads", "list", "read")
        add("GET", "/external-squads", self._external_squads, "external-squads", "list", "read")
        add("GET", "/nodes", self._nodes, "nodes", "list", "read")
        add("GET", "/subscription-settings", self._get_settings, "subscription-settings", "get", "read")
        add(
            "PATCH",
            "/subscription-settings",
            self._patch_settings,
            "subscription-settings",
            "update",
            "write",
        )
        add(
            "GET",
            "/subscriptions/connection-keys/{id}",
            self._connection_keys,
            "subscriptions",
            "connection-keys",
            "read",
        )
        add(
            "GET",
            "/subscriptions/subpage-config/{value}",
            self._subpage_config,
            "subscriptions",
            "subpage-config",
            "read",
        )
        add(
            "GET",
            "/subscription-page-configs/{value}",
            self._page_config,
            "subscription-page-configs",
            "get",
            "read",
        )
        add("GET", "/metadata/user/{id}", self._get_meta, "metadata", "get-user", "read")
        add("PUT", "/metadata/user/{id}", self._put_meta, "metadata", "upsert-user", "write")
        add(
            "POST", "/bandwidth-stats/nodes/usage", self._nodes_usage, "bandwidth-stats", "node-usage", "read"
        )
        add(
            "POST",
            "/internal-squads/{uuid}/bulk-actions/add-many-users",
            self._squad_add_users,
            "internal-squads",
            "add-many-users",
            "write",
        )

    def _guard(self, handler: Handler, resource: str, slug: str, kind: str) -> Handler:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            auth = request.headers.get("Authorization", "")
            token = self.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
            if token is None:
                return web.json_response({"message": "Unauthorized", "statusCode": 401}, status=401)
            allowed = {"*", f"{resource}:*", f"{resource}:{kind}", f"{resource}:{slug}"}
            if not allowed.intersection(token.scopes):
                return web.json_response(
                    {
                        "timestamp": iso(datetime.now(UTC)),
                        "path": request.path,
                        "message": "Forbidden",
                        "errorCode": "E000",
                    },
                    status=403,
                )
            return await handler(request)

        return wrapped

    # ---------------------------------------------------------------------------------- responses

    @staticmethod
    def _ok(payload: Any, status: int = 200) -> web.Response:
        return web.json_response(
            {"response": payload}, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    @staticmethod
    def _error(code: str, message: str, status: int, path: str = "") -> _Reply:
        return _Reply(
            web.json_response(
                {"timestamp": iso(datetime.now(UTC)), "path": path, "message": message, "errorCode": code},
                status=status,
            )
        )

    @staticmethod
    def _zod(message: str, path: list[str], code: str = "custom") -> _Reply:
        return _Reply(
            web.json_response(
                {
                    "message": "Validation failed",
                    "statusCode": 400,
                    "errors": [{"validation": "", "code": code, "message": message, "path": path}],
                },
                status=400,
            )
        )

    def _user_id_param(self, request: web.Request) -> int:
        raw = request.match_info["id"]
        try:
            value = float(raw)
        except ValueError:
            raise self._zod("Expected number, received nan", ["userId"], "invalid_type") from None
        if value <= 0 or value != int(value):
            raise self._zod("Number must be greater than 0", ["userId"], "too_small")
        return int(value)

    def _find(self, uid: int, code: str, request: web.Request) -> dict[str, Any]:
        user = self.users.get(uid)
        if user is None:
            message = "User not found" if code == "A025" else "User with specified params not found"
            raise self._error(code, message, 404, request.path)
        return user

    # ------------------------------------------------------------------------------------- system

    async def _metadata(self, request: web.Request) -> web.Response:
        return self._ok(
            {
                "version": self.version,
                "build": {"time": "2026-09-12T00:00:00Z", "number": "1"},
                "git": {
                    "backend": {"commitSha": "b22970c", "branch": "main", "commitUrl": "https://example.com"},
                    "frontend": {"commitSha": "0", "commitUrl": "https://example.com"},
                },
            }
        )

    async def _configuration(self, request: web.Request) -> web.Response:
        return self._ok(self.configuration)

    async def _stats(self, request: web.Request) -> web.Response:
        counts = dict.fromkeys(_STATUSES, 0)
        for u in self.users.values():
            counts[u["status"]] = counts.get(u["status"], 0) + 1
        return self._ok(
            {
                "cpu": {"cores": 2},
                "memory": {"total": 1, "free": 1, "used": 0},
                "uptime": 123.5,
                "timestamp": int(time.time() * 1000),
                "users": {"statusCounts": counts, "totalUsers": len(self.users)},
                "onlineStats": {"lastDay": 1, "lastWeek": 2, "neverOnline": 0, "onlineNow": 1},
                "nodes": {"totalOnline": len(self.nodes), "totalBytesLifetime": "123456789012"},
            }
        )

    # -------------------------------------------------------------------------------------- users

    def _check_squads(self, squads: Any, request: web.Request) -> list[str]:
        if not isinstance(squads, list):
            raise self._zod("Expected array", ["activeInternalSquads"], "invalid_type")
        for s in squads:
            if s not in self.internal_squads:
                raise self._error("A018", "Failed to create user", 500, request.path)
        return list(squads)

    def _check_ext(self, value: Any, request: web.Request) -> str | None:
        if value is None:
            return None
        if value not in self.external_squads:
            raise self._error("A182", "External squad not found", 404, request.path)
        return str(value)

    async def _create_user(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        username = body.get("username")
        if not isinstance(username, str) or not 3 <= len(username) <= 36 or not _USERNAME_RE.match(username):
            raise self._zod(
                "Username can only contain letters, numbers, underscores and dashes", ["username"]
            )
        expire = parse_iso(body.get("expireAt"))
        if expire is None:
            raise self._zod("Invalid datetime", ["expireAt"], "invalid_string")
        if "hwidDeviceLimit" in body and body["hwidDeviceLimit"] is None:
            raise self._zod("Expected number, received null", ["hwidDeviceLimit"], "invalid_type")
        tag = body.get("tag")
        if tag is not None and (not _TAG_RE.match(tag) or len(tag) > 16):
            raise self._zod("Tag can only contain uppercase letters, numbers, underscores", ["tag"])
        if any(u["username"] == username for u in self.users.values()):
            raise self._error("A019", "User username already exists", 400, request.path)
        short = body.get("shortUuid")
        if short is not None and any(u["shortUuid"] == short for u in self.users.values()):
            raise self._error("A020", "User short UUID already exists", 400, request.path)
        squads = self._check_squads(body.get("activeInternalSquads", []), request)
        ext = self._check_ext(body.get("externalSquadUuid"), request)
        status = "EXPIRED" if expire <= datetime.now(UTC) else body.get("status", "ACTIVE")
        fields: dict[str, Any] = {
            "username": username,
            "expireAt": expire,
            "status": status,
            "telegramId": body.get("telegramId"),
            "email": body.get("email"),
            "description": body.get("description"),
            "tag": tag,
            "trafficLimitBytes": body.get("trafficLimitBytes", 0),
            "trafficLimitStrategy": body.get("trafficLimitStrategy", "NO_RESET"),
            "hwidDeviceLimit": body.get("hwidDeviceLimit"),
            "externalSquadUuid": ext,
            "activeInternalSquads": squads,
        }
        if short is not None:
            fields["shortUuid"] = short
        if "createdAt" in body:
            fields["createdAt"] = body["createdAt"]
        user = self.add_user(**fields)
        return self._ok(self.user_json(user), status=201)

    async def _update_user(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        uid = body.get("id")
        username = body.get("username")
        if uid is None and username is None:
            raise self._zod("At least one of username, id must be provided", [])
        user = None
        if uid is not None:
            user = self.users.get(uid)
        else:
            user = next((u for u in self.users.values() if u["username"] == username), None)
        if user is None:
            raise self._error("A025", "User not found", 404, request.path)
        now_dt = datetime.now(UTC)
        if "expireAt" in body:
            expire = parse_iso(body["expireAt"])
            if expire is None:
                raise self._zod("Invalid datetime", ["expireAt"], "invalid_string")
            if expire <= now_dt:
                raise self._zod("Expiration date cannot be in the past", ["expireAt"])
        if body.get("hwidDeviceLimit") is not None and body["hwidDeviceLimit"] < 0:
            raise self._zod("Number must be greater than or equal to 0", ["hwidDeviceLimit"], "too_small")
        if "activeInternalSquads" in body:
            body["activeInternalSquads"] = self._check_squads(body["activeInternalSquads"], request)
        if "externalSquadUuid" in body:
            body["externalSquadUuid"] = self._check_ext(body["externalSquadUuid"], request)
        old_limit = user["trafficLimitBytes"]
        for key in (
            "trafficLimitBytes",
            "trafficLimitStrategy",
            "description",
            "tag",
            "telegramId",
            "email",
            "hwidDeviceLimit",
            "activeInternalSquads",
            "externalSquadUuid",
        ):
            if key in body:
                user[key] = body[key]
        if "expireAt" in body:
            user["expireAt"] = parse_iso(body["expireAt"])
            if user["status"] == "EXPIRED" and "status" not in body:
                user["status"] = "ACTIVE"
        if "trafficLimitBytes" in body and user["status"] == "LIMITED":
            new_limit = body["trafficLimitBytes"]
            if new_limit == 0 or new_limit > old_limit:
                user["status"] = "ACTIVE"
        if body.get("status") == "DISABLED" and user["status"] == "ACTIVE":
            user["status"] = "DISABLED"
        user["updatedAt"] = now_dt
        return self._ok(self.user_json(user))

    async def _get_user(self, request: web.Request) -> web.Response:
        return self._ok(self.user_json(self._find(self._user_id_param(request), "A063", request)))

    async def _by_short_uuid(self, request: web.Request) -> web.Response:
        value = request.match_info["value"]
        user = next((u for u in self.users.values() if u["shortUuid"] == value), None)
        if user is None:
            raise self._error("A063", "User with specified params not found", 404, request.path)
        return self._ok(self.user_json(user))

    async def _by_username(self, request: web.Request) -> web.Response:
        value = request.match_info["value"]
        user = next((u for u in self.users.values() if u["username"] == value), None)
        if user is None:
            raise self._error("A063", "User with specified params not found", 404, request.path)
        return self._ok(self.user_json(user))

    async def _resolve(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        given = [k for k in ("id", "shortUuid", "username") if body.get(k) is not None]
        if len(given) != 1:
            raise self._zod("Exactly one of id, shortUuid, or username must be provided", [])
        key = given[0]
        user = next((u for u in self.users.values() if u[key] == body[key]), None)
        if user is None:
            raise self._error("A063", "User with specified params not found", 404, request.path)
        return self._ok({"id": user["id"], "username": user["username"], "shortUuid": user["shortUuid"]})

    async def _stream(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            size = int(q.get("size", "250"))
            cursor = int(q["cursor"]) if "cursor" in q else 0
        except ValueError:
            raise self._zod("Expected number", ["size"], "invalid_type") from None
        if not 1 <= size <= 1000:
            raise self._zod("Number must be between 1 and 1000", ["size"], "too_big")
        items = [u for uid, u in sorted(self.users.items()) if uid > cursor]
        if "status" in q:
            items = [u for u in items if u["status"] == q["status"]]
        if "telegramId" in q:
            items = [u for u in items if str(u["telegramId"]) == q["telegramId"]]
        if "tag" in q:
            items = [u for u in items if u["tag"] == q["tag"]]
        if "email" in q:
            items = [u for u in items if u["email"] == q["email"]]
        page = items[:size]
        has_more = len(items) > size
        return self._ok(
            {
                "users": [self.user_json(u) for u in page],
                "nextCursor": str(page[-1]["id"]) if has_more and page else None,
                "hasMore": has_more,
            }
        )

    async def _enable(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        if user["status"] != "DISABLED":
            raise self._error("A030", "User already enabled", 400, request.path)
        user["status"] = "ACTIVE"
        return self._ok(self.user_json(user))

    async def _disable(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        if user["status"] == "DISABLED":
            raise self._error("A029", "User already disabled", 400, request.path)
        user["status"] = "DISABLED"
        return self._ok(self.user_json(user))

    async def _reset_traffic(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        user["usedTrafficBytes"] = 0
        user["lastTrafficResetAt"] = datetime.now(UTC)
        if user["status"] == "LIMITED":
            user["status"] = "ACTIVE"
        return self._ok(self.user_json(user))

    async def _revoke(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        body = await self._json(request)
        if not body.get("revokeOnlyPasswords"):
            user["shortUuid"] = body.get("shortUuid") or self._short_uuid()
        user["trojanPassword"] = secrets.token_urlsafe(12)
        user["ssPassword"] = secrets.token_urlsafe(12)
        user["vlessUuid"] = str(uuidlib.uuid4())
        user["subRevokedAt"] = datetime.now(UTC)
        return self._ok(self.user_json(user))

    async def _delete_user(self, request: web.Request) -> web.Response:
        uid = self._user_id_param(request)
        self._find(uid, "A025", request)
        del self.users[uid]
        return web.Response(status=204)

    async def _accessible_nodes(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        nodes = [
            {
                "uuid": n["uuid"],
                "nodeName": n["name"],
                "countryCode": n["countryCode"],
                "configProfileUuid": str(uuidlib.uuid4()),
                "configProfileName": "Default",
                "activeSquads": [{"squadName": "Default", "activeInbounds": ["VLESS"]}],
            }
            for n in self.nodes
        ]
        return self._ok({"userId": user["id"], "activeNodes": nodes})

    async def _request_history(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        record = {
            "id": 1,
            "userId": user["id"],
            "requestAt": iso(datetime.now(UTC)),
            "srrResponseType": "XRAY_JSON",
            "requestIp": "203.0.113.7",
            "userAgent": "Happ/1.0",
            "srrRuleName": None,
        }
        return self._ok({"total": 1, "records": [record]})

    async def _bulk_squads(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        ids = body.get("userIds") or []
        if not 1 <= len(ids) <= 500:
            raise self._zod("Array must contain between 1 and 500 element(s)", ["userIds"], "too_big")
        squads = self._check_squads(body.get("activeInternalSquads", []), request)
        for uid in ids:
            if uid in self.users:
                self.users[uid]["activeInternalSquads"] = list(squads)
        return web.Response(status=204)

    async def _bulk_extend(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        ids = body.get("userIds") or []
        if not 1 <= len(ids) <= 500:
            raise self._zod("Array must contain between 1 and 500 element(s)", ["userIds"], "too_big")
        days = body.get("extendDays", 0)
        for uid in ids:
            if uid in self.users:
                self.users[uid]["expireAt"] += timedelta(days=days)
        return web.Response(status=204)

    # --------------------------------------------------------------------------------------- hwid

    def _devices_json(self, uid: int) -> dict[str, Any]:
        items = self.devices.get(uid, [])
        return {"total": len(items), "devices": list(items)}

    async def _devices(self, request: web.Request) -> web.Response:
        return self._ok(self._devices_json(self._user_id_param(request)))

    async def _delete_device(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        uid = body.get("userId")
        hwid = body.get("hwid")
        items = self.devices.get(uid, [])
        if not any(d["hwid"] == hwid for d in items):
            raise self._error("A204", "HWID device not found", 404, request.path)
        self.devices[uid] = [d for d in items if d["hwid"] != hwid]
        return self._ok(self._devices_json(uid))

    async def _delete_all_devices(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        uid = body.get("userId")
        self.devices[uid] = []
        return self._ok(self._devices_json(uid))

    async def _drop(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        if (body.get("dropBy") or {}).get("by") not in ("userIds", "ipAddresses"):
            raise self._zod("Invalid discriminator value", ["dropBy", "by"], "invalid_union_discriminator")
        self.dropped.append(body)
        return web.Response(status=202)

    # ---------------------------------------------------------------------------- squads, nodes

    async def _internal_squads(self, request: web.Request) -> web.Response:
        items = list(self.internal_squads.values())
        for squad in items:
            squad["info"]["membersCount"] = sum(
                1 for u in self.users.values() if squad["uuid"] in u["activeInternalSquads"]
            )
        return self._ok({"total": len(items), "internalSquads": items})

    async def _external_squads(self, request: web.Request) -> web.Response:
        items = list(self.external_squads.values())
        return self._ok({"total": len(items), "externalSquads": items})

    async def _nodes(self, request: web.Request) -> web.Response:
        return self._ok(self.nodes)

    async def _get_settings(self, request: web.Request) -> web.Response:
        return self._ok(self.subscription_settings)

    async def _patch_settings(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        if body.get("uuid") != self.subscription_settings["uuid"]:
            raise self._error("A000", "Subscription settings not found", 404, request.path)
        for key, value in body.items():
            if key != "uuid":
                self.subscription_settings[key] = value
        return self._ok(self.subscription_settings)

    async def _connection_keys(self, request: web.Request) -> web.Response:
        user = self._find(self._user_id_param(request), "A025", request)
        key = f"vless://{user['vlessUuid']}@nl-1.example.com:443?security=reality#NL"
        return self._ok({"enabledKeys": [key], "hiddenKeys": [], "disabledKeys": []})

    async def _subpage_config(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        if not isinstance(body.get("requestHeaders"), dict):
            raise self._zod("Required", ["requestHeaders"], "invalid_type")
        value = request.match_info["value"]
        if not any(u["shortUuid"] == value for u in self.users.values()):
            raise self._error("A063", "User with specified params not found", 404, request.path)
        page = next(iter(self.page_configs), None)
        return self._ok({"subpageConfigUuid": page, "webpageAllowed": True})

    async def _page_config(self, request: web.Request) -> web.Response:
        value = request.match_info["value"]
        page = self.page_configs.get(value)
        if page is None:
            raise self._error("A230", "Subscription page config not found", 404, request.path)
        return self._ok(page)

    async def _get_meta(self, request: web.Request) -> web.Response:
        uid = self._user_id_param(request)
        self._find(uid, "A025", request)
        return self._ok({"metadata": self.user_metadata.get(uid, {})})

    async def _put_meta(self, request: web.Request) -> web.Response:
        uid = self._user_id_param(request)
        self._find(uid, "A025", request)
        body = await self._json(request)
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            raise self._zod("Expected object", ["metadata"], "invalid_type")
        self.user_metadata[uid] = dict(metadata)
        return self._ok({"metadata": self.user_metadata[uid]})

    async def _json(self, request: web.Request) -> dict[str, Any]:
        raw = await request.read()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise self._zod("Invalid JSON", [], "invalid_type") from None
        if not isinstance(data, dict):
            raise self._zod("Expected object", [], "invalid_type")
        return data

    # ------------------------------------------------------------------------------- LTE (ext/lte)

    async def _nodes_usage(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        nodes = body.get("nodesUuids")
        if not isinstance(nodes, list) or not nodes:
            raise self._zod("Array must contain at least 1 element(s)", ["nodesUuids"], "too_small")
        start, end = request.query.get("start"), request.query.get("end")
        if not start or not end:
            raise self._zod("Required", ["start"], "invalid_type")
        out = []
        for node in nodes:
            rows = self.node_usage.get((str(node), start), {}) if start == end else {}
            out.append(
                {"uuid": node, "users": [{"id": uid, "totalBytes": total} for uid, total in rows.items()]}
            )
        return self._ok({"nodes": out})

    async def _squad_add_users(self, request: web.Request) -> web.Response:
        squad = request.match_info["uuid"]
        if squad not in self.internal_squads:
            raise self._error("A118", "Internal squad not found", 404, request.path)
        body = await self._json(request)
        ids = [int(i) for i in body.get("userIds") or []]
        self.squad_resends.append((squad, ids))
        return self._ok({"eventSent": True})
