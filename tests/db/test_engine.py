"""``svbg.db.engine``: every connection layer is bounded; the LISTEN connection heals itself.

These tests need the SQLAlchemy asyncio extension (``greenlet``) only to import the module; the LISTEN tests
talk to PostgreSQL through plain asyncpg.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

pytest.importorskip("greenlet", reason="SQLAlchemy asyncio needs greenlet (run `uv sync`)")

from svbg.db import engine as engine_mod
from svbg.db.engine import Database, Listener


async def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


# ------------------------------------------------------------------ pool / driver / server timeouts


def test_connect_args_bound_driver_and_server() -> None:
    db = Database("postgresql://u:p@localhost/x")
    args = db.connect_args()
    assert args["timeout"] == engine_mod.CONNECT_TIMEOUT
    assert args["command_timeout"] == engine_mod.COMMAND_TIMEOUT
    assert args["server_settings"] == {
        "application_name": "svbg",
        "statement_timeout": "30000",
        "lock_timeout": "10000",
        "idle_in_transaction_session_timeout": "60000",
    }
    custom = Database(
        "postgresql://u:p@localhost/x",
        connect_timeout=3,
        command_timeout=None,
        statement_timeout_ms=0,
        lock_timeout_ms=500,
    ).connect_args()
    assert custom["timeout"] == 3
    assert "command_timeout" not in custom
    assert custom["server_settings"]["statement_timeout"] == "0"
    assert custom["server_settings"]["lock_timeout"] == "500"


class _FakeConnCtx:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    async def __aenter__(self) -> Any:
        if self.error is not None:
            raise self.error
        conn = type("C", (), {})()

        async def exec_driver_sql(_sql: str) -> None:
            return None

        conn.exec_driver_sql = exec_driver_sql  # type: ignore[attr-defined]
        return conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.disposed = False

    def connect(self) -> _FakeConnCtx:
        return _FakeConnCtx(self.error)

    async def dispose(self) -> None:
        self.disposed = True


async def test_start_passes_timeouts_and_failed_start_leaves_no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, dict[str, Any], _FakeEngine]] = []
    error: list[BaseException | None] = [OSError("connection refused")]

    def fake_create(url: str, **kw: Any) -> _FakeEngine:
        eng = _FakeEngine(error[0])
        created.append((url, kw, eng))
        return eng

    monkeypatch.setattr(engine_mod, "create_async_engine", fake_create)
    db = Database("postgresql://u:p@localhost/x", pool_size=3)
    with pytest.raises(OSError, match="refused"):
        await db.start()
    url, kw, eng = created[0]
    assert url == "postgresql+asyncpg://u:p@localhost/x"
    assert kw["pool_timeout"] == engine_mod.POOL_TIMEOUT
    assert kw["pool_size"] == 3
    assert kw["connect_args"] == db.connect_args()
    assert eng.disposed  # the failed pool is not leaked
    with pytest.raises(RuntimeError, match="not started"):
        _ = db.engine

    error[0] = None  # the next attempt succeeds and keeps its engine
    await db.start()
    assert db.engine is created[1][2]  # type: ignore[comparison-overlap]
    assert not created[1][2].disposed


# ------------------------------------------------------------------ LISTEN with fakes


class FakeConn:
    def __init__(self, *, listen_error: BaseException | None = None, hang_ping: bool = False) -> None:
        self.listen_error = listen_error
        self.hang_ping = hang_ping
        self.closed = False
        self.terminated = False
        self.listeners: list[tuple[str, Any]] = []

    def is_closed(self) -> bool:
        return self.closed or self.terminated

    async def add_listener(self, channel: str, cb: Any) -> None:
        if self.listen_error is not None:
            raise self.listen_error
        self.listeners.append((channel, cb))

    async def fetchval(self, _sql: str) -> int:
        if self.hang_ping:
            await asyncio.sleep(3600)  # a half-open TCP connection: no answer, no error
        return 1

    async def close(self, *, timeout: float | None = None) -> None:
        if self.hang_ping:
            await asyncio.sleep(3600)
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


def _fake_connect(monkeypatch: pytest.MonkeyPatch, conns: list[FakeConn]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def connect(dsn: str, **kw: Any) -> FakeConn:
        calls.append({"dsn": dsn, **kw})
        if not conns:
            raise OSError("database is down")
        return conns.pop(0)

    monkeypatch.setattr(engine_mod.asyncpg, "connect", connect)
    return calls


async def test_listen_connect_is_bounded_and_failed_subscription_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = FakeConn(listen_error=asyncpg.InterfaceError("cannot listen"))
    calls = _fake_connect(monkeypatch, [broken])
    listener = Listener("postgresql://u:p@h/x", "svbg_jobs", lambda c, p: None, connect_timeout=7)
    with pytest.raises(asyncpg.InterfaceError):
        await listener.start()
    assert calls[0]["timeout"] == 7
    assert broken.closed  # not leaked
    assert not listener.connected  # never kept without a subscription
    await listener.close()


async def test_watchdog_replaces_a_half_open_connection(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(engine_mod, "CLOSE_TIMEOUT", 0.05)
    first, second = FakeConn(hang_ping=True), FakeConn()
    _fake_connect(monkeypatch, [first, second])
    listener = Listener(
        "postgresql://u:p@h/x", "ch", lambda c, p: None, check_interval=0.05, ping_timeout=0.05
    )
    await listener.start()
    try:
        await _wait_until(lambda: second.listeners)
        assert first.terminated  # graceful close hung → terminated, not left open
        assert listener.connected
        assert any("not responding" in r.getMessage() for r in caplog.records)
    finally:
        await listener.close()
    assert second.closed


async def test_watchdog_keeps_retrying_and_masks_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn = FakeConn()
    calls = _fake_connect(monkeypatch, [conn])
    listener = Listener("postgresql://u:p@h/x", "ch", lambda c, p: None, check_interval=0.03)
    await listener.start()
    conn.closed = True  # the server dropped us; reconnects fail for a while

    async def failing(dsn: str, **kw: Any) -> FakeConn:
        calls.append(kw)
        raise OSError("could not connect to postgresql://svbg:topsecret@db/svbg")

    monkeypatch.setattr(engine_mod.asyncpg, "connect", failing)
    try:
        await _wait_until(lambda: len(calls) >= 4)
        assert not listener.connected
        healed = FakeConn()
        _fake_connect(monkeypatch, [healed])
        await _wait_until(lambda: healed.listeners)
        assert listener.connected
    finally:
        await listener.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reconnect failed" in text
    assert "topsecret" not in text


async def test_coroutine_callbacks_are_kept_until_done(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _fake_connect(monkeypatch, [conn])
    release = asyncio.Event()
    seen: list[str] = []

    async def callback(channel: str, payload: str) -> None:
        await release.wait()
        seen.append(payload)

    listener = Listener("postgresql://u:p@h/x", "ch", callback, check_interval=60)
    await listener.start()
    try:
        _channel, on_notify = conn.listeners[0]
        on_notify(conn, 1, "ch", "a")
        on_notify(conn, 1, "ch", "b")
        assert len(listener._callbacks) == 2  # strong references: not garbage-collected mid-flight
        release.set()
        await _wait_until(lambda: not listener._callbacks)
        assert sorted(seen) == ["a", "b"]
    finally:
        await listener.close()


# ------------------------------------------------------------------ real PostgreSQL


async def test_listener_reconnects_after_the_server_kills_it(pg_dsn: str) -> None:
    got: list[str] = []
    dsn = engine_mod.normalize_dsn(pg_dsn)[1]
    listener = Listener(dsn, "svbg_test", lambda _c, p: got.append(p), check_interval=0.1, ping_timeout=1)
    await listener.start()
    admin = await asyncpg.connect(dsn)
    try:
        killed = await admin.fetchval(
            "select count(pg_terminate_backend(pid)) from pg_stat_activity "
            "where application_name = 'svbg:listen'"
        )
        assert killed == 1
        await _wait_until(lambda: listener.connected and listener._conn is not None, timeout=10)

        async def delivered() -> None:
            while not got:
                await admin.execute("select pg_notify('svbg_test', 'after')")
                await asyncio.sleep(0.1)

        await asyncio.wait_for(delivered(), 10)
        assert "after" in got
    finally:
        await admin.close()
        await listener.close()
