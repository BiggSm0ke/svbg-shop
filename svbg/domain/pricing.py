"""Order pricing (07 §4.5 step 1, 05 §3.3 X4): the price comes from the catalog, the result is a frozen
:class:`Quote` stored with the order (``orders.snapshot`` + ``order_items``).

* :func:`quote_plan` — a plan period (``new`` / ``renew`` / ``change``), optionally with paid extra devices
for
  the whole period (06 M1: 5 devices included, +N at the addon price, prorated by the period);
* :func:`quote_devices` — extra devices for the rest of the current period (``addon_devices``);
* discounts are a skeleton: :class:`Discount` objects (percent / fixed) applied in order to the subtotal,
  never below zero. Promo codes plug in here later.

Pure: works on the catalog's immutable objects (duck-typed, see :class:`PlanLike`), no SQL. Amounts are
integer minor units; prorating rounds **up** to a whole minor unit (the catalog's rule), percent discounts
round the discount **down** — the customer never pays a fraction of a kopeck less than the shop computes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from svbg.core.money import MAX_AMOUNT_MINOR

__all__ = [
    "ITEM_DEVICES",
    "ITEM_PLAN",
    "PURCHASE_KINDS",
    "AddonLike",
    "AppliedDiscount",
    "Discount",
    "FixedOff",
    "Line",
    "PercentOff",
    "PlanLike",
    "PricingError",
    "Quote",
    "apply_discounts",
    "days_left",
    "purchase_kind",
    "quote_devices",
    "quote_plan",
]

ITEM_PLAN: Final = "plan_period"
ITEM_DEVICES: Final = "devices"
#: Order kinds that buy something (``topup`` is not priced here).
PURCHASE_KINDS: Final = ("new", "renew", "change", "addon_devices")
MAX_DAYS: Final = 3650
DAY_S: Final = 86_400

Kind = Literal["new", "renew", "change", "addon_devices"]


class PricingError(ValueError):
    """The order cannot be priced; ``str(error)`` is a short Russian message for the user. A second argument
    (an old English translation) is accepted and ignored."""

    def __init__(self, text: str, _unused: str | None = None) -> None:
        super().__init__(text)


class AddonLike(Protocol):
    """The catalog's ``DeviceAddon``."""

    price_minor: int
    per_days: int
    max_devices: int | None
    currency: str

    def price_for(self, extra: int, days: int) -> int: ...


class PlanLike(Protocol):
    """The catalog's ``Plan`` (only what pricing reads)."""

    @property
    def id(self) -> int: ...
    @property
    def device_limit(self) -> int | None: ...
    @property
    def addon(self) -> AddonLike | None: ...

    def price(self, days: int, currency: str) -> Any: ...
    def title(self, lang: str | None = None) -> str: ...
    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Line:
    """One position of the order (``order_items`` row): ``type``, price and what it gives."""

    type: str
    amount_minor: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_row(self, position: int) -> dict[str, Any]:
        return {
            "position": position,
            "type": self.type,
            "amount_minor": self.amount_minor,
            "payload": dict(self.payload),
        }


class Discount(Protocol):
    """A discount source (promo code, personal offer…). ``amount_off`` gets the running subtotal."""

    @property
    def label(self) -> str: ...
    @property
    def source(self) -> str: ...

    def amount_off(self, subtotal_minor: int) -> int: ...


@dataclass(frozen=True, slots=True)
class PercentOff:
    percent: int
    label: str
    source: str = "promo"

    def __post_init__(self) -> None:
        if isinstance(self.percent, bool) or not 1 <= self.percent <= 100:
            raise PricingError("Скидка должна быть от 1 до 100 %")

    def amount_off(self, subtotal_minor: int) -> int:
        return subtotal_minor * self.percent // 100


@dataclass(frozen=True, slots=True)
class FixedOff:
    amount_minor: int
    label: str
    source: str = "promo"

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not 0 < self.amount_minor <= MAX_AMOUNT_MINOR:
            raise PricingError("Скидка должна быть больше нуля")

    def amount_off(self, subtotal_minor: int) -> int:
        return min(self.amount_minor, subtotal_minor)


@dataclass(frozen=True, slots=True)
class AppliedDiscount:
    label: str
    source: str
    amount_minor: int  # > 0: how much was taken off


def apply_discounts(
    subtotal_minor: int, discounts: Sequence[Discount]
) -> tuple[tuple[AppliedDiscount, ...], int]:
    """Apply ``discounts`` in order; each sees what is left after the previous ones; never below zero."""
    if subtotal_minor < 0:
        raise PricingError("Сумма заказа не может быть отрицательной")
    left = subtotal_minor
    applied: list[AppliedDiscount] = []
    for d in discounts:
        off = min(max(0, int(d.amount_off(left))), left)
        if off:
            applied.append(AppliedDiscount(d.label, d.source, off))
            left -= off
    return tuple(applied), left


