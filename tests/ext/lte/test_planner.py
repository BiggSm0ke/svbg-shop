"""LTE planner: limits, decisions and the safety fuses — the owner's planner table ported (05 §2.1.10).

Owner → SvBG: ``enforce_scope=all|list|off`` → ``lte.enforce=on`` (+ pilot list) / ``shadow`` / ``off``;
``pending_release`` → ``releasing``; ``cancel_release`` → ``Restore``; exemptions come from ``lte_overrides``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from svbg.ext.lte import planner
from svbg.ext.lte.decide import (
    NOTIFY_EXHAUSTED,
    NOTIFY_RESET,
    NOTIFY_WARN,
    RELEASE_IDENTITY,
    EnforceSettings,
    GroupInput,
    LiveBlock,
    Override,
    SubjectInput,
)

GB = 10**9
GROUP = 1
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
ON = EnforceSettings(mode="on")


def group(**values: Any) -> GroupInput:
    defaults: dict[str, Any] = {
        "id": GROUP,
        "state": "active",
        "enforce": True,
        "limit_rows": {"default": 50 * GB},
    }
    defaults.update(values)
    return GroupInput(**defaults)


def subject(**values: Any) -> SubjectInput:
    defaults: dict[str, Any] = {
        "subscription_id": 10,
        "panel_user_id": 100,
        "period_id": 7,
        "rights": frozenset({GROUP}),
    }
    defaults.update(values)
    return SubjectInput(**defaults)


def block(**values: Any) -> LiveBlock:
    defaults: dict[str, Any] = {
        "id": 1,
        "group_id": GROUP,
        "mode": "enforce",
        "status": "active",
        "reason": "quota",
        "period_id": 7,
    }
    defaults.update(values)
    return LiveBlock(**defaults)


def decide(
    *, groups: list[GroupInput] | None = None, subjects: list[SubjectInput], **kw: Any
) -> planner.Plan:
    return planner.decide(
        groups=[group()] if groups is None else groups,
        subjects=subjects,
        settings=kw.pop("settings", ON),
        now=kw.pop("now", NOW),
        params=kw.pop("params", None),
    )


# ---------------------------------------------------------------- blocks


def test_block_when_used_reaches_limit() -> None:
    plan = decide(subjects=[subject(used={GROUP: 50 * GB})])
    assert len(plan.blocks) == 1
    candidate = plan.blocks[0]
    assert (candidate.reason, candidate.mode, candidate.period_id) == ("quota", "enforce", 7)
    assert candidate.limit_bytes == 50 * GB
    assert [n.kind for n in plan.notifications] == [NOTIFY_EXHAUSTED]
    assert plan.panel_writes == 1


def test_no_block_below_limit_but_warning_notification() -> None:
    plan = decide(subjects=[subject(used={GROUP: 41 * GB})])
    assert plan.blocks == ()
    assert [n.kind for n in plan.notifications] == [NOTIFY_WARN]
    assert plan.notifications[0].threshold == 80
    assert plan.notifications[0].limit_bytes == 50 * GB


def test_warn_threshold_follows_settings() -> None:
    plan = decide(
        subjects=[subject(used={GROUP: 41 * GB})], settings=EnforceSettings(mode="on", warn_percent=90)
    )
    assert plan.notifications == ()


def test_warn_notification_is_not_repeated() -> None:
    plan = decide(subjects=[subject(used={GROUP: 41 * GB}, notified=frozenset({(GROUP, NOTIFY_WARN)}))])
    assert plan.notifications == ()


def test_incomplete_data_still_blocks_and_records_reasons() -> None:
    plan = decide(
        groups=[group(incomplete=("node_disconnected:abc",))], subjects=[subject(used={GROUP: 60 * GB})]
    )
    assert plan.blocks[0].decided_incomplete == ("node_disconnected:abc",)


def test_anomaly_forbids_new_blocks() -> None:
    plan = decide(groups=[group(anomaly=("counter_regress:abc",))], subjects=[subject(used={GROUP: 60 * GB})])
    assert plan.blocks == ()
    assert plan.group_reasons[GROUP] == ("anomaly",)
    assert plan.notifications == ()


def test_suspended_or_inactive_group_forbids_new_blocks() -> None:
    plan = decide(groups=[group(suspended=True)], subjects=[subject(used={GROUP: 60 * GB})])
    assert plan.blocks == ()
    assert "suspended" in plan.group_reasons[GROUP]
    draft = decide(groups=[group(state="draft")], subjects=[subject(used={GROUP: 60 * GB})])
    assert draft.blocks == ()
    assert draft.group_reasons[GROUP] == ("group_not_active",)


def test_clamped_sanity_delta_forbids_block_for_that_subject() -> None:
    plan = decide(subjects=[subject(used={GROUP: 60 * GB}, clamped_sanity=frozenset({GROUP}))])
    assert plan.blocks == ()
    assert any("clamped_sanity" in w for w in plan.warnings)


def test_no_block_override_disables_quota_block() -> None:
    plan = decide(subjects=[subject(used={GROUP: 60 * GB}, overrides=(Override("no_block", period_id=7),))])
    assert plan.blocks == ()
    expired = Override("no_block", valid_until=NOW - timedelta(seconds=1))
    assert len(decide(subjects=[subject(used={GROUP: 60 * GB}, overrides=(expired,))]).blocks) == 1
    other_period = Override("no_block", period_id=6)
    assert len(decide(subjects=[subject(used={GROUP: 60 * GB}, overrides=(other_period,))]).blocks) == 1


def test_zero_limit_creates_unavailable_block_without_exhausted_notice() -> None:
    plan = decide(groups=[group(limit_rows={"default": 0})], subjects=[subject()])
    assert [item.reason for item in plan.blocks] == ["unavailable"]
    assert plan.notifications == ()


def test_trial_zero_limit_blocks_only_trial_periods() -> None:
    rows = {"default": 50 * GB, "trial": 0}
    assert [
        b.reason
        for b in decide(groups=[group(limit_rows=rows)], subjects=[subject(period_is_trial=True)]).blocks
    ] == ["unavailable"]
    assert decide(groups=[group(limit_rows=rows)], subjects=[subject()]).blocks == ()


def test_missing_limit_row_warns_and_does_not_block() -> None:
    plan = decide(groups=[group(limit_rows={})], subjects=[subject(used={GROUP: 900 * GB})])
    assert plan.blocks == ()
    assert "group:1:no_limit_row" in plan.warnings


def test_gap_estimated_share_alone_does_not_block() -> None:
    plan = decide(subjects=[subject(used={GROUP: 51 * GB}, gap_estimated={GROUP: 3 * GB})])
    assert plan.blocks == ()
    assert any("gap_estimated" in item for item in plan.warnings)


def test_gap_estimated_share_does_not_save_real_overuse() -> None:
    plan = decide(subjects=[subject(used={GROUP: 70 * GB}, gap_estimated={GROUP: 3 * GB})])
    assert len(plan.blocks) == 1


def test_subject_without_live_period_is_skipped() -> None:
    assert decide(subjects=[subject(period_state="closed", used={GROUP: 60 * GB})]).blocks == ()
    assert decide(subjects=[subject(period_id=None, used={GROUP: 60 * GB})]).blocks == ()


def test_deferred_period_still_blocks() -> None:
    assert len(decide(subjects=[subject(period_state="deferred", used={GROUP: 60 * GB})]).blocks) == 1


def test_subject_without_group_rights_is_not_blocked() -> None:
    assert decide(subjects=[subject(rights=frozenset(), used={GROUP: 60 * GB})]).blocks == ()


def test_frozen_subscription_is_not_decided() -> None:
    plan = decide(
        subjects=[
            subject(used={GROUP: 60 * GB}, frozen=True),
            subject(subscription_id=11, frozen=True, live_blocks=(block(),)),
        ]
    )
    assert plan.empty


# ---------------------------------------------------------------- restore, rebind, release


def test_releasing_block_is_restored_instead_of_new_insert() -> None:
    plan = decide(
        subjects=[
            subject(used={GROUP: 60 * GB}, live_blocks=(block(status="releasing", release_reason="reset"),))
        ]
    )
    assert plan.blocks == ()
    assert [item.block_id for item in plan.restores] == [1]
    assert plan.restores[0].period_id == 7


def test_restored_release_is_outside_throttling() -> None:
    params = planner.PlannerParams(max_new_blocks_per_cycle=1)
    subjects = [
        subject(
            subscription_id=index,
            panel_user_id=100 + index,
            used={GROUP: 60 * GB},
            live_blocks=(block(id=index, status="releasing", release_reason="topup"),),
        )
        for index in range(1, 6)
    ]
    plan = decide(subjects=subjects, params=params)
    assert len(plan.restores) == 5
    assert plan.throttled == ()


def test_block_of_previous_period_is_rebound_without_release() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 0})],
        subjects=[subject(live_blocks=(block(reason="unavailable", period_id=6),))],
    )
    assert plan.releases == ()
    assert [(item.block_id, item.period_id) for item in plan.rebinds] == [(1, 7)]
    assert plan.panel_writes == 0


def test_manual_block_is_rebound_and_never_released_automatically() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 100 * GB})],
        subjects=[subject(used={GROUP: 1 * GB}, live_blocks=(block(reason="manual", period_id=6),))],
    )
    assert plan.releases == ()
    assert [(r.block_id, r.reason) for r in plan.rebinds] == [(1, "manual")]


def test_limit_raise_releases_block() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 100 * GB})],
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(),))],
    )
    assert [(item.block_id, item.release_reason) for item in plan.releases] == [(1, "limit_change")]


def test_credit_release_is_marked_as_topup() -> None:
    plan = decide(subjects=[subject(used={GROUP: 60 * GB}, credits={GROUP: 25 * GB}, live_blocks=(block(),))])
    assert [item.release_reason for item in plan.releases] == ["topup"]


def test_block_of_deleted_group_or_lost_rights_is_released() -> None:
    gone = decide(groups=[], subjects=[subject(rights=frozenset(), live_blocks=(block(),))])
    assert [item.release_reason for item in gone.releases] == ["group_deleted"]
    lost = decide(subjects=[subject(rights=frozenset(), live_blocks=(block(),))])
    assert [item.release_reason for item in lost.releases] == ["no_rights"]


def test_releasing_block_is_not_released_twice() -> None:
    plan = decide(
        groups=[],
        subjects=[subject(rights=frozenset(), live_blocks=(block(status="releasing", release_reason="x"),))],
    )
    assert plan.releases == ()


# ---------------------------------------------------------------- exemptions and identity


def test_exempt_subject_never_becomes_candidate() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 0})],
        subjects=[subject(used={GROUP: 900 * GB}, overrides=(Override("exempt", exempt_kind="owner"),))],
    )
    assert plan.empty


def test_exempt_subject_live_blocks_go_to_release_manual_too() -> None:
    plan = decide(
        subjects=[
            subject(
                used={GROUP: 60 * GB},
                overrides=(Override("exempt", exempt_kind="manual"),),
                live_blocks=(block(), block(id=2, group_id=2, reason="manual")),
            )
        ]
    )
    assert [(r.block_id, r.release_reason) for r in plan.releases] == [(1, "exempt"), (2, "exempt")]


def test_exemption_for_one_group_only() -> None:
    plan = decide(
        groups=[group(), group(id=2)],
        subjects=[
            subject(
                rights=frozenset({1, 2}),
                used={1: 60 * GB, 2: 60 * GB},
                overrides=(Override("exempt", group_id=2, exempt_kind="manual"),),
                live_blocks=(block(id=5, group_id=2),),
            )
        ],
    )
    assert [b.group_id for b in plan.blocks] == [1]
    assert [(r.block_id, r.release_reason) for r in plan.releases] == [(5, "exempt")]


def test_expired_exemption_does_not_count() -> None:
    expired = Override("exempt", exempt_kind="manual", valid_until=NOW)
    assert len(decide(subjects=[subject(used={GROUP: 60 * GB}, overrides=(expired,))]).blocks) == 1


def test_identity_mismatch_releases_live_blocks_and_blocks_nothing() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 0})],
        subjects=[subject(used={GROUP: 900 * GB}, live_blocks=(block(),), identity_ok=False)],
    )
    assert plan.blocks == ()
    assert plan.notifications == ()
    assert [item.release_reason for item in plan.releases] == [RELEASE_IDENTITY]


def test_identity_mismatch_does_not_repeat_its_own_release() -> None:
    plan = decide(
        subjects=[
            subject(
                live_blocks=(block(status="releasing", release_reason=RELEASE_IDENTITY),), identity_ok=False
            )
        ]
    )
    assert plan.releases == ()


# ---------------------------------------------------------------- individual limits


def test_individual_limit_rows() -> None:
    paid_row = Override("limit", limit_bytes=30 * GB, applies_to="paid")
    all_row = Override("limit", limit_bytes=10 * GB, applies_to="all")
    plan = decide(subjects=[subject(used={GROUP: 20 * GB}, overrides=(all_row, paid_row))])
    assert plan.blocks == ()  # the paid row (30) wins over the all row (10)
    trial = decide(
        subjects=[subject(used={GROUP: 20 * GB}, period_is_trial=True, overrides=(all_row, paid_row))]
    )
    assert [b.limit_bytes for b in trial.blocks] == [10 * GB]
    unlimited = decide(
        subjects=[subject(used={GROUP: 900 * GB}, overrides=(Override("limit", limit_bytes=None),))]
    )
    assert unlimited.empty


def test_group_specific_individual_limit_wins_over_global_one() -> None:
    rows = (Override("limit", limit_bytes=5 * GB), Override("limit", group_id=GROUP, limit_bytes=100 * GB))
    assert decide(subjects=[subject(used={GROUP: 60 * GB}, overrides=rows)]).blocks == ()


# ---------------------------------------------------------------- enforcement modes


def test_shadow_mode_records_decision_without_notifications() -> None:
    plan = decide(subjects=[subject(used={GROUP: 60 * GB})], settings=EnforceSettings(mode="shadow"))
    assert [item.mode for item in plan.blocks] == ["shadow"]
    assert plan.notifications == ()
    assert plan.panel_writes == 0


def test_shadow_keeps_imported_enforce_blocks() -> None:
    """Migration (05 §2.1.17 p.3): imported active blocks stay in the desired state while in shadow."""
    plan = decide(
        groups=[group(limit_rows={"default": 100 * GB})],
        subjects=[subject(used={GROUP: 1 * GB}, live_blocks=(block(),))],
        settings=EnforceSettings(mode="shadow"),
    )
    assert plan.empty


def test_off_releases_or_holds_blocks() -> None:
    released = decide(
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(),))],
        settings=EnforceSettings(mode="off"),
    )
    assert [item.release_reason for item in released.releases] == ["feature_off"]
    assert released.blocks == ()
    held = decide(
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(),))],
        settings=EnforceSettings(mode="off", release_when_off=False),
    )
    assert held.empty  # "оставить": the blocks stay as they are, nothing new is decided


def test_off_releases_blocks_without_a_live_period_and_skips_releasing_ones() -> None:
    plan = decide(
        subjects=[
            subject(period_id=None, live_blocks=(block(),)),
            subject(
                subscription_id=11, live_blocks=(block(id=2, status="releasing", release_reason="topup"),)
            ),
        ],
        settings=EnforceSettings(mode="off"),
    )
    assert [(r.block_id, r.release_reason) for r in plan.releases] == [(1, "feature_off")]


def test_off_with_hold_still_drops_shadow_blocks() -> None:
    plan = decide(
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(mode="shadow"),))],
        settings=EnforceSettings(mode="off", release_when_off=False),
    )
    assert [(r.release_reason, r.immediate) for r in plan.releases] == [("feature_off", True)]


def test_pilot_list_applies_only_to_listed_subjects() -> None:
    plan = decide(
        subjects=[
            subject(used={GROUP: 60 * GB}),
            subject(subscription_id=11, panel_user_id=101, used={GROUP: 60 * GB}),
        ],
        settings=EnforceSettings(mode="on", pilot=frozenset({100})),
    )
    assert {item.panel_user_id: item.mode for item in plan.blocks} == {100: "enforce", 101: "shadow"}
    assert [(n.panel_user_id, n.kind) for n in plan.notifications] == [(100, NOTIFY_EXHAUSTED)]


def test_enforce_block_outside_pilot_is_released() -> None:
    plan = decide(
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(),))],
        settings=EnforceSettings(mode="on", pilot=frozenset({999})),
    )
    assert [item.release_reason for item in plan.releases] == ["feature_off"]
    assert plan.blocks == ()


def test_group_without_enforce_gives_shadow_mode() -> None:
    plan = decide(groups=[group(enforce=False)], subjects=[subject(used={GROUP: 60 * GB})])
    assert [item.mode for item in plan.blocks] == ["shadow"]


def test_shadow_block_is_dropped_when_enforcement_starts() -> None:
    plan = decide(subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(mode="shadow"),))])
    assert [(r.release_reason, r.immediate) for r in plan.releases] == [("mode_change", True)]
    assert [b.mode for b in plan.blocks] == ["enforce"]


def test_shadow_block_released_below_limit_is_immediate() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 100 * GB})],
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(mode="shadow"),))],
        settings=EnforceSettings(mode="shadow"),
    )
    assert [(r.release_reason, r.immediate) for r in plan.releases] == [("limit_change", True)]
    assert plan.panel_writes == 0


# ---------------------------------------------------------------- throttling and quarantine


def _wave(count: int, *, used_step: int = GB) -> list[SubjectInput]:
    return [
        subject(subscription_id=index, panel_user_id=1000 + index, used={GROUP: 50 * GB + index * used_step})
        for index in range(1, count + 1)
    ]


def _quiet(count: int) -> list[SubjectInput]:
    return [subject(subscription_id=5000 + i, panel_user_id=5000 + i, used={GROUP: GB}) for i in range(count)]


def test_throttling_applies_thirty_and_defers_the_rest_by_overuse() -> None:
    plan = decide(subjects=[*_wave(40), *_quiet(200)])
    assert (len(plan.blocks), len(plan.throttled)) == (30, 10)
    assert plan.blocks[0].used_bytes == 90 * GB
    assert max(item.used_bytes for item in plan.throttled) < min(item.used_bytes for item in plan.blocks)
    assert len([n for n in plan.notifications if n.kind == NOTIFY_EXHAUSTED]) == 30


def test_unavailable_candidates_are_outside_throttling() -> None:
    params = planner.PlannerParams(max_new_blocks_per_cycle=1)
    subjects = [subject(subscription_id=i, panel_user_id=2000 + i) for i in range(1, 6)]
    plan = decide(groups=[group(limit_rows={"default": 0})], subjects=subjects, params=params)
    assert len(plan.blocks) == 5
    assert plan.throttled == ()


def test_quarantine_when_candidates_exceed_threshold() -> None:
    params = planner.PlannerParams(max_new_blocks_per_cycle=3, quarantine_new_blocks=5)
    plan = decide(subjects=_wave(6), params=params)
    assert plan.quarantine == frozenset({GROUP})
    assert plan.blocks == ()
    assert plan.notifications == ()
    assert "quarantine" in plan.group_reasons[GROUP]


def test_quarantine_when_blocked_share_exceeds_threshold() -> None:
    params = planner.PlannerParams(max_blocked_share_pct=25, quarantine_new_blocks=100, share_min_eligible=5)
    plan = decide(subjects=[*_wave(2), *_quiet(3)], params=params)
    assert plan.quarantine == frozenset({GROUP})


def test_blocked_share_counts_existing_blocks() -> None:
    params = planner.PlannerParams(max_blocked_share_pct=25, share_min_eligible=4)
    blocked = [
        subject(
            subscription_id=900 + i,
            panel_user_id=900 + i,
            used={GROUP: 60 * GB},
            live_blocks=(block(id=900 + i),),
        )
        for i in range(1)
    ]
    plan = decide(subjects=[*_wave(1), *blocked, *_quiet(2)], params=params)
    assert plan.quarantine == frozenset({GROUP})  # (1 blocked + 1 new) / 4 = 50 %


def test_blocked_share_is_not_checked_on_a_tiny_pilot() -> None:
    plan = decide(
        subjects=_wave(1), params=planner.PlannerParams(max_blocked_share_pct=25, share_min_eligible=20)
    )
    assert plan.quarantine == frozenset()
    assert len(plan.blocks) == 1


def test_quarantine_cleared_group_skips_thresholds() -> None:
    params = planner.PlannerParams(max_new_blocks_per_cycle=30, quarantine_new_blocks=5)
    plan = decide(groups=[group(quarantine_cleared=True)], subjects=_wave(6), params=params)
    assert plan.quarantine == frozenset()
    assert len(plan.blocks) == 6


def test_quarantined_group_still_releases() -> None:
    plan = decide(
        groups=[group(quarantined=True, limit_rows={"default": 100 * GB})],
        subjects=[subject(used={GROUP: 60 * GB}, live_blocks=(block(),))],
    )
    assert len(plan.releases) == 1


def test_exhausted_notification_only_for_applied_blocks() -> None:
    plan = decide(subjects=_wave(3), params=planner.PlannerParams(max_new_blocks_per_cycle=1))
    assert [n.kind for n in plan.notifications] == [NOTIFY_EXHAUSTED]
    assert plan.notifications[0].panel_user_id == plan.blocks[0].panel_user_id


def test_planner_params_from_values() -> None:
    params = planner.PlannerParams.from_values({"max_new_blocks_per_cycle": 50, "quarantine_new_blocks": 10})
    assert params.quarantine_new_blocks == 50
    assert planner.PlannerParams.from_values({"max_blocked_share_pct": 0}).max_blocked_share_pct == 1
    assert planner.PlannerParams.from_values({}) == planner.PlannerParams()


# ---------------------------------------------------------------- reset notice


def test_reset_notice_after_boundary_for_blocked_or_warned_subject() -> None:
    plan = decide(
        subjects=[
            subject(used={GROUP: 1 * GB}, reset_notice_groups=frozenset({GROUP})),
            subject(subscription_id=11, panel_user_id=101, used={GROUP: 1 * GB}),
        ]
    )
    assert [(n.panel_user_id, n.kind, n.period_id) for n in plan.notifications] == [(100, NOTIFY_RESET, 7)]
    assert plan.notifications[0].limit_bytes == 50 * GB


def test_reset_notice_waits_until_previous_block_is_released() -> None:
    plan = decide(
        subjects=[
            subject(
                used={GROUP: 1 * GB},
                reset_notice_groups=frozenset({GROUP}),
                live_blocks=(block(status="releasing", release_reason="reset", period_id=6),),
            )
        ]
    )
    assert plan.notifications == ()


def test_reset_notice_is_not_repeated_and_not_sent_in_shadow() -> None:
    repeated = decide(
        subjects=[
            subject(reset_notice_groups=frozenset({GROUP}), notified=frozenset({(GROUP, NOTIFY_RESET)}))
        ]
    )
    assert repeated.notifications == ()
    shadow = decide(
        subjects=[subject(reset_notice_groups=frozenset({GROUP}))], settings=EnforceSettings(mode="shadow")
    )
    assert shadow.notifications == ()


def test_reset_notice_is_not_sent_when_new_period_blocks_again() -> None:
    plan = decide(
        groups=[group(limit_rows={"default": 0})], subjects=[subject(reset_notice_groups=frozenset({GROUP}))]
    )
    assert NOTIFY_RESET not in [n.kind for n in plan.notifications]
    assert [item.reason for item in plan.blocks] == ["unavailable"]


# ---------------------------------------------------------------- admin preview


def test_preview_counts_and_histogram() -> None:
    subjects = [
        subject(subscription_id=1, panel_user_id=1, used={GROUP: 60 * GB}),  # 60/40 → block, ≥150 %
        subject(
            subscription_id=2, panel_user_id=2, used={GROUP: 10 * GB}, live_blocks=(block(id=2),)
        ),  # → release
        subject(subscription_id=3, panel_user_id=3, used={GROUP: 5 * GB}),  # 12 %
        subject(subscription_id=4, panel_user_id=4, period_id=None),
    ]
    result = planner.preview(
        groups=[group(limit_rows={"default": 40 * GB})], subjects=subjects, settings=ON, now=NOW
    )
    assert (result.blocked_now, result.new_blocks, result.releases, result.blocked_after) == (1, 1, 1, 1)
    assert result.histogram[150] == 1
    assert result.histogram[20] == 1  # 10/40 = 25 %
    assert result.histogram[10] == 1  # 5/40 = 12 %
    assert sum(result.histogram.values()) == 3
    assert [(r.subscription_id, r.action) for r in result.sample] == [(1, "block"), (2, "release")]
    assert set(result.histogram) == set(planner.PREVIEW_BUCKETS)


def test_preview_shows_throttled_and_quarantine() -> None:
    result = planner.preview(
        groups=[group()],
        subjects=_wave(8),
        settings=ON,
        now=NOW,
        params=planner.PlannerParams(max_new_blocks_per_cycle=2, quarantine_new_blocks=100),
    )
    assert (result.new_blocks, result.throttled) == (2, 6)
    assert len(result.sample) == 2
