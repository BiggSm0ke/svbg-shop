"""Squad contributors applied by the core (07 §2.4.3): forward/reverse, module states, fail-closed plan."""

from __future__ import annotations

from typing import Any

import pytest

from svbg.remnawave.contributors import (
    ATTENTION_PREFIX,
    ModuleState,
    SquadContributors,
    Substitution,
    forward,
    reverse,
    same_squads,
)
from tests.subscriptions.kit import sync_env

SUBS = [Substitution("base", "twin", "lte")]


def test_forward_and_reverse_are_inverse() -> None:
    assert forward(["base", "other"], SUBS) == ["twin", "other"]
    assert reverse(["twin", "other"], {"twin": "base"}) == ["base", "other"]
    assert forward(["base", "twin"], SUBS) == ["twin"]  # never a duplicate
    assert same_squads(["a", "b"], ["b", "a", "a"]) and not same_squads(["a"], ["b"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, ModuleState.OK),
        ("ok", ModuleState.OK),
        (ModuleState.DEGRADED, ModuleState.DEGRADED),
        (False, ModuleState.DEGRADED),
        ("weird", ModuleState.DEGRADED),
        (RuntimeError("boom"), ModuleState.DEGRADED),
    ],
)
def test_module_state(value: Any, expected: ModuleState) -> None:
    c = SquadContributors()

    def status() -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    c.register("lte", status)
    assert c.state("lte") is expected
    assert c.state("never_loaded") is ModuleState.UNLOADED


def test_plan_is_fail_closed() -> None:
    c = SquadContributors()
    unregister = c.register("lte", lambda: "ok")
    plan = c.plan(["base"], SUBS)
    assert plan.squads == ["twin"] and not plan.frozen
    unregister()
    frozen = c.plan(["base"], SUBS)
    assert frozen.squads is None and frozen.frozen_modules == ("lte",)
    # No live substitution, but the panel still shows a twin of a module that is down: still frozen.
    assert c.plan(["base"], [], panel_squads=["twin"], twin_owners={"twin": "lte"}).frozen
    assert c.plan(["base"], []).squads == ["base"]
    assert c.plan([], []).squads is None  # never an empty list


async def test_frozen_attention_counts_subscriptions(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        twin = env.panel.add_internal_squad("twin")
        a = await env.new_sub(1)
        b = await env.new_sub(2)
        await env.substitute(a, env.squad, twin)
        await env.substitute(b, env.squad, twin)
        assert await env.contributors.refresh_attention(env.db) == ["lte"]
        item = await env.attention.get(ATTENTION_PREFIX + "lte")
        assert item is not None and "2 подписок" in item.title and item.fix_action == "screen:status"
        env.register_module("lte", "ok")
        assert await env.contributors.refresh_attention(env.db) == []
        item = await env.attention.get(ATTENTION_PREFIX + "lte")
        assert item is not None and item.resolved_at is not None
