"""aiohttp application factory and server lifecycle.

Everything public (Telegram webhook ``/tg/*``, provider webhooks ``/webhooks/*``) and local-only
(``/health``, ``/ready``) is served by one :class:`aiohttp.web.Application` built here:

* request bodies are capped at :data:`MAX_BODY_BYTES` (256 KB) — early ``413`` by ``Content-Length``,
  and aiohttp enforces the same cap while reading chunked bodies;
* every response carries security headers and ``X-Request-Id``;
* unexpected handler errors become a generic ``500`` JSON (no stack traces leak to clients);
* the access log never contains query strings, and path parameters that look like secrets
  (``{secret}``, ``{token}``, ``{webhook_token}``…) are replaced with ``***``;
* ``/health`` and ``/ready`` answer only to direct loopback peers (``request.remote``); everybody else gets
  ``404`` as if the route did not exist. Forwarded headers are never trusted to *grant* access, but
  their presence denies it: a reverse proxy on the same host (Caddy → ``127.0.0.1:8080``) makes every
  outside request look local, and such proxies always add ``X-Forwarded-For``/``Via``/….
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

if TYPE_CHECKING:
    from aiohttp.web_request import BaseRequest

    from svbg.core.component import ComponentRegistry

__all__ = [
    "MAX_BODY_BYTES",
    "REQUEST_ID_KEY",
    "ReadyCheck",
    "SafeAccessLogger",
    "WebServer",
    "build_web_app",
    "components_ready",
    "is_loopback",
]

log = logging.getLogger("svbg.web")
access_log = logging.getLogger("svbg.web.access")

MAX_BODY_BYTES: Final = 256 * 1024
READY_TIMEOUT: Final = 5.0
REQUEST_ID_KEY: Final = web.RequestKey("svbg_request_id", str)
_READY_CHECK_KEY: Final = web.AppKey("svbg_ready_check", object)
_MAX_BODY_KEY: Final = web.AppKey("svbg_max_body", int)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SECRET_PARAM_RE = re.compile(r"(secret|token|key|sig)", re.IGNORECASE)
_LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# Any of these means the request came through a proxy, i.e. the real client is not local.
_PROXY_HEADERS: Final = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "Via",
)
_MAX_LOGGED_PATH = 200

SECURITY_HEADERS: Final[Mapping[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

ReadyCheck = Callable[[], Awaitable[tuple[bool, Mapping[str, Any]]]]
"""Returns ``(ready, details)``; details must be safe to show (no secrets)."""


def is_loopback(request: web.BaseRequest) -> bool:
    """True if the TCP peer is the local host and the request was not relayed by a proxy.

    Proxy headers never make a request local (they are client-controlled), but any of them makes it
    non-local: a same-host reverse proxy connects from 127.0.0.1 on behalf of an outside client.
    """
    if request.remote not in _LOOPBACK:
        return False
    headers = request.headers
    return not any(name in headers for name in _PROXY_HEADERS)


def _request_id(request: web.BaseRequest) -> str:
    incoming = request.headers.get("X-Request-Id", "")
    if _REQUEST_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex


def _safe_path(request: BaseRequest) -> str:
    path = request.raw_path.split("?", 1)[0]
    match_info = getattr(request, "match_info", None)
    if match_info:
        for name, value in match_info.items():
            if value and _SECRET_PARAM_RE.search(name):
                path = path.replace(value, "***")
    if len(path) > _MAX_LOGGED_PATH:
        path = path[:_MAX_LOGGED_PATH] + "…"
    return path


class SafeAccessLogger(AbstractAccessLogger):
    """Access log line without query strings and with secret path parameters masked."""

    def log(self, request: BaseRequest, response: web.StreamResponse, time: float) -> None:
        rid = request.get(REQUEST_ID_KEY, "-")
        self.logger.info(
            "%s %s %s %d %d %.1fms rid=%s",
            request.remote or "-",
            request.method,
            _safe_path(request),
            response.status,
            response.body_length,
            time * 1000,
            rid,
        )


def _json_error(status: int, error: str, request: web.Request) -> web.Response:
    return web.json_response({"error": error, "request_id": request.get(REQUEST_ID_KEY)}, status=status)


@web.middleware
async def _guard_middleware(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    request[REQUEST_ID_KEY] = _request_id(request)
    length = request.content_length
    if length is not None and length > request.app[_MAX_BODY_KEY]:
        return _json_error(413, "payload_too_large", request)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("unhandled error in %s %s", request.method, _safe_path(request))
        return _json_error(500, "internal", request)


async def _on_prepare(request: web.Request, response: web.StreamResponse) -> None:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers["Server"] = "svbg"
    rid = request.get(REQUEST_ID_KEY)
    if rid:
        response.headers["X-Request-Id"] = rid


async def _health(request: web.Request) -> web.Response:
    if not is_loopback(request):
        raise web.HTTPNotFound
    return web.json_response({"status": "ok"})


async def _ready(request: web.Request) -> web.Response:
    if not is_loopback(request):
        raise web.HTTPNotFound
    check: ReadyCheck | None = request.app[_READY_CHECK_KEY]  # type: ignore[assignment]
    if check is None:
        return web.json_response({"ready": True, "components": {}})
    try:
        async with asyncio.timeout(READY_TIMEOUT):
            ready, details = await check()
    except TimeoutError:
        return web.json_response({"ready": False, "error": "timeout"}, status=503)
    return web.json_response({"ready": ready, "components": dict(details)}, status=200 if ready else 503)


def build_web_app(
    routes: Iterable[web.AbstractRouteDef] = (),
    *,
    ready_check: ReadyCheck | None = None,
    client_max_size: int = MAX_BODY_BYTES,
) -> web.Application:
    """Create the application with hardening middleware, ``/health``, ``/ready`` and ``routes``."""
    app = web.Application(client_max_size=client_max_size, middlewares=[_guard_middleware])
    app[_READY_CHECK_KEY] = ready_check
    app[_MAX_BODY_KEY] = client_max_size
    app.on_response_prepare.append(_on_prepare)
    app.router.add_get("/health", _health, allow_head=True)
    app.router.add_get("/ready", _ready, allow_head=True)
    app.router.add_routes(list(routes))
    return app


def components_ready(registry: ComponentRegistry) -> ReadyCheck:
    """Ready check over a component registry: ready unless some component is ``DOWN``."""
    from svbg.core.component import Health

    async def check() -> tuple[bool, Mapping[str, Any]]:
        reports = await registry.health_all()
        ready = all(r.status is not Health.DOWN for r in reports.values())
        return ready, {name: r.status.value for name, r in reports.items()}

    return check


class WebServer:
    """Runs an application on ``host:port``. ``port=0`` binds a free port (see :attr:`port`)."""

    def __init__(
        self,
        host: str,
        port: int,
        app: web.Application,
        *,
        shutdown_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self._requested_port = port
        self.app = app
        self._shutdown_timeout = shutdown_timeout
        self._runner: web.AppRunner | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def port(self) -> int:
        if self._runner is None:
            return self._requested_port
        addresses = self._runner.addresses
        return int(addresses[0][1]) if addresses else self._requested_port

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        async with self._lock:
            if self._runner is not None:
                return
            runner = web.AppRunner(
                self.app,
                access_log_class=SafeAccessLogger,
                access_log=access_log,
                shutdown_timeout=self._shutdown_timeout,
            )
            await runner.setup()
            try:
                site = web.TCPSite(runner, self.host, self._requested_port)
                await site.start()
            except OSError:
                await runner.cleanup()
                raise
            self._runner = runner
            log.info("web server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        async with self._lock:
            runner, self._runner = self._runner, None
            if runner is not None:
                await runner.cleanup()
                log.info("web server stopped")
