"""Panel import (02 §7.1): dry run, apply, idempotency, resume, 10k users with bounded memory."""

from __future__ import annotations

import gc
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest

from svbg.remnawave.importer import PanelImporter
from tests.subscriptions.kit import SyncEnv, sync_env

pytestmark = pytest.mark.timeout(300)


async def counts(env: SyncEnv) -> tuple[int, int]:
    rows = await env.db.raw("select (select count(*) from users) u, (select count(*) from subscriptions) s")
    return int(rows[0]["u"]), int(rows[0]["s"])


def seed(env: SyncEnv) -> None:
    expire = datetime.now(UTC) + timedelta(days=10)
    env.panel.add_user(username="a", telegramId=111, expireAt=expire, activeInternalSquads=[env.squad])
    env.panel.add_user(
        username="b", telegramId=111, expireAt=expire, status="DISABLED"
    )  # 2nd account, same tg
    env.panel.add_user(username="c", telegramId=None, expireAt=expire, hwidDeviceLimit=0)  # no Telegram
    env.panel.add_user(username="d", telegramId=222, expireAt=expire, tag="VIP", trafficLimitBytes=5)


async def test_dry_run_reports_and_writes_nothing(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        seed(env)
        await env.add_user(222)  # already a bot user
        report = await PanelImporter(env.db, env.current_api).run("dry_run")
        assert (report.total, report.with_telegram, report.without_telegram) == (4, 3, 1)
        assert report.users_created == 1 and report.subscriptions_created == 4 and report.finished
        assert await counts(env) == (1, 0)
        run = (await env.db.raw("select * from import_runs"))[0]
        assert run["mode"] == "dry_run" and run["status"] == "done" and run["report"]["total"] == 4


async def test_apply_links_claims_and_is_idempotent(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        seed(env)
        await env.add_user(222)
        first = await PanelImporter(env.db, env.current_api).run("apply")
        assert first.users_created == 1 and first.subscriptions_created == 4 and first.conflicts == 0
        rows = {
            r["panel_username"]: dict(r)
            for r in await env.db.raw(
                "select s.*, u.telegram_id owner_tg from subscriptions s "
                "left join users u on u.id = s.user_id"
            )
        }
        assert (
            rows["a"]["owner_tg"] == rows["b"]["owner_tg"] == 111
        )  # several accounts → several subscriptions
        assert rows["c"]["user_id"] is None  # unclaimed
        assert rows["c"]["desired_device_limit"] == 0
        assert rows["d"]["desired_tag"] == "VIP" and rows["d"]["desired_traffic_bytes"] == 5
        # Disabled in the panel = the panel admin's decision (02 §6.3): an override, not a bot disable.
        assert rows["b"]["desired_status"] == "active" and rows["b"]["disabled_reason"] is None
        assert rows["b"]["overrides"] == {"status": "DISABLED"} and rows["a"]["overrides"] == {}
        assert all(r["link_state"] == "linked" and r["plan_id"] is None for r in rows.values())
        assert rows["a"]["desired_squads"] == [env.squad] and rows["a"]["subscription_url"]
        events = await env.db.raw("select count(*) n from subscription_events where kind = 'imported'")
        assert events[0]["n"] == 4

        again = await PanelImporter(env.db, env.current_api).run("apply")
        assert again.users_created == 0 and again.subscriptions_created == 0 and again.already_linked == 4
        assert await counts(env) == (2, 4)
        assert not [r for r in env.panel.requests if r.method != "GET"], "импорт ничего не пишет в панель"
        assert not await env.jobs()


async def test_resume_from_cursor(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        for i in range(25):
            env.panel.add_user(username=f"u{i}", telegramId=5000 + i)
        importer = PanelImporter(env.db, env.current_api, page_size=10)

        # Let the first page through, then fail: the run stays "running" with its cursor saved.
        calls = {"n": 0}
        real_stream = env.api.stream

        async def flaky(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("обрыв посреди импорта")
            return await real_stream(*args, **kwargs)  # type: ignore[arg-type]

        env.api.stream = flaky  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await importer.run("apply")
        run = (await env.db.raw("select * from import_runs"))[0]
        assert run["status"] == "running" and run["cursor"] == "10"
        assert (await counts(env))[1] == 10
        report = await importer.run("apply", resume_run_id=int(run["id"]))
        assert report.subscriptions_created == 25 and report.finished
        assert (await counts(env))[1] == 25


async def test_filters_are_validated(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with pytest.raises(ValueError, match="filters"):
            await PanelImporter(env.db, env.current_api).run("dry_run", filters={"evil": 1})
        env.panel.add_user(username="x", telegramId=1, status="EXPIRED")
        env.panel.add_user(username="y", telegramId=2)
        report = await PanelImporter(env.db, env.current_api).run("apply", filters={"status": "EXPIRED"})
        assert report.total == 1 and report.subscriptions_created == 1


@pytest.mark.slow
async def test_import_10k_users_idempotent_with_bounded_memory(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        expire = datetime.now(UTC) + timedelta(days=30)
        for i in range(10_000):
            env.panel.add_user(
                username=f"user_{i}",
                telegramId=(700_000_000 + i // 2)
                if i % 5
                else None,  # some share a Telegram id, some have none
                expireAt=expire,
                trafficLimitBytes=50 * 2**30,
                activeInternalSquads=[env.squad],
                description="Иван Иванов " * 10,
            )
        importer = PanelImporter(env.db, env.current_api)
        await importer.run("dry_run")  # warm-up: imports, decoders, statement caches
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        try:
            baseline, _ = tracemalloc.get_traced_memory()
            report = await importer.run("apply")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        elapsed = time.perf_counter() - start
        grow_mb = (peak - baseline) / 2**20
        print(f"import 10k: {elapsed:.1f}s, peak +{grow_mb:.1f} MB")
        assert report.total == 10_000 and report.subscriptions_created == 10_000 and report.pages == 20
        assert grow_mb <= 30, f"пик памяти +{grow_mb:.1f} МБ"
        users_before, subs_before = await counts(env)
        expected_users = len({700_000_000 + i // 2 for i in range(10_000) if i % 5})
        assert subs_before == 10_000 and users_before == expected_users

        again = await importer.run("apply")
        assert (
            again.subscriptions_created == 0 and again.users_created == 0 and again.already_linked == 10_000
        )
        assert await counts(env) == (users_before, subs_before)
        assert not [r for r in env.panel.requests if r.method != "GET"]


async def test_hot_swap_during_import(pg_dsn: str) -> None:
    """The API is taken per page: the same panel on a new session continues; another panel address stops
    the run (resumable) instead of mixing the users of two panels."""
    from typing import Any

    from svbg.remnawave.errors import RemnawaveError

    async with sync_env(pg_dsn) as env:
        seed(env)
        inner = env.api
        swapped: list[Any] = []

        class OtherPanel:
            transport = type("T", (), {"config": type("C", (), {"base_url": "https://other.example"})})()

        class SwappingApi:
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            async def stream(self, *args: Any, **kwargs: Any) -> Any:
                page = await inner.stream(*args, **kwargs)
                env.api = swapped[0]
                return page

        swapped.append(OtherPanel())
        env.api = SwappingApi()  # type: ignore[assignment]
        with pytest.raises(RemnawaveError, match="адрес панели изменился"):
            await PanelImporter(env.db, env.current_api, page_size=2).run("apply")
        assert (await counts(env))[1] == 2, "импортирована только первая страница"
        run = (await env.db.raw("select id, status from import_runs"))[0]
        assert run["status"] == "running"

        env.api = inner
        report = await PanelImporter(env.db, env.current_api, page_size=2).run(
            "apply", resume_run_id=int(run["id"])
        )
        assert report.finished and (await counts(env))[1] == 4
