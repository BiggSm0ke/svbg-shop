"""«📱 Подписка»: the section that takes the place of «Купить подписку» on home (Bedolaga's «Моя подписка»).

* ``sub`` — status, plan, time left, valid until, traffic used / limit, devices used / limit and the plan's
  servers, with «Подключиться», «Продлить», «Сменить тариф» and «Устройства» (content buttons). Without a
  subscription the same route shows ``sub_none``: a short text, «Купить подписку» and the trial when it is
  offered. One SQL (the status with the device cache), servers from the catalog snapshot, no HTTP.
* :func:`sub_button` — the home button: ``{left}`` is the time left («12 дн.», «2 дн. 5 ч», «5 ч»,
  «закончилась») and the colour follows the status: a trial is always red; a paid subscription is green, blue
  below ``SUB_BUTTON_BLUE_DAYS`` and red below ``SUB_BUTTON_RED_DAYS``; expired or on hold — red; without a
  subscription the button keeps its own look and loses the suffix. Pure: home already has the status.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.core.clock import now
from svbg.tg.ui import codec
from svbg.tg.ui.renderer import SYSTEM_SCREEN
from svbg.tg.user import seeds
from svbg.tg.user.base import Base
from svbg.tg.user.deps import cfg_int
from svbg.tg.user.render import screen_view
from svbg.tg.user.texts import fmt_bytes, fmt_date, fmt_datetime, fmt_left_short, t

if TYPE_CHECKING:
    from svbg.tg.ui.router import ScreenCtx, ScreenRouter
    from svbg.tg.ui.view import View
    from svbg.tg.user.status import SubInfo, UserStatus

__all__ = [
    "BLUE_DAYS_DEFAULT",
    "RED_DAYS_DEFAULT",
    "SubButton",
    "SubscriptionScreens",
    "apply_sub_button",
    "sub_button",
    "sub_thresholds",
]

BLUE_DAYS_DEFAULT: Final = 10
RED_DAYS_DEFAULT: Final = 3
_SERVERS_MAX: Final = 4
_LEFT: Final = "{" + seeds.SUB_LEFT + "}"
#: ``{left}`` with the separator in front of it («Подписка · {left}» → «Подписка» without a subscription).
_LEFT_RE: Final = re.compile(r"\s*[·•|:,—–-]?\s*" + re.escape(_LEFT))
#: Callbacks that open the section (the seeded screen button and ``system:sub``).
_TARGETS: Final = frozenset({codec.encode(seeds.SUB), codec.encode(SYSTEM_SCREEN, "sub")})


@dataclass(frozen=True, slots=True)
class SubButton:
    left: str  # "" → no suffix
    style: str | None  # None → the button's own colour


def shown_days(seconds: float) -> float:
    """Days as the button shows them: whole days (rounded up) from 3 days up, the exact value below."""
    s = max(0.0, seconds)
    return float(math.ceil(s / 86_400)) if s >= 3 * 86_400 else s / 86_400


def sub_button(
    status: UserStatus | None,
    lang: str,
    *,
    blue_days: int = BLUE_DAYS_DEFAULT,
    red_days: int = RED_DAYS_DEFAULT,
    at: datetime | None = None,
) -> SubButton:
    at = at or now()
    sub = status.sub if status is not None else None
    if status is None or sub is None:
        return SubButton("", None)
    state = status.sub_state(at)
    if state == "frozen":
        return SubButton(t(lang, "sub_btn_paused"), "danger")
    if state == "expired":
        return SubButton(t(lang, "sub_btn_expired"), "danger")
    seconds = sub.seconds_left(at)
    left = fmt_left_short(seconds, lang)
    if state == "trial":
        return SubButton(left, "danger")
    days = shown_days(seconds)
    if days < red_days:
        return SubButton(left, "danger")
    if days < blue_days:
        return SubButton(left, "primary")
    return SubButton(left, "success")


def label_with_left(label: str, left: str) -> str:
    if _LEFT not in label:
        return label
    if left:
        return label.replace(_LEFT, left)
    return _LEFT_RE.sub("", label).strip() or label.replace(_LEFT, "").strip()


def apply_sub_button(
    rows: Sequence[Sequence[InlineKeyboardButton]], button: SubButton
) -> list[list[InlineKeyboardButton]]:
    """``{left}`` in every label, the colour on the buttons that open «Подписка»."""
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        built: list[InlineKeyboardButton] = []
        for b in row:
            update: dict[str, Any] = {}
            if _LEFT in b.text:
                update["text"] = label_with_left(b.text, button.left)
            if button.style is not None and b.callback_data in _TARGETS:
                update["style"] = button.style
            built.append(b.model_copy(update=update) if update else b)
        out.append(built)
    return out


def sub_thresholds(config: Any) -> tuple[int, int]:
    """``(blue, red)`` days of the home button: ``SUB_BUTTON_BLUE_DAYS`` / ``SUB_BUTTON_RED_DAYS`` (hot)."""
    return (
        cfg_int(config, "SUB_BUTTON_BLUE_DAYS", BLUE_DAYS_DEFAULT),
        cfg_int(config, "SUB_BUTTON_RED_DAYS", RED_DAYS_DEFAULT),
    )


class SubscriptionScreens(Base):
    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.SUB)(self.section)
        router.screen(seeds.SUB_NONE)(self.section)

    async def section(self, ctx: ScreenCtx, _arg: Any) -> View:
        status = await self.enrich(ctx, with_devices=True)
        lang = ctx.lang
        if status is None or status.sub is None:
            trial = t(lang, "sub_trial_line", days=self.trial_days) if "trial" in ctx.user.flags else ""
            return screen_view(ctx, seeds.SUB_NONE, {"trial": trial})
        return screen_view(ctx, seeds.SUB, self.values(status, lang))

    # ------------------------------------------------------------------ values

    def values(self, status: UserStatus, lang: str, at: datetime | None = None) -> dict[str, str]:
        at = at or now()
        sub = status.sub
        assert sub is not None
        state = status.sub_state(at)
        seconds = sub.seconds_left(at)
        until = fmt_datetime(sub.paid_until, self.tz) if sub.is_trial else fmt_date(sub.paid_until, self.tz)
        return {
            "status": t(lang, _state_key(state, sub)),
            "plan": sub.plan_title(lang) or "—",
            "left": fmt_left_short(seconds, lang) if seconds > 0 else "—",
            "until": until,
            "traffic": _traffic(sub, lang),
            "devices": _devices(sub, status, lang),
            "servers": self._servers(sub, lang),
        }

    def _servers(self, sub: SubInfo, lang: str) -> str:
        catalog = self.deps.catalog
        squads = sub.plan_snapshot.get("squads")
        if catalog is None or not isinstance(squads, list) or not squads:
            return ""
        try:
            snap = catalog.snapshot
            names = [
                loc.label(lang) for s in squads if (loc := snap.location(str(s))) is not None and loc.present
            ]
        except Exception:  # noqa: BLE001 - the servers line is extra, never worth an error screen
            return ""
        if not names:
            return ""
        shown = ", ".join(names[:_SERVERS_MAX])
        if len(names) > _SERVERS_MAX:
            shown = t(lang, "sub_servers_more", list=shown, n=len(names) - _SERVERS_MAX)
        return t(lang, "sub_servers", list=shown)


def _state_key(state: str, sub: SubInfo) -> str:
    if state == "frozen":
        return "sub_state_frozen"
    if sub.link_state == "panel_missing":
        return "sub_state_missing"
    if state == "expired":
        return "sub_state_expired"
    if sub.link_state == "pending":
        return "sub_state_pending"
    return "sub_state_trial" if state == "trial" else "sub_state_active"


def _traffic(sub: SubInfo, lang: str) -> str:
    used = fmt_bytes(sub.used_traffic, lang)
    if not sub.traffic_bytes:
        return t(lang, "sub_no_limit", used=used)
    return t(lang, "sub_used_of", used=used, limit=fmt_bytes(sub.traffic_bytes, lang))


def _devices(sub: SubInfo, status: UserStatus, lang: str) -> str:
    limit = sub.device_limit
    cache = status.devices
    used = str(len(cache.devices)) if cache is not None else None
    if limit is None:
        return used or "—"
    if limit == 0:
        return t(lang, "sub_no_limit", used=used) if used is not None else t(lang, "devices_unlimited")
    if used is None:
        return t(lang, "sub_devices_upto", limit=limit)
    return t(lang, "sub_used_of", used=used, limit=limit)
