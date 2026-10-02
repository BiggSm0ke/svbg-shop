"""Bootstrap: file > environ > default, LOCKED_KEYS, SECRET_KEY generation, waiting for BOT_TOKEN."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import stat
from pathlib import Path

import pytest

from svbg.boot.envfile import EnvDocument
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.bootstrap import (
    BootstrapError,
    default_env_path,
    load_bootstrap,
    read_bootstrap,
    wait_for_token,
)
from svbg.core.settings.registry import Registry, core_registry

TOKEN = "123456789:" + "A" * 35
TOKEN2 = "987654321:" + "B" * 35


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_priority_file_over_environ_over_default(env_path: Path) -> None:
    write(env_path, f"BOT_TOKEN={TOKEN}\n")
    cfg = read_bootstrap(env_path, {"BOT_TOKEN": TOKEN2, "OWNER_IDS": "1,2"})
    assert cfg.bot_token == TOKEN and cfg.sources["BOT_TOKEN"] == "env_file"
    assert cfg.owner_ids == [1, 2] and cfg.sources["OWNER_IDS"] == "environ"
    assert cfg.database_url is None and cfg.sources["DATABASE_URL"] == "default"
    assert cfg.file_exists and cfg.has_token


def test_locked_keys_take_environ(env_path: Path) -> None:
    write(env_path, f"BOT_TOKEN={TOKEN}\nLOCKED_KEYS=BOT_TOKEN,TRIAL_DAYS,NOT_A_KEY\n")
    cfg = read_bootstrap(env_path, {"BOT_TOKEN": TOKEN2})
    assert cfg.bot_token == TOKEN2 and cfg.sources["BOT_TOKEN"] == "locked"
    assert cfg.locked == frozenset({"BOT_TOKEN"})  # TRIAL_DAYS is not in the environment


def test_aliases_are_read(env_path: Path) -> None:
    reg = Registry()
    for defn in core_registry().all():
        reg.add(
            dataclasses.replace(defn, aliases=("TELEGRAM_BOT_TOKEN",)) if defn.key == "BOT_TOKEN" else defn
        )
    write(env_path, f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
    assert read_bootstrap(env_path, {}, registry=reg).bot_token == TOKEN


def test_invalid_values_become_problems_not_crashes(env_path: Path) -> None:
    write(env_path, "BOT_TOKEN=garbage\nOWNER_IDS=\nDATABASE_URL=sqlite:///x\n")
    cfg = read_bootstrap(env_path, {"BOT_TOKEN": TOKEN})
    assert cfg.bot_token == TOKEN and cfg.sources["BOT_TOKEN"] == "environ"  # falls through to environ
    assert "BOT_TOKEN" in cfg.problems and "garbage" not in cfg.problems["BOT_TOKEN"]
    assert cfg.owner_ids == []  # blank list is just "default"
    assert cfg.database_url is None and "DATABASE_URL" in cfg.problems


def test_missing_file_is_fine(env_path: Path) -> None:
    cfg = read_bootstrap(env_path, {})
    assert not cfg.file_exists and not cfg.has_token


def test_unreadable_file_raises_owner_message(env_path: Path) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_bytes(b"BOT_TOKEN=\xff\xfe\n")
    with pytest.raises(BootstrapError, match="UTF-8"):
        read_bootstrap(env_path, {})


def test_load_bootstrap_generates_and_persists_secret_key(env_path: Path) -> None:
    write(env_path, f"# мой комментарий\nBOT_TOKEN={TOKEN}\n")
    cfg = load_bootstrap(env_path, {})
    assert cfg.generated == ("SECRET_KEY",)
    Crypto([cfg.secret_key])  # a valid Fernet key
    text = env_path.read_text(encoding="utf-8")
    assert "# мой комментарий" in text and f"BOT_TOKEN={TOKEN}" in text
    doc = EnvDocument.parse(text)
    assert doc.get("SECRET_KEY") == cfg.secret_key
    assert "НЕ ТЕРЯЙТЕ" in text and "── Запуск" in text
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    again = load_bootstrap(env_path, {})
    assert again.secret_key == cfg.secret_key and again.generated == ()
    assert cfg.secret_key not in repr(cfg) and TOKEN not in repr(cfg)


def test_load_bootstrap_creates_missing_file_and_dir(env_path: Path) -> None:
    cfg = load_bootstrap(env_path, {})
    assert env_path.exists() and cfg.secret_key
    assert read_bootstrap(env_path, {}).secret_key == cfg.secret_key


def test_secret_key_from_environ_is_not_regenerated(env_path: Path) -> None:
    key = generate_key()
    cfg = load_bootstrap(env_path, {"SECRET_KEY": key})
    assert cfg.secret_key == key and cfg.generated == () and not env_path.exists()


def test_broken_secret_key_stops_bootstrap_without_leaking(env_path: Path) -> None:
    write(env_path, "SECRET_KEY=definitely-not-a-fernet-key\n")
    with pytest.raises(BootstrapError) as info:
        load_bootstrap(env_path, {})
    assert "definitely-not-a-fernet-key" not in str(info.value)
    assert "SECRET_KEY" in str(info.value)
    assert env_path.read_text(encoding="utf-8") == "SECRET_KEY=definitely-not-a-fernet-key\n"


def test_generated_key_that_cannot_be_saved_is_an_error(tmp_path: Path) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(BootstrapError, match="SECRET_KEY"):
        load_bootstrap(blocker / ".env", {})


def test_default_env_path() -> None:
    assert default_env_path({}) == Path("./data") / ".env"
    assert default_env_path({"DATA_DIR": "/srv/x"}) == Path("/srv/x") / ".env"


async def test_wait_for_token_picks_up_appended_token(
    env_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(env_path, "SECRET_KEY=" + generate_key() + "\n")
    hints: list[str] = []
    caplog.set_level(logging.INFO, logger="svbg")
    task = asyncio.create_task(wait_for_token(env_path, {}, interval=0.05, on_waiting=hints.append))
    await asyncio.sleep(0.2)
    assert not task.done()
    with env_path.open("a", encoding="utf-8") as fh:
        fh.write(f"BOT_TOKEN={TOKEN}\n")
    cfg = await asyncio.wait_for(task, 2)
    assert cfg.bot_token == TOKEN
    assert len(hints) == 1 and "BOT_TOKEN" in hints[0]
    assert TOKEN not in caplog.text


async def test_wait_for_token_survives_broken_file_and_times_out(env_path: Path) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_bytes(b"\xff")
    with pytest.raises(TimeoutError):
        await wait_for_token(env_path, {}, interval=0.02, max_wait=0.1)


async def test_wait_for_token_returns_immediately_when_present(env_path: Path) -> None:
    cfg = await wait_for_token(env_path, {"BOT_TOKEN": TOKEN}, interval=10)
    assert cfg.bot_token == TOKEN


def test_blank_value_in_file_falls_back_to_environ(env_path: Path) -> None:
    """The first-start template has ``DATABASE_URL=""``: that is "not set", the environment still applies."""
    write(env_path, 'DATABASE_URL=""\nBOT_TOKEN=\nTELEGRAM_PROXY=\nOWNER_IDS=\n')
    dsn = "postgresql://svbg@db/svbg"
    cfg = read_bootstrap(env_path, {"DATABASE_URL": dsn, "BOT_TOKEN": TOKEN, "OWNER_IDS": "7"})
    assert cfg.database_url == dsn and cfg.sources["DATABASE_URL"] == "environ"
    assert cfg.bot_token == TOKEN and cfg.sources["BOT_TOKEN"] == "environ"
    assert cfg.owner_ids == [7]
    assert cfg.get("TELEGRAM_PROXY") is None and cfg.sources["TELEGRAM_PROXY"] == "default"
    assert not cfg.problems


async def test_wait_for_token_regenerates_a_secret_key_removed_meanwhile(env_path: Path) -> None:
    first = load_bootstrap(env_path, {})
    assert first.secret_key and not first.has_token
    task = asyncio.create_task(wait_for_token(env_path, {}, interval=0.05))
    await asyncio.sleep(0.15)
    write(env_path, f"BOT_TOKEN={TOKEN}\n")  # the owner overwrote the file: SECRET_KEY is gone
    cfg = await asyncio.wait_for(task, 2)
    assert cfg.has_token and cfg.secret_key and cfg.generated == ("SECRET_KEY",)
    doc = EnvDocument.parse(env_path.read_text(encoding="utf-8"))
    assert doc.get("SECRET_KEY") == cfg.secret_key and doc.get("BOT_TOKEN") == TOKEN
