"""LTE periods E0–E10: the owner's acceptance table (scenarios 1–64a) ported as table tests (05 §2.1.9).

Owner kinds are mapped onto SvBG engine kinds: ``freeze`` (= unfreeze in the owner's code) → ``unfreeze``,
``panel_id_changed`` → ``unclassified`` (no term change → E0). Checkpoint replay of the owner's code is
replaced by ``simulate`` from the start of the series or from an imported point (``StateInput``).
"""

from __future__ import annotations

import calendar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from svbg.ext.lte import periods as p
from svbg.ext.lte.model import MSK

PARAMS = p.PeriodParams()


def m(month: int, day: int, hour: int = 0, minute: int = 0, *, year: int = 2026) -> datetime:
    """A moment in Moscow time (every time of the owner's table is Moscow time)."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def ev(
    at: datetime,
    *,
    kind: str = "paid",
    new_end: datetime | None = None,
    old_end: datetime | None = None,
    paid_at: datetime | None = None,
    is_trial: bool | None = None,
    was_trial: bool | None = None,
    daily: bool | None = None,
    new_row: bool = False,
    source: str | None = None,
    eid: int | None = None,
) -> p.Event:
    return p.Event(
        occurred_at=at,
        kind=kind,  # type: ignore[arg-type]
        paid_at=paid_at,
        old_end=old_end,
        new_end=new_end,
        was_trial=was_trial,
        is_trial=is_trial,
        is_daily_tariff=daily,
        is_new_row=new_row,
        source=source,
        event_id=eid,
    )


def state_at(
    anchor_at: datetime,
    *,
    kind: str = "paid",
    coverage_end: datetime | None = None,
    is_trial: bool = False,
    series_open: bool = True,
    series_closed_at: datetime | None = None,
    period_state: str = "open",
    at: datetime | None = None,
) -> p.AnchorState:
    """A series with a live period — "as if the series ran for a while"."""
    base = p.AnchorState(
        anchor_at=anchor_at,
        anchor_kind=kind,  # type: ignore[arg-type]
        anchor_source="fixture",
        series_started_at=anchor_at,
        series_open=series_open,
        series_closed_at=series_closed_at,
        coverage_end=coverage_end,
        is_trial=is_trial,
    )
    if not series_open:
        return base
    period = p.rebuild_period(base, at or anchor_at, params=PARAMS)
    return replace(base, period=replace(period, state=period_state))


def spans(out: p.Outcome) -> list[tuple[datetime, datetime, str]]:
    return [(per.starts_at, per.ended_at or per.planned_end_at, per.state) for per in out.periods]


def steps(out: p.Outcome) -> list[str]:
    return [a.cause for a in out.actions if a.kind == "step"]


def action_pairs(out: p.Outcome) -> list[tuple[str, datetime]]:
    return [(a.kind, a.at) for a in out.actions]


# ---------------------------------------------------------------- month boundary


def test_boundary_all_days_24_months_no_drift() -> None:
    for day in range(1, 32):
        anchor = m(1, day, 15, 0, year=2028)
        for k in range(1, 25):
            edge = p.boundary(anchor, k).astimezone(MSK)
            year, month = p.add_months(2028, 1, k)
            assert (edge.year, edge.month) == (year, month)
            assert (edge.hour, edge.minute, edge.second) == (0, 0, 0)
            assert edge.day == min(day, calendar.monthrange(year, month)[1]), (day, k, edge)


def test_boundary_leap_february_and_back() -> None:
    anchor = m(1, 31, 15, 0, year=2028)
    assert p.boundary(anchor, 1) == m(2, 29, year=2028)
    assert p.boundary(anchor, 2) == m(3, 31, year=2028)
    assert p.boundary(anchor, 3) == m(4, 30, year=2028)
    assert p.boundary(anchor, 4) == m(5, 31, year=2028)
    assert p.boundary(m(1, 31, 15, 0, year=2027), 1) == m(2, 28, year=2027)


def test_boundary_rejects_negative_k() -> None:
    with pytest.raises(ValueError, match="k"):
        p.boundary(m(9, 7, 14), -1)


def test_index_at_and_period_bounds() -> None:
    anchor = m(9, 7, 14)
    assert p.index_at(anchor, anchor) == 0
    assert p.index_at(anchor, m(10, 6, 23, 59)) == 0
    assert p.index_at(anchor, m(10, 7)) == 1
    assert p.index_at(anchor, m(12, 7)) == 3
    assert p.index_at(anchor, m(9, 1)) == 0
    assert p.period_bounds(anchor, 0) == (anchor, m(10, 7))
    assert p.period_bounds(anchor, 2) == (m(11, 7), m(12, 7))


def test_anchor_day_counted_in_msk_not_utc() -> None:
    """Сц. 36–37: 23:30 and 01:00 MSK give days 7 and 8 although both are the 7th in UTC."""
    late = m(10, 7, 23, 30)
    early = m(10, 8, 1, 0)
    assert late.astimezone(UTC).day == 7
    assert early.astimezone(UTC).day == 7
    assert p.anchor_day(late) == 7
    assert p.anchor_day(early) == 8
    assert p.boundary(late, 1) == m(11, 7)
    assert p.boundary(early, 1) == m(11, 8)


# ---------------------------------------------------------------- t_eff and G_eff


def test_t_eff_uses_paid_at_inside_window() -> None:
    event = ev(m(10, 2, 13, 30), paid_at=m(10, 2, 11, 0), old_end=m(10, 1, 12), new_end=m(11, 1, 12))
    eff = p.effective_time(event, params=PARAMS)
    assert eff.at == m(10, 2, 11, 0)
    assert eff.from_paid_at and not eff.out_of_window


def test_t_eff_ignores_paid_at_outside_window() -> None:
    too_old = ev(m(10, 12, 12), paid_at=m(10, 1, 12))
    future = ev(m(10, 12, 12), paid_at=m(10, 12, 12, 5))
    for event in (too_old, future):
        eff = p.effective_time(event, params=PARAMS)
        assert eff.at == event.occurred_at
        assert eff.out_of_window


def test_paid_at_outside_window_marks_needs_review() -> None:
    out = p.apply(
        p.AnchorState(),
        ev(m(10, 12, 12), paid_at=m(10, 1, 12), new_end=m(11, 11, 12), new_row=True),
        params=PARAMS,
    )
    assert out.state.needs_review


def test_g_eff_daily_tariff() -> None:
    assert p.grace_for(p.AnchorState(), None, params=PARAMS) == timedelta(hours=24)
    assert p.grace_for(p.AnchorState(is_daily_tariff=True), None, params=PARAMS) == timedelta(days=7)
    assert p.grace_for(p.AnchorState(), ev(m(9, 1), daily=True), params=PARAMS) == timedelta(days=7)


def test_eff_old_end_falls_back_to_series_coverage() -> None:
    state = state_at(m(9, 5, 12), kind="provisional", coverage_end=m(10, 20, 12))
    value, source = p.effective_old_end(
        state, ev(m(9, 25, 12), kind="import", new_end=m(10, 20, 12), new_row=True)
    )
    assert (value, source) == (m(10, 20, 12), "series_coverage")


# ---------------------------------------------------------------- the owner's scenario table


TRIAL_12 = [ev(m(10, 1, 12), kind="trial", new_end=m(10, 31, 12), is_trial=True, new_row=True, eid=1)]
PAID_08_09 = ev(m(9, 8, 14), new_end=m(10, 8, 14), new_row=True, eid=1)
PAID_07_09 = ev(m(9, 7, 14), new_end=m(12, 6, 14), new_row=True, eid=1)
IMPORT_20_10 = ev(
    m(10, 20, 3), kind="import", old_end=m(10, 17, 12), new_end=m(11, 19, 12), source="import", eid=1
)
PAY_FALLBACK_20_10 = ev(
    m(10, 20, 12, 1),
    paid_at=m(10, 20, 12),
    old_end=m(10, 17, 12),
    new_end=m(11, 19, 12),
    source="site",
    eid=2,
)

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "01-new-payment-90-days",
        "events": [PAID_07_09],
        "until": m(11, 7),
        "anchor": m(9, 7, 14),
        "kind": "paid",
        "spans": [
            (m(9, 7, 14), m(10, 7), "closed"),
            (m(10, 7), m(11, 7), "closed"),
            (m(11, 7), m(12, 7), "open"),
        ],
        "rules_include": ("E3",),
    },
    {
        "id": "02-deferred-reset-and-series-end",
        "events": [PAID_08_09],
        "until": m(10, 10),
        "anchor": m(9, 8, 14),
        "series_open": False,
        "spans": [(m(9, 8, 14), m(10, 9, 14), "closed")],
        "steps_include": ("deferred", "series_end"),
    },
    {
        "id": "03-renewal-before-boundary",
        "events": [PAID_08_09, ev(m(10, 7, 20), old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=2)],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E6",),
    },
    {
        "id": "04-renewal-in-deferred-period",
        "events": [PAID_08_09, ev(m(10, 8, 10), old_end=m(10, 8, 14), new_end=m(11, 8, 14), eid=2)],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8, 10), "closed"), (m(10, 8, 10), m(11, 8), "open")],
        "rules_include": ("E6", "E7"),
    },
    {
        "id": "05-late-21h-in-grace",
        "events": [PAID_08_09, ev(m(10, 9, 11), old_end=m(10, 8, 14), new_end=m(11, 8, 14), eid=2)],
        "until": m(10, 10),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 9, 11), "closed"), (m(10, 9, 11), m(11, 8), "open")],
        "rules_include": ("E5", "E7"),
    },
    {
        "id": "06-late-25h-new-series",
        "events": [PAID_08_09, ev(m(10, 9, 15), old_end=m(10, 8, 14), new_end=m(11, 8, 14), eid=2)],
        "until": m(10, 10),
        "anchor": m(10, 9, 15),
        "kind": "paid",
        "spans": [(m(9, 8, 14), m(10, 9, 14), "closed"), (m(10, 9, 15), m(11, 9), "open")],
        "rules_include": ("E3",),
    },
    {
        "id": "07-early-renewal",
        "events": [PAID_08_09, ev(m(10, 1, 12), old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=2)],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E6",),
    },
    {
        "id": "08-switch-to-360-days",
        "events": [
            ev(m(9, 7, 14), new_end=m(10, 7, 14), new_row=True, eid=1),
            ev(m(9, 20, 12), old_end=m(10, 7, 14), new_end=m(9, 15, 14, year=2027), eid=2),
        ],
        "until": m(9, 25),
        "anchor": m(9, 7, 14),
        "spans": [(m(9, 7, 14), m(10, 7), "open")],
        "rules_include": ("E6",),
    },
    {
        "id": "09-referral-days",
        "events": [
            PAID_08_09,
            ev(m(9, 15, 12), kind="bonus", old_end=m(10, 8, 14), new_end=m(10, 22, 14), eid=2),
        ],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E6",),
    },
    {
        "id": "10-promo-without-payment",
        "events": [
            PAID_08_09,
            ev(m(10, 5, 12), kind="bonus", old_end=m(10, 8, 14), new_end=m(10, 15, 14), eid=2),
        ],
        "until": m(10, 20),
        "anchor": m(9, 8, 14),
        "series_open": False,
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(10, 16, 14), "closed")],
    },
    {
        "id": "11-payment-in-grace-after-promo",
        "events": [
            PAID_08_09,
            ev(m(10, 5, 12), kind="bonus", old_end=m(10, 8, 14), new_end=m(10, 15, 14), eid=2),
            ev(m(10, 16, 10), old_end=m(10, 15, 14), new_end=m(11, 15, 14), eid=3),
        ],
        "until": m(10, 20),
        "anchor": m(9, 8, 14),
        "series_open": True,
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E5",),
    },
    {
        "id": "12-trial-after-launch",
        "events": TRIAL_12,
        "until": m(11, 2),
        "anchor": m(10, 1, 12),
        "kind": "trial",
        "series_open": False,
        "spans": [(m(10, 1, 12), m(11, 1, 12), "closed")],
        "steps_include": ("trial", "deferred", "series_end"),
    },
    {
        "id": "13-payment-after-trial-reanchors",
        "events": [
            *TRIAL_12,
            ev(m(10, 20, 15, 3), old_end=m(10, 31, 12), new_end=m(11, 30, 12), is_trial=False, eid=2),
        ],
        "until": m(10, 25),
        "anchor": m(10, 20, 15, 3),
        "kind": "paid",
        "trial": False,
        "spans": [(m(10, 1, 12), m(10, 20, 15, 3), "closed"), (m(10, 20, 15, 3), m(11, 20), "open")],
        "rules_include": ("E4",),
    },
    {
        "id": "14-trial-limit-zero",  # the zero limit is decide's business; here: the trial anchor
        "events": TRIAL_12,
        "until": m(10, 2),
        "kind": "trial",
        "trial": True,
    },
    {
        "id": "15-trial-expired-paid-later",
        "events": [
            ev(m(9, 10, 12), kind="trial", new_end=m(10, 10, 12), is_trial=True, new_row=True, eid=1),
            ev(m(10, 20, 12), old_end=m(10, 10, 12), new_end=m(11, 19, 12), is_trial=False, eid=2),
        ],
        "until": m(10, 25),
        "anchor": m(10, 20, 12),
        "kind": "paid",
        "rules_include": ("E3",),
    },
    {
        "id": "16-revived-by-bonus-then-paid",
        "events": [
            ev(m(9, 1, 12), new_end=m(10, 1, 12), new_row=True, eid=1),
            ev(m(10, 20, 12), kind="bonus", old_end=m(10, 1, 12), new_end=m(10, 27, 12), eid=2),
            ev(m(10, 25, 12), old_end=m(10, 27, 12), new_end=m(11, 24, 12), eid=3),
        ],
        "until": m(10, 28),
        "anchor": m(10, 25, 12),
        "kind": "paid",
        "spans": [
            (m(9, 1, 12), m(10, 2, 12), "closed"),
            (m(10, 20, 12), m(10, 25, 12), "closed"),
            (m(10, 25, 12), m(11, 25), "open"),
        ],
        "rules_include": ("E3", "E4"),
    },
    {
        "id": "17-revived-by-bonus-no-payment",
        "events": [
            ev(m(9, 1, 12), new_end=m(10, 1, 12), new_row=True, eid=1),
            ev(m(10, 20, 12), kind="bonus", old_end=m(10, 1, 12), new_end=m(10, 27, 12), eid=2),
        ],
        "until": m(10, 30),
        "anchor": m(10, 20, 12),
        "kind": "provisional",
        "spans": [(m(9, 1, 12), m(10, 2, 12), "closed"), (m(10, 20, 12), m(10, 28, 12), "closed")],
    },
    {
        "id": "18-bonus-in-grace-keeps-day",
        "events": [
            ev(m(9, 1, 14), new_end=m(10, 1, 14), new_row=True, eid=1),
            ev(m(10, 2, 4), kind="bonus", old_end=m(10, 1, 14), new_end=m(10, 4, 14), eid=2),
        ],
        "until": m(10, 3),
        "anchor": m(9, 1, 14),
        "rules_include": ("E5", "E7"),
    },
    {
        "id": "19-site-before-expiry",
        "events": [
            ev(m(9, 7, 14), new_end=m(10, 7, 14), new_row=True, eid=1),
            ev(
                m(10, 1, 12, 5),
                paid_at=m(10, 1, 12),
                old_end=m(10, 7, 14),
                new_end=m(11, 6, 14),
                source="site",
                eid=2,
            ),
        ],
        "until": m(10, 2),
        "anchor": m(9, 7, 14),
        "rules_include": ("E6",),
    },
    {
        "id": "20-site-after-expiry-anchor-by-paid_at",
        "events": [
            ev(
                m(10, 20, 12, 5),
                paid_at=m(10, 20, 12),
                old_end=m(10, 10, 12),
                new_end=m(11, 19, 12),
                source="site",
                eid=1,
            )
        ],
        "until": m(10, 21),
        "anchor": m(10, 20, 12),
        "kind": "paid",
        "rules_include": ("E3",),
    },
    {
        "id": "21-site-report-after-import",
        "events": [IMPORT_20_10, PAY_FALLBACK_20_10],
        "until": m(10, 21),
        "anchor": m(10, 20, 12),
        "kind": "paid",
    },
    {
        "id": "22-import-without-report",
        "events": [IMPORT_20_10],
        "until": m(10, 21),
        "anchor": m(10, 20, 3),
        "kind": "provisional",
        "needs_review": True,
    },
    {
        "id": "23-payment-without-telegram-like-21",
        "events": [IMPORT_20_10, PAY_FALLBACK_20_10],
        "until": m(10, 21),
        "anchor": m(10, 20, 12),
        "kind": "paid",
    },
    {
        "id": "24-auto-renewal-after-1_5h",
        "events": [PAID_08_09, ev(m(10, 8, 15, 30), old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=2)],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8, 15, 30), "closed"), (m(10, 8, 15, 30), m(11, 8), "open")],
        "rules_include": ("E5", "E7"),
    },
    {
        "id": "25-recurring-payment",
        "events": [
            ev(m(9, 7, 14), new_end=m(10, 7, 14), new_row=True, eid=1),
            ev(m(10, 1, 12), old_end=m(10, 7, 14), new_end=m(11, 6, 14), source="P17", eid=2),
        ],
        "until": m(10, 2),
        "anchor": m(9, 7, 14),
        "rules_include": ("E6",),
    },
    {
        "id": "26-gift-activated-when-expired",
        "events": [ev(m(10, 20, 12), old_end=m(10, 10, 12), new_end=m(11, 19, 12), source="P21", eid=1)],
        "until": m(10, 21),
        "anchor": m(10, 20, 12),
        "kind": "paid",
        "rules_include": ("E3",),
    },
    {
        "id": "27-admin-plus-30-to-expired",
        "events": [
            PAID_08_09,
            ev(m(10, 13, 16), kind="admin", old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=2),
        ],
        "until": m(10, 15),
        "anchor": m(10, 13, 16),
        "kind": "admin",
        "needs_review": False,
        "spans": [(m(9, 8, 14), m(10, 9, 14), "closed"), (m(10, 13, 16), m(11, 13), "open")],
        "rules_include": ("E3",),
    },
    {
        "id": "28-admin-buys-from-wallet",
        "events": [
            ev(m(9, 7, 14), new_end=m(10, 7, 14), new_row=True, eid=1),
            ev(m(10, 1, 12), old_end=m(10, 7, 14), new_end=m(11, 6, 14), source="P22", eid=2),
        ],
        "until": m(10, 2),
        "anchor": m(9, 7, 14),
        "kind": "paid",
        "rules_include": ("E6",),
    },
    {
        "id": "29-ip-guard-hold-with-days-returned",
        "events": [
            ev(m(9, 8, 14), new_end=m(10, 30, 14), new_row=True, eid=1),
            ev(m(11, 15, 12), kind="unfreeze", old_end=m(10, 30, 14), new_end=m(12, 5, 12), eid=2),
        ],
        "holds": [p.HoldInterval(m(10, 10, 12), m(11, 15, 12))],
        "until": m(11, 20),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "series_open": True,
        "spans": [
            (m(9, 8, 14), m(10, 8), "closed"),
            (m(10, 8), m(11, 15, 12), "closed"),
            (m(11, 15, 12), m(12, 8), "open"),
        ],
        "rules_include": ("E1", "E7"),
    },
    {
        "id": "29a-ip-guard-unblocked-expired",
        "events": [ev(m(9, 8, 14), new_end=m(10, 30, 14), new_row=True, eid=1)],
        "holds": [p.HoldInterval(m(10, 10, 12), m(11, 15, 12))],
        "until": m(11, 20),
        "anchor": m(9, 8, 14),
        "series_open": False,
        "series_closed_at": m(11, 15, 12),
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 15, 12), "closed")],
    },
    {
        "id": "29b-unfreeze-of-closed-series",
        "state": state_at(
            m(9, 8, 14), coverage_end=m(10, 8, 14), series_open=False, series_closed_at=m(10, 9, 14)
        ),
        "events": [ev(m(11, 15, 12), kind="unfreeze", new_end=m(12, 5, 12), eid=1)],
        "until": m(11, 20),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "series_open": True,
        "spans": [(m(11, 15, 12), m(12, 8), "open")],
        "rules_include": ("E1",),
        "steps_include": ("unfreeze_reopen",),
    },
    {
        "id": "30-close-by-ip-guard",
        "events": [
            ev(m(9, 8, 14), new_end=m(10, 30, 14), new_row=True, eid=1),
            ev(m(10, 10, 12), kind="close", old_end=m(10, 30, 14), new_end=m(10, 10, 12), eid=2),
        ],
        "until": m(10, 12),
        "anchor": m(9, 8, 14),
        "series_open": False,
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(10, 11, 12), "closed")],
        "rules_include": ("E2",),
    },
    {
        "id": "31-shortened-and-extended-same-day",
        "events": [
            ev(m(9, 8, 14), new_end=m(10, 30, 14), new_row=True, eid=1),
            ev(m(10, 10, 12), kind="admin", old_end=m(10, 30, 14), new_end=m(10, 10, 11), eid=2),
            ev(m(10, 10, 18), kind="admin", old_end=m(10, 10, 11), new_end=m(11, 9, 14), eid=3),
        ],
        "until": m(10, 12),
        "anchor": m(9, 8, 14),
        "series_open": True,
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E2", "E5"),
    },
    {
        "id": "32-refund",  # a refund writes no term event: the anchor does not move
        "events": [PAID_08_09],
        "until": m(9, 20),
        "anchor": m(9, 8, 14),
        "kind": "paid",
    },
    {
        "id": "33-accounts-merged",
        "events": [PAID_08_09, p.Event(occurred_at=m(9, 20, 12), kind="unclassified", event_id=2)],
        "until": m(9, 25),
        "anchor": m(9, 8, 14),
        "rules_include": ("E0",),
    },
    {
        "id": "34-panel-account-recreated",
        "events": [PAID_08_09, p.Event(occurred_at=m(9, 20, 12), kind="unclassified", event_id=2)],
        "until": m(9, 25),
        "anchor": m(9, 8, 14),
        "rules_include": ("E0",),
    },
    {
        "id": "35-anchor-31-january",
        "events": [ev(m(1, 31, 15, year=2028), new_end=m(1, 31, 15, year=2029), new_row=True, eid=1)],
        "until": m(6, 1, year=2028),
        "anchor": m(1, 31, 15, year=2028),
        "spans": [
            (m(1, 31, 15, year=2028), m(2, 29, year=2028), "closed"),
            (m(2, 29, year=2028), m(3, 31, year=2028), "closed"),
            (m(3, 31, year=2028), m(4, 30, year=2028), "closed"),
            (m(4, 30, year=2028), m(5, 31, year=2028), "closed"),
            (m(5, 31, year=2028), m(6, 30, year=2028), "open"),
        ],
    },
    {
        "id": "36-payment-23_30-msk",
        "events": [ev(m(10, 7, 23, 30), new_end=m(1, 5, 12, year=2027), new_row=True, eid=1)],
        "until": m(10, 8),
        "anchor_day": 7,
        "spans": [(m(10, 7, 23, 30), m(11, 7), "open")],
    },
    {
        "id": "37-payment-01_00-msk",
        "events": [ev(m(10, 8, 1), new_end=m(1, 5, 12, year=2027), new_row=True, eid=1)],
        "until": m(10, 9),
        "anchor_day": 8,
        "spans": [(m(10, 8, 1), m(11, 8), "open")],
    },
    {
        "id": "38-plan-change",
        "events": [
            ev(m(9, 7, 14), new_end=m(10, 7, 14), new_row=True, eid=1),
            ev(m(9, 20, 12), kind="paid", old_end=m(10, 7, 14), new_end=m(10, 20, 12), source="C1", eid=2),
        ],
        "until": m(9, 25),
        "anchor": m(9, 7, 14),
        "rules_include": ("E6",),
    },
    {
        "id": "39-daily-plan-pause-3-days",
        "events": [
            ev(m(9, 1, 12), new_end=m(9, 10, 12), daily=True, new_row=True, eid=1),
            ev(m(9, 13, 12), old_end=m(9, 10, 12), new_end=m(9, 20, 12), daily=True, eid=2),
        ],
        "until": m(9, 14),
        "anchor": m(9, 1, 12),
        "rules_include": ("E5",),
    },
    {
        "id": "39a-daily-plan-pause-8-days",
        "events": [
            ev(m(9, 1, 12), new_end=m(9, 10, 12), daily=True, new_row=True, eid=1),
            ev(m(9, 18, 12), old_end=m(9, 10, 12), new_end=m(9, 28, 12), daily=True, eid=2),
        ],
        "until": m(9, 20),
        "anchor": m(9, 18, 12),
        "kind": "paid",
        "rules_include": ("E3",),
    },
    {
        "id": "40-panel-only-admin-extended-in-ui",
        "events": [
            ev(
                m(10, 20, 3),
                kind="import",
                old_end=m(10, 17, 12),
                new_end=m(11, 19, 12),
                source="panel",
                eid=1,
            )
        ],
        "until": m(10, 21),
        "anchor": m(10, 20, 3),
        "kind": "provisional",
        "needs_review": True,
    },
    {
        "id": "41-forever-subscription-monthly",
        "state": state_at(
            m(9, 3, 12), kind="provisional", coverage_end=m(12, 31, year=2099), at=m(9, 18, 12)
        ),
        "events": [],
        "until": m(12, 1),
        "anchor": m(9, 3, 12),
        "kind": "provisional",
        "spans": [
            (m(9, 3, 12), m(10, 3), "closed"),
            (m(10, 3), m(11, 3), "closed"),
            (m(11, 3), m(12, 3), "open"),
        ],
    },
    {
        "id": "42-two-payments-almost-at-once",
        "events": [
            ev(m(10, 7, 12), new_end=m(11, 6, 12), new_row=True, eid=1),
            ev(m(10, 7, 12, 1), old_end=m(11, 6, 12), new_end=m(12, 6, 12), eid=2),
        ],
        "until": m(10, 10),
        "anchor": m(10, 7, 12),
        "rules_include": ("E3", "E6"),
    },
    {
        "id": "43-bot-down-across-boundary",
        "events": [ev(m(9, 8, 14), new_end=m(11, 8, 14), new_row=True, eid=1)],
        "until": m(10, 8, 1, 20),
        "anchor": m(9, 8, 14),
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
    },
    {
        "id": "44-credits-expire-on-reset",
        "events": [ev(m(9, 8, 14), new_end=m(11, 8, 14), new_row=True, eid=1)],
        "until": m(10, 8, 1, 20),
        "actions_include": [("expire_credits", m(10, 8)), ("release_blocks", m(10, 8))],
    },
    {
        "id": "45-imported-trial-then-paid",
        "state": state_at(m(9, 3, 12), kind="trial", coverage_end=m(10, 3, 12), is_trial=True),
        "events": [ev(m(9, 20, 12), old_end=m(10, 3, 12), new_end=m(11, 2, 12), is_trial=False, eid=1)],
        "until": m(9, 25),
        "anchor": m(9, 20, 12),
        "kind": "paid",
        "spans": [(m(9, 3, 12), m(9, 20, 12), "closed"), (m(9, 20, 12), m(10, 20), "open")],
        "rules_include": ("E4",),
    },
    {
        "id": "48-site-early-renewal",
        "events": [
            ev(m(9, 1, 12), new_end=m(9, 30, 12), new_row=True, eid=1),
            ev(
                m(9, 25, 12),
                paid_at=m(9, 25, 11),
                old_end=m(9, 30, 12),
                new_end=m(10, 30, 12),
                source="site",
                eid=2,
            ),
        ],
        "until": m(9, 28),
        "anchor": m(9, 1, 12),
        "rules_include": ("E6",),
    },
    {
        "id": "49-site-report-two-days-after-import",
        "events": [
            ev(m(9, 1, 12), new_end=m(9, 30, 12), new_row=True, eid=1),
            ev(m(9, 25, 12), kind="import", old_end=m(9, 30, 12), new_end=m(10, 30, 12), eid=2),
            ev(
                m(9, 27, 12),
                paid_at=m(9, 27, 11),
                old_end=m(9, 30, 12),
                new_end=m(10, 30, 12),
                source="site",
                eid=3,
            ),
        ],
        "until": m(9, 28),
        "anchor": m(9, 1, 12),
        "rules_include": ("E6",),
    },
    {
        "id": "50-site-payment-before-e8-bot-extended-later",
        "events": [
            ev(m(9, 1, 12), new_end=m(10, 1, 12), new_row=True, eid=1),
            ev(m(10, 2, 13, 30), paid_at=m(10, 2, 11), old_end=m(10, 1, 12), new_end=m(11, 1, 12), eid=2),
        ],
        "until": m(10, 5),
        "anchor": m(9, 1, 12),
        "series_open": True,
        "spans": [(m(9, 1, 12), m(10, 2, 11), "closed"), (m(10, 2, 11), m(11, 1), "open")],
        "rules_include": ("E5", "E7"),
    },
    {
        "id": "52-panel-user-registers-in-bot",
        "state": state_at(m(9, 5, 12), kind="provisional", coverage_end=m(10, 20, 12)),
        "events": [ev(m(9, 25, 12), kind="import", new_end=m(10, 20, 12), new_row=True, eid=1)],
        "until": m(9, 28),
        "anchor": m(9, 5, 12),
        "kind": "provisional",
        "rules_include": ("E6",),
        "steps_include": ("extend",),
    },
    {
        "id": "53-import-without-panel-id",
        "events": [ev(m(9, 25, 12), kind="import", new_end=m(10, 20, 12), new_row=True, eid=1)],
        "until": m(9, 28),
        "anchor": m(9, 25, 12),
        "kind": "provisional",
        "needs_review": True,
        "rules_include": ("E3",),
    },
    {
        "id": "54-imported-point-then-renewal",
        "inputs": [
            p.StateInput(at=m(10, 5, 14), state=state_at(m(9, 8, 14), coverage_end=m(10, 5, 12)), row_id=1),
            ev(m(10, 5, 16), old_end=m(10, 5, 12), new_end=m(11, 4, 14), eid=1),
        ],
        "until": m(10, 6),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "spans": [(m(9, 8, 14), m(10, 8), "open")],
        "rules_include": ("import", "E5"),
    },
    {
        "id": "55-reconciliation-found-payment",
        "events": [
            ev(m(10, 21, 4), paid_at=m(10, 20, 15), old_end=m(10, 10, 12), new_end=m(11, 19, 12), eid=1)
        ],
        "until": m(10, 22),
        "anchor": m(10, 20, 15),
        "kind": "paid",
        "rules_include": ("E3",),
    },
    {
        # the exemption does not touch anchors; admin's extension of a live provisional series is E4 (admin)
        "id": "56-owner-forever-admin-extended",
        "state": state_at(m(9, 3, 12), kind="provisional", coverage_end=m(1, 1, year=2099), at=m(11, 3, 12)),
        "events": [
            ev(m(11, 10, 12), kind="admin", old_end=m(1, 1, year=2099), new_end=m(12, 31, year=2099), eid=1)
        ],
        "until": m(11, 20),
        "anchor": m(11, 10, 12),
        "kind": "admin",
        "rules_include": ("E4",),
        "actions_exclude": ("revoke_exemption",),
    },
    {
        "id": "57-launch-trial-paid",
        "state": state_at(m(9, 1, 12), kind="trial", coverage_end=m(10, 1, 12), is_trial=True),
        "events": [
            ev(m(9, 30, 15, 3), old_end=m(10, 1, 12), new_end=m(10, 30, 15, 3), is_trial=False, eid=1)
        ],
        "until": m(10, 5),
        "anchor": m(9, 30, 15, 3),
        "kind": "paid",
        "trial": False,
        "spans": [(m(9, 1, 12), m(9, 30, 15, 3), "closed"), (m(9, 30, 15, 3), m(10, 30), "open")],
        "rules_include": ("E4",),
        "actions_include": [("revoke_exemption", m(9, 30, 15, 3))],
    },
    {
        "id": "58-trial-after-launch",
        "events": [ev(m(10, 5, 12), kind="trial", new_end=m(11, 4, 12), is_trial=True, new_row=True, eid=1)],
        "until": m(10, 10),
        "anchor": m(10, 5, 12),
        "kind": "trial",
        "trial": True,
        "spans": [(m(10, 5, 12), m(11, 5), "open")],
    },
    {
        "id": "59-admin-extends-expired",
        "events": [
            PAID_08_09,
            ev(m(10, 20, 16, 40), kind="admin", old_end=m(10, 8, 14), new_end=m(11, 19, 14), eid=2),
        ],
        "until": m(10, 25),
        "anchor": m(10, 20, 16, 40),
        "kind": "admin",
        "needs_review": False,
        "spans": [(m(9, 8, 14), m(10, 9, 14), "closed"), (m(10, 20, 16, 40), m(11, 20), "open")],
        "rules_include": ("E3",),
    },
    {
        "id": "60-admin-extends-live",
        "events": [
            PAID_08_09,
            ev(m(10, 2, 12), kind="admin", old_end=m(10, 8, 14), new_end=m(10, 23, 14), eid=2),
        ],
        "until": m(10, 9),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "spans": [(m(9, 8, 14), m(10, 8), "closed"), (m(10, 8), m(11, 8), "open")],
        "rules_include": ("E6",),
    },
    {
        "id": "60a-admin-extends-in-grace",
        "events": [
            PAID_08_09,
            ev(m(10, 8, 23), kind="admin", old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=2),
        ],
        "until": m(10, 10),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "spans": [(m(9, 8, 14), m(10, 8, 23), "closed"), (m(10, 8, 23), m(11, 8), "open")],
        "rules_include": ("E5", "E7"),
    },
    {
        "id": "61-admin-extends-launch-trial",
        "state": state_at(m(9, 1, 12), kind="trial", coverage_end=m(10, 1, 12), is_trial=True),
        "events": [
            ev(m(9, 22, 12), kind="admin", old_end=m(10, 1, 12), new_end=m(11, 21, 12), is_trial=True, eid=1)
        ],
        "until": m(9, 25),
        "anchor": m(9, 22, 12),
        "kind": "admin",
        "trial": True,
        "spans": [(m(9, 1, 12), m(9, 22, 12), "closed"), (m(9, 22, 12), m(10, 22), "open")],
        "rules_include": ("E4",),
        "actions_exclude": ("revoke_exemption",),
    },
    {
        "id": "61a-first-payment-after-admin-extended-trial",
        "state": state_at(
            m(9, 22, 12), kind="admin", coverage_end=m(11, 21, 12), is_trial=True, at=m(10, 1, 12)
        ),
        "events": [ev(m(10, 10, 16), old_end=m(11, 21, 12), new_end=m(12, 21, 12), is_trial=True, eid=1)],
        "until": m(10, 15),
        "anchor": m(9, 22, 12),
        "kind": "admin",
        "trial": False,
        "spans": [(m(9, 22, 12), m(10, 10, 16), "closed"), (m(10, 10, 16), m(10, 22), "open")],
        "rules_include": ("E6", "E6a"),
        "steps_include": ("trial_conversion",),
        "actions_include": [("revoke_exemption", m(10, 10, 16))],
    },
    {
        "id": "62-admin-drops-trial-flag-with-extension",
        "state": state_at(m(10, 5, 12), kind="trial", coverage_end=m(11, 4, 12), is_trial=True),
        "events": [
            ev(
                m(10, 12, 12),
                kind="admin",
                old_end=m(11, 4, 12),
                new_end=m(12, 4, 12),
                is_trial=False,
                was_trial=True,
                eid=1,
            )
        ],
        "until": m(10, 15),
        "anchor": m(10, 12, 12),
        "kind": "admin",
        "trial": False,
        "spans": [(m(10, 5, 12), m(10, 12, 12), "closed"), (m(10, 12, 12), m(11, 12), "open")],
        "rules_include": ("E4",),
        "rules_exclude": ("E9",),
    },
    {
        "id": "63-term-change-without-kind",
        "state": state_at(m(9, 8, 14), coverage_end=m(10, 8, 14)),
        "events": [ev(m(10, 1, 12), kind="bonus", old_end=m(10, 8, 14), new_end=m(11, 7, 14), eid=1)],
        "until": m(10, 5),
        "anchor": m(9, 8, 14),
        "kind": "paid",
        "rules_include": ("E6",),
    },
    {
        "id": "64-admin-trial-then-paid",
        "events": [
            ev(
                m(10, 5, 12),
                kind="trial",
                new_end=m(11, 4, 12),
                is_trial=True,
                new_row=True,
                source="T7",
                eid=1,
            ),
            ev(m(10, 15, 14, 20), old_end=m(11, 4, 12), new_end=m(12, 4, 12), is_trial=False, eid=2),
        ],
        "until": m(10, 20),
        "anchor": m(10, 15, 14, 20),
        "kind": "paid",
        "trial": False,
        "spans": [(m(10, 5, 12), m(10, 15, 14, 20), "closed"), (m(10, 15, 14, 20), m(11, 15), "open")],
        "rules_include": ("E3", "E4"),
    },
    {
        "id": "64a-admin-grants-paid",
        "events": [
            ev(
                m(10, 5, 12),
                kind="admin",
                new_end=m(11, 4, 12),
                is_trial=False,
                new_row=True,
                source="B10",
                eid=1,
            )
        ],
        "until": m(10, 10),
        "anchor": m(10, 5, 12),
        "kind": "admin",
        "trial": False,
        "needs_review": False,
        "spans": [(m(10, 5, 12), m(11, 5), "open")],
        "rules_include": ("E3",),
    },
]


@pytest.mark.parametrize("case", SCENARIOS, ids=[case["id"] for case in SCENARIOS])
def test_scenarios(case: dict[str, Any]) -> None:
    inputs = case.get("inputs") or case.get("events") or []
    out = p.simulate(
        case.get("state") or p.AnchorState(),
        inputs,
        until=case["until"],
        params=PARAMS,
        holds=case.get("holds", ()),
    )
    assert not out.truncated
    if "anchor" in case:
        assert out.state.anchor_at == case["anchor"], f"anchor: {out.state.anchor_at}"
    if "anchor_day" in case:
        assert out.state.anchor_at is not None
        assert p.anchor_day(out.state.anchor_at) == case["anchor_day"]
    if "kind" in case:
        assert out.state.anchor_kind == case["kind"]
    if "series_open" in case:
        assert out.state.series_open is case["series_open"]
    if "series_closed_at" in case:
        assert out.state.series_closed_at == case["series_closed_at"]
    if "spans" in case:
        assert spans(out) == case["spans"]
    if "trial" in case:
        assert out.state.is_trial is case["trial"]
        if out.state.period is not None:
            assert out.state.period.is_trial is case["trial"]
    if "needs_review" in case:
        assert out.state.needs_review is case["needs_review"]
    for rule in case.get("rules_include", ()):
        assert rule in out.rules, f"{rule} ∉ {out.rules}"
    for rule in case.get("rules_exclude", ()):
        assert rule not in out.rules, f"{rule} ∈ {out.rules}"
    for cause in case.get("steps_include", ()):
        assert cause in steps(out), f"{cause} ∉ {steps(out)}"
    for pair in case.get("actions_include", ()):
        assert tuple(pair) in action_pairs(out), f"{pair} ∉ {action_pairs(out)}"
    for kind in case.get("actions_exclude", ()):
        assert kind not in [a.kind for a in out.actions]


def test_scenario_table_size() -> None:
    """The owner's acceptance table has 67 rows; the replay rows 46, 47, 51, 51a are separate tests below."""
    assert len(SCENARIOS) == 67
    assert len({case["id"] for case in SCENARIOS}) == len(SCENARIOS)


