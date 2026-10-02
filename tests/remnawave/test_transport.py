"""Transport: headers, timeouts, retries, 429 pause, token buckets, breaker, ProxyCheck (02 §2.4–2.5)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from svbg.core.errors.breaker import BreakerState
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.errors import ErrorKind, PanelUnavailableError, RemnawaveError
from svbg.remnawave.transport import (
    USER_AGENT,
    Lane,
    PanelBreaker,
    TokenBucket,
    Transport,
    TransportConfig,
    normalize_url,
)
from tests.fakes.remnawave import FakeRemnawave


class FakeTime:
    """Monotonic clock + sleep that only moves the clock (retries and pauses run instantly)."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


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


# --------------------------------------------------------------------------------------------- config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("panel.example.com", "https://panel.example.com"),
        ("https://panel.example.com/", "https://panel.example.com"),
        ("https://panel.example.com/api/", "https://panel.example.com"),
        ("https://example.com/panel/api", "https://example.com/panel"),
        ("remnawave", "http://remnawave:3000"),
        ("remnawave:3001", "http://remnawave:3001"),
        ("10.0.0.5", "http://10.0.0.5:3000"),
        ("http://remnawave:3000", "http://remnawave:3000"),
        ("  HTTP://Remnawave:3000  ", "http://Remnawave:3000"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "ftp://x.example.com", "https://", "https://x.example.com/?a=1", "https://u:p@x.example.com"]
)
def test_normalize_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_from_settings() -> None:
    assert TransportConfig.from_settings({}) is None
    assert TransportConfig.from_settings({"REMNAWAVE_URL": None, "REMNAWAVE_TOKEN": "  "}) is None
    with pytest.raises(ValueError, match="токен"):
        TransportConfig.from_settings({"REMNAWAVE_URL": "https://p.example.com"})
    with pytest.raises(ValueError, match="адрес"):
        TransportConfig.from_settings({"REMNAWAVE_TOKEN": "t"})
    cfg = TransportConfig.from_settings(
        {
            "REMNAWAVE_URL": "remnawave",
            "REMNAWAVE_TOKEN": "tok",
            "REMNAWAVE_COOKIE": "sid=abc",
            "REMNAWAVE_TLS_VERIFY": False,
            "REMNAWAVE_RPS_INTERACTIVE": 7,
        }
    )
    assert cfg is not None
    assert cfg.base_url == "http://remnawave:3000"
    assert cfg.tls_verify is False
    assert cfg.interactive_rps == 7
    assert "tok" not in repr(cfg) and "sid=abc" not in repr(cfg)


def test_headers_by_profile() -> None:
    http = TransportConfig(base_url="http://remnawave:3000", token="T").headers()
    assert http["Authorization"] == "Bearer T"
    assert http["User-Agent"] == USER_AGENT and USER_AGENT.startswith("SvBG-Shop/")
    assert http["X-Forwarded-For"] == "127.0.0.1" and http["X-Forwarded-Proto"] == "https"
    https = TransportConfig(base_url="https://panel.example.com", token="T").headers()
    assert "X-Forwarded-For" not in https and "X-Forwarded-Proto" not in https
    assert not {"X-Api-Key", "CF-Access-Client-Id", "Cookie"} & set(https)
    full = TransportConfig(
        base_url="https://panel.example.com",
        token="T",
        caddy_token="Basic dXNlcjpwYXNz",
        cf_client_id="cid.access",
        cf_client_secret="csecret",
        cookie="secretname=secretvalue",
    ).headers()
    assert full["X-Api-Key"] == "Basic dXNlcjpwYXNz"
    assert full["CF-Access-Client-Id"] == "cid.access" and full["CF-Access-Client-Secret"] == "csecret"
    assert full["Cookie"] == "secretname=secretvalue"
    forced = TransportConfig(base_url="http://remnawave:3000", token="T", forwarded_headers=False).headers()
    assert "X-Forwarded-For" not in forced


