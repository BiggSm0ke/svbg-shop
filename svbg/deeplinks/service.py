"""Deep links (07 §2.4.4): resolve ``/start <payload>``, record the hit, keep the intent through onboarding.

Flow of ``/start <payload>`` (:meth:`DeeplinkService.on_start`, called from the app's start hook):

1. :meth:`accept` — order of resolution (06 §2.7, R13): ``setup_`` (ignored: the owner router) → exact
   ``ad_links.code`` (old Bedolaga campaigns without a prefix) → ``ref…`` (old Bedolaga referral codes) →
   prefixes ``s_ p_ pr_ t_ r_ a_`` (+ long aliases) → ``l_<code>`` (a ``deeplinks`` row). The hit is
   written to ``deeplink_hits`` (``l_``: in the same transaction as ``uses + 1``, at most 2 statements); the
   ad tag and the referral code are attached right away (first touch; referral only for a new user);
2. the onboarding gate (required channel …) runs; when it shows its screen the actionable part of the
   intent (target, promo) is kept in ``ui_state.pending_intent`` with an expiry and
   :meth:`resume` executes it after the gate is passed;
3. otherwise :meth:`execute` runs it now: a promo by link is applied through the promo module (days or
   wallet now, a discount → ``pending_promo`` for checkout) and the user lands on the target screen.

No Telegram types here: screens are returned as ``(code, arg)``, user-facing lines as plain text (sent by
the ``notify`` callback at ``/start``, shown as a toast after a callback). Other modules are reached through
:mod:`svbg.deeplinks.ports`; each call has a timeout and a failure is captured, never propagated.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.content.defaults import HOME
from svbg.core import clock
from svbg.core.errors import Capturer
from svbg.core.money import exponent
from svbg.core.tables import admin_audit
from svbg.db.meta import JSONB
from svbg.deeplinks import codec
from svbg.deeplinks.codec import Kind
from svbg.deeplinks.model import Intent, IntentError
from svbg.deeplinks.ports import AdRef, AdsPort, PromoInfo, PromoOutcome, PromoPort, PromoStatus, ReferralPort
from svbg.deeplinks.tables import deeplink_daily, deeplink_hits, deeplinks

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.tg.ui.context import UserCtx

__all__ = [
    "INTENT_TTL_KEY",
    "SCREEN_BUY",
    "SCREEN_BUY_PLAN",
    "SCREEN_TOPUP",
    "TEXTS",
    "TEXTS_EN",
    "Accepted",
    "Actor",
    "CatalogLike",
    "DeeplinkService",
    "Landing",
    "LinkRow",
    "LinkStats",
    "StartGate",
    "describe",
]

log = logging.getLogger("svbg.deeplinks")

T = TypeVar("T")

#: Setting: how long an intent survives onboarding, hours (default 24).
INTENT_TTL_KEY: Final = "DEEPLINK_INTENT_TTL_HOURS"
DEFAULT_TTL_H: Final = 24
PORT_TIMEOUT: Final = 5.0
AD_CACHE_TTL: Final = 60.0
AD_CACHE_SIZE: Final = 2048
PLAN_GRANTS_SIZE: Final = 50_000
CREATE_ATTEMPTS: Final = 5
LIST_PAGE_MAX: Final = 50

# Screens of the user path (svbg.tg.user.seeds; repeated here to keep this module free of the UI layer).
SCREEN_BUY: Final = "buy"
SCREEN_BUY_PLAN: Final = "buy_plan"
SCREEN_TOPUP: Final = "topup"

TEXTS: Final = {
    "link_gone": "⏳ Эта ссылка больше не действует.",
    "plan_gone": "Этот тариф сейчас недоступен — вот что есть.",
    "screen_gone": "Раздел по ссылке недоступен — открыл меню.",
    "promo_applied": "🎟 Промокод {code} применён ✓",
    "promo_pending": "🎟 Промокод {code} применится при оплате ✓",
    "promo_refused": "🎟 Промокод {code} не подошёл.",
    "promo_unavailable": "🎟 Промокоды сейчас недоступны.",
}
#: English of :data:`TEXTS` (same keys and placeholders).
TEXTS_EN: Final = {
    "link_gone": "⏳ This link is no longer valid.",
    "plan_gone": "This plan is not available right now — here is what we have.",
    "screen_gone": "The section from the link is not available — opened the menu.",
    "promo_applied": "🎟 Promo code {code} applied ✓",
    "promo_pending": "🎟 Promo code {code} will apply at checkout ✓",
    "promo_refused": "🎟 Promo code {code} did not fit.",
    "promo_unavailable": "🎟 Promo codes are not available right now.",
}


def _text(key: str, lang: str | None) -> str:
    """``TEXTS[key]`` in ``lang`` (Russian fallback)."""
    return TEXTS_EN[key] if lang == "en" else TEXTS[key]


#: ``(user, chat_id, None)`` → the onboarding screen to show instead (channel gate …) or ``None``.
StartGate = Callable[["UserCtx", int, Any], Awaitable[tuple[str, Any] | None]]
Notify = Callable[[int, str], Awaitable[Any]]
CanOpen = Callable[["UserCtx", str], bool]


class PendingStore(Protocol):
    """``svbg.tg.ui.router.UiStateStore`` (write-through cached ``ui_state``)."""

    async def get(self, user_id: int) -> Any: ...

    async def set_pending_intent(self, user_id: int, intent: dict[str, Any] | None) -> None: ...


class _CatalogSnapshot(Protocol):
    def plan(self, plan_id: int | None) -> Any: ...

    def by_code(self, code: str | None) -> Any: ...


class CatalogLike(Protocol):
    @property
    def snapshot(self) -> _CatalogSnapshot: ...


# ---------------------------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class Landing:
    """Where the user goes after an intent ran (``screen`` + ``arg``) and a line to tell them."""

    screen: str
    arg: Any = None
    notice: str | None = None


@dataclass(frozen=True, slots=True)
class Accepted:
    intent: Intent | None = None
    notice: str | None = None
    kind: str | None = None  # the hit kind, ``None`` when nothing was recorded


@dataclass(frozen=True, slots=True)
class LinkRow:
    id: int
    code: str
    title: str
    intent: Intent
    promo_id: int | None
    ad_link_id: int | None
    expires_at: datetime | None
    max_uses: int | None
    uses: int
    enabled: bool
    created_by: int | None
    created_at: datetime

    @property
    def payload(self) -> str:
        return "l_" + self.code

    def unusable(self, now: datetime, *, seen: bool = False) -> str | None:
        """Why a tap cannot use the link (``None`` when it can); a returning user is not a new use."""
        if not self.enabled:
            return "disabled"
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        if not seen and self.max_uses is not None and self.uses >= self.max_uses:
            return "used_up"
        return None


@dataclass(frozen=True, slots=True)
class LinkStats:
    """Hits / distinct users / new users over the last 1, 7 and 30 days."""

    hits: tuple[int, int, int] = (0, 0, 0)
    users: tuple[int, int, int] = (0, 0, 0)
    new_users: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who changes links (``admin_audit``)."""

    user_id: int
    role: str


