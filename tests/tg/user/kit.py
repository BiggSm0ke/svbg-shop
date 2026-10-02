"""User path test kit: the billing kit (real PostgreSQL, payment core with stub providers, the real
subscription lifecycle and panel writer against the fake Remnawave panel) + a real :class:`ScreenRouter`
over a recording Telegram transport + the real :class:`UserDirectory` + :class:`UserPath`.

The user path's tables (``notification_log``, ``user_devices``) are attached to the shared metadata only while
a test runs (integration registers them in ``TABLE_MODULES`` + a migration), like the billing kit does.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    CreateInvoiceLink,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, PhotoSize, User

import svbg.services.notify_user as notify_mod
import svbg.tg.user.tables as user_tables
from svbg.content import defaults
from svbg.content.store import ContentStore, seed_system_screens
from svbg.db import schema
from svbg.db.meta import metadata
from svbg.subscriptions.channel import ChannelService, Membership
from svbg.subscriptions.devices import SubscriptionActions
from svbg.subscriptions.terms import CatalogTrialSource
from svbg.subscriptions.trial import TrialService
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from svbg.tg.user.deps import PanelDevice, UserPathDeps
from svbg.tg.user.directory import UserDirectory
from svbg.tg.user.seeds import USER_SCREENS
from svbg.tg.user.wiring import UserPath
from tests.billing.kit import (
    BillingEnv,
    FakeCatalog,
    build_billing_env,
    owner_plan_rows,
)
from tests.billing.kit import (
    attach_tables as attach_billing,
)
from tests.billing.kit import (
    detach_tables as detach_billing,
)
from tests.tg.ui.ui_harness import FakeHub

__all__ = ["Tg", "UserEnv", "attach_tables", "build_user_env", "detach_tables"]

USER_TABLES = (notify_mod.notification_log, user_tables.user_devices)
_REGISTERED = {
    "notify": "svbg.services.notify_user" in schema.TABLE_MODULES,
    "user": "svbg.tg.user.tables" in schema.TABLE_MODULES,
}
DATE = datetime(2026, 10, 1, tzinfo=UTC)
CHANNEL = -100_777


def detach_tables() -> None:
    detach_billing()
    for table, key in ((notify_mod.notification_log, "notify"), (user_tables.user_devices, "user")):
        if not _REGISTERED[key] and table.name in metadata.tables:
            metadata.remove(table)


def attach_tables() -> None:
    attach_billing()
    for table in USER_TABLES:
        if table.name not in metadata.tables:
            metadata._add_table(table.name, table.schema, table)


detach_tables()


@dataclass
class Shown:
    message_id: int
    chat_id: int
    text: str
    markup: InlineKeyboardMarkup | None
    photo: bool = False

    def buttons(self) -> list[Any]:
        return [b for row in (self.markup.inline_keyboard if self.markup else []) for b in row]

    def __post_init__(self) -> None:
        self.text = self.text.replace(" ", " ")  # Telegram shows a no-break space as a space

    def labels(self) -> list[str]:
        return [b.text.replace(" ", " ") for b in self.buttons()]

    def data(self, label_part: str) -> str:
        """``callback_data`` of the first button whose label contains ``label_part``."""
        for b in self.buttons():
            if label_part in b.text.replace(" ", " ") and b.callback_data:
                return str(b.callback_data)
        raise AssertionError(f"no button {label_part!r} in {self.labels()}")

    def button(self, label_part: str) -> Any:
        for b in self.buttons():
            if label_part in b.text.replace(" ", " "):
                return b
        raise AssertionError(f"no button {label_part!r} in {self.labels()}")


class Tg:
    """Recording Telegram transport that remembers what every message currently shows."""

    bot_id: int | None = 42
    bot_username: str | None = "svbg_bot"

    def __init__(self) -> None:
        self.calls: list[TelegramMethod[Any]] = []
        self.answers: list[AnswerCallbackQuery] = []
        self.messages: dict[tuple[int, int], Shown] = {}
        self.blocked: set[int] = set()
        self._ids = itertools.count(1000)

    async def answer(self, method: AnswerCallbackQuery) -> None:
        self.answers.append(method)

    async def call(self, method: TelegramMethod[Any], *, chat_id: int) -> Any:
        self.calls.append(method)
        if isinstance(method, CreateInvoiceLink):
            return f"https://t.me/$invoice-{method.payload[:8]}"
        if isinstance(method, (SendMessage, SendPhoto)):
            if chat_id in self.blocked:
                from aiogram.exceptions import TelegramForbiddenError

                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
            mid = next(self._ids)
            is_photo = isinstance(method, SendPhoto)
            text = (method.caption or "") if is_photo else method.text
            self.messages[(chat_id, mid)] = Shown(mid, chat_id, text or "", method.reply_markup, is_photo)  # type: ignore[arg-type]
            extra: dict[str, Any] = (
                {"photo": [PhotoSize(file_id="qr", file_unique_id="u", width=1, height=1)], "caption": text}
                if is_photo
                else {"text": text}
            )
            return Message(message_id=mid, date=DATE, chat=Chat(id=chat_id, type="private"), **extra)
        if isinstance(method, EditMessageText):
            key = (int(method.chat_id or 0), int(method.message_id or 0))
            old = self.messages.get(key)
            if old is None:
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            if old.photo:
                raise TelegramBadRequest(
                    method=method, message="Bad Request: there is no text in the message to edit"
                )
            self.messages[key] = Shown(key[1], key[0], method.text, method.reply_markup)  # type: ignore[arg-type]
            return True
        if isinstance(method, EditMessageMedia):  # a text message becomes a picture in place
            key = (int(method.chat_id or 0), int(method.message_id or 0))
            if key not in self.messages:
                raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
            caption = method.media.caption or ""
            self.messages[key] = Shown(key[1], key[0], caption, method.reply_markup, True)  # type: ignore[arg-type]
            return Message(
                message_id=key[1],
                date=DATE,
                chat=Chat(id=key[0], type="private"),
                photo=[PhotoSize(file_id="qr", file_unique_id="u", width=1, height=1)],
                caption=caption,
            )
        return True

    def shown(self, chat_id: int, message_id: int) -> Shown:
        return self.messages[(chat_id, message_id)]

    def last(self, chat_id: int) -> Shown:
        mine = [m for (c, _), m in self.messages.items() if c == chat_id]
        assert mine, "nothing was shown"
        return max(mine, key=lambda m: m.message_id)

    def toasts(self) -> list[str | None]:
        return [a.text for a in self.answers]


class Settings:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def current(self) -> Mapping[str, Any]:
        return self.config


@dataclass
class Lookup:
    member: bool = True
    calls: int = 0

    async def __call__(self, chat_id: int, telegram_id: int) -> Membership:
        self.calls += 1
        return Membership("member" if self.member else "left", self.member)


@dataclass
class Panel:
    """``fetch_devices`` over the fake panel (what the app wires from ``RemnawaveApi.devices``)."""

    env: BillingEnv
    calls: int = 0

    async def __call__(self, panel_user_id: int) -> list[PanelDevice]:
        self.calls += 1
        result = await self.env.s.api.devices(panel_user_id)
        return [
            PanelDevice(d.hwid, d.platform, d.os_version, d.device_model, str(d.created_at or ""))
            for d in result.devices
        ]


@dataclass
class UserEnv:
    b: BillingEnv
    tg: Tg
    router: ScreenRouter
    directory: UserDirectory
    path: UserPath
    config: dict[str, Any]
    content: ContentStore
    lookup: Lookup
    panel_fetch: Panel
    hub: FakeHub
    _cq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def db(self) -> Any:
        return self.b.db

    async def new_user(self, *, balance: int = 0, first_name: str = "Аня") -> tuple[int, int]:
        """``(user_id, telegram_id)`` of a fresh user (optionally with money on the balance)."""
        uid = await self.b.user()
        tg_id = await self.b.telegram_id(uid)
        await self.db.raw("update users set first_name = $1 where id = $2", first_name, uid)
        if balance:
            await self.b.fund(uid, balance)
        return uid, tg_id

    def tg_user(self, tg_id: int) -> User:
        return User(id=tg_id, is_bot=False, first_name="Аня", language_code="ru")

    async def ctx(self, tg_id: int) -> UserCtx:
        ctx = await self.directory.load(self.tg_user(tg_id))
        assert ctx is not None
        return ctx

    async def open(self, tg_id: int, screen: str = "home", arg: Any = None) -> Shown:
        """``/start``-like: show ``screen`` as a fresh main message."""
        user = await self.ctx(tg_id)
        await self.router.show(user, tg_id, screen, arg, new=True)
        return self.tg.last(tg_id)

    async def click(self, tg_id: int, data: str, *, message_id: int | None = None) -> Shown:
        """Press a button with ``data`` on ``message_id`` (default: the user's latest message)."""
        mid = message_id if message_id is not None else self.tg.last(tg_id).message_id
        shown = self.tg.messages.get((tg_id, mid))
        chat = Chat(id=tg_id, type="private")
        if shown is not None and shown.photo:
            msg = Message(
                message_id=mid,
                date=DATE,
                chat=chat,
                photo=[PhotoSize(file_id="qr", file_unique_id="u", width=1, height=1)],
                caption=shown.text,
            )
        else:
            msg = Message(message_id=mid, date=DATE, chat=chat, text=shown.text if shown else "old")
        query = CallbackQuery(
            id=f"cq{next(self._cq)}",
            from_user=self.tg_user(tg_id),
            chat_instance="ci",
            data=data,
            message=msg,
        )
        await self.router.dispatch_callback(query)
        return self.tg.last(tg_id)

    async def press(self, tg_id: int, label_part: str, *, message_id: int | None = None) -> Shown:
        mid = message_id if message_id is not None else self.tg.last(tg_id).message_id
        data = self.tg.shown(tg_id, mid).data(label_part)
        return await self.click(tg_id, data, message_id=mid)

    async def drain(self, **kw: Any) -> list[Any]:
        return await self.b.drain(**kw)

    async def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.raw(sql, *args)]


