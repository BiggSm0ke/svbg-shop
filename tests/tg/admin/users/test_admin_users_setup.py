"""Module entry points ``setup(router, deps)`` of users / roles / dashboard on one router (as ``svbg.app``
does)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiogram import Router

from svbg.catalog.service import CatalogService
from svbg.content.store import ContentStore
from svbg.tg.admin import dashboard
from svbg.tg.admin import roles as roles_screens
from svbg.tg.admin import users as users_module
from svbg.tg.admin.users.screens import SCREEN_CARD
from svbg.tg.admin.users.search import ensure_indexes
from svbg.tg.ui.codec import CallbackCodec, encode
from svbg.tg.ui.router import UiStateStore
from tests.catalog.kit import add_plan
from tests.dbkit import CountingDatabase
from tests.tg.admin.users.kit import ADMIN, ALL_ADMIN, USER, FakeNotifier
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router


class Directory:
    def __init__(self) -> None:
        self.invalidated: list[int | None] = []

    def configured_owner_ids(self) -> frozenset[int]:
        return frozenset({1})

    async def owner_ids(self) -> frozenset[int]:
        return frozenset({1})

    def invalidate(self, telegram_id: int | None = None) -> None:
        self.invalidated.append(telegram_id)


class Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def current(self) -> dict[str, Any]:
        return self.values


async def test_setup_wires_everything(db: CountingDatabase) -> None:
    async with db.tx() as conn:
        await ensure_indexes(conn)
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    router = make_router(transport, users, UiStateStore(db), content, CallbackCodec(db, key=b"s" * 32), hub)
    await add_plan(db, "std", prices=((30, 17900),))
    catalog = CatalogService(db, currency="RUB")
    await catalog.load()
    settings = Settings({"CURRENCY": "RUB", "TIMEZONE": "Europe/Moscow", "OWNER_IDS": [1]})
    deps = SimpleNamespace(
        db=db, settings=settings, users=Directory(), notifier=FakeNotifier(), catalog=catalog, attention=None
    )
    assert isinstance(users_module.setup(router, deps), Router)
    assert roles_screens.setup(router, deps) is None
    assert isinstance(dashboard.setup(router, deps), Router)

    from tests.tg.admin.users.kit import UEnv

    env = UEnv(
        db, transport, hub, users, router, router.ui_state, catalog, None, None, None, None, None, [], {}
    )  # type: ignore[arg-type]
    await env.add(ADMIN, "admin", ALL_ADMIN)
    uid = await env.add(USER, username="ivan")
    await env.click(ADMIN, encode(SCREEN_CARD, arg=str(uid)))
    assert "@ivan" in env.text
    # the wallet limit comes from the catalog (most expensive plan: 179 ₽) and applies through setup's wiring
    await env.press(ADMIN, "💰 Баланс")
    await env.type(ADMIN, "180")
    await env.type(ADMIN, "проверка лимита")
    assert "только владелец" in env.text
    await env.press(ADMIN, "💰 Баланс")
    await env.type(ADMIN, "179")
    await env.type(ADMIN, "проверка лимита")
    assert "✅ Баланс: +179 ₽" in env.text
    settings.values["ADMIN_WALLET_ADJUST_MAX"] = 1000  # hot: the next operation uses it
    await env.press(ADMIN, "💰 Баланс")
    await env.type(ADMIN, "999")
    await env.type(ADMIN, "проверка лимита")
    assert "✅ Баланс: +999 ₽" in env.text
    await env.click(ADMIN, encode(dashboard.SCREEN))
    assert "Админка" in env.text and "Выручка" in env.text
    # a callback to the card from the private chat of a non-staff user
    await router.dispatch_callback(callback(USER, encode(SCREEN_CARD, arg=str(uid))))
    assert transport.toasts[-1] == "Нет прав"