def _audit_insert(
    actor: Actor,
    action: str,
    target: str,
    details: Mapping[str, Any],
    *,
    exists: Any = None,
) -> Any:
    """``INSERT INTO admin_audit`` for a CTE; with ``exists`` only when that condition holds."""
    source = sa.select(
        sa.literal(actor.user_id, sa.BigInteger),
        sa.literal(actor.role, sa.Text),
        sa.literal(action, sa.Text),
        sa.literal(target[:200], sa.Text),
        sa.literal(dict(details), JSONB),
    )
    if exists is not None:
        source = source.where(exists)
    a = admin_audit.c
    return sa.insert(admin_audit).from_select([a.actor_id, a.role, a.action, a.target, a.details], source)


def _row_to_link(row: Mapping[str, Any]) -> LinkRow:
    try:
        intent = Intent.from_spec(row["intent"])
    except IntentError:
        intent = Intent()
    return LinkRow(
        id=int(row["id"]),
        code=str(row["code"]),
        title=str(row["title"]),
        intent=intent,
        promo_id=row["promo_id"],
        ad_link_id=row["ad_link_id"],
        expires_at=row["expires_at"],
        max_uses=row["max_uses"],
        uses=int(row["uses"]),
        enabled=bool(row["enabled"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------------------------- service


class DeeplinkService:
    def __init__(
        self,
        db: Database,
        *,
        ui_state: PendingStore,
        config: Callable[[], Mapping[str, Any]] | None = None,
        catalog: CatalogLike | None = None,
        promo: PromoPort | None = None,
        ads: AdsPort | None = None,
        referral: ReferralPort | None = None,
        hub: Capturer | None = None,
        notify: Notify | None = None,
        can_open: CanOpen | None = None,
        port_timeout: float = PORT_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if port_timeout <= 0:
            raise ValueError("port_timeout must be positive")
        self.db = db
        self.ui_state = ui_state
        self._config = config
        self.catalog = catalog
        self.promo = promo
        self.ads = ads
        self.referral = referral
        self.hub = hub
        self.notify = notify
        self.can_open = can_open
        self.port_timeout = port_timeout
        self._monotonic = monotonic
        self._ad_cache: OrderedDict[str, tuple[float, AdRef | None]] = OrderedDict()
        self._plan_grants: OrderedDict[int, tuple[str, datetime]] = OrderedDict()

    # ------------------------------------------------------------------ settings

    def setting(self, key: str, default: T) -> T:
        if self._config is None:
            return default
        try:
            value = self._config()[key]
        except (KeyError, RuntimeError, TypeError):
            return default
        return default if value is None or type(value) is not type(default) else value

    def intent_ttl(self) -> timedelta:
        hours = self.setting(INTENT_TTL_KEY, DEFAULT_TTL_H)
        return timedelta(hours=min(max(hours, 1), 24 * 30))

    def currency(self) -> str:
        return self.setting("CURRENCY", "RUB") or "RUB"

    # ------------------------------------------------------------------ isolation

    async def _port(self, place: str, call: Awaitable[T], *, user_id: int | None = None) -> T | None:
        """Run a call into another module with a timeout; any failure is captured and becomes ``None``."""
        try:
            async with asyncio.timeout(self.port_timeout):
                return await call
        except Exception as exc:  # noqa: BLE001 - isolation boundary: /start must work without the module
            log.warning("deeplinks: %s failed: %s", place, type(exc).__name__)
            if self.hub is not None:
                try:
                    await self.hub.capture(
                        exc, f"deeplinks:{place}", module="deeplinks", user_id=user_id, handled="пропущено"
                    )
                except Exception:
                    log.exception("error hub failed")
            return None

    # ------------------------------------------------------------------ /start

    async def on_start(
        self, user: UserCtx, chat_id: int, link: Any, *, gate: StartGate | None = None
    ) -> tuple[str, Any] | None:
        """The ``/start`` hook: the screen to open instead of the home screen, or ``None``.

        ``link`` is the stage-2 ``DeepLink`` (its ``raw``), a payload string or ``None``. ``gate`` is the user
        path's own start hook (required channel …) called with ``link=None``: the intent is kept here.
        """
        payload = link if isinstance(link, str) else getattr(link, "raw", None)
        accepted = Accepted()
        if isinstance(payload, str) and payload:
            accepted = await self.accept(user, payload)
        intent = accepted.intent if accepted.intent is not None and accepted.intent.actionable else None
        picked = await gate(user, chat_id, None) if gate is not None else None
        if picked is not None:
            if intent is not None:
                await self._keep(user.user_id, intent)
            if accepted.notice:
                await self._notify(chat_id, accepted.notice)
            return picked
        if intent is None:
            intent = await self._take(user.user_id)  # a link that waited behind the gate
            if accepted.notice:
                await self._notify(chat_id, accepted.notice)
            if intent is None:
                return None
        else:
            await self._drop(user.user_id)  # the new link replaces an older pending one
        landing = await self.execute(user, intent)
        if landing.notice:
            await self._notify(chat_id, landing.notice)
        return None if landing.screen == HOME and landing.arg is None else (landing.screen, landing.arg)

    def start_hook(self, gate: StartGate | None = None) -> Callable[[UserCtx, int, Any], Awaitable[Any]]:
        """``build_start_router(on_start=service.start_hook(path.on_start))``."""

        async def hook(user: UserCtx, chat_id: int, link: Any) -> tuple[str, Any] | None:
            return await self.on_start(user, chat_id, link, gate=gate)

        return hook

    async def resume(self, user: UserCtx) -> Landing | None:
        """Run the intent kept through onboarding (after the channel check, the language, the consent)."""
        intent = await self._take(user.user_id)
        if intent is None:
            return None
        return await self.execute(user, intent)

    async def _notify(self, chat_id: int, text: str) -> None:
        if self.notify is not None:
            await self._port("notify", self.notify(chat_id, text))

    # ------------------------------------------------------------------ pending intent

    async def _keep(self, user_id: int, intent: Intent) -> None:
        pending = intent.with_meta(expires_at=clock.now() + self.intent_ttl())
        try:
            await self.ui_state.set_pending_intent(user_id, pending.to_pending())
        except Exception:  # noqa: BLE001 - the intent is a convenience; /start must work without it
            log.warning("could not keep the deep link intent of user %s", user_id)

    async def _take(self, user_id: int) -> Intent | None:
        """Pop the pending intent (no SQL when the cached state has none); expired → dropped."""
        try:
            state = await self.ui_state.get(user_id)
            raw = getattr(state, "pending_intent", None)
            if raw is None:
                return None
            await self.ui_state.set_pending_intent(user_id, None)
        except Exception:  # noqa: BLE001 - see _keep
            log.warning("could not read the deep link intent of user %s", user_id)
            return None
        intent = Intent.from_pending(raw)
        if intent is None or intent.expired(clock.now()):
            return None
        return intent

    async def _drop(self, user_id: int) -> None:
        try:
            state = await self.ui_state.get(user_id)
            if getattr(state, "pending_intent", None) is not None:
                await self.ui_state.set_pending_intent(user_id, None)
        except Exception:  # noqa: BLE001 - see _keep
            log.warning("could not drop the deep link intent of user %s", user_id)

    # ------------------------------------------------------------------ resolution

    async def accept(self, user: UserCtx, payload: str) -> Accepted:
        """Resolve a payload, record the hit and attach the ad tag / referrer. Never raises."""
        try:
            return await self._accept(user, payload.strip())
        except Exception as exc:  # noqa: BLE001 - isolation boundary: a broken link must not break /start
            log.warning("deeplinks: accepting %r failed: %s", payload[:64], type(exc).__name__)
            if self.hub is not None:
                await self.hub.capture(
                    exc,
                    "deeplinks:accept",
                    module="deeplinks",
                    user_id=user.user_id,
                    handled="ссылка пропущена",
                )
            return Accepted()

    async def _accept(self, user: UserCtx, raw: str) -> Accepted:
        if not codec.is_payload(raw) or raw.startswith("setup_"):
            return Accepted()
        # Old campaign codes are matched as is, before any prefix (07 §2.4.4, R13): "t_tiktok" or "p_VK2024"
        # look like prefixes with an invalid value, yet they are valid Bedolaga campaign codes.
        ad = await self.find_ad(raw)
        if ad is not None:
            await self._attach_ad(user, ad)
            await self._record(user, raw, "ad_code")
            return Accepted(Intent(ad=ad.code, source=raw), kind="ad_code")
        parsed = codec.parse(raw)
        if parsed is None or parsed.kind is Kind.SETUP:
            return Accepted()
        match parsed.kind:
            case Kind.LINK:
                return await self._accept_link(user, parsed.value, raw)
            case Kind.BARE:
                return Accepted()
            case Kind.AD:
                found = await self.find_ad(parsed.value)
                if found is not None:
                    await self._attach_ad(user, found)
            case Kind.REF | Kind.LEGACY_REF:
                await self._attach_ref(user, parsed)
            case _:
                pass
        intent = Intent.from_parsed(parsed)
        await self._record(user, raw, parsed.kind.value)
        return Accepted(intent, kind=parsed.kind.value)

    async def _accept_link(self, user: UserCtx, code: str, raw: str) -> Accepted:
        now = clock.now()
        async with self.db.tx() as conn:
            seen = (
                sa.select(deeplink_hits.c.id)
                .where(deeplink_hits.c.link_id == deeplinks.c.id, deeplink_hits.c.user_id == user.user_id)
                .limit(1)
                .exists()
            )
            row = (
                (
                    await conn.execute(
                        sa.select(deeplinks, seen.label("seen"))
                        .where(deeplinks.c.code == code)
                        .with_for_update(of=deeplinks)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return Accepted()
            link = _row_to_link(row)
            first = not row["seen"]
            if link.unusable(now, seen=not first) is not None:
                return Accepted(notice=_text("link_gone", user.lang))
            hit = sa.insert(deeplink_hits).values(
                link_id=link.id, payload=raw, kind="link", user_id=user.user_id, is_new=user.is_new, ts=now
            )
            if first:
                bump = (
                    sa.update(deeplinks)
                    .where(deeplinks.c.id == link.id)
                    .values(uses=deeplinks.c.uses + 1)
                    .returning(deeplinks.c.id)
                    .cte("bump")
                )
                hit = hit.add_cte(bump)
            await conn.execute(hit)
        intent = link.intent.with_meta(link_id=link.id, source=raw)
        if intent.ad is not None:
            ad = await self.find_ad(intent.ad)
            if ad is not None:
                await self._attach_ad(user, ad)
        if intent.ref is not None:
            await self._attach_ref(user, codec.Parsed(Kind.REF, intent.ref, raw))
        return Accepted(intent, kind="link")

    async def _record(self, user: UserCtx, raw: str, kind: str) -> None:
        async with self.db.tx() as conn:
            await conn.execute(
                sa.insert(deeplink_hits).values(
                    link_id=None,
                    payload=raw,
                    kind=kind,
                    user_id=user.user_id,
                    is_new=user.is_new,
                    ts=clock.now(),
                )
            )

    async def find_ad(self, code: str) -> AdRef | None:
        """Exact ``ad_links.code`` match through the ads module, cached for a minute (also misses)."""
        if self.ads is None or not code:
            return None
        now = self._monotonic()
        cached = self._ad_cache.get(code)
        if cached is not None and cached[0] > now:
            self._ad_cache.move_to_end(code)
            return cached[1]
        found = await self._port("ads.find", self.ads.find(code))
        ad = found if isinstance(found, AdRef) and found.active else None
        self._ad_cache[code] = (now + AD_CACHE_TTL, ad)
        self._ad_cache.move_to_end(code)
        while len(self._ad_cache) > AD_CACHE_SIZE:
            self._ad_cache.popitem(last=False)
        return ad

    def forget_ads(self) -> None:
        """Drop the ad cache (the ads module calls it after an edit)."""
        self._ad_cache.clear()

    async def _attach_ad(self, user: UserCtx, ad: AdRef) -> None:
        if self.ads is not None:
            await self._port(
                "ads.attach", self.ads.attach(user.user_id, ad.id, is_new=user.is_new), user_id=user.user_id
            )

    async def _attach_ref(self, user: UserCtx, parsed: codec.Parsed) -> bool:
        """Referral codes bind only a user who has just registered (no retro-binding, 05 §2.3)."""
        if self.referral is None or not user.is_new:
            return False
        codes = [parsed.value]
        if parsed.kind is Kind.LEGACY_REF:  # Bedolaga: the code may be stored with or without "ref"
            codes = [parsed.raw, parsed.value]
        for code in codes:
            done = await self._port(
                "referral.attach", self.referral.attach_referrer(user.user_id, code), user_id=user.user_id
            )
            if done:
                return True
        return False

    # ------------------------------------------------------------------ execution

    async def execute(self, user: UserCtx, intent: Intent) -> Landing:
        """Apply the promo of ``intent`` and pick the landing screen. Never raises."""
        notice: str | None = None
        if intent.promo is not None:
            notice = await self._apply_promo(user, intent.promo)
        landing = self._target(user, intent)
        if notice and landing.notice:
            notice = f"{notice}\n{landing.notice}"
        return replace(landing, notice=notice or landing.notice)

    async def _apply_promo(self, user: UserCtx, code: str) -> str:
        if self.promo is None:
            log.info("promo link %s ignored: the promo module is not connected", code)
            return _text("promo_unavailable", user.lang)
        outcome = await self._port(
            "promo.apply", self.promo.apply_from_link(user.user_id, code), user_id=user.user_id
        )
        if not isinstance(outcome, PromoOutcome):
            return _text("promo_refused", user.lang).format(code=code)
        if outcome.text and (user.lang != "en" or not outcome.text_en):
            return outcome.text
        if outcome.text_en:
            return outcome.text_en
        key = {
            PromoStatus.APPLIED: "promo_applied",
            PromoStatus.PENDING: "promo_pending",
        }.get(outcome.status, "promo_refused")
        return _text(key, user.lang).format(code=code)

    def _target(self, user: UserCtx, intent: Intent) -> Landing:
        if intent.screen is not None:
            if self.can_open is not None and self._safe_can_open(user, intent.screen):
                return Landing(intent.screen)
            return Landing(HOME, notice=_text("screen_gone", user.lang))
        if intent.plan is not None:
            return self._plan_landing(user, intent.plan, via_short_link=intent.link_id is not None)
        if intent.topup is not None:
            try:
                minor = intent.topup * 10 ** exponent(self.currency())
            except (KeyError, ValueError):
                minor = intent.topup * 100
            return Landing(SCREEN_TOPUP, str(minor))
        return Landing(HOME)

    def _safe_can_open(self, user: UserCtx, screen: str) -> bool:
        assert self.can_open is not None
        try:
            return bool(self.can_open(user, screen))
        except Exception:
            log.exception("can_open(%s) failed", screen)
            return False

    def _find_plan(self, value: str) -> Any:
        if self.catalog is None:
            return None
        snap = self.catalog.snapshot
        plan = snap.by_code(value)
        if plan is None and value.isdigit() and len(value) <= 12:
            plan = snap.plan(int(value))
        return plan

    def _plan_landing(self, user: UserCtx, value: str, *, via_short_link: bool = False) -> Landing:
        """The plan screen; a link-only plan is granted only by its exact code or by a short link.

        A numeric id (``p_7``) never opens a link-only plan: ids are sequential, so ``p_1``, ``p_2``, … would
        reveal hidden plans to anyone. Short links (``l_``) carry random codes and were built by an admin.
        """
        plan = self._find_plan(value)
        if (
            plan is None
            or not getattr(plan, "enabled", False)
            or getattr(plan, "is_trial", False)
            or getattr(plan, "broken_reason", None) is not None
        ):
            return Landing(SCREEN_BUY, notice=_text("plan_gone", user.lang))
        if getattr(plan, "availability", "all") == "link":
            if not via_short_link and value != str(plan.code):
                return Landing(SCREEN_BUY, notice=_text("plan_gone", user.lang))
            self._grant_plan(user.user_id, str(plan.code))
        return Landing(SCREEN_BUY_PLAN, str(plan.id))

    def _grant_plan(self, user_id: int, code: str) -> None:
        self._plan_grants[user_id] = (code, clock.now() + self.intent_ttl())
        self._plan_grants.move_to_end(user_id)
        while len(self._plan_grants) > PLAN_GRANTS_SIZE:
            self._plan_grants.popitem(last=False)

    def granted_plan_code(self, user_id: int) -> str | None:
        """The code of a link-only plan this user opened through its link (``link_code`` for the catalog's
        availability rule); in memory, no SQL, valid for the intent TTL."""
        grant = self._plan_grants.get(user_id)
        if grant is None:
            return None
        if grant[1] <= clock.now():
            del self._plan_grants[user_id]
            return None
        return grant[0]

    # ------------------------------------------------------------------ short links (admin)

    async def check_spec(self, spec: Intent) -> list[str]:
        """Problems that make a spec useless (Russian lines for the link builder); empty when fine."""
        problems: list[str] = []
        if spec.empty:
            problems.append("Выберите цель или добавьте промокод")
        if spec.plan is not None and self.catalog is not None and self._find_plan(spec.plan) is None:
            problems.append(f"Тарифа «{spec.plan}» нет")
        if spec.promo is not None:
            if self.promo is None:
                problems.append("Модуль промокодов не подключён — промокод не сработает")
            else:
                info = await self.lookup_promo(spec.promo)
                if info is None:
                    problems.append(f"Промокода «{spec.promo}» нет")
                elif not info.active:
                    problems.append(f"Промокод «{spec.promo}» выключен или истёк")
        if spec.ad is not None and self.ads is not None:
            self._ad_cache.pop(spec.ad, None)
            if await self.find_ad(spec.ad) is None:
                problems.append(f"Рекламной метки «{spec.ad}» нет")
        return problems

    async def lookup_promo(self, code: str) -> PromoInfo | None:
        if self.promo is None:
            return None
        found = await self._port("promo.lookup", self.promo.lookup(code))
        return found if isinstance(found, PromoInfo) else None

    async def create_link(
        self,
        spec: Intent,
        *,
        title: str,
        actor: Actor | None,
        expires_at: datetime | None = None,
        max_uses: int | None = None,
        code: str | None = None,
    ) -> LinkRow:
        """Insert a short link (a fresh random code unless ``code`` is given) and its ``admin_audit`` row in
        one statement. ``ValueError`` (Russian) when invalid or when ``code`` is taken."""
        title = " ".join(title.split())
        if not 1 <= len(title) <= 64:
            raise ValueError("Название — от 1 до 64 символов")
        if spec.empty:
            raise ValueError("Выберите цель или добавьте промокод")
        if max_uses is not None and max_uses <= 0:
            raise ValueError("Лимит — целое число больше нуля")
        if code is not None and not codec.valid_value(Kind.LINK, code):
            raise ValueError("Код: латиница, цифры, «_» и «-», до 62 символов")
        promo_id = ad_id = None
        if spec.promo is not None:
            info = await self.lookup_promo(spec.promo)
            promo_id = info.id if info is not None else None
        if spec.ad is not None:
            ad = await self.find_ad(spec.ad)
            ad_id = ad.id if ad is not None else None
        stored = Intent.from_spec(spec.to_spec()).to_spec()
        values = {
            "title": title,
            "intent": stored,
            "promo_id": promo_id,
            "ad_link_id": ad_id,
            "expires_at": expires_at,
            "max_uses": max_uses,
            "created_by": actor.user_id if actor is not None else None,
        }
        for _ in range(1 if code is not None else CREATE_ATTEMPTS):
            link_code = code or codec.new_link_code()
            stmt = sa.insert(deeplinks).values(code=link_code, **values).returning(*deeplinks.c)
            if actor is not None:
                audit = _audit_insert(actor, "deeplink.create", "l_" + link_code, {"title": title, **stored})
                stmt = stmt.add_cte(audit.cte("audit"))
            try:
                async with self.db.tx() as conn:
                    row = (await conn.execute(stmt)).mappings().one()
                return _row_to_link(row)
            except IntegrityError as exc:
                if "uq_deeplinks_code" not in str(exc.orig):
                    raise
                if code is not None:
                    raise ValueError("Такой код уже занят") from exc
        raise RuntimeError("could not find a free link code")  # pragma: no cover - 62^8 codes

    async def get_by_code(self, code: str) -> LinkRow | None:
        async with self.db.read() as conn:
            result = await conn.execute(sa.select(deeplinks).where(deeplinks.c.code == code))
            row = result.mappings().first()
        return None if row is None else _row_to_link(row)

    async def get_link(self, link_id: int) -> LinkRow | None:
        async with self.db.read() as conn:
            result = await conn.execute(sa.select(deeplinks).where(deeplinks.c.id == link_id))
            row = result.mappings().first()
        return None if row is None else _row_to_link(row)

    async def list_links(self, *, offset: int = 0, limit: int = 10) -> tuple[list[LinkRow], int]:
        """Newest first, with the total count (one statement)."""
        limit = min(max(limit, 1), LIST_PAGE_MAX)
        total = sa.func.count().over().label("total")
        async with self.db.read() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(deeplinks, total)
                        .order_by(deeplinks.c.id.desc())
                        .offset(max(offset, 0))
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        if not rows and offset > 0:
            return await self.list_links(offset=0, limit=limit)
        return [_row_to_link(r) for r in rows], int(rows[0]["total"]) if rows else 0

    async def set_enabled(self, link_id: int, enabled: bool, *, actor: Actor | None) -> LinkRow | None:
        """Turn a link on/off (links are never deleted: the hit history stays) + ``admin_audit``, one
        statement."""
        stmt = (
            sa.update(deeplinks)
            .where(deeplinks.c.id == link_id)
            .values(enabled=enabled, updated_at=sa.func.now())
            .returning(*deeplinks.c)
        )
        if actor is not None:
            action = "deeplink.enable" if enabled else "deeplink.disable"
            audit = _audit_insert(
                actor, action, f"deeplink:{link_id}", {}, exists=sa.exists().where(deeplinks.c.id == link_id)
            )
            stmt = stmt.add_cte(audit.cte("audit"))
        async with self.db.tx() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        return None if row is None else _row_to_link(row)

    async def stats(self, link_id: int) -> LinkStats:
        """Counts for the last 1/7/30 days in one statement."""
        now = clock.now()
        spans = [now - timedelta(days=d) for d in (1, 7, 30)]
        h = deeplink_hits.c
        cols: list[Any] = []
        for i, since in enumerate(spans):
            cond = h.ts >= since
            cols += [
                sa.func.count().filter(cond).label(f"h{i}"),
                sa.func.count(sa.distinct(h.user_id)).filter(cond).label(f"u{i}"),
                sa.func.count(sa.distinct(h.user_id)).filter(sa.and_(cond, h.is_new)).label(f"n{i}"),
            ]
        async with self.db.read() as conn:
            row = (
                (await conn.execute(sa.select(*cols).where(h.link_id == link_id, h.ts >= spans[-1])))
                .mappings()
                .one()
            )
        return LinkStats(
            hits=(row["h0"], row["h1"], row["h2"]),
            users=(row["u0"], row["u1"], row["u2"]),
            new_users=(row["n0"], row["n1"], row["n2"]),
        )

    # ------------------------------------------------------------------ daily aggregates

    async def aggregate_day(self, day: date) -> int:
        """(Re)compute ``deeplink_daily`` for one UTC day from ``deeplink_hits``; returns the row count."""
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        h = deeplink_hits.c
        key = sa.case((h.link_id.is_(None), h.payload), else_=sa.literal("l:") + sa.cast(h.link_id, sa.Text))
        users = sa.func.count(sa.distinct(h.user_id))
        source = (
            sa.select(
                sa.literal(day, sa.Date).label("day"),
                key.label("link_key"),
                sa.func.max(h.link_id).label("link_id"),
                sa.func.count().label("hits"),
                users.label("users"),
                sa.func.count(sa.distinct(h.user_id)).filter(h.is_new).label("new_users"),
            )
            .where(h.ts >= start, h.ts < end)
            .group_by(key)
        )
        stmt = pg_insert(deeplink_daily).from_select(
            ["day", "link_key", "link_id", "hits", "users", "new_users"], source
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[deeplink_daily.c.day, deeplink_daily.c.link_key],
            set_={
                "link_id": stmt.excluded.link_id,
                "hits": stmt.excluded.hits,
                "users": stmt.excluded.users,
                "new_users": stmt.excluded.new_users,
            },
        )
        async with self.db.tx() as conn:
            result = await conn.execute(stmt)
        return int(result.rowcount or 0)

    async def aggregate_recent(self) -> None:
        """Yesterday and today (UTC): the scheduled job (cheap and idempotent)."""
        today = clock.now().astimezone(UTC).date()
        for day in (today - timedelta(days=1), today):
            await self.aggregate_day(day)

    def schedule(self, scheduler: Any) -> None:
        """``scheduler.every("deeplinks.daily", 1 h)`` → :meth:`aggregate_recent`."""
        scheduler.every("deeplinks.daily", 3600, self.aggregate_recent, jitter_s=300)


def describe(intent: Intent, *, plan_title: Callable[[str], str | None] | None = None) -> Sequence[str]:
    """Human lines for a spec («Цель: тариф «Стандарт»», «Промокод: AUTUMN» …) — the admin card."""
    lines: list[str] = []
    if intent.screen is not None:
        lines.append(f"Цель: экран «{intent.screen}»")
    elif intent.plan is not None:
        title = plan_title(intent.plan) if plan_title is not None else None
        lines.append(f"Цель: тариф «{title or intent.plan}»")
    elif intent.topup is not None:
        lines.append(f"Цель: пополнение на {intent.topup}")
    else:
        lines.append("Цель: главное меню")
    if intent.promo is not None:
        lines.append(f"Промокод: {intent.promo}")
    if intent.ad is not None:
        lines.append(f"Метка: {intent.ad}")
    if intent.ref is not None:
        lines.append(f"Реферальный код: {intent.ref}")
    return lines
