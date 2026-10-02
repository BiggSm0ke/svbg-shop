"""Harness for the admin user screens, roles and dashboard: real router + real PostgreSQL, recording Telegram
transport, fake directory (owners, cache invalidation) and a fake notifier."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import svbg.catalog.tables
import svbg.subscriptions.tables  # noqa: F401
from svbg.catalog.service import CatalogService
from svbg.content.store import ContentStore
from svbg.core.clock import now
from svbg.services.roles import ADMIN_PERMS, Limits
from svbg.tg.admin.dashboard import Dashboard
from svbg.tg.admin.menu import AdminMenu
from svbg.tg.admin.roles import RoleScreens
from svbg.tg.admin.users.ops import UserOps
from svbg.tg.admin.users.screens import UserScreens
from svbg.tg.admin.users.search import ensure_indexes
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.dbkit import CountingDatabase, add_user
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message

OWNER = 1001
ADMIN = 2002  # every Admin right
ADMIN_STATS = 3003  # only «stats»
SUPPORT = 4004
USER = 5005  # the target of most tests
OTHER = 6006
CONF_OWNER = 7007  # owner by OWNER_IDS only

ALL_ADMIN = frozenset(ADMIN_PERMS)


class FakeDirectory:
    """``UserDirectory`` stand-in: owners (``OWNER_IDS`` ∪ stored) and recorded invalidations."""

    def __init__(self, configured: frozenset[int] = frozenset({CONF_OWNER})) -> None:
        self.configured = configured
        self.invalidated: list[int | None] = []

    def configured_owner_ids(self) -> frozenset[int]:
        return self.configured

    async def owner_ids(self) -> frozenset[int]:
        return self.configured | {OWNER}

    async def configured_async(self) -> frozenset[int]:
        return self.configured

    def invalidate(self, telegram_id: int | None = None) -> None:
        self.invalidated.append(telegram_id)


@dataclass
class FakeNotifier:
    sent: list[tuple[int, str, str | None]] = field(default_factory=list)
    blocked: set[int] = field(default_factory=set)
    fail: Exception | None = None

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = None, **_kw: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        if chat_id in self.blocked:
            return None
        self.sent.append((chat_id, text, parse_mode))
        return object()


@dataclass
class UEnv:
    db: CountingDatabase
    transport: FakeTransport
    hub: FakeHub
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    catalog: CatalogService
    directory: FakeDirectory
    notifier: FakeNotifier
    screens: UserScreens
    ops: UserOps
    dashboard: Dashboard
    denied: list[tuple[int, str]]
    settings: dict[str, Any]
    ids: dict[int, int] = field(default_factory=dict)  # telegram id → users.id
    menu: Any = None  # svbg.tg.admin.menu.AdminMenu

    async def add(
        self,
        tg_id: int,
        role: str = "user",
        perms: frozenset[str] = frozenset(),
        *,
        username: str | None = None,
        first_name: str | None = None,
    ) -> int:
        uid = await add_user(self.db, tg_id, role)
        await self.db.raw(
            "update users set perms = $2::jsonb, username = $3, first_name = $4 where id = $1",
            uid,
            json.dumps(sorted(perms)),
            username,
            first_name,
        )
        self.users.by_tg[tg_id] = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.ids[tg_id] = uid
        return uid

    async def staff(self) -> None:
        await self.add(OWNER, "owner", first_name="Влад")
        await self.add(ADMIN, "admin", ALL_ADMIN, first_name="Админ")
        await self.add(ADMIN_STATS, "admin", frozenset({"stats"}), first_name="Аналитик")
        await self.add(SUPPORT, "support", first_name="Саппорт")
        await self.add(USER, "user", username="ivan_petrov", first_name="Иван")

    async def main_id(self, tg_id: int) -> int:
        state = await self.ui_state.get(self.users.by_tg[tg_id].user_id)
        return state.main_msg_id or 10

    async def click(self, tg_id: int, data: str) -> None:
        msg_id = await self.main_id(tg_id)
        await self.router.dispatch_callback(callback(tg_id, data, message_id=msg_id))

    async def press(self, tg_id: int, label: str) -> None:
        await self.click(tg_id, self.button(label))

    async def type(self, tg_id: int, text: str) -> bool:
        return await self.router.dispatch_message(text_message(tg_id, text, message_id=700))

    def rendered(self) -> list[SendMessage | EditMessageText]:
        return [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText)]

    @property
    def text(self) -> str:
        return self.rendered()[-1].text

    def buttons(self) -> list[InlineKeyboardButton]:
        markup = self.rendered()[-1].reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return [b for row in markup.inline_keyboard for b in row]

    def labels(self) -> list[str]:
        return [b.text for b in self.buttons()]

    def button(self, label: str) -> str:
        for b in self.buttons():
            if label in b.text:
                assert b.callback_data is not None
                return b.callback_data
        raise AssertionError(f"no button {label!r} in {self.labels()}")

    @property
    def toasts(self) -> list[str | None]:
        return self.transport.toasts

    async def audit(self) -> list[Any]:
        return await self.db.raw("select * from admin_audit order by id")

    async def jobs(self, kind: str | None = None) -> list[Any]:
        if kind is None:
            return await self.db.raw("select * from jobs order by id")
        return await self.db.raw("select * from jobs where kind = $1 order by id", kind)


async def add_sub(
    db: CountingDatabase,
    user_id: int,
    *,
    days: float = 10,
    link_state: str = "linked",
    is_trial: bool = False,
    panel_user_id: int | None = None,
    username: str | None = None,
    short_uuid: str | None = None,
    plan_name: str = "Стандарт",
    hold: bool = False,
) -> int:
    paid_until = now() + timedelta(days=days)
    rows = await db.raw(
        "insert into subscriptions (user_id, link_state, panel_user_id, panel_username, panel_short_uuid, "
        "paid_until, desired_expire_at, desired_squads, plan_snapshot, is_trial, desired_device_limit, "
        "hold_kind, hold_since, subscription_url) "
        "values ($1, $2, $3, $4, $5, $6, $6, $7::jsonb, $8::jsonb, $9, 5, $10, $11, $12) returning id",
        user_id,
        link_state,
        panel_user_id
        if panel_user_id is not None
        else (900_000 + user_id if link_state == "linked" else None),
        username,
        short_uuid,
        paid_until,
        json.dumps(["11111111-1111-4111-8111-111111111111"]),
        json.dumps(
            {"name": {"ru": plan_name}, "code": "std", "squads": ["11111111-1111-4111-8111-111111111111"]}
        ),
        is_trial,
        "admin" if hold else None,
        now() if hold else None,
        f"https://sub.example/{short_uuid}" if short_uuid else None,
    )
    return int(rows[0]["id"])


async def add_instance(db: CountingDatabase, title: str = "RollyPay", slug: str = "rollypay") -> int:
    rows = await db.raw(
        "insert into payment_instances (provider, slug, title, enabled, config, webhook_token) "
        "values ($1, $2, $3, true, 'enc:v1:x', 'enc:v1:y') returning id",
        slug,
        slug,
        title,
    )
    return int(rows[0]["id"])


async def add_payment(
    db: CountingDatabase,
    instance_id: int,
    user_id: int,
    amount: int,
    *,
    status: str = "paid",
    currency: str = "RUB",
    paid_at: datetime | None = None,
    is_test: bool = False,
    is_imported: bool = False,
) -> str:
    at = paid_at or now()
    rows = await db.raw(
        "insert into payments (instance_id, user_id, status, amount_minor, currency, paid_amount_minor, "
        "paid_at, is_test, is_imported, created_at) "
        "values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning id",
        instance_id,
        user_id,
        status,
        amount,
        currency,
        amount if status == "paid" else None,
        at if status == "paid" else None,
        is_test,
        is_imported,
        at,
    )
    return str(rows[0]["id"])


async def build_uenv(
    db: CountingDatabase,
    *,
    settings: dict[str, Any] | None = None,
    staff: bool = True,
    module_perms: tuple[tuple[str, str], ...] = (),
    settings_service: Any = None,
    components: Any = None,
) -> UEnv:
    async with db.tx() as conn:
        await ensure_indexes(conn)
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    denied: list[tuple[int, str]] = []

    async def on_denied(user: UserCtx, place: str) -> None:
        denied.append((user.user_id, place))

    router = make_router(
        transport, users, ui_state, content, CallbackCodec(db, key=b"u" * 32), hub, on_denied=on_denied
    )
    catalog = CatalogService(db, currency="RUB")
    await catalog.load()
    directory = FakeDirectory()
    notifier = FakeNotifier()
    values: dict[str, Any] = {"CURRENCY": "RUB", "TIMEZONE": "Europe/Moscow", **(settings or {})}

    def limits() -> Limits:
        prices = [p.amount_minor for plan in catalog.snapshot.plans for p in plan.prices_in("RUB")]
        return Limits.from_settings(
            values, currency_exponent=2, max_plan_price_minor=max(prices, default=None)
        )

    ops = UserOps(
        db,
        owner_ids=directory.configured_async,
        limits=limits,
        currency=lambda: "RUB",
        plans=lambda: catalog.snapshot,
        notifier=notifier,
    )
    screens = UserScreens(
        router,
        db,
        ops,
        owner_ids=directory.configured_async,
        invalidate=directory.invalidate,
        plans=lambda: catalog.snapshot,
        timezone=lambda: values["TIMEZONE"],
    )
    screens.install()
    RoleScreens(
        router,
        db,
        owner_ids=directory.configured_async,
        configured_owners=directory.configured_owner_ids,
        invalidate=directory.invalidate,
        module_perms=lambda: module_perms,
    ).install()
    dashboard = Dashboard(db, timezone=lambda: values["TIMEZONE"])
    menu = AdminMenu(router, dashboard, settings=settings_service, components=components)
    menu.install()
    env = UEnv(
        db,
        transport,
        hub,
        users,
        router,
        ui_state,
        catalog,
        directory,
        notifier,
        screens,
        ops,
        dashboard,
        denied,
        values,
        menu=menu,
    )
    if staff:
        await env.staff()
    return env
