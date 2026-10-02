"""Immutable catalog objects and pure rules (no SQL, no aiogram).

* :class:`Plan`, :class:`PlanPrice`, :class:`Location`, :class:`DeviceAddon` — what the in-memory snapshot
  holds; built once per reload from the rows, read on every click without touching the database;
* :func:`is_available` — the availability rule (``all | new | existing | link``);
* :meth:`Plan.snapshot` — the frozen description of a plan stored with an order / subscription
  (``plan_snapshot``): later edits of the plan never change what was bought;
* :meth:`DeviceAddon.price_for` — the price of extra devices for a period (integer minor units, rounded up);
* validators shared by the editor and the write functions (Russian messages for the admin).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from svbg.catalog.tables import AVAILABILITY, DEVICES_ON_RENEW, RESET_STRATEGIES, TRAFFIC_ON_RENEW
from svbg.core.money import MAX_AMOUNT_MINOR

__all__ = [
    "AVAILABILITY",
    "DEFAULT_LANG",
    "DEVICES_ON_RENEW",
    "GIB",
    "MAX_DAYS",
    "MAX_DEVICES",
    "RESET_STRATEGIES",
    "SNAPSHOT_VERSION",
    "TRAFFIC_ON_RENEW",
    "Audience",
    "CatalogError",
    "DeviceAddon",
    "Location",
    "Plan",
    "PlanPrice",
    "is_available",
    "pick_lang",
    "slugify",
    "validate_code",
    "validate_name",
    "validate_squads",
    "validate_tag",
]

DEFAULT_LANG: Final = "ru"
MAX_DAYS: Final = 3650
MAX_DEVICES: Final = 100
MAX_NAME: Final = 64
MAX_LOCATION_TITLE: Final = 48
MAX_SQUADS: Final = 32
GIB: Final = 1024**3
MAX_TRAFFIC_GB: Final = 1_000_000
#: Format version of :meth:`Plan.snapshot` (consumers may branch on it).
SNAPSHOT_VERSION: Final = 1

_CODE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_TAG_RE: Final = re.compile(r"^[A-Z0-9_]{1,16}$")
_UUID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")

_TRANSLIT: Final[Mapping[str, str]] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip


class CatalogError(ValueError):
    """A catalog rule was violated; ``str(error)`` is a short Russian message for the admin."""


class Audience(Protocol):
    """What the availability rule needs to know about a buyer (``UserCtx`` satisfies it)."""

    @property
    def has_paid(self) -> bool: ...


def pick_lang(values: Mapping[str, str], lang: str | None, default: str = DEFAULT_LANG) -> str:
    """The value for ``lang``, else the default language, else any; ``""`` for an empty mapping."""
    if lang and values.get(lang):
        return values[lang]
    if values.get(default):
        return values[default]
    return next((v for v in values.values() if v), "")


# ------------------------------------------------------------------------------------------- validators


def validate_name(value: str, *, limit: int = MAX_NAME) -> str:
    text = " ".join(str(value).split())
    if not text:
        raise CatalogError("Название не может быть пустым")
    if len(text) > limit:
        raise CatalogError(f"Название слишком длинное: максимум {limit} символов")
    if _CONTROL_RE.search(text):
        raise CatalogError("В названии недопустимые символы")
    return text


def validate_code(value: str) -> str:
    if not _CODE_RE.match(value):
        raise CatalogError("Код тарифа: латиница в нижнем регистре, цифры и «_», до 32 символов")
    return value


def validate_tag(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    tag = value.strip().upper()
    if not _TAG_RE.match(tag):
        raise CatalogError("Тег: A–Z, 0–9 и «_», до 16 символов")
    return tag


def validate_squads(squads: Sequence[str]) -> tuple[str, ...]:
    """Unique squad UUIDs in the given order; at least one (an empty set would cut users off every node)."""
    out = tuple(dict.fromkeys(str(s) for s in squads))
    if not out:
        raise CatalogError("Нужен хотя бы один сквад: без сквадов подписка не подключится ни к одной ноде")
    if len(out) > MAX_SQUADS:
        raise CatalogError(f"Слишком много сквадов: максимум {MAX_SQUADS}")
    for s in out:
        if not _UUID_RE.match(s):
            raise CatalogError("Неверный идентификатор сквада")
    return out


def slugify(name: str) -> str:
    """A plan code from a name: transliterated, lower case, ``[a-z0-9_]``, at most 32 characters."""
    out: list[str] = []
    for ch in name.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")[:32].strip("_")
    if not slug or not slug[0].isalnum():
        slug = "plan"
    return slug


# ------------------------------------------------------------------------------------------- objects


@dataclass(frozen=True, slots=True)
class DeviceAddon:
    """Paid extra devices: ``price_minor`` per extra device per ``per_days`` days, prorated by the period.

    ``max_devices`` caps the *total* number of devices (included + extra); ``None`` = no cap.
    """

    price_minor: int
    per_days: int = 30
    max_devices: int | None = None
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if isinstance(self.price_minor, bool) or not 0 < self.price_minor <= MAX_AMOUNT_MINOR:
            raise CatalogError("Цена доп. устройства должна быть больше нуля")
        if isinstance(self.per_days, bool) or not 1 <= self.per_days <= MAX_DAYS:
            raise CatalogError(f"Период цены устройства: от 1 до {MAX_DAYS} дней")
        if self.max_devices is not None and not 1 <= self.max_devices <= MAX_DEVICES:
            raise CatalogError(f"Максимум устройств: от 1 до {MAX_DEVICES}")

    @classmethod
    def from_json(cls, value: Any, *, currency: str = "RUB") -> DeviceAddon | None:
        """``None`` for ``{}`` / garbage (the option is then simply off)."""
        if not isinstance(value, Mapping) or not value:
            return None
        try:
            price = value["price_minor"]
            per_days = value.get("per_days", 30)
            cap = value.get("max_devices")
            if not isinstance(price, int) or not isinstance(per_days, int):
                return None
            if cap is not None and not isinstance(cap, int):
                return None
            return cls(int(price), int(per_days), cap, str(value.get("currency") or currency))
        except (KeyError, TypeError, ValueError):
            return None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "price_minor": self.price_minor,
            "per_days": self.per_days,
            "currency": self.currency,
        }
        if self.max_devices is not None:
            out["max_devices"] = self.max_devices
        return out

    def price_for(self, extra: int, days: int) -> int:
        """Price of ``extra`` additional devices for ``days`` days (rounded up to a whole minor unit)."""
        if isinstance(extra, bool) or extra < 0:
            raise CatalogError("Число устройств не может быть отрицательным")
        if isinstance(days, bool) or not 1 <= days <= MAX_DAYS:
            raise CatalogError(f"Период: от 1 до {MAX_DAYS} дней")
        # integer ceil division: a float quotient loses precision for large amounts and may undercharge
        return -(-(self.price_minor * extra * days) // self.per_days)

    def max_extra(self, included: int) -> int | None:
        """How many devices may be bought on top of ``included`` (``None`` = unlimited)."""
        if self.max_devices is None:
            return None
        return max(0, self.max_devices - included)


@dataclass(frozen=True, slots=True)
class PlanPrice:
    plan_id: int
    days: int
    currency: str
    amount_minor: int
    highlight: bool = False


@dataclass(frozen=True, slots=True)
class Location:
    squad_uuid: str
    title: Mapping[str, str] = field(default_factory=dict)
    flag: str | None = None
    sort: int = 0
    panel_name: str = ""
    members: int | None = None
    missing_since: datetime | None = None

    @property
    def present(self) -> bool:
        return self.missing_since is None

    def label(self, lang: str | None = None) -> str:
        name = pick_lang(self.title, lang) or self.panel_name or self.squad_uuid[:8]
        return f"{self.flag} {name}" if self.flag else name


@dataclass(frozen=True, slots=True)
class Plan:
    id: int
    code: str
    name: Mapping[str, str]
    availability: str = "all"
    is_trial: bool = False
    enabled: bool = False
    traffic_bytes: int = 0
    reset_strategy: str = "NO_RESET"
    device_limit: int | None = None
    squads: tuple[str, ...] = ()
    ext_squad: str | None = None
    panel_tag: str | None = None
    traffic_on_renew: str = "reset"
    devices_on_renew: str = "keep"
    device_addon: DeviceAddon | None = None
    broken_reason: str | None = None
    sort: int = 0
    version: int = 1
    prices: tuple[PlanPrice, ...] = ()

    def title(self, lang: str | None = None) -> str:
        return pick_lang(self.name, lang) or self.code

    @property
    def unlimited_traffic(self) -> bool:
        return self.traffic_bytes == 0

    @property
    def broken(self) -> bool:
        return self.broken_reason is not None

    def prices_in(self, currency: str) -> tuple[PlanPrice, ...]:
        return tuple(p for p in self.prices if p.currency == currency)

    def price(self, days: int, currency: str) -> PlanPrice | None:
        return next((p for p in self.prices if p.days == days and p.currency == currency), None)

    def min_price(self, currency: str) -> PlanPrice | None:
        own = self.prices_in(currency)
        return min(own, key=lambda p: p.amount_minor) if own else None

    @property
    def addon(self) -> DeviceAddon | None:
        """The extra-device option when it can be offered: a positive device limit (02 §4.6)."""
        if self.device_addon is None or self.device_limit is None or self.device_limit <= 0:
            return None
        return self.device_addon

    @property
    def sellable_shape(self) -> bool:
        """Enabled, not broken, not the trial, with squads."""
        return self.enabled and not self.broken and not self.is_trial and bool(self.squads)

    def desired_kwargs(self) -> dict[str, Any]:
        """Keyword arguments of :class:`svbg.subscriptions.service.Desired` except ``expire_at``."""
        return {
            "squads": list(self.squads),
            "traffic_bytes": self.traffic_bytes,
            "reset_strategy": self.reset_strategy,
            "device_limit": self.device_limit,
            "ext_squad": self.ext_squad,
            "tag": self.panel_tag,
        }

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe frozen description stored with orders and subscriptions (``plan_snapshot``)."""
        return {
            "v": SNAPSHOT_VERSION,
            "plan_id": self.id,
            "code": self.code,
            "version": self.version,
            "name": dict(self.name),
            "is_trial": self.is_trial,
            "traffic_bytes": self.traffic_bytes,
            "reset_strategy": self.reset_strategy,
            "device_limit": self.device_limit,
            "squads": list(self.squads),
            "ext_squad": self.ext_squad,
            "panel_tag": self.panel_tag,
            "traffic_on_renew": self.traffic_on_renew,
            "devices_on_renew": self.devices_on_renew,
            "device_addon": self.device_addon.to_json() if self.device_addon else None,
        }


def is_available(plan: Plan, user: Audience, *, currency: str, link_code: str | None = None) -> bool:
    """May ``user`` buy ``plan`` as a *new* purchase (the buy list, deep links)?

    ``all`` — everyone; ``new`` — users who never paid; ``existing`` — users who paid before; ``link`` —
    only when the user came through the plan's deep link (``link_code`` = the plan's code). The plan must be
    on sale (enabled, not broken, not the trial, has squads) and have a price in ``currency``.
    """
    if not plan.sellable_shape or not plan.prices_in(currency):
        return False
    match plan.availability:
        case "all":
            return True
        case "new":
            return not user.has_paid
        case "existing":
            return bool(user.has_paid)
        case "link":
            return link_code is not None and link_code == plan.code
    return False
