"""Reconciliation: keyset pages of 500, zero writes when consistent, the safety fuse, no-webhook mode."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.remnawave.importer import PanelImporter
from svbg.remnawave.sync import FUSE_KEY, Reconciler
from tests.dbkit import SqlCounter
from tests.subscriptions.kit import SyncEnv, sync_env

pytestmark = pytest.mark.timeout(120)


def reconciler(env: SyncEnv, *, webhooks: bool = True) -> Reconciler:
    return Reconciler(
        env.db,
        env.current_api,
        contributors=env.contributors,
        attention=env.attention,
        bus=env.bus,
        webhooks_enabled=lambda: webhooks,
    )


def seed_panel(env: SyncEnv, n: int, *, start_tg: int = 1_000_000) -> None:
    expire = datetime.now(UTC) + timedelta(days=30)
    for i in range(n):
        env.panel.add_user(
            username=f"user_{i}",
            telegramId=start_tg + i if i % 3 else None,
            expireAt=expire,
            trafficLimitBytes=10 * 2**30,
            activeInternalSquads=[env.squad],
        )


async def imported(env: SyncEnv, n: int) -> None:
    seed_panel(env, n)
    report = await PanelImporter(env.db, env.current_api).run("apply")
    assert report.subscriptions_created == n


async def test_full_pass_uses_keyset_pages_of_500_and_writes_nothing(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await imported(env, 1200)
        env.panel.requests.clear()
        report = await reconciler(env).full_pass()
        assert report.status == "ok" and report.pages == 3 and report.seen == 1200 and report.matched == 1200
        streams = env.panel.calls("/users/stream", "GET")
        assert [r.query.get("size") for r in streams] == ["500", "500", "500"]
        assert [r.query.get("cursor") for r in streams] == [None, "500", "1000"]
        assert not [r for r in env.panel.requests if r.method != "GET"], "сверка ничего не пишет в панель"
        assert report.updated == 0 and report.missing == 0 and report.drift == 0
        with SqlCounter(env.db.engine) as counter:
            again = await reconciler(env).full_pass()
        assert again.status == "ok"
        writes = [
            s
            for s in counter.recent
            if s.lstrip().upper().startswith(("UPDATE SUBSCRIPTIONS", "INSERT INTO SUBSCRIPTION"))
        ]
        assert writes == [], "согласованное состояние не порождает записей в БД"
        assert not await env.jobs()


async def test_fuse_on_empty_panel(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await imported(env, 10)
        env.panel.users.clear()
        snapshot = await env.db.raw("select * from subscriptions order by id")
        report = await reconciler(env).full_pass()
        assert report.status == "aborted" and report.reason == "empty"
        assert await env.db.raw("select * from subscriptions order by id") == snapshot
        assert FUSE_KEY in await env.attention_keys()
        confirms = [r for r in env.panel.calls("/users/*", "GET") if r.path != "/users/stream"]
        assert confirms == [], "ни одного подтверждения «удалён»"


async def test_fuse_on_foreign_panel(pg_dsn: str) -> None:
    """Same numeric ids, different people (another panel): nothing is projected, nothing marked missing."""
    async with sync_env(pg_dsn) as env:
        await imported(env, 20)
        for user in env.panel.users.values():
            user["username"] = "other_" + user["username"]
            user["trafficLimitBytes"] = 1
        snapshot = await env.db.raw("select * from subscriptions order by id")
        report = await reconciler(env).full_pass()
        assert report.status == "aborted" and report.reason == "coverage" and report.foreign == 20
        assert await env.db.raw("select * from subscriptions order by id") == snapshot
        assert not await env.jobs()


async def test_fuse_on_changed_subscription_domain(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await imported(env, 3)
        assert (await reconciler(env).full_pass()).status == "ok"
        env.panel.configuration["misc"]["subPublicDomain"] = "other-panel.example.org"
        report = await reconciler(env).full_pass()
        assert report.status == "aborted" and report.reason == "domain"
        assert FUSE_KEY in await env.attention_keys()


async def test_missing_users_are_confirmed_by_get(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await imported(env, 10)
        gone = min(env.panel.users)
        del env.panel.users[gone]
        report = await reconciler(env).full_pass()
        assert report.status == "ok" and report.missing == 1
        assert len(env.panel.calls(f"/users/{gone}", "GET")) == 1
        rows = await env.db.raw("select link_state from subscriptions where panel_user_id = $1", gone)
        assert rows[0]["link_state"] == "panel_missing"
        assert FUSE_KEY not in await env.attention_keys()


async def test_drift_from_released_substitution_is_fixed_by_the_writer(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        twin = env.panel.add_internal_squad("LTE-blocked")
        env.register_module("lte", "ok")
        sid = await env.linked_sub(77)
        pid = (await env.sub(sid))["panel_user_id"]
        await env.substitute(sid, env.squad, twin)
        env.panel_user(pid)["activeInternalSquads"] = [twin]
        assert (await reconciler(env).full_pass()).drift == 0  # twin present and expected
        await env.db.raw(
            "delete from panel_squad_substitutions where subscription_id = $1", sid
        )  # LTE released
        env.module_states["lte"] = "degraded"
        assert (await reconciler(env).full_pass()).drift == 0, "модуль недоступен — сквады заморожены"
        env.module_states["lte"] = "ok"
        report = await reconciler(env).full_pass()
        assert report.drift == 1 and (await env.sub(sid))["overrides"] == {}
        await env.drain()
        assert env.panel_user(pid)["activeInternalSquads"] == [env.squad]
        assert (await reconciler(env).full_pass()).drift == 0, "после восстановления расхождений 0"


async def test_pending_operation_is_not_fought(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(78, traffic_bytes=10)
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=20)  # queued, not yet written
        report = await reconciler(env).full_pass()
        assert report.status == "ok"
        row = await env.sub(sid)
        assert row["overrides"] == {} and row["desired_traffic_bytes"] == 20


async def test_no_webhook_mode_runs_fast_and_frequent_full_passes(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(79)
        pid = (await env.sub(sid))["panel_user_id"]
        rec = reconciler(env, webhooks=False)
        await rec.tick()
        assert rec.last["fast"].status == "ok" and rec.last["full"].status == "ok"
        env.panel_user(pid)["status"] = "LIMITED"
        env.panel.requests.clear()
        await rec.tick()  # full pass not due yet (15 min), fast pass catches LIMITED
        streams = env.panel.calls("/users/stream", "GET")
        assert [r.query.get("status") for r in streams] == ["LIMITED"]
        assert (await env.sub(sid))["panel_status"] == "LIMITED"
        assert any(e.name == "subscription.panel_status" for e in env.events)
        await env.db.raw(
            "update rw_sync_state set last_ok_at = now() - interval '16 minutes' where name = 'full'"
        )
        env.panel.requests.clear()
        await rec.tick()
        assert [r.query.get("status") for r in env.panel.calls("/users/stream", "GET")] == ["LIMITED", None]

        with_hooks = reconciler(env, webhooks=True)
        env.panel.requests.clear()
        await with_hooks.tick()
        assert env.panel.calls("/users/stream", "GET") == [], (
            "с вебхуками быстрый проход не нужен, полный — раз в час"
        )


async def test_one_pass_at_a_time(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await env.db.raw(
            "insert into rw_sync_state (name, holder, locked_until) "
            "values ('full', 'other', now() + interval '5 minutes')"
        )
        report = await reconciler(env).full_pass()
        assert report.status == "busy" and not env.panel.calls("/users/stream", "GET")
        await env.db.raw("update rw_sync_state set locked_until = now() - interval '1 second'")
        assert (await reconciler(env).full_pass()).status == "ok"


# ------------------------------------------------------------------------------------- review fixes


class _Proxy:
    """Forwards everything to ``inner``; subclasses override single methods."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_stale_page_does_not_roll_back_a_write_finished_meanwhile(pg_dsn: str) -> None:
    """A renew completes between the stream answer and the projection of that page: the page is older than
    the writer's snapshot, so it is skipped — no roll-back of ``paid_until``, no false «срок уменьшен»."""
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(300, days=10)
        inner = env.api
        renewed: list[bool] = []

        class RacingApi(_Proxy):
            async def stream(self, *args: Any, **kwargs: Any) -> Any:
                page = await inner.stream(*args, **kwargs)
                if not renewed:
                    renewed.append(True)
                    async with env.db.tx() as conn:
                        await env.service.renew(conn, sid, 30)
                    env.api = inner
                    assert [o for _, o in await env.drain()] == ["done"]
                    env.api = self  # type: ignore[assignment]
                return page

        env.api = RacingApi(inner)  # type: ignore[assignment]
        report = await reconciler(env).full_pass()
        assert report.status == "ok"
        row = await env.sub(sid)
        target = env.panel_user(row["panel_user_id"])["expireAt"]
        assert abs(row["paid_until"] - target) < timedelta(seconds=1), "оплата не откатилась"
        assert f"rw:expire_reduced:{sid}" not in await env.attention_keys()
        kinds = {r["kind"] for r in await env.db.raw("select kind from subscription_events")}
        assert "expire_reduced_in_panel" not in kinds


