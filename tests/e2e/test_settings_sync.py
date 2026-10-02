"""Two-way ``.env`` sync on the running application (07 §5 stage 0 criteria):

* ``TRIAL_DAYS`` changed in the bot (``SettingsService.apply(source="bot")``) appears in the file ≤ 1 s;
* a manual edit of the file is applied ≤ 3 s and audited with ``source=env_file``;
* ``svbg set KEY=VALUE`` on the host reaches the running bot the same way;
* an invalid edit is not applied and gets a ``# ⚠`` note; the owner is told in DM.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest

from svbg.__main__ import main as cli_main
from svbg.boot.envfile import EnvDocument, write_atomic
from svbg.core.settings import Change
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, counting_db

pytestmark = pytest.mark.pg


def _file_value(app_env: AppEnv, key: str) -> str | None:
    try:
        return EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8")).get(key)
    except OSError:
        return None


async def _poll(predicate: object, timeout: float) -> float:
    began = time.perf_counter()
    while not predicate():  # type: ignore[operator]
        if time.perf_counter() - began > timeout:
            raise AssertionError(f"not reached within {timeout}s")
        await asyncio.sleep(0.01)
    return time.perf_counter() - began


async def test_bot_change_reaches_file_within_1s(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    assert app.settings is not None
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "3", 5)  # first render of the full file

    began = time.perf_counter()
    result = await app.settings.apply([Change("TRIAL_DAYS", "7")], source="bot", actor_id=None)
    assert result.ok, result.rejected
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "7", 1.0)
    elapsed = time.perf_counter() - began
    assert elapsed <= 1.0
    text = app_env.env_path.read_text(encoding="utf-8")
    assert "# ── Продажи и триал" in text  # full file by sections


async def test_file_edit_is_applied_within_3s(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    assert app.settings is not None and app.mirror is not None
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "3", 5)
    await asyncio.sleep(0.1)

    doc = EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8"))
    doc.set("TRIAL_DAYS", "9")
    began = time.perf_counter()
    write_atomic(app_env.env_path, doc.render())
    settings = app.settings
    await _poll(lambda: settings.current()["TRIAL_DAYS"] == 9, 3.0)
    assert time.perf_counter() - began <= 3.0
    assert settings.current().source("TRIAL_DAYS") == "env_file"
    rows = await counting_db(app.db).raw(
        "select source, applied from settings_audit where key = 'TRIAL_DAYS'"
    )
    assert [(r["source"], r["applied"]) for r in rows] == [("env_file", True)]


async def test_cli_set_reaches_running_bot(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    assert app.settings is not None
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "3", 5)
    await asyncio.sleep(0.1)
    out, err = io.StringIO(), io.StringIO()
    code = await asyncio.to_thread(
        cli_main,
        ["set", "TRIAL_DAYS=11"],
        environ={"DATA_DIR": str(app_env.data_dir)},
        stdout=out,
        stderr=err,
        configure_logging=False,
    )
    assert code == 0, err.getvalue()
    settings = app.settings
    await _poll(lambda: settings.current()["TRIAL_DAYS"] == 11, 3.5)


async def test_invalid_file_edit_is_annotated_and_reported(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    assert app.settings is not None
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "3", 5)
    await asyncio.sleep(0.1)
    doc = EnvDocument.parse(app_env.env_path.read_text(encoding="utf-8"))
    doc.set("TRIAL_DAYS", "9999")  # out of range 0..365
    write_atomic(app_env.env_path, doc.render())
    await _poll(lambda: "⚠" in app_env.env_path.read_text(encoding="utf-8"), 4.0)
    assert app.settings.current()["TRIAL_DAYS"] == 3
    notice = await app_env.tg.wait_for(
        "sendMessage",
        lambda c: c.params.get("chat_id") == OWNER_ID and "TRIAL_DAYS" in c.params["text"],
        timeout=5,
    )
    assert "не применено" in notice.params["text"]


async def test_owner_set_command_reaches_file_within_1s(start_app: StartApp, app_env: AppEnv) -> None:
    """The contract's "edit TRIAL_DAYS via bot command": ``/set TRIAL_DAYS 8`` from the owner's chat."""
    app = await start_app()
    if "svbg.tg.admin.settings" not in app.wired_modules:
        pytest.skip("settings screens are not installed")
    tg = app_env.tg
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "3", 5)
    began = time.perf_counter()
    tg.push_message(OWNER_ID, "/set TRIAL_DAYS 8")
    await _poll(lambda: _file_value(app_env, "TRIAL_DAYS") == "8", 1.0)
    assert time.perf_counter() - began <= 1.0
    assert app.settings is not None and app.settings.current()["TRIAL_DAYS"] == 8

    # A plain user cannot change settings with the same command.
    start = len(tg.calls)
    tg.push_message(12345, "/set TRIAL_DAYS 1")
    await asyncio.sleep(0.5)
    assert app.settings.current()["TRIAL_DAYS"] == 8
    assert _file_value(app_env, "TRIAL_DAYS") == "8"
    assert all(
        c.params.get("chat_id") != 12345 or "Применено" not in str(c.params.get("text"))
        for c in tg.calls[start:]
    )


async def test_owner_opens_settings_screen(start_app: StartApp, app_env: AppEnv) -> None:
    app = await start_app()
    if "svbg.tg.admin.settings" not in app.wired_modules:
        pytest.skip("settings screens are not installed")
    tg = app_env.tg
    tg.push_message(OWNER_ID, "/settings")
    call = await tg.wait_for("sendMessage", lambda c: c.params.get("chat_id") == OWNER_ID, timeout=10)
    assert "Настройки" in call.params["text"]
