from __future__ import annotations

import sys
from typing import Any

import pytest

from svbg.tg.ui.conditions import (
    MAX_DEPTH,
    ConditionError,
    compile_condition,
    to_sql,
    validate_condition,
)
from svbg.tg.ui.context import UserCtx

BASE = UserCtx(
    user_id=1,
    telegram_id=100,
    role="admin",
    perms=frozenset({"stats"}),
    lang="ru",
    sub_state="active",
    days_left=3,
    balance_minor=15_000,
    has_paid=True,
    is_new=False,
    channel_member=True,
    ref_count=4,
    source="ads_vk",
    plan_code="month",
    flags=frozenset({"lte.blocked"}),
    segments=frozenset({"vip"}),
)


@pytest.mark.parametrize(
    ("dsl", "expected"),
    [
        (None, True),
        ({}, True),
        ({"role": "admin"}, True),
        ({"role": ["owner", "support"]}, False),
        ({"role": {"gte": "admin"}}, True),
        ({"role": {"gt": "admin"}}, False),
        ({"role": {"lt": "owner", "gte": "support"}}, True),
        ({"lang": "ru"}, True),
        ({"lang": ["en", "de"]}, False),
        ({"sub": "active"}, True),
        ({"sub": ["trial", "expired"]}, False),
        ({"days_left": 3}, True),
        ({"days_left": {"lte": 3}}, True),
        ({"days_left": {"lt": 3}}, False),
        ({"days_left": {"gt": 1, "lt": 5}}, True),
        ({"days_left": {"ne": 3}}, False),
        ({"balance_minor": {"gte": 15_000}}, True),
        ({"balance_minor": {"eq": 1}}, False),
        ({"ref_count": {"gte": 5}}, False),
        ({"has_paid": True}, True),
        ({"has_paid": False}, False),
        ({"is_new": False}, True),
        ({"channel_member": True}, True),
        ({"source": "ads_vk"}, True),
        ({"source": ["tg", "yt"]}, False),
        ({"plan": "month"}, True),
        ({"flag:lte.blocked": True}, True),
        ({"flag:lte.warn": True}, False),
        ({"flag:lte.warn": False}, True),
        ({"segment:vip": True}, True),
        ({"segment:churn": True}, False),
        ({"all": []}, True),
        ({"any": []}, False),
        ({"all": [{"sub": "active"}, {"days_left": {"lte": 3}}]}, True),
        ({"all": [{"sub": "active"}, {"days_left": {"lte": 2}}]}, False),
        ({"any": [{"sub": "trial"}, {"has_paid": True}]}, True),
        ({"any": [{"sub": "trial"}, {"has_paid": False}]}, False),
        ({"not": {"sub": "expired"}}, True),
        ({"not": {"any": [{"role": "admin"}]}}, False),
        ({"sub": "active", "lang": "en"}, False),  # several keys = implicit "all"
        ({"sub": "active", "lang": "ru"}, True),
    ],
)
def test_atoms_and_combinators(dsl: Any, expected: bool) -> None:
    assert compile_condition(dsl)(BASE) is expected


def test_missing_subscription_never_matches_numeric_days() -> None:
    nosub = UserCtx(user_id=2, sub_state="none", days_left=None, channel_member=None)
    assert compile_condition({"days_left": {"lte": 100}})(nosub) is False
    assert compile_condition({"days_left": {"gte": 0}})(nosub) is False
    assert compile_condition({"not": {"days_left": {"lte": 3}}})(nosub) is True
    # unknown channel membership counts as "not a member"
    assert compile_condition({"channel_member": False})(nosub) is True
    assert compile_condition({"channel_member": True})(nosub) is False