async def test_frozen_squads_change_is_applied_after_the_module_recovers(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        twin = env.panel.add_internal_squad("NL-LTE-blocked")
        other = env.panel.add_internal_squad("DE")
        env.register_module("lte", "ok")
        sid = await env.linked_sub(310)
        pid = (await env.sub(sid))["panel_user_id"]
        await env.substitute(sid, env.squad, twin)
        env.module_states["lte"] = "degraded"
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_squads=[other])
        await env.drain()
        assert env.panel_user(pid)["activeInternalSquads"] == [env.squad], "сквады заморожены"
        assert (await env.sub(sid))["overrides"] == {"_squads_pending": True}
        rec = reconciler(env)
        assert (await rec.full_pass()).status == "ok"
        row = await env.sub(sid)
        assert "squads" not in row["overrides"], "своя отложенная смена — не ручная правка"
        assert all(j["status"] == "done" for j in await env.jobs("panel.update"))
        env.module_states["lte"] = "ok"
        await rec.tick()  # the module is back: the held change is queued
        await env.drain()
        assert env.panel_user(pid)["activeInternalSquads"] == [other]
        assert (await env.sub(sid))["overrides"] == {}
        report = await rec.full_pass()
        assert report.drift == 0 and (await env.sub(sid))["overrides"] == {}


