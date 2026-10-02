"""Helpers shared by ``env.py`` and the migration API (import-light: no SQLAlchemy asyncio, no greenlet)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

__all__ = ["asyncpg_dsn", "resolve_dsn", "sqlalchemy_url"]

_PREFIXES: Final = ("postgresql+asyncpg://", "postgres://", "postgresql://")


def _rest(dsn: str) -> str:
    raw = dsn.strip()
    for prefix in _PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    raise ValueError("DATABASE_URL must start with postgresql://")


def sqlalchemy_url(dsn: str) -> str:
    return f"postgresql+asyncpg://{_rest(dsn)}"


def asyncpg_dsn(dsn: str) -> str:
    return f"postgresql://{_rest(dsn)}"


def resolve_dsn(explicit: str | None, environ: Mapping[str, str] | None = None) -> str | None:
    """``explicit`` or ``DATABASE_URL`` from ``data/.env`` / the environment (bootstrap priority)."""
    if explicit:
        return explicit
    from svbg.core.settings.bootstrap import default_env_path, read_bootstrap

    env = os.environ if environ is None else environ
    boot = read_bootstrap(default_env_path(env), env)
    return boot.database_url
