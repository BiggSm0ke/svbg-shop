"""HTTP transport to the Remnawave panel (02 §2.4–2.5).

One :class:`Transport` owns one ``aiohttp.ClientSession`` and everything that protects the panel and the bot:

* headers: ``Authorization: Bearer``, ``User-Agent``, automatic ``X-Forwarded-For/Proto`` for ``http://``
  (otherwise a production panel destroys the socket without a response), Caddy ``X-Api-Key``, Cloudflare
  Access, ``Cookie``; ``REMNAWAVE_TLS_VERIFY`` is honoured and never guessed; plain ``http://`` is accepted
  only for docker / LAN hosts unless the owner allowed it explicitly (the token would travel in clear);
* response bodies are read with a hard limit (``max_body``), never buffered whole;
* timeouts ``connect=5 s``, ``sock_read=15 s``, ``total=20 s`` and an optional per-call budget;
* retries **only** for idempotent calls and only for ``transient`` errors: 3 attempts, 0.5 → 1 → 2 s with
  jitter, ``Retry-After`` honoured; a 429 sets a process-wide pause shared by every caller;
* token buckets: interactive lane 20 rps, background lane 5 rps (background also never takes more than half
  of the connection pool, so it yields to interactive work);
* a circuit breaker (CLOSED → OPEN after 5 consecutive transient/proxy failures within 30 s or at once on
  ``auth``; OPEN → HALF_OPEN after 30 s, then exponentially up to 5 min; one ``GET /system/metadata`` trial
  in its own task, so a cancelled caller never leaves the breaker in HALF_OPEN);
* in-flight accounting so a hot swap can close the old session only after its requests finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import logging
import random
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from svbg import __version__
from svbg.core.clock import now
from svbg.core.errors.breaker import BreakerState
from svbg.core.log import register_secret
from svbg.remnawave.errors import (
    ErrorKind,
    PanelUnavailableError,
    RemnawaveError,
    error_from_response,
    safe_path,
)

log = logging.getLogger("svbg.remnawave")

__all__ = [
    "Lane",
    "PanelBreaker",
    "RawResponse",
    "TokenBucket",
    "Transport",
    "TransportConfig",
    "is_internal_host",
    "normalize_url",
]

USER_AGENT: Final = f"SvBG-Shop/{__version__}"
DEFAULT_PORT: Final = 3000
#: Largest accepted response body (a 500-user ``users/stream`` page is ~2 MB); read with a limit, never whole.
MAX_BODY: Final = 16 * 1024 * 1024
_READ_CHUNK: Final = 64 * 1024
RETRY_AFTER_CAP: Final = 300.0
_TRIAL_PATH: Final = "/system/metadata"
#: Methods aiohttp itself re-sends once when a persistent connection drops before the response.
_AIOHTTP_RETRIES: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})


class Lane(StrEnum):
    """Who is waiting: a user/owner click (``interactive``) or a sync/import pass (``background``)."""

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


# ------------------------------------------------------------------------------------------------ config


def normalize_url(raw: str) -> str:
    """Normalize the panel address (idea of Remnashop ``remnawave.py``, MIT).

    ``panel.example.com`` → ``https://panel.example.com``; ``remnawave`` → ``http://remnawave:3000``;
    a trailing ``/`` or ``/api`` is removed. Raises :class:`ValueError` with a Russian message.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("адрес панели пуст")
    if "://" not in text:
        host_part = text.split("/", 1)[0]
        host = host_part.rsplit(":", 1)[0] if host_part.count(":") == 1 else host_part
        if _is_ip(host) or "." not in host:
            port = "" if host_part.count(":") == 1 else f":{DEFAULT_PORT}"
            text = f"http://{text.split('/', 1)[0]}{port}" + (
                "/" + text.split("/", 1)[1] if "/" in text else ""
            )
        else:
            text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("адрес панели должен начинаться с http:// или https://")
    if not parts.hostname:
        raise ValueError("в адресе панели нет имени хоста")
    if parts.query or parts.fragment:
        raise ValueError("адрес панели не должен содержать ? или #")
    if parts.username or parts.password:
        raise ValueError("не указывайте логин и пароль в адресе панели — используйте API-токен")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


