"""Auto-maintenance («техработы») while the Remnawave panel is down (07 §5 stage 1, 02 §6.5).

The flag is for the **purchase paths** (stage 2): while it is on, new purchases, trials and plan changes
show a «техработы» notice instead of starting work that needs the panel. Payments already made are never
blocked — delivery is guaranteed by the panel outbox (02 §6.5) and happens after recovery.

``MAINTENANCE_MODE`` (setting, hot):

* ``off``  — never (the owner accepts that buyers see «готовим доступ…» during an outage);
* ``on``   — always (manual maintenance);
* ``auto`` — (default) on when the panel's circuit breaker has been OPEN (or re-trying in HALF_OPEN) for
  longer than :data:`AUTO_AFTER` (3 min), off as soon as the breaker closes again.

Every transition publishes ``maintenance.on`` / ``maintenance.off`` on the bus and keeps one «Требует
внимания» item in sync (``maintenance:auto`` / ``maintenance:manual``; the admin-chat relay posts it to
«⚙️ Система»). :attr:`MaintenanceService.active` is a plain attribute read — no I/O on the purchase hot path.

Evaluation is cheap (no SQL, no HTTP) and runs on a timer (every :data:`CHECK_EVERY` s, earlier when the
3-minute mark is due) and immediately on ``remnawave.breaker.*`` events and on ``MAINTENANCE_MODE`` changes.
While auto-maintenance is on, the timer also asks the panel component for its health, which runs the breaker's
HALF_OPEN trial — recovery is noticed even when nobody calls the panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from svbg.core.bus import Event, EventBus
from svbg.core.clock import now
from svbg.core.component import fix_screen, fix_setting
from svbg.core.errors.breaker import BreakerState

if TYPE_CHECKING:
    from svbg.core.errors import Capturer

log = logging.getLogger("svbg.services.maintenance")

__all__ = [
    "ATT_AUTO",
    "ATT_MANUAL",
    "AUTO_AFTER",
    "CHECK_EVERY",
    "EVENT_OFF",
    "EVENT_ON",
    "MODE_KEY",
    "MaintenanceService",
    "MaintenanceState",
    "Mode",
]

MODE_KEY: Final = "MAINTENANCE_MODE"
AUTO_AFTER: Final = 180.0  # s of an OPEN panel breaker before auto-maintenance
CHECK_EVERY: Final = 15.0  # s between evaluations
RECOVERY_PROBE_TIMEOUT: Final = 10.0  # s for the panel health check that runs the HALF_OPEN trial
EVENT_ON: Final = "maintenance.on"
EVENT_OFF: Final = "maintenance.off"
ATT_AUTO: Final = "maintenance:auto"
ATT_MANUAL: Final = "maintenance:manual"
BREAKER_EVENTS: Final = "remnawave.breaker.*"

Reason = Literal["manual", "auto"]

# Owner-facing texts (Russian) in one place.
_T: Final[dict[str, str]] = {
    "auto_title": "Техработы включены автоматически: панель Remnawave недоступна",
    "auto_body": (
        "Панель не отвечает с {since} (дольше {minutes:g} мин). Новые покупки, триалы и смена тарифа "
        "на паузе — покупатели видят «техработы». Уже оплаченные заказы выдадутся сами после "
        "восстановления панели. "
        "Техработы выключатся автоматически, когда панель ответит. Проверьте, что панель запущена и доступна "
        "боту («Состояние → Remnawave»)."
    ),
    "manual_title": "Техработы включены вручную",
    "manual_body": (
        "MAINTENANCE_MODE=on: новые покупки, триалы и смена тарифа на паузе. Чтобы открыть продажи, "
        "переключите настройку на auto или off."
    ),
    "off": "Техработы выключены",
    "off_mode": "Техработы выключены (MAINTENANCE_MODE=off)",
    "auto_idle": "Техработы: выключены (включатся сами, если панель недоступна дольше {minutes:g} мин)",
    "auto_wait": "Техработы: панель недоступна с {since}; включатся автоматически в {at}",
    "auto_on": "Техработы включены автоматически с {since}: панель недоступна",
    "manual_on": "Техработы включены вручную (MAINTENANCE_MODE=on)",
}


class Mode(enum.StrEnum):
    OFF = "off"
    ON = "on"
    AUTO = "auto"


def _hm(value: datetime) -> str:
    return value.strftime("%H:%M UTC")


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    """Result of one evaluation. ``since`` — when maintenance started (auto: when the panel went down)."""

    active: bool
    reason: Reason | None
    since: datetime | None
    mode: Mode
    #: When the panel breaker opened (also before the 3-minute mark), ``None`` while the panel is fine.
    panel_down_since: datetime | None = None
    auto_after: float = AUTO_AFTER

    @property
    def key(self) -> tuple[bool, Reason | None]:
        return self.active, self.reason

    def text(self) -> str:
        """One line for «Состояние» (Russian)."""
        minutes = self.auto_after / 60
        if self.active and self.reason == "manual":
            return _T["manual_on"]
        if self.active and self.since is not None:
            return _T["auto_on"].format(since=_hm(self.since))
        if self.mode is Mode.OFF:
            return _T["off_mode"]
        if self.panel_down_since is not None:
            at = self.panel_down_since + timedelta(seconds=self.auto_after)
            return _T["auto_wait"].format(since=_hm(self.panel_down_since), at=_hm(at))
        return _T["auto_idle"].format(minutes=minutes)


class _Panel(Protocol):
    """What maintenance needs from :class:`svbg.remnawave.RemnawaveComponent`."""

    @property
    def configured(self) -> bool: ...

    @property
    def breaker_state(self) -> BreakerState | None: ...

    @property
    def breaker_open_since(self) -> datetime | None: ...


class _Settings(Protocol):
    def current(self) -> Mapping[str, Any]: ...


class _Attention(Protocol):
    async def raise_item(
        self, dedup_key: str, severity: Any, title: str, body: str = "", fix_action: str | None = None
    ) -> Any: ...

    async def resolve(self, dedup_key: str) -> bool: ...


PanelSource = Callable[[], "_Panel | None"]


def parse_mode(value: Any) -> Mode:
    """``MAINTENANCE_MODE`` value → :class:`Mode` (unknown or missing → ``auto``)."""
    if isinstance(value, Mode):
        return value
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return Mode(value.strip().lower())
    return Mode.AUTO


class MaintenanceService:
    """See the module docstring. Use from one event loop; :meth:`start` / :meth:`stop` own the timer."""

    def __init__(
        self,
        *,
        settings: _Settings,
        panel: PanelSource,
        attention: _Attention | None = None,
        bus: EventBus | None = None,
        hub: Capturer | None = None,
        auto_after: float = AUTO_AFTER,
        interval: float = CHECK_EVERY,
        recovery_timeout: float = RECOVERY_PROBE_TIMEOUT,
        clock: Callable[[], datetime] = now,
    ) -> None:
        if auto_after < 0 or interval <= 0 or recovery_timeout <= 0:
            raise ValueError("auto_after must be >= 0, interval and recovery_timeout > 0")
        self._settings = settings
        self._panel = panel
        self._attention = attention
        self._bus = bus
        self._hub = hub
        self.auto_after = auto_after
        self.interval = interval
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._state = MaintenanceState(False, None, None, Mode.AUTO, auto_after=auto_after)
        self._synced: tuple[bool, Reason | None] | None = None  # attention items written for this state
        self._lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        self._bad_mode_logged = False

    # ------------------------------------------------------------------------------------- reading

    @property
    def state(self) -> MaintenanceState:
        """The last evaluated state (no I/O)."""
        return self._state

    @property
    def active(self) -> bool:
        """``True`` while purchase paths must show «техработы» (a plain attribute read)."""
        return self._state.active

    def mode(self) -> Mode:
        try:
            raw = self._settings.current().get(MODE_KEY, Mode.AUTO.value)
        except Exception:  # noqa: BLE001 - settings not loaded yet: fall back to the safe default
            return Mode.AUTO
        mode = parse_mode(raw)
        if raw is not None and mode.value != str(raw).strip().lower() and not self._bad_mode_logged:
            self._bad_mode_logged = True
            log.warning("maintenance: unknown %s=%r, using auto", MODE_KEY, raw)
        return mode

    def evaluate(self) -> MaintenanceState:
        """Compute the state from the settings and the panel breaker (pure, no I/O)."""
        mode = self.mode()
        down_since = self._down_since()
        if mode is Mode.ON:
            since = self._state.since if self._state.key == (True, "manual") else self._clock()
            return MaintenanceState(True, "manual", since, mode, down_since, self.auto_after)
        if (
            mode is Mode.AUTO
            and down_since is not None
            and (self._clock() - down_since).total_seconds() >= self.auto_after
        ):
            return MaintenanceState(True, "auto", down_since, mode, down_since, self.auto_after)
        return MaintenanceState(False, None, None, mode, down_since, self.auto_after)

    def _panel_now(self) -> _Panel | None:
        try:
            return self._panel()
        except Exception:  # a broken provider must not break the purchase flag
            log.exception("maintenance: panel provider failed")
            return None

    def _down_since(self) -> datetime | None:
        panel = self._panel_now()
        if panel is None or not panel.configured:
            return None
        state = panel.breaker_state
        if state is None or state is BreakerState.CLOSED:
            return None
        return panel.breaker_open_since or self._clock()

    # ------------------------------------------------------------------------------------ updating

    async def tick(self) -> MaintenanceState:
        """Re-evaluate; on a change publish the event and update «Требует внимания». Never raises."""
        async with self._lock:  # state only; events go out without the lock (a handler may call tick())
            old = self._state
            new = self.evaluate()
            self._state = new
        if new.key != old.key:
            log.warning(
                "maintenance: %s → %s",
                old.reason if old.active else "off",
                new.reason if new.active else "off",
            )
            await self._publish(old, new)
        async with self._sync_lock:
            current = self._state
            if self._synced != current.key:
                await self._sync_attention(current)
        return new

    async def _publish(self, old: MaintenanceState, new: MaintenanceState) -> None:
        bus = self._bus
        if bus is None:
            return
        ts = self._clock()
        if new.active:
            payload: dict[str, Any] = {
                "reason": new.reason,
                "since": new.since.isoformat() if new.since else None,
                "mode": new.mode.value,
            }
            event = Event(EVENT_ON, payload)
        else:
            duration = (ts - old.since).total_seconds() if old.since is not None else None
            event = Event(EVENT_OFF, {"reason": old.reason, "duration_s": duration, "mode": new.mode.value})
        try:
            await bus.publish(event)
        except Exception as exc:  # the flag itself is already switched
            log.exception("maintenance: publishing %s failed", event.name)
            await self._capture(exc, "maintenance:publish")

    async def _sync_attention(self, state: MaintenanceState) -> None:
        att = self._attention
        if att is None:
            self._synced = state.key
            return
        try:
            if state.active and state.reason == "auto":
                since = state.since or self._clock()
                await att.raise_item(
                    ATT_AUTO,
                    "error",
                    _T["auto_title"],
                    _T["auto_body"].format(since=_hm(since), minutes=self.auto_after / 60),
                    fix_screen("status"),
                )
                await att.resolve(ATT_MANUAL)
            elif state.active:
                await att.raise_item(
                    ATT_MANUAL, "warn", _T["manual_title"], _T["manual_body"], fix_setting(MODE_KEY)
                )
                await att.resolve(ATT_AUTO)
            else:
                await att.resolve(ATT_AUTO)
                await att.resolve(ATT_MANUAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            log.warning("maintenance: attention update failed (%s), will retry", type(exc).__name__)
            await self._capture(exc, "maintenance:attention")
            return
        self._synced = state.key

    async def _capture(self, exc: BaseException, place: str) -> None:
        hub = self._hub
        if hub is None:
            return
        try:
            await hub.capture(
                exc, place, module="maintenance", handled="флаг техработ работает, повтор позже"
            )
        except Exception:  # reporting must never break the service
            log.exception("maintenance: error hub capture failed")

    async def _probe_recovery(self) -> None:
        """While auto-maintenance is on, let the panel component run its HALF_OPEN trial."""
        panel = self._panel_now()
        health = getattr(panel, "health", None)
        if not callable(health):
            return
        try:
            async with asyncio.timeout(self.recovery_timeout):
                result = health()
                if isinstance(result, Awaitable):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failing check only means "still down"
            log.debug("maintenance: panel health check failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------------------ wiring / timer

    def install(self, bus: EventBus | None = None, settings: Any = None) -> None:
        """Subscribe to breaker events and to ``MAINTENANCE_MODE`` changes (idempotent per service)."""
        bus = bus or self._bus
        if bus is not None and not self._unsubscribe:
            self._unsubscribe.append(bus.subscribe(BREAKER_EVENTS, self._on_breaker))
        subscribe = getattr(settings, "subscribe", None)
        if callable(subscribe):
            try:
                subscribe([MODE_KEY], self._on_settings)
            except KeyError:
                log.warning(
                    "maintenance: %s is not in the settings registry; mode changes apply on the timer",
                    MODE_KEY,
                )

    async def _on_breaker(self, _event: Event) -> None:
        await self.tick()
        self._wake.set()

    async def _on_settings(self, _snap: Any, _keys: set[str]) -> None:
        await self.tick()
        self._wake.set()

    async def start(self) -> None:
        """First evaluation (also resolves stale items left by a previous process) and the timer."""
        if self._task is not None:
            return
        await self.tick()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="maintenance")

    async def stop(self) -> None:
        for off in self._unsubscribe:
            off()
        self._unsubscribe.clear()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _next_wait(self) -> float:
        wait = self.interval
        st = self._state
        if not st.active and st.mode is Mode.AUTO and st.panel_down_since is not None:
            due = st.panel_down_since + timedelta(seconds=self.auto_after)
            wait = min(wait, max(0.05, (due - self._clock()).total_seconds() + 0.05))
        return wait

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._next_wait()):
                    await self._wake.wait()
            try:
                if self._state.active and self._state.reason == "auto":
                    await self._probe_recovery()
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the timer must survive anything
                log.exception("maintenance: tick failed")
                await self._capture(exc, "maintenance:tick")


class _Deps(Protocol):
    @property
    def settings(self) -> Any: ...

    @property
    def components(self) -> Any: ...

    @property
    def attention(self) -> Any: ...

    @property
    def bus(self) -> EventBus: ...

    @property
    def hub(self) -> Any: ...


async def start_service(deps: _Deps, **kwargs: Any) -> MaintenanceService:
    """Build, wire and start the service for ``svbg.app`` (the caller registers :meth:`stop` on shutdown)."""
    components = deps.components

    def panel() -> _Panel | None:
        return components.find("remnawave")

    service = MaintenanceService(
        settings=deps.settings, panel=panel, attention=deps.attention, bus=deps.bus, hub=deps.hub, **kwargs
    )
    service.install(deps.bus, deps.settings)
    await service.start()
    return service
