"""``RemnawaveComponent``: probe / reconfigure / health of the panel connection (02 §2.7, 03 §5.3–5.4).

* :meth:`probe` builds a **separate** session for the candidate settings and runs the self-test (metadata,
  version gate, scope probes, token expiry) without touching the running client;
* :meth:`reconfigure` atomically swaps ``client`` to a new session; the old session is closed in the
  background once its in-flight requests finished (at most ``close_grace`` = 30 s), so nothing in flight is
  lost and the settings pipeline is not blocked;
* :meth:`health` reports version, latency, breaker state and token expiry for «Состояние» and «Требует
  внимания»; when the breaker is due for its HALF_OPEN trial, health runs it (so recovery is noticed even
  when nobody calls the panel).

Callers must take ``component.client`` per operation and never keep the API object across awaits of other
work: after a swap the old one is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from svbg.core.bus import Event, EventBus
from svbg.core.clock import now
from svbg.core.component import Health, HealthReport, ProbeError, fix_screen, fix_setting
from svbg.core.errors.breaker import BreakerState
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.capabilities import (
    Capabilities,
    SelfTestReport,
    Support,
    TokenWarning,
    gate_version,
    self_test,
    token_expires_at,
    token_warning,
)
from svbg.remnawave.errors import ErrorKind, PanelNotConfiguredError, RemnawaveError
from svbg.remnawave.transport import Lane, Transport, TransportConfig

log = logging.getLogger("svbg.remnawave")

__all__ = ["EVENT_BREAKER", "EVENT_TOKEN", "EVENT_VERSION", "RemnawaveComponent"]

EVENT_BREAKER: Final = "remnawave.breaker"  # + .opened / .half_open / .closed
EVENT_TOKEN: Final = "remnawave.token.expiring"  # noqa: S105 - an event name, not a secret
EVENT_VERSION: Final = "remnawave.version.detected"
PING_AFTER: Final = 60.0
PING_BUDGET: Final = 5.0
#: Codes of the bot's own congestion (not the panel's): a health ping that hit them proves nothing.
_LOCAL_BUSY: Final = frozenset({"BUSY", "RATE_LIMIT"})

_TXT = {
    "off": "Панель не подключена",
    "down_since": "Панель недоступна с {since}",
    "unsupported": "Версия панели не поддерживается",
    "token_expired": "API-токен панели истёк",
    "ok": "Панель {version} · {latency}",
    "ping_failed": "Панель не ответила на проверку",
    "probe_timeout": "Панель не ответила за {seconds:g} с",
    "probe_timeout_hint": "Проверьте адрес панели и что она запущена; из docker-сети используйте "
    "http://remnawave:3000.",
    "plain_http": "Соединение с панелью не шифруется (http:// на внешний адрес): API-токен виден в сети. "
    "Перейдите на https:// или адрес из docker-сети.",
}


@dataclass(slots=True)
class _Active:
    config: TransportConfig
    transport: Transport
    api: RemnawaveApi
    confirmed_major: int | None


def _confirmed_major(cfg: Mapping[str, Any]) -> int | None:
    value = cfg.get("REMNAWAVE_CONFIRMED_MAJOR")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RemnawaveComponent:
    """The panel connection as a runtime component (``name = "remnawave"``)."""

    name = "remnawave"

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        close_grace: float = 30.0,
        probe_timeout: float = 12.0,
        transport_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self._bus = bus
        self._close_grace = close_grace
        self._probe_timeout = probe_timeout
        self._overrides = dict(transport_overrides or {})
        self._active: _Active | None = None
        self._caps: Capabilities | None = None
        self._probe_cache: tuple[TransportConfig, Capabilities] | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing: set[Transport] = set()
        self._token_level: int | None = None
        self.last_report: SelfTestReport | None = None

    # ---------------------------------------------------------------------------------------- access

    @property
    def client(self) -> RemnawaveApi:
        """Current API client; raises :class:`PanelNotConfiguredError` when the panel is not connected."""
        active = self._active
        if active is None:
            raise PanelNotConfiguredError()
        return active.api

    @property
    def current(self) -> RemnawaveApi | None:
        active = self._active
        return None if active is None else active.api

    @property
    def configured(self) -> bool:
        return self._active is not None

    @property
    def capabilities(self) -> Capabilities | None:
        return self._caps

    @property
    def breaker_state(self) -> BreakerState | None:
        active = self._active
        return None if active is None else active.transport.breaker.state

    @property
    def breaker_open_since(self) -> datetime | None:
        """When the breaker opened (UTC) — for auto-maintenance «breaker OPEN > 3 min»."""
        active = self._active
        return None if active is None else active.transport.breaker.opened_at

    def token_warning(self, at: datetime | None = None) -> TokenWarning | None:
        active = self._active
        if active is None:
            return None
        return token_warning(token_expires_at(active.config.token), at)

    # ------------------------------------------------------------------------------------------ probe

    def _build_config(self, cfg: Mapping[str, Any]) -> TransportConfig | None:
        config = TransportConfig.from_settings(cfg)
        if config is not None and self._overrides:
            config = dataclasses.replace(config, **self._overrides)
        return config

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        """Self-test the candidate on its own session; the running client is untouched."""
        try:
            config = self._build_config(candidate)
        except ValueError as exc:
            text = str(exc)
            key = "REMNAWAVE_TOKEN" if "токен" in text and "адрес" not in text else "REMNAWAVE_URL"
            raise ProbeError(text[:1].upper() + text[1:], fix_action=fix_setting(key)) from None
        if config is None:
            return  # disconnecting the panel is always allowed
        confirmed = _confirmed_major(candidate)
        transport = Transport(config)
        try:
            api = RemnawaveApi(transport, confirmed_major=confirmed)
            try:
                async with asyncio.timeout(self._probe_timeout):
                    report = await self_test(api, config.token, confirmed_major=confirmed)
            except TimeoutError:
                raise ProbeError(
                    _TXT["probe_timeout"].format(seconds=self._probe_timeout),
                    _TXT["probe_timeout_hint"],
                    fix_action=fix_setting("REMNAWAVE_URL"),
                ) from None
        finally:
            await transport.aclose()
        self.last_report = report
        if not report.ok:
            raise ProbeError(
                report.fatal or "Проверка панели не прошла", report.fatal_hint, fix_action=_fix_for(report)
            )
        if report.capabilities is not None:
            self._probe_cache = (config, report.capabilities)

    # ------------------------------------------------------------------------------------ reconfigure

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        """Atomic swap to ``cfg`` (idempotent). Never waits for the panel or for the old session."""
        try:
            config = self._build_config(cfg)
        except ValueError as exc:
            raise RuntimeError(f"неверные настройки панели: {exc}") from None
        confirmed = _confirmed_major(cfg)
        async with self._lock:
            old = self._active
            if old is not None and old.config == config:
                if old.confirmed_major != confirmed:
                    old.confirmed_major = confirmed
                    old.api.confirmed_major = confirmed
                    if old.api.gate is not None:
                        old.api.gate = gate_version(old.api.gate.version, confirmed_major=confirmed)
                        if self._caps is not None:
                            self._caps = dataclasses.replace(self._caps, gate=old.api.gate)
                return
            if config is None:
                self._active = None
                self._caps = None
            else:
                caps = None
                if self._probe_cache is not None and self._probe_cache[0] == config:
                    caps = self._probe_cache[1]
                holder: list[Transport] = []
                transport = Transport(config, on_breaker_change=self._breaker_hook(holder))
                holder.append(transport)
                if old is not None and old.config.base_url == config.base_url:
                    # Same panel, new profile (rps, headers, token): an ongoing outage goes on, so
                    # auto-maintenance's «OPEN > 3 min» timer and the trip count are not reset.
                    transport.breaker.inherit(old.transport.breaker)
                gate = gate_version(caps.gate.version, confirmed_major=confirmed) if caps else None
                api = RemnawaveApi(transport, gate=gate, confirmed_major=confirmed)
                self._active = _Active(config, transport, api, confirmed)
                self._caps = dataclasses.replace(caps, gate=gate) if caps and gate else None
                self._token_level = None
                if caps is None:
                    self._spawn(self._refresh_quietly(), "remnawave-refresh")
            self._probe_cache = None
        if old is not None:
            self._closing.add(old.transport)
            self._spawn(self._close_later(old.transport), "remnawave-close-old")
        log.info("remnawave: client %s", "replaced" if config is not None else "disabled")

    async def _close_later(self, transport: Transport) -> None:
        try:
            await transport.aclose(grace=self._close_grace)
        finally:
            self._closing.discard(transport)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh()
        except RemnawaveError as err:
            log.warning("remnawave: capability refresh failed: %s", err)

    # ---------------------------------------------------------------------------------------- refresh

    async def refresh(self) -> Capabilities | None:
        """Re-read version and panel configuration (start, every 6 h, ``service.panel_started``).

        Scope probes are not repeated here (they run in :meth:`probe`); previously known ones are kept.
        """
        active = self._active
        if active is None:
            return None
        api = active.api
        version: str | None
        try:
            version = (await api.metadata(lane=Lane.BACKGROUND)).version
        except RemnawaveError as err:
            if err.kind is not ErrorKind.FORBIDDEN_SCOPE:
                raise
            version = None
        gate = gate_version(version, confirmed_major=active.confirmed_major)
        previous = api.gate
        api.gate = gate
        config = None
        hwid = None
        with contextlib.suppress(RemnawaveError):
            config = await api.configuration(lane=Lane.BACKGROUND)
        with contextlib.suppress(RemnawaveError):
            hwid = (await api.subscription_settings(lane=Lane.BACKGROUND)).hwid_enabled
        old_caps = self._caps
        caps = Capabilities(
            gate,
            old_caps.scopes if old_caps else (),
            config if config is not None else (old_caps.config if old_caps else None),
            hwid if hwid is not None else (old_caps.hwid_enabled if old_caps else None),
            token_expires_at(active.config.token),
        )
        if self._active is active:
            self._caps = caps
        if previous is None or previous.version != gate.version:
            await self._publish(
                EVENT_VERSION,
                {"version": gate.version, "support": gate.support.value, "message": gate.message_ru},
            )
        await self._check_token()
        return caps

    async def _check_token(self) -> None:
        warning = self.token_warning()
        if warning is None:
            self._token_level = None
            return
        if self._token_level is not None and warning.level >= self._token_level:
            return
        self._token_level = warning.level
        await self._publish(
            EVENT_TOKEN,
            {
                "level": warning.level,
                "days_left": round(warning.days_left, 2),
                "expires_at": warning.expires_at.isoformat(),
                "severity": warning.severity,
                "message": warning.message_ru,
            },
        )

    async def check(self) -> SelfTestReport | None:
        """«Проверить» for the running connection: the full self-test, refreshing capabilities and scopes."""
        active = self._active
        if active is None:
            return None
        report = await self_test(active.api, active.config.token, confirmed_major=active.confirmed_major)
        self.last_report = report
        if report.capabilities is not None and self._active is active:
            self._caps = report.capabilities
        await self._check_token()
        return report

    # ----------------------------------------------------------------------------------------- health

    async def health(self) -> HealthReport:
        active = self._active
        if active is None:
            return HealthReport(Health.DISABLED, _TXT["off"], {}, fix_action=fix_setting("REMNAWAVE_URL"))
        transport = active.transport
        breaker = transport.breaker
        if breaker.trial_due() or transport.trial_running:
            # Bounded wait; the probe itself survives the health_all timeout cancelling us.
            await transport.trial(transport.deadline(PING_BUDGET))
        details: dict[str, Any] = {
            "host": active.config.host,
            "breaker": breaker.state.value,
            "inflight": transport.inflight,
        }
        if breaker.state is not BreakerState.CLOSED:
            since = breaker.opened_at or now()
            details["opened_at"] = since.isoformat()
            details["retry_in_s"] = round(breaker.retry_in(), 1)
            err = transport.last_error
            if err is not None:
                details["last_error"] = f"{err.kind.value} {err.code or ''}".strip()
            return HealthReport(
                Health.DOWN,
                _TXT["down_since"].format(since=since.strftime("%H:%M UTC")),
                details,
                fix_action=_fix_for_error(err),
            )
        last_ok = transport.last_ok_at
        if last_ok is None or (now() - last_ok).total_seconds() > PING_AFTER:
            try:
                meta = await active.api.metadata(lane=Lane.BACKGROUND, budget=PING_BUDGET)
                if active.api.gate is None or active.api.gate.version != meta.version:
                    active.api.gate = gate_version(meta.version, confirmed_major=active.confirmed_major)
            except RemnawaveError as err:
                if err.kind is ErrorKind.FORBIDDEN_SCOPE:
                    if active.api.gate is None:
                        active.api.gate = gate_version(None, confirmed_major=active.confirmed_major)
                elif err.code in _LOCAL_BUSY:
                    details["ping"] = "skipped: bot busy"
                else:
                    details["last_error"] = f"{err.kind.value} {err.code or ''}".strip()
                    status = (
                        Health.DOWN
                        if err.kind in (ErrorKind.AUTH, ErrorKind.PROXY_CHECK)
                        else Health.DEGRADED
                    )
                    return HealthReport(
                        status,
                        f"{_TXT['ping_failed']}: {err.message}",
                        details,
                        fix_action=_fix_for_error(err),
                    )
        gate = active.api.gate
        latency = transport.last_latency_ms
        details["latency_ms"] = None if latency is None else round(latency, 1)
        details["throttled_s"] = round(transport.throttled_for, 1)
        status = Health.OK
        notes: list[str] = []
        fix: str | None = None
        if active.config.plain_http_external:
            details["plain_http"] = True
            notes.append(_TXT["plain_http"])
            status = Health.DEGRADED
            fix = fix_setting("REMNAWAVE_URL")
        if gate is not None:
            details["version"] = gate.version
            details["support"] = gate.support.value
            details["writes_allowed"] = gate.writes_allowed
            if not gate.usable:
                return HealthReport(
                    Health.DOWN, gate.message_ru, details, fix_action=fix_setting("REMNAWAVE_URL")
                )
            if gate.support is Support.UNVERIFIED_MAJOR and not gate.writes_allowed:
                status = Health.DEGRADED
                fix = fix_screen("status")
            if gate.support is not Support.FULL:
                notes.append(gate.message_ru)
        warning = self.token_warning()
        expires = token_expires_at(active.config.token)
        details["token_expires_at"] = None if expires is None else expires.isoformat()
        if warning is not None:
            notes.append(warning.message_ru)
            if warning.level == 0:
                return HealthReport(
                    Health.DOWN, _TXT["token_expired"], details, fix_action=fix_setting("REMNAWAVE_TOKEN")
                )
            if warning.level <= 3:
                status = Health.DEGRADED
                fix = fix_setting("REMNAWAVE_TOKEN")
        caps = self._caps
        if caps is not None:
            details["webhooks"] = caps.webhooks_enabled
            details["hwid"] = caps.hwid_enabled
            if caps.missing_scopes:
                details["missing_scopes"] = list(caps.missing_scopes)
        version = gate.version if gate is not None and gate.version else "?"
        summary = _TXT["ok"].format(version=version, latency="—" if latency is None else f"{latency:.0f} мс")
        if notes:
            summary = summary + ". " + " ".join(notes)
        return HealthReport(status, summary, details, fix_action=fix)

    # ------------------------------------------------------------------------------------------- misc

    def _breaker_hook(self, holder: list[Transport]) -> Any:
        """Breaker transitions of the *current* session → bus events (old sessions are ignored)."""

        async def hook(old: BreakerState, new: BreakerState) -> None:
            active = self._active
            if active is None or not holder or active.transport is not holder[0]:
                return
            suffix = {
                BreakerState.OPEN: "opened",
                BreakerState.HALF_OPEN: "half_open",
                BreakerState.CLOSED: "closed",
            }
            opened = active.transport.breaker.opened_at
            await self._publish(
                f"{EVENT_BREAKER}.{suffix[new]}",
                {"old": old.value, "new": new.value, "opened_at": opened.isoformat() if opened else None},
            )

        return hook

    async def _publish(self, name: str, payload: Mapping[str, Any]) -> None:
        bus = self._bus
        if bus is None:
            return
        try:
            await bus.publish(Event(name, dict(payload)))
        except Exception:
            log.exception("remnawave: publishing %s failed", name)

    async def aclose(self, grace: float = 5.0) -> None:
        """Shutdown: close the current session (after ``grace``) and everything still closing."""
        async with self._lock:
            active, self._active = self._active, None
            self._caps = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if active is not None:
            await active.transport.aclose(grace=grace)
        for transport in list(self._closing):
            await transport.aclose()


def _fix_for_error(err: RemnawaveError | None) -> str | None:
    if err is None:
        return fix_screen("status")
    if err.kind is ErrorKind.AUTH:
        return fix_setting("REMNAWAVE_TOKEN")
    if err.kind is ErrorKind.PROXY_CHECK:
        return fix_setting("REMNAWAVE_URL")
    return fix_screen("status")


def _fix_for(report: SelfTestReport) -> str:
    text = (report.fatal or "").lower()
    if "токен" in text or "прав" in text:
        return fix_setting("REMNAWAVE_TOKEN")
    return fix_setting("REMNAWAVE_URL")
