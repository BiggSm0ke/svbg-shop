"""``POST /webhooks/pay/{instance_id}/{token}`` over real HTTP: body cap, token, raw bytes, dedup, latency,
and the access log never shows the token."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

import aiohttp
import pytest

from svbg.web.app import WebServer, build_web_app
from svbg.web.routes.payments import MAX_BODY, PATH, payment_routes
from tests.payments.conftest import Env, stub_webhook


@pytest.fixture
async def base(env: Env) -> AsyncIterator[str]:
    server = WebServer("127.0.0.1", 0, build_web_app(payment_routes(env.core)))
    await server.start()
    try:
        yield server.url
    finally:
        await server.stop()


def _url(base: str, env: Env, *, slug: str = "stubpay", token: str | None = None) -> str:
    inst = env.inst(slug)
    return base + PATH.format(instance_id=inst.id, token=token or inst.webhook_token)


async def test_paid_webhook_over_http(env: Env, base: str) -> None:
    pid = await env.pending()
    req = stub_webhook("paid", ext="inv-1", order=pid)
    async with aiohttp.ClientSession() as http:
        async with http.post(_url(base, env), data=req.body, headers=dict(req.headers)) as resp:
            assert resp.status == 200 and await resp.text() == "OK"
        async with http.post(_url(base, env), data=req.body, headers=dict(req.headers)) as resp:
            assert resp.status == 200  # duplicate delivery
    assert (await env.payment(pid))["status"] == "paid" and len(env.credited) == 1


async def test_raw_bytes_are_authenticated_exactly(env: Env, base: str) -> None:
    pid = await env.pending()
    req = stub_webhook("paid", ext="inv-1", order=pid)
    reformatted = req.body.replace(b", ", b",")  # same JSON, different bytes
    async with (
        aiohttp.ClientSession() as http,
        http.post(_url(base, env), data=reformatted, headers=dict(req.headers)) as resp,
    ):
        assert resp.status == 401
    assert (await env.payment(pid))["status"] == "pending"


@pytest.mark.parametrize("variant", ["wrong-token", "unknown-instance", "non-numeric", "huge-id"])
async def test_wrong_address_is_404(env: Env, base: str, variant: str) -> None:
    inst = env.inst()
    url = {
        "wrong-token": base + PATH.format(instance_id=inst.id, token="x" * 43),
        "unknown-instance": base + PATH.format(instance_id=inst.id + 100, token=inst.webhook_token),
        "non-numeric": base + PATH.format(instance_id="abc", token=inst.webhook_token),
        "huge-id": base + PATH.format(instance_id="9" * 30, token=inst.webhook_token),
    }[variant]
    req = stub_webhook("paid", ext="inv-1")
    async with (
        aiohttp.ClientSession() as http,
        http.post(url, data=req.body, headers=dict(req.headers)) as resp,
    ):
        assert resp.status == 404
    assert await env.count("payment_events") == 0


async def test_body_cap(env: Env, base: str) -> None:
    big = b"x" * (MAX_BODY + 1)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"y" * (MAX_BODY // 4)

    async with aiohttp.ClientSession() as http:
        async with http.post(_url(base, env), data=big) as resp:
            assert resp.status == 413
        async with http.post(_url(base, env), data=chunks()) as resp:  # chunked, no Content-Length
            assert resp.status == 413
    assert await env.count("payment_events") == 0


async def test_rejections_over_http(env: Env, base: str) -> None:
    pid = await env.pending()
    stale = stub_webhook("paid", ext="inv-1", order=pid, at=None)
    stale_headers = dict(stale.headers)
    stale_headers["X-Ts"] = str(int(time.time()) - 3600)
    from tests.payments.conftest import sign

    stale_headers["X-Sig"] = sign(stale.body, stale_headers["X-Ts"])
    test_mode = stub_webhook("paid", ext="inv-1", order=pid, test=True)
    async with aiohttp.ClientSession() as http:
        async with http.post(_url(base, env), data=stale.body, headers=stale_headers) as resp:
            assert resp.status == 401
        async with http.post(_url(base, env), data=test_mode.body, headers=dict(test_mode.headers)) as resp:
            assert resp.status == 400
        async with http.post(_url(base, env), data=b"not json", headers={"X-Ts": "1", "X-Sig": "0"}) as resp:
            assert resp.status == 401
    assert (await env.payment(pid))["status"] == "pending"


async def test_webhook_latency_and_token_never_logged(
    env: Env, base: str, caplog: pytest.LogCaptureFixture
) -> None:
    pids = [await env.pending() for _ in range(15)]
    timings: list[float] = []
    caplog.set_level(logging.INFO, logger="svbg.web.access")
    async with aiohttp.ClientSession() as http:
        for n, pid in enumerate(pids):
            req = stub_webhook("paid", ext=f"inv-{n + 1}", order=pid)
            started = time.perf_counter()
            async with http.post(_url(base, env), data=req.body, headers=dict(req.headers)) as resp:
                assert resp.status == 200
            timings.append(time.perf_counter() - started)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 0.1, timings  # ≤ 100 ms per webhook, crediting included
    assert len(env.credited) == 15
    token = env.inst().webhook_token
    assert caplog.records, "access log expected"
    assert all(token not in r.getMessage() for r in caplog.records)
