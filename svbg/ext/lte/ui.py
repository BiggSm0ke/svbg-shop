"""LTE quotas: what the user sees (05 §2.1.3–2.1.4) — slot lines, the pack button and the «докупка» screens.

* :func:`load_view` — the read model of «Главное меню» / «Моя подписка»: **one** SQL
  (:func:`svbg.ext.lte.packs.load_facts`), 0 HTTP. The slot renderers are pure functions over it.
* ``home.status_lines`` — ``🌐 LTE: 12,4 / 50 ГБ``; hidden when the module does not enforce (shadow, off,
  outside the pilot), for an exemption, a zero limit, a frozen subscription or no live period.
* ``subscription.blocks`` — ``🌐 Трафик LTE`` and ``└ 12,4 из 50 ГБ · сброс 07.10 (+10 ГБ докуплено)`` (or the
  exhausted / unavailable / unlimited variants); ``subscription.buttons`` — «⚡ Докупить трафик LTE» right
  after «Подключиться» when ``availability`` passes and access is blocked or ``used ≥ warn %``.
* Screen ``lte_topup`` (pack choice, 1 SQL) → ``pick`` (confirmation «сгорит {дата} в 00:00 МСК», a warning
  with «Всё равно купить» when the pack does not return access, 1 SQL) → ``buy`` (the ``addon_lte`` draft,
  2 SQL) → the core checkout ``co`` with its «Оплатить» (balance, auto-complete after a top-up).

Every callback argument is re-validated (callback data can be forged) and the order is re-checked in the
fulfill transaction anyway (:class:`svbg.ext.lte.packs.AddonLteKind`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from svbg.core.clock import now
from svbg.ext.api import SlotButton, SlotResult
from svbg.ext.lte import packs
from svbg.ext.lte.notify import fmt_date, fmt_gb, group_name

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.ext.api import SlotCall
    from svbg.ext.lte.packs import PackFacts
    from svbg.ext.lte.service import LteConfig, LteService

__all__ = [
    "ACTION_TOPUP",
    "SCREEN_TOPUP",
    "T",
    "UiModel",
    "install",
    "load_view",
    "render_blocks",
    "render_buttons",
    "render_status_line",
    "visible",
]

log = logging.getLogger("svbg.ext.lte.ui")

SCREEN_TOPUP: Final = "lte_topup"
ACTION_TOPUP: Final = "lte.topup"  # module action of the slot button: ``v1:mod:lte.topup``
_ARG_RE: Final = re.compile(r"^\d{1,12}(?::\d{1,12}){0,2}$")

T: Final[Mapping[str, str]] = {
    "status_line": "🌐 {group}: {used} / {limit} ГБ",
    "block_title": "🌐 <b>Трафик {group}</b>",
    "line": "└ {warn}{used} из {limit} ГБ · сброс {reset}{credits}",
    "line_credits": " (+{gb} ГБ докуплено)",
    "line_blocked": "└ 🚫 лимит исчерпан · доступ вернётся {when}",
    "line_blocked_renew": "после продления",
    "line_trial_zero": "└ 🔒 недоступно на пробном периоде",
    "line_zero": "└ 🔒 недоступно на вашем тарифе",
    "line_unlimited": "└ ♾ без ограничения · израсходовано {used} ГБ",
    "btn_topup": "⚡ Докупить трафик LTE",
    "topup_title": "⚡ <b>Трафик {group}</b>\nСейчас: {used} из {limit} ГБ · сброс {reset}.",
    "topup_blocked": "Доступ к серверам {group} закрыт: лимит превышен на {over} ГБ.",
    "topup_pick": "Выберите пакет. Он действует до сброса {reset} (00:00 МСК).",
    "pack": "+{gb} ГБ — {price}",
    "pack_enough": "✅ +{gb} ГБ — {price} · хватит",
    "confirm": "⚡ <b>Пакет +{gb} ГБ за {price}</b>\nСгорит {reset} в 00:00 МСК. Оплата с баланса.",
    "insufficient": "⚠️ Пакета не хватит: лимит превышен на {over} ГБ, доступ не вернётся.",
    "btn_confirm": "✅ Подтвердить",
    "btn_anyway": "Всё равно купить",
    "btn_back": "◀️ Назад",
    "btn_renew": "🔄 Продлить",
    "btn_support": "💬 Поддержка",
    "btn_menu": "🏠 Меню",
    "unavailable": "Докупка сейчас недоступна.",
    "gone": "Предложение устарело. Откройте докупку заново.",
}
# ------------------------------------------------------------------------------------------- read model


@dataclass(frozen=True, slots=True)
class UiModel:
    """The user's subscription × LTE groups (``PackFacts``), loaded once per screen render."""

    facts: tuple[PackFacts, ...] = ()
    at: datetime | None = None


