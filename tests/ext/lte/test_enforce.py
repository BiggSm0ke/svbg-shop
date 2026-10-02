"""LTE enforcement through the core (05 §2.1.10, 07 §2.4.3): one transaction with the substitution rows, the
core
writer applies the twin, confirm / release / re-send in 150 s, never an empty squad set."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from svbg.ext.lte.decide import BlockCandidate, Release, Twin
from svbg.ext.lte.enforce import RESEND_DELAY, block_ref, fallback_substitutions, manual_candidate
from tests.ext.lte.rkit import GB, lte_env

pytestmark = pytest.mark.pg


async def test_block_and_release_go_through_the_core_writer(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(101)
        pid = await env.open_period(sid, used=11 * GB)
        applied = await env.service.process_subscription(sid)
        assert applied is not None and len(applied.placed) == 1
        (block,) = await env.blocks(sid)
        assert block["status"] == "active" and block["mode"] == "enforce" and block["reason"] == "quota"
        subs = await env.substitutions(sid)
        assert [(s["base_squad_uuid"], s["substitute_squad_uuid"], s["owner_module"]) for s in subs] == [
            (env.base, env.twin, "lte")
        ]
        assert subs[0]["source_ref"] == block_ref(block["id"])
        await env.drain()
        assert await env.panel_squads(sid) == [env.twin]
        (block,) = await env.blocks(sid)
        assert block["applied_at"] is not None  # lte.confirm saw the twin in the panel snapshot
        # the user is told once, silently, with the connect button
        assert len(env.notifier.sent) == 1
        sent = env.notifier.sent[0]
        assert "исчерпан" in sent.text and sent.kwargs["disable_notification"] is True

        # usage drops under the limit (e.g. a correction) → release, base squad back, re-send in 150 s
        await env.set_used(pid, 2 * GB)
        await env.service.process_subscription(sid)
        assert await env.substitutions(sid) == []
        (block,) = await env.blocks(sid)
        assert block["status"] == "releasing" and block["release_reason"] == "limit_change"
        await env.drain()
        assert await env.panel_squads(sid) == [env.base]
        (block,) = await env.blocks(sid)
        assert block["status"] == "released"
        rows = await env.db.raw(
            "select next_run_at as run_at from jobs where kind = 'lte.resend' and status = 'ready'"
        )
        assert len(rows) == 1
        assert rows[0]["run_at"] - datetime.now(UTC) > RESEND_DELAY - timedelta(seconds=30)
        assert env.panel.squad_resends == []
        await env.drain(make_due=True)
        assert env.panel.squad_resends == [(env.base, [await env.panel_id(sid)])]
        (block,) = await env.blocks(sid)
        assert block["resend_done_at"] is not None


async def test_block_rows_and_substitutions_commit_together(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(102)
        pid = await env.open_period(sid, used=11 * GB)
        cand = BlockCandidate(
            sid, await env.panel_id(sid), env.group_id, pid, "enforce", "quota", 11 * GB, 10 * GB
        )
        twins = {env.base: Twin(env.base, env.group_id, env.twin)}
        with pytest.raises(RuntimeError):
            async with env.db.tx() as conn:
                placed = await env.service.enforcer.block(conn, cand, desired=[env.base], twins=twins)
                assert placed is not None
                raise RuntimeError("crash between the two writes")
        assert await env.blocks(sid) == [] and await env.substitutions(sid) == []
        assert "panel.update" not in await env.job_kinds()


async def test_no_healthy_twin_means_no_block_and_an_alert(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        await env.db.raw("update lte_twins set problem = 'twin_empty'")
        sid = await env.linked_sub(103)
        await env.open_period(sid, used=11 * GB)
        applied = await env.service.process_subscription(sid)
        assert applied is not None and applied.placed == [] and applied.unenforceable == [(sid, env.group_id)]
        assert await env.blocks(sid) == [] and await env.substitutions(sid) == []
        assert f"lte:unenforceable:{sid}" in await env.rw.attention_keys()


def test_fallback_substitutions_skip_broken_twins() -> None:
    twins = {
        "a": Twin("a", 1, "a-twin"),
        "b": Twin("b", 1, "b-twin", "twin_empty"),
        "c": Twin("c", 2, "c-twin"),
    }
    assert fallback_substitutions(["a", "b", "c", "x"], {1}, twins) == {"a": "a-twin"}
    assert fallback_substitutions(["x"], {1}, twins) == {}


async def test_shadow_blocks_never_reach_the_panel(pg_dsn: str) -> None:
    async with lte_env(pg_dsn, LTE_ENFORCE="shadow") as env:
        sid = await env.linked_sub(104)
        await env.open_period(sid, used=11 * GB)
        await env.service.process_subscription(sid)
        (block,) = await env.blocks(sid)
        assert block["mode"] == "shadow" and block["applied_at"] is not None
        assert await env.substitutions(sid) == []
        assert "panel.update" not in await env.job_kinds()
        assert env.notifier.sent == []


async def test_release_all_releases_in_batches_and_shadow_at_once(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        a = await env.linked_sub(105)
        b = await env.linked_sub(106)
        pa = await env.open_period(a, used=11 * GB)
        pb = await env.open_period(b, used=1 * GB)
        await env.service.process_subscription(a)
        async with env.db.tx() as conn:
            cand = manual_candidate(
                subscription_id=b,
                panel_user_id=await env.panel_id(b),
                group_id=env.group_id,
                period_id=pb,
                used=GB,
                limit=10 * GB,
                mode="shadow",
            )
            await env.service.enforcer.block(conn, cand, desired=[env.base], twins={})
        del pa
        await env.drain()
        n = await env.service.release_all("emergency")
        assert n == 2
        assert {b_["status"] for b_ in await env.blocks()} == {"releasing", "released"}
        await env.drain()
        assert {b_["status"] for b_ in await env.blocks()} == {"released"}
        assert await env.panel_squads(a) == [env.base]
        assert any("блоки сняты" in text for _, text in env.admin_chat.posts)
        audit = await env.db.raw("select action, details from admin_audit where action = 'lte.release_all'")
        assert audit and audit[0]["details"]["released"] == 2


async def test_release_of_a_gone_block_is_a_noop(pg_dsn: str) -> None:
    async with lte_env(pg_dsn) as env:
        sid = await env.linked_sub(107)
        async with env.db.tx() as conn:
            assert not await env.service.enforcer.release(conn, Release(999, sid, 0, env.group_id, "topup"))
