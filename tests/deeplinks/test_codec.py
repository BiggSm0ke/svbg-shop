"""The ``start`` parameter codec and the intent model (pure, no database)."""

from __future__ import annotations

import random
import string
from datetime import UTC, datetime

import pytest

from svbg.deeplinks import codec
from svbg.deeplinks.codec import Kind
from svbg.deeplinks.model import Intent, IntentError

# ------------------------------------------------------------------------------------------- parse


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("s_buy", Kind.SCREEN, "buy"),
        ("p_std", Kind.PLAN, "std"),
        ("p_12", Kind.PLAN, "12"),
        ("plan_std", Kind.PLAN, "std"),  # printed by the plan editor
        ("pr_AUTUMN", Kind.PROMO, "AUTUMN"),
        ("promo_AUTUMN-20", Kind.PROMO, "AUTUMN-20"),
        ("t_500", Kind.TOPUP, "500"),
        ("r_abc123", Kind.REF, "abc123"),
        ("ref_abc", Kind.REF, "abc"),
        ("a_tiktok", Kind.AD, "tiktok"),
        ("ad_tiktok", Kind.AD, "tiktok"),
        ("l_Ab3dE6gH", Kind.LINK, "Ab3dE6gH"),
        ("setup_xyz", Kind.SETUP, "xyz"),
        ("refA1b2C3d4", Kind.LEGACY_REF, "A1b2C3d4"),  # Bedolaga referral link
        ("summer2025", Kind.BARE, "summer2025"),  # Bedolaga campaign code (exact ad_links match later)
        ("  s_buy  ", Kind.SCREEN, "buy"),
    ],
)
def test_parse(raw: str, kind: Kind, value: str) -> None:
    parsed = codec.parse(raw)
    assert parsed is not None
    assert (parsed.kind, parsed.value, parsed.raw) == (kind, value, raw.strip())


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "s_",  # empty value
        "t_abc",
        "t_0",
        "t_007",
        "t_99999999999",
        "p_Std",  # plan codes are lowercase
        "x" * 65,
        "s_bad.dots",
        "привет",
        "s_buy space",
        "pr_" + "A" * 62,  # 65 characters in total
    ],
)
def test_parse_rejects(raw: str | None) -> None:
    assert codec.parse(raw) is None


def test_topup_upper_bound() -> None:
    assert codec.parse("t_10000000") is not None
    assert codec.parse("t_10000001") is None


def test_build_and_start_url() -> None:
    assert codec.build(Kind.PLAN, "std") == "p_std"
    assert codec.build(Kind.TOPUP, 500) == "t_500"
    assert codec.build(Kind.LINK, "abc") == "l_abc"
    with pytest.raises(ValueError, match="латиница"):
        codec.build(Kind.PROMO, "ОСЕНЬ")
    with pytest.raises(ValueError, match="не собрать"):
        codec.build(Kind.SETUP, "x")
    assert codec.start_url("@svbg_bot", "p_std") == "https://t.me/svbg_bot?start=p_std"
    assert codec.start_url(None, "p_std") is None
    assert codec.start_url("bot", "bad payload") is None


def test_every_built_payload_parses_back() -> None:
    rng = random.Random(7)
    alphabet = string.ascii_letters + string.digits + "_-"
    for _ in range(300):
        kind = rng.choice([Kind.PROMO, Kind.REF, Kind.AD, Kind.LINK])
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 62)))
        payload = codec.build(kind, value)
        assert len(payload) <= codec.PAYLOAD_MAX
        parsed = codec.parse(payload)
        assert parsed is not None
        # ``r_`` + "ef…" can never become a legacy ref: legacy refs have no underscore
        assert (parsed.kind, parsed.value) == (kind, value)


def test_new_link_code() -> None:
    codes = {codec.new_link_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(codec.valid_value(Kind.LINK, c) and len(c) == codec.LINK_CODE_LEN for c in codes)
    with pytest.raises(ValueError):
        codec.new_link_code(2)


# ------------------------------------------------------------------------------------------- intent


def test_intent_single_target() -> None:
    with pytest.raises(IntentError):
        Intent(screen="buy", plan="std")
    assert Intent(plan="std", promo="X").target == "plan"
    assert Intent(promo="X").actionable and not Intent(ad="x").actionable
    assert Intent().empty and not Intent(ref="r").empty


def test_spec_round_trip_and_validation() -> None:
    spec = Intent(topup=500, promo="AUTUMN", ad="tiktok", ref="abc")
    assert Intent.from_spec(spec.to_spec()) == spec
    for broken in (
        None,
        [],
        {"screen": 5},
        {"plan": "Bad Code"},
        {"topup": True},
        {"topup": 0},
        {"topup": "500"},
        {"promo": "с пробелом"},
        {"screen": "buy", "plan": "std"},
    ):
        with pytest.raises(IntentError):
            Intent.from_spec(broken)


def test_pending_round_trip() -> None:
    exp = datetime(2026, 10, 3, tzinfo=UTC)
    intent = Intent(plan="std", promo="AUTUMN", link_id=7, source="l_abc", expires_at=exp)
    assert Intent.from_pending(intent.to_pending()) == intent
    assert intent.expired(exp) and not intent.expired(datetime(2026, 10, 2, tzinfo=UTC))


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        {"kind": "form"},
        {"kind": "deeplink", "v": 99},
        {"kind": "deeplink", "v": 2, "plan": "Bad Code"},
        {"kind": "deeplink", "v": 2, "exp": "garbage"},
        {"kind": "deeplink", "v": 2, "exp": "2026-10-03T00:00:00"},  # naive
    ],
)
def test_pending_garbage_is_ignored(raw: object) -> None:
    assert Intent.from_pending(raw) is None


def test_pending_from_the_stage2_stub() -> None:
    old = {"kind": "deeplink", "v": 1, "type": "plan", "value": "std", "raw": "plan_std"}
    assert Intent.from_pending(old) == Intent(plan="std", source="plan_std")
    legacy = {"kind": "deeplink", "v": 1, "type": "legacy_ref", "value": "A1b2", "raw": "refA1b2C3"}
    assert Intent.from_pending(legacy) == Intent(ref="refA1b2C3", source="refA1b2C3")
    assert Intent.from_pending({"kind": "deeplink", "v": 1, "raw": 5}) is None


def test_direct_payload() -> None:
    assert Intent(plan="std").direct_payload() == "p_std"
    assert Intent(topup=300).direct_payload() == "t_300"
    assert Intent(promo="AUTUMN").direct_payload() == "pr_AUTUMN"
    assert Intent(plan="std", promo="AUTUMN").direct_payload() is None
    assert Intent().direct_payload() is None
