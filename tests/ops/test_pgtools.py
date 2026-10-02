"""PgTools.arun: output, masked errors, timeout and cancellation stop the child process (no orphan)."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from svbg.ops.pgtools import PgToolError, PgTools


@dataclass(frozen=True)
class PyTools(PgTools):
    """Every "tool" is the current Python interpreter: the args are a ``-c`` script."""

    def path(self, tool: str) -> str:
        return sys.executable


def _late_marker(marker: Path, delay: float) -> list[str]:
    """A child that would write ``marker`` after ``delay`` seconds unless it is stopped first."""
    script = f"import time, pathlib; time.sleep({delay}); pathlib.Path({str(marker)!r}).write_text('alive')"
    return ["-c", script]


async def test_arun_returns_stdout_and_writes_a_file(tmp_path: Path) -> None:
    tools = PyTools()
    assert (await tools.arun("x", ["-c", "print('hello')"], timeout=30)).strip() == "hello"
    out = tmp_path / "dump.bin"
    text = await tools.arun("x", ["-c", "import sys; sys.stdout.write('data')"], timeout=30, stdout_path=out)
    assert text == "" and out.read_bytes() == b"data"


async def test_arun_error_masks_the_password() -> None:
    script = "import os, sys; sys.stderr.write('auth failed for ' + os.environ['PGPASSWORD']); sys.exit(2)"
    with pytest.raises(PgToolError) as err:
        await PyTools().arun("pg_dump", ["-c", script], timeout=30, dsn_env={"PGPASSWORD": "s3cr3t-pass"})
    assert "s3cr3t-pass" not in str(err.value) and "auth failed" in str(err.value)


async def test_arun_missing_tool() -> None:
    with pytest.raises(PgToolError, match="не найден"):
        await PgTools(Path("/nonexistent-bin")).arun("pg_dump", [], timeout=5)


async def test_arun_timeout_kills_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    started = time.monotonic()
    with pytest.raises(PgToolError, match="не уложился"):
        await PyTools().arun("pg_dump", _late_marker(marker, 1.5), timeout=0.3, kill_grace=1.0)
    assert time.monotonic() - started < 1.4
    await asyncio.sleep(1.8)
    assert not marker.exists(), "the child kept running after the timeout"


async def test_cancel_stops_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    task = asyncio.ensure_future(
        PyTools().arun("pg_dump", _late_marker(marker, 1.5), timeout=60, kill_grace=1.0)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.8)
    assert not marker.exists(), "a cancelled backup must not leave pg_dump running"
