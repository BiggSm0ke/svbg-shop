"""IP Guard in Telegram: buttons of the cards in «🛡 Антиабуз», the admin screen ``ipguard`` and the module
actions of the admin user card (05 §2.2.2).

**Every press is authorised by the bot's own roles**, read from the database at the press (a member of the
admin group is not an admin of the bot): view — staff (support and up); block / anomaly — ``ip_guard.block``;
unblock / close — ``ip_guard.unblock``; exempt list and CDN flags — ``ip_guard.config``. The owner has all.
Card texts and keyboards are rebuilt from the database, never from ``callback.message``.

:class:`CardActions` holds the logic (testable without aiogram); :func:`cards_router` is the thin aiogram
adapter the integration includes into the dispatcher; :func:`install_screens` registers the admin screen and
the ``mod`` actions on the :class:`~svbg.tg.ui.router.ScreenRouter`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import DeleteMessage, EditMessageReplyMarkup, SendDocument
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from svbg.core.clock import now
from svbg.ext.ip_guard import service as svc
from svbg.ext.ip_guard import texts
from svbg.ext.ip_guard.tables import ip_guard_alerts, ip_guard_blocks, ip_guard_exempt, ip_guard_nodes
from svbg.services.roles import load_actor
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from svbg.db.engine import Database
    from svbg.ext.ip_guard.service import IpGuardService

__all__ = [
    "PERM_BLOCK",
    "PERM_CONFIG",
    "PERM_UNBLOCK",
    "PERM_VIEW",
    "SCREEN",
    "CardActions",
    "Press",
    "cards_router",
    "install_screens",
]

log = logging.getLogger("svbg.ext.ip_guard.tg")

PERM_VIEW: Final = "ip_guard.view"
PERM_BLOCK: Final = "ip_guard.block"
PERM_UNBLOCK: Final = "ip_guard.unblock"
PERM_CONFIG: Final = "ip_guard.config"
SCREEN: Final = "ipguard"
PAGE: Final = 10

_DATA_RE: Final = re.compile(r"^ipg:([a-z]{1,4})(?::(block|alert))?(?::(\d{1,18}))?(?::(\d{1,18}))?$")
_PERMS: Final = {
    "u": PERM_UNBLOCK,
    "uy": PERM_UNBLOCK,
    "ur": PERM_UNBLOCK,
    "x": PERM_UNBLOCK,
    "xy": PERM_UNBLOCK,
    "ok": PERM_VIEW,
    "b": PERM_BLOCK,
    "by": PERM_BLOCK,
    "ab": PERM_BLOCK,
    "aby": PERM_BLOCK,
    "ad": PERM_BLOCK,
    "dg": PERM_VIEW,
    "ip": PERM_VIEW,
    "c": PERM_VIEW,
}
_STAFF: Final = ("support", "admin", "owner")
_T: Final = {
    "no_rights": "⛔ Только для администраторов бота",
    "bad": "Кнопка устарела",
    "confirm": "Подтвердите действие",
    "file": "Файл отправлен",
    "no_file": "Нет данных",
    "failed": "Не получилось, попробуйте ещё раз",
}

Keyboard = list[list[InlineKeyboardButton]]
OwnerIds = Callable[[], Awaitable[frozenset[int]]]


@dataclass(frozen=True, slots=True)
class Press:
    """What to do with a press: toast, optional new keyboard of the card, optional file to send."""

    toast: str
    alert: bool = False
    keyboard: Keyboard | None = None
    document: BufferedInputFile | None = None
    delete: bool = False


class CardActions:
    def __init__(self, service: Callable[[], IpGuardService], db: Database, owner_ids: OwnerIds) -> None:
        self._service = service
        self._db = db
        self._owner_ids = owner_ids

    async def allowed(self, telegram_id: int, perm: str) -> tuple[bool, int | None]:
        """Role re-read now (1 SQL). Returns ``(allowed, users.id of the actor)``."""
        ok, user_id, _role = await self._check(telegram_id, perm)
        return ok, user_id

    async def _check(self, telegram_id: int, perm: str) -> tuple[bool, int | None, str | None]:
        """``(allowed, users.id, role)`` of the presser, read now (1 SQL); the role is for ``admin_audit``."""
        async with self._db.read() as conn:
            actor = await load_actor(conn, telegram_id=telegram_id, owner_ids=await self._owner_ids())
        if actor is None or actor.banned:
            return False, None, None
        if perm == PERM_VIEW:
            return actor.role in _STAFF, actor.user_id, actor.role
        return actor.has_perm(perm), actor.user_id, actor.role

    async def press(self, data: str, telegram_id: int) -> Press:
        if data == "ipg:close":
            return Press("", delete=True)
        match = _DATA_RE.match(data)
        if match is None or match[1] not in _PERMS:
            return Press(_T["bad"])
        act, kind, raw_id, extra = match[1], match[2], match[3], match[4]
        if raw_id is None:
            return Press(_T["bad"])
        ident = int(raw_id)
        ok, actor_id, role = await self._check(telegram_id, _PERMS[act])
        if not ok:
            return Press(_T["no_rights"], alert=True)
        service = self._service()
        if act == "u":
            return Press(_T["confirm"], keyboard=svc.unblock_confirm_keyboard(ident))
        if act in ("uy", "ur"):
            res = await service.unblock(ident, actor_id=actor_id, revoke=act == "ur", actor_role=role)
            return Press(res.text, keyboard=await self._card_keyboard(f"block:{ident}"))
        if act == "x":
            return Press(texts.T["close_pick"], keyboard=svc.close_confirm_keyboard(ident))
        if act == "xy":
            # the term is zeroed: only with a reason picked on the confirmation (a reason-less press re-asks)
            if extra is None or int(extra) >= len(texts.CLOSE_REASONS):
                return Press(texts.T["close_pick"], keyboard=svc.close_confirm_keyboard(ident))
            reason = texts.CLOSE_REASONS[int(extra)]
            res = await service.close(ident, actor_id=actor_id, reason=reason, actor_role=role)
            return Press(res.text, keyboard=await self._card_keyboard(f"block:{ident}"))
        if act == "ok" and kind is not None:
            res = await service.confirm(f"{kind}:{ident}", actor_id=actor_id)
            return Press(res.text, keyboard=await self._card_keyboard(f"{kind}:{ident}"))
        if act == "b":
            return Press(_T["confirm"], keyboard=svc.block_confirm_keyboard(ident))
        if act == "by":
            res = await service.block_from_alert(ident, actor_id=actor_id, actor_role=role)
            return Press(res.text, keyboard=await self._card_keyboard(f"alert:{ident}"))
        if act in ("ab", "aby") and extra is not None:
            if act == "ab":
                return Press(_T["confirm"], keyboard=svc.anomaly_confirm_keyboard(ident, int(extra)))
            res = await service.anomaly_block(ident, actor_id=actor_id, expected=int(extra), actor_role=role)
            return Press(
                res.text, alert=res.code == "changed", keyboard=await self._card_keyboard(f"alert:{ident}")
            )
        if act == "ad":
            res = await service.anomaly_dismiss(ident, actor_id=actor_id)
            return Press(res.text, keyboard=await self._card_keyboard(f"alert:{ident}"))
        if act == "dg" and extra is not None:
            res = await service.digest_member(ident, extra)
            return Press(res.text)
        if act == "ip" and kind is not None:
            doc = await service.ips_document(f"{kind}:{ident}")
            return Press(_T["file"] if doc else _T["no_file"], document=doc)
        if act == "c" and kind is not None:
            return Press("", keyboard=await self._card_keyboard(f"{kind}:{ident}"))
        return Press(_T["bad"])

    async def _card_keyboard(self, ref: str) -> Keyboard | None:
        rendered = await self._service().render_card(ref)
        return None if rendered is None else rendered[1]


def cards_router(actions: CardActions, *, name: str = "svbg-ip-guard-cards") -> Router:
    """Callback buttons ``ipg:*`` of the cards (admin chat or owner DMs) and of the user's block message."""
    router = Router(name=name)

    @router.callback_query(F.data.startswith("ipg:"))
    async def on_press(query: CallbackQuery) -> None:
        try:
            result = await actions.press(query.data or "", query.from_user.id)
        except Exception as exc:  # noqa: BLE001 - a press never breaks the bot; reported as a warning
            log.warning("ip guard press failed: %s", type(exc).__name__)
            result = Press(_T["failed"])
        message = query.message
        bot = query.bot
        try:
            if bot is not None and message is not None and hasattr(message, "message_id"):
                chat_id = message.chat.id
                if result.delete:
                    await bot(DeleteMessage(chat_id=chat_id, message_id=message.message_id))
                elif result.keyboard is not None:
                    await bot(
                        EditMessageReplyMarkup(
                            chat_id=chat_id,
                            message_id=message.message_id,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=result.keyboard),
                        )
                    )
                if result.document is not None:
                    await bot(
                        SendDocument(
                            chat_id=chat_id,
                            document=result.document,
                            message_thread_id=getattr(message, "message_thread_id", None),
                        )
                    )
        except TelegramAPIError as exc:
            log.warning("ip guard card update failed: %s", type(exc).__name__)
        try:
            await query.answer(result.toast[:190] or None, show_alert=result.alert)
        except TelegramAPIError as exc:
            log.warning("answerCallbackQuery failed: %s", type(exc).__name__)

    return router


