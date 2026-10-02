"""Database access: SQLAlchemy Core on asyncpg.

Usage::

    db = Database(dsn)
    await db.start()
    async with db.tx() as conn:          # one transaction
        await conn.execute(stmt)
    async with db.read() as conn:        # autocommit-style read
        rows = (await conn.execute(q)).all()
    listener = await db.listen("svbg_jobs", callback)   # LISTEN on a dedicated asyncpg connection
    await db.notify("svbg_jobs", "payload")
    await db.close()

``dsn`` may be ``postgresql://`` or ``postgresql+asyncpg://``; it is normalized.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from svbg.core.log import mask

log = logging.getLogger("svbg.db")

APPLICATION_NAME = "svbg"
CONNECT_TIMEOUT = 10.0  # s: TCP connect + authentication of one connection
POOL_TIMEOUT = 10.0  # s: waiting for a free pooled connection
COMMAND_TIMEOUT = 30.0  # s: client-side bound of one statement (a dropped TCP connection)
STATEMENT_TIMEOUT_MS = 30_000  # server side: one statement
LOCK_TIMEOUT_MS = 10_000  # server side: waiting for a lock (a migration's ALTER TABLE, a FOR UPDATE)
IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000  # server side: a transaction left open by a stuck task
LISTEN_CHECK_INTERVAL = 5.0  # s: LISTEN connection watchdog period
LISTEN_PING_TIMEOUT = 5.0  # s: watchdog ping bound
CLOSE_TIMEOUT = 5.0  # s: graceful close of a LISTEN connection

NotifyCallback = Callable[[str, str], Awaitable[None] | None]


def normalize_dsn(dsn: str) -> tuple[str, str]:
    """Return (sqlalchemy_url, asyncpg_dsn)."""
    raw = dsn.strip()
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if raw.startswith(prefix):
            rest = raw[len(prefix) :]
            return f"postgresql+asyncpg://{rest}", f"postgresql://{rest}"
    raise ValueError("DATABASE_URL must start with postgresql://")


class Listener:
    """A LISTEN subscription on a dedicated connection.

    A watchdog pings the connection every ``check_interval`` seconds (``select 1`` bounded by
    ``ping_timeout``): a closed connection, a half-open one (the database host vanished without a RST) or a
    connection whose subscription could not be set up is dropped and re-established, so wake-ups do not
    silently degrade to polling.
    """

    def __init__(
        self,
        dsn: str,
        channel: str,
        callback: NotifyCallback,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        check_interval: float = LISTEN_CHECK_INTERVAL,
        ping_timeout: float = LISTEN_PING_TIMEOUT,
    ) -> None:
        self._dsn = dsn
        self._channel = channel
        self._callback = callback
        self._connect_timeout = connect_timeout
        self._check_interval = check_interval
        self._ping_timeout = ping_timeout
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._callbacks: set[asyncio.Future[Any]] = set()
        self._closed = False

    @property
    def connected(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.is_closed()

    async def start(self) -> None:
        await self._connect()
        self._task = asyncio.create_task(self._watch(), name=f"listen:{self._channel}")

    async def _connect(self) -> None:
        conn = await asyncpg.connect(
            self._dsn,
            timeout=self._connect_timeout,
            command_timeout=self._ping_timeout,
            server_settings={"application_name": f"{APPLICATION_NAME}:listen"},
        )
        try:
            await conn.add_listener(self._channel, self._on_notify)
        except BaseException:
            await _close_quietly(conn)
            raise
        self._conn = conn  # only a connection with a live subscription is kept

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        try:
            res = self._callback(channel, payload)
            if asyncio.iscoroutine(res):
                task = asyncio.ensure_future(res)
                self._callbacks.add(task)  # keep a reference until it finishes
                task.add_done_callback(self._callback_done)
        except Exception:
            log.exception("notify callback failed for %s", channel)

    def _callback_done(self, task: asyncio.Future[Any]) -> None:
        self._callbacks.discard(task)
        _log_task_error(task)

    async def _alive(self, conn: asyncpg.Connection) -> bool:
        if conn.is_closed():
            return False
        try:
            async with asyncio.timeout(self._ping_timeout):
                await conn.fetchval("select 1")
        except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            log.warning("LISTEN %s connection is not responding (%s), reconnecting", self._channel, _why(e))
            return False
        return True

    async def _watch(self) -> None:
        # Keep the connection alive; reconnect if it drops or stops answering.
        while not self._closed:
            await asyncio.sleep(self._check_interval)
            conn = self._conn
            if conn is not None and await self._alive(conn):
                continue
            if conn is not None:
                self._conn = None
                await _close_quietly(conn)
            if self._closed:
                return
            try:
                await self._connect()
                log.info("LISTEN %s reconnected", self._channel)
            except Exception as e:  # noqa: BLE001 - retry loop: the watchdog itself must never die
                log.warning("LISTEN %s reconnect failed: %s", self._channel, _why(e))

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        conn, self._conn = self._conn, None
        if conn is not None:
            await _close_quietly(conn)


async def _close_quietly(conn: asyncpg.Connection) -> None:
    """Close gracefully within ``CLOSE_TIMEOUT``; a dead peer gets the connection terminated instead."""
    if conn.is_closed():
        return
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT + 1):  # asyncpg's own bound, backed by ours
            await conn.close(timeout=CLOSE_TIMEOUT)
    except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
        conn.terminate()


def _why(exc: BaseException) -> str:
    """Exception type and message for a log line, secrets masked (asyncpg messages may quote the DSN)."""
    return mask(f"{type(exc).__name__}: {exc}")[:300]


def _log_task_error(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("notify callback task failed", exc_info=task.exception())


class Database:
    """The connection pool. Every layer has a bound, so a dead or locked database fails fast instead of
    hanging a loop or the shutdown: ``connect_timeout`` (TCP + auth), ``pool_timeout`` (waiting for a free
    connection), ``command_timeout`` (client side, per statement) and the server-side ``statement_timeout``,
    ``lock_timeout`` and ``idle_in_transaction_session_timeout`` (milliseconds; ``0`` disables one)."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 10,
        max_overflow: int = 5,
        echo: bool = False,
        pool_timeout: float = POOL_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        command_timeout: float | None = COMMAND_TIMEOUT,
        statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
        lock_timeout_ms: int = LOCK_TIMEOUT_MS,
        idle_in_transaction_timeout_ms: int = IDLE_IN_TRANSACTION_TIMEOUT_MS,
        application_name: str = APPLICATION_NAME,
    ) -> None:
        self.sa_url, self.pg_dsn = normalize_dsn(dsn)
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._echo = echo
        self._pool_timeout = pool_timeout
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._server_settings = {
            "application_name": application_name,
            "statement_timeout": str(int(statement_timeout_ms)),
            "lock_timeout": str(int(lock_timeout_ms)),
            "idle_in_transaction_session_timeout": str(int(idle_in_transaction_timeout_ms)),
        }
        self._engine: AsyncEngine | None = None
        self._listeners: list[Listener] = []

    def connect_args(self) -> dict[str, Any]:
        """Arguments for ``asyncpg.connect`` of every pooled connection."""
        args: dict[str, Any] = {
            "timeout": self._connect_timeout,
            "server_settings": dict(self._server_settings),
        }
        if self._command_timeout is not None:
            args["command_timeout"] = self._command_timeout
        return args

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database is not started")
        return self._engine

    async def start(self) -> None:
        if self._engine is not None:
            return
        engine = create_async_engine(
            self.sa_url,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=self._echo,
            connect_args=self.connect_args(),
        )
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql("select 1")
        except BaseException:
            await engine.dispose()  # a failed start leaves no pool behind (the app retries start())
            raise
        self._engine = engine

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncConnection]:
        """A connection inside one transaction (commit on success, rollback on error)."""
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        """A connection for reads (implicit transaction, rolled back at the end)."""
        async with self.engine.connect() as conn:
            yield conn

    async def listen(self, channel: str, callback: NotifyCallback) -> Listener:
        listener = Listener(self.pg_dsn, channel, callback, connect_timeout=self._connect_timeout)
        await listener.start()
        self._listeners.append(listener)
        return listener

    async def notify(self, channel: str, payload: str = "") -> None:
        async with self.engine.connect() as conn:
            await conn.exec_driver_sql("select pg_notify($1, $2)", (channel, payload))
            await conn.commit()

    async def close(self) -> None:
        for listener in self._listeners:
            await listener.close()
        self._listeners.clear()
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
