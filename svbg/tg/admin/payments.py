"""«💳 Оплата» screens of the admin: cash desks and manual receipts waiting for a decision.

* ``apay`` — «🏦 Кассы» (owner): the enabled cash desks first with their state (✅ работает, ⚠️ ошибка,
  🧪 тестовый режим), then «➕ Подключить кассу» (``apay:add``) with the rest of the catalog;
* ``apay.c`` — the card of one cash desk (``<slug>``, ``<slug>:m`` with the rarely needed fields): the
  required fields as a checklist, the webhook address with a copy button once the desk is on, «▶️ Включить» /
  «⏸ Выключить» and the test mode (``apaya:on|off|tm``), «📖 Как подключить»;
* ``apay.rc`` — «🧾 Ждут подтверждения» (``payments.confirm``): manual receipts nobody decided yet; the
  decision itself stays on the receipt card in the admin group.

Every change goes through :meth:`SettingsService.apply` on ``PAY_<SLUG>_*`` — the same path as the settings
card: the keys are probed before the desk turns on, the change is audited and mirrored to ``.env``. A probe
can take longer than a click may wait: the card then says «проверяю» and comes back by itself with the result.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram.types import CopyTextButton, InlineKeyboardButton
from sqlalchemy.exc import SQLAlchemyError

from svbg.core.component import Health
from svbg.core.money import format_money
from svbg.core.settings.registry import PAYMENTS_SECTION
from svbg.core.settings.service import Change, SettingsError
from svbg.tg.admin import nav
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.core.component import ComponentRegistry
    from svbg.core.settings.registry import SettingDef
    from svbg.core.settings.service import ApplyResult, SettingsService
    from svbg.db.engine import Database
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "SCREEN_CARD",
    "SCREEN_LIST",
    "SCREEN_RECEIPTS",
    "PaymentScreens",
    "setup",
    "webhook_hint",
    "webhook_state",
]

log = logging.getLogger("svbg.tg.admin.payments")

SCREEN_LIST: Final = "apay"
SCREEN_CARD: Final = "apay.c"
SCREEN_RECEIPTS: Final = "apay.rc"
ACTIONS: Final = "apaya"
SETTING_CARD: Final = "set.key"  # svbg.tg.admin.settings.SCREEN_KEY
USER_CARD: Final = "au"  # svbg.tg.admin.users.screens.SCREEN_CARD
PERM_CONFIRM: Final = "payments.confirm"
DOCS_URL: Final = "https://github.com/BiggSm0ke/svbg-shop/blob/main/docs/providers/{slug}.md"
RECEIPTS_LIMIT: Final = 20
_SLUG_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TITLE_RE: Final = re.compile(r"^«(.+?)»: ")
_NOTES_MAX: Final = 256

_T: Final[dict[str, str]] = {
    "list_title": "🏦 <b>Кассы</b>",
    "list_on": "Включены: {n} из {total}.",
    "list_none": "Пока ни одна касса не включена.",
    "list_hint": "Нажмите на кассу, чтобы проверить ключи и адрес для уведомлений. Новую подключите кнопкой "
    "«➕ Подключить кассу».",
    "add_title": "➕ <b>Подключить кассу</b>",
    "add_hint": "Выберите кассу. Дальше бот попросит ключи из её личного кабинета.",
    "b_add": "➕ Подключить кассу",
    "b_back_list": "⬅️ Кассы",
    "card_title": "🏦 <b>{title}</b>",
    "now": "Сейчас: {state}",
    "st_off": "⏸ выключена",
    "st_ok": "✅ работает",
    "st_test": "🧪 работает в тестовом режиме",
    "st_degraded": "⚠️ {text}",
    "st_down": "❌ {text}",
    "field_ok": "{n}. {title}: {value} ✅",
    "field_missing": "{n}. {title}: не задано",
    "hook": "{n}. Адрес для уведомлений, вставьте его в кабинете кассы:",
    "hook_wait": "{n}. Адрес для уведомлений появится здесь, когда касса включится.",
    "hook_none": "Кассе не нужен адрес для уведомлений: бот сам проверяет оплату.",
    "hook_no_url": "{n}. Чтобы касса присылала уведомления, задайте публичный адрес бота (PUBLIC_URL) "
    "в разделе «⚙️ Система → 🧰 Сервер и .env».",
    "b_edit": "✏️ {title}",
    "b_copy": "📋 Скопировать адрес",
    "b_on": "▶️ Включить",
    "b_on_blocked": "▶️ Включить (сначала заполните поля)",
    "b_off": "⏸ Выключить",
    "b_test": "🧪 Тестовый режим: {state}",
    "b_more": "🧰 Ещё ({n})",
    "b_less": "🙈 Скрыть редкие",
    "b_docs": "📖 Как подключить",
    "on": "вкл",
    "off": "выкл",
    "missing": "Сначала заполните: {fields}",
    "checking": "⏳ Проверяю ключи кассы. Это может занять до минуты, карточка обновится сама.",
    "done_on": "Касса включена.",
    "done_off": "Касса выключена.",
    "done_test_on": "Тестовый режим включён.",
    "done_test_off": "Тестовый режим выключен.",
    "unchanged": "Ничего не изменилось.",
    "failed": "Не получилось: {reason}",
    "not_found": "Такой кассы нет",
    "db_down": "База данных не отвечает, попробуйте позже",
    "rc_title": "🧾 <b>Ждут подтверждения</b>",
    "rc_hint": "Чеки ручной оплаты, по которым ещё нет решения. Подтвердить или отклонить чек можно в его "
    "карточке в админ-группе.",
    "rc_empty": "Сейчас таких чеков нет.",
    "rc_more": "Показаны первые {n}.",
    "rc_line": "{at} · {who} · {amount}",
    "no_name": "без имени",
}


def webhook_state(instances: Any, slug: str) -> tuple[str, str | None]:
    """``(kind, url)``: ``wait`` (no live instance yet: the address appears once the desk is on), ``none``
    (the provider needs no webhooks), ``no_url`` (no public address of the bot) or ``ok`` with the address."""
    inst = instances.by_slug(slug) if instances is not None else None
    if inst is None:
        return "wait", None
    if not inst.caps.webhook:
        return "none", None
    url = instances.webhook_url(inst)
    return ("no_url", None) if url is None else ("ok", url)


def webhook_hint(instances: Any, slug: str) -> tuple[list[str], str | None]:
    """Where the provider must send its webhooks: HTML lines and the address itself (``None``: not known yet,
    not needed or no public address). Shared by the cash desk card and the all-settings section."""
    kind, url = webhook_state(instances, slug)
    if kind == "wait":
        return ["Адрес для вебхука появится здесь, когда касса включится."], None
    if kind == "none":
        return ["Вебхук этой кассе не нужен: бот сам проверяет оплату."], None
    if url is None:
        return ["Чтобы касса присылала вебхуки, задайте публичный адрес бота (PUBLIC_URL)."], None
    return ["Адрес для вебхука, вставьте его в кабинете кассы:", f"<code>{html.escape(url)}</code>"], url


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


@dataclass(frozen=True, slots=True)
class _Field:
    defn: SettingDef
    title: str  # without the «Касса»: prefix
    required: bool
    advanced: bool


@dataclass(frozen=True, slots=True)
class _Desk:
    slug: str
    title: str
    provider: str  # provider slug (docs page)
    fields: tuple[_Field, ...]  # the provider's own fields, in manifest order
    extra: tuple[SettingDef, ...]  # PROVIDER, PROXY_URL (rarely needed)

    def missing(self, snap: Mapping[str, Any]) -> list[_Field]:
        return [f for f in self.fields if f.required and snap[f.defn.key] in (None, "", [])]


class PaymentScreens:
    """See the module docstring. ``instances()`` — the live :class:`InstanceRegistry` (or ``None``)."""

    def __init__(
        self,
        router: ScreenRouter,
        settings: SettingsService,
        *,
        db: Database | None = None,
        components: ComponentRegistry | None = None,
        instances: Callable[[], Any] = lambda: None,
        currency: Callable[[], str] = lambda: "RUB",
        health_timeout: float = 1.0,
    ) -> None:
        self.router = router
        self.settings = settings
        self.registry = settings.registry
        self.db = db
        self.components = components
        self.instances = instances
        self.currency = currency
        self.health_timeout = health_timeout
        self._notes: OrderedDict[int, str] = OrderedDict()  # user → line shown once on the next card
        self._tasks: set[asyncio.Task[Any]] = set()
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        owner: dict[str, Any] = {"required_role": "owner"}
        r.screen(SCREEN_LIST, **owner)(self._list_screen)
        r.screen(SCREEN_CARD, **owner)(self._card_screen)
        r.screen(SCREEN_RECEIPTS, required_role="admin", perm=PERM_CONFIRM)(self._receipts_screen)
        r.action(ACTIONS, "on", **owner)(self._a_on)
        r.action(ACTIONS, "off", **owner)(self._a_off)
        r.action(ACTIONS, "tm", **owner)(self._a_test)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.wait(list(self._tasks))

    # ------------------------------------------------------------ catalog

    def slugs(self) -> list[str]:
        prefix = f"{PAYMENTS_SECTION}."
        return [sid[len(prefix) :] for sid in self.registry.subsections(PAYMENTS_SECTION)]

    def desk(self, slug: str) -> _Desk | None:
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            return None
        enabled = self.registry.find(_key(slug, "ENABLED"))
        if enabled is None or enabled.section != f"{PAYMENTS_SECTION}.{slug}":
            return None
        match = _TITLE_RE.match(enabled.title)
        title = match[1] if match else slug
        provider = str(self.settings.current().get(_key(slug, "PROVIDER")) or slug)
        manifest_fields = _manifest_fields(provider)
        own = [
            d
            for d in self.registry.by_section().get(enabled.section, [])
            if d.key not in {_key(slug, s) for s in ("ENABLED", "PROVIDER", "TEST_MODE", "PROXY_URL")}
        ]
        fields = []
        for d in own:
            spec = manifest_fields.get(d.key[len(_key(slug, "")) :])
            required = spec.required and spec.default is None if spec is not None else False
            fields.append(_Field(d, _strip_title(d.title, title), required, d.advanced))
        extra = tuple(
            d for d in (self.registry.find(_key(slug, s)) for s in ("PROVIDER", "PROXY_URL")) if d is not None
        )
        return _Desk(slug, title, provider, tuple(fields), extra)

    def _enabled(self, slug: str) -> bool:
        return bool(self.settings.current().get(_key(slug, "ENABLED")))

    async def _health(self, slugs: Sequence[str]) -> dict[str, Any]:
        comps = self.components
        if comps is None:
            return {}
        names = [f"payments.{s}" for s in slugs if f"payments.{s}" in comps]
        if not names:
            return {}
        reports = await asyncio.gather(*(comps.health(n, limit_s=self.health_timeout) for n in names))
        return {n.partition(".")[2]: r for n, r in zip(names, reports, strict=True)}

    def _state(self, slug: str, report: Any) -> str:
        snap = self.settings.current()
        if not snap.get(_key(slug, "ENABLED")):
            return _T["st_off"]
        status = getattr(report, "status", None)
        summary = _esc(_cut(str(getattr(report, "summary", "") or ""), 160))
        if status is Health.DEGRADED:
            return _T["st_degraded"].format(text=summary)
        if status is Health.DOWN:
            return _T["st_down"].format(text=summary)
        if snap.get(_key(slug, "TEST_MODE")):
            return _T["st_test"]
        return _T["st_ok"]

    @staticmethod
    def _icon(state: str) -> str:
        return state.split(" ", 1)[0]

    # ------------------------------------------------------------ list

    async def _list_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        adding = arg == "add"
        slugs = self.slugs()
        on = [s for s in slugs if self._enabled(s)]
        rows: list[list[InlineKeyboardButton]] = []
        if adding:
            lines = [_T["add_title"], _esc(nav.breadcrumb(SCREEN_LIST) + " › ➕"), "", _T["add_hint"]]
            pair: list[InlineKeyboardButton] = []
            for slug in slugs:
                if slug in on:
                    continue
                desk = self.desk(slug)
                if desk is None:
                    continue
                pair.append(nav_button(_cut(desk.title, 30), SCREEN_CARD, arg=slug))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
            rows.append([nav_button(_T["b_back_list"], SCREEN_LIST), nav_button("🛠 Админка", nav.ROOT)])
            return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)
        health = await self._health(on)
        lines = [_T["list_title"], _esc(nav.breadcrumb(SCREEN_LIST)), ""]
        lines.append(_T["list_on"].format(n=len(on), total=len(slugs)) if on else _T["list_none"])
        lines.append(_T["list_hint"])
        for slug in on:
            desk = self.desk(slug)
            if desk is None:
                continue
            icon = self._icon(self._state(slug, health.get(slug)))
            rows.append([nav_button(_cut(f"{icon} {desk.title}", 60), SCREEN_CARD, arg=slug)])
        rows.append([nav_button(_T["b_add"], SCREEN_LIST, arg="add")])
        skew = self.registry.find("PAY_CLOCK_SKEW_ALERT_COUNT")
        if skew is not None:
            value = self.settings.current().get(skew.key)
            label = _cut(f"⚙️ {skew.title}: {value}", 60)
            rows.append([nav_button(label, SETTING_CARD, arg=skew.key)])
        rows.append(nav.back_row(SCREEN_LIST))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ card

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        slug, _, flag = arg.partition(":") if isinstance(arg, str) else ("", "", "")
        desk = self.desk(slug)
        if desk is None or flag not in ("", "m"):
            return Redirect(SCREEN_LIST, toast=_T["not_found"])
        note = self._notes.pop(ctx.user.user_id, None)
        return await self.card_view(ctx, desk, expanded=flag == "m", note=note)

    async def card_view(
        self, ctx: ScreenCtx, desk: _Desk, *, expanded: bool = False, note: str | None = None
    ) -> View:
        snap = self.settings.current()
        health = await self._health([desk.slug])
        state = self._state(desk.slug, health.get(desk.slug))
        lines = [
            _T["card_title"].format(title=_esc(desk.title)),
            _esc(nav.breadcrumb(SCREEN_CARD, f"🏦 {desk.title}")),
            "",
        ]
        if note:
            lines += [note, ""]
        lines.append(_T["now"].format(state=state))
        n = 0
        for f in desk.fields:
            if not f.required:
                continue
            n += 1
            value = snap[f.defn.key]
            if value in (None, "", []):
                lines.append(_T["field_missing"].format(n=n, title=_esc(f.title)))
            else:
                shown = _shown(f.defn, value)
                lines.append(_T["field_ok"].format(n=n, title=_esc(f.title), value=_esc(shown)))
        kind, url = webhook_state(self.instances(), desk.slug) if self._enabled(desk.slug) else ("wait", None)
        n += 1
        if kind == "ok" and url is not None:
            lines += [_T["hook"].format(n=n), f"<code>{_esc(url)}</code>"]
        elif kind == "none":
            lines.append(_T["hook_none"])
        elif kind == "no_url":
            lines.append(_T["hook_no_url"].format(n=n))
        else:
            lines.append(_T["hook_wait"].format(n=n))

        rows: list[list[InlineKeyboardButton]] = []
        regular = [f for f in desk.fields if not f.advanced]
        rare = [f.defn for f in desk.fields if f.advanced] + list(desk.extra)
        pair: list[InlineKeyboardButton] = []
        for f in regular:
            label = _cut(_T["b_edit"].format(title=f.title), 40)
            pair.append(
                InlineKeyboardButton(
                    text=label, callback_data=await ctx.callback(SETTING_CARD, arg=f.defn.key)
                )
            )
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        if url is not None:
            rows.append([InlineKeyboardButton(text=_T["b_copy"], copy_text=CopyTextButton(text=url[:256]))])
        if self._enabled(desk.slug):
            rows.append([nav_button(_T["b_off"], ACTIONS, "off", desk.slug)])
        else:
            label = _T["b_on_blocked"] if desk.missing(snap) else _T["b_on"]
            rows.append([nav_button(label, ACTIONS, "on", desk.slug, style="success")])
        test_key = _key(desk.slug, "TEST_MODE")
        if test_key in self.registry:
            test = _T["on"] if snap.get(test_key) else _T["off"]
            rows.append([nav_button(_T["b_test"].format(state=test), ACTIONS, "tm", desk.slug)])
        if rare:
            if expanded:
                for d in rare:
                    title = _strip_title(d.title, desk.title)
                    label = _cut(f"{title}: {_shown(d, snap[d.key])}", 60)
                    data = await ctx.callback(SETTING_CARD, arg=d.key)
                    rows.append([InlineKeyboardButton(text=label, callback_data=data)])
                rows.append([nav_button(_T["b_less"], SCREEN_CARD, arg=desk.slug)])
            else:
                rows.append([nav_button(_T["b_more"].format(n=len(rare)), SCREEN_CARD, arg=f"{desk.slug}:m")])
        rows.append([InlineKeyboardButton(text=_T["b_docs"], url=DOCS_URL.format(slug=desk.provider))])
        rows.append(nav.back_row(SCREEN_CARD))
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ actions

    async def _a_on(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        desk = self.desk(arg) if isinstance(arg, str) else None
        if desk is None:
            return Toast(_T["not_found"])
        missing = desk.missing(self.settings.current())
        if missing:
            names = ", ".join(f"«{f.title}»" for f in missing)
            return Toast(_cut(_T["missing"].format(fields=names), 190), alert=True)
        return await self._apply(ctx, desk, _key(desk.slug, "ENABLED"), True, _T["done_on"])

    async def _a_off(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        desk = self.desk(arg) if isinstance(arg, str) else None
        if desk is None:
            return Toast(_T["not_found"])
        return await self._apply(ctx, desk, _key(desk.slug, "ENABLED"), False, _T["done_off"])

    async def _a_test(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        desk = self.desk(arg) if isinstance(arg, str) else None
        key = _key(desk.slug, "TEST_MODE") if desk is not None else ""
        if desk is None or key not in self.registry:
            return Toast(_T["not_found"])
        value = not bool(self.settings.current().get(key))
        return await self._apply(ctx, desk, key, value, _T["done_test_on" if value else "done_test_off"])

    async def _apply(self, ctx: ScreenCtx, desk: _Desk, key: str, value: Any, done: str) -> HandlerResult:
        """``apply()`` with a probe: wait a while, then let the card come back by itself with the result."""
        task = asyncio.ensure_future(
            self.settings.apply([Change(key, value)], source="bot", actor_id=ctx.user.user_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        budget = max(self.router.handler_timeout - 3.0, self.router.handler_timeout * 0.5)
        finished, _ = await asyncio.wait({task}, timeout=budget)
        if task not in finished:
            later = asyncio.ensure_future(self._deliver_later(task, ctx, desk, key, done))
            self._tasks.add(later)
            later.add_done_callback(self._tasks.discard)
            return View(text=_T["checking"], keyboard=[nav.back_row(SCREEN_CARD)])
        try:
            result = task.result()
        except (SettingsError, SQLAlchemyError, OSError) as e:
            log.warning("cash desk change failed: %s", type(e).__name__)
            return Toast(_T["db_down"], alert=True)
        view = await self.card_view(ctx, desk, note=self._outcome(result, key, done))
        view.toast = done if key in result.applied else None
        return view

    def _outcome(self, result: ApplyResult, key: str, done: str) -> str:
        if result.rejected:
            reason = next(iter(result.rejected.values()))
            return _esc(_T["failed"].format(reason=_cut(reason, 400)))
        return _esc(done if key in result.applied else _T["unchanged"])

    async def _deliver_later(
        self, task: asyncio.Future[ApplyResult], ctx: ScreenCtx, desk: _Desk, key: str, done: str
    ) -> None:
        try:
            result = await asyncio.shield(task)
        except (SettingsError, SQLAlchemyError, OSError) as e:
            log.warning("delayed cash desk change failed: %s", type(e).__name__)
            return
        self._notes[ctx.user.user_id] = self._outcome(result, key, done)
        while len(self._notes) > _NOTES_MAX:
            self._notes.popitem(last=False)
        try:
            await self.router.show(ctx.user, ctx.chat_id, SCREEN_CARD, desk.slug)
        except Exception:  # the result is still in the card's note and the settings history
            log.exception("could not show the cash desk result")

    # ------------------------------------------------------------ receipts

    async def _receipts_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        from svbg.billing.tables import manual_receipts
        from svbg.core.tables import users

        lines = [_T["rc_title"], _esc(nav.breadcrumb(SCREEN_RECEIPTS)), "", _T["rc_hint"], ""]
        rows: list[list[InlineKeyboardButton]] = []
        found: list[Any] = []
        if self.db is not None:
            query = (
                sa.select(
                    manual_receipts.c.user_id,
                    manual_receipts.c.amount_minor,
                    manual_receipts.c.currency,
                    manual_receipts.c.created_at,
                    users.c.first_name,
                    users.c.username,
                )
                .join(users, users.c.id == manual_receipts.c.user_id)
                .where(manual_receipts.c.status == "submitted")
                .order_by(manual_receipts.c.created_at.desc())
                .limit(RECEIPTS_LIMIT + 1)
            )
            try:
                async with self.db.read() as conn:
                    found = list((await conn.execute(query)).all())
            except (SQLAlchemyError, OSError) as e:
                log.warning("pending receipts unavailable: %s", type(e).__name__)
                lines.append(_T["db_down"])
        if not found:
            lines.append(_T["rc_empty"])
        for r in found[:RECEIPTS_LIMIT]:
            who = (r.first_name or "").strip()[:24] or _T["no_name"]
            if r.username:
                who += f" @{r.username[:24]}"
            amount = _money(int(r.amount_minor), str(r.currency))
            line = _T["rc_line"].format(at=r.created_at.strftime("%d.%m %H:%M"), who=who, amount=amount)
            rows.append([nav_button(_cut("👤 " + line, 60), USER_CARD, arg=str(r.user_id))])
        if len(found) > RECEIPTS_LIMIT:
            lines.append(_T["rc_more"].format(n=RECEIPTS_LIMIT))
        rows.append(nav.back_row(SCREEN_RECEIPTS))
        return View(text="\n".join(lines).rstrip(), parse_mode="HTML", keyboard=rows)


def _key(slug: str, suffix: str) -> str:
    return f"PAY_{slug.upper()}_{suffix.upper()}" if suffix else f"PAY_{slug.upper()}_"


def _strip_title(title: str, desk_title: str) -> str:
    """«ЮKassa»: Идентификатор магазина → Идентификатор магазина (inside the desk's own card)."""
    prefix = f"«{desk_title}»: "
    stripped = title[len(prefix) :] if title.startswith(prefix) else title
    return stripped[:1].upper() + stripped[1:]


def _shown(defn: SettingDef, value: Any) -> str:
    from svbg.core.settings import values

    if defn.kind == "bool":
        return "вкл" if value else "выкл"
    return _cut(values.display(defn, value, human=True), 40)


def _money(amount: int, currency: str) -> str:
    try:
        return format_money(amount, currency, "ru")
    except (ValueError, KeyError):
        return f"{amount} {currency}"


def _manifest_fields(provider: str) -> dict[str, Any]:
    """``{ENV_SUFFIX: ConfigField}`` of a built-in provider (``required`` / ``default``); empty if unknown."""
    try:
        from svbg.payments.providers import BUILTIN_PROVIDERS
    except ImportError:  # pragma: no cover - a tree without the payment plugins
        return {}
    for cls in BUILTIN_PROVIDERS:
        if cls.manifest.slug == provider:
            return {f.env_suffix: f for f in cls.manifest.config.fields().values()}
    return {}


def setup(router: Any, deps: Any) -> None:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    payments = getattr(deps, "payments", None)

    def instances() -> Any:
        return getattr(payments, "instances", None)

    def currency() -> str:
        try:
            return str(deps.settings.current().get("CURRENCY") or "RUB")
        except RuntimeError:
            return "RUB"

    screens = PaymentScreens(
        router,
        deps.settings,
        db=getattr(deps, "db", None),
        components=getattr(deps, "components", None),
        instances=instances,
        currency=currency,
    )
    screens.install()
    on_stop = getattr(deps, "on_stop", None)
    if callable(on_stop):
        on_stop("cash desks ui", screens.drain)
