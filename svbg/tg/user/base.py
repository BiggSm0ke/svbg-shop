"""Shared helpers of the user screens: settings, the status read, common buttons, argument parsing."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import CopyTextButton, InlineKeyboardButton

from svbg.core.clock import now
from svbg.tg.ui import codec
from svbg.tg.ui.renderer import SYSTEM_SCREEN, nav_button
from svbg.tg.user import seeds
from svbg.tg.user.deps import UserPathDeps, cfg_int, cfg_str
from svbg.tg.user.status import StatusReader, UserStatus
from svbg.tg.user.texts import fmt_date, fmt_datetime, fmt_left, t

if TYPE_CHECKING:
    from svbg.tg.ui.router import ScreenCtx

__all__ = ["Base", "parse_ids"]

_ID_RE: Final = re.compile(r"^\d{1,18}$")
COPY_MAX: Final = 256  # Bot API CopyTextButton limit


def parse_ids(arg: Any, n: int) -> list[int] | None:
    """``"12:30"`` → ``[12, 30]`` (exactly ``n`` non-negative integers); ``None`` for anything else."""
    if not isinstance(arg, str):
        return None
    parts = arg.split(":")
    if len(parts) != n or not all(_ID_RE.match(p) for p in parts):
        return None
    return [int(p) for p in parts]


class Base:
    def __init__(self, deps: UserPathDeps, status: StatusReader) -> None:
        self.deps = deps
        self.status = status

    # ------------------------------------------------------------------ settings

    @property
    def currency(self) -> str:
        return (cfg_str(self.deps.config, "CURRENCY", "RUB") or "RUB").upper()

    @property
    def tz(self) -> str:
        return cfg_str(self.deps.config, "TIMEZONE", "Europe/Moscow") or "Europe/Moscow"

    @property
    def trial_days(self) -> int:
        return max(0, cfg_int(self.deps.config, "TRIAL_DAYS", 0))

    def support_url(self) -> str | None:
        url = cfg_str(self.deps.config, "SUPPORT_URL")
        return url if url and url.startswith(("https://", "http://", "tg://")) else None

    # ------------------------------------------------------------------ status

    async def enrich(self, ctx: ScreenCtx, *, with_devices: bool = False) -> UserStatus | None:
        """Read the status (1 SQL) and put the enriched ``UserCtx`` into ``ctx`` (visibility conditions)."""
        user, status = await self.status.enriched(ctx.user, with_devices=with_devices)
        ctx.user = user
        return status

    def status_values(self, status: UserStatus | None, lang: str) -> dict[str, str]:
        """``{status}``, ``{plan}``, ``{until}``, ``{devices}`` of the home card."""
        at = now()
        name = (status.first_name if status is not None else None) or t(lang, "friend")
        values = {"name": name, "plan": "—", "until": "—", "devices": "—"}
        if status is None or status.sub is None:
            values["status"] = t(lang, "status_none")
            return values
        sub = status.sub
        until = fmt_datetime(sub.paid_until, self.tz) if sub.is_trial else fmt_date(sub.paid_until, self.tz)
        values["plan"] = sub.plan_title(lang) or "—"
        values["until"] = until
        values["devices"] = _devices_text(sub.device_limit, lang)
        left = fmt_left(sub.seconds_left(at), lang)
        state = status.sub_state(at)
        if state == "frozen":
            values["status"] = t(lang, "status_frozen")
        elif sub.link_state == "panel_missing":
            values["status"] = t(lang, "status_missing")
        elif state == "expired":
            values["status"] = t(lang, "status_expired", until=until)
        elif sub.link_state == "pending":
            values["status"] = t(lang, "status_pending")
        elif state == "trial":
            values["status"] = t(lang, "status_trial", until=until, left=left)
        else:
            values["status"] = t(lang, "status_active", plan=values["plan"], until=until, left=left)
        return values

    # ------------------------------------------------------------------ buttons

    @staticmethod
    def menu(lang: str) -> list[InlineKeyboardButton]:
        return [nav_button(t(lang, "btn_menu"), seeds.HOME)]

    @staticmethod
    def back(lang: str, screen: str, arg: str | None = None) -> list[InlineKeyboardButton]:
        return [nav_button(t(lang, "btn_back"), screen, codec.ACTION_OPEN, arg)]

    def support_row(self, lang: str) -> list[list[InlineKeyboardButton]]:
        """«💬 Поддержка»: the ``SUPPORT_URL`` link (``SUPPORT_MODE=link``) or ``system:support`` — the
        support screen with the ticket dialog (``tickets`` / ``both``, :mod:`svbg.support`)."""
        if cfg_str(self.deps.config, "SUPPORT_MODE", "link") in ("tickets", "both"):
            return [[nav_button(t(lang, "btn_support"), SYSTEM_SCREEN, "support")]]
        url = self.support_url()
        return [[InlineKeyboardButton(text=t(lang, "btn_support"), url=url)]] if url else []

    @staticmethod
    def copy_button(lang: str, url: str) -> InlineKeyboardButton | None:
        if len(url) > COPY_MAX:
            return None
        return InlineKeyboardButton(text=t(lang, "btn_copy"), copy_text=CopyTextButton(text=url))


def _devices_text(limit: int | None, lang: str) -> str:
    if limit is None:
        return "—"
    if limit == 0:
        return t(lang, "devices_unlimited")
    return str(limit)
