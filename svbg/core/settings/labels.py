"""Owner-facing wording of setting values for the bot's screens (the ``.env`` keeps the raw values).

* :data:`CHOICE_LABELS` — a human label per enum value («Чат прямо в боте» instead of ``tickets``);
* :data:`PRESETS` — ready values shown as buttons on a setting's card (``TRIAL_DAYS``: 1 / 3 / 7);
* :data:`UI_DESCRIPTIONS` — card texts that explain the values by their labels and other settings by their
  titles. The registry descriptions stay as they are: they are the ``.env`` comments and document the raw
  values of the file.

A definition may carry its own ``choice_labels`` / ``presets`` (plugins); these tables are the fallback for
the keys of the core, the referral program and the bundled modules. Lookups never raise.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from svbg.core.settings.registry import SettingDef

__all__ = ["CHOICE_LABELS", "PRESETS", "UI_DESCRIPTIONS", "choice_label", "description", "presets"]

CHOICE_LABELS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "SUPPORT_MODE": {
            "link": "Ссылка на аккаунт поддержки",
            "tickets": "Чат прямо в боте",
            "both": "Ссылка и чат",
        },
        "MAINTENANCE_MODE": {"auto": "Сами, если панель недоступна", "on": "Включены", "off": "Выключены"},
        "TRIAL_AUDIENCE": {"all": "Всем", "channel_members": "Только подписчикам канала"},
        "CHANNEL_REQUIRED_FOR": {"trial": "Только для пробного", "all": "Для всего бота"},
        "CHANNEL_LEAVE_ACTION": {"off": "Ничего", "trial": "Отключить пробный", "all": "Отключить и платную"},
        "ONBOARDING_RULES": {"off": "Не спрашивать", "on": "Просить согласие"},
        "REFERRAL_MODE": {"days": "Дни обоим", "percent": "Процент с покупок"},
        "REFERRAL_TRIGGER": {
            "paid": "После оплаты друга",
            "trial_or_paid": "После пробного или оплаты",
            "register": "Сразу за переход",
        },
        "BOT_MODE": {"polling": "Опрос (домен не нужен)", "webhook": "Вебхук (нужен домен)"},
        "ENV_LAYOUT": {"full": "Все ключи", "compact": "Только заданные"},
        "ENV_SECRETS": {"plain": "Хранить в файле", "omit": "Только в базе"},
        "DEFAULT_LANGUAGE": {"ru": "Русский", "en": "English"},
        "LTE_ENFORCE": {"on": "Включено", "shadow": "Тень (только журнал)", "off": "Выключено"},
        "LTE_OFF_ACTION": {"keep": "Оставить блоки", "release": "Снять блоки"},
    }
)

PRESETS: Final[Mapping[str, tuple[Any, ...]]] = MappingProxyType(
    {
        "TRIAL_DAYS": (1, 3, 7),
        "NOTIFY_TRIAL_ENDING_HOURS": (2, 12, 24),
        "BACKUP_KEEP": (3, 7, 14),
        "REFERRAL_INVITER_DAYS": (3, 7, 14),
        "REFERRAL_INVITEE_DAYS": (3, 7, 14),
        "WALLET_AUTOCOMPLETE_MINUTES": (30, 60, 180),
    }
)

UI_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "BOT_MODE": "Как бот получает сообщения. Опрос работает без домена. Вебхук быстрее, но нужен "
        "публичный адрес бота.",
        "REMNAWAVE_CF_CLIENT_SECRET": "Вторая половина ключа Cloudflare Access, пара к Client ID.",
        "TRIAL_AUDIENCE": "Кто может взять пробный период: все или только подписчики обязательного канала.",
        "CHANNEL_REQUIRED_FOR": "Для чего нужна подписка на канал: только для пробного периода или для всего "
        "бота, включая покупки.",
        "CHANNEL_LEAVE_ACTION": "Что делать, если человек отписался от канала. Отключённый пробный вернётся, "
        "когда он подпишется снова. Боту нужны права администратора канала.",
        "ONBOARDING_RULES": "Новый пользователь сначала соглашается с правилами (страница «Правила»), потом "
        "пользуется ботом.",
        "ADMIN_WALLET_ADJUST_DAY_MAX": "Сколько один админ может начислить и списать за 24 часа, в валюте "
        "магазина. Пусто — втрое больше лимита на одну операцию.",
        "ADMIN_GRANT_DAYS_DAY_MAX": "Сколько дней один админ может выдать или убавить за 24 часа. Пусто — "
        "втрое больше лимита на одну операцию.",
        "NOTIFY_USER_EXPIRING": "Сообщение клиенту за несколько часов до конца платной подписки. Когда "
        "именно, задаёт «Когда напоминать о конце подписки».",
        "NOTIFY_USER_TRIAL_ENDING": "Сообщение клиенту незадолго до конца пробного периода с предложением "
        "купить подписку.",
        "SUPPORT_MODE": "Как клиент пишет в поддержку: по ссылке на ваш аккаунт или прямо в бота. Во втором "
        "случае у каждого клиента своя тема в админ-группе, ответ из темы приходит ему в бот.",
        "MAINTENANCE_MODE": "Во время техработ новые покупки и пробный период показывают заглушку, "
        "оплаченное выдаётся после. «Сами» значит: бот включит техработы, если панель недоступна дольше "
        "3 минут, и снимет, когда она вернётся.",
        "ENV_LAYOUT": "Какие ключи писать в файл .env: все или только те, что отличаются от значений по "
        "умолчанию.",
        "ENV_SECRETS": "Где хранить секреты: в файле .env (доступ только у владельца файла) или только в "
        "базе, а в файле оставить заглушку.",
        "BACKUP_ENABLED": "Каждый день делать бэкап базы, контента и зашифрованной копии .env. Время задаёт "
        "«Время бэкапа».",
        "BACKUP_TO_TELEGRAM": "Отправлять бэкап в тему «💾 Бэкапы» админ-группы, частями до 45 МБ. Нужен "
        "пароль бэкапов.",
        "REPORT_DAILY_ENABLED": "Каждый день присылать отчёт за вчера в тему «📊 Отчёты» админ-группы. "
        "Время задаёт «Время ежедневного отчёта».",
        "REFERRAL_MODE": "Чем награждать: днями подписки обоим или процентом с покупок приглашённого на "
        "баланс пригласившего.",
        "REFERRAL_TRIGGER": "Когда начислять дни: после оплаты друга, после его пробного периода или оплаты, "
        "или сразу за переход по ссылке (только тем, у кого уже есть подписка).",
        "LTE_ENFORCE": "Включено: блоки ставятся в панели. Тень: решения только пишутся в журнал. Выключено: "
        "решений нет.",
        "LTE_OFF_ACTION": "Что сделать с уже поставленными блоками, когда модуль выключают.",
    }
)


def choice_label(defn: SettingDef, value: Any) -> str | None:
    """Human label of an enum value; ``None`` when there is none (show the raw value)."""
    labels = getattr(defn, "choice_labels", None) or CHOICE_LABELS.get(defn.key)
    if not labels or not isinstance(value, str):
        return None
    return labels.get(value)


def presets(defn: SettingDef) -> tuple[Any, ...]:
    """Ready values for the card's buttons (empty: none)."""
    own = getattr(defn, "presets", None)
    return tuple(own) if own else PRESETS.get(defn.key, ())


def description(defn: SettingDef) -> str:
    """The card text of a setting (the registry description unless a human one is defined)."""
    return UI_DESCRIPTIONS.get(defn.key) or defn.description
