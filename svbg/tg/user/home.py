"""Home card, language, the free trial in one button, the required-channel gate and the ``/start`` hook.

* ``home`` — the status card (subscription, until, days left, balance) with ≤ 7 content buttons whose
  visibility follows the status (``sub``, ``flag:trial``…) plus «💬 Поддержка» (a plain ``SUPPORT_URL`` link).
  One SQL (the status), no HTTP.
* ``sys:trial`` — «🎁 Попробовать бесплатно»: checks (one SQL, plus a cached channel check), then the trial,
  the panel job and the «ready» job in **one** transaction; the same message later turns into «✅ Готово +
  🔗 Подключиться» by itself (job ``user.ui_ready`` runs after the panel job: same ``ordering_key``).
* ``chan`` — «Подпишитесь на канал» with «✅ Я подписался» (a fresh ``getChatMember``); after a successful
  check the trial (or the menu) continues.
* ``lang`` — the language list; the choice is stored and the cached context dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.billing.ports import UiRef
from svbg.core.clock import now
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View
from svbg.tg.user import seeds
from svbg.tg.user.base import Base
from svbg.tg.user.deeplink import DeepLink
from svbg.tg.user.deps import cfg_str
from svbg.tg.user.directory import SUPPORTED_LANGS
from svbg.tg.user.jobs import enqueue_ui_ready
from svbg.tg.user.render import screen_view
from svbg.tg.user.texts import t

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["CHANNEL_GATE_ALL", "HomeScreens"]

log = logging.getLogger("svbg.tg.user.home")

#: ``CHANNEL_REQUIRED_FOR``: ``trial`` (default — only the trial needs the channel, 06 M2) or ``all``.
CHANNEL_GATE_ALL: Final = "all"
_LANG_LABELS: Final = {"ru": "btn_lang_ru", "en": "btn_lang_en"}
_SYS_ALIASES: Final = {
    "buy": seeds.BUY,
    "renew": seeds.BUY,
    "topup": seeds.BALANCE,
    "connect": seeds.CONNECT,
    "devices": seeds.DEVICES,
    "lang": seeds.LANG,
}


#: Called after an onboarding step is done (channel joined, language picked): the consent page or the kept
#: deep-link intent (decision C9 of the integration). ``None`` → the step's own screen.
AfterOnboarding = Callable[["ScreenCtx"], Awaitable["HandlerResult | None"]]


class HomeScreens(Base):
    #: Set by the app (pages + deep links); ``None`` in the plain user path.
    after_onboarding: AfterOnboarding | None = None

    async def _resume(self, ctx: ScreenCtx) -> HandlerResult | None:
        hook = self.after_onboarding
        if hook is None or ctx.user.at_least("support"):
            return None
        try:
            return await hook(ctx)
        except Exception:  # resuming the intent is a convenience; the step itself is done
            log.warning("onboarding resume failed for user %s", ctx.user.user_id, exc_info=True)
            return None

    # ------------------------------------------------------------------ registration

    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.HOME)(self.home)
        router.screen(seeds.LANG)(self.lang_screen)
        router.screen(seeds.CHANNEL)(self.channel_screen)
        router.action(seeds.LANG, "set")(self.set_lang)
        router.action(seeds.CHANNEL, "check")(self.channel_check)
        router.action("sys", "trial")(self.trial)
        for name, target in _SYS_ALIASES.items():
            router.action("sys", name)(self._alias(target))

    @staticmethod
    def _alias(target: str) -> Any:
        async def go(_ctx: ScreenCtx, _arg: Any) -> HandlerResult:
            return Redirect(target)

        go.__name__ = f"sys_{target}"
        return go

    # ------------------------------------------------------------------ home

    async def home(self, ctx: ScreenCtx, _arg: Any) -> View:
        status = await self.enrich(ctx)
        lang = ctx.lang
        return screen_view(ctx, seeds.HOME, self.status_values(status, lang), bottom=self.support_row(lang))

    # ------------------------------------------------------------------ language

    async def lang_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        rows = [
            [
                nav_button(
                    ("• " if code == ctx.lang else "") + t(ctx.lang, _LANG_LABELS[code]),
                    seeds.LANG,
                    "set",
                    code,
                )
            ]
            for code in sorted(SUPPORTED_LANGS, key=lambda c: (c != "ru", c))
            if code in _LANG_LABELS
        ]
        return screen_view(ctx, seeds.LANG, top=rows)

    async def set_lang(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if not isinstance(arg, str) or arg not in SUPPORTED_LANGS:
            return Redirect(seeds.LANG)
        await self.deps.users.set_language(ctx.user.user_id, ctx.user.telegram_id, arg)
        ctx.user = replace(ctx.user, lang=arg, _placeholders=None)
        if (resumed := await self._resume(ctx)) is not None:
            return resumed
        view = await self.home(ctx, None)
        view.toast = t(arg, "lang_set")
        return view

    # ------------------------------------------------------------------ channel gate

    def channel_url(self) -> str | None:
        url = cfg_str(self.deps.config, "REQUIRED_CHANNEL_URL")
        return url if url and url.startswith(("https://", "tg://")) else None

    def gate_for_all(self) -> bool:
        channel = self.deps.channel
        return (
            channel is not None
            and channel.required_chat() is not None
            and cfg_str(self.deps.config, "CHANNEL_REQUIRED_FOR", "trial") == CHANNEL_GATE_ALL
        )

    async def channel_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        lang = ctx.lang
        rows: list[list[InlineKeyboardButton]] = []
        url = self.channel_url()
        if url:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_join"), url=url, style="primary")])
        then = arg if arg == "trial" else None
        rows.append([nav_button(t(lang, "btn_joined"), seeds.CHANNEL, "check", then, style="success")])
        bottom = [] if self.gate_for_all() and then is None else [self.menu(lang)]
        return screen_view(ctx, seeds.CHANNEL, top=rows, bottom=bottom)

    async def channel_check(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        channel = self.deps.channel
        tg_id = ctx.user.telegram_id
        if channel is None or tg_id is None:
            return Redirect(seeds.HOME)
        member = await channel.is_member(tg_id, fresh=True)
        if member is None:
            return Toast(t(ctx.lang, "channel_check_failed"), alert=True)
        if not member:
            return Toast(t(ctx.lang, "not_member_yet"), alert=True)
        if arg == "trial":
            return await self.trial(ctx, None)
        if (resumed := await self._resume(ctx)) is not None:
            return resumed
        return Redirect(seeds.HOME)

    async def on_start(self, user: UserCtx, chat_id: int, link: DeepLink | None) -> tuple[str, Any] | None:
        """``/start``: keep the deep link as the pending intent (stage 3b); the channel gate when required."""
        del chat_id
        if link is not None:
            try:
                await self.deps.screens.ui_state.set_pending_intent(user.user_id, link.as_intent())
            except Exception:  # noqa: BLE001 - the intent is a convenience; /start must work without it
                log.warning("could not store the deep link intent of user %s", user.user_id)
        if not self.gate_for_all() or user.telegram_id is None or user.at_least("support"):
            return None
        channel = self.deps.channel
        assert channel is not None
        member = await channel.is_member(user.telegram_id)
        return (seeds.CHANNEL, None) if member is False else None

    # ------------------------------------------------------------------ trial

    async def trial(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        from svbg.subscriptions.trial import TrialRefused, trial_text

        service = self.deps.trial
        lang = ctx.lang
        if service is None:
            return Toast(t(lang, "trial_unavailable"), alert=True)
        pre = await service.check(ctx.user.user_id)
        if not pre.ok:
            if pre.reason == "not_member":
                return Redirect(seeds.CHANNEL, "trial")
            return Toast(pre.localized(lang) or t(lang, "trial_unavailable"), alert=True)
        ref = UiRef(ctx.chat_id, ctx.message_id, now()) if ctx.message_id is not None else None
        try:
            async with self.deps.db.tx() as conn:
                result = await service.grant(
                    conn, user_id=ctx.user.user_id, days=pre.days, caused_by=f"user:{ctx.user.user_id}"
                )
                if result.subscription_id is not None:
                    await enqueue_ui_ready(
                        conn,
                        "trial",
                        subscription_id=result.subscription_id,
                        user_id=ctx.user.user_id,
                        ui_ref=ref,
                    )
        except TrialRefused as refused:
            return Toast(trial_text(refused.reason, lang) or t(lang, "trial_unavailable"), alert=True)
        return screen_view(ctx, seeds.TRIAL_STARTED, {"days": str(result.days or pre.days)})
