"""Promo codes for users: «👤 Профиль» → «🎟 Промокоды» and the code entry (a form that survives restarts).

* screen ``promos`` (content, owner-editable) — the codes the user entered (code, what it gave, date; the last
  10), the discount that waits for the purchase, «✏️ Ввести промокод». One SQL (the history); the waiting
  discount is read from memory;
* screen ``promo`` (and the content action ``system:promo``, the promo deep links of ``s_promo``) — asks for
  the code; a waiting discount is shown;
* form ``user.promo_code`` → :meth:`PromoService.activate`: «🎁 применён: +7 дней…», «✅ принят: −20 %… скидка
  применится при оплате» with «🛒 Купить», or the refusal with «🔁 Другой код».

Unknown codes are rate-limited by the service (brute force).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from svbg.tg.ui.forms import Field, Form
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, View
from svbg.tg.user import seeds
from svbg.tg.user.render import screen_view
from svbg.tg.user.texts import fmt_date

if TYPE_CHECKING:
    from svbg.promo.service import PromoService, UsedCode
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["FORM", "HISTORY_LIMIT", "SCREEN", "SCREEN_LIST", "TEXTS", "PromoUserScreens"]

SCREEN: Final = seeds.PROMO_ENTRY
SCREEN_LIST: Final = seeds.PROMOS
FORM: Final = "user.promo_code"
#: How many entered codes the list shows.
HISTORY_LIMIT: Final = 10
#: A code or a description longer than this is cut (the screen is a picture caption: ≤ 1024 characters).
_CODE_MAX: Final = 24
_WHAT_MAX: Final = 40

TEXTS: Final[dict[str, str]] = {
    "prompt": "🎟 Отправьте промокод ответным сообщением.",
    "waiting": "Уже ждёт оплаты промокод {code}: {label}. Новый код заменит его.",
    "pending": "Скидка ждёт оплаты: {code}, {label}. Действует до {until}.\n\n",
    "list_head": "Вы вводили:\n",
    "line": "{code} · {what} · {date}",
    "line_waiting": "{code} · {what} · ждёт оплаты",
    "list_empty": "Вы ещё не вводили промокоды.",
    "buy": "🛒 Купить",
    "again": "🔁 Другой код",
    "back": "◀️ Назад",
    "menu": "🏠 Меню",
}


def _t(key: str, **values: Any) -> str:
    return TEXTS[key].format(**values)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def history_text(items: list[UsedCode], tz: str) -> str:
    """``{list}`` of the «🎟 Промокоды» screen."""
    if not items:
        return _t("list_empty")
    lines = [
        _t(
            "line_waiting" if item.waiting else "line",
            code=_cut(item.code, _CODE_MAX),
            what=_cut(item.what, _WHAT_MAX),
            date=fmt_date(item.used_at, tz),
        )
        for item in items
    ]
    return _t("list_head") + "\n".join(lines)


class PromoUserScreens:
    def __init__(self, service: PromoService, *, home: str = "home", buy: str = "buy") -> None:
        self.service = service
        self.home = home
        self.buy = buy

    def register(self, router: ScreenRouter) -> None:
        router.screen(SCREEN)(self.screen)
        router.screen(SCREEN_LIST)(self.list_screen)
        router.action("sys", "promo")(self.sys_promo)
        router.form(
            Form(
                FORM,
                (Field("code", _t("prompt"), text_validator(max_len=64)),),
                on_done=self._done,
            )
        )

    async def sys_promo(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return Redirect(SCREEN)

    async def list_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        """«🎟 Промокоды»: the entered codes and the waiting discount (content text and buttons)."""
        tz = self.service.timezone
        items = await self.service.history(ctx.user.user_id, HISTORY_LIMIT)
        entry = self.service.pending(ctx.user.user_id)
        pending = ""
        if entry is not None:
            pending = _t(
                "pending",
                code=_cut(entry.code, _CODE_MAX),
                label=_cut(entry.label, _WHAT_MAX),
                until=fmt_date(entry.until, tz),
            )
        return screen_view(ctx, SCREEN_LIST, {"list": history_text(items, tz), "pending": pending})

    async def screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        view = await ctx.start_form(FORM)
        entry = self.service.pending(ctx.user.user_id)
        if entry is not None:
            view.text = _t("waiting", code=entry.code, label=entry.label) + "\n\n" + view.text
        return view

    async def _done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        res = await self.service.activate(ctx.user.user_id, data.get("code"), source="bot")
        rows = []
        if res.outcome == "pending":
            rows.append([nav_button(_t("buy"), self.buy, style="success")])
        elif res.outcome == "refused" and res.reason != "too_many":
            rows.append([nav_button(_t("again"), SCREEN)])
        rows.append([nav_button(_t("back"), SCREEN_LIST), nav_button(_t("menu"), self.home)])
        return View(text=res.text, keyboard=rows)
