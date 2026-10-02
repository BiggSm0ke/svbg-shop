"""Stage 3–4 parts of the composition root (:mod:`svbg.app`): module settings, ports and small screens.

What lives here (the app itself only calls these):

* :func:`module_settings` / :func:`full_registry` — the core registry plus the keys of the ops module, the
  referral program, the shadow mode and the owner modules (LTE, IP Guard: their manifests, X12). The CLI uses
  :func:`full_registry` too, so ``svbg set`` / ``svbg env render`` know every key the bot knows;
* :class:`PromoPortAdapter`, :class:`AdsPortAdapter` — the deep-link ports over the promo and ads services;
* :class:`ReferralScreens` — «🤝 Пригласить» (screen ``invite``, content button ``system:referral``);
* :func:`media_limits` — :class:`~svbg.content.media.MediaLimits` from the live settings (``MEDIA_PHOTO_*``).

Every function is defensive about keys the registry may not know yet: a missing key reads as its default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from aiogram.types import CopyTextButton, InlineKeyboardButton

from svbg.core.settings import Registry, core_registry
from svbg.core.settings.registry import SettingDef
from svbg.deeplinks.ports import AdRef, PromoInfo, PromoOutcome, PromoStatus
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, View

if TYPE_CHECKING:
    from svbg.ads.service import AdService
    from svbg.content.media import MediaLimits, PublicMedia
    from svbg.ext.api import ExtensionHost
    from svbg.promo.service import PromoService
    from svbg.referral.service import ReferralService
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "EXT_SPEC_PATHS",
    "INVITE_SCREEN",
    "SHADOW_ENABLED_KEY",
    "SHADOW_SOURCE_KEY",
    "AdsPortAdapter",
    "PromoPortAdapter",
    "ReferralScreens",
    "cfg",
    "full_registry",
    "load_extensions",
    "media_limits",
    "module_settings",
]

log = logging.getLogger("svbg.app")

#: Owner modules with an extension manifest (``SPEC``). Referral has no manifest: it is core (05 §2.3).
EXT_SPEC_PATHS: Final[tuple[str, ...]] = ("svbg.ext.lte.service", "svbg.ext.ip_guard")

SHADOW_ENABLED_KEY: Final = "IMPORT_SHADOW_ENABLED"
SHADOW_SOURCE_KEY: Final = "IMPORT_SOURCE_DSN"

#: Shadow mode of the Bedolaga migration (06 §4.2): off by default; the stand turns it on with the DSN of a
#: fresh copy of the Bedolaga database. Applied without a restart (read at every daily pass).
SHADOW_SETTINGS: Final[tuple[SettingDef, ...]] = (
    SettingDef(
        SHADOW_ENABLED_KEY,
        bool,
        False,
        "system",
        "Shadow-сверка с Bedolaga",
        "Раз в сутки (06:00) импорт копии БД Bedolaga в режиме shadow и отчёт С1–С12 в тему «Система». "
        "Включать только на стенде переезда; в панель ничего не пишется.",
        owner_only=True,
        advanced=True,
        tags=("bedolaga", "миграция", "переезд", "shadow"),
    ),
    SettingDef(
        SHADOW_SOURCE_KEY,
        "secret",
        None,
        "system",
        "Адрес копии БД Bedolaga",
        "postgresql://… свежей копии базы Bedolaga для shadow-сверки (только чтение).",
        nullable=True,
        owner_only=True,
        advanced=True,
        tags=("bedolaga", "dsn", "миграция"),
        hint="postgresql://user:pass@host:5432/bedolaga_copy",
    ),
)


def cfg(config: Callable[[], Mapping[str, Any]] | None, key: str, default: Any) -> Any:
    """``config()[key]`` or ``default`` (unknown key, empty value, settings not loaded)."""
    if config is None:
        return default
    try:
        value = config()[key]
    except (KeyError, RuntimeError, TypeError, AttributeError):
        return default
    return default if value is None else value


# ------------------------------------------------------------------------------------------ settings


def module_settings(registry: Registry) -> dict[str, str]:
    """Add the keys of ops, referral and the shadow mode (skipping keys already there). Returns problems."""
    problems: dict[str, str] = {}

    def add_all(name: str, defs: Any) -> None:
        try:
            for defn in defs():
                if defn.key not in registry:
                    registry.add(defn)
        except Exception as exc:
            problems[name] = f"настройки не зарегистрированы ({type(exc).__name__})"
            log.exception("settings of %s are not registered", name)

    def ops() -> Any:
        from svbg.ops.settings import OPS_SETTINGS

        return OPS_SETTINGS

    def referral() -> Any:
        from svbg.referral.config import SETTINGS

        return SETTINGS

    add_all("svbg.ops", ops)
    add_all("svbg.referral", referral)
    add_all("svbg.importers.shadow", lambda: SHADOW_SETTINGS)
    return problems


def load_extensions(registry: Registry) -> tuple[ExtensionHost | None, dict[str, str]]:
    """The extension host over :data:`EXT_SPEC_PATHS` with its settings installed into ``registry`` (X12).

    A manifest that does not import is reported (``errs``); the other modules still load. Settings are added
    once: a registry that already has a module's switch keeps its own declarations.
    """
    try:
        from svbg.ext.api import ExtensionHost, load_specs

        specs, errs = load_specs(EXT_SPEC_PATHS)
        host = ExtensionHost(specs)
        if all(spec.enabled_key is None or spec.enabled_key not in registry for spec in specs):
            host.install_settings(registry)
    except Exception as exc:
        log.exception("extension host is not available")
        return None, {"svbg.ext": f"ошибка при подключении ({type(exc).__name__})"}
    return host, {f"ext:{k}": v for k, v in errs.items()}


def full_registry() -> Registry:
    """The registry the running bot uses: core + module settings (CLI: ``svbg set``, ``env render``)."""
    registry = core_registry()
    module_settings(registry)
    load_extensions(registry)
    return registry


def public_media_url(public: PublicMedia) -> Callable[[str, Any], str | None]:
    """``ScreenRouter.media_url``: the ``/m/<token>`` link of a picture or video; ``None`` for kinds the route
    never serves (documents), so the screen sends the media itself instead of a dead link preview."""
    from svbg.content.media import PUBLIC_KINDS

    def url(base: str, item: Any) -> str | None:
        return public.url(base, item) if item.kind in PUBLIC_KINDS else None

    return url


def media_limits(config: Callable[[], Mapping[str, Any]] | None) -> MediaLimits:
    from svbg.content.media import MediaLimits

    base = MediaLimits()
    side = cfg(config, "MEDIA_PHOTO_MAX_SIDE", base.photo_max_side)
    quality = cfg(config, "MEDIA_PHOTO_JPEG_QUALITY", base.jpeg_quality)
    try:
        return MediaLimits(photo_max_side=int(side), jpeg_quality=int(quality))
    except (TypeError, ValueError):
        return base


# ------------------------------------------------------------------------------------------ deep links


class PromoPortAdapter:
    """:class:`svbg.deeplinks.ports.PromoPort` over :class:`svbg.promo.service.PromoService`."""

    def __init__(self, service: PromoService) -> None:
        self.service = service

    async def lookup(self, code: str) -> PromoInfo | None:
        promo = await self.service.find(code)
        if promo is None:
            return None
        try:
            summary: str | None = self.service.describe(promo)
        except Exception:  # noqa: BLE001 - the summary is decoration of the link builder
            summary = None
        return PromoInfo(promo.id, promo.code, promo.kind, active=promo.enabled, summary=summary)

    async def apply_from_link(self, user_id: int, code: str) -> PromoOutcome:
        result = await self.service.activate(user_id, code, source="link")
        status = {
            "applied": PromoStatus.APPLIED,
            "pending": PromoStatus.PENDING,
        }.get(result.outcome, PromoStatus.REFUSED)
        return PromoOutcome(status, result.text or None, self.service.localize(result, "en") or None)


class AdsPortAdapter:
    """:class:`svbg.deeplinks.ports.AdsPort` over :class:`svbg.ads.service.AdService` (in memory)."""

    def __init__(self, service: AdService) -> None:
        self.service = service

    async def find(self, code: str) -> AdRef | None:
        link = self.service.by_code(code)
        if link is None:
            return None
        return AdRef(link.id, link.code, active=link.enabled, title=link.title)

    async def attach(self, user_id: int, ad_link_id: int, *, is_new: bool) -> None:
        link = self.service.get(ad_link_id)
        if link is not None:
            await self.service.record_start(link, user_id, is_new=is_new)


# ------------------------------------------------------------------------------------------ referral

INVITE_SCREEN: Final = "invite"

_INVITE_T: Final[Mapping[str, Mapping[str, str]]] = {
    "ru": {
        "title": "🤝 <b>Пригласите друзей</b>",
        "link": "🔗 Ваша ссылка:",
        "share": "📤 Поделиться",
        "copy": "📋 Скопировать ссылку",
        "menu": "🏠 Меню",
        "no_link": "Ссылка появится, когда бот подключится к Telegram.",
    },
    "en": {
        "title": "🤝 <b>Invite friends</b>",
        "link": "🔗 Your link:",
        "share": "📤 Share",
        "copy": "📋 Copy the link",
        "menu": "🏠 Menu",
        "no_link": "The link appears once the bot is connected to Telegram.",
    },
}


def _it(lang: str, key: str) -> str:
    return (_INVITE_T.get(lang) or _INVITE_T["ru"])[key]


class ReferralScreens:
    """«🤝 Пригласить»: the rules, the personal link, «Поделиться» and the counters (one SQL; none when the
    program is off). Content buttons reach it as ``system:referral`` (or ``system:invite``)."""

    def __init__(self, service: ReferralService, *, home: str = "home") -> None:
        self.service = service
        self.home = home

    def register(self, router: ScreenRouter) -> None:
        router.screen(INVITE_SCREEN)(self.screen)
        for name in ("referral", "invite"):
            router.action("sys", name)(self._go)

    async def _go(self, _ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        return Redirect(INVITE_SCREEN)

    async def screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        from html import escape

        lang = ctx.lang
        view = await self.service.invite_view(ctx.user.user_id, lang)
        lines = [_it(lang, "title"), ""]
        rows: list[list[InlineKeyboardButton]] = []
        if view.enabled:
            lines.extend(view.lines)
            if view.link:
                lines += ["", _it(lang, "link"), f"<code>{escape(view.link)}</code>"]
                if view.share_url:
                    rows.append([InlineKeyboardButton(text=_it(lang, "share"), url=view.share_url)])
                if len(view.link) <= 256:
                    rows.append(
                        [
                            InlineKeyboardButton(
                                text=_it(lang, "copy"), copy_text=CopyTextButton(text=view.link)
                            )
                        ]
                    )
            else:
                lines += ["", _it(lang, "no_link")]
        else:
            lines.extend(view.lines)
        rows.append([nav_button(_it(lang, "menu"), self.home)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)