# ------------------------------------------------------------------------------------- admin screen


SECTIONS: Final = (
    ("a", "Активные"),
    ("w", "Ждут панель"),
    ("p", "Предупреждения"),
    ("q", "Карантин"),
    ("n", "Ноды"),
    ("e", "Белый список"),
)


async def screen_rows(db: Database, section: str, page: int) -> tuple[list[str], list[tuple[str, str]], bool]:
    """Lines of a section page, ``(button text, action arg)`` pairs, and whether there is a next page.

    One SQL per screen (``LIMIT PAGE + 1``)."""
    offset = page * PAGE
    b = ip_guard_blocks.c
    a = ip_guard_alerts.c
    s = subscriptions.c
    lines: list[str] = []
    buttons: list[tuple[str, str]] = []
    async with db.read() as conn:
        if section in ("a", "w"):
            cond = (
                b.status == "active"
                if section == "a"
                else sa.and_(
                    b.status == "unblocked", s.panel_status == "DISABLED", s.desired_status == "active"
                )
            )
            rows = (
                await conn.execute(
                    sa.select(b.id, b.subscription_id, b.ip_count, b.blocked_at)
                    .select_from(ip_guard_blocks.join(subscriptions, s.id == b.subscription_id))
                    .where(cond)
                    .order_by(b.blocked_at.desc())
                    .offset(offset)
                    .limit(PAGE + 1)
                )
            ).all()
            for r in rows[:PAGE]:
                lines.append(
                    f"• №{int(r.subscription_id)} — {int(r.ip_count)} IP, {texts.fmt_dt(r.blocked_at)}"
                )
                buttons.append((f"🔄 Карточка №{int(r.subscription_id)}", f"block:{int(r.id)}"))
        elif section in ("p", "q"):
            cond = (
                sa.and_(
                    a.kind.in_(("warn", "not_blocked", "block_failed")),
                    a.created_at > now() - timedelta(days=1),
                )
                if section == "p"
                else sa.and_(a.kind == "anomaly", a.quarantine_until > now() - timedelta(hours=1))
            )
            rows = (
                await conn.execute(
                    sa.select(a.id, a.kind, a.reason, a.subscription_id, a.metrics, a.created_at)
                    .where(cond)
                    .order_by(a.created_at.desc())
                    .offset(offset)
                    .limit(PAGE + 1)
                )
            ).all()
            for r in rows[:PAGE]:
                label = (
                    texts.KIND_TITLES.get(str(r.reason or r.kind), str(r.kind))
                    .split("(")[0]
                    .split("{")[0]
                    .strip()
                )
                sub = f"№{int(r.subscription_id)}" if r.subscription_id else "—"
                ips = int((r.metrics or {}).get("ip_count") or 0)
                lines.append(f"• {texts.fmt_time(r.created_at)} {sub} — {ips} IP · {texts.esc(label, 60)}")
                buttons.append((f"🔄 Карточка {texts.fmt_time(r.created_at)} {sub}", f"alert:{int(r.id)}"))
        elif section == "n":
            n = ip_guard_nodes.c
            rows = (
                await conn.execute(
                    sa.select(n.node_uuid, n.name, n.cdn)
                    .order_by(n.name, n.node_uuid)
                    .offset(offset)
                    .limit(PAGE + 1)
                )
            ).all()
            for r in rows[:PAGE]:
                flag = "☁️ CDN — не проверяется" if r.cdn else "проверяется"
                lines.append(f"• {texts.esc(r.name or r.node_uuid[:8], 40)}: {flag}")
                buttons.append(
                    (
                        ("Снять CDN: " if r.cdn else "Это CDN: ") + (r.name or r.node_uuid[:8])[:30],
                        f"node:{r.node_uuid}",
                    )
                )
        else:
            e = ip_guard_exempt.c
            rows = (
                await conn.execute(
                    sa.select(e.subscription_id, e.reason, e.until)
                    .where(sa.or_(e.until.is_(None), e.until > now()))
                    .order_by(e.created_at.desc())
                    .offset(offset)
                    .limit(PAGE + 1)
                )
            ).all()
            for r in rows[:PAGE]:
                until = f" до {texts.fmt_dt(r.until)}" if r.until else ""
                lines.append(f"• №{int(r.subscription_id)} — {texts.esc(r.reason, 80)}{until}")
                buttons.append((f"Убрать №{int(r.subscription_id)}", f"ex:{int(r.subscription_id)}"))
    return lines, buttons, len(rows) > PAGE


