"""Texts of the referral program: user messages (in the recipient's language), the «Пригласить» screen and
the reports to the «🤝 Партнёры» topic (Russian, owner-facing).

Numbers in the texts come from the same :class:`~svbg.referral.rules.Rules` the rewards are computed with:
the screen promises exactly what will be granted. Every value coming from users (names, usernames) is
HTML-escaped here; the templates are Telegram HTML.

An override hook (``overrides(lang, key) -> str | None``, e.g. the content store's ``text_overrides``) may
replace any user template; a broken override (unknown placeholder) falls back to the built-in text.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from html import escape
from typing import Any, Final
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ADMIN",
    "USER",
    "Overrides",
    "fmt_date",
    "mention",
    "render",
    "share_url",
]

log = logging.getLogger("svbg.referral.texts")

Overrides = Callable[[str, str], str | None]

#: User texts by language (``ru`` is the fallback). Placeholders: ``{name}`` (already escaped HTML),
#: ``{days}``, ``{percent}``, ``{amount}``.
USER: Final[Mapping[str, Mapping[str, str]]] = {
    "ru": {
        "welcome_invitee_paid": (
            "🎉 Вы пришли по приглашению {name}.\nОформите подписку и получите +{days} дн. в подарок."
        ),
        "welcome_invitee_trial": (
            "🎉 Вы пришли по приглашению {name}.\nПопробуйте бесплатно или оформите подписку, и получите "
            "+{days} дн. в подарок."
        ),
        "welcome_invitee_register": (
            "🎉 Вы пришли по приглашению {name}.\nНачисляем вам +{days} дн. за переход."
        ),
        "welcome_invitee_plain": "🎉 Вы пришли по приглашению {name}.",
        "new_referral_days": (
            "👥 Новый друг по вашей ссылке: {name}!\nКогда он оформит подписку, вам начислится +{days} дн."
        ),
        "new_referral_register": "👥 Новый друг по вашей ссылке: {name}!\nНачисляем вам +{days} дн.",
        "new_referral_percent": (
            "👥 Новый друг по вашей ссылке: {name}!\nВы будете получать {percent}% с его покупок на баланс."
        ),
        "new_referral_plain": "👥 Новый друг по вашей ссылке: {name}!",
        "granted_inviter": "🎁 +{days} дн. подписки за приглашённого {name}!",
        "granted_invitee": "🎁 +{days} дн. подписки в подарок по приглашению {name}!",
        "granted_percent": "💰 +{amount} на баланс за покупку приглашённого {name}.",
    },
    "en": {
        "welcome_invitee_paid": ("🎉 You were invited by {name}.\nSubscribe and get +{days} days as a gift."),
        "welcome_invitee_trial": (
            "🎉 You were invited by {name}.\n"
            "Start the free trial or subscribe, and get +{days} days as a gift."
        ),
        "welcome_invitee_register": ("🎉 You were invited by {name}.\nYou get +{days} days for joining."),
        "welcome_invitee_plain": "🎉 You were invited by {name}.",
        "new_referral_days": (
            "👥 New friend via your link: {name}!\nWhen they subscribe, you get +{days} days."
        ),
        "new_referral_register": "👥 New friend via your link: {name}!\nYou get +{days} days.",
        "new_referral_percent": (
            "👥 New friend via your link: {name}!\n{percent}% of their purchases will go to your balance."
        ),
        "new_referral_plain": "👥 New friend via your link: {name}!",
        "granted_inviter": "🎁 +{days} days of subscription for inviting {name}!",
        "granted_invitee": "🎁 +{days} days of subscription as a gift from {name}!",
        "granted_percent": "💰 +{amount} to your balance for a purchase by {name}.",
    },
}

#: The «Пригласить» screen (placeholders of the screen text / lines for it).
SCREEN: Final[Mapping[str, Mapping[str, str]]] = {
    "ru": {
        "how_title": "🎁 <b>Как работают награды</b>",
        "inviter_paid": (
            "• Вы получаете <b>+{days} дн.</b> за каждого приглашённого, который оформит подписку"
        ),
        "inviter_trial": (
            "• Вы получаете <b>+{days} дн.</b> за каждого приглашённого, "
            "который попробует бесплатно или оформит подписку"
        ),
        "inviter_register": "• Вы получаете <b>+{days} дн.</b> за каждого, кто перейдёт по вашей ссылке",
        "invitee": "• Приглашённый получает <b>+{days} дн.</b>",
        "invitee_register": "• Приглашённый сразу получает <b>+{days} дн.</b>",
        "percent": "• С каждой покупки приглашённых вам на баланс приходит <b>{percent}%</b>",
        "cap": "• Не больше {cap} наград за 30 дней",
        "stats": "👥 Приглашено: <b>{invited}</b>\nОформили подписку: <b>{subscribed}</b> ({conversion}%)",
        "pending": "Ждут начисления: {pending}",
        "share_days": "🎁 Оформи подписку по этой ссылке и получишь {days} дн. в подарок!",
        "share_register": "🎁 Просто перейди по ссылке и получишь {days} дн. в подарок!",
        "share_plain": "🔐 Подключайся к VPN по моей ссылке!",
        "off": "Реферальная программа сейчас выключена.",
    },
    "en": {
        "how_title": "🎁 <b>How rewards work</b>",
        "inviter_paid": "• You get <b>+{days} days</b> for every friend who subscribes",
        "inviter_trial": (
            "• You get <b>+{days} days</b> for every friend who starts the free trial or subscribes"
        ),
        "inviter_register": "• You get <b>+{days} days</b> for everyone who follows your link",
        "invitee": "• Your friend gets <b>+{days} days</b>",
        "invitee_register": "• Your friend gets <b>+{days} days</b> right away",
        "percent": "• <b>{percent}%</b> of every purchase your friends make goes to your balance",
        "cap": "• Up to {cap} rewards per 30 days",
        "stats": "👥 Invited: <b>{invited}</b>\nSubscribed: <b>{subscribed}</b> ({conversion}%)",
        "pending": "Pending rewards: {pending}",
        "share_days": "🎁 Subscribe via this link and get {days} days as a gift!",
        "share_register": "🎁 Just follow the link and get {days} days as a gift!",
        "share_plain": "🔐 Here is my link to the VPN I use",
        "off": "The referral program is off right now.",
    },
}

#: Reports to the «🤝 Партнёры» topic (owner-facing, Russian).
ADMIN: Final[Mapping[str, str]] = {
    "granted_title": "🎁 <b>Реферальная награда (дни)</b>",
    "inviter": "👤 Пригласивший: {who}",
    "invitee": "🙋 Приглашённый: {who}",
    "side_granted": "   Начислено <b>+{days} дн.</b>, подписка до {until}",
    "side_waiting": "   — не выдано: {reason}",
    "side_none": "   —",
    "trigger": "Условие: {trigger}",
    "total": "Всего наград у пригласившего: {total}",
    "deferred_title": "⚠️ <b>Реферальная награда не выдана</b>",
    "pct_title": "💰 <b>Реферальная награда (процент)</b>",
    "pct_line": "   Начислено <b>+{amount}</b> на баланс ({percent}% от {total})",
    "pct_order": "Заказ №{order}",
}

#: Why a side is waiting (admin reports).
REASONS: Final[Mapping[str, str]] = {
    "cap_30d": "у пригласившего достигнут лимит {cap} наград за 30 дней, ждём до {until}",
    "cap_total": "у пригласившего достигнут общий лимит {cap} наград, ждём до {until}",
    "no_subscription": "нет подписки, ждём до {until}",
}

#: The trigger in plain words (admin).
TRIGGERS: Final[Mapping[str, str]] = {
    "paid": "оплата подписки",
    "trial_or_paid": "пробный период или оплата",
    "register": "переход по ссылке",
}


def _lang(lang: str | None, table: Mapping[str, Mapping[str, str]]) -> str:
    code = (lang or "ru").split("-")[0].lower()
    return code if code in table else "ru"


def render(
    key: str,
    lang: str | None = "ru",
    *,
    overrides: Overrides | None = None,
    table: Mapping[str, Mapping[str, str]] = USER,
    **values: Any,
) -> str:
    """The template ``key`` in ``lang`` (fallback ``ru``) formatted with ``values``."""
    code = _lang(lang, table)
    builtin = table[code].get(key) or table["ru"][key]
    if overrides is not None:
        try:
            custom = overrides(code, f"referral.{key}")
        except Exception:  # noqa: BLE001 - an override store failure must not lose the message
            custom = None
        if custom:
            try:
                return custom.format(**values)
            except (KeyError, IndexError, ValueError):
                log.warning("referral text override %s is broken, using the built-in one", key)
    return builtin.format(**values)


def mention(username: str | None, first_name: str | None, telegram_id: int | None, user_id: int) -> str:
    """@username → name → «ID 123» → «#id» (HTML-escaped): username first, it is how people are found."""
    if username:
        return escape(f"@{username.strip()[:64]}")
    name = (first_name or "").strip()
    if name:
        return escape(name[:64])
    if telegram_id:
        return f"ID {int(telegram_id)}"
    return f"#{int(user_id)}"


def admin_who(username: str | None, first_name: str | None, telegram_id: int | None, user_id: int) -> str:
    """``Аня (@anya, id 123)`` for the admin topic (HTML-escaped)."""
    name = escape((first_name or "").strip()[:64]) or "без имени"
    parts = [f"@{escape(username.strip()[:64])}"] if username else []
    parts.append(f"id <code>{int(telegram_id)}</code>" if telegram_id else f"#{int(user_id)}")
    return f"{name} ({', '.join(parts)})"


def fmt_date(value: datetime | str | None, tz: str = "Europe/Moscow") -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if not isinstance(value, datetime):
        return "—"
    with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
        value = value.astimezone(ZoneInfo(tz))
    return value.strftime("%d.%m.%Y %H:%M")


def share_url(link: str, text: str) -> str:
    """Telegram «share» link: opens the chat picker with the invitation text and the link."""
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"