@dataclass(frozen=True, slots=True)
class Quote:
    """A priced order, frozen into ``orders.snapshot`` (later catalog edits never change it)."""

    kind: Kind
    currency: str
    lines: tuple[Line, ...]
    discounts: tuple[AppliedDiscount, ...]
    subtotal_minor: int
    total_minor: int
    plan_id: int | None
    days: int
    extra_devices: int
    plan_snapshot: Mapping[str, Any]
    title: str
    subscription_id: int | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "v": 1,
            "kind": self.kind,
            "currency": self.currency,
            "title": self.title,
            "plan_id": self.plan_id,
            "days": self.days,
            "extra_devices": self.extra_devices,
            "subtotal_minor": self.subtotal_minor,
            "total_minor": self.total_minor,
            "discounts": [
                {"label": d.label, "source": d.source, "amount_minor": d.amount_minor} for d in self.discounts
            ],
            "plan": dict(self.plan_snapshot),
            "subscription_id": self.subscription_id,
        }

    def item_rows(self) -> list[dict[str, Any]]:
        return [line.as_row(i) for i, line in enumerate(self.lines)]


def purchase_kind(*, live_plan_id: int | None, live_is_trial: bool, has_live: bool, plan_id: int) -> Kind:
    """``new`` (no live subscription), ``renew`` (same paid plan) or ``change`` (another plan / from a trial;
    the subscription service keeps the trial remainder, 06 M2)."""
    if not has_live:
        return "new"
    if not live_is_trial and live_plan_id == plan_id:
        return "renew"
    return "change"


def _check_days(days: int) -> None:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise PricingError("Неверный срок")


def _total(subtotal: int, discounts: Sequence[Discount]) -> tuple[tuple[AppliedDiscount, ...], int]:
    if subtotal > MAX_AMOUNT_MINOR:
        raise PricingError("Слишком большая сумма заказа")
    return apply_discounts(subtotal, discounts)


def quote_plan(
    plan: PlanLike,
    *,
    days: int,
    currency: str,
    kind: Kind,
    extra_devices: int = 0,
    discounts: Sequence[Discount] = (),
    subscription_id: int | None = None,
) -> Quote:
    """Price ``days`` of ``plan`` (+ ``extra_devices`` paid devices for the whole period)."""
    _check_days(days)
    if kind not in ("new", "renew", "change"):
        raise PricingError("Неверный вид заказа")
    price = plan.price(days, currency)
    if price is None:
        raise PricingError("Этот срок сейчас не продаётся")
    lines = [Line(ITEM_PLAN, int(price.amount_minor), {"plan_id": plan.id, "days": days})]
    if isinstance(extra_devices, bool) or extra_devices < 0:
        raise PricingError("Число устройств не может быть отрицательным")
    if extra_devices:
        addon = plan.addon
        if addon is None:
            raise PricingError("Для этого тарифа докупка устройств недоступна")
        cap = None if addon.max_devices is None else max(0, addon.max_devices - (plan.device_limit or 0))
        if cap is not None and extra_devices > cap:
            raise PricingError(f"Можно не больше {addon.max_devices} устройств на подписку")
        lines.append(
            Line(ITEM_DEVICES, addon.price_for(extra_devices, days), {"count": extra_devices, "days": days})
        )
    subtotal = sum(line.amount_minor for line in lines)
    applied, total = _total(subtotal, discounts)
    return Quote(
        kind=kind,
        currency=currency,
        lines=tuple(lines),
        discounts=applied,
        subtotal_minor=subtotal,
        total_minor=total,
        plan_id=plan.id,
        days=days,
        extra_devices=extra_devices,
        plan_snapshot=plan.snapshot(),
        title=plan.title(),
        subscription_id=subscription_id,
    )


def days_left(seconds_left: float) -> int:
    """Whole days the addon is charged for: the rest of the period rounded up, at least one day."""
    return max(1, min(MAX_DAYS, math.ceil(max(0.0, seconds_left) / DAY_S)))


def quote_devices(
    addon: AddonLike | None,
    *,
    device_limit: int | None,
    current_extra: int,
    count: int,
    seconds_left: float,
    currency: str,
    plan_snapshot: Mapping[str, Any],
    subscription_id: int,
    title: str,
    discounts: Sequence[Discount] = (),
) -> Quote:
    """Price ``count`` more devices until the end of the current period (02 §4.6)."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise PricingError("Выберите число устройств")
    if addon is None or not device_limit:
        raise PricingError("Для этого тарифа докупка устройств недоступна")
    if seconds_left <= 0:
        raise PricingError("Подписка закончилась — сначала продлите её")
    if addon.max_devices is not None and device_limit + current_extra + count > addon.max_devices:
        raise PricingError(f"Можно не больше {addon.max_devices} устройств на подписку")
    days = days_left(seconds_left)
    line = Line(ITEM_DEVICES, addon.price_for(count, days), {"count": count, "days": days})
    applied, total = _total(line.amount_minor, discounts)
    return Quote(
        kind="addon_devices",
        currency=currency,
        lines=(line,),
        discounts=applied,
        subtotal_minor=line.amount_minor,
        total_minor=total,
        plan_id=plan_snapshot.get("plan_id") if isinstance(plan_snapshot.get("plan_id"), int) else None,
        days=days,
        extra_devices=count,
        plan_snapshot=dict(plan_snapshot),
        title=title,
        subscription_id=subscription_id,
    )
