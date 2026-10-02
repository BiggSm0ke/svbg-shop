"""Glue between the extension host and :class:`~svbg.ext.ip_guard.service.IpGuardService`.

The host hands periodic tasks and hooks a :class:`~svbg.ext.api.ModuleContext`, but job handlers only get
``(job, job_ctx)``. Jobs of this module must run even while it is switched off (a drop or a card of a block
made earlier), so the context is remembered at the first hook the host calls (``ui`` at start, or ``setup``)
and the service is built from its dependencies on first use.

Dependencies (``ModuleContext.deps``): ``db`` (required), ``api`` (callable → ``RemnawaveApi``, required),
``admin_chat``, ``notifier``, ``attention``, ``owner_ids`` (async callable → ``frozenset[int]``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from svbg.core.component import HealthReport
from svbg.ext.api import SlotButton, SlotCall, SlotResult
from svbg.ext.ip_guard import texts
from svbg.ext.ip_guard.panel import DropIpsJob
from svbg.ext.ip_guard.service import IpGuardService
from svbg.ext.ip_guard.tables import ip_guard_blocks, ip_guard_exempt
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.ext.api import ModuleContext
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext

__all__ = [
    "RUNTIME",
    "Runtime",
    "card_job",
    "collect",
    "drop_job",
    "health",
    "load_admin_card",
    "load_subscription",
    "notify_job",
    "purge",
    "render_admin_section",
    "render_banner",
    "render_status_line",
    "report",
    "setup",
    "status",
    "teardown",
]

log = logging.getLogger("svbg.ext.ip_guard")


class Runtime:
    def __init__(self) -> None:
        self.ctx: ModuleContext | None = None
        self._service: IpGuardService | None = None
        self._drop: DropIpsJob | None = None

    def bind(self, ctx: ModuleContext) -> None:
        if self.ctx is not ctx:
            self.ctx = ctx
            self._service = None
            self._drop = None

    def service(self) -> IpGuardService:
        if self._service is None:
            ctx = self._ctx()
            deps = ctx.deps
            self._service = IpGuardService(
                ctx.dep("db"),
                ctx.dep("api"),
                config=ctx.config,
                admin_chat=deps.get("admin_chat"),
                notifier=deps.get("notifier"),
                attention=deps.get("attention"),
            )
        return self._service

    def drop(self) -> DropIpsJob:
        if self._drop is None:
            ctx = self._ctx()
            self._drop = DropIpsJob(ctx.dep("db"), ctx.dep("api"), attention=ctx.deps.get("attention"))
        return self._drop

    def _ctx(self) -> ModuleContext:
        if self.ctx is None:
            raise LookupError("ip_guard: the module context is not bound yet")
        return self.ctx

    def set_service(self, service: IpGuardService) -> None:
        """Tests and the integration may inject a ready service."""
        self._service = service


RUNTIME = Runtime()


# ------------------------------------------------------------------------------------------- lifecycle


async def setup(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    ctx.dep("db")
    ctx.dep("api")
    RUNTIME.service()


async def teardown(ctx: ModuleContext) -> None:
    del ctx
    if RUNTIME._service is not None:
        RUNTIME._service.reset()


async def collect(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().run_pass()


async def purge(ctx: ModuleContext) -> None:
    RUNTIME.bind(ctx)
    await RUNTIME.service().purge()


async def health(ctx: ModuleContext) -> HealthReport:
    RUNTIME.bind(ctx)
    return await RUNTIME.service().health()


async def status(ctx: ModuleContext) -> Sequence[str]:
    RUNTIME.bind(ctx)
    return await RUNTIME.service().status_lines()


async def report(ctx: ModuleContext) -> Sequence[str]:
    RUNTIME.bind(ctx)
    return await RUNTIME.service().report_lines()


async def card_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.service().card_job(job, jctx)


async def notify_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.service().notify_job(job, jctx)


async def drop_job(job: Job, jctx: JobContext) -> None:
    await RUNTIME.drop()(job, jctx)


# ---------------------------------------------------------------------------------------------- slots


def _uid(user: Any) -> int | None:
    value = getattr(user, "user_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def load_subscription(conn: AsyncConnection, user: Any, view: Mapping[str, Any]) -> Any:
    """Read model of the user's screens: the frozen subscription, if any (one query; none when the screen
    already loaded the subscription row with its ``hold_kind``)."""
    sub = view.get("subscription") if isinstance(view, Mapping) else None
    if isinstance(sub, Mapping) and "hold_kind" in sub:
        if sub.get("hold_kind") != "ip_guard":
            return None
        return {"frozen_seconds": int(sub.get("hold_frozen_seconds") or 0)}
    uid = _uid(user)
    if uid is None:
        return None
    row = (
        await conn.execute(
            sa.select(subscriptions.c.hold_frozen_seconds)
            .where(subscriptions.c.user_id == uid, subscriptions.c.hold_kind == "ip_guard")
            .limit(1)
        )
    ).first()
    return None if row is None else {"frozen_seconds": int(row.hold_frozen_seconds or 0)}


def _lang(call: SlotCall) -> str:
    lang = getattr(call.user, "lang", "ru")
    return lang if lang in ("ru", "en") else "ru"


def _support_button(call: SlotCall) -> tuple[SlotButton, ...]:
    try:
        url = call.module.config().get("SUPPORT_URL")
    except Exception:  # noqa: BLE001 - no button rather than a broken screen
        url = None
    if isinstance(url, str) and url.startswith(("https://", "tg://")):
        return (SlotButton(texts.user_t("btn_support", _lang(call)), url=url),)
    return ()


def render_banner(call: SlotCall) -> SlotResult | None:
    """``subscription.banner``: the frozen subscription replaces the screen (support + back only)."""
    model = call.model
    if not model:
        return None
    lang = _lang(call)
    text = texts.user_t("banner", lang).format(
        left=texts.fmt_duration(int(model.get("frozen_seconds") or 0), lang)
    )
    return SlotResult(lines=(text,), buttons=_support_button(call), banner=True)


def render_status_line(call: SlotCall) -> SlotResult | None:
    """``home.status_lines``."""
    if not call.model:
        return None
    return SlotResult(lines=(texts.user_t("status_line", _lang(call)),))


async def load_admin_card(conn: AsyncConnection, user: Any, view: Mapping[str, Any]) -> Any:
    """Read model of the admin user card: active / last block and the exempt flag of the viewed user."""
    del user
    target = view.get("user_id") or view.get("target_user_id")
    if isinstance(target, bool) or not isinstance(target, int):
        return None
    b = ip_guard_blocks.c
    row = (
        (
            await conn.execute(
                sa.select(
                    subscriptions.c.id.label("sid"),
                    b.id.label("block_id"),
                    b.status,
                    b.blocked_at,
                    b.ip_count,
                    ip_guard_exempt.c.until.label("exempt_until"),
                    ip_guard_exempt.c.subscription_id.label("exempt_sid"),
                )
                .select_from(
                    subscriptions.outerjoin(
                        ip_guard_blocks,
                        sa.and_(b.subscription_id == subscriptions.c.id, b.status != "unblocked"),
                    ).outerjoin(ip_guard_exempt, ip_guard_exempt.c.subscription_id == subscriptions.c.id)
                )
                .where(subscriptions.c.user_id == target, subscriptions.c.link_state != "closed")
                .order_by(b.blocked_at.desc().nulls_last())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    return None if row is None else dict(row)


def render_admin_section(call: SlotCall) -> SlotResult | None:
    """``admin.user_card.sections``: «IP Guard» block and «🔓 Снять IP-блок»."""
    m = call.model
    if not m:
        return None
    sid = int(m["sid"])
    buttons: list[SlotButton] = []
    if m.get("block_id") is not None and m.get("status") in ("active", "closed"):
        line = f"🛡 IP Guard: блок с {texts.fmt_dt(m.get('blocked_at'))}, {int(m.get('ip_count') or 0)} IP"
        buttons.append(
            SlotButton(
                "🔓 Снять IP-блок", action="ip_guard.unblock", arg=str(m["block_id"]), perm="ip_guard.unblock"
            )
        )
    else:
        line = "🛡 IP Guard: блоков нет"
        buttons.append(
            SlotButton(
                "🚫 IP-блок", action="ip_guard.block", arg=str(sid), perm="ip_guard.block", style="danger"
            )
        )
    exempt = m.get("exempt_sid") is not None and m.get("exempt_until") is None
    if exempt:
        line += " · в белом списке"
        buttons.append(
            SlotButton(
                "Убрать из белого списка", action="ip_guard.exempt_off", arg=str(sid), perm="ip_guard.config"
            )
        )
    else:
        buttons.append(
            SlotButton("В белый список", action="ip_guard.exempt_on", arg=str(sid), perm="ip_guard.config")
        )
    return SlotResult(lines=(line,), buttons=tuple(buttons))
