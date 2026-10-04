"""Entry captcha: «Нажмите на 🍓» with a few emojis as buttons, before a new user gets anywhere in the bot.

* **Who.** Users who have not passed it (``users.captcha_passed_at IS NULL``: the migration marked everybody
  who was already there, the importers mark the users they bring) while ``CAPTCHA_ENABLED`` is on. Staff and
  owners never see it.
* **Where.** ``/start`` and ``/menu`` (:meth:`HomeScreens.on_start` keeps the deep link as the pending intent
  first) and every callback of the screen router (:meth:`CaptchaScreens.gate`): an old broadcast button
  opens the captcha too. Payment webhooks and background jobs do not go through the router and are not
  affected.
* **The challenge.** The target and the shuffled ``CAPTCHA_EMOJIS`` are kept here, in memory, per user, under
  a fresh nonce; a button carries only ``<nonce>.<position>`` and a tap is checked against the stored
  challenge. A tap on an older challenge (a double tap, an old message, a restart of the bot) shows the
  current one again and counts for nothing.
* **Wrong tap.** A toast and a new challenge (another target, another order) in the same message. After
  :data:`MAX_MISSES` wrong taps in a row there is a :data:`COOLDOWN_S` pause: taps only get «подождите»,
  then the challenge on the screen counts again.
* **Right tap.** ``captcha_passed_at`` is stored and the flow goes on as after a plain ``/start``: the
  required channel, the kept deep-link intent (the consent page first, in the app), else the menu.
"""

from __future__ import annotations

import inspect
import logging
import math
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import InlineKeyboardButton

from svbg.core.settings.registry import CAPTCHA_EMOJIS_DEFAULT, CAPTCHA_EMOJIS_MAX
from svbg.tg.ui import codec
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View
from svbg.tg.user import seeds
from svbg.tg.user.deps import UserPathDeps, cfg_bool
from svbg.tg.user.render import screen_view
from svbg.tg.user.texts import t

if TYPE_CHECKING:
    from svbg.tg.ui.codec import Decoded
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "COOLDOWN_S",
    "MAX_MISSES",
    "PER_ROW",
    "TAP",
    "AfterCaptcha",
    "CaptchaScreens",
    "Challenge",
    "OnPassed",
    "captcha_emojis",
]

log = logging.getLogger("svbg.tg.user.captcha")

TAP: Final = "tap"
PER_ROW: Final = 3
MAX_MISSES: Final = 5
COOLDOWN_S: Final = 60.0
CACHE_SIZE: Final = 20_000
_OWN_ACTIONS: Final = frozenset({codec.ACTION_OPEN, TAP})
_SHUFFLE_TRIES: Final = 8

#: The next screen after a passed captcha, as after a plain ``/start`` (``HomeScreens.after_captcha``).
AfterCaptcha = Callable[["ScreenCtx"], Awaitable["HandlerResult"]]
#: A hook run once when a user passes the captcha (``CaptchaScreens.on_passed``).
OnPassed = Callable[["UserCtx"], Awaitable[None] | None]


def captcha_emojis(raw: Any) -> tuple[str, ...]:
    """``CAPTCHA_EMOJIS`` ready to use: distinct non-empty items in order; the default when fewer than 2."""
    items = raw.split(",") if isinstance(raw, str) else raw
    out: list[str] = []
    if isinstance(items, (list, tuple)):
        for item in items:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return tuple(out[:CAPTCHA_EMOJIS_MAX]) if len(out) >= 2 else CAPTCHA_EMOJIS_DEFAULT


@dataclass(slots=True)
class Challenge:
    """One «tap the X» question of a user; ``misses`` and ``until`` carry over to the next challenge."""

    nonce: str
    options: tuple[str, ...]  # in the order of the buttons
    answer: int  # position of the target in ``options``
    misses: int = 0  # wrong taps in a row
    until: float = 0.0  # end of the pause after too many misses (the screens' clock)

    @property
    def target(self) -> str:
        return self.options[self.answer]


def _parse_tap(arg: Any) -> tuple[str | None, int | None]:
    """``"<nonce>.<position>"`` → ``(nonce, position)``; ``None`` parts for anything else."""
    if not isinstance(arg, str):
        return None, None
    nonce, dot, pos = arg.partition(".")
    if not dot or not nonce or not (pos.isascii() and pos.isdigit()) or len(pos) > 2:
        return nonce or None, None
    return nonce, int(pos)


