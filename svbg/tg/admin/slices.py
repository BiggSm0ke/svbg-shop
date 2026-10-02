"""Settings «slices»: the keys of one admin section on one screen (``set.v:<slice>``).

The registry is laid out like ``.env`` (sections «Запуск», «Telegram», «Платёжки»…). The admin shows each key
where an owner looks for it: trial days next to the plans, the support link under «📣 Связь», the backup
schedule in «💾 Бэкапы». A slice is that place: a short sentence, the keys as buttons, the rarely needed keys
behind «🧰 Ещё», links to related screens. Every key has exactly one *home* (:func:`home_of`): the card's
«⬅️» leads there. A key may be mirrored in another slice (a button to the same card).

Some homes are other screens: the keys of a cash desk live on its card (``apay.c:<slug>``), the admin group
on ``achat``. A key nobody mapped falls into «Прочее» under «⚙️ Система» (a test keeps it empty).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from svbg.tg.admin import nav

if TYPE_CHECKING:
    from svbg.core.settings.registry import SettingDef

__all__ = [
    "OTHER",
    "PAY_LIST",
    "SCREEN",
    "SLICES",
    "Link",
    "Slice",
    "home_of",
    "pay_slice",
    "slice_of",
    "target_of",
    "title_of",
]

SCREEN: Final = "set.v"
OTHER: Final = "sys.other"
PAY_LIST: Final = "pay.list"
_PAY_PREFIX: Final = "pay."


@dataclass(frozen=True, slots=True)
class Link:
    """A button to a related screen or action on a slice, shown when the viewer has ``role`` and ``perm``."""

    label: str
    screen: str
    action: str = "o"
    arg: str | None = None
    role: str = "admin"
    perm: str | None = None


@dataclass(frozen=True, slots=True)
class Slice:
    id: str
    title: str  # emoji + name, as on the button that opens it
    hub: str  # the screen «⬅️» leads to
    intro: str  # one plain sentence under the header
    keys: tuple[str, ...] = ()
    more: tuple[str, ...] = ()  # behind «🧰 Ещё (N)»
    sections: tuple[str, ...] = ()  # every key of these registry sections (modules)
    mirrors: tuple[str, ...] = ()  # keys whose home is elsewhere, shown here too
    toggles: bool = False  # a switch is toggled right on the slice
    links: tuple[Link, ...] = ()
    screen: str | None = None  # the keys live on another screen (``achat``, ``apay.c``)


def _s(*keys: str) -> tuple[str, ...]:
    return keys


SLICES: Final[Mapping[str, Slice]] = MappingProxyType(
    {
        s.id: s
        for s in (
            Slice(
                "u.access",
                "🚪 Вход в бот",
                nav.HUB_USERS,
                "Что видит новый человек до меню: капча, согласие с правилами, подписка на канал.",
                _s(
                    "CAPTCHA_ENABLED",
                    "ONBOARDING_RULES",
                    "REQUIRED_CHANNEL_ID",
                    "REQUIRED_CHANNEL_URL",
                    "CHANNEL_REQUIRED_FOR",
                    "CHANNEL_LEAVE_ACTION",
                ),
                more=_s("CAPTCHA_EMOJIS"),
                toggles=True,
            ),
            Slice(
                "p.trial",
                "🎁 Пробный период",
                "plans",
                "Сколько дней дать бесплатно и кому. 0 дней выключает пробный период.",
                _s("TRIAL_DAYS", "TRIAL_AUDIENCE"),
                more=_s("TRIAL_CARRY_OVER"),
            ),
            Slice(
                "p.more",
                "⚙️ Правила подписки",
                "plans",
                "Как часто клиент может менять ссылку и сбрасывать устройства, как округлять цены и когда "
                "кнопка «📱 Подписка» в меню синеет и краснеет.",
                _s(
                    "REISSUE_COOLDOWN_MINUTES",
                    "DEVICES_RESET_COOLDOWN_MINUTES",
                    "PRICING_ROUNDING",
                    "SUB_BUTTON_BLUE_DAYS",
                    "SUB_BUTTON_RED_DAYS",
                ),
                more=_s("DEVICES_CACHE_TTL_S"),
            ),
            Slice(
                "pay.wallet",
                "💰 Баланс и пополнение",
                nav.HUB_PAY,
                "Сколько можно положить на баланс за раз и какие суммы предложить кнопками.",
                _s(
                    "WALLET_TOPUP_MIN",
                    "WALLET_TOPUP_MAX",
                    "WALLET_TOPUP_PRESETS",
                    "WALLET_AUTOCOMPLETE_MINUTES",
                ),
                mirrors=_s("CURRENCY"),
            ),
            Slice(
                PAY_LIST,
                "🏦 Кассы",
                nav.HUB_PAY,
                "Кассы",
                more=_s("PAY_CLOCK_SKEW_ALERT_COUNT"),
                screen="apay",
            ),
            Slice(
                "m.ref",
                "🤝 Рефералка",
                nav.HUB_MARKETING,
                "Клиент зовёт друзей по своей ссылке и получает за них дни или процент.",
                _s(
                    "REFERRAL_ENABLED",
                    "REFERRAL_MODE",
                    "REFERRAL_INVITER_DAYS",
                    "REFERRAL_INVITEE_DAYS",
                    "REFERRAL_TRIGGER",
                    "REFERRAL_PERCENT",
                    "REFERRAL_INVITER_CAP_30D",
                    "REFERRAL_INVITER_CAP_TOTAL",
                ),
                more=_s("ONBOARDING_ASK_REFERRAL_CODE"),
            ),
            Slice(
                "m.links",
                "⚙️ Настройки ссылок",
                "dl",
                "Сколько бот помнит, куда вела ссылка, пока человек подписывается на канал.",
                more=_s("DEEPLINK_INTENT_TTL_HOURS"),
            ),
            Slice(
                "c.notify",
                "🔔 Уведомления клиентам",
                nav.HUB_COMM,
                "Какие сообщения бот сам пишет клиентам. Нажмите, чтобы включить или выключить.",
                _s(
                    "NOTIFY_USER_EXPIRING",
                    "NOTIFY_EXPIRING_HOURS",
                    "NOTIFY_USER_EXPIRED",
                    "NOTIFY_USER_TRIAL_ENDING",
                    "NOTIFY_TRIAL_ENDING_HOURS",
                    "NOTIFY_USER_TRAFFIC",
                    "NOTIFY_USER_FIRST_CONNECTED",
                    "NOTIFY_USER_DEVICES",
                    "NOTIFY_USER_REVOKED",
                ),
                toggles=True,
            ),
            Slice(
                "c.support",
                "💬 Поддержка",
                nav.HUB_COMM,
                "Куда ведёт кнопка «Поддержка» у клиентов.",
                _s("SUPPORT_MODE", "SUPPORT_URL", "SUPPORT_CHAT_ID"),
            ),
            Slice(
                "c.achat",
                "🛎 Админ-группа",
                nav.HUB_COMM,
                "Админ-группа",
                _s("ADMIN_CHAT_ID", "NOTIFY_ADMIN_NODES"),
                screen="achat",
            ),
            Slice(
                "l.lang",
                "🌐 Языки",
                nav.HUB_LOOK,
                "На каком языке бот говорит с новыми людьми и из каких языков можно выбрать.",
                _s("DEFAULT_LANGUAGE", "I18N_AVAILABLE", "I18N_ASK_ON_START"),
            ),
            Slice(
                "l.media",
                "🖼 Картинки и баннер",
                nav.HUB_LOOK,
                "Как бот сжимает загруженные картинки и сколько копий текстов хранит.",
                more=_s("MEDIA_PHOTO_MAX_SIDE", "MEDIA_PHOTO_JPEG_QUALITY", "CONTENT_BACKUPS_KEEP"),
                links=(
                    Link("🖼 Заглушка: убрать со всех экранов", "ce.a", "bnx", perm="content.edit"),
                    Link("🖼 Заглушка: вернуть на экраны без картинки", "ce.a", "bnr", perm="content.edit"),
                ),
            ),
            Slice(
                "st.report",
                "📰 Ежедневный отчёт",
                nav.STATS,
                "Сводка за вчера каждый день в админ-группу.",
                _s("REPORT_DAILY_ENABLED", "REPORT_DAILY_AT"),
            ),
            Slice(
                "sys.main",
                "🧭 Основное",
                nav.HUB_SYSTEM,
                "Часовой пояс для отчётов и расписаний, валюта цен и язык по умолчанию.",
                _s("TIMEZONE", "CURRENCY"),
                mirrors=_s("DEFAULT_LANGUAGE"),
            ),
            Slice(
                "sys.maint",
                "🛠 Техработы",
                nav.HUB_SYSTEM,
                "Пока идут техработы, новые покупки и пробный период недоступны, оплаченное выдаётся потом.",
                _s("MAINTENANCE_MODE", "MAINTENANCE_MESSAGE"),
            ),
            Slice(
                "sys.backup",
                "⚙️ Настройки бэкапов",
                "ops",
                "Когда делать бэкап, сколько хранить и куда отправлять. Проверка обновлений тоже здесь.",
                _s(
                    "BACKUP_ENABLED",
                    "BACKUP_AT",
                    "BACKUP_KEEP",
                    "BACKUP_PASSWORD",
                    "BACKUP_TO_TELEGRAM",
                    "UPDATE_CHECK",
                ),
                more=_s("UPDATE_REPO", "UPDATE_PRERELEASES"),
            ),
            Slice(
                "sys.panel",
                "⚙️ Настройки панели",
                nav.PANEL,
                "Адрес панели Remnawave и ключ доступа. Остальное нужно редко.",
                _s("REMNAWAVE_URL", "REMNAWAVE_TOKEN"),
                more=_s(
                    "REMNAWAVE_CADDY_TOKEN",
                    "REMNAWAVE_COOKIE",
                    "REMNAWAVE_CF_CLIENT_ID",
                    "REMNAWAVE_CF_CLIENT_SECRET",
                    "REMNAWAVE_TLS_VERIFY",
                    "REMNAWAVE_RPS_INTERACTIVE",
                    "REMNAWAVE_RPS_BACKGROUND",
                    "REMNAWAVE_CONFIRMED_MAJOR",
                    "REMNAWAVE_WEBHOOK_SECRET",
                    "REMNAWAVE_WEBHOOK_SECRET_PREVIOUS",
                    "REMNAWAVE_SYNC_MINUTES",
                    "REMNAWAVE_SYNC_NO_WEBHOOKS_MINUTES",
                    "REMNAWAVE_ALLOW_PLAIN_HTTP",
                    "CATALOG_LOCATIONS_SYNC_MINUTES",
                    "PANEL_USERNAME_PREFIX",
                    "PANEL_DESCRIPTION_TEMPLATE",
                ),
            ),
            Slice(
                "sys.team",
                "⚙️ Лимиты команды",
                "roles",
                "Кто владелец и сколько дней и денег админ может выдать без владельца.",
                _s(
                    "OWNER_IDS",
                    "ADMIN_GRANT_DAYS_MAX",
                    "ADMIN_GRANT_DAYS_DAY_MAX",
                    "ADMIN_WALLET_ADJUST_MAX",
                    "ADMIN_WALLET_ADJUST_DAY_MAX",
                ),
            ),
            Slice(
                "sys.server",
                "🧰 Сервер и .env",
                nav.HUB_SYSTEM,
                "Адрес бота, токен и то, что обычно задают один раз при установке.",
                _s("PUBLIC_URL", "BOT_MODE", "BOT_TOKEN"),
                more=_s(
                    "LOG_LEVEL",
                    "SECRET_KEY",
                    "LOCKED_KEYS",
                    "DATA_DIR",
                    "DATABASE_URL",
                    "TELEGRAM_PROXY",
                    "TELEGRAM_API_URL",
                    "WEBHOOK_SECRET",
                    "ENV_LAYOUT",
                    "ENV_SECRETS",
                    "IMPORT_SOURCE_DSN",
                    "IMPORT_SHADOW_ENABLED",
                ),
            ),
            Slice(
                "mod.lte",
                "⚙️ Настройки LTE",
                "lte",
                "Лимиты мобильного трафика и что делать, когда клиент их превысил.",
                sections=("lte",),
                toggles=True,
            ),
            Slice(
                "mod.ipguard",
                "⚙️ Настройки IP Guard",
                "ipguard",
                "Сколько адресов можно одной подписке и когда предупреждать или блокировать.",
                sections=("ip_guard",),
                toggles=True,
            ),
            Slice(
                OTHER,
                "🗂 Прочее",
                nav.HUB_SYSTEM,
                "Настройки, которым ещё не нашлось места в разделах.",
            ),
        )
    }
)

_BY_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {key: s.id for s in SLICES.values() for key in (*s.keys, *s.more)}
)
_BY_SECTION: Final[Mapping[str, str]] = MappingProxyType(
    {section: s.id for s in SLICES.values() for section in s.sections}
)


def pay_slice(slug: str) -> str:
    """Home of the keys of one cash desk (its card ``apay.c:<slug>``)."""
    return f"{_PAY_PREFIX}{slug}"


def home_of(defn: SettingDef) -> str:
    """The slice id where ``defn`` lives (exactly one per key)."""
    home = _BY_KEY.get(defn.key)
    if home is not None:
        return home
    section = defn.section
    if section.startswith("payments."):
        return pay_slice(section.partition(".")[2])
    return _BY_SECTION.get(section) or _BY_SECTION.get(section.partition(".")[0]) or OTHER


def slice_of(slice_id: str) -> Slice | None:
    """A rendered slice (``set.v``); pseudo homes (a cash desk, the admin group) are not slices."""
    found = SLICES.get(slice_id)
    return None if found is None or found.screen is not None else found


def target_of(slice_id: str) -> tuple[str, str | None]:
    """``(screen, arg)`` that shows the home ``slice_id``."""
    found = SLICES.get(slice_id)
    if found is not None:
        return (found.screen, None) if found.screen is not None else (SCREEN, slice_id)
    if slice_id.startswith(_PAY_PREFIX):
        return "apay.c", slice_id[len(_PAY_PREFIX) :]
    return SCREEN, OTHER


def title_of(slice_id: str) -> str:
    found = SLICES.get(slice_id)
    if found is not None:
        return found.title
    if slice_id.startswith(_PAY_PREFIX):
        return "🏦 Касса"
    return SLICES[OTHER].title