# ---------------------------------------------------------------- rule order


@pytest.mark.parametrize("anchor_kind", ["paid", "admin", "manual", "import"])
@pytest.mark.parametrize("kind", ["paid", "admin"])
def test_rule_order_e5_before_e3_for_paid_admin_manual(anchor_kind: str, kind: str) -> None:
    state = state_at(m(9, 8, 14), kind=anchor_kind, coverage_end=m(10, 8, 14))
    event = ev(m(10, 9, 10), kind=kind, old_end=m(10, 8, 14), new_end=m(11, 8, 14))
    rule = p.match_rule(state, event, t_eff=event.occurred_at, eff_old_end=m(10, 8, 14), grace=PARAMS.grace)
    assert rule == "E5"


@pytest.mark.parametrize("anchor_kind", ["trial", "provisional"])
@pytest.mark.parametrize("kind", ["paid", "admin"])
def test_rule_order_e4_before_e5_for_trial_provisional_in_grace(anchor_kind: str, kind: str) -> None:
    state = state_at(
        m(9, 1, 12), kind=anchor_kind, coverage_end=m(10, 1, 12), is_trial=anchor_kind == "trial"
    )
    event = ev(m(10, 1, 20), kind=kind, old_end=m(10, 1, 12), new_end=m(10, 31, 20))
    rule = p.match_rule(state, event, t_eff=event.occurred_at, eff_old_end=m(10, 1, 12), grace=PARAMS.grace)
    assert rule == "E4"