async def test_headers_reach_the_panel(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    token = panel.add_token()
    cfg = TransportConfig(
        base_url=panel.url,
        token=token,
        caddy_token="ck",
        cf_client_id="id",
        cf_client_secret="sec",
        cookie="a=b",
    )
    api = RemnawaveApi(make(cfg))
    await api.metadata()
    req = panel.calls("/system/metadata")[-1]
    assert req.headers["Authorization"] == f"Bearer {token}"
    assert req.headers["X-Forwarded-For"] == "127.0.0.1"
    assert req.headers["X-Forwarded-Proto"] == "https"
    assert req.headers["X-Api-Key"] == "ck"
    assert req.headers["CF-Access-Client-Id"] == "id"
    assert req.headers["CF-Access-Client-Secret"] == "sec"
    assert req.headers["Cookie"] == "a=b"
    assert req.headers["User-Agent"] == USER_AGENT


# ------------------------------------------------------------------------------------- ProxyCheck


async def test_production_panel_works_with_automatic_forwarded_headers(
    make: Callable[..., Transport],
) -> None:
    async with FakeRemnawave(production=True) as panel:
        api = RemnawaveApi(make(TransportConfig(base_url=panel.url, token=panel.add_token())))
        assert (await api.metadata()).version == "3.4.4"


async def test_production_panel_without_forwarded_headers_is_proxy_check(
    make: Callable[..., Transport],
) -> None:
    async with FakeRemnawave(production=True) as panel:
        cfg = TransportConfig(base_url=panel.url, token=panel.add_token(), forwarded_headers=False)
        api = RemnawaveApi(make(cfg))
        with pytest.raises(RemnawaveError) as info:
            await api.metadata()
        err = info.value
        assert err.kind is ErrorKind.PROXY_CHECK
        assert "X-Forwarded" in err.hint_ru and "http://remnawave:3000" in err.hint_ru
        # Idempotent GET: one immediate fresh retry (stale keep-alive), then the ProxyCheck verdict.
        assert len(panel.calls("/system/metadata")) == 2


async def test_https_disconnect_hint_mentions_reverse_proxy() -> None:
    cfg = TransportConfig(base_url="https://panel.example.com", token="T")
    transport = Transport(cfg)
    try:
        err = transport._proxy_check_error("GET", "/x")
    finally:
        await transport.aclose()
    assert err.kind is ErrorKind.PROXY_CHECK
    assert "reverse proxy" in err.hint_ru


async def test_single_disconnect_on_idempotent_call_is_survived(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    api = RemnawaveApi(make(TransportConfig(base_url=panel.url, token=panel.add_token())))
    panel.inject("disconnect", path="/system/metadata", times=1)
    assert (await api.metadata()).version == "3.4.4"
    assert len(panel.calls("/system/metadata")) == 2


async def test_disconnect_on_non_idempotent_call_is_not_repeated(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    api = RemnawaveApi(make(TransportConfig(base_url=panel.url, token=panel.add_token())))
    panel.inject("disconnect", path="/users", method="POST", times=1)
    from datetime import UTC, datetime, timedelta

    with pytest.raises(RemnawaveError) as info:
        await api.create_user(username="sv_1", expire_at=datetime.now(UTC) + timedelta(days=1))
    assert info.value.kind is ErrorKind.PROXY_CHECK
    assert len(panel.calls("/users", "POST")) == 1
    assert panel.users == {}


# ----------------------------------------------------------------------------------------- retries


async def test_idempotent_call_retries_transient_with_backoff(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    api = RemnawaveApi(
        make(TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep)
    )
    user = panel.add_user()
    panel.inject("503", path=f"/users/{user['id']}", times=2)
    assert (await api.get_user(user["id"])).id == user["id"]
    assert len(panel.calls(f"/users/{user['id']}")) == 3
    backoffs = [s for s in clock.sleeps if s > 0.1]
    assert len(backoffs) == 2
    assert 0.375 <= backoffs[0] <= 0.625 and 0.75 <= backoffs[1] <= 1.25  # 0.5 → 1 s with jitter


async def test_retries_give_up_after_three_attempts(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    api = RemnawaveApi(
        make(TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep)
    )
    panel.inject("502", path="/system/metadata", times=None)
    with pytest.raises(RemnawaveError) as info:
        await api.metadata()
    assert info.value.kind is ErrorKind.TRANSIENT and info.value.status == 502
    assert len(panel.calls("/system/metadata")) == 3


@pytest.mark.parametrize("fault", ["500", "503"])
async def test_non_idempotent_calls_are_never_retried(
    panel: FakeRemnawave, make: Callable[..., Transport], fault: Any
) -> None:
    clock = FakeTime()
    api = RemnawaveApi(
        make(TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep)
    )
    user = panel.add_user()
    panel.inject(fault, path=f"/users/{user['id']}/actions/reset-traffic", times=None)
    with pytest.raises(RemnawaveError):
        await api.reset_traffic(user["id"])
    assert len(panel.calls(f"/users/{user['id']}/actions/reset-traffic")) == 1
    panel.inject(fault, path=f"/users/{user['id']}/actions/revoke", times=None)
    with pytest.raises(RemnawaveError):
        await api.revoke(user["id"])
    assert len(panel.calls(f"/users/{user['id']}/actions/revoke")) == 1


async def test_server_errors_are_not_retried_even_for_get(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    api = RemnawaveApi(make(TransportConfig(base_url=panel.url, token=panel.add_token())))
    panel.inject("500", path="/nodes", times=None)
    with pytest.raises(RemnawaveError) as info:
        await api.nodes()
    assert info.value.kind is ErrorKind.SERVER
    assert len(panel.calls("/nodes")) == 1


async def test_timeout_is_transient(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    api = RemnawaveApi(make(TransportConfig(base_url=panel.url, token=panel.add_token(), total_timeout=0.3)))
    user = panel.add_user()
    panel.inject("latency", path=f"/users/{user['id']}", delay=1.0, times=None)
    with pytest.raises(RemnawaveError) as info:
        await api.get_user(user["id"], budget=0.5)
    assert info.value.kind is ErrorKind.TRANSIENT
    assert info.value.code == "TIMEOUT"


async def test_connection_refused_is_transient(make: Callable[..., Transport]) -> None:
    async with FakeRemnawave() as fake:
        url = fake.url
    api = RemnawaveApi(make(TransportConfig(base_url=url, token="T", max_attempts=1)))
    with pytest.raises(RemnawaveError) as info:
        await api.metadata()
    assert info.value.kind is ErrorKind.TRANSIENT
    assert info.value.code == "CONNECT"
    assert "REMNAWAVE_URL" in info.value.hint_ru


# --------------------------------------------------------------------------------------------- 429


async def test_429_sets_a_shared_pause(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    clock = FakeTime()
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep
    )
    api = RemnawaveApi(transport)
    panel.inject("429", path="/system/metadata", times=1, retry_after="10")
    assert (await api.metadata()).version == "3.4.4"
    assert 10.0 in clock.sleeps  # Retry-After honoured
    # Every other caller waits for the shared pause too, instead of hammering the panel.
    panel.inject("429", path="/nodes", times=1, retry_after="5")
    clock.sleeps.clear()
    results = await asyncio.gather(api.nodes(), api.internal_squads(), api.external_squads())
    assert [type(r) for r in results] == [list, list, list]
    assert len(panel.calls("/nodes")) == 2
    assert clock.sleeps.count(5.0) >= 1


async def test_429_pause_longer_than_budget_fails_fast(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    api = RemnawaveApi(
        make(TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep)
    )
    panel.inject("429", path="/nodes", times=1, retry_after="120")
    with pytest.raises(RemnawaveError) as info:
        await api.nodes()
    assert info.value.kind is ErrorKind.TRANSIENT
    before = len(panel.requests)
    with pytest.raises(RemnawaveError) as info2:
        await api.metadata(lane=Lane.INTERACTIVE)
    assert info2.value.code == "THROTTLED"
    assert len(panel.requests) == before  # nothing was sent during the pause


# ------------------------------------------------------------------------------------ token buckets


def test_token_bucket_rates() -> None:
    clock = FakeTime()
    bucket = TokenBucket(5, clock=clock)
    waits = [bucket.reserve() for _ in range(10)]
    assert waits[:5] == [0.0] * 5
    assert waits[5:] == pytest.approx([0.2, 0.4, 0.6, 0.8, 1.0])
    clock.t += 10
    assert bucket.reserve() == 0.0
    bucket.refund()
    with pytest.raises(ValueError):
        TokenBucket(0)


async def test_lanes_have_their_own_rates(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    clock = FakeTime()
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token(), interactive_rps=20, background_rps=5),
        clock=clock,
        sleep=clock.sleep,
    )
    api = RemnawaveApi(transport)
    for _ in range(10):
        await api.metadata(lane=Lane.BACKGROUND)
    background_wait = sum(clock.sleeps)
    assert background_wait == pytest.approx(1.0)  # 10 requests at 5 rps with a burst of 5
    clock.sleeps.clear()
    for _ in range(20):
        await api.metadata(lane=Lane.INTERACTIVE)
    assert sum(clock.sleeps) == 0.0  # the interactive bucket is untouched by background work


# ----------------------------------------------------------------------------------------- breaker


def test_breaker_state_machine() -> None:
    clock = FakeTime()
    changes: list[tuple[BreakerState, BreakerState]] = []
    br = PanelBreaker(clock=clock, on_change=lambda o, n: changes.append((o, n)))
    for _ in range(4):
        br.record_failure(ErrorKind.TRANSIENT)
    assert br.state is BreakerState.CLOSED
    br.record_failure(ErrorKind.PROXY_CHECK)
    assert br.state is BreakerState.OPEN and br.opened_at is not None
    assert not br.trial_due() and br.retry_in() == pytest.approx(30)
    clock.t += 30
    assert br.trial_due()
    br.half_open()
    assert br.state is BreakerState.HALF_OPEN
    br.record_failure(ErrorKind.TRANSIENT)  # failed trial: OPEN again with a doubled cooldown
    assert br.state is BreakerState.OPEN and br.retry_in() == pytest.approx(60)
    clock.t += 60
    br.half_open()
    br.record_success()
    assert br.state is BreakerState.CLOSED and br.opened_at is None
    assert [n for _, n in changes] == [
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
        BreakerState.CLOSED,
    ]


def test_breaker_window_success_and_auth() -> None:
    clock = FakeTime()
    br = PanelBreaker(clock=clock)
    for _ in range(4):
        br.record_failure(ErrorKind.TRANSIENT)
    br.record_failure(ErrorKind.VALIDATION)  # the panel answered: failures are not consecutive any more
    for _ in range(4):
        br.record_failure(ErrorKind.TRANSIENT)
    assert br.state is BreakerState.CLOSED
    br.record_success()
    for _ in range(5):
        br.record_failure(ErrorKind.TRANSIENT)
        clock.t += 10  # 5 failures spread over 40 s: outside the 30 s window
    assert br.state is BreakerState.CLOSED
    br.record_failure(ErrorKind.AUTH)  # 401 opens at once
    assert br.state is BreakerState.OPEN


def test_breaker_cooldown_is_capped() -> None:
    clock = FakeTime()
    br = PanelBreaker(clock=clock, cooldown=30, max_cooldown=300)
    br.trip()
    for _ in range(10):
        clock.t += br.retry_in()
        br.half_open()
        br.record_failure(ErrorKind.TRANSIENT)
    assert br.retry_in() == pytest.approx(300)


async def test_breaker_opens_blocks_and_recovers(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    changes: list[BreakerState] = []
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token(), max_attempts=1),
        clock=clock,
        sleep=clock.sleep,
        on_breaker_change=lambda _o, n: changes.append(n),
    )
    api = RemnawaveApi(transport)
    panel.inject("503", times=5)
    for _ in range(5):
        with pytest.raises(RemnawaveError):
            await api.nodes()
    assert transport.breaker.state is BreakerState.OPEN
    sent = len(panel.requests)
    with pytest.raises(PanelUnavailableError) as info:
        await api.nodes()
    assert len(panel.requests) == sent  # nothing reached the network
    assert info.value.retry_after == pytest.approx(30)
    clock.t += 31
    nodes = await api.nodes()  # HALF_OPEN trial (GET /system/metadata) succeeds → CLOSED → the call goes
    assert nodes == []
    assert [r.path for r in panel.requests[sent:]] == ["/system/metadata", "/nodes"]
    assert changes == [BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED]


async def test_breaker_failed_trial_doubles_cooldown(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token()), clock=clock, sleep=clock.sleep
    )
    transport.breaker.trip()
    clock.t += 31
    panel.inject("502", path="/system/metadata", times=1)
    with pytest.raises(PanelUnavailableError):
        await RemnawaveApi(transport).nodes()
    assert transport.breaker.state is BreakerState.OPEN
    assert transport.breaker.retry_in() == pytest.approx(60)
    assert panel.calls("/nodes") == []


async def test_breaker_opens_at_once_on_401(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    transport = make(TransportConfig(base_url=panel.url, token="not-a-valid-token"))
    with pytest.raises(RemnawaveError) as info:
        await RemnawaveApi(transport).metadata()
    assert info.value.kind is ErrorKind.AUTH
    assert transport.breaker.state is BreakerState.OPEN
    assert len(panel.calls("/system/metadata")) == 1  # 401 is never retried


async def test_breaker_trial_403_counts_as_alive(
    panel: FakeRemnawave, make: Callable[..., Transport]
) -> None:
    clock = FakeTime()
    transport = make(
        TransportConfig(base_url=panel.url, token=panel.add_token(["users:read"])),
        clock=clock,
        sleep=clock.sleep,
    )
    transport.breaker.trip()
    clock.t += 31
    assert await transport.trial() is True
    assert transport.breaker.state is BreakerState.CLOSED


async def test_async_breaker_hook(panel: FakeRemnawave, make: Callable[..., Transport]) -> None:
    seen: list[str] = []

    async def hook(old: BreakerState, new: BreakerState) -> None:
        seen.append(f"{old.value}->{new.value}")

    transport = make(TransportConfig(base_url=panel.url, token=panel.add_token()), on_breaker_change=hook)
    transport.breaker.trip()
    await asyncio.sleep(0.01)
    assert seen == ["closed->open"]


# ------------------------------------------------------------------------------------- lifecycle


async def test_aclose_waits_for_inflight_requests(panel: FakeRemnawave) -> None:
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
    api = RemnawaveApi(transport)
    panel.inject("latency", path="/nodes", delay=0.3)
    call = asyncio.create_task(api.nodes())
    await asyncio.sleep(0.05)
    assert transport.inflight == 1
    await transport.aclose(grace=5)
    assert (await call) == []
    assert transport.closed
    with pytest.raises(RemnawaveError) as info:
        await api.nodes()
    assert info.value.code == "CLIENT_CLOSED"


async def test_redirect_is_not_followed(make: Callable[..., Transport]) -> None:
    from aiohttp import web

    async def login(request: web.Request) -> web.Response:
        raise web.HTTPFound("https://login.example.com/")

    app = web.Application()
    app.router.add_get("/api/system/metadata", login)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        api = RemnawaveApi(make(TransportConfig(base_url=f"http://127.0.0.1:{port}", token="T")))
        with pytest.raises(RemnawaveError) as info:
            await api.metadata()
        assert info.value.kind is ErrorKind.AUTH
        assert info.value.status == 302
    finally:
        await runner.cleanup()


async def test_html_answer_is_reported_as_not_an_api(make: Callable[..., Transport]) -> None:
    from aiohttp import web

    async def page(request: web.Request) -> web.Response:
        return web.Response(text="<html>Cloudflare Access</html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/api/system/metadata", page)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        api = RemnawaveApi(make(TransportConfig(base_url=f"http://127.0.0.1:{port}", token="T")))
        with pytest.raises(RemnawaveError) as info:
            await api.metadata()
        assert info.value.kind is ErrorKind.SERVER
        assert info.value.code == "BAD_RESPONSE"
        assert "Cloudflare" in info.value.hint_ru
    finally:
        await runner.cleanup()


async def test_secrets_are_registered_for_log_masking(make: Callable[..., Transport]) -> None:
    from svbg.core.log import mask

    secret_cookie = "panelsid=" + "q" * 24
    make(TransportConfig(base_url="https://p.example.com", token="tok-" + "z" * 30, cookie=secret_cookie))
    assert "z" * 30 not in mask("token tok-" + "z" * 30)
    assert "q" * 24 not in mask(f"cookie was {secret_cookie}")