async def test_domain_change_is_confirmed_by_the_owner(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        await imported(env, 3)
        rec = reconciler(env)
        assert (await rec.full_pass()).status == "ok"
        env.panel.configuration["misc"]["subPublicDomain"] = "sub.new-domain.example.org"
        aborted = await rec.full_pass()
        assert aborted.status == "aborted" and aborted.reason == "domain"
        assert aborted.domain == "sub.new-domain.example.org"
        item = await env.attention.get(FUSE_KEY)
        assert item is not None and item.fix_action == "screen:rw_sync_confirm"
        assert "подтвердить" in item.body
        wrong = await rec.confirm_domain("something-else.example.org")
        assert wrong.status == "aborted", "подтверждается только тот домен, который видел владелец"
        ok = await rec.confirm_domain("sub.new-domain.example.org")
        assert ok.status == "ok"
        assert FUSE_KEY not in await env.attention_keys()
        assert (await rec.full_pass()).status == "ok", "новый домен запомнен"


async def test_hot_swap_mid_pass(pg_dsn: str) -> None:
    """Same panel, new session (e.g. a new token): the pass continues on the new API object. Another panel
    address: the pass stops quietly (``busy``) and nothing is written."""
    from svbg.remnawave.api import RemnawaveApi
    from svbg.remnawave.transport import Transport, TransportConfig

    async with sync_env(pg_dsn) as env:
        await imported(env, 5)
        old = env.api
        fresh = RemnawaveApi(
            Transport(TransportConfig(base_url=env.panel.url, token=env.panel.add_token(), backoff_base=0.01))
        )
        try:

            class SwappingApi(_Proxy):
                async def stream(self, *args: Any, **kwargs: Any) -> Any:
                    page = await old.stream(*args, **kwargs)
                    env.api = target  # the component swapped its client after this page
                    return page

            target: Any = fresh
            env.api = SwappingApi(old)  # type: ignore[assignment]
            rec = Reconciler(env.db, env.current_api, contributors=env.contributors, page_size=2)
            report = await rec.full_pass()
            assert report.status == "ok" and report.seen == 5

            class OtherPanel:
                transport = type("T", (), {"config": type("C", (), {"base_url": "https://other.example"})})()

            target = OtherPanel()
            env.api = SwappingApi(old)  # type: ignore[assignment]
            snapshot = await env.db.raw("select * from subscriptions order by id")
            report = await rec.full_pass()
            assert report.status == "busy" and "переподключена" in (report.reason or "")
            assert await env.db.raw("select * from subscriptions order by id") == snapshot
        finally:
            await fresh.transport.aclose()


async def test_closed_subscriptions_are_not_projected(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(320)
        async with env.db.tx() as conn:
            await env.service.close(conn, sid)
        await env.drain()
        pid = (await env.sub(sid))["panel_user_id"]
        env.panel_user(pid)["trafficLimitBytes"] = 999  # edited in the panel after closing
        before = await env.sub(sid)
        assert (await reconciler(env).full_pass()).status == "ok"
        after = await env.sub(sid)
        assert after["overrides"] == before["overrides"] and after["updated_at"] == before["updated_at"]