@pytest.mark.parametrize("anchor_kind", ["trial", "provisional"])
def test_bonus_in_grace_of_trial_still_e5(anchor_kind: str) -> None:
    state = state_at(
        m(9, 1, 12), kind=anchor_kind, coverage_end=m(10, 1, 12), is_trial=anchor_kind == "trial"
    )
    event = ev(m(10, 1, 20), kind="bonus", old_end=m(10, 1, 12), new_end=m(10, 8, 20))
    rule = p.match_rule(state, event, t_eff=event.occurred_at, eff_old_end=m(10, 1, 12), grace=PARAMS.grace)
    assert rule == "E5"


def test_rule_order_e4_before_e6() -> None:
    state = state_at(m(9, 1, 12), kind="trial", coverage_end=m(10, 1, 12), is_trial=True)
    event = ev(m(9, 20, 12), old_end=m(10, 1, 12), new_end=m(11, 2, 12))
    rule = p.match_rule(state, event, t_eff=event.occurred_at, eff_old_end=m(10, 1, 12), grace=PARAMS.grace)
    assert rule == "E4"


def test_e4_does_not_touch_paid_admin_manual() -> None:
    for kind in ("paid", "admin", "manual", "import"):
        state = state_at(m(9, 8, 14), kind=kind, coverage_end=m(10, 8, 14))
        event = ev(m(10, 2, 12), old_end=m(10, 8, 14), new_end=m(11, 1, 12))
        rule = p.match_rule(
            state, event, t_eff=event.occurred_at, eff_old_end=m(10, 8, 14), grace=PARAMS.grace
        )
        assert rule == "E6", kind


