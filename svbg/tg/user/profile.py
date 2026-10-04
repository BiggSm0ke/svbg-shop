"""«👤 Профиль»: who the user is and their subscription, with the buttons to manage it. It is also the
subscription section: the old «📱 Подписка» codes (``sub``, ``sub_none``) open it.

* ``profile`` — name and @username, Telegram ID, «С нами с» (registration date), the balance and the
  subscription block (plan, status, time left, valid until, devices used / limit, traffic used / limit, the
  plan's servers) or «Подписки пока нет» with the trial line when the trial is offered. Buttons are content
  (owner-editable): «🔗 Подключиться», «🔄 Продлить» + «📦 Сменить тариф» or «🛒 Купить подписку»,
  «📱 Устройства», «💳 Пополнить», «🎁 Попробовать бесплатно», «🎟 Промокоды» (the promo module is on),
  «🤝 Пригласить друзей» (the referral program is on), «◀️ Назад». One SQL (the status with the device
  cache), servers from the catalog snapshot, no HTTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from svbg.tg.user import seeds
from svbg.tg.user.base import Base
from svbg.tg.user.render import screen_view
from svbg.tg.user.subscription import SubButton, apply_sub_button, servers_line, sub_button, sub_values
from svbg.tg.user.texts import fmt_date, t

if TYPE_CHECKING:
    from datetime import datetime

    from svbg.tg.ui.router import ScreenCtx, ScreenRouter
    from svbg.tg.ui.view import View
    from svbg.tg.user.status import UserStatus

__all__ = ["ProfileScreens"]

_NONE = "—"
_SUB_KEYS = ("plan", "status", "left", "until", "devices", "traffic", "servers")


class ProfileScreens(Base):
    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.PROFILE)(self.profile)

    async def profile(self, ctx: ScreenCtx, _arg: Any) -> View:
        status = await self.enrich(ctx, with_devices=True)
        trial = t(ctx.lang, "sub_trial_line", days=self.trial_days) if "trial" in ctx.user.flags else ""
        view = screen_view(ctx, seeds.PROFILE, self.values(status, ctx.user.telegram_id, trial=trial))
        # «{left}» works in the labels here too (no colour: the colour belongs to home)
        left = sub_button(status, ctx.lang).left
        view.keyboard = apply_sub_button(view.keyboard or [], SubButton(left, None))
        return view

    def values(
        self,
        status: UserStatus | None,
        telegram_id: int | None = None,
        *,
        trial: str = "",
        at: datetime | None = None,
    ) -> dict[str, str]:
        """The placeholders; ``trial`` — the trial line shown without a subscription («» when not offered)."""
        tg_id = telegram_id if telegram_id is not None else (status.telegram_id if status else None)
        first = (status.first_name or "").strip() if status is not None else ""
        username = (status.username or "").strip().lstrip("@") if status is not None else ""
        handle = f"@{username}" if username else ""
        name = f"{first} ({handle})" if first and handle else first or handle or t(None, "profile_no_name")
        values = {
            "name": name,
            "username": handle or _NONE,
            "id": str(tg_id) if tg_id is not None else _NONE,
            "since": fmt_date(status.created_at if status is not None else None, self.tz),
            "status": _NONE,
            "plan": _NONE,
            "left": _NONE,
            "until": _NONE,
            "devices": _NONE,
            "traffic": _NONE,
            "servers": "",
        }
        if status is None or status.sub is None:
            values["sub"] = t(None, "profile_no_sub") + trial
            return values
        values.update(sub_values(status, self.tz, at))
        values["servers"] = servers_line(self.deps.catalog, status.sub)
        values["sub"] = t(None, "profile_sub", **{k: values[k] for k in _SUB_KEYS})
        return values