#: Names that never resolve on the public internet (RFC 6762, RFC 8375, docker's ``host.docker.internal``).
_INTERNAL_SUFFIXES: Final = (".internal", ".local", ".lan", ".home.arpa", ".localhost")
_CGNAT: Final = ip_network("100.64.0.0/10")  # Tailscale / carrier-grade NAT: not routed on the internet


def is_internal_host(host: str) -> bool:
    """The host is reachable only inside a docker / local network, so plain ``http://`` does not leave it.

    Single-label docker names (``remnawave``, ``svbg-panel``), reserved internal suffixes, loopback,
    private (RFC 1918 / ULA), link-local and CGNAT addresses. Public IPs and dotted domains are not.
    """
    name = (host or "").strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    try:
        ip = ip_address(name.split("%", 1)[0])
    except ValueError:
        return "." not in name or name.endswith(_INTERNAL_SUFFIXES)
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, IPv4Address) and ip in _CGNAT:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _is_ip(host: str) -> bool:
    try:
        ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Connection profile of the panel. ``repr`` never shows secrets."""

    base_url: str
    token: str = field(repr=False)
    caddy_token: str | None = field(default=None, repr=False)
    cf_client_id: str | None = field(default=None, repr=False)
    cf_client_secret: str | None = field(default=None, repr=False)
    cookie: str | None = field(default=None, repr=False)
    tls_verify: bool = True
    #: ``None`` — automatic (only for ``http://``); ``True``/``False`` force the X-Forwarded-* headers.
    forwarded_headers: bool | None = None
    #: Owner's explicit consent (``REMNAWAVE_ALLOW_PLAIN_HTTP``) to ``http://`` on an external address: the
    #: token then travels unencrypted. Without it such an address is refused.
    allow_plain_http: bool = False
    user_agent: str = USER_AGENT
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    total_timeout: float = 20.0
    interactive_rps: float = 20.0
    background_rps: float = 5.0
    max_attempts: int = 3
    backoff_base: float = 0.5
    connector_limit: int = 8
    keepalive_timeout: float = 30.0
    breaker_threshold: int = 5
    breaker_window: float = 30.0
    breaker_cooldown: float = 30.0
    breaker_max_cooldown: float = 300.0
    max_body: int = MAX_BODY

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_url(self.base_url))
        if self.plain_http_external and not self.allow_plain_http:
            raise ValueError(
                f"адрес панели {self.base_url} ведёт во внешнюю сеть, а по http:// API-токен и заголовки "
                "доступа уйдут без шифрования. Укажите https://… (через reverse proxy) или адрес из "
                "docker-сети (http://remnawave:3000). Если канал защищён иначе (VPN), разрешите это явно: "
                "REMNAWAVE_ALLOW_PLAIN_HTTP=true"
            )
        if not self.token or not self.token.strip():
            raise ValueError("не указан API-токен панели")
        if self.interactive_rps <= 0 or self.background_rps <= 0:
            raise ValueError("темп запросов должен быть больше нуля")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    @property
    def is_http(self) -> bool:
        return self.base_url.startswith("http://")

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def plain_http_external(self) -> bool:
        """``http://`` to a public IP or a dotted domain: everything, the token included, goes in clear."""
        return self.is_http and not is_internal_host(self.host)

    @property
    def sends_forwarded(self) -> bool:
        return self.is_http if self.forwarded_headers is None else self.forwarded_headers

    def secrets(self) -> tuple[str, ...]:
        return tuple(
            s
            for s in (self.token, self.caddy_token, self.cf_client_secret, self.cookie, self.cf_client_id)
            if s
        )

    def headers(self) -> dict[str, str]:
        """Default headers of every request (02 §2.4)."""
        headers = {
            "Authorization": f"Bearer {self.token.strip()}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.sends_forwarded:
            headers["X-Forwarded-For"] = "127.0.0.1"
            headers["X-Forwarded-Proto"] = "https"
        if self.caddy_token:
            headers["X-Api-Key"] = self.caddy_token
        if self.cf_client_id and self.cf_client_secret:
            headers["CF-Access-Client-Id"] = self.cf_client_id
            headers["CF-Access-Client-Secret"] = self.cf_client_secret
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any]) -> TransportConfig | None:
        """Build from a settings snapshot; ``None`` when the panel is not configured (no URL and no token).

        Raises :class:`ValueError` (Russian) when only one of URL / token is set or a value is invalid.
        """
        url = _opt_str(cfg.get("REMNAWAVE_URL"))
        token = _opt_str(cfg.get("REMNAWAVE_TOKEN"))
        if url is None and token is None:
            return None
        if url is None:
            raise ValueError("укажите адрес панели (REMNAWAVE_URL)")
        if token is None:
            raise ValueError("укажите API-токен панели (REMNAWAVE_TOKEN)")
        tls = cfg.get("REMNAWAVE_TLS_VERIFY", True)
        return cls(
            base_url=url,
            token=token,
            caddy_token=_opt_str(cfg.get("REMNAWAVE_CADDY_TOKEN")),
            cf_client_id=_opt_str(cfg.get("REMNAWAVE_CF_CLIENT_ID")),
            cf_client_secret=_opt_str(cfg.get("REMNAWAVE_CF_CLIENT_SECRET")),
            cookie=_opt_str(cfg.get("REMNAWAVE_COOKIE")),
            tls_verify=True if tls is None else bool(tls),
            allow_plain_http=bool(cfg.get("REMNAWAVE_ALLOW_PLAIN_HTTP") or False),
            interactive_rps=float(cfg.get("REMNAWAVE_RPS_INTERACTIVE") or 20.0),
            background_rps=float(cfg.get("REMNAWAVE_RPS_BACKGROUND") or 5.0),
        )