def _uid(user: Any) -> int | None:
    value = getattr(user, "user_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def load_view(conn: AsyncConnection, user: Any, view: Mapping[str, Any]) -> UiModel | None:
    """``ViewLoader`` of ``home`` / ``subscription``: one query; ``None`` without a subscription."""
    del view
    uid = _uid(user)
    if uid is None:
        return None
    at = now()
    facts = await packs.load_facts(conn, user_id=uid, at=at)
    return UiModel(tuple(facts), at) if facts else None


def _cfg(call: SlotCall) -> LteConfig:
    from svbg.ext.lte.service import LteConfig

    try:
        return LteConfig.from_snapshot(call.module.config())
    except Exception:  # noqa: BLE001 - a broken snapshot means defaults, never a broken screen
        return LteConfig()


def visible(facts: PackFacts, cfg: LteConfig) -> bool:
    """Whether the quota is shown at all: enforced for this user, a live period with rights, not frozen."""
    if facts.frozen or not facts.rights or facts.group_state != "active":
        return False
    if cfg.mode != "on" or not facts.group_enforce:
        return False
    if cfg.pilot and (facts.panel_user_id or 0) not in cfg.pilot:
        return False
    return facts.period_id is not None and facts.period_state in ("open", "deferred")


def _shown(model: Any, cfg: LteConfig) -> list[PackFacts]:
    if not isinstance(model, UiModel):
        return []
    return [f for f in model.facts if visible(f, cfg)]


def _percent(facts: PackFacts) -> int | None:
    return facts.limit.percent_of(facts.used_bytes)


# ---------------------------------------------------------------------------------------------- slots


def render_status_line(call: SlotCall) -> SlotResult | None:
    """``home.status_lines``: numbers only (also while blocked)."""
    cfg = _cfg(call)
    tx = T
    lines = []
    for f in _shown(call.model, cfg):
        if f.exempt or f.limit.unlimited or f.limit.zero:
            continue
        lines.append(
            tx["status_line"].format(
                group=group_name(f.group_name),
                used=fmt_gb(f.used_bytes, cfg.gb_bytes),
                limit=fmt_gb(f.limit.shown, cfg.gb_bytes),
            )
        )
    return SlotResult(lines=tuple(lines)) if lines else None


def block_lines(f: PackFacts, cfg: LteConfig, _lang: str | None = None) -> list[str]:
    """The «Моя подписка» block of one group."""
    gb = cfg.gb_bytes
    tx = T
    title = tx["block_title"].format(group=group_name(f.group_name))
    deferred = f.period_state == "deferred"
    if f.exempt or f.limit.unlimited:
        return [title, tx["line_unlimited"].format(used=fmt_gb(f.used_bytes, gb))]
    if f.limit.zero:
        return [title, tx["line_trial_zero"] if f.period_is_trial else tx["line_zero"]]
    if f.blocked:
        when = tx["line_blocked_renew"] if deferred else fmt_date(f.planned_end_at)
        return [title, tx["line_blocked"].format(when=when)]
    percent = _percent(f)
    warn = "⚠️ " if percent is not None and percent >= cfg.warn_percent else ""
    credits = tx["line_credits"].format(gb=fmt_gb(f.credit_bytes, gb)) if f.credit_bytes > 0 else ""
    reset = tx["line_blocked_renew"] if deferred else fmt_date(f.planned_end_at)
    return [
        title,
        tx["line"].format(
            warn=warn,
            used=fmt_gb(f.used_bytes, gb),
            limit=fmt_gb(f.limit.shown, gb),
            reset=reset,
            credits=credits,
        ),
    ]


def render_blocks(call: SlotCall) -> SlotResult | None:
    """``subscription.blocks``."""
    cfg = _cfg(call)
    lines: list[str] = []
    for f in _shown(call.model, cfg):
        lines.extend(block_lines(f, cfg))
    return SlotResult(lines=tuple(lines)) if lines else None


def wants_button(f: PackFacts, cfg: LteConfig, at: datetime) -> bool:
    """The pack button: ``availability`` passes and the user is blocked or at ``warn %``."""
    if not packs.availability(f, cfg, at=at).ok:
        return False
    percent = _percent(f)
    return f.blocked or (percent is not None and percent >= cfg.warn_percent)


def render_buttons(call: SlotCall) -> SlotResult | None:
    """``subscription.buttons``: «⚡ Докупить трафик LTE» right after «Подключиться»."""
    cfg = _cfg(call)
    model = call.model
    at = model.at if isinstance(model, UiModel) and model.at is not None else now()
    for f in _shown(model, cfg):
        if wants_button(f, cfg, at):
            button = SlotButton(T["btn_topup"], action=ACTION_TOPUP, style="success", after="connect")
            return SlotResult(buttons=(button,))
    return None


# --------------------------------------------------------------------------------------------- screens


def _args(arg: Any, n_min: int, n_max: int) -> list[int] | None:
    if arg is None and n_min == 0:
        return []
    if not isinstance(arg, str) or not _ARG_RE.match(arg):
        return None
    parts = [int(p) for p in arg.split(":")]
    return parts if n_min <= len(parts) <= n_max else None


def _pick_facts(found: Sequence[PackFacts], group_id: int | None) -> PackFacts | None:
    """The group the user asked for, else the first group with rights."""
    for f in found:
        if (group_id is None or f.group_id == group_id) and f.rights:
            return f
    return found[0] if found and group_id is None else None


def _money(amount_minor: int, currency: str) -> str:
    from svbg.core.money import format_money

    return format_money(amount_minor, currency, nbsp=True)


def install(router: Any, service: Callable[[], LteService]) -> None:
    """User screens: ``lte_topup`` (+ ``pick`` / ``buy``) and the slot button's module action."""
    from svbg.tg.ui.renderer import MODULE_SCREEN, nav_button
    from svbg.tg.ui.view import Redirect, View

    def menu_row() -> list[Any]:
        return [nav_button(T["btn_menu"], "home")]

    def refusal_view(result: packs.Availability, cfg: LteConfig) -> Any:
        tx = T
        text = result.text or tx["unavailable"]
        rows: list[list[Any]] = []
        if result.code == "expiring":
            rows.append([nav_button(tx["btn_renew"], "buy", style="primary")])
        if result.code == "manual_block" and cfg.support_url:
            from aiogram.types import InlineKeyboardButton

            rows.append([InlineKeyboardButton(text=tx["btn_support"], url=cfg.support_url)])
        rows.append(menu_row())
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def facts_for(ctx: Any, group_id: int | None) -> PackFacts | None:
        svc = service()
        async with svc.db.read() as conn:
            found = await packs.load_facts(conn, user_id=ctx.user.user_id, group_id=group_id)
        return _pick_facts(found, group_id)

    @router.screen(SCREEN_TOPUP)
    async def topup_screen(ctx: Any, arg: Any) -> Any:
        ids = _args(arg, 0, 1)
        if ids is None:
            return Redirect("home")
        svc = service()
        cfg = svc.cfg()
        tx = T
        f = await facts_for(ctx, ids[0] if ids else None)
        at = now()
        if f is None:
            return refusal_view(packs.Availability.refuse("no_subscription"), cfg)
        result = packs.availability(f, cfg, at=at)
        if not result.ok:
            return refusal_view(result, cfg)
        gb = cfg.gb_bytes
        name = group_name(f.group_name)
        lines = [
            tx["topup_title"].format(
                group=name,
                used=fmt_gb(f.used_bytes, gb),
                limit=fmt_gb(f.limit.shown, gb),
                reset=fmt_date(f.planned_end_at),
            )
        ]
        if f.blocked:
            lines.append(tx["topup_blocked"].format(group=name, over=fmt_gb(f.overage(), gb)))
        lines.append(tx["topup_pick"].format(reset=fmt_date(f.planned_end_at)))
        rows: list[list[Any]] = []
        for pack, enough in packs.order_packs(f, gb):
            label = (tx["pack_enough"] if enough else tx["pack"]).format(
                gb=pack.gb, price=_money(pack.amount_minor, pack.currency)
            )
            rows.append([nav_button(label, SCREEN_TOPUP, "pick", f"{f.group_id}:{pack.id}")])
        rows.append(menu_row())
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    @router.action(SCREEN_TOPUP, "pick")
    async def pick(ctx: Any, arg: Any) -> Any:
        ids = _args(arg, 2, 2)
        if ids is None:
            return Redirect(SCREEN_TOPUP)
        group_id, pack_id = ids
        svc = service()
        cfg = svc.cfg()
        tx = T
        f = await facts_for(ctx, group_id)
        pack = next((p for p in f.packs if p.id == pack_id), None) if f is not None else None
        if f is None or pack is None:
            return Redirect(SCREEN_TOPUP, toast=tx["gone"])
        result = packs.availability(f, cfg, at=now(), pack=pack)
        if not result.ok:
            return refusal_view(result, cfg)
        text = tx["confirm"].format(
            gb=pack.gb,
            price=_money(pack.amount_minor, pack.currency),
            reset=fmt_date(f.planned_end_at),
        )
        rows: list[list[Any]] = []
        if result.insufficient:
            text += "\n\n" + tx["insufficient"].format(over=fmt_gb(f.overage(), cfg.gb_bytes))
            rows.append([nav_button(tx["btn_anyway"], SCREEN_TOPUP, "buy", f"{group_id}:{pack_id}:1")])
        else:
            rows.append(
                [
                    nav_button(
                        tx["btn_confirm"], SCREEN_TOPUP, "buy", f"{group_id}:{pack_id}:0", style="success"
                    )
                ]
            )
        rows.append([nav_button(tx["btn_back"], SCREEN_TOPUP, arg=str(group_id))])
        return View(text=text, parse_mode="HTML", keyboard=rows)

    @router.action(SCREEN_TOPUP, "buy")
    async def buy(ctx: Any, arg: Any) -> Any:
        ids = _args(arg, 3, 3)
        if ids is None or ids[2] not in (0, 1):
            return Redirect(SCREEN_TOPUP)
        group_id, pack_id, force = ids
        svc = service()
        res = await packs.create_order(svc, ctx.user.user_id, group_id, pack_id, force=force == 1)
        if res.order_id is None:
            refusal = res.refusal or packs.Availability.refuse("no_period")
            if refusal.ok and refusal.insufficient:  # the overage grew meanwhile: ask again
                return Redirect(SCREEN_TOPUP, toast=T["gone"])
            return refusal_view(refusal, svc.cfg())
        return Redirect("co", str(res.order_id))

    @router.action(MODULE_SCREEN, ACTION_TOPUP)
    async def open_topup(ctx: Any, arg: Any) -> Any:
        del ctx, arg
        return Redirect(SCREEN_TOPUP)
