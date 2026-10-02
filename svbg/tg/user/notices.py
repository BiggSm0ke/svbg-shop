"""Telegram side of :mod:`svbg.services.notify_user`: render ``notify_<kind>`` and send it.

Texts are content screens (``notify_expiring``, ``notify_trial_ending``…, defaults in
:mod:`svbg.tg.user.seeds`); the buttons carry the next step: «🔄 Продлить» for expiry and traffic,
«🗑 Это не я — удалить» for a new device, «🔗 Подключиться» for a renewed link. A notification is a new
message (the user's menu message stays where it is).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from aiogram.types import InlineKeyboardButton

from svbg.core.clock import now
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import nav_button
from svbg.tg.user import seeds
from svbg.tg.user.messenger import link_button, menu_row
from svbg.tg.user.render import plain_view
from svbg.tg.user.texts import fmt_date, fmt_datetime, fmt_left, t

if TYPE_CHECKING:
    from svbg.content.store import ContentStore
    from svbg.services.notify_user import Notification
    from svbg.tg.ui.view import View
    from svbg.tg.user.messenger import UserMessenger

__all__ = ["TelegramNotificationSender", "gb"]

_RENEW_KINDS: Final = ("expiring", "expired", "traffic", "limited")


def gb(value: int | None) -> str:
    if not value:
        return "0 GB"
    return f"{value / 1024**3:.1f} GB".replace(".0 GB", " GB")


class TelegramNotificationSender:
    """:class:`svbg.services.notify_user.NotificationSender` over :class:`UserMessenger`."""

    def __init__(
        self,
        messenger: UserMessenger,
        *,
        content: ContentStore | None = None,
        tz: Callable[[], str] = lambda: "Europe/Moscow",
        bot_username: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._messenger = messenger
        self._content = content
        self._tz = tz
        self._bot_username = bot_username

    def view(self, n: Notification) -> View:
        lang = n.lang if n.lang in ("ru", "en") else self._messenger.lang_of(n.telegram_id)
        tz = self._tz()
        until = fmt_datetime(n.paid_until, tz) if n.is_trial else fmt_date(n.paid_until, tz)
        left = fmt_left((n.paid_until - now()).total_seconds(), lang) if n.paid_until is not None else "—"
        name = n.plan_snapshot.get("name")
        plan = ""
        if isinstance(name, dict):
            plan = str(name.get(lang) or name.get("ru") or "")
        percent = n.payload.get("percent")
        values = {
            "until": until,
            "left": left,
            "plan": plan,
            "percent": str(percent) if percent is not None else "",
            "used": gb(n.used_traffic),
            "limit": gb(n.traffic_limit),
            "device": str(n.payload.get("device") or t(lang, "device_unknown")),
        }
        top: list[list[InlineKeyboardButton]] = []
        if n.base in _RENEW_KINDS:
            top.append([nav_button(t(lang, "btn_renew"), seeds.BUY, style="success")])
        elif n.base == "trial_ending":
            top.append([nav_button(t(lang, "btn_buy"), seeds.BUY, style="success")])
        elif n.base == "device_added":
            top.append([nav_button(t(lang, "btn_not_me"), seeds.DEVICES, style="danger")])
        elif n.base == "revoked" and n.subscription_url:
            top.append([link_button(t(lang, "btn_connect"), n.subscription_url)])
        user = UserCtx(n.user_id, telegram_id=n.telegram_id, lang=lang)
        return plain_view(
            user,
            self._content,
            seeds.NOTICE_PREFIX + n.base,
            values,
            top=top,
            bottom=[menu_row(lang)],
            bot_username=self._bot_username(),
        )

    async def send(self, notification: Notification) -> bool:
        ref = await self._messenger.send_view(notification.telegram_id, self.view(notification))
        return ref is not None
