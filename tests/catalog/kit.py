"""Catalog test kit: seeding helpers over the real database and the plan-editor harness (real router, real
PostgreSQL, recording Telegram transport). Shared by ``tests/catalog`` and ``tests/tg/admin/test_plans*``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import svbg.catalog.tables  # registers the catalog tables in the shared metadata
import svbg.subscriptions.tables  # noqa: F401
from svbg.catalog.service import CatalogService
from svbg.content.store import ContentStore
from svbg.core.attention import AttentionService
from svbg.remnawave.models import InternalSquad, SquadInfo
from svbg.tg.admin.plans import PlanScreens
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.dbkit import CountingDatabase, add_user
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message

SQ_NL = "11111111-1111-4111-8111-111111111111"
SQ_DE = "22222222-2222-4222-8222-222222222222"
SQ_FI = "33333333-3333-4333-8333-333333333333"


async def add_location(
    db: CountingDatabase,
    uuid: str,
    name: str,
    *,
    sort: int = 0,
    flag: str | None = None,
    missing: bool = False,
) -> None:
    await db.raw(
        "insert into locations (squad_uuid, panel_name, sort, flag, missing_since) "
        "values ($1, $2, $3, $4, case when $5 then now() else null end)",
        uuid,
        name,
        sort,
        flag,
        missing,
    )


async def add_plan(
    db: CountingDatabase,
    code: str = "std",
    *,
    name: str = "Стандарт",
    squads: Sequence[str] = (SQ_NL,),
    enabled: bool = True,
    prices: Sequence[tuple[int, int]] = ((30, 17900),),
    currency: str = "RUB",
    **cols: Any,
) -> int:
    values: dict[str, Any] = {
        "code": code,
        "name": json.dumps({"ru": name}),
        "squads": json.dumps(list(squads)),
        "enabled": enabled,
        **cols,
    }
    names = ", ".join(values)
    params = ", ".join(
        f"${i}::jsonb" if k in ("name", "squads", "device_addon") else f"${i}"
        for i, k in enumerate(values, 1)
    )
    rows = await db.raw(f"insert into plans ({names}) values ({params}) returning id", *values.values())
    pid = int(rows[0]["id"])
    for days, amount in prices:
        await db.raw(
            "insert into plan_prices (plan_id, days, currency, amount_minor) values ($1, $2, $3, $4)",
            pid,
            days,
            currency,
            amount,
        )
    return pid


async def add_sub(
    db: CountingDatabase,
    plan_id: int | None,
    *,
    squads: Sequence[str] = (SQ_NL,),
    link_state: str = "linked",
    manual: bool = False,
    panel_user_id: int | None = None,
) -> int:
    overrides = {"squads": True} if manual else {}
    rows = await db.raw(
        "insert into subscriptions (plan_id, link_state, panel_user_id, desired_squads, overrides, "
        "plan_snapshot) "
        "values ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb) returning id",
        plan_id,
        link_state,
        panel_user_id
        if panel_user_id is not None
        else (None if link_state != "linked" else _next_panel_id()),
        json.dumps(list(squads)),
        json.dumps(overrides),
        json.dumps({"code": "std", "squads": list(squads)}),
    )
    return int(rows[0]["id"])


_panel_ids = iter(range(10_000, 10**9))


def _next_panel_id() -> int:
    return next(_panel_ids)


def squad(uuid: str, name: str, position: int = 0, members: int = 0) -> InternalSquad:
    return InternalSquad(uuid=uuid, name=name, view_position=position, info=SquadInfo(members_count=members))


@dataclass
class StubSource:
    """A panel stand-in for ``sync_locations``: returns ``squads`` or raises ``error``."""

    squads: list[InternalSquad] = field(default_factory=list)
    error: BaseException | None = None
    calls: int = 0

    async def internal_squads(self, *, lane: Any = None) -> list[InternalSquad]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.squads)


# ------------------------------------------------------------------------------------------- editor harness

OWNER = 1001
ADMIN = 2002
ADMIN_NOPERM = 3003
SUPPORT = 4004
USER = 5005


@dataclass
class PEnv:
    db: CountingDatabase
    transport: FakeTransport
    hub: FakeHub
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    catalog: CatalogService
    screens: PlanScreens
    attention: AttentionService
    source: StubSource
    denied: list[tuple[int, str]]

    async def add(self, tg_id: int, role: str = "user", perms: frozenset[str] = frozenset()) -> UserCtx:
        uid = await add_user(self.db, tg_id, role)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.users.by_tg[tg_id] = ctx
        return ctx

    async def staff(self) -> None:
        await self.add(OWNER, "owner")
        await self.add(ADMIN, "admin", frozenset({"plans"}))
        await self.add(ADMIN_NOPERM, "admin", frozenset({"settings.business"}))
        await self.add(SUPPORT, "support")
        await self.add(USER, "user")

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


async def build_penv(db: CountingDatabase, *, currency: str = "RUB", panel: bool = True) -> PEnv:
    transport, hub, users = FakeTransport(), FakeHub(), Users()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    denied: list[tuple[int, str]] = []

    async def on_denied(user: UserCtx, place: str) -> None:
        denied.append((user.user_id, place))

    router = make_router(
        transport, users, ui_state, content, CallbackCodec(db, key=b"p" * 32), hub, on_denied=on_denied
    )
    catalog = CatalogService(db, currency=currency)
    await catalog.load()
    attention = AttentionService(db)
    source = StubSource()
    screens = PlanScreens(
        router,
        catalog,
        currency=lambda: currency,
        squad_source=(lambda: source) if panel else (lambda: None),
        attention=attention,
    )
    screens.install()
    return PEnv(db, transport, hub, users, router, ui_state, catalog, screens, attention, source, denied)
