"""``svbg lte release`` (05 §2.1.6 «аварийно»): database modes through the core writer, the panel-only mode
with the "twin → base" map built from the panel, never an empty squad set."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from svbg.ext.lte import cli
from svbg.ext.lte.cli import panel_desired, panel_twin_bases
from tests.ext.lte.rkit import GB, LteEnv, lte_env

SQUADS = {
    "base-nl": ("NL", frozenset({"main", "lte"})),
    "twin-nl": ("NL noLTE", frozenset({"main"})),
    "base-de": ("DE", frozenset({"de", "lte"})),
    "twin-de": ("DE noLTE", frozenset({"de"})),
    "big": ("ALL", frozenset({"main", "de", "lte"})),
}


def test_twin_bases_from_the_panel() -> None:
    bases, warnings = panel_twin_bases(SQUADS, twin_suffix="noLTE")
    # "twin-nl" ⊂ NL and ⊂ ALL: ambiguous, skipped; "twin-de" ⊂ DE and ⊂ ALL as well
    assert bases == {} and len(warnings) == 2
    bases, warnings = panel_twin_bases({k: v for k, v in SQUADS.items() if k != "big"}, twin_suffix="nolte")
    assert bases == {"twin-nl": "base-nl", "twin-de": "base-de"} and warnings == []
    pinned, warnings = panel_twin_bases(SQUADS, base_map={"TWIN-NL": "base-nl", "nope": "base-nl"})
    assert pinned == {"twin-nl": "base-nl"} and len(warnings) == 1
    assert panel_twin_bases(SQUADS) == ({}, [])


def test_panel_desired_replaces_twins_and_never_writes_empty() -> None:
    bases = {"twin-nl": "base-nl"}
    assert panel_desired(["twin-nl", "x"], bases) == ["base-nl", "x"]
    assert panel_desired(["twin-nl", "base-nl"], bases) == ["base-nl"]  # no duplicates
    assert panel_desired(["x"], bases) is None
    assert panel_desired([], bases) is None


def test_parser_and_base_map() -> None:
    parser = argparse.ArgumentParser()
    cli.add_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["lte", "release"])
    assert args.mode == "due" and args.handler is cli.cmd_release
    assert parser.parse_args(["lte", "release", "--all", "--yes"]).mode == "all"
    assert parser.parse_args(["lte", "release", "--panel-only", "--base-map", "a=b"]).base_map == ["a=b"]
    with pytest.raises(SystemExit):
        parser.parse_args(["lte", "release", "--all", "--due-only"])
    assert cli._base_map(["t=b", " x = y "]) == {"t": "b", "x": "y"}
    with pytest.raises(cli._Fail):
        cli._base_map(["nothing"])


@dataclass
class Io:
    out: list[str] = field(default_factory=list)
    err: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.out.append(text)

    def warn(self, text: str) -> None:
        self.err.append(text)


async def _blocks(env: LteEnv) -> tuple[int, int]:
    due = await env.linked_sub(801)
    await env.open_period(due, used=11 * GB)
    later = await env.linked_sub(802)
    await env.open_period(later, used=11 * GB)
    for sid in (due, later):
        await env.service.process_subscription(sid)
    await env.drain()
    await env.db.raw(
        "update lte_periods set planned_end_at = now() - interval '1 minute' where subscription_id = $1", due
    )
    return due, later


@pytest.mark.pg
async def test_release_db_due_only_then_all(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        due, later = await _blocks(env)
        dry = await cli.release_db(env.db, mode="due", dry_run=True)
        assert (dry.blocks, dry.dry_run) == (1, True)
        assert {b["status"] for b in await env.blocks()} == {"active"}
        report = await cli.release_db(env.db, mode="due")
        assert report.blocks == 1
        assert [b["status"] for b in await env.blocks(due)] == ["releasing"]
        assert [b["status"] for b in await env.blocks(later)] == ["active"]
        await env.drain()
        assert await env.panel_squads(due) == [env.base] and await env.panel_squads(later) == [env.twin]
        report = await cli.release_db(env.db, mode="all")
        assert report.blocks == 1 and report.twins_dropped == 1  # nothing uses the twin map any more
        await env.drain()
        assert await env.panel_squads(later) == [env.base]
        audit = await env.db.raw("select reason from admin_audit where action = 'lte.cli_release'")
        assert [r["reason"] for r in audit] == ["svbg lte release --due", "svbg lte release --all"]


@pytest.mark.pg
async def test_release_panel_without_the_database(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = (await _blocks(env))[1]
        lone = env.panel.add_user(username="lone", activeInternalSquads=[env.twin])
        dry = await cli.release_panel(env.rw.api, twin_suffix="noLTE", dry_run=True)
        assert dry.bases == {env.twin: env.base} and dry.users == 3 and dry.patched == 0
        # default write: the core writer's out-of-band one (writer.emergency_set_squads)
        if cli.emergency_writer() is None:
            unwired = await cli.release_panel(env.rw.api, twin_suffix="noLTE", write=None)
            assert unwired.patched == 0 and any("не подключена" in w for w in unwired.warnings)
            report = await cli.release_panel(env.rw.api, twin_suffix="noLTE", write=_patch)
        else:
            report = await cli.release_panel(env.rw.api, twin_suffix="noLTE", write=None)
        assert report.patched == 3 and report.failed == 0
        assert await env.panel_squads(sid) == [env.base]
        squads = env.panel.users[lone["id"]]["activeInternalSquads"]
        assert [s["uuid"] if isinstance(s, dict) else s for s in squads] == [env.base]
        empty = await cli.release_panel(env.rw.api)
        assert empty.users == 0 and empty.warnings


@pytest.mark.pg
async def test_cmd_release_on_the_database(pg_dsn: str, tmp_path: Path) -> None:
    async with lte_env(pg_dsn) as env:
        await _blocks(env)
        parser = argparse.ArgumentParser()
        cli.add_commands(parser.add_subparsers(dest="command"))
        environ = {"DATA_DIR": str(tmp_path), "DATABASE_URL": pg_dsn}
        io = Io()
        args = parser.parse_args(["lte", "release", "--all", "--dry-run"])
        assert await _in_thread(cli.cmd_release, args, environ, io) == 0
        assert "Будет снято блоков: 2" in io.out[-1]
        io = Io()
        args = parser.parse_args(["lte", "release", "--all"])  # no tty, no --yes: refused
        assert await _in_thread(cli.cmd_release, args, environ, io) == 2
        args = parser.parse_args(["lte", "release", "--all", "--yes"])
        assert await _in_thread(cli.cmd_release, args, environ, io) == 0
        assert "Снято блоков: 2" in io.out[-1]
        io = Io()
        args = parser.parse_args(["lte", "release", "--panel-only", "--yes"])
        assert await _in_thread(cli.cmd_release, args, {"DATA_DIR": str(tmp_path)}, io) == 78


async def _patch(api: Any, panel_user_id: int, squads: list[str]) -> None:
    """What ``svbg.remnawave.writer.emergency_set_squads`` does (the integration adds it to the writer)."""
    await api.update_user(panel_user_id, active_internal_squads=squads)


async def _in_thread(fn: object, *args: object) -> int:
    """``cmd_release`` runs its own event loop (``asyncio.run``), like the real CLI."""
    import asyncio

    return int(await asyncio.to_thread(fn, *args))  # type: ignore[arg-type]
