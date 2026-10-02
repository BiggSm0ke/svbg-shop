"""Telegram glue of the deep links: build the service from the app, access probe, resume after onboarding.

Wiring (``svbg/app.py``, see the integration notes in the module docstring of :mod:`svbg.deeplinks`)::

    self.deeplinks = from_app(self.deps, promo=..., ads=..., referral=...)
    # /start: the deep-link service wraps the user path's hook (channel gate)
    build_start_router(..., on_start=self.deeplinks.start_hook(self._on_start))
    # after the channel check / language / consent succeeded:
    redirect = await resume_redirect(self.deeplinks, ctx)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from aiogram.methods import SendMessage

from svbg.deeplinks.ports import AdsPort, PromoPort, referral_port
from svbg.deeplinks.service import CanOpen, DeeplinkService
from svbg.tg.ui.view import Redirect

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import ScreenCtx, ScreenRouter

__all__ = ["NOT_LINKABLE", "from_app", "resume_redirect", "router_can_open"]

log = logging.getLogger("svbg.deeplinks")

#: Screens a link never opens directly: transient steps of a flow and service screens.
NOT_LINKABLE: Final = frozenset(
    {
        "error",
        "menu_fallback",
        "co",
        "pay_wait",
        "pay_short",
        "pay_invoice",
        "pay_details",
        "trial_start",
        "trial_done",
        "reissue_done",
        "reissue_wait",
        "dev_reset",
    }
)
_NOT_LINKABLE_PREFIXES: Final = ("notify_",)


def router_can_open(router: ScreenRouter) -> CanOpen:
    """``(user, code) → bool``: the screen exists and the router would let this user open it.

    Same rule as the router's own check (code routes, then enabled content screens with their policy), so a
    link to an admin screen sent to a user lands on the menu instead of a silent «Нет прав».
    """
    from svbg.tg.ui.router import PUBLIC

    def check(user: UserCtx, code: str) -> bool:
        if code in NOT_LINKABLE or code.startswith(_NOT_LINKABLE_PREFIXES):
            return False
        routes: dict[str, Any] = getattr(router, "_screens", {})
        route = routes.get(code)
        if route is not None:
            return bool(route.access.allows(user))
        content = router.content
        entry = None if content is None else content.get_screen(code)
        if entry is None or not entry.screen.enabled or entry.code != code:
            return False
        policies: dict[str, Any] = getattr(router, "_policies", {})
        return bool(policies.get(code, PUBLIC).allows(user))

    return check


def from_app(
    deps: Any,
    *,
    promo: PromoPort | None = None,
    ads: AdsPort | None = None,
    referral: Any = None,
) -> DeeplinkService:
    """The service over ``AppDeps`` (``screens``, ``settings``, ``catalog``, ``hub``, ``db``).

    ``referral`` may be the referral service or its ``attach_referrer(user_id, code)`` function.
    """
    screens: ScreenRouter = deps.screens
    settings = getattr(deps, "settings", None)

    def config() -> Any:
        return settings.current() if settings is not None else {}

    async def notify(chat_id: int, text: str) -> None:
        await screens.transport.call(SendMessage(chat_id=chat_id, text=text), chat_id=chat_id)

    return DeeplinkService(
        deps.db,
        ui_state=screens.ui_state,
        config=config,
        catalog=getattr(deps, "catalog", None),
        promo=promo,
        ads=ads,
        referral=referral_port(referral),
        hub=getattr(deps, "hub", None),
        notify=notify,
        can_open=router_can_open(screens),
    )


async def resume_redirect(service: DeeplinkService | None, ctx: ScreenCtx) -> Redirect | None:
    """The kept intent as a router result (``None`` when there is none): call it after onboarding."""
    if service is None:
        return None
    landing = await service.resume(ctx.user)
    if landing is None:
        return None
    return Redirect(landing.screen, landing.arg, toast=landing.notice)
