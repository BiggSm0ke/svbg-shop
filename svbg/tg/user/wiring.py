""":class:`UserPath` — the user path as one object the app builds, registers and schedules.

::

    deps = UserPathDeps(db=db, config=settings.current, screens=screens, users=users, content=content,
                        catalog=catalog, payments=payment_core, trial=trial, channel=channel,
                        actions=actions, receipts=receipts, i_paid=poller.check_now,
                        fetch_devices=panel_devices)
    path = UserPath(deps, call=payments_call, notify_call=notifications_call)
    billing = Billing(..., messenger=path.messenger)    # the purchase message is edited by the user path
    path.register()                                     # screens, actions, forms on the ScreenRouter
    dispatcher.include_router(path.aiogram_router())    # Stars + receipts (before the screen router)
    worker_handlers.update(path.handlers())             # user.ui_ready, user.devices_refresh, notify.user
    path.install(bus); path.schedule(scheduler)         # panel events → notifications, fallback scanner
    build_start_router(..., on_start=path.on_start)     # deep-link stub, entry captcha, channel gate

``billing`` may be ``None`` while :class:`UserPathDeps` is built (it needs the messenger first): set
``path.deps.billing`` afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram import Router

from svbg.services.notify_user import NotifyUser
from svbg.tg.user.account import AccountScreens
from svbg.tg.user.captcha import CaptchaScreens
from svbg.tg.user.chat_payments import ChatPayments
from svbg.tg.user.deps import UserPathDeps, cfg_int, cfg_str
from svbg.tg.user.home import HomeScreens
from svbg.tg.user.jobs import DevicesWatch, UserJobs
from svbg.tg.user.messenger import SERIALIZE_WAIT_S, Caller, Serializer, UserMessenger
from svbg.tg.user.notices import TelegramNotificationSender
from svbg.tg.user.shop import ShopScreens
from svbg.tg.user.status import StatusReader
from svbg.tg.user.subscription import SubscriptionScreens
from svbg.tg.user.tables import user_devices

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from aiogram.methods import TelegramMethod

    from svbg.core.bus import Event, EventBus
    from svbg.jobs.scheduler import Scheduler
    from svbg.jobs.worker import Handler
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenRouter
    from svbg.tg.user.deeplink import DeepLink

__all__ = ["SCAN_INTERVAL_S", "UserPath"]

log = logging.getLogger("svbg.tg.user")

SCAN_INTERVAL_S: Final = 600


def screen_lock(screens: ScreenRouter) -> Serializer | None:
    """The screen router's per-user lock for background edits (see :class:`UserMessenger`), bounded by
    :data:`SERIALIZE_WAIT_S`. ``None`` when the router does not expose it (edits then simply do not wait)."""
    hold = getattr(getattr(screens, "_locks", None), "hold", None)
    if not callable(hold):
        return None

    def serialize(telegram_id: int) -> AbstractAsyncContextManager[bool]:
        return hold(telegram_id, wait=SERIALIZE_WAIT_S)

    return serialize


class UserPath:
    def __init__(
        self, deps: UserPathDeps, *, call: Caller | None = None, notify_call: Caller | None = None
    ) -> None:
        self.deps = deps
        screens = deps.screens

        async def direct(method: TelegramMethod[Any], chat_id: int) -> Any:
            return await screens.transport.call(method, chat_id=chat_id)

        def bot_username() -> str | None:
            return screens.transport.bot_username

        self.status = StatusReader(deps.db, trial_days=lambda: max(0, cfg_int(deps.config, "TRIAL_DAYS", 0)))
        self.messenger = UserMessenger(
            call or direct,
            content=deps.content,
            users=deps.users,
            config=deps.config,
            bot_username=bot_username,
            serialize=screen_lock(screens),
        )
        self.notice_messenger = UserMessenger(
            notify_call or call or direct,
            content=deps.content,
            users=deps.users,
            config=deps.config,
            bot_username=bot_username,
        )
        self.home = HomeScreens(deps, self.status)
        self.captcha = CaptchaScreens(deps)
        self.home.captcha = self.captcha
        self.captcha.after = self.home.after_captcha
        self.shop = ShopScreens(deps, self.status)
        self.account = AccountScreens(deps, self.status)
        self.subscription = SubscriptionScreens(deps, self.status)
        self.devices_watch = DevicesWatch(deps.users.activity)
        self.account.watch = self.devices_watch
        self.jobs = UserJobs(
            deps.db,
            messenger=self.messenger,
            content=deps.content,
            tz=self._tz,
            fetch_devices=deps.fetch_devices,
            show=self._show,
            watch=self.devices_watch,
        )
        self.notifications = NotifyUser(
            deps.db,
            config=deps.config,
            sender=TelegramNotificationSender(
                self.notice_messenger, content=deps.content, tz=self._tz, bot_username=bot_username
            ),
        )
        self.chat_payments = ChatPayments(
            deps.db,
            payments_core=deps.payments,
            users=deps.users,
            receipts=deps.receipts,
            hub=screens.hub,
        )
        self._registered = False

    def _tz(self) -> str:
        return cfg_str(self.deps.config, "TIMEZONE", "Europe/Moscow") or "Europe/Moscow"

    async def _show(self, user: UserCtx, chat_id: int, screen: str, arg: Any) -> Any:
        return await self.deps.screens.show(user, chat_id, screen, arg)

    # ------------------------------------------------------------------------------------------ wiring

    def register(self) -> None:
        """Screens, actions and forms on the screen router (once); the entry captcha becomes its gate."""
        if self._registered:
            return
        router = self.deps.screens
        self.home.register(router)
        self.captcha.register(router)
        self.shop.register(router)
        self.account.register(router)
        self.subscription.register(router)
        self._registered = True

    def aiogram_router(self) -> Router:
        """Stars (pre-checkout, successful payment) and manual-transfer receipts."""
        return self.chat_payments.router()

    async def on_start(self, user: UserCtx, chat_id: int, link: DeepLink | None) -> tuple[str, Any] | None:
        return await self.home.on_start(user, chat_id, link)

    def handlers(self) -> dict[str, Handler]:
        return {**self.jobs.handlers(), **self.notifications.handlers()}

    def install(self, bus: EventBus) -> Callable[[], None]:
        off_notify = self.notifications.install(bus)
        off_devices = bus.subscribe("remnawave.user_hwid_devices.*", self._devices_changed)

        def uninstall() -> None:
            off_notify()
            off_devices()

        return uninstall

    def schedule(self, scheduler: Scheduler) -> None:
        scheduler.every("notify.user.scan", SCAN_INTERVAL_S, self.notifications.scan, jitter_s=60)

        async def purge() -> None:
            await self.notifications.purge()

        scheduler.every("notify.user.purge", 86_400, purge, jitter_s=600)

    async def _devices_changed(self, event: Event) -> None:
        """A device was added or removed in the panel: the cached list is stale (next opening refreshes)."""
        sid = event.payload.get("subscription_id")
        if isinstance(sid, bool) or not isinstance(sid, int):
            return
        async with self.deps.db.tx() as conn:
            await conn.execute(sa.delete(user_devices).where(user_devices.c.subscription_id == sid))
