"""Deep links to any section of the bot (07 §2.4.4, stage 3b).

* :mod:`svbg.deeplinks.codec` — the ``start`` parameter: prefixes, validation, building, short codes;
* :mod:`svbg.deeplinks.model` — :class:`~svbg.deeplinks.model.Intent` (target + promo + ad + referral);
* :mod:`svbg.deeplinks.tables` — ``deeplinks``, ``deeplink_hits``, ``deeplink_daily``;
* :mod:`svbg.deeplinks.ports` — what is needed from the promo, ads and referral modules;
* :mod:`svbg.deeplinks.service` — :class:`~svbg.deeplinks.service.DeeplinkService` (no Telegram types);
* :mod:`svbg.deeplinks.hook` — glue for the app: ``from_app``, the router access probe, ``resume_redirect``.

The admin link builder is :mod:`svbg.tg.admin.deeplinks`.
"""

from __future__ import annotations

from svbg.deeplinks.codec import Kind, Parsed, build, parse, start_url
from svbg.deeplinks.model import Intent, IntentError
from svbg.deeplinks.ports import AdRef, AdsPort, PromoInfo, PromoOutcome, PromoPort, PromoStatus, ReferralPort
from svbg.deeplinks.service import Accepted, DeeplinkService, Landing, LinkRow, LinkStats

__all__ = [
    "Accepted",
    "AdRef",
    "AdsPort",
    "DeeplinkService",
    "Intent",
    "IntentError",
    "Kind",
    "Landing",
    "LinkRow",
    "LinkStats",
    "Parsed",
    "PromoInfo",
    "PromoOutcome",
    "PromoPort",
    "PromoStatus",
    "ReferralPort",
    "build",
    "parse",
    "start_url",
]
