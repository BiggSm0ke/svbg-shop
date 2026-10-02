"""The ops module inside the real application (``AppOptions.optional_modules``): real AppDeps, fake Telegram.

``svbg.ops.module`` is one of ``OPTIONAL_UI_MODULES``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from svbg.app import OPTIONAL_UI_MODULES
from svbg.ops.module import A_BACKUP, ACTIONS, SCREEN
from svbg.ops.pgtools import PgTools
from svbg.tg.ui.codec import encode
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, app_env, e2e_dsn, start_app, tg  # noqa: F401
from tests.fakes.telegram import FakeTelegram

pytestmark = pytest.mark.pg


async def test_ops_module_in_the_app(
    start_app: StartApp,  # noqa: F811
    app_env: AppEnv,  # noqa: F811
    tg: FakeTelegram,  # noqa: F811
    pg_tools: PgTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if pg_tools.bindir is not None:
        monkeypatch.setenv("SVBG_PG_BIN", str(pg_tools.bindir))
    assert "svbg.ops.module" in OPTIONAL_UI_MODULES
    app = await start_app()
    assert "svbg.ops.module" in app.wired_modules, app.missing_modules
    assert app.scheduler is not None
    assert {"ops.backup.tick", "ops.report.tick", "ops.updates"} <= set(app.scheduler.tasks())

    start = len(tg.calls)
    tg.push_callback(OWNER_ID, encode(SCREEN), 1)
    screen = await tg.wait_for(
        "sendMessage|sendPhoto",
        lambda c: c.params.get("chat_id") == OWNER_ID and "Бэкапы и обновления" in c.text,
        timeout=10,
        start=start,
    )
    assert "Пароль бэкапов не задан" in screen.text

    start = len(tg.calls)
    tg.push_callback(OWNER_ID, encode(ACTIONS, A_BACKUP), screen.result["message_id"])
    toast = await tg.wait_for("answerCallbackQuery", lambda c: True, timeout=10, start=start)
    assert "Бэкап запущен" in toast.text
    # No password, no admin group: the backup stays local and the owner gets a DM about it.
    done = await tg.wait_for(
        "sendMessage|sendPhoto",
        lambda c: c.params.get("chat_id") == OWNER_ID and "Бэкап готов" in c.text,
        timeout=60,
        start=start,
    )
    assert "BACKUP_PASSWORD" in done.text
    backups = list(Path(app_env.data_dir, "backups").glob("svbg-*-manual.tar.gz"))
    assert len(backups) == 1
    assert tg.calls_for("sendDocument") == [], "unencrypted backups never go to Telegram"
