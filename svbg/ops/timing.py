"""Daily "at HH:MM in the owner's time zone" decisions for minute ticks (hot-reloadable schedule).

The scheduler's ``daily()`` fixes the time at registration; ops reads ``REPORT_DAILY_AT`` / ``BACKUP_AT`` /
``TIMEZONE`` on every tick instead, so a change in the bot applies at the next minute without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from svbg.ops.settings import parse_hhmm

__all__ = ["Due", "day_window", "due_today", "today_window", "zone_of"]


def zone_of(name: Any) -> tzinfo:
    """``ZoneInfo(name)``; UTC for an unknown or empty name (the settings validator rejects those)."""
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


@dataclass(frozen=True, slots=True)
class Due:
    day: str  # local date (ISO) the run belongs to
    run: bool  # False: the moment was missed by more than the catch-up window → only mark the day


def due_today(
    now: datetime, at: Any, zone: tzinfo, last_day: str | None, *, catch_up: timedelta = timedelta(hours=3)
) -> Due | None:
    """``None`` while today's moment has not come or today's run is done; else what to do now."""
    try:
        hour, minute = parse_hhmm(at)
    except ValueError:
        return None
    local = now.astimezone(zone)
    due = datetime(local.year, local.month, local.day, hour, minute, tzinfo=zone)
    if local < due:
        return None
    day = local.date().isoformat()
    if last_day == day:
        return None
    return Due(day, now - due.astimezone(UTC) <= catch_up)


def _midnight(day: date, zone: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)


def day_window(now: datetime, zone: tzinfo) -> tuple[datetime, datetime, date]:
    """Yesterday (local) as ``[start, end)`` in UTC, plus the local date."""
    today = now.astimezone(zone).date()
    yesterday = today - timedelta(days=1)
    return _midnight(yesterday, zone), _midnight(today, zone), yesterday


def today_window(now: datetime, zone: tzinfo) -> tuple[datetime, datetime, date]:
    """Today (local) from midnight until ``now``."""
    today = now.astimezone(zone).date()
    return _midnight(today, zone), now, today