@pytest.mark.parametrize(
    ("dsl", "path"),
    [
        ({"weather": "sunny"}, "weather"),
        ({"all": [{"sub": "active"}, {"bogus": 1}]}, "all[1].bogus"),
        ({"all": {"sub": "active"}}, "all"),
        ({"not": [{"sub": "active"}]}, "not"),
        ({"sub": "paused"}, "sub"),
        ({"role": "god"}, "role"),
        ({"role": {"gte": "god"}}, "role.gte"),
        ({"role": {"approx": "admin"}}, "role"),
        ({"days_left": "3"}, "days_left"),
        ({"days_left": True}, "days_left"),
        ({"days_left": {"lte": 2.5}}, "days_left.lte"),
        ({"days_left": {"around": 3}}, "days_left"),
        ({"days_left": {}}, "days_left"),
        ({"has_paid": 1}, "has_paid"),
        ({"lang": ""}, "lang"),
        ({"lang": []}, "lang"),
        ({"flag:": True}, "flag:"),
        ({"flag:bad name": True}, "flag:bad name"),
        ({"segment:vip": "yes"}, "segment:vip"),
        (["sub"], "<root>"),
        ({"any": [{"sub": "active"}, "oops"]}, "any[1]"),
    ],
)
def test_invalid_conditions_fail_at_compile_time(dsl: Any, path: str) -> None:
    with pytest.raises(ConditionError) as ei:
        compile_condition(dsl)
    assert ei.value.path == path
    with pytest.raises(ConditionError):
        validate_condition(dsl)


def test_size_and_depth_limits() -> None:
    deep: dict[str, Any] = {"sub": "active"}
    for _ in range(MAX_DEPTH + 1):
        deep = {"not": deep}
    with pytest.raises(ConditionError, match="вложенность"):
        compile_condition(deep)
    wide = {"any": [{"all": [{"sub": "active"}] * 50} for _ in range(5)]}
    with pytest.raises(ConditionError, match="слишком большое"):
        compile_condition(wide)


def test_evaluation_does_no_io() -> None:
    """Compiled predicates only read UserCtx attributes: no DB, sockets or awaitables involved."""
    cond = compile_condition(
        {"all": [{"sub": "active"}, {"days_left": {"lte": 3}}, {"not": {"flag:lte.warn": True}}]}
    )
    before = set(sys.modules)
    calls = []

    def tracer(frame: Any, event: str, arg: Any) -> None:
        if event == "call":
            calls.append(frame.f_code.co_filename)

    sys.setprofile(tracer)
    try:
        assert cond(BASE) is True
    finally:
        sys.setprofile(None)
    assert set(sys.modules) == before
    assert all("conditions.py" in f or "<" in f for f in calls), calls


def test_to_sql_validates_and_rejects_runtime_atoms() -> None:
    # Full SQL semantics are checked against PostgreSQL in tests/broadcasts/test_segments_sql.py.
    with pytest.raises(ConditionError):
        to_sql({"bogus": 1})
    for dsl in ({"is_new": True}, {"flag:lte.blocked": True}, {"any": [{"segment:vip": True}]}):
        with pytest.raises(ConditionError):
            to_sql(dsl)
    assert to_sql({"sub": "active"}) is not None


def test_user_ctx_permissions_and_placeholders() -> None:
    owner = UserCtx(1, role="owner")
    admin = UserCtx(2, role="admin", perms=frozenset({"stats"}))
    support = UserCtx(3, role="support", perms=frozenset({"*"}))
    user = UserCtx(4, perms=frozenset({"*"}))
    assert owner.has_perm("anything")
    assert admin.has_perm("stats") and not admin.has_perm("broadcast")
    assert support.has_perm("broadcast")
    assert not user.has_perm("stats")  # plain users never get staff permissions
    assert admin.at_least("support") and not admin.at_least("owner")
    with pytest.raises(ValueError, match="role"):
        UserCtx(5, role="root")
    with pytest.raises(ValueError, match="subscription"):
        UserCtx(5, sub_state="paused")
    ph = UserCtx(6, days_left=None, balance_minor=17_950, currency="RUB").placeholders()
    assert ph["days_left"] == "—"
    assert "179,50" in ph["balance"]
    assert UserCtx(7, balance_minor=5, currency="???").placeholders()["balance"] == "5 ???"
