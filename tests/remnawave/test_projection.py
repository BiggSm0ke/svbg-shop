"""Projection rules of 02 §6.3: who owns which field, overrides, reverse of module twins, staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import msgspec

from svbg.remnawave.contributors import Substitution
from svbg.remnawave.models import PanelUser
from svbg.remnawave.projection import compute
from tests.subscriptions.kit import sync_env

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
BASE = "base-squad"
TWIN = "twin-squad"
EXPIRE = NOW + timedelta(days=30)


def panel_user(**over: Any) -> PanelUser:
    data: dict[str, Any] = {
        "id": 7,
        "shortUuid": "abcdefgh12345678",
        "username": "sv_100",
        "status": "ACTIVE",
        "trafficLimitBytes": 100,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": EXPIRE.isoformat(),
        "telegramId": 100,
        "hwidDeviceLimit": None,
        "externalSquadUuid": None,
        "tag": None,
        "subscriptionUrl": "https://sub.example.com/abcdefgh12345678",
        "activeInternalSquads": [{"uuid": BASE}],
        "userTraffic": {"usedTrafficBytes": 5},
    }
    data.update(over)
    return msgspec.convert(data, PanelUser, strict=False)


def sub_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "link_state": "linked",
        "panel_user_id": 7,
        "panel_username": "sv_100",
        "panel_short_uuid": "abcdefgh12345678",
        "subscription_url": "https://sub.example.com/abcdefgh12345678",
        "paid_until": EXPIRE,
        "desired_expire_at": EXPIRE,
        "desired_traffic_bytes": 100,
        "desired_reset_strategy": "NO_RESET",
        "desired_device_limit": None,
        "desired_squads": [BASE],
        "desired_ext_squad": None,
        "desired_tag": None,
        "desired_status": "active",
        "disabled_reason": None,
        "overrides": {},
        "owner_telegram_id": 100,
        "panel_status": "ACTIVE",
        "panel_expire_at": EXPIRE,
        "panel_traffic_limit": 100,
        "panel_used_traffic": 5,
        "panel_reset_strategy": "NO_RESET",
        "panel_device_limit": None,
        "panel_squads": [BASE],
        "panel_ext_squad": None,
        "panel_tag": None,
        "panel_telegram_id": 100,
        "panel_online_at": None,
        "panel_first_connected_at": None,
        "panel_last_traffic_reset_at": None,
        "panel_state_ts": NOW - timedelta(minutes=5),
    }
    row.update(over)
    return row


def test_identical_state_changes_nothing() -> None:
    res = compute(sub_row(), panel_user(), twins={}, at=NOW)
    assert not res.changed and res.values == {} and not res.drift and not res.alerts


def test_runtime_fields_are_copied_from_the_panel() -> None:
    res = compute(
        sub_row(), panel_user(status="LIMITED", userTraffic={"usedTrafficBytes": 100}), twins={}, at=NOW
    )
    assert res.values["panel_status"] == "LIMITED" and res.values["panel_used_traffic"] == 100
    assert "overrides" not in res.values
    assert [e.name for e in res.bus] == ["subscription.panel_status"]


def test_longer_term_in_panel_is_accepted_with_audit() -> None:
    longer = EXPIRE + timedelta(days=10)
    res = compute(sub_row(), panel_user(expireAt=longer.isoformat()), twins={}, at=NOW)
    assert res.values["paid_until"] == longer and res.values["desired_expire_at"] == longer
    assert [e["kind"] for e in res.events] == ["expire_extended_in_panel"]
    assert res.events[0]["delta_seconds"] == 10 * 86400 and not res.alerts


def test_shorter_term_in_panel_is_accepted_and_alerts_the_owner() -> None:
    shorter = EXPIRE - timedelta(days=10)
    res = compute(sub_row(), panel_user(expireAt=shorter.isoformat()), twins={}, at=NOW)
    assert res.values["paid_until"] == shorter
    assert [e["kind"] for e in res.events] == ["expire_reduced_in_panel"]
    assert [a.dedup_key for a in res.alerts] == ["rw:expire_reduced:1"]
    assert "subscription.expire_reduced" in [e.name for e in res.bus]


def test_millisecond_rounding_is_not_a_change() -> None:
    res = compute(
        sub_row(desired_expire_at=EXPIRE + timedelta(microseconds=900)), panel_user(), twins={}, at=NOW
    )
    assert "desired_expire_at" not in res.values


def test_manual_plan_edits_become_overrides_and_clear_when_reverted() -> None:
    res = compute(sub_row(), panel_user(trafficLimitBytes=999, hwidDeviceLimit=4), twins={}, at=NOW)
    assert res.values["overrides"] == {"traffic_bytes": 999, "device_limit": 4}
    assert [e["kind"] for e in res.events] == ["override"]
    reverted = compute(
        sub_row(overrides={"traffic_bytes": 999, "device_limit": 4}), panel_user(), twins={}, at=NOW
    )
    assert reverted.values["overrides"] == {}


def test_unmanaged_tag_is_not_an_override() -> None:
    res = compute(sub_row(), panel_user(tag="OWNER_TAG"), twins={}, at=NOW)
    assert "overrides" not in res.values


def test_reverse_hides_module_twins() -> None:
    subs = [Substitution(BASE, TWIN, "lte")]
    res = compute(
        sub_row(panel_squads=[TWIN]),
        panel_user(activeInternalSquads=[{"uuid": TWIN}]),
        twins={TWIN: BASE},
        substitutions=subs,
        at=NOW,
    )
    assert not res.changed and not res.drift, "двойник модуля не должен выглядеть ручной правкой"


def test_released_substitution_is_drift_not_override() -> None:
    res = compute(
        sub_row(panel_squads=[TWIN]),
        panel_user(activeInternalSquads=[{"uuid": TWIN}]),
        twins={TWIN: BASE},
        substitutions=[],
        at=NOW,
    )
    assert res.drift == ["squads"] and "overrides" not in res.values
    frozen = compute(
        sub_row(panel_squads=[TWIN]),
        panel_user(activeInternalSquads=[{"uuid": TWIN}]),
        twins={TWIN: BASE},
        substitutions=[],
        frozen=True,
        at=NOW,
    )
    assert frozen.drift == []


def test_manual_squad_change_is_an_override_after_reverse() -> None:
    res = compute(
        sub_row(),
        panel_user(activeInternalSquads=[{"uuid": TWIN}, {"uuid": "other"}]),
        twins={TWIN: BASE},
        at=NOW,
    )
    assert res.values["overrides"] == {"squads": [BASE, "other"]}


def test_empty_squads_mean_unknown() -> None:
    res = compute(sub_row(), panel_user(activeInternalSquads=[]), twins={}, at=NOW)
    assert not res.changed


def test_pending_operation_blocks_desired_comparison() -> None:
    res = compute(
        sub_row(),
        panel_user(trafficLimitBytes=1, expireAt=(EXPIRE + timedelta(days=3)).isoformat()),
        twins={},
        pending=True,
        at=NOW,
    )
    assert "overrides" not in res.values and "paid_until" not in res.values
    assert res.values["panel_traffic_limit"] == 1  # the snapshot itself is still refreshed


def test_stale_snapshot_is_ignored() -> None:
    res = compute(
        sub_row(panel_state_ts=NOW),
        panel_user(status="EXPIRED"),
        twins={},
        state_ts=NOW - timedelta(1),
        at=NOW,
    )
    assert res.stale and not res.changed


def test_admin_disable_is_an_override_but_bot_ban_is_not() -> None:
    res = compute(sub_row(), panel_user(status="DISABLED"), twins={}, at=NOW)
    assert res.values["overrides"] == {"status": "DISABLED"}
    ours = compute(
        sub_row(disabled_reason="BOT_BAN", desired_status="disabled"),
        panel_user(status="DISABLED"),
        twins={},
        at=NOW,
    )
    assert "overrides" not in ours.values
    enabled = compute(
        sub_row(overrides={"status": "DISABLED"}, panel_status="DISABLED"), panel_user(), twins={}, at=NOW
    )
    assert enabled.values["overrides"] == {}


def test_telegram_transfer_alerts_and_link_change_is_reported() -> None:
    res = compute(sub_row(), panel_user(telegramId=555, shortUuid="zzzzzzzzzzzzzzzz"), twins={}, at=NOW)
    assert [a.dedup_key for a in res.alerts] == ["rw:tg_changed:1"]
    assert {e["kind"] for e in res.events} == {"telegram_changed_in_panel", "link_changed_in_panel"}
    assert "subscription.link_changed" in [e.name for e in res.bus]


def test_reappeared_user_is_linked_again() -> None:
    res = compute(sub_row(link_state="panel_missing"), panel_user(), twins={}, at=NOW)
    assert res.values["link_state"] == "linked"


async def test_manual_panel_edit_is_kept_by_the_bot(pg_dsn: str) -> None:
    """End to end: admin edits the limit in the panel → override; the next plan push does not revert it."""
    from svbg.remnawave.sync import Reconciler

    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(300, traffic_bytes=100)
        pid = (await env.sub(sid))["panel_user_id"]
        env.panel_user(pid)["trafficLimitBytes"] = 5000  # admin, by hand, in the panel
        reconciler = Reconciler(
            env.db, env.current_api, contributors=env.contributors, attention=env.attention
        )
        report = await reconciler.full_pass()
        assert report.status == "ok" and report.overrides == 1
        assert (await env.sub(sid))["overrides"] == {"traffic_bytes": 5000}
        async with env.db.tx() as conn:
            await env.service.change(
                conn, sid, desired_traffic_bytes=200, desired_expire_at=datetime.now(UTC) + timedelta(days=90)
            )
        await env.drain()
        assert env.panel_user(pid)["trafficLimitBytes"] == 5000, "бот перетёр ручную правку"
        assert "trafficLimitBytes" not in env.panel.calls("/users", "PATCH")[-1].body


def test_frozen_squads_are_never_taken_for_a_manual_edit() -> None:
    """07 §2.4.3 fail-closed: while the module is down (or our change is held), the panel's squads differing
    from the plan are not an override; once the module is back the held change is drift, not an override."""
    other = "other-squad"
    frozen = compute(sub_row(desired_squads=[other]), panel_user(), twins={}, frozen=True, at=NOW)
    assert "overrides" not in frozen.values and frozen.drift == []
    held = sub_row(desired_squads=[other], overrides={"_squads_pending": True})
    recovered = compute(held, panel_user(), twins={}, at=NOW)
    assert "overrides" not in recovered.values and recovered.drift == ["squads"]
    # Without the marker and with the module OK the same difference is a manual edit, as before.
    manual = compute(sub_row(desired_squads=[other]), panel_user(), twins={}, at=NOW)
    assert manual.values["overrides"] == {"squads": [BASE]}
    # The marker is dropped once the panel shows the plan's squads; bookkeeping keys are not "overrides".
    done = compute(sub_row(overrides={"_squads_pending": True}), panel_user(), twins={}, at=NOW)
    assert done.values["overrides"] == {}
    mixed = compute(
        sub_row(desired_squads=[other], overrides={"_squads_pending": True}),
        panel_user(trafficLimitBytes=7),
        twins={},
        at=NOW,
    )
    override_events = [e for e in mixed.events if e["kind"] == "override"]
    assert [e["details"]["fields"] for e in override_events] == [["traffic_bytes"]]


def test_snapshot_older_than_the_stored_one_is_stale() -> None:
    """A page/GET requested before our last write (``state_ts`` = request time) never rolls it back."""
    res = compute(
        sub_row(panel_state_ts=NOW),
        panel_user(expireAt=(NOW - timedelta(days=1)).isoformat()),
        twins={},
        state_ts=NOW - timedelta(seconds=1),
        at=NOW,
    )
    assert res.stale and not res.changed
