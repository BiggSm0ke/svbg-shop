"""Default content of the user path: system screens as data (07 §2.4.1), import-light (no aiogram, no SQL).

Every user screen is a *content* screen with a code: the owner may change its text, add buttons and reorder or
recolour the system ones (``system_key``). Screens are rendered by the code routes of :mod:`svbg.tg.user`
with screen-specific ``{placeholders}`` (see ``PLACEHOLDERS``); when the content store has no such screen yet
(a fresh database before seeding, a deleted row) the very same seed below is the fallback, so the bot never
shows an empty message. The bot speaks Russian only: every text is under ``ru``.

``USER_SCREENS`` is meant to be appended to :data:`svbg.content.defaults.SYSTEM_SCREENS` by integration (the
``home`` seed replaces the stage-0 one). Button colours are ``style`` (``primary`` / ``success`` /
``danger``); every system button also has a slot for ``icon_custom_emoji_id`` (``None`` by default — the
owner picks a premium emoji in the constructor).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from svbg.content.defaults import HOME, SeedButton, SeedScreen

__all__ = [
    "BALANCE",
    "BUY",
    "BUY_PLAN",
    "CAPTCHA",
    "CHANNEL",
    "CHECKOUT",
    "CONNECT",
    "DEVICES",
    "DEVICES_RESET",
    "HOME",
    "HOME_RELAYOUT",
    "HOME_V2",
    "HOME_V3",
    "NOTICE_PREFIX",
    "PAY_DETAILS",
    "PAY_INVOICE",
    "PAY_WAIT",
    "PLACEHOLDERS",
    "PROFILE",
    "PROFILE_RELAYOUT",
    "PROMOS",
    "PROMO_ENTRY",
    "REISSUE",
    "REISSUE_DONE",
    "REISSUE_WAIT",
    "SEEDS",
    "SHORTFALL",
    "SUB",
    "SUB_LEFT",
    "SUB_NONE",
    "TOPUP",
    "TRIAL_DONE",
    "TRIAL_STARTED",
    "USER_SCREENS",
    "seed_text",
]

# Screen codes (code routes use the same codes, so content edits apply to them).
BUY: Final = "buy"
BUY_PLAN: Final = "buy_plan"
CHECKOUT: Final = "co"
PAY_WAIT: Final = "pay_wait"
SHORTFALL: Final = "pay_short"
PAY_INVOICE: Final = "pay_invoice"
PAY_DETAILS: Final = "pay_details"
BALANCE: Final = "bal"
TOPUP: Final = "topup"
CONNECT: Final = "connect"
DEVICES: Final = "dev"
DEVICES_RESET: Final = "dev_reset"
REISSUE: Final = "reissue"
REISSUE_DONE: Final = "reissue_done"
REISSUE_WAIT: Final = "reissue_wait"
TRIAL_STARTED: Final = "trial_start"
TRIAL_DONE: Final = "trial_done"
CHANNEL: Final = "chan"
#: The old «Подписка» section (``sub``, ``sub_none`` without a subscription): «👤 Профиль» took its place.
#: The codes stay as aliases of the profile for old buttons, deep links and notifications.
SUB: Final = "sub"
SUB_NONE: Final = "sub_none"
#: «👤 Профиль»: who the user is, the balance, the subscription (plan, status, time left, devices,
#: traffic, servers) and the buttons to manage it.
PROFILE: Final = "profile"
#: «🎟 Промокоды» of the profile: the codes the user entered, a waiting discount, «✏️ Ввести промокод»
#: (rendered by :mod:`svbg.promo.user`, which owns the promo engine).
PROMOS: Final = "promos"
#: The code entry form of :mod:`svbg.promo.user` (also ``system:promo`` and the promo deep links).
PROMO_ENTRY: Final = "promo"
#: Placeholder of the home «👤 Профиль» button: the time left («12 дн.», «2 дн. 5 ч», «закончилась»); empty
#: without a subscription, and then the separator in front of it goes too. The colour follows the status.
SUB_LEFT: Final = "left"
#: The entry captcha (``svbg.tg.user.captcha``): the emoji buttons are added by code.
CAPTCHA: Final = "captcha"
#: Notifications: ``notify_<kind>`` (expiring, expired, trial_ending, traffic, limited, first_connected,
#: device_added, revoked).
NOTICE_PREFIX: Final = "notify_"


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _b(text: str) -> dict[str, Any]:
    """Body block whose first line is bold."""
    first = text.split("\n", 1)[0]
    return {"text": text, "entities": [{"type": "bold", "offset": 0, "length": _utf16(first)}]}


def _body(text: str) -> dict[str, dict[str, Any]]:
    return {"ru": _b(text)}


def _btn(
    key: str,
    label: str,
    action: Mapping[str, Any],
    *,
    row: int = 0,
    sort: int = 0,
    style: str | None = None,
    visible_if: Mapping[str, Any] | None = None,
) -> SeedButton:
    return SeedButton(
        system_key=key,
        label={"ru": label},
        action=dict(action),
        row=row,
        sort=sort,
        style=style,
        visible_if=visible_if,
    )


def _was(
    key: str,
    ru: str,
    en: str,
    action: Mapping[str, Any],
    *,
    row: int = 0,
    sort: int = 0,
    style: str | None = None,
    visible_if: Mapping[str, Any] | None = None,
) -> SeedButton:
    """A button exactly as an older version stored it (with its old English label). Never seeded or shown:
    only compared with what an install has, to tell an untouched row from one the owner edited."""
    return SeedButton(
        system_key=key,
        label={"ru": ru, "en": en},
        action=dict(action),
        row=row,
        sort=sort,
        style=style,
        visible_if=visible_if,
    )


def _to(target: str) -> dict[str, str]:
    return {"type": "screen", "target": target}


_MENU: Final = _btn("home", "🏠 Меню", _to(HOME), row=9)
_NO_PAID: Final = {"sub": ["none", "trial"]}
_PAID: Final = {"sub": ["active", "expired", "frozen"]}
_LIVE: Final = {"sub": ["trial", "active"]}
_TRIAL: Final = {"type": "system", "name": "trial"}
_TRIAL_SHOWN: Final = {"flag:trial": True}

#: The home buttons as they were seeded before the «Подписка» section (v1).
_HOME_V1: Final = (
    _was("buy", "🛒 Купить подписку", "🛒 Buy", _to(BUY), style="success", visible_if=_NO_PAID),
    _was("renew", "🔄 Продлить", "🔄 Renew", _to(BUY), style="success", visible_if=_PAID),
    _was("connect", "🔗 Подключиться", "🔗 Connect", _to(CONNECT), row=1, style="primary", visible_if=_LIVE),
    _was(
        "trial",
        "🎁 Попробовать бесплатно",
        "🎁 Free trial",
        _TRIAL,
        row=1,
        style="success",
        visible_if=_TRIAL_SHOWN,
    ),
    _was("devices", "📱 Устройства", "📱 Devices", _to(DEVICES), row=2, visible_if=_LIVE),
    _was("balance", "💰 Баланс", "💰 Balance", _to(BALANCE), row=2, sort=1),
)

#: The home buttons as they were seeded with the «Подписка» section (v2, before «👤 Профиль»).
HOME_V2: Final = (
    _was("connect", "🔗 Подключиться", "🔗 Connect", _to(CONNECT), style="primary", visible_if=_LIVE),
    _was("balance", "💰 Баланс: {balance}", "💰 Balance: {balance}", _to(BALANCE), row=1),
    _was(
        "trial",
        "🎁 Попробовать бесплатно",
        "🎁 Free trial",
        _TRIAL,
        row=2,
        style="success",
        visible_if=_TRIAL_SHOWN,
    ),
    _was("sub", "📱 Подписка · {left}", "📱 Subscription · {left}", _to(SUB), row=3),
)


def _old(
    key: str,
    label: str,
    action: Mapping[str, Any],
    *,
    row: int = 0,
    sort: int = 0,
    style: str | None = None,
    visible_if: Mapping[str, Any] | None = None,
) -> SeedButton:
    """A Russian-only button as an older version stored it: compared with an install, never seeded."""
    return _btn(key, label, action, row=row, sort=sort, style=style, visible_if=visible_if)


#: The home buttons with both «📱 Подписка» and «👤 Профиль» (v3, Russian-only). «Подключиться» and the trial
#: did not move since and are not listed.
HOME_V3: Final = (
    _old("sub", "📱 Подписка · {left}", _to(SUB), row=1),
    _old("profile", "👤 Профиль", _to(PROFILE), row=2),
    _old("balance", "💰 Баланс: {balance}", _to(BALANCE), row=2, sort=1),
)

#: The profile buttons of v3 that changed: «📱 Подписка» leaves (the profile is the section now), the rest
#: moves to make room for the subscription buttons. «Продлить» and «Назад» stayed where they were.
_PROFILE_V3: Final = (
    _old("sub", "📱 Подписка", _to(SUB)),
    _old("connect", "🔗 Подключиться", _to(CONNECT), sort=1, style="primary", visible_if=_LIVE),
    _old("topup", "💳 Пополнить", _to(BALANCE), row=1, sort=1),
    _old("promos", "🎟 Промокоды", _to(PROMOS), row=2, visible_if={"flag:promo": True}),
    _old(
        "referral",
        "🤝 Пригласить друзей",
        {"type": "system", "name": "referral"},
        row=2,
        sort=1,
        visible_if={"flag:referral": True},
    ),
)

#: Placeholders each screen understands (besides ``{balance}`` and ``{days_left}`` available everywhere).
PLACEHOLDERS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        HOME: ("status", "plan", "until", "devices", "name"),
        PROFILE: (
            "name",
            "username",
            "id",
            "since",
            "sub",
            "status",
            "plan",
            "left",
            "until",
            "devices",
            "traffic",
            "servers",
        ),
        PROMOS: ("list", "pending"),
        BUY: (),
        BUY_PLAN: ("plan", "devices", "traffic"),
        CHECKOUT: ("plan", "period", "until", "price", "pay_line"),
        PAY_WAIT: (),
        SHORTFALL: ("price", "missing", "surplus_note"),
        PAY_INVOICE: ("amount", "pay_amount", "after"),
        PAY_DETAILS: ("amount", "details"),
        BALANCE: (),
        TOPUP: ("amount", "surplus_note"),
        CONNECT: ("until", "url", "state"),
        DEVICES: ("count", "limit", "list", "note"),
        DEVICES_RESET: ("count",),
        REISSUE: (),
        REISSUE_DONE: (),
        REISSUE_WAIT: (),
        TRIAL_STARTED: ("days",),
        TRIAL_DONE: ("until",),
        CHANNEL: (),
        CAPTCHA: ("emoji",),
        "notify_expiring": ("until", "left", "plan"),
        "notify_expired": ("until", "plan"),
        "notify_trial_ending": ("until", "left"),
        "notify_traffic": ("percent", "used", "limit"),
        "notify_limited": ("used", "limit"),
        "notify_first_connected": (),
        "notify_device_added": ("device",),
        "notify_revoked": (),
    }
)

USER_SCREENS: Final[tuple[SeedScreen, ...]] = (
    SeedScreen(
        code=HOME,
        title={"ru": "Главная"},
        body=_body("👋 Привет, {name}!\n\n{status}\nБаланс: {balance}"),
        # Rows: connect, the profile (with the time left and the colour), balance + invite (the module button
        # of svbg.content.defaults), the trial, information + support (support is drawn by the code) and the
        # staff «🛠 Админка» (row 9). The profile is also the subscription section; promo codes live there.
        buttons=(
            _btn("connect", "🔗 Подключиться", _to(CONNECT), style="primary", visible_if=_LIVE),
            _btn("profile", "👤 Профиль · {left}", _to(PROFILE), row=1),
            _btn("balance", "💰 Баланс: {balance}", _to(BALANCE), row=2),
            _btn(
                "trial", "🎁 Попробовать бесплатно", _TRIAL, row=3, style="success", visible_if=_TRIAL_SHOWN
            ),
            # staff: one «🛠 Админка» (svbg.content.defaults.ADMIN_BUTTON); the old «⚙️ Настройки» is retired
        ),
    ),
    SeedScreen(
        code=PROFILE,
        title={"ru": "Профиль"},
        body=_body("👤 Профиль\n\n{name}\nID: {id}\nС нами с {since}\nБаланс: {balance}\n\n{sub}"),
        # With a subscription: connect, renew + change plan, devices + top up; without one: buy, top up and
        # the trial. Promo codes and invites when their modules are on.
        buttons=(
            _btn("connect", "🔗 Подключиться", _to(CONNECT), style="primary", visible_if=_LIVE),
            _btn(
                "renew",
                "🔄 Продлить",
                {"type": "system", "name": "renew"},
                row=1,
                style="success",
                visible_if=_PAID,
            ),
            _btn(
                "change",
                "📦 Сменить тариф",
                _to(BUY),
                row=1,
                sort=1,
                visible_if={"sub": ["active", "expired"]},
            ),
            _btn("buy", "🛒 Купить подписку", _to(BUY), row=1, style="success", visible_if=_NO_PAID),
            _btn("devices", "📱 Устройства", _to(DEVICES), row=2, visible_if=_LIVE),
            _btn("topup", "💳 Пополнить", _to(BALANCE), row=2, sort=1),
            _btn(
                "trial", "🎁 Попробовать бесплатно", _TRIAL, row=3, style="success", visible_if=_TRIAL_SHOWN
            ),
            _btn("promos", "🎟 Промокоды", _to(PROMOS), row=4, visible_if={"flag:promo": True}),
            _btn(
                "referral",
                "🤝 Пригласить друзей",
                {"type": "system", "name": "referral"},
                row=4,
                sort=1,
                visible_if={"flag:referral": True},
            ),
            _btn("back", "◀️ Назад", _to(HOME), row=9),
        ),
    ),
    SeedScreen(
        code=PROMOS,
        title={"ru": "Промокоды"},
        body=_body("🎟 Промокоды\n\n{pending}{list}"),
        buttons=(
            _btn("enter", "✏️ Ввести промокод", _to(PROMO_ENTRY), style="primary"),
            _btn("back", "◀️ Назад", _to(PROFILE), row=9),
        ),
    ),
    SeedScreen(
        code=BUY,
        title={"ru": "Покупка"},
        body=_body("🛒 Выберите тариф"),
        buttons=(_btn("back", "◀️ Назад", _to(PROFILE), row=8), _MENU),
    ),
    SeedScreen(
        code=BUY_PLAN,
        title={"ru": "Срок"},
        body=_body(
            "📦 {plan}\nУстройств: {devices} · Трафик: {traffic}\n\nВыберите срок. Чем дольше, тем выгоднее:",
        ),
    ),
    SeedScreen(
        code=CHECKOUT,
        title={"ru": "Оформление"},
        body=_body(
            "🧾 Проверьте заказ\n\nТариф: {plan}\nСрок: {period}\nДействует до: {until}\n"
            "Цена: {price}\nНа балансе: {balance}\n\n{pay_line}",
        ),
    ),
    SeedScreen(
        code=PAY_WAIT,
        title={"ru": "Оформляю"},
        body=_body(
            "⏳ Оформляю подписку…\n\nЭто займёт несколько секунд, сообщение обновится само.",
        ),
    ),
    SeedScreen(
        code=SHORTFALL,
        title={"ru": "Не хватает"},
        body=_body(
            "💳 Не хватает {missing}\n\nЦена: {price}, на балансе: {balance}.\n"
            "Пополните баланс, и подписка оформится сразу после оплаты.{surplus_note}",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=PAY_INVOICE,
        title={"ru": "Счёт"},
        body=_body(
            "🧾 Счёт на {amount} готов\n\nОплатите по кнопке ниже. {after}",
        ),
    ),
    SeedScreen(
        code=PAY_DETAILS,
        title={"ru": "Перевод"},
        body=_body(
            "🏦 Перевод на {amount}\n\n{details}\n\nПосле перевода пришлите сюда фото или PDF чека.",
        ),
    ),
    SeedScreen(
        code=BALANCE,
        title={"ru": "Баланс"},
        body=_body(
            "💰 Баланс: {balance}\n\nС баланса оплачивается подписка. Выберите сумму пополнения:",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=TOPUP,
        title={"ru": "Пополнение"},
        body=_body(
            "💳 Пополнение на {amount}\n\nВыберите способ оплаты:{surplus_note}",
        ),
    ),
    SeedScreen(
        code=CONNECT,
        title={"ru": "Подключение"},
        body=_body(
            "🔗 Подключение\n\n{state}\n\nОткройте страницу подключения: там приложение и инструкция "
            "для вашего устройства. Ссылку можно скопировать или показать QR-кодом.",
        ),
        buttons=(
            _btn("devices", "📱 Устройства", _to(DEVICES), row=3, visible_if=_LIVE),
            _btn(
                "reissue",
                "♻️ Перевыпустить ссылку",
                _to(REISSUE),
                row=3,
                sort=1,
                visible_if=_LIVE,
            ),
            _MENU,
        ),
    ),
    SeedScreen(
        code=DEVICES,
        title={"ru": "Устройства"},
        body=_body(
            "📱 Устройства: {count} из {limit}\n\n{list}{note}",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=DEVICES_RESET,
        title={"ru": "Сброс устройств"},
        body=_body(
            "🧹 Отвязать все устройства ({count})?\n\n"
            "На каждом устройстве подписку придётся добавить заново.",
        ),
    ),
    SeedScreen(
        code=REISSUE,
        title={"ru": "Перевыпуск ссылки"},
        body=_body(
            "♻️ Перевыпустить ссылку?\n\nСтарая ссылка перестанет работать на всех устройствах, "
            "подписку придётся добавить заново. Делайте это, если ссылка попала к чужим.",
        ),
    ),
    SeedScreen(
        code=REISSUE_DONE,
        title={"ru": "Ссылка перевыпущена"},
        body=_body(
            "✅ Новая ссылка готова\n\nДобавьте подписку на устройства заново, кнопка ниже.",
        ),
    ),
    SeedScreen(
        code=REISSUE_WAIT,
        title={"ru": "Перевыпускаем ссылку"},
        body=_body(
            "♻️ Перевыпускаем ссылку…\n\nЭто займёт несколько секунд, сообщение обновится само.",
        ),
    ),
    SeedScreen(
        code=TRIAL_STARTED,
        title={"ru": "Пробный период"},
        body=_body(
            "🎁 Пробный период на {days} дн. активирован!\n\nПодключаем, это займёт несколько секунд. "
            "Сообщение обновится само.",
        ),
    ),
    SeedScreen(
        code=TRIAL_DONE,
        title={"ru": "Пробный период готов"},
        body=_body(
            "✅ Готово! Пробный период до {until}\n\nНажмите «Подключиться» и следуйте инструкции.",
        ),
    ),
    SeedScreen(
        code=CHANNEL,
        title={"ru": "Канал"},
        body=_body(
            "📣 Подпишитесь на наш канал\n\nПосле подписки нажмите «Я подписался».",
        ),
    ),
    SeedScreen(
        code=CAPTCHA,
        title={"ru": "Проверка на бота"},
        body=_body(
            "Проверим, что вы не бот\n\nНажмите на {emoji}",
        ),
    ),
    SeedScreen(
        code="notify_expiring",
        title={"ru": "Скоро закончится"},
        body=_body(
            "⏳ Подписка закончится через {left}\n\nОна действует до {until}. Продлите заранее, "
            "чтобы VPN не отключился.",
        ),
    ),
    SeedScreen(
        code="notify_expired",
        title={"ru": "Закончилась"},
        body=_body(
            "⌛ Подписка закончилась\n\nПродлите её: доступ вернётся сразу после оплаты, ссылка останется "
            "прежней.",
        ),
    ),
    SeedScreen(
        code="notify_trial_ending",
        title={"ru": "Триал заканчивается"},
        body=_body(
            "🎁 Пробный период закончится через {left}\n\nПонравилось? Оформите подписку, "
            "подключение останется прежним.",
        ),
    ),
    SeedScreen(
        code="notify_traffic",
        title={"ru": "Трафик"},
        body=_body(
            "📊 Использовано {percent}% трафика\n\n{used} из {limit}.",
        ),
    ),
    SeedScreen(
        code="notify_limited",
        title={"ru": "Трафик закончился"},
        body=_body(
            "🚫 Трафик закончился\n\nИспользовано {used} из {limit}. Продлите подписку, чтобы продолжить.",
        ),
    ),
    SeedScreen(
        code="notify_first_connected",
        title={"ru": "Подключение работает"},
        body=_body(
            "✅ Подключение работает!\n\nVPN настроен. Если что-то пойдёт не так, напишите в поддержку.",
        ),
    ),
    SeedScreen(
        code="notify_device_added",
        title={"ru": "Новое устройство"},
        body=_body(
            "📱 Новое устройство: {device}\n\nЕсли это не вы, удалите его и перевыпустите ссылку.",
        ),
    ),
    SeedScreen(
        code="notify_revoked",
        title={"ru": "Ссылка обновлена"},
        body=_body(
            "♻️ Ссылка подписки обновлена\n\nСтарая больше не работает, добавьте подписку на устройства "
            "заново.",
        ),
    ),
)

SEEDS: Final[Mapping[str, SeedScreen]] = MappingProxyType({s.code: s for s in USER_SCREENS})

_HOME_NOW: Final = {b.system_key: b for b in SEEDS[HOME].buttons}


def _relayout() -> tuple[tuple[SeedButton, SeedButton | None], ...]:
    """v1, v2 and v3 fingerprints → the current seed (``None``: the button left home). A row equal to any
    version moves (v2 «Подключиться» is already where it belongs and is not listed)."""
    old = [*_HOME_V1, *(b for b in HOME_V2 if b.system_key != "connect"), *HOME_V3]
    return tuple((b, _HOME_NOW.get(b.system_key)) for b in old)


#: ``(old seed, new seed or None)`` of the home buttons, applied on start to rows the owner never touched
#: (``svbg.content.editing.relayout_system_buttons``): v1 «Купить», «Продлить» and «Устройства» and the old
#: «📱 Подписка» leave home (they live in «👤 Профиль»), «👤 Профиль» takes the colour and the time left of
#: «Подписка», the rest takes the current rows. An install without a profile row gets the new one by seeding.
HOME_RELAYOUT: Final[tuple[tuple[SeedButton, SeedButton | None], ...]] = _relayout()

_PROFILE_NOW: Final = {b.system_key: b for b in SEEDS[PROFILE].buttons}

#: The same for the profile of v3: its «📱 Подписка» goes away, the moved buttons take their new rows.
PROFILE_RELAYOUT: Final[tuple[tuple[SeedButton, SeedButton | None], ...]] = tuple(
    (b, _PROFILE_NOW.get(b.system_key) if b.system_key != "sub" else None) for b in _PROFILE_V3
)


def seed_text(code: str, _lang: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Text and entities of a seed; ``("", [])`` for an unknown code."""
    seed = SEEDS.get(code)
    if seed is None:
        return "", []
    block = seed.body.get("ru") or next(iter(seed.body.values()), {})
    return str(block.get("text") or ""), [dict(e) for e in block.get("entities") or ()]
