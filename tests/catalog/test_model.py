"""Pure catalog rules: availability, extra-device prices, snapshot format, validators."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from svbg.catalog.model import (
    CatalogError,
    DeviceAddon,
    Location,
    Plan,
    PlanPrice,
    is_available,
    pick_lang,
    slugify,
    validate_code,
    validate_name,
    validate_squads,
    validate_tag,
)
from svbg.subscriptions.service import Desired
from svbg.tg.ui.context import UserCtx
from tests.catalog.kit import SQ_DE, SQ_NL

NEWBIE = UserCtx(1, has_paid=False)
PAYER = UserCtx(2, has_paid=True)


def plan(**kw: object) -> Plan:
    base = Plan(
        id=7,
        code="std",
        name={"ru": "Стандарт", "en": "Standard"},
        enabled=True,
        squads=(SQ_NL,),
        device_limit=5,
        prices=(PlanPrice(7, 30, "RUB", 17900), PlanPrice(7, 90, "RUB", 49900, highlight=True)),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------- availability

AVAIL_CASES = [
    ("all", NEWBIE, None, True),
    ("all", PAYER, None, True),
    ("new", NEWBIE, None, True),
    ("new", PAYER, None, False),
    ("existing", NEWBIE, None, False),
    ("existing", PAYER, None, True),
    ("link", NEWBIE, None, False),
    ("link", PAYER, "std", True),
    ("link", NEWBIE, "other", False),
]


@pytest.mark.parametrize(
    ("availability", "user", "link", "expected"),
    AVAIL_CASES,
    ids=[f"{a}-{'paid' if u.has_paid else 'new'}-{lc}" for a, u, lc, _ in AVAIL_CASES],
)
def test_availability_matrix(availability: str, user: UserCtx, link: str | None, expected: bool) -> None:
    assert is_available(plan(availability=availability), user, currency="RUB", link_code=link) is expected


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"broken_reason": "В панели нет сквадов: NL"},
        {"is_trial": True},
        {"squads": ()},
        {"prices": ()},
    ],
    ids=["hidden", "broken", "trial", "no-squads", "no-prices"],
)
def test_not_for_sale(change: dict[str, object]) -> None:
    assert not is_available(plan(**change), PAYER, currency="RUB")


def test_prices_in_other_currency_do_not_count() -> None:
    p = plan(prices=(PlanPrice(7, 30, "USD", 300),))
    assert not is_available(p, PAYER, currency="RUB")
    assert is_available(p, PAYER, currency="USD")
    assert p.price(30, "USD") is not None and p.price(30, "RUB") is None
    assert p.min_price("USD") == PlanPrice(7, 30, "USD", 300)


# ------------------------------------------------------------------------------------------- device addon


def test_addon_price_owner_numbers() -> None:
    addon = DeviceAddon(price_minor=1900, per_days=30, max_devices=15)
    assert addon.price_for(1, 30) == 1900
    assert addon.price_for(2, 90) == 11400  # 19 ₽ × 2 × 3 months
    assert addon.price_for(1, 360) == 22800
    assert addon.price_for(1, 7) == 444  # 443.33… rounded up: never undercharge by a kopeck
    assert addon.price_for(0, 30) == 0
    assert addon.max_extra(5) == 10
    assert DeviceAddon(1900).max_extra(5) is None


@pytest.mark.parametrize(
    ("price", "per_days", "extra", "days", "expected"),
    [
        # a float quotient rounds these to a whole number (or below it) and ceil undercharges
        (999_999_999_999_999, 30, 1, 3650, 121_666_666_666_666_545),
        (1_000_000_000_000_000, 31, 1, 3650, 117_741_935_483_870_968),
        (1_000_000_000_000_000, 90, 1, 3650, 40_555_555_555_555_556),
        (999_999_999_999_999, 7, 1, 3650, 521_428_571_428_570_908),
        (999_999_999_999_999, 1, 100, 3650, 364_999_999_999_999_635_000),
    ],
)
def test_addon_price_is_exact_integer_ceil(
    price: int, per_days: int, extra: int, days: int, expected: int
) -> None:
    addon = DeviceAddon(price_minor=price, per_days=per_days)
    got = addon.price_for(extra, days)
    assert got == expected
    # the defining property: the smallest whole amount not below the exact prorated price
    assert got * per_days >= price * extra * days > (got - 1) * per_days


def test_addon_price_is_monotonic_and_never_below_exact_property() -> None:
    rnd = random.Random(20261001)
    for _ in range(500):
        addon = DeviceAddon(rnd.randint(1, 10**6), rnd.randint(1, 365))
        extra, days = rnd.randint(0, 50), rnd.randint(1, 3650)
        price = addon.price_for(extra, days)
        exact = addon.price_minor * extra * days / addon.per_days
        assert exact <= price < exact + 1
        assert addon.price_for(extra + 1, days) >= price
        assert addon.price_for(extra, min(days + 1, 3650)) >= price


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"price_minor": 0}, "больше нуля"),
        ({"price_minor": 100, "per_days": 0}, "Период"),
        ({"price_minor": 100, "max_devices": 0}, "Максимум"),
    ],
    ids=["zero-price", "zero-period", "zero-cap"],
)
def test_addon_validation(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(CatalogError, match=message):
        DeviceAddon(**kwargs)


def test_addon_rejects_bad_input_and_roundtrips_json() -> None:
    addon = DeviceAddon(1900, 30, 15, "RUB")
    with pytest.raises(CatalogError):
        addon.price_for(-1, 30)
    with pytest.raises(CatalogError):
        addon.price_for(1, 0)
    assert DeviceAddon.from_json(addon.to_json()) == addon
    assert DeviceAddon.from_json({}) is None
    assert DeviceAddon.from_json({"price_minor": "19"}) is None
    assert DeviceAddon.from_json({"price_minor": 0}) is None
    assert DeviceAddon.from_json("garbage") is None
    assert DeviceAddon.from_json({"price_minor": 500}, currency="USD") == DeviceAddon(500, 30, None, "USD")


def test_addon_offered_only_with_a_concrete_device_limit() -> None:
    addon = DeviceAddon(1900)
    assert plan(device_addon=addon, device_limit=5).addon == addon
    assert plan(device_addon=addon, device_limit=0).addon is None  # no limit: nothing to buy
    assert plan(device_addon=addon, device_limit=None).addon is None  # panel fallback: unknown base
    assert plan(device_addon=None).addon is None


# ------------------------------------------------------------------------------------------- snapshot


def test_snapshot_is_json_and_complete() -> None:
    p = plan(device_addon=DeviceAddon(1900, 30, 15), panel_tag="PAID", ext_squad="ext-1", version=4)
    snap = p.snapshot()
    assert json.loads(json.dumps(snap)) == snap
    assert snap["plan_id"] == 7 and snap["code"] == "std" and snap["version"] == 4 and snap["v"] == 1
    assert snap["squads"] == [SQ_NL] and snap["ext_squad"] == "ext-1" and snap["panel_tag"] == "PAID"
    assert snap["device_addon"] == {"price_minor": 1900, "per_days": 30, "max_devices": 15, "currency": "RUB"}
    assert snap["name"] == {"ru": "Стандарт", "en": "Standard"}


def test_desired_kwargs_build_a_valid_desired() -> None:
    p = plan(squads=(SQ_NL, SQ_DE), traffic_bytes=10 * 2**30, reset_strategy="MONTH", panel_tag="PAID")
    desired = Desired(expire_at=datetime.now(UTC) + timedelta(days=30), **p.desired_kwargs())
    assert list(desired.squads) == [SQ_NL, SQ_DE]
    assert desired.device_limit == 5 and desired.tag == "PAID" and desired.reset_strategy == "MONTH"


def test_titles_and_labels_fall_back() -> None:
    assert plan().title("en") == "Standard"
    assert plan().title("de") == "Стандарт"
    assert plan(name={}).title() == "std"
    assert pick_lang({}, "ru") == ""
    loc = Location(SQ_NL, title={"ru": "Нидерланды"}, flag="🇳🇱", panel_name="NL-1")
    assert loc.label("ru") == "🇳🇱 Нидерланды"
    assert Location(SQ_NL, panel_name="NL-1").label() == "NL-1"
    assert Location(SQ_NL).label() == SQ_NL[:8]
    assert not Location(SQ_NL, missing_since=datetime.now(UTC)).present


# ------------------------------------------------------------------------------------------- validators


@pytest.mark.parametrize(
    ("name", "slug"),
    [("Стандарт", "standart"), ("Pro 2025!", "pro_2025"), ("!!!", "plan"), ("Щука ёж", "schuka_ezh")],
    ids=["cyrillic", "latin", "symbols", "special"],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug
    assert validate_code(slug) == slug


def test_slugify_is_always_a_valid_code_property() -> None:
    rnd = random.Random(7)
    alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя ABCxyz0123456789-_!?.🇳🇱"
    for _ in range(500):
        name = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 80)))
        assert validate_code(slugify(name))


def test_validators() -> None:
    assert validate_name("  Стандарт   плюс ") == "Стандарт плюс"
    for bad in ("", "   ", "x" * 65, "a\x00b"):
        with pytest.raises(CatalogError):
            validate_name(bad)
    assert validate_tag("paid") == "PAID"
    assert validate_tag(None) is None and validate_tag("") is None
    with pytest.raises(CatalogError):
        validate_tag("bad tag")
    assert validate_squads([SQ_NL, SQ_NL, SQ_DE]) == (SQ_NL, SQ_DE)
    with pytest.raises(CatalogError, match="хотя бы один"):
        validate_squads([])
    with pytest.raises(CatalogError):
        validate_squads(["bad uuid!"])
    with pytest.raises(CatalogError):
        validate_code("Bad-Code")


def test_addon_price_matches_exact_fraction_ceil() -> None:
    from fractions import Fraction

    for price in (1, 7, 1900, 99_999, 10**15 - 1):
        for per_days in (1, 7, 30, 31, 365, 3650):
            addon = DeviceAddon(price_minor=price, per_days=per_days)
            for extra in (1, 3, 100):
                for days in (1, 7, 29, 90, 3650):
                    exact = Fraction(price * extra * days, per_days)
                    assert addon.price_for(extra, days) == -(-exact.numerator // exact.denominator)
