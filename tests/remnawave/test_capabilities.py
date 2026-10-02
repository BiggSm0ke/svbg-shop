"""Version gate, JWT expiry warnings, scope probes, self-test (02 §1, §2.3)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.capabilities import (
    Support,
    gate_version,
    jwt_claims,
    parse_version,
    probe_scopes,
    self_test,
    token_expires_at,
    token_warning,
)
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.transport import Transport, TransportConfig
from tests.fakes.remnawave import FakeRemnawave, make_jwt

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3.4.4", (3, 4, 4)),
        ("v3.5.0-beta.1", (3, 5, 0)),
        ("3.4", (3, 4, 0)),
        ("4", (4, 0, 0)),
        ("", None),
        (None, None),
        ("dev", None),
    ],
)
def test_parse_version(raw: str | None, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(raw) == expected


@pytest.mark.parametrize(
    ("version", "support", "writes", "usable"),
    [
        ("2.8.0", Support.UNSUPPORTED, False, False),
        ("1.6.0", Support.UNSUPPORTED, False, False),
        ("3.0.0", Support.BEST_EFFORT, True, True),
        ("3.1.9", Support.BEST_EFFORT, True, True),
        ("3.2.0", Support.FULL, True, True),
        ("3.4.4", Support.FULL, True, True),
        ("3.4.99", Support.FULL, True, True),
        ("3.5.0", Support.NEWER_MINOR, True, True),
        ("3.12.1", Support.NEWER_MINOR, True, True),
        ("4.0.0", Support.UNVERIFIED_MAJOR, False, True),
        (None, Support.UNKNOWN, True, True),
    ],
)
def test_version_gate(version: str | None, support: Support, writes: bool, usable: bool) -> None:
    gate = gate_version(version)
    assert (gate.support, gate.writes_allowed, gate.usable) == (support, writes, usable)
    assert gate.message_ru


def test_new_major_unblocked_by_owner_confirmation() -> None:
    assert gate_version("4.1.0", confirmed_major=4).writes_allowed
    assert not gate_version("5.0.0", confirmed_major=4).writes_allowed  # a confirmation covers one major
    assert "2.x" not in gate_version("2.8.0").message_ru and "3.2" in gate_version("2.8.0").message_ru


def test_jwt_decode_without_verification() -> None:
    exp = int(NOW.timestamp()) + 86400
    token = make_jwt({"uuid": "u", "role": "API", "exp": exp})
    claims = jwt_claims(token)
    assert claims is not None and claims["role"] == "API"
    assert token_expires_at(token) == datetime.fromtimestamp(exp, UTC)
    assert token_expires_at(make_jwt({"uuid": "u"})) is None  # old tokens without exp do not expire
    garbage = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
    for bad in (
        None,
        "",
        "abc",
        "a.b",
        f"x.{garbage}.y",
        "x.!!!.y",
        "x." + base64.urlsafe_b64encode(b"[1]").decode() + ".y",
    ):
        assert token_expires_at(bad) is None
    assert token_expires_at(make_jwt({"exp": "soon"})) is None
    assert token_expires_at(make_jwt({"exp": True})) is None


@pytest.mark.parametrize(
    ("days_left", "level", "severity"),
    [
        (30, None, None),
        (14.5, None, None),
        (14, 14, "warn"),
        (10, 14, "warn"),
        (3, 3, "warn"),
        (2.5, 3, "warn"),
        (1, 1, "error"),
        (0.1, 1, "error"),
        (0, 0, "error"),
        (-5, 0, "error"),
    ],
)
def test_token_warning_thresholds(days_left: float, level: int | None, severity: str | None) -> None:
    warning = token_warning(NOW + timedelta(days=days_left), NOW)
    if level is None:
        assert warning is None
        return
    assert warning is not None
    assert (warning.level, warning.severity) == (level, severity)
    assert "Настройки → Remnawave" in warning.message_ru
    assert token_warning(None, NOW) is None


def test_token_warning_text_days() -> None:
    warning = token_warning(NOW + timedelta(days=2, hours=1), NOW)
    assert warning is not None and "через 3 дн." in warning.message_ru
    expired = token_warning(NOW - timedelta(hours=1), NOW)
    assert expired is not None and "истёк" in expired.message_ru


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        fake.add_internal_squad()
        yield fake


async def run_self_test(panel: FakeRemnawave, token: str, **kw: object):
    transport = Transport(TransportConfig(base_url=panel.url, token=token, max_attempts=1))
    try:
        api = RemnawaveApi(transport)
        return await self_test(api, token, **kw), api  # type: ignore[arg-type]
    finally:
        await transport.aclose()


async def test_probe_scopes_full_and_partial(panel: FakeRemnawave) -> None:
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
    try:
        checks = await probe_scopes(RemnawaveApi(transport))
    finally:
        await transport.aclose()
    assert all(c.ok for c in checks)
    # write does not include read: a token with users:write cannot list users
    token = panel.add_token(["users:write", "internal-squads:read", "system:metadata"])
    transport = Transport(TransportConfig(base_url=panel.url, token=token))
    try:
        checks = {c.scope: c for c in await probe_scopes(RemnawaveApi(transport))}
    finally:
        await transport.aclose()
    assert checks["users:stream"].ok is False and checks["users:stream"].required
    assert checks["users:resolve"].ok is False
    assert checks["internal-squads:list"].ok is True
    assert checks["nodes:list"].ok is False and not checks["nodes:list"].required


async def test_probe_scopes_propagates_auth(panel: FakeRemnawave) -> None:
    transport = Transport(TransportConfig(base_url=panel.url, token="bad"))
    try:
        with pytest.raises(RemnawaveError) as info:
            await probe_scopes(RemnawaveApi(transport))
    finally:
        await transport.aclose()
    assert info.value.kind is ErrorKind.AUTH


async def test_self_test_ok(panel: FakeRemnawave) -> None:
    token = panel.add_token(exp_days=200)
    report, api = await run_self_test(panel, token)
    assert report.ok, report.render()
    caps = report.capabilities
    assert caps is not None
    assert caps.gate.support is Support.FULL and api.gate is caps.gate
    assert caps.webhooks_enabled is True and caps.hwid_enabled is False
    assert caps.missing_scopes == [] and caps.scope_ok("users:stream") is True
    assert caps.token_expires_at is not None
    text = report.render()
    assert "✅ Панель 3.4.4 · совместимо" in text
    assert "API-токен действует до" in text


async def test_self_test_minimal_preset_without_optional_scopes(panel: FakeRemnawave) -> None:
    token = panel.add_token(["users:read", "internal-squads:read", "system:metadata"], exp_days=None)
    report, _ = await run_self_test(panel, token)
    assert report.ok
    assert report.capabilities is not None
    assert set(report.capabilities.missing_scopes) == {
        "external-squads:list",
        "nodes:list",
        "system:configuration",
        "subscription-settings:get",
    }
    assert report.capabilities.webhooks_enabled is None
    assert "⚠️" in report.render() and "бессрочный" in report.render()


async def test_self_test_missing_required_scope_is_fatal(panel: FakeRemnawave) -> None:
    report, _ = await run_self_test(panel, panel.add_token(["internal-squads:*", "system:*"]))
    assert not report.ok
    assert report.fatal is not None and "users:stream" in report.fatal
    assert report.fatal_hint is not None and "1 часа" in report.fatal_hint
    assert "❌" in report.render()


async def test_self_test_bad_token_and_unreachable(panel: FakeRemnawave) -> None:
    report, _ = await run_self_test(panel, "bad-token")
    assert not report.ok and report.fatal == "Панель отклонила API-токен"
    assert report.fatal_hint is not None and "API-токены" in report.fatal_hint
    async with FakeRemnawave() as gone:
        url = gone.url
    transport = Transport(TransportConfig(base_url=url, token="t", max_attempts=1))
    try:
        report2 = await self_test(RemnawaveApi(transport), "t")
    finally:
        await transport.aclose()
    assert not report2.ok and report2.fatal == "Панель недоступна"


async def test_self_test_old_panel_refused() -> None:
    async with FakeRemnawave(version="2.8.0") as panel:
        report, _ = await run_self_test(panel, panel.add_token())
    assert not report.ok
    assert report.fatal is not None and "не поддерживается" in report.fatal
    assert panel.calls("/users/stream") == []  # nothing else was probed


async def test_self_test_metadata_forbidden_works_by_probes(panel: FakeRemnawave) -> None:
    token = panel.add_token(["users:*", "internal-squads:*"])
    report, _ = await run_self_test(panel, token)
    assert report.ok
    assert report.capabilities is not None and report.capabilities.gate.support is Support.UNKNOWN
    assert "system:metadata" in report.render()


async def test_self_test_expired_token_is_fatal(panel: FakeRemnawave) -> None:
    exp = datetime(2026, 9, 1, tzinfo=UTC)
    token = make_jwt({"uuid": "u", "role": "API", "exp": int(exp.timestamp())})
    panel.tokens[token] = panel.tokens[panel.add_token()]  # the fake still accepts it
    report, _ = await run_self_test(panel, token, at=NOW)
    assert not report.ok and report.fatal is not None and "истёк" in report.fatal


async def test_self_test_newer_minor_warns(panel: FakeRemnawave) -> None:
    panel.version = "3.6.0"
    report, _ = await run_self_test(panel, panel.add_token())
    assert report.ok
    assert report.items[0].status == "warn"
    assert json.dumps([i.text for i in report.items], ensure_ascii=False).count("новее проверенной") == 1