def catalog_with(squad: str, plan_ids: tuple[int, ...] = (1,), *, trial: bool = True) -> FakeCatalog:
    from svbg.catalog.service import build_snapshot

    plans: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    for pid in plan_ids:
        row, pr = owner_plan_rows(squad, plan_id=pid)
        plans.append(row)
        prices.extend(pr)
    if trial:
        row, _ = owner_plan_rows(squad, plan_id=7, is_trial=True, code="trial", device_addon={}, sort=99)
        plans.append(row)
    return FakeCatalog(build_snapshot(plans, prices, [], version=1))


DEFAULT_USER_CONFIG: dict[str, Any] = {
    "TRIAL_DAYS": 3,
    "TRIAL_AUDIENCE": "all",
    "REQUIRED_CHANNEL_ID": None,
    "SUPPORT_URL": "https://t.me/svbg_support",
    "DEFAULT_LANGUAGE": "ru",
    "TIMEZONE": "Europe/Moscow",
    "WALLET_TOPUP_PRESETS": [179, 499, 899],
    "CAPTCHA_ENABLED": False,  # the entry captcha has its own tests (test_user_captcha.py)
}


@asynccontextmanager
async def build_user_env(
    pg_dsn: str,
    *,
    config: Mapping[str, Any] | None = None,
    plans: tuple[int, ...] = (1,),
    seed_content: bool = True,
    with_fetch: bool = True,
) -> AsyncIterator[UserEnv]:
    attach_tables()
    try:
        async with build_billing_env(pg_dsn, config={**DEFAULT_USER_CONFIG, **dict(config or {})}) as b:
            cfg = b.config
            catalog = catalog_with(b.s.squad, plans)
            b.catalog.snapshot = catalog.snapshot
            content = ContentStore(b.db, seed=False)
            if seed_content:
                seeds = [s for s in defaults.SYSTEM_SCREENS if s.code != defaults.HOME]
                async with b.db.tx() as conn:
                    await seed_system_screens(conn, [*seeds, *USER_SCREENS])
            await content.load()
            tg = Tg()
            hub = FakeHub()
            directory = UserDirectory(b.db, Settings(cfg))
            router = ScreenRouter(
                transport=tg,
                user_loader=directory.load,
                ui_state=UiStateStore(b.db),
                content=content,
                codec=CallbackCodec(b.db, key=b"u" * 32),
                hub=hub,
                answer_deadline=2.0,
                handler_timeout=10.0,
            )
            lookup = Lookup()
            channel = ChannelService(b.db, config=lambda: cfg, lookup=lookup)
            trial = TrialService(b.db, CatalogTrialSource(b.catalog), config=lambda: cfg, channel=channel)
            fetch = Panel(b)
            deps = UserPathDeps(
                db=b.db,
                config=lambda: cfg,
                screens=router,
                users=directory,
                content=content,
                catalog=b.catalog,
                billing=b.billing,
                payments=b.pay.core,
                trial=trial,
                channel=channel,
                actions=SubscriptionActions(config=lambda: cfg),
                fetch_devices=fetch if with_fetch else None,
            )
            path = UserPath(deps)
            path.register()
            b.billing.fulfiller.messenger = path.messenger
            path.install(b.s.bus)
            base_handlers = BillingEnv.handlers

            def handlers() -> dict[str, Any]:
                from tests.subscriptions.hwid_jobs import DeviceJobs

                return {
                    **base_handlers(b),
                    **DeviceJobs(b.db, b.s.current_api, attention=b.s.attention).handlers(),
                    **path.handlers(),
                }

            b.handlers = handlers  # type: ignore[method-assign]
            yield UserEnv(b, tg, router, directory, path, cfg, content, lookup, fetch, hub)
    finally:
        detach_tables()
