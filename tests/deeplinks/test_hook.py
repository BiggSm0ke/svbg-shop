"""App glue: the router access probe, ``from_app`` (notify through the transport), ``resume_redirect``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiogram.methods import SendMessage

from svbg.content.store import ContentStore
from svbg.core import clock
from svbg.deeplinks.hook import from_app, resume_redirect, router_can_open
from svbg.deeplinks.service import TEXTS
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenCtx, ScreenRouter, UiStateStore
from svbg.tg.ui.view import Redirect, View
from tests.dbkit import CountingDatabase, add_user
from tests.deeplinks.kit import FakeCatalog, FakePromo
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, make_router

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


async def _router(db: CountingDatabase) -> tuple[ScreenRouter, FakeTransport]:
    transport = FakeTransport()
    content = ContentStore(db)
    await content.load()
    router = make_router(
        transport, Users(), UiStateStore(db), content, CallbackCodec(db, key=b"h" * 32), FakeHub()
    )

    async def screen(_ctx: Any, _arg: Any) -> View:
        return View(text="x")

    router.screen("buy")(screen)
    router.screen("secret", required_role="admin")(screen)
    router.screen("co")(screen)
    return router, transport


class _Settings:
    def current(self) -> dict[str, Any]:
        return {"CURRENCY": "RUB"}


class _Deps:
    def __init__(self, db: CountingDatabase, router: ScreenRouter) -> None:
        self.db = db
        self.screens = router
        self.settings = _Settings()
        self.catalog = FakeCatalog()
        self.hub = FakeHub()


async def test_router_can_open(db: CountingDatabase) -> None:
    router, _ = await _router(db)
    check = router_can_open(router)
    user = UserCtx(1)
    admin = UserCtx(2, role="admin")
    assert check(user, "buy") and check(user, "home")
    assert not check(user, "secret") and check(admin, "secret")
    assert not check(user, "settings_root") and check(admin, "settings_root")  # content screen policy
    assert not check(admin, "co") and not check(admin, "error") and not check(admin, "notify_x")
    assert not check(user, "nope")


async def test_from_app_start_and_resume(db: CountingDatabase) -> None:
    clock.set_clock(NOW)
    try:
        router, transport = await _router(db)
        service = from_app(_Deps(db, router), promo=FakePromo(), referral=lambda _uid, _code: True)
        uid = await add_user(db, 77)
        user = UserCtx(uid, telegram_id=77, is_new=True)
        gate_closed = True

        async def gate(_user: UserCtx, _chat: int, _link: Any) -> tuple[str, Any] | None:
            return ("chan", None) if gate_closed else None

        hook = service.start_hook(gate)
        assert await hook(user, 77, "pr_WEEK") == ("chan", None)
        gate_closed = False
        ctx = ScreenCtx(router, user, 77)
        redirect = await resume_redirect(service, ctx)
        assert redirect == Redirect("home", None, toast=TEXTS["promo_applied"].format(code="WEEK"))
        assert await resume_redirect(service, ctx) is None
        assert await resume_redirect(None, ctx) is None
        assert await hook(user, 77, "s_secret") is None  # the router would deny it: home + a line
        sent = [c for c in transport.calls if isinstance(c, SendMessage)]
        assert [c.text for c in sent] == [TEXTS["screen_gone"]]
        assert await hook(user, 77, "s_buy") == ("buy", None)
    finally:
        clock.reset_clock()
