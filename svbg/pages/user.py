"""Pages for users: screen ``page`` (arg = page code), aliases ``page.<code>`` for content buttons
(``screen:page.faq``) and ``system:faq`` / ``system:rules`` / ``system:offer``.

The text is sent with its entities exactly as the owner wrote it (premium emoji, spoilers, links…). The
consent page carries «✅ Принимаю» (``page:ok:<code>:<version>``); after it the ``after_consent`` hook decides
what to open (e.g. the deep-link intent kept through onboarding), the home screen by default. Staff may open a
switched off page (preview); users get the home screen. Rendering costs no SQL (pages are in memory).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from svbg.tg.ui.renderer import nav_button, to_entities
from svbg.tg.ui.view import Redirect, View

if TYPE_CHECKING:
    from svbg.pages.service import Page, PageService
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = ["ALIAS_PREFIX", "SCREEN", "SYS_ALIASES", "PageUserScreens"]

SCREEN: Final = "page"
ALIAS_PREFIX: Final = "page."
SYS_ALIASES: Final = ("faq", "rules", "offer")
AfterConsent = Callable[["ScreenCtx"], Awaitable["HandlerResult"]]

_T: Final[dict[str, str]] = {
    "accept": "✅ Принимаю",
    "menu": "🏠 Меню",
    "gone": "Страница недоступна",
    "thanks": "Спасибо!",
}


class PageUserScreens:
    def __init__(
        self, service: PageService, *, home: str = "home", after_consent: AfterConsent | None = None
    ) -> None:
        self.service = service
        self.home = home
        self.after_consent = after_consent
        self._router: ScreenRouter | None = None
        self._aliases: set[str] = set()

    def register(self, router: ScreenRouter) -> None:
        self._router = router
        router.screen(SCREEN)(self.screen)
        router.action(SCREEN, "ok")(self.accept)
        for name in SYS_ALIASES:
            router.action("sys", name)(self._sys(name))
        for page in self.service.all():
            self.register_alias(page.code)

    def register_alias(self, code: str) -> None:
        """``page.<code>`` as a screen of its own (content buttons open screens without an argument)."""
        if self._router is None or code in self._aliases:
            return
        self._aliases.add(code)

        async def alias(ctx: ScreenCtx, _arg: Any) -> HandlerResult:
            return await self.screen(ctx, code)

        alias.__name__ = f"page_{code}"
        self._router.screen(ALIAS_PREFIX + code)(alias)

    def _sys(self, code: str) -> Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]:
        async def go(_ctx: ScreenCtx, _arg: Any) -> HandlerResult:
            return Redirect(SCREEN, code)

        go.__name__ = f"sys_{code}"
        return go

    async def screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        page = self.service.get(arg) if isinstance(arg, str) else None
        if page is None or (not page.enabled and not ctx.user.at_least("admin")):
            return Redirect(self.home, toast=_T["gone"])
        return self.view(page, ctx.lang)

    def view(self, page: Page, lang: str) -> View:
        block = page.block_for(lang)
        text = block.text if block is not None else page.title_for(lang)
        entities = to_entities(block.entities) if block is not None and block.entities else None
        rows = []
        if page.kind == "consent" and page.consent_version is not None:
            arg = f"{page.code}:{page.consent_version}"
            rows.append([nav_button(_T["accept"], SCREEN, "ok", arg, style="success")])
        rows.append([nav_button(_T["menu"], self.home)])
        return View(text=text, entities=entities, keyboard=rows)

    async def accept(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = arg.rsplit(":", 1) if isinstance(arg, str) else []
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
            return Redirect(self.home)
        if not await self.service.accept(ctx.user.user_id, parts[0], int(parts[1])):
            page = self.service.consent_page()
            # the text changed meanwhile: show the current one to accept
            return Redirect(SCREEN, page.code) if page is not None else Redirect(self.home)
        if self.after_consent is not None:
            result = await self.after_consent(ctx)
            if result is not None:
                return result
        return Redirect(self.home, toast=_T["thanks"])
