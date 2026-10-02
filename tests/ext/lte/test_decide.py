"""LTE decide: effective limits, the squad contribution (X1), invariants, "traffic after a block", model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from svbg.ext.lte import decide as d
from svbg.ext.lte import model

GB = 10**9
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def group(**values: object) -> d.GroupInput:
    values.setdefault("id", 1)
    values.setdefault("limit_rows", {"default": 50 * GB})
    return d.GroupInput(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- limits by presence of a row


def test_group_default_limit_is_used_when_no_user_rows() -> None:
    limit = d.effective_limit(group(), is_trial=False)
    assert (limit.found, limit.base, limit.limit, limit.shown) == (True, 50 * GB, 50 * GB, 50 * GB)
    assert not limit.unlimited


def test_trial_inherits_default_when_trial_row_is_absent() -> None:
    assert d.effective_limit(group(), is_trial=True).limit == 50 * GB


def test_trial_row_with_null_means_unlimited_even_with_paid_default() -> None:
    limit = d.effective_limit(group(limit_rows={"default": 50 * GB, "trial": None}), is_trial=True)
    assert limit.unlimited
    assert limit.limit is None
    assert limit.shown is None


def test_user_rows() -> None:
    assert d.effective_limit(group(), is_trial=False, user_rows={"all": None}).unlimited
    assert (
        d.effective_limit(group(), is_trial=False, user_rows={"all": 10 * GB, "paid": 30 * GB}).base
        == 30 * GB
    )
    assert d.effective_limit(group(), is_trial=False, user_rows={"trial": 1 * GB}).base == 50 * GB


def test_missing_limit_row_is_not_found_and_never_blocks() -> None:
    limit = d.effective_limit(group(limit_rows={}), is_trial=False)
    assert (limit.found, limit.unlimited, limit.zero) == (False, True, False)


def test_zero_limit_is_unavailable_and_ignores_margin_and_credits() -> None:
    limit = d.effective_limit(
        group(limit_rows={"default": 0}, margin_bytes=5 * GB), is_trial=False, credit_bytes=10 * GB
    )
    assert limit.zero
    assert not limit.unlimited


def test_credits_raise_limit_and_margin_lowers_it_but_user_sees_no_margin() -> None:
    limit = d.effective_limit(group(margin_bytes=2 * GB, margin_pct=10), is_trial=False, credit_bytes=25 * GB)
    assert limit.margin == 7 * GB
    assert limit.limit == 50 * GB + 25 * GB - 7 * GB
    assert limit.shown == 75 * GB
    assert limit.percent_of(34 * GB) == 50


def test_margin_never_makes_a_negative_limit() -> None:
    limit = d.effective_limit(group(limit_rows={"default": 1 * GB}, margin_bytes=5 * GB), is_trial=False)
    assert limit.limit == 0
    assert limit.percent_of(10) is None


def test_exempt_subject_has_no_limit_at_all() -> None:
    limit = d.effective_limit(group(limit_rows={"default": 0}), is_trial=False, exempt=True)
    assert limit.unlimited and not limit.zero and limit.limit is None


def test_group_limit_rows_from_columns() -> None:
    assert model.group_limit_rows(has_default=True, limit_default=5, has_trial=False, limit_trial=None) == {
        "default": 5
    }
    assert model.group_limit_rows(has_default=True, limit_default=None, has_trial=True, limit_trial=0) == {
        "default": None,
        "trial": 0,
    }
    assert (
        model.group_limit_rows(has_default=False, limit_default=None, has_trial=False, limit_trial=None) == {}
    )


# ---------------------------------------------------------------- overrides


def test_override_alive_and_coverage() -> None:
    o = d.Override("no_block", group_id=2, period_id=7, valid_until=NOW + timedelta(hours=1))
    assert o.alive(NOW, 7) and not o.alive(NOW, 8) and not o.alive(NOW + timedelta(hours=1), 7)
    assert o.covers(2) and not o.covers(1)
    assert d.Override("exempt").covers(99)


def test_subject_override_helpers() -> None:
    subject = d.SubjectInput(
        subscription_id=1,
        panel_user_id=1,
        period_id=7,
        rights=frozenset({1, 2}),
        overrides=(
            d.Override("exempt", group_id=2, exempt_kind="manual"),
            d.Override("no_block", group_id=1),
            d.Override("limit", limit_bytes=3, applies_to="trial"),
        ),
    )
    assert subject.exempt_groups(NOW) == (False, frozenset({2}))
    assert subject.no_block_groups(NOW, subject.rights) == frozenset({1})
    assert subject.limit_rows(1, NOW) == {"trial": 3}


# ---------------------------------------------------------------- squads (X1)

BASE, BASE_2, TWIN, OTHER = "sq-base", "sq-base-2", "sq-twin", "sq-other"
INBOUNDS = {
    BASE: frozenset({"vless-de", "vless-lte"}),
    BASE_2: frozenset({"vless-nl", "vless-lte"}),
    TWIN: frozenset({"vless-de"}),
    OTHER: frozenset({"vless-nl"}),
}
TAGS = {1: frozenset({"vless-lte"})}
TWINS = {BASE: d.Twin(BASE, 1, TWIN)}


def test_rights_need_every_tag_of_the_group() -> None:
    assert d.rights_of([BASE], squad_inbounds=INBOUNDS, group_tags=TAGS) == {1}
    assert d.rights_of([OTHER], squad_inbounds=INBOUNDS, group_tags=TAGS) == frozenset()
    two_tags = {1: frozenset({"vless-lte", "hy-lte"})}
    assert d.rights_of([BASE], squad_inbounds=INBOUNDS, group_tags=two_tags) == frozenset()
    assert d.rights_of([BASE], squad_inbounds=INBOUNDS, group_tags={1: frozenset()}) == frozenset()


def test_substitutions_replace_base_by_twin_for_blocked_groups_only() -> None:
    subs, bad = d.substitutions_for([BASE, OTHER], {1}, TWINS, squad_inbounds=INBOUNDS, group_tags=TAGS)
    assert (subs, bad) == ({BASE: TWIN}, frozenset())
    assert d.project_squads([BASE, OTHER], subs) == [TWIN, OTHER]
    assert d.reverse_squads([TWIN, OTHER], {TWIN: BASE}) == [BASE, OTHER]
    none, _ = d.substitutions_for([BASE], set(), TWINS, squad_inbounds=INBOUNDS, group_tags=TAGS)
    assert none == {}


def test_blocked_group_without_valid_twin_is_unenforceable() -> None:
    _, bad = d.substitutions_for([BASE_2], {1}, TWINS, squad_inbounds=INBOUNDS, group_tags=TAGS)
    assert bad == {1}  # BASE_2 has the LTE inbound but no twin
    broken = {BASE: d.Twin(BASE, 1, TWIN, problem="twin_mismatch")}
    assert d.substitutions_for([BASE], {1}, broken, squad_inbounds=INBOUNDS, group_tags=TAGS) == (
        {},
        frozenset({1}),
    )
    empty = {**INBOUNDS, TWIN: frozenset()}
    assert d.substitutions_for([BASE], {1}, TWINS, squad_inbounds=empty, group_tags=TAGS) == (
        {},
        frozenset({1}),
    )
    _, none = d.substitutions_for([OTHER], {1}, TWINS, squad_inbounds=INBOUNDS, group_tags=TAGS)
    assert none == frozenset()  # no entitled base: nothing to enforce, nothing to report


def test_projection_never_duplicates() -> None:
    assert d.project_squads([BASE, TWIN], {BASE: TWIN}) == [TWIN]
    assert d.reverse_squads([TWIN, BASE], {TWIN: BASE}) == [BASE]


def test_check_twins_invariants() -> None:
    good = d.check_twins(TWINS.values(), squad_inbounds=INBOUNDS, group_tags=TAGS)
    assert [(p.code, p.subject) for p in good] == [("base_without_twin", BASE_2)]
    problems = d.check_twins(
        [
            d.Twin("a", 1, "a"),
            d.Twin(BASE, 1, "gone"),
            d.Twin(BASE_2, 1, "empty"),
            d.Twin("x", 1, "wrong"),
        ],
        squad_inbounds={**INBOUNDS, "empty": (), "x": {"vless-lte", "a1"}, "wrong": {"a1", "vless-lte"}},
        group_tags=TAGS,
    )
    assert [(p.code, p.subject) for p in problems] == [
        ("twin_is_base", "a"),
        ("twin_missing", "gone"),
        ("twin_empty", "empty"),
        ("twin_mismatch", "wrong"),
    ]
    assert "лишние: vless-lte" in problems[-1].detail


def test_check_foreign_nodes_i2() -> None:
    problems = d.check_foreign_nodes(
        node_inbounds={"n-lte": {"vless-lte"}, "n-de": {"vless-de"}, "n-bad": {"vless-de", "vless-lte"}},
        group_nodes={1: {"n-lte"}},
        group_tags=TAGS,
    )
    assert [(p.code, p.subject, p.group_id) for p in problems] == [("foreign_node", "n-bad", 1)]


# ---------------------------------------------------------------- traffic after a block


@pytest.mark.parametrize(
    ("last_delta", "resend_done", "now", "expected"),
    [
        (None, None, 60, False),
        (10, None, 60, False),  # within 20 min of the block: the flush lag, not a leak
        (25, None, 60, True),
        (25, 30, 60, False),  # nothing new after the last resend (lesson 0324a19b4)
        (45, 30, 60, False),  # new, but the last resend was < 1 h ago
        (45, 30, 95, True),
    ],
    ids=["no-deltas", "lag", "leak", "old-leak", "hourly", "again"],
)
def test_after_block_resend_due(
    last_delta: int | None, resend_done: int | None, now: int, expected: bool
) -> None:
    t0 = datetime(2026, 10, 2, 10, tzinfo=UTC)

    def at(minutes: int | None) -> datetime | None:
        return None if minutes is None else t0 + timedelta(minutes=minutes)

    now_at = at(now)
    assert now_at is not None
    assert (
        d.after_block_resend_due(
            applied_at=t0, last_delta_at=at(last_delta), resend_done_at=at(resend_done), now=now_at
        )
        is expected
    )
    assert (
        d.after_block_resend_due(applied_at=None, last_delta_at=t0, resend_done_at=None, now=now_at) is False
    )


# ---------------------------------------------------------------- model


@pytest.mark.parametrize(
    ("core", "source", "details", "kind"),
    [
        ("purchase_new", "bot", None, "paid"),
        ("purchase_renew", "bot", None, "paid"),
        ("plan_changed", "bot", None, "paid"),
        ("trial_converted", "bot", None, "paid"),
        ("trial_started", "bot", None, "trial"),
        ("frozen", "system", None, "freeze"),
        ("unfrozen", "admin", None, "unfreeze"),
        ("closed", "bot", None, "close"),
        ("extended", "admin", None, "admin"),
        ("extended", "bot", {"reason": "referral"}, "bonus"),
        ("extended", "bot", {"reason": "admin"}, "admin"),
        ("extended", "bot", None, "unclassified"),
        ("devices_added", "bot", None, "unclassified"),
    ],
    ids=str,
)
def test_engine_event_kind(core: str, source: str, details: dict[str, str] | None, kind: str) -> None:
    assert model.engine_event_kind(core, source=source, details=details) == kind


def test_paid_core_kinds_revoke_launch_trial() -> None:
    assert {
        "purchase_new",
        "purchase_renew",
        "plan_changed",
        "trial_converted",
        "site",
    } <= model.PAID_CORE_KINDS
    assert "extended" not in model.PAID_CORE_KINDS


def test_model_helpers() -> None:
    from datetime import date

    assert model.aware(datetime(2026, 1, 1, 3, tzinfo=model.MSK)) == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="часовым"):
        model.aware(datetime(2026, 1, 1))
    assert model.cap(datetime(9999, 1, 1, tzinfo=UTC)) == model.FAR_FUTURE
    assert model.cap(None) is None
    assert model.msk_midnight(date(2026, 10, 7)) == datetime(2026, 10, 6, 21, tzinfo=UTC)
    assert model.int_param({"x": True}, "x", 5, 0, 10) == 5
    assert model.int_param({"x": "7"}, "x", 5, 0, 10) == 7
    assert model.int_param({"x": 70}, "x", 5, 0, 10) == 10
