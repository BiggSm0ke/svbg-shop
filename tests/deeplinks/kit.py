"""Test doubles for the deep-link service: promo/ads/referral modules, a catalog, a notifier."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from svbg.deeplinks.ports import AdRef, PromoInfo, PromoOutcome, PromoStatus
from svbg.deeplinks.service import Actor, DeeplinkService
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.router import UiStateStore
from tests.dbkit import CountingDatabase, add_user
from tests.tg.ui.ui_harness import FakeHub


@dataclass
class FakePromo:
    codes: dict[str, str] = field(default_factory=lambda: {"AUTUMN": "percent", "WEEK": "days"})
    calls: list[tuple[int, str]] = field(default_factory=list)
    fail: Exception | None = None
    hang: bool = False
    used: set[tuple[int, str]] = field(default_factory=set)

    async def lookup(self, code: str) -> PromoInfo | None:
        kind = self.codes.get(code)
        return None if kind is None else PromoInfo(id=len(code), code=code, kind=kind)

    async def apply_from_link(self, user_id: int, code: str) -> PromoOutcome:
        self.calls.append((user_id, code))
        if self.hang:
            await asyncio.sleep(10)
        if self.fail is not None:
            raise self.fail
        kind = self.codes.get(code)
        if kind is None or (user_id, code) in self.used:
            return PromoOutcome(PromoStatus.REFUSED)
        self.used.add((user_id, code))
        return PromoOutcome(PromoStatus.PENDING if kind == "percent" else PromoStatus.APPLIED)


@dataclass
class FakeAds:
    links: dict[str, int] = field(default_factory=lambda: {"summer2025": 11, "tiktok": 12, "p_test": 13})
    finds: list[str] = field(default_factory=list)
    attached: list[tuple[int, int, bool]] = field(default_factory=list)

    async def find(self, code: str) -> AdRef | None:
        self.finds.append(code)
        ad_id = self.links.get(code)
        return None if ad_id is None else AdRef(ad_id, code)

    async def attach(self, user_id: int, ad_link_id: int, *, is_new: bool) -> None:
        self.attached.append((user_id, ad_link_id, is_new))


@dataclass
class FakeReferral:
    known: set[str] = field(default_factory=lambda: {"abc123", "refA1b2C3d4", "Z9y8X7w6"})
    calls: list[tuple[int, str]] = field(default_factory=list)

    async def attach_referrer(self, user_id: int, code: str) -> bool:
        self.calls.append((user_id, code))
        return code in self.known


def plan(pid: int, code: str, **kw: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": pid,
        "code": code,
        "enabled": True,
        "is_trial": False,
        "broken_reason": None,
        "availability": "all",
        "name": {"ru": code.upper()},
    }
    values.update(kw)
    ns = SimpleNamespace(**values)
    ns.title = lambda lang=None: ns.name.get("ru", ns.code)
    return ns


class FakeCatalogSnapshot:
    def __init__(self, plans: list[SimpleNamespace]) -> None:
        self.plans = tuple(plans)

    def plan(self, plan_id: int | None) -> Any:
        return next((p for p in self.plans if p.id == plan_id), None)

    def by_code(self, code: str | None) -> Any:
        return next((p for p in self.plans if p.code == code), None)


class FakeCatalog:
    def __init__(self, plans: list[SimpleNamespace] | None = None) -> None:
        self.snapshot = FakeCatalogSnapshot(
            plans
            if plans is not None
            else [plan(1, "std"), plan(2, "vip", availability="link"), plan(3, "old", enabled=False)]
        )


@dataclass
class Sent:
    messages: list[tuple[int, str]] = field(default_factory=list)

    async def __call__(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@dataclass
class Kit:
    db: CountingDatabase
    service: DeeplinkService
    ui_state: UiStateStore
    promo: FakePromo
    ads: FakeAds
    referral: FakeReferral
    sent: Sent
    hub: FakeHub
    config: dict[str, Any]
    _actor: Actor | None = None

    async def user(self, tg_id: int, *, is_new: bool = True, role: str = "user") -> UserCtx:
        uid = await add_user(self.db, tg_id, role)
        return UserCtx(uid, telegram_id=tg_id, role=role, is_new=is_new)

    async def actor(self) -> Actor:
        """The owner as the actor of admin actions (created once)."""
        if self._actor is None:
            uid = await add_user(self.db, 1, "owner")
            self._actor = Actor(user_id=uid, role="owner")
        return self._actor

    async def hits(self) -> list[Mapping[str, Any]]:
        rows = await self.db.raw(
            "select link_id, payload, kind, user_id, is_new from deeplink_hits order by id"
        )
        return [dict(r) for r in rows]


def build_kit(
    db: CountingDatabase,
    *,
    can_open: Any = None,
    promo: bool = True,
    ads: bool = True,
    referral: bool = True,
    catalog: FakeCatalog | None = None,
) -> Kit:
    ui_state = UiStateStore(db)
    fake_promo, fake_ads, fake_ref = FakePromo(), FakeAds(), FakeReferral()
    sent, hub = Sent(), FakeHub()
    config: dict[str, Any] = {"CURRENCY": "RUB", "DEEPLINK_INTENT_TTL_HOURS": 24}
    service = DeeplinkService(
        db,
        ui_state=ui_state,
        config=lambda: config,
        catalog=catalog or FakeCatalog(),
        promo=fake_promo if promo else None,
        ads=fake_ads if ads else None,
        referral=fake_ref if referral else None,
        hub=hub,
        notify=sent,
        can_open=can_open if can_open is not None else (lambda user, code: code in {"buy", "bal", "faq"}),
        port_timeout=0.5,
    )
    return Kit(db, service, ui_state, fake_promo, fake_ads, fake_ref, sent, hub, config)