def test_freeze_event_itself_changes_nothing() -> None:
    """SvBG writes ``frozen`` without a new end: it must not look like "term ends now" (E2)."""
    state = state_at(m(9, 8, 14), coverage_end=m(10, 8, 14))
    out = p.apply(state, ev(m(9, 20, 12), kind="freeze", old_end=m(10, 8, 14), new_end=None), params=PARAMS)
    assert out.rules == ("E0",)
    assert out.state == state


# ---------------------------------------------------------------- trial conversion by payment


def _expired_trial(*, anchor_kind: str = "trial") -> p.AnchorState:
    return state_at(m(9, 1, 12), kind=anchor_kind, coverage_end=m(10, 1, 12), is_trial=True, at=m(9, 20))


@pytest.mark.parametrize("row_trial", [False, True, None])
def test_trial_paid_within_grace_after_expiry_reanchors(row_trial: bool | None) -> None:
    payment = ev(
        m(10, 1, 20),
        old_end=m(10, 1, 12),
        new_end=m(10, 31, 20),
        is_trial=row_trial,
        was_trial=True if row_trial is False else None,
        eid=1,
    )
    out = p.simulate(_expired_trial(), [payment], until=m(10, 3), params=PARAMS)
    assert "E4" in out.rules and "E5" not in out.rules
    assert out.state.anchor_at == m(10, 1, 20)
    assert out.state.anchor_kind == "paid"
    assert out.state.is_trial is False
    assert out.state.period is not None
    assert out.state.period.is_trial is False
    assert out.state.period.series_first is True
    assert spans(out) == [(m(9, 1, 12), m(10, 1, 20), "closed"), (m(10, 1, 20), m(11, 1), "open")]
    assert [a.period.end_cause for a in out.actions_of("close_period") if a.period] == ["reanchor"]
    assert [a.cause for a in out.actions_of("release_blocks")] == ["reanchor"]
    assert ("revoke_exemption", m(10, 1, 20)) in action_pairs(out)
    assert out.events[-1].effect == "reanchor"


