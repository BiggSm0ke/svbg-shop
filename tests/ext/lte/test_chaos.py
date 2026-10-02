"""Chaos tests of 07 §2.4.3 p.5 with the real module manifest in the extension host.

The LTE module throws / did not load / is switched off → a renewal PATCH of a blocked user does not touch
``activeInternalSquads`` (the twin stays in the panel), the reconciliation writes nothing for that user, and
after the module recovers there are 0 discrepancies. Switching the module off is never silent: «Снять»
gives the base squads back through the writer, «Оставить» keeps the twin and freezes the squads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import pytest

from svbg.core.errors.breaker import CircuitBreaker
from svbg.ext.api import ExtensionHost, ModuleState
from svbg.ext.lte.admin import LteAdmin
from svbg.ext.lte.service import K_ENABLED, RUNTIME, SPEC, enqueue_term
from svbg.remnawave.sync import Reconciler
from svbg.remnawave.writer import enqueue_update
from svbg.tg.ui.context import UserCtx
from tests.ext.lte.rkit import GB, LteEnv, lte_env

pytestmark = pytest.mark.pg

OWNER = UserCtx(user_id=1, role="owner")


@dataclass
class FakeSettings:
    """``SettingsService.apply`` over the test config (the host re-syncs like the real subscription)."""

    cfg: dict[str, Any]
    host: ExtensionHost | None = None
    applied: list[Mapping[str, Any]] = field(default_factory=list)

    async def apply(self, changes: Any, *, source: str, actor_id: int | None) -> Any:
        del source, actor_id
        values = {c.key: c.raw for c in changes}
        self.cfg.update(values)
        self.applied.append(values)
        if self.host is not None:
            await self.host.sync()

        class _Ok:
            ok = True
            rejected: Mapping[str, str] = MappingProxyType({})

        return _Ok()


def _reconciler(env: LteEnv) -> Reconciler:
    rw = env.rw
    return Reconciler(
        rw.db,
        rw.current_api,
        contributors=rw.contributors,
        attention=rw.attention,
        bus=rw.bus,
        webhooks_enabled=lambda: True,
    )


async def _host(env: LteEnv, settings: FakeSettings | None = None) -> ExtensionHost:
    """The real ``SPEC`` in a host whose liveness feeds the core contributors (instead of the kit's stub)."""
    env.rw.contributors._status.pop("lte", None)
    deps: dict[str, Any] = {
        "db": env.db,
        "api": env.rw.current_api,
        "attention": env.rw.attention,
        "admin_chat": env.admin_chat,
        "notifier": env.notifier,
    }
    if settings is not None:
        deps["settings"] = settings
    host = ExtensionHost(
        [SPEC],
        deps=deps,
        config=lambda: env.config,
        breaker_factory=lambda name: CircuitBreaker(name, threshold=1),
    )
    host.install_contributors(env.rw.contributors)
    if settings is not None:
        settings.host = host
    await host.start()
    assert host.state("lte") is ModuleState.ACTIVE
    return host


async def _blocked_user(env: LteEnv, tg: int) -> int:
    sid = await env.linked_sub(tg)
    await env.open_period(sid, used=11 * GB)
    await RUNTIME.service().process_subscription(sid)
    await env.drain()
    assert await env.panel_squads(sid) == [env.twin]
    return sid


@pytest.mark.parametrize("failure", ["raises", "failed", "disabled"])
async def test_module_down_keeps_the_twin_and_recovers_with_zero_drift(pg_dsn: str, failure: str) -> None:
    async with lte_env(pg_dsn) as env:
        host = await _host(env)
        sid = await _blocked_user(env, 501)
        ctx = host.context("lte")

        if failure == "raises":
            jobs: dict[str, Any] = {}
            host.install_jobs(jobs)

            async def boom(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("LTE упал")

            service = RUNTIME.service()
            original = service.process_subscription
            service.process_subscription = boom  # type: ignore[method-assign]
            async with env.db.tx() as conn:
                await enqueue_term(conn, sid)
            from svbg.jobs.queue import JobQueue
            from svbg.jobs.worker import JobContext

            queue = JobQueue(env.db)
            (job,) = [j for j in await queue.claim("interactive", "chaos", 10) if j.kind == "lte.term"]
            for _ in range(2):  # more errors than the breaker threshold
                with pytest.raises(RuntimeError):
                    await jobs["lte.term"](job, JobContext(db=env.db, queue=queue, worker_id="chaos"))
            await queue.fail(job.id, "boom", permanent=True, worker_id="chaos", attempt=job.attempts)
            assert host.state("lte") is ModuleState.DEGRADED
        elif failure == "failed":
            env.config[K_ENABLED] = False
            await host.sync()
            host.attach(deps={"db": None})
            env.config[K_ENABLED] = True
            await host.sync()
            assert host.state("lte") is ModuleState.FAILED
        else:
            env.config[K_ENABLED] = False
            await host.sync()
            assert host.state("lte") is ModuleState.DISABLED

        # a renewal and a plan change of the blocked user while the module is down
        env.panel.requests.clear()
        async with env.db.tx() as conn:
            await env.rw.service.change(conn, sid, desired_squads=[env.base])
        async with env.db.tx() as conn:
            await env.rw.service.renew(conn, sid, 30)
        await env.drain()
        patches = [r.body for r in env.panel.calls("/users", "PATCH")]
        assert patches and all("activeInternalSquads" not in b for b in patches), patches
        assert any("expireAt" in b for b in patches)
        assert await env.panel_squads(sid) == [env.twin], "двойник снят молча"
        assert "rw:squads_frozen:lte" in await env.rw.attention_keys()

        # the reconciliation writes nothing to the panel for this user
        env.panel.requests.clear()
        report = await _reconciler(env).full_pass()
        assert report.status == "ok" and report.drift == 0
        assert [r for r in env.panel.requests if r.method != "GET"] == []
        assert await env.panel_squads(sid) == [env.twin]

        # recovery
        if failure == "raises":
            service.process_subscription = original  # type: ignore[method-assign]
            ctx.breaker.reset()
        elif failure == "failed":
            host.attach(deps={"db": env.db})
            await host.restart("lte")
        else:
            env.config[K_ENABLED] = True
            await host.sync()
        assert host.state("lte") is ModuleState.ACTIVE
        assert await env.rw.contributors.refresh_attention(env.db) == []
        await env.drain()
        async with env.db.tx() as conn:
            await enqueue_update(conn, sid, ["squads"])
        env.panel.requests.clear()
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body["activeInternalSquads"] == [env.twin]
        report = await _reconciler(env).full_pass()
        assert report.status == "ok" and report.drift == 0, "после восстановления расхождений 0"
        (block,) = await env.blocks(sid)
        assert block["status"] == "active"
        assert await env.panel_squads(sid) == [env.twin]


async def test_switch_off_with_release_gives_the_base_back_through_the_writer(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        settings = FakeSettings(env.config)
        host = await _host(env, settings)
        sid = await _blocked_user(env, 502)
        res = await LteAdmin(RUNTIME.service()).switch_off("release", actor=OWNER)
        assert res.ok, res.text
        assert settings.applied == [{K_ENABLED: False}]
        assert host.state("lte") is ModuleState.DISABLED
        assert await env.substitutions(sid) == []
        await env.drain()  # the writer runs while the module is off: nothing is frozen any more
        assert await env.panel_squads(sid) == [env.base]
        (block,) = await env.blocks(sid)
        assert block["status"] == "released" and block["release_reason"] == "feature_off"
        assert "rw:squads_frozen:lte" not in await env.rw.attention_keys()
        assert (await _reconciler(env).full_pass()).drift == 0
        audit = await env.db.raw("select details from admin_audit where action = 'lte.switch_off'")
        assert audit[0]["details"]["action"] == "release" and audit[0]["details"]["n"] == 1


async def test_switch_off_with_keep_leaves_the_blocks_frozen(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        settings = FakeSettings(env.config)
        host = await _host(env, settings)
        sid = await _blocked_user(env, 503)
        res = await LteAdmin(RUNTIME.service()).switch_off("keep", actor=OWNER)
        assert res.ok and "оставлены (1)" in res.text
        assert host.state("lte") is ModuleState.DISABLED
        assert len(await env.substitutions(sid)) == 1
        async with env.db.tx() as conn:
            await env.rw.service.renew(conn, sid, 30)
        await env.drain()
        assert await env.panel_squads(sid) == [env.twin]
        assert await env.rw.contributors.refresh_attention(env.db) == ["lte"]  # the periodic check
        assert "rw:squads_frozen:lte" in await env.rw.attention_keys()


async def test_switch_off_needs_the_config_right(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        settings = FakeSettings(env.config)
        await _host(env, settings)
        admin = UserCtx(user_id=2, role="admin", perms=frozenset({"*"}))
        res = await LteAdmin(RUNTIME.service()).switch_off("release", actor=admin)
        assert not res.ok and settings.applied == []
