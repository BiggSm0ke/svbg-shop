"""Read-only access to a Bedolaga PostgreSQL (06 §2, §4.2).

* its own asyncpg connection (``--source-dsn``), never the bot's pool: the session is
  ``default_transaction_read_only`` and everything is read in **one** ``REPEATABLE READ READ ONLY``
  transaction,
  so a run sees one consistent snapshot even against a live database;
* explicit column lists only: a column missing in this schema version reads as ``NULL`` (tolerance to
  4.x–5.x schemas), and the connection secrets of users (06 §2.1, R22) can never be selected — asking for one
  is a programming error;
* the effective Bedolaga setting = ``.env`` if the key is there, else the ``system_settings`` row (06 §3.1).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg

__all__ = ["FORBIDDEN_COLUMNS", "BedolagaSource", "SourceSettings", "open_source"]

#: Never read (06 §2.1 «Секреты пользователя», R22).
FORBIDDEN_COLUMNS: Final = frozenset(
    {
        "vless_uuid",
        "trojan_password",
        "ss_password",
        "password_hash",
        "email_verification_token",
        "password_reset_token",
        "email_change_code",
        "subscription_crypto_link",
    }
)
_IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"bad identifier {name!r}")
    return f'"{name}"'


class BedolagaSource:
    """One read-only snapshot of the source database."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn
        self._tables: set[str] | None = None
        self._columns: dict[str, set[str]] = {}
        #: Every statement sent (tests assert that no secret column is ever read).
        self.statements: list[str] = []

    async def tables(self) -> set[str]:
        if self._tables is None:
            rows = await self._conn.fetch(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
            self._tables = {str(r[0]) for r in rows}
        return self._tables

    async def has(self, table: str) -> bool:
        return table in await self.tables()

    async def columns(self, table: str) -> set[str]:
        if table not in self._columns:
            rows = await self._conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = $1",
                table,
            )
            self._columns[table] = {str(r[0]) for r in rows}
        return self._columns[table]

    async def rows(
        self,
        table: str,
        cols: Sequence[str],
        *,
        where: str | None = None,
        order: str | None = "id",
        args: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """``SELECT cols FROM table`` (missing table → ``[]``, missing column → ``NULL``)."""
        bad = FORBIDDEN_COLUMNS.intersection(cols)
        if bad:
            raise ValueError(f"secret columns are never imported: {sorted(bad)}")
        if not await self.has(table):
            return []
        present = await self.columns(table)
        parts = [_ident(c) if c in present else f"NULL AS {_ident(c)}" for c in cols]
        sql = f"SELECT {', '.join(parts)} FROM public.{_ident(table)}"
        if where:
            sql += f" WHERE {where}"
        if order and order in present:
            sql += f" ORDER BY {_ident(order)}"
        return [dict(r) for r in await self.fetch(sql, *args)]

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        self.statements.append(sql)
        return list(await self._conn.fetch(sql, *args))

    async def scalar(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        return await self._conn.fetchval(sql, *args)

    async def alembic_version(self) -> str | None:
        if not await self.has("alembic_version"):
            return None
        return await self.scalar("SELECT version_num FROM public.alembic_version LIMIT 1")

    async def system_settings(self) -> dict[str, str | None]:
        rows = await self.rows("system_settings", ["key", "value"], order="key")
        return {str(r["key"]): r["value"] for r in rows}


class SourceSettings:
    """The effective Bedolaga configuration: ``.env`` wins, then ``system_settings`` (06 §3.1)."""

    def __init__(self, env: Mapping[str, str | None], system: Mapping[str, str | None]) -> None:
        self._env = {str(k): v for k, v in env.items()}
        self._system = dict(system)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._env and self._env[key] not in (None, ""):
            return self._env[key]
        value = self._system.get(key)
        return default if value in (None, "") else value

    def int(self, key: str, default: int | None = None) -> int | None:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(str(raw).strip())
        except ValueError:
            return default

    def float(self, key: str, default: float) -> float:
        raw = self.get(key)
        try:
            return float(str(raw).replace(",", ".")) if raw is not None else default
        except ValueError:
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def ints(self, key: str) -> list[int]:
        raw = self.get(key) or ""
        out: list[int] = []
        for part in re.split(r"[\s,;]+", raw):
            if part.strip().lstrip("-").isdigit():
                out.append(int(part))
        return out

    def keys(self) -> Iterable[str]:
        return set(self._env) | set(self._system)


@asynccontextmanager
async def open_source(dsn: str, *, connect_timeout: float = 15.0) -> AsyncIterator[BedolagaSource]:
    """Connect read-only and open the snapshot transaction (rolled back at the end: nothing is ever
    written)."""
    conn = await asyncpg.connect(
        dsn,
        timeout=connect_timeout,
        server_settings={"default_transaction_read_only": "on", "application_name": "svbg-import"},
    )
    try:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        try:
            yield BedolagaSource(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()
