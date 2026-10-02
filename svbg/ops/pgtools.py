"""PostgreSQL client tools (``pg_dump``, ``pg_restore``): where to find them and how to run them safely.

* The password never appears on a command line (``ps`` shows it to every user of the host): the DSN is
  split into a password-less ``postgresql://`` URL and ``PGPASSWORD`` in the child's environment.
* Every run has a timeout; on expiry the child is killed. Error output is masked and shortened.
* :meth:`PgTools.arun` (backups, restore) is cancel-safe: a cancelled or timed-out run terminates the child
  (``SIGTERM``, then ``SIGKILL`` after a grace period) instead of leaving it running in a worker thread.
* Lookup order: ``SVBG_PG_BIN`` → ``PATH`` → ``<repo>/.tools/pgsql/bin`` (development checkout).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit, urlunsplit

from svbg.core.log import mask

__all__ = ["PgToolError", "PgTools", "conninfo", "major_version"]

_EXE: Final = ".exe" if sys.platform == "win32" else ""
_REPO_BIN: Final = Path(__file__).resolve().parents[2] / ".tools" / "pgsql" / "bin"
_VERSION_RE: Final = re.compile(r"(\d+)(?:\.(\d+))?")
_STDERR_MAX: Final = 600
_KILL_GRACE_S: Final = 5.0


class PgToolError(Exception):
    """A client tool is missing or failed. ``str()`` is owner-facing (Russian), secrets masked."""


def conninfo(dsn: str) -> tuple[str, dict[str, str]]:
    """``(url_without_password, extra_env)`` for libpq tools from an application DSN."""
    raw = dsn.strip()
    for prefix in ("postgresql+asyncpg://", "postgres://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
    if not raw.startswith("postgresql://"):
        raise PgToolError("DATABASE_URL должен начинаться с postgresql://")
    parts = urlsplit(raw)
    env: dict[str, str] = {}
    netloc = parts.netloc
    if "@" in netloc:
        auth, host = netloc.rsplit("@", 1)
        user, sep, password = auth.partition(":")
        if sep and password:
            env["PGPASSWORD"] = unquote(password)
        netloc = f"{user}@{host}" if user else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), env


def major_version(text: str) -> int | None:
    """``"pg_dump (PostgreSQL) 17.10"`` → 17."""
    m = _VERSION_RE.search(text.rsplit(")", 1)[-1] if ")" in text else text)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class PgTools:
    """Located client tools. ``bindir=None``: look the tools up (see the module docstring)."""

    bindir: Path | None = None

    @classmethod
    def locate(cls, environ: Mapping[str, str] | None = None) -> PgTools:
        env = os.environ if environ is None else environ
        explicit = env.get("SVBG_PG_BIN", "").strip()
        if explicit:
            return cls(Path(explicit))
        if shutil.which("pg_dump") is not None:
            return cls(None)
        if (_REPO_BIN / f"pg_dump{_EXE}").exists():
            return cls(_REPO_BIN)
        return cls(None)

    def path(self, tool: str) -> str:
        if self.bindir is not None:
            candidate = self.bindir / f"{tool}{_EXE}"
            if candidate.exists():
                return str(candidate)
            raise PgToolError(f"{tool} не найден в {self.bindir}")
        found = shutil.which(tool)
        if found is None:
            raise PgToolError(
                f"{tool} не найден: установите postgresql-client-18 (в образе Docker он уже есть) "
                "или укажите каталог программ в SVBG_PG_BIN"
            )
        return found

    def version(self, tool: str) -> str:
        res = self.run(tool, ["--version"], timeout=30)
        return res.strip()

    def run(
        self,
        tool: str,
        args: Sequence[str],
        *,
        timeout: float,
        dsn_env: Mapping[str, str] | None = None,
        stdout_path: Path | None = None,
    ) -> str:
        """Run ``tool args`` and return its stdout (text); ``PgToolError`` with masked stderr on failure.

        Blocking: for quick calls (``--version``) and the host CLI. Long runs from the bot use :meth:`arun`.
        """
        exe = self.path(tool)
        env, secrets = _child_env(tool, dsn_env)
        try:
            if stdout_path is not None:
                with stdout_path.open("wb") as out:
                    proc = subprocess.run(  # noqa: S603 - fixed tool path, argument list without a shell
                        [exe, *args],
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=subprocess.PIPE,
                        env=env,
                        timeout=timeout,
                        check=False,
                    )
                stdout = b""
            else:
                proc = subprocess.run(  # noqa: S603 - fixed tool path, argument list without a shell
                    [exe, *args],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    env=env,
                    timeout=timeout,
                    check=False,
                )
                stdout = proc.stdout
        except subprocess.TimeoutExpired:
            raise PgToolError(f"{tool} не уложился в {timeout:g} с и остановлен") from None
        except OSError as exc:
            raise PgToolError(f"{tool} не запустился: {exc.strerror or type(exc).__name__}") from None
        if proc.returncode != 0:
            raise PgToolError(f"{tool} завершился с ошибкой: {_clean(proc.stderr, secrets)}")
        return stdout.decode("utf-8", "replace")

    async def arun(
        self,
        tool: str,
        args: Sequence[str],
        *,
        timeout: float,  # noqa: ASYNC109 - the child's own deadline, enforced by stopping it
        dsn_env: Mapping[str, str] | None = None,
        stdout_path: Path | None = None,
        kill_grace: float = _KILL_GRACE_S,
    ) -> str:
        """:meth:`run` as an asyncio subprocess; a timeout or cancellation stops the child (term → kill)."""
        exe = self.path(tool)
        env, secrets = _child_env(tool, dsn_env)
        with contextlib.ExitStack() as stack:
            out: Any = stack.enter_context(stdout_path.open("wb")) if stdout_path is not None else None
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe,
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out if out is not None else asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except OSError as exc:
                raise PgToolError(f"{tool} не запустился: {exc.strerror or type(exc).__name__}") from None
            try:
                async with asyncio.timeout(timeout):
                    stdout, stderr = await proc.communicate()
            except TimeoutError:
                await _stop(proc, kill_grace)
                raise PgToolError(f"{tool} не уложился в {timeout:g} с и остановлен") from None
            except BaseException:  # cancelled (shutdown, scheduler deadline): never leave the child running
                await _stop(proc, kill_grace)
                raise
        if proc.returncode != 0:
            raise PgToolError(f"{tool} завершился с ошибкой: {_clean(stderr or b'', secrets)}")
        return (stdout or b"").decode("utf-8", "replace")

    async def aversion(self, tool: str) -> str:
        return (await self.arun(tool, ["--version"], timeout=30)).strip()


def _child_env(tool: str, dsn_env: Mapping[str, str] | None) -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    env.update({"PGCONNECT_TIMEOUT": "10", "PGAPPNAME": f"svbg-{tool}", "PGCLIENTENCODING": "UTF8"})
    env.update(dsn_env or {})
    secrets = [v for k, v in (dsn_env or {}).items() if k == "PGPASSWORD" and v]
    return env, secrets


async def _stop(proc: asyncio.subprocess.Process, grace: float) -> None:
    """Terminate the child, kill it after ``grace`` seconds; a second cancellation kills it at once."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        async with asyncio.timeout(grace):
            await proc.wait()
    except TimeoutError:
        pass
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    if proc.returncode is None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(grace):
                await proc.wait()


def _clean(stderr: bytes, secrets: Sequence[str]) -> str:
    text = stderr.decode("utf-8", "replace").strip()
    for secret in secrets:
        text = text.replace(secret, "***")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = " | ".join(lines[:6]) or "без подробностей"
    text = mask(text)
    return text if len(text) <= _STDERR_MAX else text[: _STDERR_MAX - 1] + "…"