def test_provisional_paid_within_grace_after_expiry_reanchors() -> None:
    out = p.simulate(
        state_at(m(9, 1, 12), kind="provisional", coverage_end=m(10, 1, 12), at=m(9, 20)),
        [ev(m(10, 2, 11), old_end=m(10, 1, 12), new_end=m(11, 1, 11), eid=1)],
        until=m(10, 3),
        params=PARAMS,
    )
    assert "E4" in out.rules
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(10, 2, 11), "paid")


def test_admin_extends_trial_within_grace_reanchors_as_admin() -> None:
    out = p.simulate(
        _expired_trial(),
        [ev(m(10, 1, 20), kind="admin", old_end=m(10, 1, 12), new_end=m(11, 30, 20), is_trial=True, eid=1)],
        until=m(10, 3),
        params=PARAMS,
    )
    assert "E4" in out.rules
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(10, 1, 20), "admin")
    assert out.state.is_trial is True
    assert not out.actions_of("revoke_exemption")


def test_autobuy_of_live_trial_converts_to_paid() -> None:
    out = p.apply(
        _expired_trial(),
        ev(m(9, 20, 12), old_end=m(10, 1, 12), new_end=m(10, 31, 12), is_trial=True, eid=1),
        params=PARAMS,
    )
    assert out.rules[0] == "E4"
    assert out.state.anchor_kind == "paid"
    assert out.state.is_trial is False
    assert out.state.period is not None and out.state.period.is_trial is False
    assert out.actions_of("revoke_exemption")