# ----------------------------------------------------------------------------------------- rate limiting


class TokenBucket:
    """Reservation token bucket: every caller gets a slot immediately and sleeps until it comes (FIFO)."""

    def __init__(
        self, rate: float, burst: float | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = max(1.0, burst if burst is not None else rate)
        self._tokens = self.burst
        self._clock = clock
        self._stamp = clock()

    def reserve(self) -> float:
        """Take one token; returns how long to wait before using it (0 when available now)."""
        t = self._clock()
        self._tokens = min(self.burst, self._tokens + (t - self._stamp) * self.rate)
        self._stamp = t
        self._tokens -= 1.0
        return 0.0 if self._tokens >= 0 else -self._tokens / self.rate

    def refund(self) -> None:
        """Give a reserved token back (the caller gave up before using it)."""
        self._tokens = min(self.burst, self._tokens + 1.0)


# ------------------------------------------------------------------------------------------------ breaker

BreakerHook = Callable[[BreakerState, BreakerState], Awaitable[None] | None]


class PanelBreaker:
    """Panel circuit breaker (02 §2.5). Times use the monotonic clock; ``opened_at`` is wall time (UTC)."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        window: float = 30.0,
        cooldown: float = 30.0,
        max_cooldown: float = 300.0,
        trial_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        on_change: BreakerHook | None = None,
    ) -> None:
        self.threshold = max(1, threshold)
        #: A HALF_OPEN older than this has lost its trial (it can never stay HALF_OPEN for good).
        self.trial_timeout = trial_timeout
        self.window = window
        self.base_cooldown = cooldown
        self.max_cooldown = max(cooldown, max_cooldown)
        self._clock = clock
        self._on_change = on_change
        self._state = BreakerState.CLOSED
        self._failures: deque[float] = deque(maxlen=self.threshold)
        self._cooldown = cooldown
        self._open_until = 0.0
        self._half_open_at = 0.0
        self.opened_at: datetime | None = None
        self.trips = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def state(self) -> BreakerState:
        return self._state

    def retry_in(self) -> float:
        return max(0.0, self._open_until - self._clock())

    def trial_due(self) -> bool:
        """The next request should run the HALF_OPEN trial: OPEN with the cooldown over, or a stale
        HALF_OPEN whose trial never reported back."""
        if self._state is BreakerState.OPEN:
            return self._clock() >= self._open_until
        if self._state is BreakerState.HALF_OPEN:
            return self._clock() - self._half_open_at >= self.trial_timeout
        return False

    def record_failure(self, kind: ErrorKind) -> None:
        if kind is ErrorKind.AUTH:
            self.trip()
            return
        if kind not in (ErrorKind.TRANSIENT, ErrorKind.PROXY_CHECK):
            self.record_success()
            return
        if self._state is BreakerState.HALF_OPEN:
            self.trip()
            return
        t = self._clock()
        self._failures.append(t)
        if (
            self._state is BreakerState.CLOSED
            and len(self._failures) >= self.threshold
            and t - self._failures[0] <= self.window
        ):
            self.trip()

    def record_success(self) -> None:
        self._failures.clear()
        if self._state is not BreakerState.CLOSED:
            self._cooldown = self.base_cooldown
            self.opened_at = None
            self._set(BreakerState.CLOSED)

    def trip(self) -> None:
        """Open now; a re-open from HALF_OPEN doubles the cooldown (up to ``max_cooldown``)."""
        if self._state is BreakerState.HALF_OPEN:
            self._cooldown = min(self._cooldown * 2, self.max_cooldown)
        elif self._state is BreakerState.OPEN:
            return
        self._failures.clear()
        self._open_until = self._clock() + self._cooldown
        if self.opened_at is None:
            self.opened_at = now()
        self.trips += 1
        self._set(BreakerState.OPEN)

    def half_open(self) -> None:
        if self._state is BreakerState.OPEN or self.trial_due():
            self._half_open_at = self._clock()
            self._set(BreakerState.HALF_OPEN)

    def abort_trial(self) -> None:
        """The trial ended without an answer (cancelled, session closed): back to OPEN, same cooldown."""
        if self._state is BreakerState.HALF_OPEN:
            self._open_until = self._clock() + self._cooldown
            self._set(BreakerState.OPEN)

    def inherit(self, other: PanelBreaker) -> None:
        """Continue ``other``'s outage after a hot swap to the same panel (no hook events: nothing changed).

        ``opened_at`` (auto-maintenance's «OPEN > 3 min»), cooldown and trip count carry over; the trial is
        due at once because the new settings may be the fix.
        """
        if other._state is BreakerState.CLOSED:
            return
        self._failures.clear()
        self._state = BreakerState.OPEN
        self.opened_at = other.opened_at
        self._cooldown = other._cooldown
        self.trips = other.trips
        self._open_until = self._clock()

    def reset(self) -> None:
        self.record_success()

    def _set(self, new: BreakerState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        log.warning("remnawave breaker: %s → %s", old.value, new.value)
        hook = self._on_change
        if hook is None:
            return
        try:
            result = hook(old, new)
        except Exception:
            log.exception("remnawave breaker hook failed")
            return
        if asyncio.iscoroutine(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                result.close()
                return
            task = loop.create_task(_swallow(result), name="remnawave-breaker-hook")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


async def _swallow(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception:
        log.exception("remnawave breaker hook failed")


# ------------------------------------------------------------------------------------------------ transport


@dataclass(frozen=True, slots=True, kw_only=True)
class _Call:
    method: str
    path: str
    json_body: bytes | None = None
    params: Mapping[str, str] | None = None
    idempotent: bool
    user_scoped: bool = False
    scope: str | None = None
    lane: Lane
    deadline: float


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    body: bytes
    #: Time of the panel's ``Date`` header (its clock), for ``panel_state_ts`` (02 §5.6).
    date: datetime | None
    latency_ms: float


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now()).total_seconds()
    return min(max(seconds, 0.0), RETRY_AFTER_CAP)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


class Transport:
    """One panel connection profile = one session. Create inside a running event loop."""

    def __init__(
        self,
        config: TransportConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_breaker_change: BreakerHook | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        for secret in config.secrets():
            register_secret(secret)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
        self.breaker = PanelBreaker(
            threshold=config.breaker_threshold,
            window=config.breaker_window,
            cooldown=config.breaker_cooldown,
            max_cooldown=config.breaker_max_cooldown,
            trial_timeout=config.total_timeout * 2,
            clock=clock,
            on_change=on_breaker_change,
        )
        self._buckets = {
            Lane.INTERACTIVE: TokenBucket(config.interactive_rps, clock=clock),
            Lane.BACKGROUND: TokenBucket(config.background_rps, clock=clock),
        }
        self._background_slots = asyncio.Semaphore(max(1, config.connector_limit // 2))
        self._throttled_until = 0.0
        self._trial_task: asyncio.Task[None] | None = None
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._ssl: ssl.SSLContext | bool = bool(config.tls_verify)
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=config.connector_limit, keepalive_timeout=config.keepalive_timeout
            ),
            headers=config.headers(),
            cookie_jar=aiohttp.DummyCookieJar(),
            json_serialize=_no_json_serialize,
            raise_for_status=False,
            trust_env=False,
        )
        # Observability (health / «Состояние»).
        self.last_latency_ms: float | None = None
        self.last_ok_at: datetime | None = None
        self.last_error: RemnawaveError | None = None
        self.requests_total = 0

    # ---- lifecycle

    @property
    def closed(self) -> bool:
        """The session is closed: every further call fails with ``CLIENT_CLOSED``."""
        return self._session.closed

    @property
    def closing(self) -> bool:
        """Replaced by a hot swap: still finishing in-flight requests (late callers are still served)."""
        return self._closed

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def throttled_for(self) -> float:
        return max(0.0, self._throttled_until - self._clock())

    async def aclose(self, grace: float = 0.0) -> None:
        """Close the session after in-flight requests finished (waits at most ``grace`` seconds)."""
        self._closed = True
        if self._session.closed:
            return
        try:
            if grace > 0 and self._inflight:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(grace):
                        await self._idle.wait()
        finally:
            trial, self._trial_task = self._trial_task, None
            if trial is not None and not trial.done():
                trial.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await trial
            if not self._session.closed:
                await self._session.close()

    # ---- requests

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: bytes | None = None,
        params: Mapping[str, str] | None = None,
        idempotent: bool,
        user_scoped: bool = False,
        scope: str | None = None,
        lane: Lane = Lane.INTERACTIVE,
        budget: float | None = None,
    ) -> RawResponse:
        """Send one logical call (with retries when allowed). Raises :class:`RemnawaveError` on failure."""
        if self._session.closed:
            raise self._closed_error(method, path)
        call = _Call(
            method=method,
            path=path,
            json_body=json_body,
            params=params,
            idempotent=idempotent,
            user_scoped=user_scoped,
            scope=scope,
            lane=lane,
            deadline=self._clock() + (budget if budget is not None else self.config.total_timeout * 3),
        )
        self._inflight += 1
        self._idle.clear()
        try:
            await self._pass_breaker(call)
            if lane is Lane.BACKGROUND:
                await self._take_background_slot(call)
                try:
                    return await self._with_retries(call)
                finally:
                    self._background_slots.release()
            return await self._with_retries(call)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def _take_background_slot(self, call: _Call) -> None:
        """Wait for a background connection slot, but never past the call's deadline.

        A full lane is the bot's own congestion, not a panel failure: the error does not touch the breaker.
        """
        if not self._background_slots.locked():
            await self._background_slots.acquire()
            return
        try:
            async with asyncio.timeout(max(0.0, call.deadline - self._clock())):
                await self._background_slots.acquire()
        except TimeoutError:
            raise RemnawaveError(
                ErrorKind.TRANSIENT,
                None,
                "BUSY",
                "все фоновые соединения с панелью заняты",
                "Бот сейчас выполняет много фоновых запросов к панели (сверка, импорт); запрос будет "
                "повторён позже. Панель при этом может быть исправна.",
                method=call.method,
                path=call.path,
                retry_after=1.0,
            ) from None

    async def _with_retries(self, call: _Call) -> RawResponse:
        attempt = 0
        reconnected = False
        while True:
            attempt += 1
            await self._pace(call)
            try:
                resp = await self._send(call)
            except _Disconnected as exc:
                # A stale keep-alive connection looks exactly like ProxyCheck; one immediate fresh retry
                # for idempotent calls tells them apart (aiohttp already does it for GET/PUT/DELETE).
                if call.idempotent and not reconnected and call.method not in _AIOHTTP_RETRIES:
                    reconnected = True
                    attempt -= 1
                    continue
                err = exc.error
                self._account_failure(err)
                raise err from None
            except RemnawaveError as err:
                self._account_failure(err)
                if (
                    call.idempotent
                    and err.retryable
                    and attempt < self.config.max_attempts
                    and self.breaker.state is not BreakerState.OPEN
                    and not self._session.closed
                ):
                    delay = self._backoff(attempt, err.retry_after)
                    if self._clock() + delay < call.deadline:
                        log.info(
                            "remnawave %s %s: %s, retry %d in %.2fs",
                            call.method,
                            safe_path(call.path),
                            err.kind,
                            attempt,
                            delay,
                        )
                        await self._sleep(delay)
                        continue
                raise
            self.breaker.record_success()
            self.last_ok_at = now()
            self.last_latency_ms = resp.latency_ms
            return resp

    def _account_failure(self, err: RemnawaveError) -> None:
        if err.code == "CLIENT_CLOSED":
            return  # our own shutdown says nothing about the panel
        self.last_error = err
        self.breaker.record_failure(err.kind)

    @staticmethod
    def _closed_error(method: str, path: str) -> RemnawaveError:
        return RemnawaveError(
            ErrorKind.TRANSIENT,
            None,
            "CLIENT_CLOSED",
            "клиент панели закрыт (была перенастройка)",
            "Подключение к панели было перенастроено; операция будет повторена с новым подключением.",
            method=method,
            path=path,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = self.config.backoff_base * (2 ** (attempt - 1))
        delay = base * (0.75 + self._rng.random() * 0.5)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    async def _pace(self, call: _Call) -> None:
        """Shared 429 pause, then the lane's token bucket. Never sleeps past the deadline."""
        pause = self._throttled_until - self._clock()
        if pause > 0:
            if self._clock() + pause >= call.deadline:
                raise RemnawaveError(
                    ErrorKind.TRANSIENT,
                    429,
                    "THROTTLED",
                    f"панель просит подождать {pause:.0f} с (429)",
                    method=call.method,
                    path=call.path,
                    retry_after=pause,
                )
            await self._sleep(pause)
        bucket = self._buckets[call.lane]
        wait = bucket.reserve()
        if wait > 0:
            if self._clock() + wait >= call.deadline:
                bucket.refund()
                raise RemnawaveError(
                    ErrorKind.TRANSIENT,
                    None,
                    "RATE_LIMIT",
                    "собственный лимит запросов к панели исчерпан",
                    method=call.method,
                    path=call.path,
                    retry_after=wait,
                )
            await self._sleep(wait)

    async def _pass_breaker(self, call: _Call) -> None:
        breaker = self.breaker
        if breaker.state is BreakerState.CLOSED:
            return
        if not self.trial_running and not breaker.trial_due():
            raise PanelUnavailableError(method=call.method, path=call.path, retry_in=breaker.retry_in())
        await self.trial(call.deadline)
        if breaker.state is not BreakerState.CLOSED:
            raise PanelUnavailableError(method=call.method, path=call.path, retry_in=breaker.retry_in())

    def deadline(self, budget: float) -> float:
        """A deadline ``budget`` seconds from now on the transport's clock (for :meth:`trial`)."""
        return self._clock() + budget

    @property
    def trial_running(self) -> bool:
        task = self._trial_task
        return task is not None and not task.done()

    async def trial(self, deadline: float | None = None) -> bool:
        """HALF_OPEN trial ``GET /system/metadata`` (single flight). Returns True when the panel is back.

        The probe runs in its own task: the caller waits at most until ``deadline`` (the transport's
        monotonic clock) and may be cancelled at any moment without leaving the breaker in HALF_OPEN — the
        probe still finishes and records its verdict. Concurrent callers share one probe.
        """
        task = self._trial_task
        if task is None or task.done():
            breaker = self.breaker
            if breaker.state is BreakerState.CLOSED:
                return True
            if not breaker.trial_due() or self._session.closed:
                return False
            breaker.half_open()
            task = asyncio.get_running_loop().create_task(self._run_trial(), name="remnawave-breaker-trial")
            self._trial_task = task
        timeout = None if deadline is None else max(0.0, deadline - self._clock())
        if timeout is None or timeout > 0:
            # asyncio.wait never cancels the probe: neither on timeout nor when this caller is cancelled.
            await asyncio.wait({task}, timeout=timeout)
        return self.breaker.state is BreakerState.CLOSED

    async def _run_trial(self) -> None:
        """The probe itself. Any HTTP answer except auth / transient / proxy failures proves the panel is
        alive (a 403 on metadata only means the token lacks ``system:metadata``)."""
        probe = _Call(
            method="GET",
            path=_TRIAL_PATH,
            idempotent=True,
            scope="system:metadata",
            lane=Lane.INTERACTIVE,
            deadline=self._clock() + self.config.total_timeout,
        )
        try:
            try:
                await self._send(probe)
            except _Disconnected as exc:
                self._account_failure(exc.error)
                return
            except RemnawaveError as err:
                if err.kind in (ErrorKind.TRANSIENT, ErrorKind.PROXY_CHECK, ErrorKind.AUTH):
                    self._account_failure(err)
                    return
            except Exception:
                log.exception("remnawave: breaker trial failed unexpectedly")
                self.breaker.trip()
                return
            self.breaker.record_success()
            self.last_ok_at = now()
        finally:
            # Cancelled (shutdown), CLIENT_CLOSED or anything else that left no verdict: back to OPEN.
            self.breaker.abort_trial()

    async def _send(self, call: _Call) -> RawResponse:
        method, path = call.method, call.path
        if self._session.closed:
            raise self._closed_error(method, path)
        remaining = call.deadline - self._clock()
        if remaining <= 0:
            raise RemnawaveError(
                ErrorKind.TRANSIENT,
                None,
                "TIMEOUT",
                "время на запрос к панели истекло",
                method=method,
                path=path,
            )
        cfg = self.config
        timeout = aiohttp.ClientTimeout(
            total=min(cfg.total_timeout, remaining),
            connect=cfg.connect_timeout,
            sock_connect=cfg.connect_timeout,
            sock_read=cfg.read_timeout,
        )
        headers = {"Content-Type": "application/json"} if call.json_body is not None else None
        url = f"{cfg.base_url}/api{path}"
        started = self._clock()
        self.requests_total += 1
        try:
            async with self._session.request(
                method,
                url,
                data=call.json_body,
                params=call.params,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
                ssl=self._ssl,
            ) as resp:
                status = resp.status
                body = await _read_limited(resp, cfg.max_body)
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                date = _parse_date(resp.headers.get("Date"))
        except TimeoutError:
            raise RemnawaveError(
                ErrorKind.TRANSIENT, None, "TIMEOUT", "панель не ответила вовремя", method=method, path=path
            ) from None
        except aiohttp.ClientSSLError as exc:
            raise RemnawaveError(
                ErrorKind.PROXY_CHECK,
                None,
                "TLS",
                f"ошибка TLS: {type(exc).__name__}",
                "Сертификат панели не прошёл проверку. Проверьте домен и сертификат; отключать проверку "
                "(REMNAWAVE_TLS_VERIFY=false) допустимо только во внутренней сети.",
                method=method,
                path=path,
            ) from None
        except (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as exc:
            if isinstance(exc, aiohttp.ClientConnectorError):
                raise self._connect_error(exc, method, path) from None
            raise _Disconnected(self._proxy_check_error(method, path)) from None
        except aiohttp.ClientConnectorError as exc:
            raise self._connect_error(exc, method, path) from None
        except aiohttp.ClientError as exc:
            raise RemnawaveError(
                ErrorKind.TRANSIENT,
                None,
                "NETWORK",
                f"сетевая ошибка: {type(exc).__name__}",
                method=method,
                path=path,
            ) from None
        if body is None:
            raise RemnawaveError(
                ErrorKind.SERVER,
                status,
                "TOO_LARGE",
                "ответ панели слишком большой",
                method=method,
                path=path,
            )
        latency_ms = (self._clock() - started) * 1000.0
        if 200 <= status < 300:
            return RawResponse(status, body, date, latency_ms)
        if status == 429:
            pause = retry_after if retry_after is not None else 1.0
            self._throttled_until = max(self._throttled_until, self._clock() + pause)
            retry_after = pause
        raise error_from_response(
            status,
            body,
            user_scoped=call.user_scoped,
            method=method,
            path=path,
            scope=call.scope,
            retry_after=retry_after,
        )

    def _connect_error(self, exc: aiohttp.ClientConnectorError, method: str, path: str) -> RemnawaveError:
        return RemnawaveError(
            ErrorKind.TRANSIENT,
            None,
            "CONNECT",
            f"нет соединения с {self.config.host}: {type(exc.os_error).__name__}",
            "Бот не может подключиться к панели. Проверьте адрес (REMNAWAVE_URL), что контейнер панели "
            "запущен и что бот и панель в одной docker-сети (или панель доступна по домену).",
            method=method,
            path=path,
        )

    def _proxy_check_error(self, method: str, path: str) -> RemnawaveError:
        if self.config.is_http and not self.config.sends_forwarded:
            hint = (
                "Панель закрыла соединение без ответа: для адреса http:// нужны заголовки "
                "X-Forwarded-For и X-Forwarded-Proto. Включите их (автоматический режим) или используйте "
                "http://remnawave:3000 из docker-сети."
            )
        elif self.config.is_http:
            hint = None
        else:
            hint = (
                "Панель закрыла соединение без ответа. Похоже, reverse proxy не передаёт X-Forwarded-For и "
                "X-Forwarded-Proto: https, или панель открыта напрямую по порту. Используйте "
                "http://remnawave:3000 из docker-сети или проверьте настройки прокси."
            )
        return RemnawaveError(
            ErrorKind.PROXY_CHECK,
            None,
            "DISCONNECTED",
            "панель закрыла соединение без HTTP-ответа",
            hint,
            method=method,
            path=path,
        )


class _Disconnected(Exception):
    """Internal signal: the socket closed before an HTTP status (ProxyCheck or a stale connection)."""

    def __init__(self, error: RemnawaveError) -> None:
        super().__init__(str(error))
        self.error = error


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> bytes | None:
    """The (decompressed) body, or ``None`` as soon as it exceeds ``limit`` — never buffers more."""
    if resp.content_length is not None and resp.content_length > limit:
        return None
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(_READ_CHUNK):
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


def _no_json_serialize(_: Any) -> str:
    raise TypeError("encode request bodies with msgspec before calling the transport")
