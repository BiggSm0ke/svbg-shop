"""Throwaway PostgreSQL cluster for tests.

Resolution order:
1. ``SVBG_TEST_DATABASE_URL`` — use an existing server (CI, docker). Each test DB is created there.
2. Local binaries: ``SVBG_PG_BIN`` or ``<repo>/.tools/pgsql/bin`` (Windows zip) or ``pg_ctl`` on PATH.
   A fresh cluster is initdb'ed in a temp dir, started on a free port and removed on exit.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_bin() -> Path | None:
    env = os.environ.get("SVBG_PG_BIN")
    if env:
        return Path(env)
    local = REPO / ".tools" / "pgsql" / "bin"
    if local.exists():
        return local
    found = shutil.which("pg_ctl")
    return Path(found).parent if found else None


@dataclass
class PgCluster:
    host: str
    port: int
    user: str
    password: str
    _datadir: Path | None = None
    _bin: Path | None = None

    def dsn(self, dbname: str = "postgres") -> str:
        auth = f"{self.user}:{self.password}@" if self.password else f"{self.user}@"
        return f"postgresql://{auth}{self.host}:{self.port}/{dbname}"

    def stop(self) -> None:
        if self._datadir is None or self._bin is None:
            return
        exe = self._bin / ("pg_ctl.exe" if os.name == "nt" else "pg_ctl")
        subprocess.run(
            [str(exe), "-D", str(self._datadir), "-m", "immediate", "-w", "stop"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(self._datadir.parent, ignore_errors=True)


def start_cluster() -> PgCluster:
    url = os.environ.get("SVBG_TEST_DATABASE_URL")
    if url:
        from urllib.parse import urlparse

        u = urlparse(url)
        return PgCluster(
            u.hostname or "127.0.0.1", u.port or 5432, u.username or "postgres", u.password or ""
        )

    bindir = _find_bin()
    if bindir is None:
        raise RuntimeError("PostgreSQL binaries not found: set SVBG_TEST_DATABASE_URL or SVBG_PG_BIN")
    ext = ".exe" if os.name == "nt" else ""
    root = Path(tempfile.mkdtemp(prefix="svbg-pg-"))
    datadir = root / "data"
    pwfile = root / "pw"
    pwfile.write_text("svbg", encoding="utf-8")
    subprocess.run(
        [
            str(bindir / f"initdb{ext}"),
            "-D",
            str(datadir),
            "-U",
            "svbg",
            "--pwfile",
            str(pwfile),
            "-A",
            "scram-sha-256",
            "-E",
            "UTF8",
            "--no-locale",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
    )
    port = _free_port()
    opts = " ".join(
        [
            f"-p {port}",
            "-c listen_addresses=127.0.0.1",
            "-c fsync=off",
            "-c synchronous_commit=off",
            "-c full_page_writes=off",
            "-c max_connections=200",
        ]
    )
    pg_ctl = [str(bindir / f"pg_ctl{ext}"), "-D", str(datadir), "-o", opts]
    subprocess.run(
        [*pg_ctl, "-l", str(root / "log.txt"), "-w", "start"],
        check=True,
        # pg_ctl's child (postgres) inherits the handles: capturing them would block until the server exits.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cluster = PgCluster("127.0.0.1", port, "svbg", "svbg", datadir, bindir)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return cluster
        time.sleep(0.1)
    cluster.stop()
    raise RuntimeError("PostgreSQL did not start")