def test_autobuy_of_expired_trial_opens_paid_series() -> None:
    out = p.simulate(
        _expired_trial(),
        [ev(m(10, 5, 12), old_end=m(10, 1, 12), new_end=m(11, 4, 12), is_trial=True, eid=1)],
        until=m(10, 6),
        params=PARAMS,
    )
    assert "E3" in out.rules
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(10, 5, 12), "paid")
    assert out.state.is_trial is False
    assert out.state.period is not None and out.state.period.is_trial is False
    assert out.events[-1].effect == "new_series"


def test_paid_new_series_on_trial_row_is_paid() -> None:
    out = p.apply(
        p.AnchorState(),
        ev(m(10, 5, 12), new_end=m(11, 4, 12), is_trial=True, new_row=True, eid=1),
        params=PARAMS,
    )
    assert out.state.anchor_kind == "paid"
    assert out.state.is_trial is False


def test_scenario_64_admin_trial_then_payment_with_trial_flag() -> None:
    out = p.simulate(
        p.AnchorState(),
        [
            ev(m(10, 5, 12), kind="trial", new_end=m(11, 4, 12), is_trial=True, new_row=True, eid=1),
            ev(m(10, 15, 14, 20), old_end=m(11, 4, 12), new_end=m(12, 4, 12), is_trial=True, eid=2),
        ],
        until=m(10, 20),
        params=PARAMS,
    )
    assert out.rules[:2] == ("E3", "E4")
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(10, 15, 14, 20), "paid")
    assert out.state.is_trial is False
    assert spans(out) == [(m(10, 5, 12), m(10, 15, 14, 20), "closed"), (m(10, 15, 14, 20), m(11, 15), "open")]


def test_linked_extension_is_an_extend_step_with_detail() -> None:
    state = state_at(m(9, 5, 12), kind="provisional", coverage_end=m(10, 20, 12))
    out = p.apply(
        state, ev(m(9, 25, 12), kind="import", new_end=m(10, 20, 12), new_row=True, eid=5), params=PARAMS
    )
    rows = list(out.actions_of("step"))
    assert [row.cause for row in rows] == ["extend"]
    assert rows[0].detail.get("linked") is True
    assert rows[0].event_id == 5
    plain = p.apply(
        state,
        ev(m(9, 25, 12), kind="bonus", old_end=m(10, 20, 12), new_end=m(10, 27, 12), eid=6),
        params=PARAMS,
    )
    assert not [a for a in plain.actions_of("step") if a.detail.get("linked")]


def test_late_site_report_before_link_reanchors_on_replay() -> None:
    """A late site report with ``t_eff`` before a later import re-anchors when the series is replayed."""
    initial = state_at(m(9, 5, 12), kind="provisional", coverage_end=m(10, 20, 12), at=m(9, 18))
    link = ev(m(9, 25, 12), kind="import", new_end=m(10, 20, 12), new_row=True, eid=1)
    late = ev(m(9, 26, 12), paid_at=m(9, 20, 12), old_end=m(10, 20, 12), new_end=m(11, 19, 12), eid=2)
    out = p.simulate(initial, [link, late], until=m(9, 27), params=PARAMS)
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(9, 20, 12), "paid")
    assert "E4" in out.rules


# ---------------------------------------------------------------- unknown coverage, E6a, E9, holds


