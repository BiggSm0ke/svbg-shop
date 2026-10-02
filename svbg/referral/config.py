"""Settings of the referral program (05 §2.3.4: 21 Bedolaga keys → 7, plus the percent of the wallet mode).

All keys are ``HOT``: they are read from the snapshot on every use, so a change in the bot applies at once.
Everything that shapes a reward (mode, days, trigger, caps, percent) is ``owner_only``: an admin with
``settings.business`` could otherwise raise the days, lift the caps and pick ``register`` to farm free days
with throwaway accounts. Only the on/off switch stays a business setting.
The integration step adds :data:`SETTINGS` to the core registry (section ``referral``) — see
:func:`register_settings`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from svbg.core.settings.registry import Apply, SettingDef
from svbg.referral.rules import DEFAULTS, MAX_DAYS, MAX_PERCENT, Mode, Trigger

if TYPE_CHECKING:
    from svbg.core.settings.registry import Registry

__all__ = ["SECTION", "SETTINGS", "register_settings"]

SECTION: Final = "referral"

SETTINGS: Final[tuple[SettingDef, ...]] = (
    SettingDef(
        "REFERRAL_ENABLED",
        bool,
        DEFAULTS["REFERRAL_ENABLED"],
        SECTION,
        "Реферальная программа",
        "Включить приглашения по личной ссылке и награды за друзей.",
        apply=Apply.HOT,
        tags=("рефералка", "пригласить", "referral"),
    ),
    SettingDef(
        "REFERRAL_MODE",
        "enum",
        DEFAULTS["REFERRAL_MODE"],
        SECTION,
        "Режим наград",
        "days — дни подписки обеим сторонам; percent — процент с покупок приглашённых "
        "на баланс пригласившего.",
        apply=Apply.HOT,
        owner_only=True,
        choices=tuple(m.value for m in Mode),
    ),
    SettingDef(
        "REFERRAL_INVITER_DAYS",
        int,
        DEFAULTS["REFERRAL_INVITER_DAYS"],
        SECTION,
        "Дней пригласившему",
        "Сколько дней подписки получает пригласивший за каждого друга. 0 — не начислять.",
        apply=Apply.HOT,
        owner_only=True,
        min=0,
        max=MAX_DAYS,
    ),
    SettingDef(
        "REFERRAL_INVITEE_DAYS",
        int,
        DEFAULTS["REFERRAL_INVITEE_DAYS"],
        SECTION,
        "Дней приглашённому",
        "Сколько дней подписки в подарок получает приглашённый. 0 — не начислять.",
        apply=Apply.HOT,
        owner_only=True,
        min=0,
        max=MAX_DAYS,
    ),
    SettingDef(
        "REFERRAL_TRIGGER",
        "enum",
        DEFAULTS["REFERRAL_TRIGGER"],
        SECTION,
        "Когда начислять дни",
        "paid — после оплаты подписки другом; trial_or_paid — после пробного периода или оплаты; "
        "register — сразу за переход по ссылке (только тем, у кого уже есть подписка).",
        apply=Apply.HOT,
        owner_only=True,
        choices=tuple(t.value for t in Trigger),
    ),
    SettingDef(
        "REFERRAL_INVITER_CAP_30D",
        int,
        DEFAULTS["REFERRAL_INVITER_CAP_30D"],
        SECTION,
        "Лимит наград за 30 дней",
        "Сколько друзей за скользящие 30 дней приносят дни пригласившему. "
        "Сверх лимита награда ждёт до 7 дней. 0 — без лимита. Приглашённый получает дни всегда.",
        apply=Apply.HOT,
        owner_only=True,
        min=0,
        max=100_000,
    ),
    SettingDef(
        "REFERRAL_INVITER_CAP_TOTAL",
        int,
        DEFAULTS["REFERRAL_INVITER_CAP_TOTAL"],
        SECTION,
        "Лимит наград за всё время",
        "Сколько всего друзей могут принести дни одному пригласившему. 0 — без лимита.",
        apply=Apply.HOT,
        owner_only=True,
        min=0,
        max=1_000_000,
    ),
    SettingDef(
        "REFERRAL_PERCENT",
        int,
        DEFAULTS["REFERRAL_PERCENT"],
        SECTION,
        "Процент с покупок",
        "Режим percent: сколько процентов от каждой покупки приглашённого зачислить пригласившему на баланс.",
        apply=Apply.HOT,
        owner_only=True,
        min=0,
        max=MAX_PERCENT,
    ),
)


def register_settings(registry: Registry) -> None:
    """Add the referral keys to ``registry`` (skips keys that are already there)."""
    for defn in SETTINGS:
        if defn.key not in registry:
            registry.add(defn)
