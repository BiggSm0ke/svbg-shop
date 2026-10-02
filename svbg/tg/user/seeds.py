"""Default content of the user path: system screens as data (07 §2.4.1), import-light (no aiogram, no SQL).

Every user screen is a *content* screen with a code: the owner may change its text, add buttons and reorder or
recolour the system ones (``system_key``). Screens are rendered by the code routes of :mod:`svbg.tg.user`
with screen-specific ``{placeholders}`` (see ``PLACEHOLDERS``); when the content store has no such screen yet
(a fresh database before seeding, a deleted row) the very same seed below is the fallback, so the bot never
shows an empty message.

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
    "LANG",
    "NOTICE_PREFIX",
    "PAY_DETAILS",
    "PAY_INVOICE",
    "PAY_WAIT",
    "PLACEHOLDERS",
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
LANG: Final = "lang"
#: The «Подписка» section: status, plan, time left, traffic, devices (``sub``) or «нет подписки»
#: (``sub_none``).
SUB: Final = "sub"
SUB_NONE: Final = "sub_none"
#: Placeholder of the home «📱 Подписка» button: the time left («12 дн.», «2 дн. 5 ч», «закончилась»); empty
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


def _body(ru: str, en: str) -> dict[str, dict[str, Any]]:
    return {"ru": _b(ru), "en": _b(en)}


def _btn(
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


_MENU: Final = _btn("home", "🏠 Меню", "🏠 Menu", _to(HOME), row=9)
_NO_PAID: Final = {"sub": ["none", "trial"]}
_PAID: Final = {"sub": ["active", "expired", "frozen"]}
_LIVE: Final = {"sub": ["trial", "active"]}
_TRIAL: Final = {"type": "system", "name": "trial"}

#: The home buttons as they were seeded before the «Подписка» section. A row that still equals its old seed
#: (the owner never touched it) takes the new layout on start
#: (``svbg.content.editing.relayout_system_buttons``); «Купить», «Продлить» and «Устройства» leave home,
#: they live in «Подписка» now.
_HOME_V1: Final = (
    _btn("buy", "🛒 Купить подписку", "🛒 Buy", _to(BUY), style="success", visible_if=_NO_PAID),
    _btn("renew", "🔄 Продлить", "🔄 Renew", _to(BUY), style="success", visible_if=_PAID),
    _btn("connect", "🔗 Подключиться", "🔗 Connect", _to(CONNECT), row=1, style="primary", visible_if=_LIVE),
    _btn(
        "trial",
        "🎁 Попробовать бесплатно",
        "🎁 Free trial",
        _TRIAL,
        row=1,
        style="success",
        visible_if={"flag:trial": True},
    ),
    _btn("devices", "📱 Устройства", "📱 Devices", _to(DEVICES), row=2, visible_if=_LIVE),
    _btn("balance", "💰 Баланс", "💰 Balance", _to(BALANCE), row=2, sort=1),
    _btn("lang", "🌐 Язык", "🌐 Language", _to(LANG), row=3),
)

#: Placeholders each screen understands (besides ``{balance}`` and ``{days_left}`` available everywhere).
PLACEHOLDERS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        HOME: ("status", "plan", "until", "devices", "name"),
        SUB: ("status", "plan", "left", "until", "traffic", "devices", "servers"),
        SUB_NONE: ("trial",),
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
        LANG: (),
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
        title={"ru": "Главная", "en": "Home"},
        body=_body(
            "👋 Привет, {name}!\n\n{status}\nБаланс: {balance}",
            "👋 Hi, {name}!\n\n{status}\nBalance: {balance}",
        ),
        # Rows as in the Bedolaga main menu: connect, balance, trial, the subscription, then promo + invite
        # (row 4) and information + language (row 5; the first ones are module buttons of
        # svbg.content.defaults), support (drawn by the code) and the staff «🛠 Админка» (row 9).
        buttons=(
            _btn("connect", "🔗 Подключиться", "🔗 Connect", _to(CONNECT), style="primary", visible_if=_LIVE),
            _btn("balance", "💰 Баланс: {balance}", "💰 Balance: {balance}", _to(BALANCE), row=1),
            _btn(
                "trial",
                "🎁 Попробовать бесплатно",
                "🎁 Free trial",
                _TRIAL,
                row=2,
                style="success",
                visible_if={"flag:trial": True},
            ),
            _btn("sub", "📱 Подписка · {left}", "📱 Subscription · {left}", _to(SUB), row=3),
            _btn("lang", "🌐 Язык", "🌐 Language", _to(LANG), row=5, sort=1),
            # staff: one «🛠 Админка» (svbg.content.defaults.ADMIN_BUTTON); the old «⚙️ Настройки» is retired
        ),
    ),
    SeedScreen(
        code=SUB,
        title={"ru": "Подписка", "en": "Subscription"},
        body=_body(
            "📱 Подписка\n\nСтатус: {status}\nТариф: {plan}\nОсталось: {left}\nДействует до: {until}\n"
            "Трафик: {traffic}\nУстройства: {devices}{servers}",
            "📱 Subscription\n\nStatus: {status}\nPlan: {plan}\nTime left: {left}\nValid until: {until}\n"
            "Traffic: {traffic}\nDevices: {devices}{servers}",
        ),
        buttons=(
            _btn("connect", "🔗 Подключиться", "🔗 Connect", _to(CONNECT), style="primary", visible_if=_LIVE),
            _btn(
                "renew",
                "🔄 Продлить",
                "🔄 Renew",
                {"type": "system", "name": "renew"},
                row=1,
                style="success",
                visible_if=_PAID,
            ),
            _btn(
                "change",
                "📦 Сменить тариф",
                "📦 Change plan",
                _to(BUY),
                row=1,
                sort=1,
                visible_if={"sub": ["active", "expired"]},
            ),
            _btn(
                "buy", "🛒 Купить подписку", "🛒 Buy", _to(BUY), row=1, style="success", visible_if=_NO_PAID
            ),
            _btn("devices", "📱 Устройства", "📱 Devices", _to(DEVICES), row=2, visible_if=_LIVE),
            _MENU,
        ),
    ),
    SeedScreen(
        code=SUB_NONE,
        title={"ru": "Подписка (нет)", "en": "Subscription (none)"},
        body=_body(
            "📱 Подписка\n\nСейчас подписки нет. Нажмите «Купить подписку» и выберите тариф и срок.{trial}",
            "📱 Subscription\n\nNo subscription right now. Tap «Buy» and pick a plan and a period.{trial}",
        ),
        buttons=(
            _btn("buy", "🛒 Купить подписку", "🛒 Buy", _to(BUY), style="success"),
            _btn(
                "trial",
                "🎁 Попробовать бесплатно",
                "🎁 Free trial",
                _TRIAL,
                row=1,
                visible_if={"flag:trial": True},
            ),
            _MENU,
        ),
    ),
    SeedScreen(
        code=BUY,
        title={"ru": "Покупка", "en": "Buy"},
        body=_body("🛒 Выберите тариф", "🛒 Choose a plan"),
        buttons=(_btn("back", "◀️ Назад", "◀️ Back", _to(SUB), row=8), _MENU),
    ),
    SeedScreen(
        code=BUY_PLAN,
        title={"ru": "Срок", "en": "Period"},
        body=_body(
            "📦 {plan}\nУстройств: {devices} · Трафик: {traffic}\n\nВыберите срок. Чем дольше, тем выгоднее:",
            "📦 {plan}\nDevices: {devices} · Traffic: {traffic}\n\nChoose a period. The longer, the cheaper:",
        ),
    ),
    SeedScreen(
        code=CHECKOUT,
        title={"ru": "Оформление", "en": "Checkout"},
        body=_body(
            "🧾 Проверьте заказ\n\nТариф: {plan}\nСрок: {period}\nДействует до: {until}\n"
            "Цена: {price}\nНа балансе: {balance}\n\n{pay_line}",
            "🧾 Check your order\n\nPlan: {plan}\nPeriod: {period}\nValid until: {until}\n"
            "Price: {price}\nBalance: {balance}\n\n{pay_line}",
        ),
    ),
    SeedScreen(
        code=PAY_WAIT,
        title={"ru": "Оформляю", "en": "Processing"},
        body=_body(
            "⏳ Оформляю подписку…\n\nЭто займёт несколько секунд, сообщение обновится само.",
            "⏳ Activating your subscription…\n\n"
            "This takes a few seconds. The message will update on its own.",
        ),
    ),
    SeedScreen(
        code=SHORTFALL,
        title={"ru": "Не хватает", "en": "Not enough"},
        body=_body(
            "💳 Не хватает {missing}\n\nЦена: {price}, на балансе: {balance}.\n"
            "Пополните баланс, и подписка оформится сразу после оплаты.{surplus_note}",
            "💳 {missing} short\n\nPrice: {price}, balance: {balance}.\n"
            "Top up and the purchase goes through as soon as the payment arrives.{surplus_note}",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=PAY_INVOICE,
        title={"ru": "Счёт", "en": "Invoice"},
        body=_body(
            "🧾 Счёт на {amount} готов\n\nОплатите по кнопке ниже. {after}",
            "🧾 Invoice for {amount} is ready\n\nPay with the button below. {after}",
        ),
    ),
    SeedScreen(
        code=PAY_DETAILS,
        title={"ru": "Перевод", "en": "Transfer"},
        body=_body(
            "🏦 Перевод на {amount}\n\n{details}\n\nПосле перевода пришлите сюда фото или PDF чека.",
            "🏦 Transfer of {amount}\n\n{details}\n\n"
            "After the transfer, send a photo or PDF of the receipt here.",
        ),
    ),
    SeedScreen(
        code=BALANCE,
        title={"ru": "Баланс", "en": "Balance"},
        body=_body(
            "💰 Баланс: {balance}\n\nС баланса оплачивается подписка. Выберите сумму пополнения:",
            "💰 Balance: {balance}\n\nThe subscription is paid from the balance. Choose an amount:",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=TOPUP,
        title={"ru": "Пополнение", "en": "Top-up"},
        body=_body(
            "💳 Пополнение на {amount}\n\nВыберите способ оплаты:{surplus_note}",
            "💳 Top-up of {amount}\n\nChoose a payment method:{surplus_note}",
        ),
    ),
    SeedScreen(
        code=CONNECT,
        title={"ru": "Подключение", "en": "Connect"},
        body=_body(
            "🔗 Подключение\n\n{state}\n\nОткройте страницу подключения: там приложение и инструкция "
            "для вашего устройства. Ссылку можно скопировать или показать QR-кодом.",
            "🔗 Connect\n\n{state}\n\nOpen the connection page: it has the app and instructions for your "
            "device. You can also copy the link or show it as a QR code.",
        ),
        buttons=(
            _btn("devices", "📱 Устройства", "📱 Devices", _to(DEVICES), row=3, visible_if=_LIVE),
            _btn(
                "reissue",
                "♻️ Перевыпустить ссылку",
                "♻️ New link",
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
        title={"ru": "Устройства", "en": "Devices"},
        body=_body(
            "📱 Устройства: {count} из {limit}\n\n{list}{note}",
            "📱 Devices: {count} of {limit}\n\n{list}{note}",
        ),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code=DEVICES_RESET,
        title={"ru": "Сброс устройств", "en": "Reset devices"},
        body=_body(
            "🧹 Отвязать все устройства ({count})?\n\n"
            "На каждом устройстве подписку придётся добавить заново.",
            "🧹 Unlink all devices ({count})?\n\nYou will have to add the subscription on each device again.",
        ),
    ),
    SeedScreen(
        code=REISSUE,
        title={"ru": "Перевыпуск ссылки", "en": "New link"},
        body=_body(
            "♻️ Перевыпустить ссылку?\n\nСтарая ссылка перестанет работать на всех устройствах, "
            "подписку придётся добавить заново. Делайте это, если ссылка попала к чужим.",
            "♻️ Issue a new link?\n\nThe old link stops working on every device, so you will have to add the "
            "subscription again. Do this if the link got into the wrong hands.",
        ),
    ),
    SeedScreen(
        code=REISSUE_DONE,
        title={"ru": "Ссылка перевыпущена", "en": "Link renewed"},
        body=_body(
            "✅ Новая ссылка готова\n\nДобавьте подписку на устройства заново, кнопка ниже.",
            "✅ The new link is ready\n\nAdd the subscription to your devices again with the button below.",
        ),
    ),
    SeedScreen(
        code=REISSUE_WAIT,
        title={"ru": "Перевыпускаем ссылку", "en": "Renewing the link"},
        body=_body(
            "♻️ Перевыпускаем ссылку…\n\nЭто займёт несколько секунд, сообщение обновится само.",
            "♻️ Renewing your link…\n\nThis takes a few seconds. The message will update on its own.",
        ),
    ),
    SeedScreen(
        code=TRIAL_STARTED,
        title={"ru": "Пробный период", "en": "Trial"},
        body=_body(
            "🎁 Пробный период на {days} дн. активирован!\n\nПодключаем, это займёт несколько секунд. "
            "Сообщение обновится само.",
            "🎁 Your {days}-day trial is on!\n\nSetting it up, this takes a few seconds. "
            "The message will update on its own.",
        ),
    ),
    SeedScreen(
        code=TRIAL_DONE,
        title={"ru": "Пробный период готов", "en": "Trial ready"},
        body=_body(
            "✅ Готово! Пробный период до {until}\n\nНажмите «Подключиться» и следуйте инструкции.",
            "✅ Done! Trial until {until}\n\nTap «Connect» and follow the instructions.",
        ),
    ),
    SeedScreen(
        code=CHANNEL,
        title={"ru": "Канал", "en": "Channel"},
        body=_body(
            "📣 Подпишитесь на наш канал\n\nПосле подписки нажмите «Я подписался».",
            "📣 Join our channel\n\nThen tap «I have joined».",
        ),
    ),
    SeedScreen(
        code=CAPTCHA,
        title={"ru": "Проверка на бота", "en": "Bot check"},
        body=_body(
            "Проверим, что вы не бот\n\nНажмите на {emoji}",
            "Quick check that you're not a bot\n\nTap {emoji}",
        ),
    ),
    SeedScreen(
        code=LANG,
        title={"ru": "Язык", "en": "Language"},
        body=_body("🌐 Выберите язык", "🌐 Choose a language"),
        buttons=(_MENU,),
    ),
    SeedScreen(
        code="notify_expiring",
        title={"ru": "Скоро закончится", "en": "Expiring soon"},
        body=_body(
            "⏳ Подписка закончится через {left}\n\nОна действует до {until}. Продлите заранее, "
            "чтобы VPN не отключился.",
            "⏳ Your subscription ends in {left}\n\nIt is valid until {until}. Renew in advance so the VPN "
            "keeps working.",
        ),
    ),
    SeedScreen(
        code="notify_expired",
        title={"ru": "Закончилась", "en": "Expired"},
        body=_body(
            "⌛ Подписка закончилась\n\nПродлите её: доступ вернётся сразу после оплаты, ссылка останется "
            "прежней.",
            "⌛ Your subscription has ended\n\nRenew it and access comes back as soon as you pay. The link "
            "stays the same.",
        ),
    ),
    SeedScreen(
        code="notify_trial_ending",
        title={"ru": "Триал заканчивается", "en": "Trial ending"},
        body=_body(
            "🎁 Пробный период закончится через {left}\n\nПонравилось? Оформите подписку, "
            "подключение останется прежним.",
            "🎁 Your trial ends in {left}\n\nLiked it? Subscribe and your connection stays the same.",
        ),
    ),
    SeedScreen(
        code="notify_traffic",
        title={"ru": "Трафик", "en": "Traffic"},
        body=_body(
            "📊 Использовано {percent}% трафика\n\n{used} из {limit}.",
            "📊 {percent}% of traffic used\n\n{used} of {limit}.",
        ),
    ),
    SeedScreen(
        code="notify_limited",
        title={"ru": "Трафик закончился", "en": "Out of traffic"},
        body=_body(
            "🚫 Трафик закончился\n\nИспользовано {used} из {limit}. Продлите подписку, чтобы продолжить.",
            "🚫 Traffic is over\n\n{used} of {limit} used. Renew your subscription to keep going.",
        ),
    ),
    SeedScreen(
        code="notify_first_connected",
        title={"ru": "Подключение работает", "en": "Connected"},
        body=_body(
            "✅ Подключение работает!\n\nVPN настроен. Если что-то пойдёт не так, напишите в поддержку.",
            "✅ You are connected!\n\nThe VPN is set up. If something goes wrong, contact support.",
        ),
    ),
    SeedScreen(
        code="notify_device_added",
        title={"ru": "Новое устройство", "en": "New device"},
        body=_body(
            "📱 Новое устройство: {device}\n\nЕсли это не вы, удалите его и перевыпустите ссылку.",
            "📱 New device: {device}\n\nIf it was not you, remove it and issue a new link.",
        ),
    ),
    SeedScreen(
        code="notify_revoked",
        title={"ru": "Ссылка обновлена", "en": "Link updated"},
        body=_body(
            "♻️ Ссылка подписки обновлена\n\nСтарая больше не работает, добавьте подписку на устройства "
            "заново.",
            "♻️ Your subscription link was renewed\n\nThe old one no longer works, so add the subscription to "
            "your devices again.",
        ),
    ),
)

SEEDS: Final[Mapping[str, SeedScreen]] = MappingProxyType({s.code: s for s in USER_SCREENS})

_HOME_NOW: Final = {b.system_key: b for b in SEEDS[HOME].buttons}
#: ``(old seed, new seed or None)`` of the home buttons: see ``_HOME_V1``.
HOME_RELAYOUT: Final[tuple[tuple[SeedButton, SeedButton | None], ...]] = tuple(
    (old, _HOME_NOW.get(old.system_key)) for old in _HOME_V1
)


def seed_text(code: str, lang: str) -> tuple[str, list[dict[str, Any]]]:
    """Text and entities of a seed in ``lang`` (Russian fallback); ``("", [])`` for an unknown code."""
    seed = SEEDS.get(code)
    if seed is None:
        return "", []
    block = seed.body.get(lang) or seed.body.get("ru") or {}
    return str(block.get("text") or ""), [dict(e) for e in block.get("entities") or ()]
