"""«🎟 Промокод» for users: the code is typed as a reply (a form that survives restarts).

* screen ``promo`` (and the content action ``system:promo``) — asks for the code; a waiting discount is shown;
* form ``user.promo_code`` → :meth:`PromoService.activate`: «🎁 применён: +7 дней…», «✅ принят: −20 %… скидка
  применится при оплате» with «🛒 Купить», or the refusal with «🔁 Другой код».

Unknown codes are rate-limited by the service (brute force). Rendering a waiting discount costs no SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from svbg.tg.ui.forms import Field, Form
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, View

if TYPE_CHECKING:
    from svbg.promo.service import PromoService
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["FORM", "SCREEN", "TEXTS", "PromoUserScreens"]

SCREEN: Final = "promo"
FORM: Final = "user.promo_code"

TEXTS: Final[dict[str, dict[str, str]]] = {
    "ru": {
        "prompt": "🎟 Отправьте промокод ответным сообщением.",
        "waiting": "🏷 Уже ждёт оплаты: промокод {code} — {label}. Новый код заменит его.",
        "buy": "🛒 Купить",
        "again": "🔁 Другой код",
        "menu": "🏠 Меню",
    },
    "en": {
        "prompt": "🎟 Send the promo code as a reply.",
        "waiting": "🏷 Waiting for checkout: {code} — {label}. A new code replaces it.",
        "buy": "🛒 Buy",
        "again": "🔁 Another code",
        "menu": "🏠 Menu",
    },
}


def _t(lang: str, key: str, **values: Any) -> str:
    table = TEXTS.get(lang) or TEXTS["ru"]
    return (table.get(key) or TEXTS["ru"][key]).format(**values)


class PromoUserScreens:
    def __init__(self, service: PromoService, *, home: str = "home", buy: str = "buy") -> None:
        self.service = service
        self.home = home
        self.buy = buy

    def register(self, router: ScreenRouter) -> None:
        router.screen(SCREEN)(self.screen)
        router.action("sys", "promo")(self.sys_promo)
        router.form(
            Form(
                FORM,
                (
                    Field(
                        "code",
                        {"ru": _t("ru", "prompt"), "en": _t("en", "prompt")},
                        text_validator(max_len=64),
                    ),
                ),
                on_done=self._done,
            )
        )

    async def sys_promo(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return Redirect(SCREEN)

    async def screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        view = await ctx.start_form(FORM)
        entry = self.service.pending(ctx.user.user_id)
        if entry is not None:
            label = entry.label_en if ctx.lang == "en" and entry.label_en else entry.label
            view.text = _t(ctx.lang, "waiting", code=entry.code, label=label) + "\n\n" + view.text
        return view

    async def _done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        lang = ctx.lang
        res = await self.service.activate(ctx.user.user_id, data.get("code"), source="bot")
        rows = []
        if res.outcome == "pending":
            rows.append([nav_button(_t(lang, "buy"), self.buy, style="success")])
        elif res.outcome == "refused" and res.reason != "too_many":
            rows.append([nav_button(_t(lang, "again"), SCREEN)])
        rows.append([nav_button(_t(lang, "menu"), self.home)])
        return View(text=self.service.localize(res, lang), keyboard=rows)
