"""End-to-end fixtures: the whole application on a real PostgreSQL 17 and the fake Telegram Bot API.

* the schema is created by the **Alembic migration** (its offline SQL), so every e2e test also exercises it;
* the database is the real :class:`svbg.db.engine.Database` (:class:`tests.dbkit.CountingDatabase`: plus a
  statement counter for "SQL per click" budgets and an uncounted ``raw()`` for assertions);
* :func:`app_env` writes a ``data/.env`` (token of a fake bot, DSN, owner) into a temp dir;
* :func:`start_app` starts :class:`svbg.app.App` bound to 127.0.0.1 on a free port and stops it afterwards.
"""

from __future__ import annotations

import asyncio
import zoneinfo
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from svbg.app import App, AppOptions, DatabaseLike
from svbg.boot.envfile import EnvDocument, write_atomic
from svbg.core.crypto import generate_key
from tests.dbkit import CountingDatabase
from tests.fakes.telegram import FakeTelegram

REPO = Path(__file__).resolve().parents[2]
OWNER_ID = 777_000_001


def _ensure_tzdata() -> None:
    """Windows has no IANA database without the ``tzdata`` package: borrow PostgreSQL's copy for tests."""
    try:
        zoneinfo.ZoneInfo("Europe/Moscow")
    except zoneinfo.ZoneInfoNotFoundError:
        candidate = REPO / ".tools" / "pgsql" / "share" / "timezone"
        if (candidate / "Europe" / "Moscow").exists():
            zoneinfo.reset_tzpath(to=[str(candidate)])
            zoneinfo.ZoneInfo.clear_cache()


_ensure_tzdata()

_SCHEMA_SQL: str | None = None


def schema_sql() -> str:
    """``alembic upgrade head --sql`` (computed once per run)."""
    global _SCHEMA_SQL  # noqa: PLW0603 - per-process cache
    if _SCHEMA_SQL is None:
        from svbg.db.migrations import offline_sql

        _SCHEMA_SQL = offline_sql()
    return _SCHEMA_SQL


async def apply_sql(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def make_database(dsn: str) -> CountingDatabase:
    return CountingDatabase(dsn, pool_size=10)


def counting_db(db: DatabaseLike | None) -> CountingDatabase:
    """``app.db`` of an e2e application (always built by :func:`make_database`)."""
    assert isinstance(db, CountingDatabase), "e2e applications use tests.dbkit.CountingDatabase"
    return db


#: The entry captcha is off in e2e databases (it has its own test, ``test_captcha.py``): a stored value, as if
#: it came from ``.env`` earlier, so ``.env`` does not have to carry it and the owner gets no «✏️ .env» notice.
CAPTCHA_OFF_SQL = "insert into settings (key, value, source) values ('CAPTCHA_ENABLED', 'false', 'env_file')"


@pytest.fixture
async def e2e_dsn(pg_dsn: str) -> str:
    """A fresh database migrated to ``head`` (the entry captcha off)."""
    await apply_sql(pg_dsn, schema_sql())
    await apply_sql(pg_dsn, CAPTCHA_OFF_SQL)
    return pg_dsn


@pytest.fixture
async def tg() -> AsyncIterator[FakeTelegram]:
    async with FakeTelegram(webhook_retry_delay=0.05) as fake:
        yield fake


@dataclass
class AppEnv:
    """A data directory with ``.env`` for one test application."""

    data_dir: Path
    env_path: Path
    dsn: str
    token: str
    tg: FakeTelegram
    values: dict[str, str] = field(default_factory=dict)

    def write(self, **extra: str | None) -> None:
        """(Re)write ``.env`` with the base values plus ``extra`` (``None`` removes a key)."""
        merged: dict[str, str | None] = {**self.values, **extra}
        doc = EnvDocument.parse("")
        for key, value in merged.items():
            if value is not None:
                doc.set(key, value)
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.env_path, doc.render())

    def options(self, **overrides: Any) -> AppOptions:
        kwargs: dict[str, Any] = {
            "env_path": self.env_path,
            "environ": {"DATA_DIR": str(self.data_dir)},
            "web_host": "127.0.0.1",
            "web_port": 0,
            "configure_logging": False,
            "notify_owners_on_start": False,
            "database_factory": make_database,
            "db_connect_timeout": 10.0,
            "stop_timeout": 10.0,
            "runner_kwargs": {"start_retry_delay": 0.2, "drain_timeout": 5.0},
        }
        kwargs.update(overrides)
        return AppOptions(**kwargs)


@pytest.fixture
def app_env(tmp_path: Path, e2e_dsn: str, tg: FakeTelegram) -> AppEnv:
    token = tg.add_bot(username="svbg_e2e_bot")
    data_dir = tmp_path / "data"
    env = AppEnv(
        data_dir=data_dir,
        env_path=data_dir / ".env",
        dsn=e2e_dsn,
        token=token,
        tg=tg,
        values={
            "BOT_TOKEN": token,
            "TELEGRAM_API_URL": tg.url,
            "DATABASE_URL": e2e_dsn,
            "SECRET_KEY": generate_key(),
            "OWNER_IDS": str(OWNER_ID),
            "DATA_DIR": str(data_dir),
        },
    )
    env.write()
    return env


StartApp = Callable[..., Awaitable[App]]


@pytest.fixture
async def start_app(app_env: AppEnv) -> AsyncIterator[StartApp]:
    started: list[App] = []

    async def start(**overrides: Any) -> App:
        app = App(app_env.options(**overrides))
        await app.start()
        started.append(app)
        return app

    yield start
    for app in reversed(started):
        await app.stop()


@pytest.fixture
def wait_until() -> Callable[..., Awaitable[float]]:
    async def wait(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> float:
        """Poll ``predicate``; return the elapsed seconds or fail after ``timeout``."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        while not predicate():
            if loop.time() - began > timeout:
                raise AssertionError(f"condition not met within {timeout}s")
            await asyncio.sleep(interval)
        return loop.time() - began

    return wait
