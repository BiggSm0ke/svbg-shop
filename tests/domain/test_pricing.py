from __future__ import annotations

from typing import Any

import pytest

from svbg.catalog.model import DeviceAddon, Plan
from svbg.catalog.service import build_snapshot
from svbg.domain.pricing import (
    ITEM_DEVICES,
    ITEM_PLAN,
    FixedOff,
    PercentOff,
    PricingError,
    apply_discounts,
    days_left,
    purchase_kind,
    quote_devices,
    quote_plan,
)
from tests.billing.kit import owner_plan_rows

SQUAD = "11111111-2222-3333-4444-555555555555"


def _plan(**overrides: Any) -> Plan:
    row, prices = owner_plan_rows(SQUAD, **overrides)
    snap = build_snapshot([row], prices, [], version=1)
    plan = snap.plan(1)
    assert plan is not None, snap.problems
    return plan


@pytest.mark.parametrize(
    ("days", "price"),
    [(30, 17_900), (90, 49_900), (180, 89_900), (360, 169_900)],
    ids=["30", "90", "180", "360"],
)
def test_owner_prices(days: int, price: int) -> None:
    q = quote_plan(_plan(), days=days, currency="RUB", kind="new")
    assert q.total_minor == q.subtotal_minor == price
    assert [line.type for line in q.lines] == [ITEM_PLAN]
    snap = q.snapshot()
    assert snap["plan"]["plan_id"] == 1 and snap["days"] == days and snap["total_minor"] == price
    assert q.item_rows() == [
        {"position": 0, "type": ITEM_PLAN, "amount_minor": price, "payload": {"plan_id": 1, "days": days}}
    ]


def test_extra_devices_are_priced_for_the_period() -> None:
    q = quote_plan(_plan(), days=90, currency="RUB", kind="renew", extra_devices=2)
    # 19 ₽ per device per 30 days → 2 devices × 90 days = 114 ₽
    assert [(line.type, line.amount_minor) for line in q.lines] == [
        (ITEM_PLAN, 49_900),
        (ITEM_DEVICES, 11_400),
    ]
    assert q.total_minor == 61_300 and q.extra_devices == 2


def test_device_cap_and_unavailable_addon() -> None:
    with pytest.raises(PricingError, match="15"):
        quote_plan(_plan(), days=30, currency="RUB", kind="new", extra_devices=11)  # 5 + 11 > 15
    quote_plan(_plan(), days=30, currency="RUB", kind="new", extra_devices=10)
    with pytest.raises(PricingError, match="недоступна"):
        quote_plan(_plan(device_addon={}), days=30, currency="RUB", kind="new", extra_devices=1)
    with pytest.raises(PricingError):
        quote_plan(_plan(), days=30, currency="RUB", kind="new", extra_devices=-1)


@pytest.mark.parametrize(
    ("days", "currency"), [(45, "RUB"), (30, "USD"), (0, "RUB")], ids=["period", "cur", "zero"]
)
def test_unsold_period(days: int, currency: str) -> None:
    with pytest.raises(PricingError):
        quote_plan(_plan(), days=days, currency=currency, kind="new")


def test_discounts_apply_in_order_and_never_below_zero() -> None:
    applied, total = apply_discounts(17_900, [PercentOff(10, "−10 %"), FixedOff(1_000, "−10 ₽")])
    assert [d.amount_minor for d in applied] == [1_790, 1_000]
    assert total == 15_110
    applied, total = apply_discounts(500, [FixedOff(1_000, "big")])
    assert total == 0 and applied[0].amount_minor == 500
    applied, total = apply_discounts(17_999, [PercentOff(100, "free")])
    assert total == 0
    applied, total = apply_discounts(33, [PercentOff(10, "small")])  # 3.3 → 3: the discount rounds down
    assert total == 30
    q = quote_plan(_plan(), days=30, currency="RUB", kind="new", discounts=[PercentOff(50, "half")])
    assert q.total_minor == 8_950 and q.snapshot()["discounts"][0]["amount_minor"] == 8_950


@pytest.mark.parametrize("bad", [0, 101, True], ids=["0", "101", "bool"])
def test_bad_percent(bad: int) -> None:
    with pytest.raises(PricingError):
        PercentOff(bad, "x")


def test_purchase_kind() -> None:
    assert purchase_kind(live_plan_id=None, live_is_trial=False, has_live=False, plan_id=1) == "new"
    assert purchase_kind(live_plan_id=1, live_is_trial=False, has_live=True, plan_id=1) == "renew"
    assert purchase_kind(live_plan_id=2, live_is_trial=False, has_live=True, plan_id=1) == "change"
    assert purchase_kind(live_plan_id=1, live_is_trial=True, has_live=True, plan_id=1) == "change"


def test_days_left_rounds_up() -> None:
    assert days_left(0) == 1
    assert days_left(1) == 1
    assert days_left(86_400) == 1
    assert days_left(86_401) == 2
    assert days_left(10**12) == 3650


def test_quote_devices_for_the_rest_of_the_period() -> None:
    addon = DeviceAddon(1_900, 30, 15)
    q = quote_devices(
        addon,
        device_limit=5,
        current_extra=2,
        count=3,
        seconds_left=10 * 86_400 + 5,
        currency="RUB",
        plan_snapshot={"plan_id": 1},
        subscription_id=7,
        title="Тариф",
    )
    assert q.kind == "addon_devices" and q.days == 11
    assert q.total_minor == 2_090  # ceil(1900 × 3 × 11 / 30)
    assert q.subscription_id == 7 and q.extra_devices == 3
    with pytest.raises(PricingError, match="15"):
        quote_devices(
            addon, device_limit=5, current_extra=8, count=3, seconds_left=100, currency="RUB",
            plan_snapshot={}, subscription_id=7, title="",
        )  # fmt: skip
    with pytest.raises(PricingError, match="продлите"):
        quote_devices(
            addon, device_limit=5, current_extra=0, count=1, seconds_left=0, currency="RUB",
            plan_snapshot={}, subscription_id=7, title="",
        )  # fmt: skip
    with pytest.raises(PricingError):
        quote_devices(
            None, device_limit=5, current_extra=0, count=1, seconds_left=100, currency="RUB",
            plan_snapshot={}, subscription_id=7, title="",
        )  # fmt: skip
    with pytest.raises(PricingError):
        quote_devices(
            addon, device_limit=5, current_extra=0, count=0, seconds_left=100, currency="RUB",
            plan_snapshot={}, subscription_id=7, title="",
        )  # fmt: skip