def test_boundary_with_unknown_coverage_resets_and_flags_review() -> None:
    state = state_at(m(9, 8, 14), kind="provisional", coverage_end=None, at=m(9, 18))
    out = p.simulate(state, [], until=m(11, 9), params=PARAMS)
    assert spans(out) == [
        (m(9, 8, 14), m(10, 8), "closed"),
        (m(10, 8), m(11, 8), "closed"),
        (m(11, 8), m(12, 8), "open"),
    ]
    assert out.state.series_open
    assert out.state.needs_review
    assert [a.cause for a in out.actions_of("release_blocks")] == ["reset", "reset"]
    assert "deferred" not in steps(out)
    assert len(out.actions_of("needs_review")) == 1


def test_e3_priority_is_trial_beats_admin_mark() -> None:
    trial = p.apply(
        p.AnchorState(),
        ev(m(10, 5, 12), kind="admin", new_end=m(11, 4, 12), is_trial=True, new_row=True),
        params=PARAMS,
    )
    paid = p.apply(
        p.AnchorState(),
        ev(m(10, 5, 12), kind="admin", new_end=m(11, 4, 12), is_trial=False, new_row=True),
        params=PARAMS,
    )
    assert trial.state.anchor_kind == "trial"
    assert paid.state.anchor_kind == "admin"


def test_e6a_idempotent_and_narrow() -> None:
    state = state_at(m(9, 22, 12), kind="admin", coverage_end=m(11, 21, 12), is_trial=True, at=m(10, 1, 12))
    first = p.apply(
        state, ev(m(10, 10, 16), old_end=m(11, 21, 12), new_end=m(12, 21, 12), is_trial=True), params=PARAMS
    )
    assert "E6a" in first.rules
    second = p.apply(
        first.state, ev(m(10, 11, 16), old_end=m(12, 21, 12), new_end=m(1, 21, 12, year=2027)), params=PARAMS
    )
    assert "E6a" not in second.rules
    assert second.state.period == first.state.period
    not_trial = p.apply(
        state_at(m(9, 22, 12), kind="admin", coverage_end=m(11, 21, 12)),
        ev(m(10, 10, 16), old_end=m(11, 21, 12), new_end=m(12, 21, 12)),
        params=PARAMS,
    )
    assert "E6a" not in not_trial.rules
    bonus = p.apply(
        state, ev(m(10, 10, 16), kind="bonus", old_end=m(11, 21, 12), new_end=m(12, 21, 12)), params=PARAMS
    )
    assert "E6a" not in bonus.rules


def test_e9_flips_trial_without_reset() -> None:
    state = state_at(m(10, 5, 12), kind="trial", coverage_end=m(11, 4, 12), is_trial=True)
    out = p.apply(
        state,
        ev(
            m(10, 12, 12),
            kind="bonus",
            old_end=m(11, 4, 12),
            new_end=m(11, 14, 12),
            is_trial=False,
            was_trial=True,
        ),
        params=PARAMS,
    )
    assert "E9" in out.rules
    assert out.state.anchor_kind == "provisional"
    assert out.state.anchor_at == m(10, 5, 12)
    assert out.state.period is not None
    assert out.state.period.starts_at == m(10, 5, 12)
    assert out.state.period.is_trial is False


def test_unfreeze_never_opens_a_series() -> None:
    out = p.apply(p.AnchorState(), ev(m(11, 15, 12), kind="unfreeze", new_end=m(12, 5, 12)), params=PARAMS)
    assert out.rules == ("E1",)
    assert out.state.anchor_at is None
    assert out.state.period is None


def test_series_not_closed_while_hold_alive() -> None:
    state = state_at(m(9, 8, 14), coverage_end=m(10, 8, 14))
    live = [p.HoldInterval(m(10, 1, 12), None)]
    assert p.series_end_at(state, params=PARAMS, holds=live) is None
    out = p.simulate(state, [], until=m(12, 1), params=PARAMS, holds=live)
    assert out.state.series_open
    assert out.state.period is not None
    assert out.state.period.state == "deferred"


def test_series_end_shifts_to_unblock_moment() -> None:
    state = state_at(m(9, 8, 14), coverage_end=m(10, 8, 14))
    assert p.series_end_at(state, params=PARAMS, holds=[p.HoldInterval(m(10, 1, 12), m(11, 15, 12))]) == m(
        11, 15, 12
    )
    early = [p.HoldInterval(m(9, 20, 12), m(9, 25, 12))]
    assert p.series_end_at(state, params=PARAMS, holds=early) == m(10, 9, 14)


# ---------------------------------------------------------------- deferred reset


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [(m(10, 9) - timedelta(seconds=1), "deferred"), (m(10, 9), "open")],
    ids=["B+R-1s", "B+R"],
)
def test_rollover_threshold_exact(coverage: datetime, expected: str) -> None:
    out = p.simulate(state_at(m(9, 8, 14), coverage_end=coverage), [], until=m(10, 8, 0, 1), params=PARAMS)
    assert out.state.period is not None
    assert out.state.period.state == expected
    if expected == "open":
        assert out.state.period.starts_at == m(10, 8)


@pytest.mark.parametrize("kind", ["paid", "bonus", "unfreeze"])
def test_deferred_resolved_by_any_coverage_extension(kind: str) -> None:
    out = p.simulate(
        state_at(m(9, 8, 14), coverage_end=m(10, 8, 14)),
        [ev(m(10, 8, 10), kind=kind, old_end=m(10, 8, 14), new_end=m(11, 8, 14))],
        until=m(10, 9),
        params=PARAMS,
    )
    assert "E7" in out.rules
    assert out.state.period is not None
    assert (out.state.period.starts_at, out.state.period.planned_end_at) == (m(10, 8, 10), m(11, 8))


def test_deferred_without_extension_closes_by_e8() -> None:
    out = p.simulate(state_at(m(9, 8, 14), coverage_end=m(10, 8, 14)), [], until=m(10, 15), params=PARAMS)
    assert not out.state.series_open
    assert out.state.series_closed_at == m(10, 9, 14)
    assert out.state.period is None


def test_every_rule_and_timer_writes_a_step() -> None:
    out = p.simulate(
        p.AnchorState(),
        [PAID_08_09, ev(m(10, 8, 10), old_end=m(10, 8, 14), new_end=m(12, 8, 14), eid=2)],
        until=m(11, 9),
        params=PARAMS,
    )
    assert steps(out) == ["new_series", "deferred", "extend", "deferred_rollover", "boundary"]


# ---------------------------------------------------------------- late events: replay from the start


def test_scenario_51_late_site_report_cancels_series_end() -> None:
    first = ev(m(9, 1, 12), new_end=m(10, 1, 12), new_row=True, eid=1)
    timely = p.simulate(p.AnchorState(), [first], until=m(10, 2, 12, 5), params=PARAMS)
    assert not timely.state.series_open
    late = ev(m(10, 2, 12, 5), paid_at=m(10, 2, 11, 55), old_end=m(10, 1, 12), new_end=m(11, 1, 12), eid=2)
    assert p.process(timely.state, late, now=m(10, 2, 12, 5), params=PARAMS) is None  # late → replay
    replay = p.simulate(p.AnchorState(), [first, late], until=m(10, 2, 12, 5), params=PARAMS)
    assert replay.state.series_open
    assert replay.state.anchor_at == m(9, 1, 12)
    assert "E5" in replay.rules


def test_scenario_51a_late_report_through_boundary_and_deferral() -> None:
    first = ev(m(9, 8, 14), new_end=m(10, 8, 14), new_row=True, eid=1)
    timely = p.simulate(p.AnchorState(), [first], until=m(10, 14), params=PARAMS)
    assert not timely.state.series_open
    late = ev(m(10, 14, 12), paid_at=m(10, 9, 10), old_end=m(10, 8, 14), new_end=m(11, 8, 14), eid=2)
    replay = p.simulate(p.AnchorState(), [first, late], until=m(10, 14, 12), params=PARAMS)
    assert replay.state.anchor_at == m(9, 8, 14)
    assert replay.state.series_open
    assert replay.state.period is not None
    assert replay.state.period.starts_at == m(10, 9, 10)
    assert {"E5", "E7"} <= set(replay.rules)


def test_scenario_46_event_after_imported_point_keeps_day() -> None:
    imported = state_at(m(9, 10, 12), coverage_end=m(10, 10, 12), at=m(9, 18, 12))
    early_renewal = ev(m(9, 25, 12), old_end=m(10, 10, 12), new_end=m(11, 9, 12), eid=7)
    out = p.simulate(
        p.AnchorState(), [p.StateInput(at=m(9, 18, 12), state=imported), early_renewal], until=m(9, 27, 12)
    )
    assert out.state.anchor_at == m(9, 10, 12)
    assert "E6" in out.rules