def install_screens(router: Any, service: Callable[[], IpGuardService], db: Database) -> None:
    """Admin screen ``ipguard`` (sections, 10 per page) and the ``mod`` actions of the user card slot."""
    from svbg.tg.ui.renderer import MODULE_SCREEN, nav_button
    from svbg.tg.ui.view import Redirect, Toast, View

    def parse(arg: Any) -> tuple[str, int]:
        text = str(arg or "a:0")
        section, _, raw = text.partition(":")
        if section not in {c for c, _ in SECTIONS}:
            section = "a"
        page = int(raw) if raw.isdigit() and len(raw) < 5 else 0
        return section, page

    @router.screen(SCREEN, required_role="support", perm=None)
    async def ipguard_screen(ctx: Any, arg: Any) -> Any:
        if not (ctx.user.at_least("support")):
            return Toast(_T["no_rights"], alert=True)
        section, page = parse(arg)
        lines, buttons, more = await screen_rows(db, section, page)
        title = dict(SECTIONS)[section]
        auto = "включён" if service().params().auto_block else "выключен"
        text = "\n".join([f"🛡 <b>IP Guard · {title}</b>", f"Автоблок: {auto}", "", *(lines or ["Пусто."])])
        keyboard: list[list[InlineKeyboardButton]] = []
        keyboard.append([nav_button(t, SCREEN, arg=f"{c}:0") for c, t in SECTIONS[:3]])
        keyboard.append([nav_button(t, SCREEN, arg=f"{c}:0") for c, t in SECTIONS[3:]])
        for label, ref in buttons:
            act = (
                "card"
                if ref.startswith(("block:", "alert:"))
                else "node"
                if ref.startswith("node:")
                else "exempt"
            )
            value = ref if act == "card" else ref.split(":", 1)[1]
            if act == "node" and len(f"v1:{SCREEN}:{act}:{value}".encode()) > 64:
                continue
            keyboard.append([nav_button(label[:60], SCREEN, act, value)])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(nav_button("◀️", SCREEN, arg=f"{section}:{page - 1}"))
        if more:
            nav.append(nav_button("▶️", SCREEN, arg=f"{section}:{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([nav_button("🏠 Меню", "home")])
        return View(text=text, parse_mode="HTML", keyboard=keyboard)

    async def _perm(ctx: Any, perm: str) -> bool:
        user = ctx.user
        return bool(user.has_perm(perm)) if perm != PERM_VIEW else bool(user.at_least("support"))

    @router.action(SCREEN, "card", required_role="support")
    async def resend(ctx: Any, arg: Any) -> Any:
        res = await service().resend_card(str(arg or ""))
        return Toast(res.text)

    @router.action(SCREEN, "node", required_role="owner")
    async def toggle_node(ctx: Any, arg: Any) -> Any:
        if not await _perm(ctx, PERM_CONFIG):
            return Toast(_T["no_rights"], alert=True)
        uuid = str(arg or "")
        async with db.read() as conn:
            cdn = await conn.scalar(sa.select(ip_guard_nodes.c.cdn).where(ip_guard_nodes.c.node_uuid == uuid))
        if cdn is None:
            return Toast("Нода не найдена")
        res = await service().set_cdn(uuid, cdn=not cdn)
        return Redirect(SCREEN, "n:0", toast=res.text)

    @router.action(SCREEN, "exempt", required_role="admin")
    async def remove_exempt(ctx: Any, arg: Any) -> Any:
        if not await _perm(ctx, PERM_CONFIG) or not str(arg or "").isdigit():
            return Toast(_T["no_rights"], alert=True)
        res = await service().set_exempt(int(arg), on=False, actor_id=ctx.user.user_id)
        return Redirect(SCREEN, "e:0", toast=res.text)

    def _mod(action: str, perm: str, fn: Callable[[Any, int], Awaitable[Any]]) -> None:
        async def handler(ctx: Any, arg: Any) -> Any:
            if not await _perm(ctx, perm):
                return Toast(_T["no_rights"], alert=True)
            raw = str(arg or "")
            if not raw.isdigit() or len(raw) > 18:
                return Toast(_T["bad"])
            res = await fn(ctx, int(raw))
            return Toast(res.text)

        router.action(MODULE_SCREEN, f"ip_guard.{action}", required_role="support")(handler)

    _mod(
        "unblock",
        PERM_UNBLOCK,
        lambda ctx, i: service().unblock(i, actor_id=ctx.user.user_id, actor_role=ctx.user.role),
    )
    _mod(
        "block",
        PERM_BLOCK,
        lambda ctx, i: service().manual_block(i, actor_id=ctx.user.user_id, actor_role=ctx.user.role),
    )
    _mod("exempt_on", PERM_CONFIG, lambda ctx, i: service().set_exempt(i, on=True, actor_id=ctx.user.user_id))
    _mod(
        "exempt_off", PERM_CONFIG, lambda ctx, i: service().set_exempt(i, on=False, actor_id=ctx.user.user_id)
    )


def sections() -> Sequence[tuple[str, str]]:
    return SECTIONS
