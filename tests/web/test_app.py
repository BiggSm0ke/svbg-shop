from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import make_mocked_request

from svbg.web import MAX_BODY_BYTES, WebServer, build_web_app
from svbg.web.app import SECURITY_HEADERS, is_loopback

routes = web.RouteTableDef()


@routes.post("/echo")
async def _echo(request: web.Request) -> web.Response:
    body = await request.read()
    return web.json_response({"size": len(body)})


@routes.get("/boom")
async def _boom(request: web.Request) -> web.Response:
    raise RuntimeError("secret internals: password=hunter2")


@routes.get("/hook/{instance}/{webhook_token}")
async def _hook(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@pytest.fixture
async def server() -> AsyncIterator[WebServer]:
    async def ready() -> tuple[bool, Mapping[str, Any]]:
        return state["ready"], {"bot": "ok" if state["ready"] else "down"}

    state = {"ready": True}
    app = build_web_app(routes, ready_check=ready)
    srv = WebServer("127.0.0.1", 0, app)
    srv.state = state  # type: ignore[attr-defined]
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


@pytest.fixture
async def http() -> AsyncIterator[ClientSession]:
    async with ClientSession() as s:
        yield s


async def test_health_from_loopback(server: WebServer, http: ClientSession) -> None:
    async with http.get(f"{server.url}/health") as r:
        assert r.status == 200
        assert await r.json() == {"status": "ok"}
        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value
        assert r.headers["Server"] == "svbg"
        assert len(r.headers["X-Request-Id"]) == 32


async def test_ready_reflects_check(server: WebServer, http: ClientSession) -> None:
    async with http.get(f"{server.url}/ready") as r:
        assert r.status == 200
        assert await r.json() == {"ready": True, "components": {"bot": "ok"}}
    server.state["ready"] = False  # type: ignore[attr-defined]
    async with http.get(f"{server.url}/ready") as r:
        assert r.status == 503
        assert (await r.json())["ready"] is False


async def test_ready_timeout_is_503(http: ClientSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("svbg.web.app.READY_TIMEOUT", 0.05)

    async def slow() -> tuple[bool, Mapping[str, Any]]:
        await asyncio.sleep(5)
        return True, {}

    srv = WebServer("127.0.0.1", 0, build_web_app(ready_check=slow))
    await srv.start()
    try:
        async with http.get(f"{srv.url}/ready") as r:
            assert r.status == 503
            assert (await r.json())["error"] == "timeout"
    finally:
        await srv.stop()


def _mocked(path: str, remote: str, headers: dict[str, str] | None = None) -> web.Request:
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda name, default=None: (
        (remote, 40000) if name == "peername" else default
    )
    return make_mocked_request("GET", path, headers=headers, transport=transport)


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_local_endpoints_hidden_from_remote_peers(path: str) -> None:
    app = build_web_app()
    req = _mocked(path, "10.0.0.5", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
    assert not is_loopback(req)
    match = await app.router.resolve(req)
    with pytest.raises(web.HTTPNotFound):
        await match.handler(req)


@pytest.mark.parametrize("remote", ["127.0.0.1", "::1"])
def test_loopback_detection(remote: str) -> None:
    assert is_loopback(_mocked("/health", remote))


@pytest.mark.parametrize(
    "header",
    [
        "X-Forwarded-For",
        "Forwarded",
        "X-Real-IP",
        "Via",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "x-forwarded-for",
    ],
)
@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_local_endpoints_hidden_behind_same_host_proxy(path: str, header: str) -> None:
    """Caddy on the same host connects from 127.0.0.1 for outside clients; proxy headers give it away."""
    app = build_web_app()
    req = _mocked(path, "127.0.0.1", headers={header: "203.0.113.7"})
    assert not is_loopback(req)
    match = await app.router.resolve(req)
    with pytest.raises(web.HTTPNotFound):
        await match.handler(req)


async def test_health_via_same_host_proxy_is_404(server: WebServer, http: ClientSession) -> None:
    for path in ("/health", "/ready"):
        async with http.get(f"{server.url}{path}", headers={"X-Forwarded-For": "203.0.113.7"}) as r:
            assert r.status == 404
        async with http.get(f"{server.url}{path}") as r:
            assert r.status == 200


async def test_body_limit(server: WebServer, http: ClientSession) -> None:
    async with http.post(f"{server.url}/echo", data=b"x" * 1000) as r:
        assert r.status == 200
        assert (await r.json())["size"] == 1000
    async with http.post(f"{server.url}/echo", data=b"x" * (MAX_BODY_BYTES + 1)) as r:
        assert r.status == 413
        assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_body_limit_chunked(server: WebServer, http: ClientSession) -> None:
    async def gen() -> AsyncIterator[bytes]:
        for _ in range(40):
            yield b"y" * 10_000

    async with http.post(f"{server.url}/echo", data=gen()) as r:
        assert r.status == 413


async def test_unhandled_error_is_generic_500(
    server: WebServer, http: ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="svbg.web"):
        async with http.get(f"{server.url}/boom", headers={"X-Request-Id": "req-123"}) as r:
            assert r.status == 500
            body = await r.json()
            text = str(body)
    assert body == {"error": "internal", "request_id": "req-123"}
    assert "hunter2" not in text
    assert r.headers["X-Request-Id"] == "req-123"


async def test_bad_request_id_is_replaced(server: WebServer, http: ClientSession) -> None:
    async with http.get(f"{server.url}/health", headers={"X-Request-Id": "bad id\twith<junk>"}) as r:
        assert r.headers["X-Request-Id"] != "bad id\twith<junk>"
        assert len(r.headers["X-Request-Id"]) == 32


async def test_not_found_has_headers(server: WebServer, http: ClientSession) -> None:
    async with http.get(f"{server.url}/nope") as r:
        assert r.status == 404
        assert r.headers["X-Frame-Options"] == "DENY"


async def test_access_log_hides_query_and_secrets(
    server: WebServer, http: ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "S3cr3tT0kenValue_abcdefghijklmnop"
    with caplog.at_level(logging.INFO, logger="svbg.web.access"):
        async with http.get(f"{server.url}/hook/rollypay/{secret}?sign=topsecretquery") as r:
            assert r.status == 200
        await asyncio.sleep(0.05)
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "svbg.web.access"]
    assert any("/hook/rollypay/***" in line for line in lines)
    joined = "\n".join(lines)
    assert secret not in joined
    assert "topsecretquery" not in joined
    assert "sign=" not in joined


async def test_server_start_stop_idempotent() -> None:
    srv = WebServer("127.0.0.1", 0, build_web_app())
    await srv.start()
    port = srv.port
    await srv.start()
    assert srv.port == port
    assert srv.running
    await srv.stop()
    await srv.stop()
    assert not srv.running


async def test_port_in_use_raises() -> None:
    first = WebServer("127.0.0.1", 0, build_web_app())
    await first.start()
    try:
        second = WebServer("127.0.0.1", first.port, build_web_app())
        with pytest.raises(OSError):
            await second.start()
        assert not second.running
    finally:
        await first.stop()


async def test_components_ready_uses_registry() -> None:
    from svbg.core.component import ComponentRegistry, HealthReport
    from svbg.web import components_ready

    class Comp:
        def __init__(self, name: str, report: HealthReport) -> None:
            self.name = name
            self._report = report

        async def probe(self, candidate: Mapping[str, Any]) -> None:
            return None

        async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
            return None

        async def health(self) -> HealthReport:
            return self._report

    reg = ComponentRegistry()
    reg.register(Comp("bot", HealthReport.ok()))
    reg.register(Comp("remnawave", HealthReport.degraded("slow")))
    ready, details = await components_ready(reg)()
    assert ready is True
    assert details == {"bot": "ok", "remnawave": "degraded"}
    reg.register(Comp("payments.x", HealthReport.down("dead")))
    ready, details = await components_ready(reg)()
    assert ready is False
    assert details["payments.x"] == "down"