def test_scenario_47_stale_import_does_not_override_new_series() -> None:
    imported = state_at(m(9, 1, 12), coverage_end=m(9, 20, 12), at=m(9, 20, 12))
    payment = ev(m(9, 22, 12), old_end=m(9, 20, 12), new_end=m(10, 22, 12), eid=9)
    out = p.simulate(
        p.AnchorState(), [p.StateInput(at=m(9, 20, 12), state=imported), payment], until=m(9, 23, 12)
    )
    assert (out.state.anchor_at, out.state.anchor_kind) == (m(9, 22, 12), "paid")
    assert "E3" in out.rules


def test_is_late_detects_applied_timers() -> None:
    state = state_at(m(9, 8, 14), coverage_end=m(11, 8, 14), at=m(10, 9))  # period 1 starts 08.10 00:00
    assert p.is_late(state, m(10, 7, 12))
    assert not p.is_late(state, m(10, 9, 12))
    deferred = state_at(m(9, 8, 14), coverage_end=m(10, 8, 14), period_state="deferred")
    assert p.is_late(deferred, m(10, 7))  # deferral happened at 08.10 00:00
    closed = replace(p.AnchorState(), anchor_at=m(9, 8, 14), series_closed_at=m(10, 9, 14))
    assert p.is_late(closed, m(10, 9))
    assert not p.is_late(closed, m(10, 10))


def test_process_runs_timers_to_now() -> None:
    state = p.simulate(p.AnchorState(), [PAID_08_09], until=m(9, 8, 15)).state
    renewal = ev(m(9, 20, 12), old_end=m(10, 8, 14), new_end=m(11, 8, 14), eid=2)
    out = p.process(state, renewal, now=m(10, 9), params=PARAMS)
    assert out is not None
    assert out.state.period is not None
    assert out.state.period.starts_at == m(10, 8)  # boundary reset after the event, up to now


# ---------------------------------------------------------------- E10 and recompute


def test_set_anchor_moves_boundaries_and_keeps_credits() -> None:
    state = state_at(m(9, 20, 12), kind="provisional", coverage_end=m(11, 20, 12))
    out = p.set_anchor(state, m(9, 8, 14), at=m(10, 1, 12), params=PARAMS)
    assert out.state.anchor_kind == "manual"
    assert out.state.anchor_at == m(9, 8, 14)
    assert out.state.period is not None
    assert (out.state.period.starts_at, out.state.period.planned_end_at) == (m(9, 8, 14), m(10, 8))
    assert not out.actions_of("expire_credits")
    assert out.rules == ("E10",)


def test_manual_anchor_at_modes_and_future_guard() -> None:
    assert p.manual_anchor_at(m(10, 7, 15, 30), mode="day_start", params=PARAMS) == m(10, 7)
    assert p.manual_anchor_at(datetime(2026, 10, 7, tzinfo=UTC).date(), mode="day_start", params=PARAMS) == m(
        10, 7
    )
    assert p.manual_anchor_at(m(10, 7, 15, 30), params=PARAMS) == m(10, 7, 15, 30)
    with pytest.raises(ValueError, match="будущем"):
        p.manual_anchor_at(m(10, 7, 15, 30), params=PARAMS, now=m(10, 7, 15))
    with pytest.raises(ValueError, match="момент времени"):
        p.manual_anchor_at(datetime(2026, 10, 7, tzinfo=UTC).date(), params=PARAMS)


def test_parse_manual_anchor_contract() -> None:
    assert p.parse_manual_anchor("2026-09-01T10:00:00+03:00", "exact") == m(9, 1, 10)
    assert p.parse_manual_anchor("2026-09-01T07:00:00Z", "exact") == m(9, 1, 10)
    assert p.parse_manual_anchor("2026-09-01T10:00", "exact") == m(9, 1, 10)
    assert p.parse_manual_anchor("2026-09-01", "day_start") == datetime(2026, 9, 1).date()
    assert p.parse_manual_anchor(" 2026-09-01 ", "day_start") == datetime(2026, 9, 1).date()
    for bad, mode in (
        ("2026-09-01", "exact"),
        ("01.09.2026", "day_start"),
        ("2026-13-01", "day_start"),
        ("вчера", "exact"),
        ("", "exact"),
        ("   ", "day_start"),
        (None, "exact"),
        (1_756_710_000, "exact"),
        ("2026-09-01T10:00:00+03:00", "bogus"),
    ):
        with pytest.raises(ValueError, match="set_anchor"):
            p.parse_manual_anchor(bad, mode)


def test_manual_anchor_at_accepts_strings() -> None:
    assert p.manual_anchor_at("2026-09-01T10:00:00+03:00", mode="exact", params=PARAMS) == m(9, 1, 10)
    assert p.manual_anchor_at("2026-09-01", mode="day_start", params=PARAMS) == m(9, 1)
    assert p.manual_anchor_at("2026-08-31T21:30:00Z", mode="day_start", params=PARAMS) == m(9, 1)
    with pytest.raises(ValueError, match="будущем"):
        p.manual_anchor_at("2026-09-01", mode="day_start", params=PARAMS, now=m(8, 31, 23))
    with pytest.raises(ValueError, match="set_anchor"):
        p.manual_anchor_at("2026-09-01", mode="exact", params=PARAMS)


def test_plan_recompute_marks_stale_and_inserts_without_deleting() -> None:
    old = [
        p.PeriodState(m(9, 8, 14), 0, m(9, 8, 14), m(10, 8), state="closed", ended_at=m(10, 8), period_id=1),
        p.PeriodState(m(10, 12, 12), 0, m(10, 12, 12), m(11, 12), state="open", period_id=2),
    ]
    new = [
        p.PeriodState(m(9, 8, 14), 0, m(9, 8, 14), m(10, 8), state="closed", ended_at=m(10, 9, 10)),
        p.PeriodState(m(9, 8, 14), 1, m(10, 9, 10), m(11, 8), state="open"),
    ]
    diff = p.plan_recompute(old, new)
    assert [row.period_id for row in diff.stale] == [2]
    assert [(row.period_id, fresh.ended_at) for row, fresh in diff.keep] == [(1, m(10, 9, 10))]
    assert [(row.starts_at, row.planned_end_at) for row in diff.insert] == [(m(10, 9, 10), m(11, 8))]
    assert diff.changed


def test_plan_recompute_ignores_periods_before_since() -> None:
    ancient = p.PeriodState(
        m(1, 8, 14), 0, m(1, 8, 14), m(2, 8), state="closed", ended_at=m(2, 8), period_id=1
    )
    live = p.PeriodState(m(9, 8, 14), 0, m(9, 8, 14), m(10, 8), state="open", period_id=2)
    diff = p.plan_recompute([ancient, live], [live], since=m(9, 1))
    assert diff.stale == ()
    assert [row.period_id for row, _ in diff.keep] == [2]


def test_plan_recompute_noop_when_nothing_changed() -> None:
    live = p.PeriodState(m(9, 8, 14), 0, m(9, 8, 14), m(10, 8), state="open", period_id=2)
    diff = p.plan_recompute([live], [p.PeriodState(m(9, 8, 14), 0, m(9, 8, 14), m(10, 8), state="open")])
    assert not diff.changed


# ---------------------------------------------------------------- parameters


def test_params_from_values_clamps_out_of_range() -> None:
    params = p.PeriodParams.from_values({"renewal_grace_hours": 1000, "rollover_min_remaining_hours": "x"})
    assert params.grace == timedelta(hours=168)
    assert params.rollover_min_remaining == timedelta(hours=24)
    assert p.PeriodParams.from_values({"renewal_grace_hours": -3}).grace == timedelta(0)


def test_params_defaults_match_design() -> None:
    params = p.PeriodParams.from_values({})
    assert params.grace == timedelta(hours=24)
    assert params.rollover_min_remaining == timedelta(hours=24)
    assert params.paid_at_max_lag == timedelta(hours=168)
    assert sorted(p.ANCHORING_KINDS) == ["admin", "paid"]


def test_corrupted_coverage_is_bounded() -> None:
    """A huge coverage with an ancient anchor cannot spin the timers forever."""
    state = state_at(m(1, 1, year=1500), coverage_end=m(12, 31, year=2099), at=m(1, 1, year=1500))
    out = p.advance(state, until=m(1, 1, year=2200), params=PARAMS)
    assert out.truncated
