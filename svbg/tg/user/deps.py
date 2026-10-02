"""What the user path needs from the rest of the bot, as narrow ports (wired by the app).

Everything except the database, the settings and the screen router is optional: a missing part (payments not
configured yet, the panel not connected) hides or refuses the corresponding action with a human message
instead of breaking the menu.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from svbg.billing.receipts import Receipts
    from svbg.billing.service import Billing
    from svbg.content.store import ContentStore
    from svbg.db.engine import Database
    from svbg.payments.core import PaymentCore, PaymentRecord
    from svbg.subscriptions.channel import ChannelService
    from svbg.subscriptions.devices import SubscriptionActions
    from svbg.subscriptions.trial import TrialService
    from svbg.tg.ui.router import ScreenRouter
    from svbg.tg.user.directory import UserDirectory

__all__ = [
    "CatalogSource",
    "Config",
    "DeviceFetcher",
    "IPaidCheck",
    "PanelDevice",
    "UserPathDeps",
    "cfg_bool",
    "cfg_int",
    "cfg_int_list",
    "cfg_str",
]

Config = Callable[[], Mapping[str, Any]]


class CatalogSource(Protocol):
    """``CatalogService``: only the in-memory snapshot is read (no SQL on a click)."""

    @property
    def snapshot(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class PanelDevice:
    """A device as the devices screen shows it (``RemnawaveApi.devices`` → this)."""

    hwid: str
    platform: str | None = None
    os_version: str | None = None
    model: str | None = None
    created_at: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "hwid": self.hwid,
            "platform": self.platform,
            "os_version": self.os_version,
            "model": self.model,
            "created_at": self.created_at,
        }


#: ``panel_user_id`` → the panel's device list (one ``GET /hwid/devices/{id}``; runs in a job).
DeviceFetcher = Callable[[int], Awaitable[Sequence[PanelDevice]]]
#: «Я оплатил»: ``(payment_id, user_id)`` → the payment as it is now (``PaymentPoller.check_now``).
IPaidCheck = Callable[[str, int], Awaitable["PaymentRecord | None"]]


@dataclass
class UserPathDeps:
    db: Database
    config: Config
    screens: ScreenRouter
    users: UserDirectory
    content: ContentStore | None = None
    catalog: CatalogSource | None = None
    billing: Billing | None = None
    payments: PaymentCore | None = None
    trial: TrialService | None = None
    channel: ChannelService | None = None
    actions: SubscriptionActions | None = None
    receipts: Receipts | None = None
    i_paid: IPaidCheck | None = None
    fetch_devices: DeviceFetcher | None = None
    #: Extra kwargs for tests (e.g. timeouts); production leaves it empty.
    options: Mapping[str, Any] = field(default_factory=dict)


def _get(config: Config, key: str) -> Any:
    try:
        return config().get(key)
    except (RuntimeError, AttributeError):
        return None


def cfg_str(config: Config, key: str, default: str | None = None) -> str | None:
    value = _get(config, key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return str(value)


def cfg_int(config: Config, key: str, default: int) -> int:
    value = _get(config, key)
    if isinstance(value, bool):
        return default
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def cfg_bool(config: Config, key: str, default: bool) -> bool:
    value = _get(config, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "да")
    return default


def cfg_int_list(config: Config, key: str, default: Sequence[int]) -> list[int]:
    value = _get(config, key)
    if isinstance(value, str):
        value = [v for v in value.replace(";", ",").split(",") if v.strip()]
    if not isinstance(value, (list, tuple)):
        return list(default)
    out: list[int] = []
    for item in value:
        try:
            n = int(str(item).strip())
        except ValueError:
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out or list(default)
