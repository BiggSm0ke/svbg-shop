"""Settings screens test harness: real PostgreSQL 17 (the real ``svbg.db.engine.Database`` with the full
schema, see ``tests.dbkit``), the real settings service, a recording transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from svbg.content.store import ContentStore
from svbg.core.component import ComponentRegistry, HealthReport
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.registry import Registry, core_registry
from svbg.core.settings.service import SettingsService
from svbg.tg.admin.settings import SettingsScreens
from svbg.tg.ui.codec import CallbackCodec
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import ScreenRouter, UiStateStore
from tests.dbkit import CountingDatabase, add_user
from tests.tg.ui.ui_harness import FakeHub, FakeTransport, Users, callback, make_router, text_message


@dataclass
class FakeComponent:
    name: str
    probe_error: BaseException | None = None
    probe_delay: float = 0.0
    health_report: HealthReport = field(default_factory=HealthReport.ok)
    probes: list[dict[str, Any]] = field(default_factory=list)
    reconfigs: list[dict[str, Any]] = field(default_factory=list)

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        self.probes.append(dict(candidate))
        if self.probe_delay:
            await asyncio.sleep(self.probe_delay)
        if self.probe_error is not None:
            raise self.probe_error

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        self.reconfigs.append(dict(cfg))

    async def health(self) -> HealthReport:
        return self.health_report


@dataclass
class Denied:
    calls: list[tuple[int, str]] = field(default_factory=list)

    async def __call__(self, user: UserCtx, place: str) -> None:
        self.calls.append((user.user_id, place))


@dataclass
class SEnv:
    """Everything a settings-screen test needs."""

    db: CountingDatabase
    transport: FakeTransport
    hub: FakeHub
    users: Users
    router: ScreenRouter
    ui_state: UiStateStore
    service: SettingsService
    screens: SettingsScreens
    components: ComponentRegistry
    denied: Denied

    async def add(self, tg_id: int, role: str = "user", perms: frozenset[str] = frozenset()) -> UserCtx:
        uid = await add_user(self.db, tg_id, role)
        ctx = UserCtx(uid, telegram_id=tg_id, role=role, perms=perms)
        self.users.by_tg[tg_id] = ctx
        return ctx

    def component(self, name: str) -> FakeComponent:
        comp = self.components.get(name)
        assert isinstance(comp, FakeComponent)
        return comp

    # ---- driving the UI

    async def main_id(self, tg_id: int) -> int:
        state = await self.ui_state.get(self.users.by_tg[tg_id].user_id)
        return state.main_msg_id or 10

    async def click(self, tg_id: int, data: str) -> None:
        msg_id = await self.main_id(tg_id)
        await self.router.dispatch_callback(callback(tg_id, data, message_id=msg_id))

    async def press(self, tg_id: int, label: str) -> None:
        """Click the button of the last rendered screen whose text contains ``label``."""
        await self.click(tg_id, self.button(label))

    async def type(self, tg_id: int, text: str, *, message_id: int = 700) -> bool:
        return await self.router.dispatch_message(text_message(tg_id, text, message_id=message_id))

    # ---- inspecting what was rendered

    def rendered(self) -> list[SendMessage | EditMessageText]:
        return [c for c in self.transport.calls if isinstance(c, SendMessage | EditMessageText)]

    def last(self) -> SendMessage | EditMessageText:
        return self.rendered()[-1]

    @property
    def text(self) -> str:
        return self.last().text

    def buttons(self) -> list[InlineKeyboardButton]:
        markup = self.last().reply_markup
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

    def all_text(self) -> str:
        """Every text the bot sent or edited (for leak checks)."""
        out: list[str] = []
        for call in self.transport.calls:
            for attr in ("text", "caption"):
                value = getattr(call, attr, None)
                if isinstance(value, str):
                    out.append(value)
            markup = getattr(call, "reply_markup", None)
            if isinstance(markup, InlineKeyboardMarkup):
                out += [f"{b.text} {b.callback_data}" for row in markup.inline_keyboard for b in row]
        out += [a.text or "" for a in self.transport.answers]
        return "\n".join(out)


OWNER = 1001
ADMIN = 2002
ADMIN_NOPERM = 3003
SUPPORT = 4004
USER = 5005


async def add_staff(env: SEnv) -> dict[str, UserCtx]:
    return {
        "owner": await env.add(OWNER, "owner"),
        "admin": await env.add(ADMIN, "admin", frozenset({"settings.business"})),
        "admin_noperm": await env.add(ADMIN_NOPERM, "admin", frozenset({"plans"})),
        "support": await env.add(SUPPORT, "support"),
        "user": await env.add(USER, "user"),
    }


EnvFactory = Callable[..., Awaitable[SEnv]]


async def build_senv(
    db: CountingDatabase,
    env_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    registry: Registry | None = None,
    router_kw: Mapping[str, Any] | None = None,
    screens_kw: Mapping[str, Any] | None = None,
    transport: Any = None,
    user_loader: Any = None,
) -> SEnv:
    """``transport``/``user_loader`` replace the recording transport / dict loader (end-to-end tests)."""
    components = ComponentRegistry()
    for name in ("bot", "remnawave", "admin_chat"):
        components.register(FakeComponent(name))
    service = SettingsService(
        db,
        registry or core_registry(),
        Crypto([generate_key()]),
        components,
        environ=dict(environ or {}),
        env_path=env_path,
    )
    await service.load()
    hub, users = FakeHub(), Users()
    transport = transport if transport is not None else FakeTransport()
    content = ContentStore(db)
    await content.load()
    ui_state = UiStateStore(db)
    codec = CallbackCodec(db, key=b"k" * 32)
    denied = Denied()
    router = make_router(
        transport,
        user_loader or users,
        ui_state,
        content,
        codec,
        hub,
        on_denied=denied,
        **dict(router_kw or {}),
    )
    screens = SettingsScreens(router, service, **dict(screens_kw or {}))
    screens.install()
    return SEnv(db, transport, hub, users, router, ui_state, service, screens, components, denied)


LONG_KEY = "MODULE_" + "VERY_LONG_SETTING_NAME_" * 2 + "PADDED"  # 59 chars: needs short tokens


def extended_registry() -> Registry:
    """Core keys plus a bool, a free text, an advanced-only section key and a very long key name."""
    from svbg.core.settings.registry import Apply, SettingDef

    reg = core_registry()
    reg.add(SettingDef("FEATURE_FLAG", bool, False, "sales", "Тестовый флаг", "Включает тестовую функцию."))
    reg.add(SettingDef("NOTE_TEXT", str, "привет", "support", "Подпись", "Текст подписи под сообщениями."))
    reg.add(
        SettingDef(
            LONG_KEY,
            int,
            1,
            "modules",
            "Длинный ключ",
            "Ключ с очень длинным именем.",
            min=0,
            max=10,
        )
    )
    reg.add(
        SettingDef(
            "PANEL_LIMIT",
            int,
            5,
            "remnawave",
            "Лимит панели",
            "Бизнес-ключ, переподключающий компонент.",
            apply=Apply.RELOAD,
            component="remnawave",
        )
    )
    return reg
