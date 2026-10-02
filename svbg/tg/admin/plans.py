"""«📦 Тарифы» — the minimal plan editor (04 §7, §9.1; 02 §3.3; 06 M1).

Screens (code-defined, on the :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``plans`` — the list (status icon, name, price range); «➕ Новый тариф», «📍 Локации», the preset
  «Стандарт 179–1699 ₽» while there is no ``standard`` plan;
* ``pl`` — the card of one plan: status, availability, prices, devices (+ paid extra devices), traffic and its
  reset strategy, squads, panel tag; edit buttons;
* ``pl.pr`` prices (add/change a period, highlight, delete), ``pl.sq`` squads as checkboxes over
  ``locations``, ``pl.av`` availability, ``pl.rs`` traffic reset strategy, ``pl.dv`` devices;
* ``locs`` / ``loc`` — locations (panel squads): the title and flag users see, «🔄 Обновить из панели».

Changing the squads of a plan with live subscriptions asks «Применить к N текущим подписчикам?» (04 §7): «Да»
changes the plan and enqueues the durable job :mod:`svbg.catalog.squads_job` in **one** transaction with the
``admin_audit`` row; «Нет» changes only the plan (new purchases get the new squads; current subscriptions keep
theirs, also on renewal — their ``plan_snapshot``). The confirm buttons carry the plan version: a double click
or a concurrent edit cannot apply a change twice (compare-and-set).

Access (04 §9.1): owner, or admin with the ``plans`` permission — checked by the router on **every** screen,
action and form step; arguments from callbacks are re-validated here (callback data can be forged).
Rendering reads the in-memory catalog snapshot (no SQL); only writes, the subscriber count before a squads
change and «Обновить из панели» (one panel call) do I/O.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message

from svbg.catalog import repo
from svbg.catalog.locations import BROKEN_PREFIX, SquadSource, attention_key, sync_locations
from svbg.catalog.model import (
    GIB,
    MAX_DAYS,
    MAX_DEVICES,
    MAX_TRAFFIC_GB,
    CatalogError,
    DeviceAddon,
    Location,
    Plan,
)
from svbg.catalog.preset import STANDARD_CODE, seed_owner_preset
from svbg.catalog.service import CatalogService, CatalogSnapshot, count_plan_subscribers
from svbg.catalog.squads_job import enqueue_apply_squads
from svbg.content.defaults import HOME
from svbg.core.money import format_money, parse_money
from svbg.remnawave.errors import RemnawaveError
from svbg.remnawave.transport import Lane
from svbg.tg.ui.forms import Field, Form, ValidationError, integer
from svbg.tg.ui.forms import text as text_validator
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "PERM",
    "SCREEN_CARD",
    "SCREEN_LIST",
    "SCREEN_LOCATION",
    "SCREEN_LOCATIONS",
    "PlanScreens",
    "setup",
]

log = logging.getLogger("svbg.tg.admin.plans")

_T_ = TypeVar("_T_")

PERM: Final = "plans"
SCREEN_LIST: Final = "plans"
SCREEN_CARD: Final = "pl"
SCREEN_PRICES: Final = "pl.pr"
SCREEN_SQUADS: Final = "pl.sq"
SCREEN_AVAIL: Final = "pl.av"
SCREEN_RESET: Final = "pl.rs"
SCREEN_DEVICES: Final = "pl.dv"
SCREEN_LOCATIONS: Final = "locs"
SCREEN_LOCATION: Final = "loc"
ACTIONS: Final = "pla"

F_NEW: Final = "pl.f.new"
F_NAME: Final = "pl.f.name"
F_PRICE: Final = "pl.f.price"
F_DEVICES: Final = "pl.f.dev"
F_ADDON: Final = "pl.f.addon"
F_TRAFFIC: Final = "pl.f.trf"
F_TAG: Final = "pl.f.tag"
F_LOC_NAME: Final = "pl.f.lname"
F_LOC_FLAG: Final = "pl.f.lflag"

SYNC_TIMEOUT: Final = 10.0
_ID_RE: Final = re.compile(r"^\d{1,12}$")
_HEX_RE: Final = re.compile(r"^[0-9a-f]{1,16}$")
_SIG_RE: Final = re.compile(r"^[0-9a-f]{6}$")
_UUID_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,40}$")

AVAIL_LABELS: Final = {
    "all": "всем",
    "new": "только новым (ещё не платили)",
    "existing": "только тем, кто уже платил",
    "link": "только по ссылке",
}
RESET_LABELS: Final = {
    "NO_RESET": "без сброса",
    "DAY": "каждый день",
    "WEEK": "каждую неделю",
    "MONTH": "каждый месяц, 1-го числа",
    "MONTH_ROLLING": "каждый месяц от даты подключения",
}

_T: Final[dict[str, str]] = {
    "list_title": "📦 <b>Тарифы</b>",
    "list_hint": "Нажмите на тариф, чтобы изменить его. Покупатели видят только тарифы «в продаже».",
    "list_empty": "Тарифов пока нет. Создайте свой или возьмите готовый пресет.",
    "new": "➕ Новый тариф",
    "preset": "⭐ Пресет «Стандарт» 179–1699 ₽",
    "locations": "📍 Локации",
    "menu": "🏠 Меню",
    "back": "⬅️ Назад",
    "to_list": "⬅️ К тарифам",
    "to_card": "⬅️ К тарифу",
    "st_trial_on": "🎁 Пробный тариф · включён",
    "st_trial_off": "🎁 Пробный тариф · выключен",
    "st_broken": "⚠️ Скрыт: {reason}",
    "st_sale": "🟢 В продаже",
    "st_no_prices": "⚠️ Включён, но без цен в {cur} — покупателям не виден",
    "st_hidden": "⏸ Скрыт из продажи (текущие подписчики продлевают как обычно)",
    "avail": "👥 Доступен: {value}",
    "link": "🔗 Ссылка: <code>{link}</code>",
    "prices": "💰 Цены: {value}",
    "prices_none": "не заданы",
    "devices": "📱 Устройства: {value}",
    "dev_null": "как в панели (лимит по умолчанию)",
    "dev_zero": "без лимита",
    "dev_n": "{n} включено",
    "addon": "💳 Доп. устройство: {price} за {days} дн.{cap}",
    "addon_cap": ", всего до {n}",
    "addon_off": "💳 Доплата за устройства: выключена",
    "traffic": "📶 Трафик: {value} · сброс: {reset}",
    "unlimited": "безлимит",
    "gb": "{n} ГБ",
    "squads": "📍 Сквады: {value}",
    "squads_none": "не выбраны",
    "tag": "🏷 Тег в панели: {value}",
    "tag_none": "нет",
    "card_note": "Изменения лимитов действуют для новых покупок. Сквады можно применить и к текущим "
    "подписчикам.",
    "b_name": "✏️ Название",
    "b_prices": "💰 Цены",
    "b_squads": "📍 Сквады",
    "b_avail": "👥 Доступность",
    "b_devices": "📱 Устройства",
    "b_traffic": "📶 Трафик",
    "b_reset": "🔁 Сброс трафика",
    "b_tag": "🏷 Тег",
    "b_trial_on": "🎁 Сделать пробным",
    "b_trial_off": "🎁 Снять отметку «пробный»",
    "b_enable": "▶️ В продажу",
    "b_enable_trial": "▶️ Включить пробный",
    "b_disable": "⏸ Скрыть из продажи",
    "b_disable_trial": "⏸ Выключить пробный",
    "pr_title": "💰 <b>Цены</b> · {name}",
    "pr_hint": "⭐ — выделенный период на экране покупки. Нажмите на период, чтобы выделить его, "
    "🗑 — удалить.",
    "pr_none": "Цен пока нет.",
    "pr_add": "➕ Добавить или изменить период",
    "sq_title": "📍 <b>Сквады</b> · {name}",
    "sq_hint": "Отметьте локации, которые получит подписка. Нужна хотя бы одна. Выбрано: {n}.",
    "sq_empty": "Локаций пока нет: обновите список из панели.",
    "sq_missing": " (нет в панели)",
    "sq_save": "💾 Сохранить",
    "sq_sync": "🔄 Обновить из панели",
    "sq_need_one": "Выберите хотя бы одну локацию",
    "sq_same": "Без изменений",
    "sq_moved": "Список локаций обновился — отметьте заново",
    "sq_confirm": "📍 <b>Сквады тарифа «{name}» меняются</b>\nБыло: {old}\nСтанет: {new}\n\n"
    "У тарифа <b>{total}</b> текущих подписчиков{manual}.\nПрименить новые сквады к ним?",
    "sq_manual": " (из них {n} с ручной правкой сквадов в панели — их не тронем)",
    "sq_yes": "✅ Да, применить к {n}",
    "sq_no": "🆕 Нет, только новым покупкам",
    "sq_cancel": "✖️ Отмена",
    "sq_applying": "⏳ Применяю новые сквады к {n} подписчикам — прогресс пришлю отдельным сообщением.",
    "sq_kept": "✅ Сохранено. Текущие подписки оставлены как есть, новые сквады получат новые покупки.",
    "sq_saved": "✅ Сквады сохранены.",
    "sq_all_manual": "✅ Сохранено. У всех текущих подписок сквады правили вручную в панели — их не трогаем.",
    "av_title": "👥 <b>Кому показывать</b> · {name}",
    "av_hint": "«Только по ссылке» — тариф виден лишь тем, кто пришёл по его ссылке.",
    "rs_title": "🔁 <b>Сброс трафика</b> · {name}",
    "rs_hint": "Когда панель обнуляет израсходованный трафик.",
    "dv_title": "📱 <b>Устройства</b> · {name}",
    "dv_set": "✏️ Сколько устройств включено",
    "dv_null": "↩️ Как в панели",
    "dv_addon": "💳 Доплата за доп. устройство",
    "dv_addon_off": "🚫 Выключить доплату",
    "dv_addon_hint": "Доплата доступна, когда в тариф включено конкретное число устройств.",
    "loc_title": "📍 <b>Локации</b>",
    "loc_hint": "Это сквады панели. Название и флаг видят покупатели.",
    "loc_none": "Локаций пока нет: нажмите «🔄 Обновить из панели».",
    "loc_members": "{n} польз.",
    "loc_gone": "⚠️ нет в панели",
    "loc_card": "📍 <b>{label}</b>\nВ панели: {panel}\nUUID: <code>{uuid}</code>\nПользователей: {members}\n"
    "Тарифы: {plans}",
    "loc_rename": "✏️ Название",
    "loc_flag": "🏳️ Флаг",
    "loc_noflag": "🚫 Убрать флаг",
    "sync_off": "Панель не подключена — подключите её в /setup",
    "sync_timeout": "Панель не ответила вовремя, попробуйте ещё раз",
    "sync_error": "Панель ответила ошибкой: {error}",
    "sync_done": "🔄 Обновлено: локаций {total}, новых {added}, пропало {gone}, вернулось {back}.",
    "sync_guard": "⚠️ Панель вернула пустой список сквадов — ничего не помечено как удалённое.",
    "saved": "✅ Сохранено",
    "deleted": "🗑 Удалено",
    "not_found": "Тариф не найден",
    "loc_not_found": "Локация не найдена",
    "need_prices": "Сначала добавьте хотя бы одну цену",
    "need_squads": "Сначала выберите сквады тарифа",
    "preset_done": "⭐ Пресет добавлен{hidden}.",
    "preset_hidden": " — выберите сквады и включите продажу",
    "preset_exists": "Тариф «Стандарт» уже есть",
    "preset_rub": "Пресет — в рублях, а валюта магазина другая. Создайте тариф вручную.",
    "f_new": "✏️ Название нового тарифа (например, «Стандарт»):",
    "f_name": "✏️ Новое название тарифа:",
    "f_days": "📅 Период в днях (например, 30):",
    "f_amount": "💰 Цена за период в {cur} (например, 179):",
    "f_dev": "📱 Сколько устройств включено в тариф? 0 — без лимита.",
    "f_addon_price": "💳 Цена одного доп. устройства за 30 дней в {cur} (например, 19):",
    "f_addon_max": "📱 Сколько устройств всего можно иметь вместе с доплатой? (например, 15; "
    "«Пропустить» — без "
    "ограничения)",
    "f_traffic": "📶 Лимит трафика в ГБ за период сброса. 0 — безлимит.",
    "f_tag": "🏷 Тег в панели: A–Z, 0–9 и «_», до 16 символов (например, PAID). «-» — убрать тег.",
    "f_loc_name": "✏️ Название локации для покупателей (например, «Нидерланды»):",
    "f_loc_flag": "🏳️ Флаг локации — один эмодзи (например, 🇳🇱):",
}


class _Stop(Exception):  # control flow, not an error
    def __init__(self, result: HandlerResult) -> None:
        super().__init__("stop")
        self.result = result


# ------------------------------------------------------------------------------------------- formatting


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def format_traffic(traffic_bytes: int) -> str:
    if traffic_bytes == 0:
        return _T["unlimited"]
    gb = traffic_bytes / GIB
    n = f"{gb:.0f}" if gb == int(gb) else f"{gb:.1f}".replace(".", ",")
    return _T["gb"].format(n=n)


def format_devices(limit: int | None) -> str:
    if limit is None:
        return _T["dev_null"]
    if limit == 0:
        return _T["dev_zero"]
    return _T["dev_n"].format(n=limit)


def _money(amount: int, currency: str) -> str:
    try:
        return format_money(amount, currency, "ru")
    except (ValueError, KeyError):
        return f"{amount} {currency}"


def price_range(plan: Plan, currency: str) -> str:
    own = plan.prices_in(currency)
    if not own:
        return ""
    lo = min(p.amount_minor for p in own)
    hi = max(p.amount_minor for p in own)
    return _money(lo, currency) if lo == hi else f"{_money(lo, currency)}–{_money(hi, currency)}"


def status_icon(plan: Plan, currency: str) -> str:
    if plan.is_trial:
        return "🎁"
    if plan.broken:
        return "⚠️"
    if not plan.enabled:
        return "⏸"
    return "🟢" if plan.prices_in(currency) else "⚠️"


def _parse_ids(arg: Any, n: int) -> list[str] | None:
    """``arg`` split by ``:`` into exactly ``n`` parts (``None`` for anything else)."""
    if not isinstance(arg, str):
        return None
    parts = arg.split(":")
    return parts if len(parts) == n else None


class PlanScreens:
    """Registers the plan editor on a router. ``currency`` — the shop currency (``CURRENCY``);
    ``squad_source`` — the panel API for «Обновить из панели» (``None`` while the panel is not connected)."""

    def __init__(
        self,
        router: ScreenRouter,
        catalog: CatalogService,
        *,
        db: Database | None = None,
        currency: Callable[[], str] = lambda: "RUB",
        squad_source: Callable[[], SquadSource | None] | None = None,
        attention: AttentionService | None = None,
        lang: str = "ru",
    ) -> None:
        self.router = router
        self.catalog = catalog
        self.db: Database = db if db is not None else catalog.db
        self.currency = currency
        self.squad_source = squad_source
        self.attention = attention
        self.lang = lang
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard = {"required_role": "admin", "perm": PERM}
        for code, fn in (
            (SCREEN_LIST, self._list_screen),
            (SCREEN_CARD, self._card_screen),
            (SCREEN_PRICES, self._prices_screen),
            (SCREEN_SQUADS, self._squads_screen),
            (SCREEN_AVAIL, self._avail_screen),
            (SCREEN_RESET, self._reset_screen),
            (SCREEN_DEVICES, self._devices_screen),
            (SCREEN_LOCATIONS, self._locations_screen),
            (SCREEN_LOCATION, self._location_screen),
        ):
            r.screen(code, **guard)(self._wrap(fn))
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "new": self._a_new,
            "preset": self._a_preset,
            "name": self._a_name,
            "padd": self._a_price_add,
            "pdel": self._a_price_delete,
            "phl": self._a_price_highlight,
            "sqs": self._a_squads_save,
            "sqy": self._a_squads_yes,
            "sqn": self._a_squads_no,
            "av": self._a_availability,
            "rs": self._a_reset,
            "dev": self._a_devices,
            "dnull": self._a_devices_null,
            "addon": self._a_addon,
            "aoff": self._a_addon_off,
            "trf": self._a_traffic,
            "tag": self._a_tag,
            "trial": self._a_trial,
            "en": self._a_enabled,
            "sync": self._a_sync,
            "lname": self._a_location_name,
            "lflag": self._a_location_flag,
            "lnof": self._a_location_noflag,
        }
        for name, fn in actions.items():
            r.action(ACTIONS, name, **guard)(self._wrap(fn))
        cur = self.currency
        forms = (
            Form(F_NEW, (Field("name", _T["f_new"], text_validator(max_len=64)),), self._f_new),
            Form(F_NAME, (Field("name", _T["f_name"], text_validator(max_len=64)),), self._f_name),
            Form(
                F_PRICE,
                (
                    Field("days", _T["f_days"], integer(min_value=1, max_value=MAX_DAYS)),
                    Field("amount", _T["f_amount"].format(cur=cur()), _money_validator(cur)),
                ),
                self._f_price,
            ),
            Form(
                F_DEVICES,
                (Field("n", _T["f_dev"], integer(min_value=0, max_value=MAX_DEVICES)),),
                self._f_dev,
            ),
            Form(
                F_ADDON,
                (
                    Field("price", _T["f_addon_price"].format(cur=cur()), _money_validator(cur)),
                    Field(
                        "max", _T["f_addon_max"], integer(min_value=1, max_value=MAX_DEVICES), optional=True
                    ),
                ),
                self._f_addon,
            ),
            Form(
                F_TRAFFIC,
                (Field("gb", _T["f_traffic"], integer(min_value=0, max_value=MAX_TRAFFIC_GB)),),
                self._f_traffic,
            ),
            Form(F_TAG, (Field("tag", _T["f_tag"], _tag_validator),), self._f_tag),
            Form(
                F_LOC_NAME, (Field("title", _T["f_loc_name"], text_validator(max_len=48)),), self._f_loc_name
            ),
            Form(F_LOC_FLAG, (Field("flag", _T["f_loc_flag"], _flag_validator),), self._f_loc_flag),
        )
        for form in forms:
            r.form(
                Form(
                    form.name,
                    form.fields,
                    on_done=self._wrap(form.on_done),
                    on_cancel=self._cancel,
                    required_role="admin",
                    perm=PERM,
                )
            )

    def _wrap(
        self, fn: Callable[[ScreenCtx, Any], Awaitable[Any]]
    ) -> Callable[[ScreenCtx, Any], Awaitable[Any]]:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result

        run.__name__ = getattr(fn, "__name__", "plans_handler")
        return run

    def aiogram_router(self, name: str = "svbg-plans") -> Router:
        """``/plans`` in a private chat (owner, admin with ``plans``)."""
        router = Router(name=name)

        async def on_plans(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        router.message.register(on_plans, Command("plans"))
        return router

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        try:
            user = await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for /plans")
            return False
        if user is None or not (user.at_least("admin") and user.has_perm(PERM)):
            return False
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    # ------------------------------------------------------------ helpers

    @property
    def snap(self) -> CatalogSnapshot:
        return self.catalog.snapshot

    def _plan(self, arg: Any, *, screen: bool = True) -> Plan:
        plan = self.snap.plan(int(arg)) if isinstance(arg, str) and _ID_RE.match(arg) else None
        if plan is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]) if screen else Toast(_T["not_found"]))
        return plan

    def _plan_days(self, arg: Any) -> tuple[Plan, int]:
        parts = _parse_ids(arg, 2)
        if parts is None or not _ID_RE.match(parts[1]):
            raise _Stop(Toast(_T["not_found"]))
        return self._plan(parts[0], screen=False), int(parts[1])

    @staticmethod
    def _actor(ctx: ScreenCtx) -> repo.Actor:
        return repo.Actor(ctx.user.user_id, ctx.user.role)

    async def _write(
        self, fn: Callable[[AsyncConnection], Awaitable[_T_]], *, stale_plan: int | None = None
    ) -> _T_:
        """Run ``fn`` in one transaction, reload the snapshot; a rule violation becomes an alert toast, a
        lost compare-and-set (``stale_plan``) re-opens the plan card."""
        try:
            async with self.db.tx() as conn:
                result = await fn(conn)
        except CatalogError as e:
            await self.catalog.reload()
            if stale_plan is not None and isinstance(e, repo.StalePlanError):
                raise _Stop(Redirect(SCREEN_CARD, str(stale_plan), toast=str(e))) from None
            raise _Stop(Toast(str(e), alert=True)) from None
        await self.catalog.changed()
        return result

    async def _edit(self, ctx: ScreenCtx, plan: Plan, back: str = SCREEN_CARD, **changes: Any) -> View:
        await self._write(lambda conn: repo.update_plan(conn, plan.id, actor=self._actor(ctx), **changes))
        return self._card_or(back, plan.id, toast=_T["saved"])

    def _card_or(
        self, screen: str, plan_id: int, *, toast: str | None = None, note: str | None = None
    ) -> View:
        plan = self.snap.plan(plan_id)
        if plan is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]))
        renderers: dict[str, Callable[[Plan], View]] = {
            SCREEN_CARD: self.card_view,
            SCREEN_PRICES: self._prices_view,
            SCREEN_DEVICES: self._devices_view,
            SCREEN_AVAIL: self._avail_view,
            SCREEN_RESET: self._reset_view,
        }
        view = renderers.get(screen, self.card_view)(plan)
        if note:
            view.text = f"{note}\n\n{view.text}"
        view.toast = toast
        return view

    async def _cancel(self, ctx: ScreenCtx) -> HandlerResult:
        return Redirect(SCREEN_LIST)

    # ------------------------------------------------------------ list & card

    async def _list_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return self.list_view()

    def list_view(self) -> View:
        snap, cur = self.snap, self.currency()
        lines = [_T["list_title"], "", _T["list_hint"] if snap.plans else _T["list_empty"]]
        rows: list[list[InlineKeyboardButton]] = []
        for plan in snap.plans:
            prices = price_range(plan, cur)
            label = f"{status_icon(plan, cur)} {plan.title(self.lang)}" + (f" · {prices}" if prices else "")
            rows.append([nav_button(label[:64], SCREEN_CARD, arg=str(plan.id))])
        rows.append([nav_button(_T["new"], ACTIONS, "new")])
        if snap.by_code(STANDARD_CODE) is None and cur == "RUB":  # the owner's preset is in rubles
            rows.append([nav_button(_T["preset"], ACTIONS, "preset")])
        rows.append([nav_button(_T["locations"], SCREEN_LOCATIONS)])
        rows.append([nav_button(_T["menu"], HOME)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self.card_view(self._plan(arg))

    def _status(self, plan: Plan, cur: str) -> str:
        if plan.is_trial:
            return _T["st_trial_on"] if plan.enabled else _T["st_trial_off"]
        if plan.broken:
            return _T["st_broken"].format(reason=_esc(plan.broken_reason or ""))
        if not plan.enabled:
            return _T["st_hidden"]
        return _T["st_sale"] if plan.prices_in(cur) else _T["st_no_prices"].format(cur=cur)

    def card_view(self, plan: Plan) -> View:
        cur, snap = self.currency(), self.snap
        name = _esc(plan.title(self.lang))
        lines = [f"📦 <b>{name}</b> · <code>{plan.code}</code>", self._status(plan, cur)]
        if plan.is_trial and plan.broken:
            lines.append(_T["st_broken"].format(reason=_esc(plan.broken_reason or "")))
        if not plan.is_trial:
            lines.append(_T["avail"].format(value=AVAIL_LABELS.get(plan.availability, plan.availability)))
            if plan.availability == "link":
                bot = self.router.transport.bot_username or "<бот>"
                lines.append(_T["link"].format(link=_esc(f"https://t.me/{bot}?start=plan_{plan.code}")))
            prices = [
                f"{p.days} дн. — {_money(p.amount_minor, cur)}{' ⭐' if p.highlight else ''}"
                for p in plan.prices_in(cur)
            ]
            lines.append(_T["prices"].format(value=" · ".join(prices) or _T["prices_none"]))
        lines.append(_T["devices"].format(value=format_devices(plan.device_limit)))
        addon = plan.addon
        if addon is not None:
            cap = _T["addon_cap"].format(n=addon.max_devices) if addon.max_devices else ""
            lines.append(
                _T["addon"].format(
                    price=_money(addon.price_minor, addon.currency), days=addon.per_days, cap=cap
                )
            )
        elif not plan.is_trial:
            lines.append(_T["addon_off"])
        lines.append(
            _T["traffic"].format(
                value=format_traffic(plan.traffic_bytes),
                reset=RESET_LABELS.get(plan.reset_strategy, plan.reset_strategy),
            )
        )
        squads = snap.squad_labels(plan.squads, self.lang)
        lines.append(_T["squads"].format(value=_esc(", ".join(squads)) if squads else _T["squads_none"]))
        lines.append(_T["tag"].format(value=plan.panel_tag or _T["tag_none"]))
        lines += ["", _T["card_note"]]
        pid = str(plan.id)
        rows: list[list[InlineKeyboardButton]] = [
            [
                nav_button(_T["b_name"], ACTIONS, "name", pid),
                nav_button(_T["b_squads"], SCREEN_SQUADS, arg=pid),
            ],
        ]
        if not plan.is_trial:
            rows.append(
                [
                    nav_button(_T["b_prices"], SCREEN_PRICES, arg=pid),
                    nav_button(_T["b_avail"], SCREEN_AVAIL, arg=pid),
                ]
            )
        rows += [
            [
                nav_button(_T["b_devices"], SCREEN_DEVICES, arg=pid),
                nav_button(_T["b_traffic"], ACTIONS, "trf", pid),
            ],
            [nav_button(_T["b_reset"], SCREEN_RESET, arg=pid), nav_button(_T["b_tag"], ACTIONS, "tag", pid)],
            [nav_button(_T["b_trial_off"] if plan.is_trial else _T["b_trial_on"], ACTIONS, "trial", pid)],
        ]
        if plan.enabled:
            toggle = _T["b_disable_trial"] if plan.is_trial else _T["b_disable"]
        else:
            toggle = _T["b_enable_trial"] if plan.is_trial else _T["b_enable"]
        rows.append([nav_button(toggle, ACTIONS, "en", pid)])
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ create, preset, name

    async def _a_new(self, ctx: ScreenCtx, _arg: Any) -> View:
        return await ctx.start_form(F_NEW)

    async def _f_new(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        name = str(data.get("name") or "")

        async def create(conn: AsyncConnection) -> int:
            return await repo.create_plan(conn, name=name, lang=self.lang, actor=self._actor(ctx))

        plan_id = await self._write(create)
        return self._card_or(SCREEN_CARD, plan_id, note=_T["saved"])

    async def _a_preset(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        if self.snap.by_code(STANDARD_CODE) is not None:
            return Toast(_T["preset_exists"])
        cur = self.currency()
        if cur != "RUB":
            return Toast(_T["preset_rub"], alert=True)

        async def seed(conn: AsyncConnection) -> Any:
            return await seed_owner_preset(conn, currency=cur, actor=self._actor(ctx))

        result = await self._write(seed)
        note = _T["preset_done"].format(hidden="" if result.enabled else _T["preset_hidden"])
        return self._card_or(SCREEN_CARD, result.plan_id, note=note)

    async def _a_name(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_NAME, {"pid": plan.id})

    def _form_plan(self, data: dict[str, Any]) -> Plan:
        pid = data.get("pid")
        plan = self.snap.plan(pid) if isinstance(pid, int) else None
        if plan is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]))
        return plan

    async def _f_name(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan = self._form_plan(data)
        return await self._edit(ctx, plan, name={self.lang: str(data.get("name") or "")})

    # ------------------------------------------------------------ prices

    async def _prices_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._prices_view(self._plan(arg))

    def _prices_view(self, plan: Plan) -> View:
        cur = self.currency()
        own = plan.prices_in(cur)
        lines = [
            _T["pr_title"].format(name=_esc(plan.title(self.lang))),
            _T["pr_hint"] if own else _T["pr_none"],
        ]
        rows: list[list[InlineKeyboardButton]] = []
        for p in own:
            arg = f"{plan.id}:{p.days}"
            label = f"{'⭐' if p.highlight else '☆'} {p.days} дн. — {_money(p.amount_minor, cur)}"
            rows.append([nav_button(label, ACTIONS, "phl", arg), nav_button("🗑", ACTIONS, "pdel", arg)])
        rows.append([nav_button(_T["pr_add"], ACTIONS, "padd", str(plan.id))])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(plan.id))])
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _a_price_add(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_PRICE, {"pid": plan.id})

    async def _f_price(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan, cur = self._form_plan(data), self.currency()
        days, amount = data.get("days"), data.get("amount")
        if not isinstance(days, int) or not isinstance(amount, int):
            return Redirect(SCREEN_PRICES, str(plan.id))

        async def put(conn: AsyncConnection) -> None:
            await repo.set_price(
                conn, plan.id, days=days, amount_minor=amount, currency=cur, actor=self._actor(ctx)
            )

        await self._write(put)
        return self._card_or(SCREEN_PRICES, plan.id, note=_T["saved"])

    async def _a_price_delete(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan, days = self._plan_days(arg)
        cur = self.currency()

        async def drop(conn: AsyncConnection) -> bool:
            return await repo.delete_price(conn, plan.id, days=days, currency=cur, actor=self._actor(ctx))

        await self._write(drop)
        return self._card_or(SCREEN_PRICES, plan.id, toast=_T["deleted"])

    async def _a_price_highlight(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan, days = self._plan_days(arg)
        cur = self.currency()

        async def flip(conn: AsyncConnection) -> bool | None:
            return await repo.toggle_highlight(conn, plan.id, days=days, currency=cur, actor=self._actor(ctx))

        await self._write(flip)
        return self._card_or(SCREEN_PRICES, plan.id, toast=_T["saved"])

    # ------------------------------------------------------------ squads

    def _squads_arg(self, arg: Any) -> tuple[Plan, int, bool]:
        """``pid`` or ``pid:mask:sig`` → (plan, mask, stale). A stale signature resets the mask."""
        if isinstance(arg, str) and _ID_RE.match(arg):
            plan = self._plan(arg)
            return plan, self.snap.mask_of(plan.squads), False
        parts = _parse_ids(arg, 3)
        if parts is None or not _HEX_RE.match(parts[1]) or not _SIG_RE.match(parts[2]):
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]))
        plan = self._plan(parts[0])
        if parts[2] != self.snap.locations_signature():
            return plan, self.snap.mask_of(plan.squads), True
        return plan, int(parts[1], 16), False

    async def _squads_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        plan, mask, stale = self._squads_arg(arg)
        view = self._squads_view(plan, mask)
        if stale:  # a screen answers its callback before rendering: say it in the message
            view.text = f"⚠️ {_T['sq_moved']}\n\n{view.text}"
        return view

    def _squads_view(self, plan: Plan, mask: int) -> View:
        snap = self.snap
        sig = snap.locations_signature()
        lines = [_T["sq_title"].format(name=_esc(plan.title(self.lang)))]
        rows: list[list[InlineKeyboardButton]] = []
        chosen = 0
        for i, loc in enumerate(snap.locations):
            on = bool(mask >> i & 1)
            chosen += on
            if not loc.present and not on:
                continue
            label = f"{'✅' if on else '▫️'} {loc.label(self.lang)}{'' if loc.present else _T['sq_missing']}"
            rows.append([nav_button(label[:64], SCREEN_SQUADS, arg=f"{plan.id}:{mask ^ (1 << i):x}:{sig}")])
        lines.append(_T["sq_hint"].format(n=chosen) if snap.locations else _T["sq_empty"])
        if snap.locations:
            rows.append([nav_button(_T["sq_save"], ACTIONS, "sqs", f"{plan.id}:{mask:x}:{sig}")])
        rows.append([nav_button(_T["sq_sync"], ACTIONS, "sync", f"sq{plan.id}")])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(plan.id))])
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=rows)

    def _new_squads(self, plan: Plan, mask: int) -> tuple[str, ...]:
        squads = self.snap.squads_from_mask(mask)
        if not squads:
            raise _Stop(Toast(_T["sq_need_one"], alert=True))
        if squads == plan.squads:
            raise _Stop(self._card_or(SCREEN_CARD, plan.id, toast=_T["sq_same"]))
        return squads

    def _fix_broken(self, plan: Plan, squads: Sequence[str]) -> dict[str, Any]:
        """Clear the «squad gone» mark when every chosen squad is in the panel again."""
        reason = plan.broken_reason
        if reason is None or not reason.startswith(BROKEN_PREFIX):
            return {}
        locs = [self.snap.location(s) for s in squads]
        return {"broken_reason": None} if all(loc is not None and loc.present for loc in locs) else {}

    async def _resolve_alert(self, plan: Plan, changes: dict[str, Any]) -> None:
        if "broken_reason" in changes and self.attention is not None:
            try:
                await self.attention.resolve(attention_key(plan.id))
            except Exception:
                log.exception("cannot resolve the broken plan alert")

    async def _a_squads_save(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan, mask, stale = self._squads_arg(arg)
        if stale:
            view = self._squads_view(plan, mask)
            view.toast = _T["sq_moved"]
            return view
        squads = self._new_squads(plan, mask)
        async with self.db.read() as conn:
            total, manual = await count_plan_subscribers(conn, plan.id)
        if total == 0:
            return await self._save_squads(ctx, plan, squads, plan.version, apply=False)
        sig = self.snap.locations_signature()
        cb = f"{plan.id}:{mask:x}:{sig}:{plan.version}"
        snap = self.snap
        text = _T["sq_confirm"].format(
            name=_esc(plan.title(self.lang)),
            old=_esc(", ".join(snap.squad_labels(plan.squads, self.lang)) or _T["squads_none"]),
            new=_esc(", ".join(snap.squad_labels(squads, self.lang))),
            total=total,
            manual=_T["sq_manual"].format(n=manual) if manual else "",
        )
        rows = [
            [nav_button(_T["sq_yes"].format(n=total - manual), ACTIONS, "sqy", cb)],
            [nav_button(_T["sq_no"], ACTIONS, "sqn", cb)],
            [nav_button(_T["sq_cancel"], SCREEN_SQUADS, arg=f"{plan.id}:{mask:x}:{sig}")],
        ]
        return View(text=text, parse_mode="HTML", keyboard=rows)

    def _confirm_arg(self, arg: Any) -> tuple[Plan, tuple[str, ...], int]:
        parts = _parse_ids(arg, 4)
        if (
            parts is None
            or not _HEX_RE.match(parts[1])
            or not _SIG_RE.match(parts[2])
            or not _ID_RE.match(parts[3])
        ):
            raise _Stop(Toast(_T["not_found"]))
        plan = self._plan(parts[0], screen=False)
        if parts[2] != self.snap.locations_signature() or int(parts[3]) != plan.version:
            raise _Stop(self._card_or(SCREEN_CARD, plan.id, toast=str(repo.StalePlanError())))
        return plan, self._new_squads(plan, int(parts[1], 16)), int(parts[3])

    async def _a_squads_yes(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan, squads, version = self._confirm_arg(arg)
        return await self._save_squads(ctx, plan, squads, version, apply=True)

    async def _a_squads_no(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan, squads, version = self._confirm_arg(arg)
        return await self._save_squads(ctx, plan, squads, version, apply=False)

    async def _save_squads(
        self, ctx: ScreenCtx, plan: Plan, squads: tuple[str, ...], version: int, *, apply: bool
    ) -> HandlerResult:
        """Plan change + audit (+ the apply job) in one transaction, compare-and-set on the plan version.

        Subscribers are counted again inside the transaction: the job gets the current number, and «Да» on a
        plan whose subscribers are all gone (or all edited by hand) enqueues nothing."""
        changes: dict[str, Any] = {"squads": list(squads), **self._fix_broken(plan, squads)}
        counted = {"total": 0, "applied": 0}

        async def save(conn: AsyncConnection) -> int:
            total, manual = await count_plan_subscribers(conn, plan.id)
            counted["total"] = total
            action = "plan.squads_apply" if apply else ("plan.squads_keep" if total else "plan.squads")
            new_version = await repo.update_plan(conn, plan.id, expected_version=version, **changes)
            details = {"from": list(plan.squads), "to": list(squads), "subscribers": total, "manual": manual}
            await repo.audit(conn, self._actor(ctx), action, f"plan:{plan.id}", details)
            if apply and total - manual > 0:
                counted["applied"] = total - manual
                await enqueue_apply_squads(
                    conn,
                    plan_id=plan.id,
                    plan_version=new_version,
                    plan_name=plan.title(self.lang),
                    squads=squads,
                    total=total,
                    chat_id=ctx.chat_id,
                    caused_by=f"admin:{ctx.user.user_id}",
                )
            return new_version

        await self._write(save, stale_plan=plan.id)
        await self._resolve_alert(plan, changes)
        if counted["applied"]:
            note = _T["sq_applying"].format(n=counted["applied"])
        elif counted["total"] == 0:
            note = _T["sq_saved"]
        elif apply:
            note = _T["sq_all_manual"]
        else:
            note = _T["sq_kept"]
        return self._card_or(SCREEN_CARD, plan.id, note=note)

    # ------------------------------------------------------------ availability, reset strategy

    async def _avail_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._avail_view(self._plan(arg))

    def _avail_view(self, plan: Plan) -> View:
        rows = [
            [nav_button(f"{'✅' if plan.availability == k else '▫️'} {v}", ACTIONS, "av", f"{plan.id}:{k}")]
            for k, v in AVAIL_LABELS.items()
        ]
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(plan.id))])
        text = f"{_T['av_title'].format(name=_esc(plan.title(self.lang)))}\n\n{_T['av_hint']}"
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def _a_availability(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = _parse_ids(arg, 2)
        if parts is None or parts[1] not in AVAIL_LABELS:
            return Toast(_T["not_found"])
        plan = self._plan(parts[0], screen=False)
        return await self._edit(ctx, plan, SCREEN_CARD, availability=parts[1])

    async def _reset_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._reset_view(self._plan(arg))

    def _reset_view(self, plan: Plan) -> View:
        rows = [
            [nav_button(f"{'✅' if plan.reset_strategy == k else '▫️'} {v}", ACTIONS, "rs", f"{plan.id}:{k}")]
            for k, v in RESET_LABELS.items()
        ]
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=str(plan.id))])
        text = f"{_T['rs_title'].format(name=_esc(plan.title(self.lang)))}\n\n{_T['rs_hint']}"
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def _a_reset(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parts = _parse_ids(arg, 2)
        if parts is None or parts[1] not in RESET_LABELS:
            return Toast(_T["not_found"])
        plan = self._plan(parts[0], screen=False)
        return await self._edit(ctx, plan, SCREEN_CARD, reset_strategy=parts[1])

    # ------------------------------------------------------------ devices, traffic, tag

    async def _devices_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._devices_view(self._plan(arg))

    def _devices_view(self, plan: Plan) -> View:
        pid = str(plan.id)
        lines = [
            _T["dv_title"].format(name=_esc(plan.title(self.lang))),
            _T["devices"].format(value=format_devices(plan.device_limit)),
        ]
        addon = plan.addon
        if addon is not None:
            cap = _T["addon_cap"].format(n=addon.max_devices) if addon.max_devices else ""
            lines.append(
                _T["addon"].format(
                    price=_money(addon.price_minor, addon.currency), days=addon.per_days, cap=cap
                )
            )
        else:
            lines += [_T["addon_off"], _T["dv_addon_hint"]]
        rows = [[nav_button(_T["dv_set"], ACTIONS, "dev", pid)]]
        if plan.device_limit is not None:
            rows.append([nav_button(_T["dv_null"], ACTIONS, "dnull", pid)])
        if plan.device_limit:
            rows.append([nav_button(_T["dv_addon"], ACTIONS, "addon", pid)])
        if plan.device_addon is not None:
            rows.append([nav_button(_T["dv_addon_off"], ACTIONS, "aoff", pid)])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=pid)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _a_devices(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_DEVICES, {"pid": plan.id})

    async def _f_dev(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan = self._form_plan(data)
        n = data.get("n")
        return await self._edit(ctx, plan, SCREEN_DEVICES, device_limit=n if isinstance(n, int) else None)

    async def _a_devices_null(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan = self._plan(arg, screen=False)
        return await self._edit(ctx, plan, SCREEN_DEVICES, device_limit=None)

    async def _a_addon(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_ADDON, {"pid": plan.id})

    async def _f_addon(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan = self._form_plan(data)
        price, cap = data.get("price"), data.get("max")
        try:
            addon = DeviceAddon(
                price_minor=int(price) if isinstance(price, int) else 0,
                per_days=30,
                max_devices=cap if isinstance(cap, int) else None,
                currency=self.currency(),
            )
        except CatalogError as e:
            return Toast(str(e), alert=True)
        return await self._edit(ctx, plan, SCREEN_DEVICES, device_addon=addon)

    async def _a_addon_off(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan = self._plan(arg, screen=False)
        return await self._edit(ctx, plan, SCREEN_DEVICES, device_addon=None)

    async def _a_traffic(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_TRAFFIC, {"pid": plan.id})

    async def _f_traffic(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan = self._form_plan(data)
        gb = data.get("gb")
        return await self._edit(ctx, plan, traffic_bytes=(gb if isinstance(gb, int) else 0) * GIB)

    async def _a_tag(self, ctx: ScreenCtx, arg: Any) -> View:
        plan = self._plan(arg, screen=False)
        return await ctx.start_form(F_TAG, {"pid": plan.id})

    async def _f_tag(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        plan = self._form_plan(data)
        tag = data.get("tag")
        return await self._edit(ctx, plan, panel_tag=tag if isinstance(tag, str) and tag else None)

    # ------------------------------------------------------------ trial, on sale

    async def _a_trial(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan = self._plan(arg, screen=False)
        return await self._edit(ctx, plan, is_trial=not plan.is_trial)

    async def _a_enabled(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        plan = self._plan(arg, screen=False)
        if not plan.enabled:
            if not plan.squads:
                return Toast(_T["need_squads"], alert=True)
            if not plan.is_trial and not plan.prices_in(self.currency()):
                return Toast(_T["need_prices"], alert=True)
        return await self._edit(ctx, plan, enabled=not plan.enabled)

    # ------------------------------------------------------------ locations

    async def _locations_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        return self.locations_view()

    def locations_view(self, note: str | None = None) -> View:
        snap = self.snap
        lines = [_T["loc_title"], _T["loc_hint"] if snap.locations else _T["loc_none"]]
        if note:
            lines.insert(0, note)
        rows: list[list[InlineKeyboardButton]] = []
        for loc in snap.locations:
            extra = _T["loc_members"].format(n=loc.members) if loc.members is not None else ""
            if not loc.present:
                extra = _T["loc_gone"]
            label = loc.label(self.lang) + (f" · {extra}" if extra else "")
            rows.append([nav_button(label[:64], SCREEN_LOCATION, arg=loc.squad_uuid)])
        rows.append([nav_button(_T["sq_sync"], ACTIONS, "sync")])
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n\n".join(lines), parse_mode="HTML", keyboard=rows)

    def _location(self, arg: Any, *, screen: bool = True) -> Location:
        loc = self.snap.location(arg) if isinstance(arg, str) and _UUID_RE.match(arg) else None
        if loc is None:
            if screen:
                raise _Stop(Redirect(SCREEN_LOCATIONS, toast=_T["loc_not_found"]))
            raise _Stop(Toast(_T["loc_not_found"]))
        return loc

    async def _location_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self.location_view(self._location(arg))

    def location_view(self, loc: Location, note: str | None = None) -> View:
        used = [p.title(self.lang) for p in self.snap.plans if loc.squad_uuid in p.squads]
        text = _T["loc_card"].format(
            label=_esc(loc.label(self.lang)),
            panel=_esc(loc.panel_name or "—") + ("" if loc.present else f" · {_T['loc_gone']}"),
            uuid=_esc(loc.squad_uuid),
            members=loc.members if loc.members is not None else "—",
            plans=_esc(", ".join(used)) if used else "—",
        )
        if note:
            text = f"{note}\n\n{text}"
        rows = [
            [
                nav_button(_T["loc_rename"], ACTIONS, "lname", loc.squad_uuid),
                nav_button(_T["loc_flag"], ACTIONS, "lflag", loc.squad_uuid),
            ]
        ]
        if loc.flag:
            rows.append([nav_button(_T["loc_noflag"], ACTIONS, "lnof", loc.squad_uuid)])
        rows.append([nav_button(_T["back"], SCREEN_LOCATIONS)])
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def _a_location_name(self, ctx: ScreenCtx, arg: Any) -> View:
        loc = self._location(arg, screen=False)
        return await ctx.start_form(F_LOC_NAME, {"uuid": loc.squad_uuid})

    async def _a_location_flag(self, ctx: ScreenCtx, arg: Any) -> View:
        loc = self._location(arg, screen=False)
        return await ctx.start_form(F_LOC_FLAG, {"uuid": loc.squad_uuid})

    async def _set_location(self, ctx: ScreenCtx, uuid: Any, **changes: Any) -> HandlerResult:
        loc = self._location(uuid)

        async def put(conn: AsyncConnection) -> bool:
            return await repo.set_location(
                conn, loc.squad_uuid, lang=self.lang, actor=self._actor(ctx), **changes
            )

        await self._write(put)
        fresh = self.snap.location(loc.squad_uuid)
        if fresh is None:
            return Redirect(SCREEN_LOCATIONS, toast=_T["loc_not_found"])
        view = self.location_view(fresh, note=_T["saved"])
        view.toast = _T["saved"]
        return view

    async def _f_loc_name(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set_location(ctx, data.get("uuid"), title=str(data.get("title") or ""))

    async def _f_loc_flag(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        return await self._set_location(ctx, data.get("uuid"), flag=str(data.get("flag") or ""))

    async def _a_location_noflag(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._set_location(ctx, arg, clear_flag=True)

    async def _a_sync(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        """«Обновить из панели»: one ``GET /internal-squads`` (interactive lane, bounded)."""
        source = self.squad_source() if self.squad_source is not None else None
        if source is None:
            return Toast(_T["sync_off"], alert=True)
        try:
            result = await sync_locations(
                self.db, source, attention=self.attention, lane=Lane.INTERACTIVE, timeout=SYNC_TIMEOUT
            )
        except TimeoutError:
            return Toast(_T["sync_timeout"], alert=True)
        except RemnawaveError as err:
            reason = err.message or err.kind.value
            return Toast(_T["sync_error"].format(error=reason)[:190], alert=True)
        await self.catalog.changed()
        note = (
            _T["sync_guard"]
            if result.guarded
            else _T["sync_done"].format(
                total=result.total, added=len(result.added), gone=len(result.gone), back=len(result.returned)
            )
        )
        if isinstance(arg, str) and arg.startswith("sq") and _ID_RE.match(arg[2:]):
            plan = self.snap.plan(int(arg[2:]))
            if plan is not None:
                view = self._squads_view(plan, self.snap.mask_of(plan.squads))
                view.text = f"{note}\n\n{view.text}"
                return view
        return self.locations_view(note)


# ------------------------------------------------------------------------------------------- validators


def _money_validator(currency: Callable[[], str]) -> Callable[[str], int]:
    def check(raw: str) -> int:
        try:
            value = parse_money(raw, currency())
        except ValueError:
            raise ValidationError("Нужна сумма, например 179 или 179,50") from None
        if value <= 0:
            raise ValidationError("Сумма должна быть больше нуля")
        return value

    return check


def _tag_validator(raw: str) -> str:
    value = raw.strip()
    if value in ("-", "—"):
        return ""
    try:
        tag = repo.validate_tag(value)
    except CatalogError as e:
        raise ValidationError(str(e)) from None
    return tag or ""


def _flag_validator(raw: str) -> str:
    value = raw.strip()
    if not 1 <= len(value) <= 16 or any(ch.isspace() for ch in value):
        raise ValidationError("Пришлите один эмодзи флага, например 🇳🇱")
    return value


# ------------------------------------------------------------------------------------------- entry point


class _Deps(Protocol):
    @property
    def db(self) -> Any: ...


async def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``).

    Uses ``deps.catalog`` when the app provides one; otherwise builds and loads its own
    :class:`CatalogService` over ``deps.db``. Optional: ``settings`` (``CURRENCY``), ``remnawave`` (panel for
    «Обновить из панели»), ``attention``.
    """
    settings = getattr(deps, "settings", None)

    def currency() -> str:
        try:
            current = settings.current if settings is not None else {"CURRENCY": "RUB"}
            # SettingsService.current is a method returning the snapshot; test doubles pass a mapping.
            value = (current() if callable(current) else current)["CURRENCY"]
        except (KeyError, AttributeError, RuntimeError):
            value = "RUB"
        return str(value or "RUB")

    catalog = getattr(deps, "catalog", None)
    if not isinstance(catalog, CatalogService):
        catalog = CatalogService(deps.db, currency=currency())
        await catalog.load()
    remnawave = getattr(deps, "remnawave", None)

    def squad_source() -> SquadSource | None:
        if remnawave is None or not getattr(remnawave, "configured", False):
            return None
        return remnawave.client

    screens = PlanScreens(
        router,
        catalog,
        db=deps.db,
        currency=currency,
        squad_source=squad_source,
        attention=getattr(deps, "attention", None),
    )
    screens.install()
    return screens.aiogram_router()