class CaptchaScreens:
    def __init__(
        self,
        deps: UserPathDeps,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.deps = deps
        #: Set by :class:`~svbg.tg.user.wiring.UserPath`; ``None`` → the menu after the captcha.
        self.after: AfterCaptcha | None = None
        #: Called once when a user passes (the app: the «new user» post, the referral welcome that waited).
        self.on_passed: list[OnPassed] = []
        self._clock = clock
        self._rng = rng if rng is not None else random.SystemRandom()
        self._challenges: OrderedDict[int, Challenge] = OrderedDict()

    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.CAPTCHA)(self.screen)
        router.action(seeds.CAPTCHA, TAP)(self.tap)
        router.gate = self.gate

    # ------------------------------------------------------------------ settings

    def enabled(self) -> bool:
        return cfg_bool(self.deps.config, "CAPTCHA_ENABLED", True)

    def emojis(self) -> tuple[str, ...]:
        try:
            raw = self.deps.config().get("CAPTCHA_EMOJIS")
        except (RuntimeError, AttributeError):
            raw = None
        return captcha_emojis(raw)

    def required(self, user: UserCtx) -> bool:
        """The user has to pass the captcha before anything else (no SQL: the flag is in the context)."""
        return not user.captcha_passed and not user.at_least("support") and self.enabled()

    # ------------------------------------------------------------------ the gate of every callback

    async def gate(self, ctx: ScreenCtx, decoded: Decoded | None) -> HandlerResult:
        """Any button of a user who has not passed the captcha opens the captcha instead."""
        if not self.required(ctx.user):
            return None
        if decoded is not None and decoded.screen == seeds.CAPTCHA and decoded.action in _OWN_ACTIONS:
            return None
        return Redirect(seeds.CAPTCHA)

    # ------------------------------------------------------------------ screen and taps

    async def screen(self, ctx: ScreenCtx, _arg: Any) -> View | Redirect:
        if not self.required(ctx.user):
            return Redirect(seeds.HOME)
        uid = ctx.user.user_id
        return self._view(ctx, self._challenges.get(uid) or self._renew(uid))

    async def tap(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        user = ctx.user
        if user.captcha_passed:  # a double tap or an old message: the flow has already moved on
            return Toast(t(ctx.lang, "captcha_done"))
        if not self.required(user):  # switched off meanwhile
            return await self._continue(ctx)
        uid = user.user_id
        challenge = self._challenges.get(uid)
        now = self._clock()
        if challenge is not None and challenge.until > now:
            return Toast(t(ctx.lang, "captcha_wait", seconds=max(1, math.ceil(challenge.until - now))))
        nonce, pos = _parse_tap(arg)
        if challenge is None or nonce != challenge.nonce or pos is None or pos >= len(challenge.options):
            return self._view(ctx, challenge or self._renew(uid))  # an older challenge: counts for nothing
        if pos == challenge.answer:
            first = await self.deps.users.mark_captcha_passed(uid, user.telegram_id)
            self._challenges.pop(uid, None)
            ctx.user = replace(user, captcha_passed=True)
            log.info("user %s passed the captcha", uid)
            if first is not False:
                await self._passed(ctx.user)
            return await self._continue(ctx)
        challenge.misses += 1
        pause = challenge.misses >= MAX_MISSES
        if pause:
            challenge.misses, challenge.until = 0, now + COOLDOWN_S
        view = self._view(ctx, self._renew(uid))
        view.toast = t(ctx.lang, "captcha_cooldown" if pause else "captcha_wrong")
        view.toast_alert = pause
        return view

    async def _continue(self, ctx: ScreenCtx) -> HandlerResult:
        after = self.after
        return Redirect(seeds.HOME) if after is None else await after(ctx)

    async def _passed(self, user: UserCtx) -> None:
        """Run :attr:`on_passed` once per user; a failing hook never stops the user."""
        for hook in self.on_passed:
            try:
                res = hook(user)
                if inspect.isawaitable(res):
                    await res
            except Exception:  # isolation: the captcha is passed whatever a hook does
                name = getattr(hook, "__name__", hook)
                log.exception("captcha hook %s failed for user %s", name, user.user_id)

    # ------------------------------------------------------------------ challenges

    def forget(self, user_id: int) -> None:
        """Drop the challenge of a user who was deleted (``svbg.services.user_delete``)."""
        self._challenges.pop(user_id, None)

    def current(self, user_id: int) -> Challenge | None:
        """The challenge the user sees now (``None`` before the first one or after passing)."""
        return self._challenges.get(user_id)

    def _renew(self, user_id: int) -> Challenge:
        """A new challenge: another target, another order and another nonce than the previous one."""
        old = self._challenges.get(user_id)
        options = list(self.emojis())
        others = [e for e in options if old is None or e != old.target]
        target = self._rng.choice(others or options)
        for _ in range(_SHUFFLE_TRIES):
            self._rng.shuffle(options)
            if old is None or tuple(options) != old.options:
                break
        nonce = f"{self._rng.getrandbits(32):08x}"
        while old is not None and nonce == old.nonce:
            nonce = f"{self._rng.getrandbits(32):08x}"
        challenge = Challenge(
            nonce=nonce,
            options=tuple(options),
            answer=options.index(target),
            misses=old.misses if old is not None else 0,
            until=old.until if old is not None else 0.0,
        )
        self._challenges[user_id] = challenge
        self._challenges.move_to_end(user_id)
        while len(self._challenges) > CACHE_SIZE:
            self._challenges.popitem(last=False)
        return challenge

    def _view(self, ctx: ScreenCtx, challenge: Challenge) -> View:
        options = challenge.options
        rows: list[list[InlineKeyboardButton]] = [
            [
                nav_button(options[i], seeds.CAPTCHA, TAP, f"{challenge.nonce}.{i}")
                for i in range(start, min(start + PER_ROW, len(options)))
            ]
            for start in range(0, len(options), PER_ROW)
        ]
        view = screen_view(ctx, seeds.CAPTCHA, {"emoji": challenge.target}, top=rows)
        if challenge.target not in view.text:  # the owner took {emoji} out of the text
            view.text = f"{view.text}\n\n{challenge.target}"
        return view
