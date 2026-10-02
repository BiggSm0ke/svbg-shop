"""Review fixes of the transport: plain HTTP to external hosts, bounded body reads, shortUuid never leaks,
the breaker trial survives cancellation, per-call deadlines while waiting for shared resources."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web

from svbg.core.errors.breaker import BreakerState
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.errors import (
    ErrorKind,
    PanelUnavailableError,
    RemnawaveError,
    error_from_response,
    safe_path,
)
from svbg.remnawave.transport import (
    Lane,
    PanelBreaker,
    Transport,
    TransportConfig,
    is_internal_host,
)
from tests.fakes.remnawave import FakeRemnawave

pytestmark = pytest.mark.timeout(60)

SHORT = "Zx9SecretShortUuid42"


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        yield fake


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., Transport]]:
    created: list[Transport] = []

    def factory(config: TransportConfig, **kw: Any) -> Transport:
        t = Transport(config, **kw)
        created.append(t)
        return t

    yield factory
    for t in created:
        await t.aclose()


# ------------------------------------------------------------------------------- #1 plain http


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:3000",
        "8.8.8.8:3000",
        "1.2.3.4",
        "http://panel.example.com",
        "http://panel.example.com:3000/api",
        "http://[2001:4860:4860::8888]:3000",
    ],
)
def test_plain_http_to_external_host_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match=r"https://") as info:
        TransportConfig(base_url=url, token="T")
    assert "REMNAWAVE_ALLOW_PLAIN_HTTP" in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "remnawave",
        "http://svbg-panel:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://10.0.0.5:3000",
        "http://172.18.0.3:3000",
        "http://192.168.1.10:3000",
        "http://[fd00::1]:3000",
        "http://[::1]:3000",
        "http://host.docker.internal:3000",
        "http://panel.lan:3000",
        "http://100.64.1.2:3000",
    ],
)
def test_plain_http_inside_docker_or_lan_keeps_forwarded_headers(url: str) -> None:
    cfg = TransportConfig(base_url=url, token="T")
    assert not cfg.plain_http_external
    headers = cfg.headers()
    assert headers["X-Forwarded-Proto"] == "https" and headers["X-Forwarded-For"] == "127.0.0.1"


def test_https_never_sends_forwarded_and_is_not_plain() -> None:
    cfg = TransportConfig(base_url="https://8.8.8.8", token="T")
    assert not cfg.plain_http_external
    assert "X-Forwarded-Proto" not in cfg.headers()


def test_plain_http_external_only_with_explicit_consent() -> None:
    cfg = TransportConfig(base_url="http://8.8.8.8:3000", token="T", allow_plain_http=True)
    assert cfg.plain_http_external
    built = TransportConfig.from_settings(
        {
            "REMNAWAVE_URL": "http://panel.example.com",
            "REMNAWAVE_TOKEN": "T",
            "REMNAWAVE_ALLOW_PLAIN_HTTP": True,
        }
    )
    assert built is not None and built.plain_http_external
    with pytest.raises(ValueError, match="https://"):
        TransportConfig.from_settings({"REMNAWAVE_URL": "http://panel.example.com", "REMNAWAVE_TOKEN": "T"})


@pytest.mark.parametrize(
    ("host", "internal"),
    [
        ("remnawave", True),
        ("REMNAWAVE.", True),
        ("::ffff:10.0.0.1", True),
        ("::ffff:8.8.8.8", False),
        ("169.254.1.1", True),
        ("example.com", False),
        ("8.8.8.8", False),
        ("", False),
    ],
)
def test_is_internal_host(host: str, internal: bool) -> None:
    assert is_internal_host(host) is internal


# ------------------------------------------------------------------------------- #2 body limit


async def _serve(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_get("/api/system/metadata", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, port


async def test_endless_chunked_body_is_cut_at_the_limit(make: Callable[..., Transport]) -> None:
    sent = 0

    async def endless(request: web.Request) -> web.StreamResponse:
        nonlocal sent
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        chunk = b"x" * 16384
        with contextlib.suppress(ConnectionError, asyncio.CancelledError):
            for _ in range(100_000):  # ~1.6 GB if the client read everything
                await resp.write(chunk)
                sent += len(chunk)
        return resp

    runner, port = await _serve(endless)
    try:
        transport = make(TransportConfig(base_url=f"http://127.0.0.1:{port}", token="T", max_body=256 * 1024))
        with pytest.raises(RemnawaveError) as info:
            await RemnawaveApi(transport).metadata()
        assert info.value.code == "TOO_LARGE" and info.value.kind is ErrorKind.SERVER
        assert sent < 64 * 1024 * 1024  # the client stopped reading long before the server ran out
    finally:
        await runner.cleanup()


async def test_declared_large_body_is_refused_without_reading(make: Callable[..., Transport]) -> None:
    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"{" + b" " * 300_000 + b"}", content_type="application/json")

    runner, port = await _serve(big)
    try:
        transport = make(TransportConfig(base_url=f"http://127.0.0.1:{port}", token="T", max_body=1024))
        with pytest.raises(RemnawaveError) as info:
            await RemnawaveApi(transport).metadata()
        assert info.value.code == "TOO_LARGE"
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------- #3 shortUuid secrecy


def test_safe_path_hides_access_keys() -> None:
    assert safe_path(f"/users/by-short-uuid/{SHORT}") == "/users/by-short-uuid/***"
    assert safe_path(f"/subscriptions/subpage-config/{SHORT}") == "/subscriptions/subpage-config/***"
    assert safe_path("/users/42/actions/enable") == "/users/42/actions/enable"
    assert safe_path(None) is None


def test_error_text_never_contains_short_uuid() -> None:
    err = RemnawaveError(
        ErrorKind.TRANSIENT, 503, None, "x", method="GET", path=f"/users/by-short-uuid/{SHORT}"
    )
    assert SHORT not in str(err) and SHORT not in (err.path or "")
    from_resp = error_from_response(
        404,
        b'{"errorCode":"A063","message":"User not found"}',
        user_scoped=True,
        method="GET",
        path=f"/subscriptions/subpage-config/{SHORT}",
    )
    assert SHORT not in str(from_resp) and from_resp.kind is ErrorKind.NOT_FOUND


async def test_retry_log_never_contains_short_uuid(
    panel: FakeRemnawave, make: Callable[..., Transport], caplog: pytest.LogCaptureFixture
) -> None:
    panel.inject("503", times=None)
    transport = make(TransportConfig(base_url=panel.url, token=panel.add_token(), backoff_base=0.01))
    caplog.set_level(logging.INFO, logger="svbg.remnawave")
    with pytest.raises(RemnawaveError) as info:
        await RemnawaveApi(transport).get_by_short_uuid(SHORT)
    assert "retry" in caplog.text
    assert SHORT not in caplog.text and SHORT not in str(info.value)


# ------------------------------------------------------------------------- #4 trial cancellation


def _tripped(panel: FakeRemnawave, make: Callable[..., Transport], **kw: Any) -> Transport:
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token(), breaker_cooldown=0.05, **kw)
    )
    transport.breaker.trip()
    return transport


async def test_cancelled_trial_does_not_leave_half_open(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    transport = _tripped(panel, make)
    await asyncio.sleep(0.06)
    panel.inject("latency", path="/system/metadata", delay=0.4)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(0.1):
            await transport.trial()
    assert transport.breaker.state is BreakerState.HALF_OPEN and transport.trial_running
    await asyncio.sleep(0.5)  # the probe was not cancelled with its caller: it finished and closed
    assert transport.breaker.state is BreakerState.CLOSED
    assert await RemnawaveApi(transport).nodes() == []


async def test_trial_cancelled_by_shutdown_goes_back_to_open(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    transport = _tripped(panel, make)
    await asyncio.sleep(0.06)
    panel.inject("latency", path="/system/metadata", delay=5.0)
    assert await transport.trial(transport.deadline(0.05)) is False
    assert transport.breaker.state is BreakerState.HALF_OPEN
    await transport.aclose()
    assert transport.breaker.state is BreakerState.OPEN


async def test_unexpected_trial_exception_reopens(
    panel: FakeRemnawave, make: Callable[..., Transport], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _tripped(panel, make)
    await asyncio.sleep(0.06)

    async def boom(_call: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(transport, "_send", boom)
    assert await transport.trial() is False
    assert transport.breaker.state is BreakerState.OPEN


async def test_concurrent_callers_share_one_trial_and_respect_their_deadline(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    transport = _tripped(panel, make)
    await asyncio.sleep(0.06)
    panel.inject("latency", path="/system/metadata", delay=1.0)
    started = time.monotonic()
    results = await asyncio.gather(
        *(transport.request("GET", "/nodes", idempotent=True, budget=0.2) for _ in range(5)),
        return_exceptions=True,
    )
    assert time.monotonic() - started < 0.8  # nobody waited for the 1 s probe
    assert all(isinstance(r, PanelUnavailableError) for r in results)
    assert len(panel.calls("/system/metadata")) == 1
    await asyncio.sleep(1.0)
    assert transport.breaker.state is BreakerState.CLOSED


def test_stale_half_open_is_due_for_a_new_trial() -> None:
    t = [0.0]
    br = PanelBreaker(clock=lambda: t[0], trial_timeout=40)
    br.trip()
    t[0] += 30
    br.half_open()
    assert not br.trial_due()
    t[0] += 40
    assert br.trial_due()
    br.half_open()  # a fresh trial may start
    assert br.state is BreakerState.HALF_OPEN and not br.trial_due()
    br.abort_trial()
    assert br.state is BreakerState.OPEN and br.retry_in() == pytest.approx(30)


# ------------------------------------------------------------------------- #5 shared resources


async def test_background_slot_wait_respects_deadline_and_spares_breaker(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    transport = make(TransportConfig(base_url=panel.url, token=panel.add_token(), connector_limit=2))
    panel.inject("latency", path="/nodes", delay=1.0)
    hog = asyncio.create_task(transport.request("GET", "/nodes", idempotent=True, lane=Lane.BACKGROUND))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    with pytest.raises(RemnawaveError) as info:
        await transport.request("GET", "/system/metadata", idempotent=True, lane=Lane.BACKGROUND, budget=0.2)
    assert time.monotonic() - started < 0.6
    assert info.value.code == "BUSY" and info.value.kind is ErrorKind.TRANSIENT
    assert transport.breaker.state is BreakerState.CLOSED and transport.last_error is None
    await hog
    await transport.request("GET", "/system/metadata", idempotent=True, lane=Lane.BACKGROUND, budget=0.5)


# ----------------------------------------------------------------------------- #6 inherit


def test_breaker_inherit_continues_the_outage() -> None:
    t = [100.0]
    old = PanelBreaker(clock=lambda: t[0])
    old.trip()
    t[0] += 40
    old.half_open()
    old.record_failure(ErrorKind.TRANSIENT)  # cooldown doubled to 60
    changes: list[BreakerState] = []
    new = PanelBreaker(clock=lambda: t[0], on_change=lambda _o, n: changes.append(n))
    new.inherit(old)
    assert new.state is BreakerState.OPEN and new.opened_at == old.opened_at and new.trips == old.trips == 2
    assert new.trial_due() and changes == []  # nothing new happened: no events
    new.half_open()
    new.record_failure(ErrorKind.TRANSIENT)
    assert new.retry_in() == pytest.approx(120)
    fresh = PanelBreaker()
    fresh.inherit(PanelBreaker())
    assert fresh.state is BreakerState.CLOSED and fresh.opened_at is None
