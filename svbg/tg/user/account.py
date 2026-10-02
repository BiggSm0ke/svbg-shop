"""«Подключиться» (Web App of the subscription page, copy, QR), devices and the link reissue.

* ``connect`` — the subscription page as a Web App button (``https`` only, a plain link otherwise), «📋
  Скопировать ссылку» (``copy_text``) and «🔳 QR-код» (``segno``, a PNG sent as a photo). The link always
  comes from the panel's answer (``subscription_url``). One SQL, no HTTP.
* ``dev`` — devices from the ``user_devices`` cache (one SQL with the status); a stale or missing cache queues
  ``user.devices_refresh`` (the panel is read in the job, never on the click). «🔄 Обновить» queues it too,
  but not more often than every :data:`REFRESH_MIN_S` (a fresher list is simply kept: the panel is not a
  thing one button can hammer). «🗑 N» unlinks a device through
  the writer's job (``SubscriptionActions.delete_device``: owner, state, freeze and cooldown in one CAS), «🧹
  Отвязать все» asks first.
* ``reissue`` — asks «старая ссылка перестанет работать», then ``panel.revoke`` + ``user.ui_ready`` in one
  transaction: the message turns into «✅ Новая ссылка готова + 🔗 Подключиться» after the panel job.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

import segno
from aiogram.types import BufferedInputFile, InlineKeyboardButton

from svbg.billing.ports import UiRef
from svbg.core.clock import now
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import MediaRef, Redirect, Toast, View
from svbg.tg.user import seeds
from svbg.tg.user.base import Base, parse_ids
from svbg.tg.user.deps import cfg_int
from svbg.tg.user.jobs import (
    DEVICES_FAILED,
    DEVICES_REFRESHED,
    enqueue_devices_refresh,
    enqueue_ui_ready,
    store_devices,
)
from svbg.tg.user.messenger import link_button
from svbg.tg.user.render import screen_view
from svbg.tg.user.texts import fmt_date, fmt_datetime, t

if TYPE_CHECKING:
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter
    from svbg.tg.user.jobs import DevicesWatch
    from svbg.tg.user.status import SubInfo, UserStatus

__all__ = ["REFRESH_MIN_S", "AccountScreens", "device_fingerprint", "qr_png"]

log = logging.getLogger("svbg.tg.user.account")

DEVICES_TTL_S: Final = 300
#: «Обновить» does not ask the panel again while the list is younger than this.
REFRESH_MIN_S: Final = 30
_PER_ROW: Final = 5


def qr_png(data: str) -> bytes:
    """A PNG QR code of ``data`` (error correction M, readable from a phone screen)."""
    out = io.BytesIO()
    segno.make(data, error="m").save(out, kind="png", scale=8, border=3)
    return out.getvalue()


def device_fingerprint(hwid: str) -> str:
    """8 hex chars identifying a device in callback data (the HWID itself never goes into a button)."""
    return hashlib.sha256(hwid.encode()).hexdigest()[:8]


def device_name(device: Mapping[str, Any], lang: str) -> str:
    parts = [str(device[k]) for k in ("model", "platform", "os_version") if device.get(k)]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return " · ".join(seen) or t(lang, "device_unknown")


class AccountScreens(Base):
    #: Who waits for the device list (set by :class:`~svbg.tg.user.wiring.UserPath`).
    watch: DevicesWatch | None = None

    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.CONNECT)(self.connect)
        router.screen(seeds.DEVICES)(self.devices)
        router.screen(seeds.DEVICES_RESET)(self.devices_reset)
        router.screen(seeds.REISSUE)(self.reissue)
        router.action(seeds.CONNECT, "qr")(self.qr)
        router.action(seeds.DEVICES, "del")(self.delete_device)
        router.action(seeds.DEVICES, "reset")(self.reset_devices)
        router.action(seeds.DEVICES, "refresh")(self.refresh_devices)
        router.action(seeds.REISSUE, "ok")(self.reissue_ok)

    # ------------------------------------------------------------------ connect

    def _until(self, sub: SubInfo) -> str:
        return fmt_datetime(sub.paid_until, self.tz) if sub.is_trial else fmt_date(sub.paid_until, self.tz)

    async def connect(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        status = await self.enrich(ctx)
        lang = ctx.lang
        sub = status.sub if status is not None else None
        state = status.sub_state(now()) if status is not None else "none"
        values = {"until": "—", "url": "", "state": ""}
        top: list[list[InlineKeyboardButton]] = []
        bottom: list[list[InlineKeyboardButton]] = []
        if sub is None or state == "expired":
            values["state"] = t(lang, "connect_none")
            bottom.append([nav_button(t(lang, "btn_buy"), seeds.BUY)])
        elif state == "frozen":
            values["state"] = t(lang, "connect_frozen")
            bottom.extend(self.support_row(lang))
        elif not sub.connectable:
            values["state"] = t(lang, "connect_pending")
            bottom.append([nav_button(t(lang, "btn_refresh"), seeds.CONNECT)])
        else:
            url = str(sub.subscription_url)
            values.update(
                until=self._until(sub), url=url, state=t(lang, "connect_ready", until=self._until(sub))
            )
            top.append([link_button(t(lang, "btn_open_page"), url)])
            second = [nav_button(t(lang, "btn_qr"), seeds.CONNECT, "qr")]
            copy = self.copy_button(lang, url)
            if copy is not None:
                second.insert(0, copy)
            top.append(second)
        return screen_view(ctx, seeds.CONNECT, values, top=top, bottom=bottom)

    async def qr(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        status = await self.status.load(ctx.user.user_id)
        sub = status.sub if status is not None else None
        if sub is None or not sub.connectable or not sub.subscription_url:
            return Redirect(seeds.CONNECT)
        url = str(sub.subscription_url)
        png = qr_png(url)
        key = "qr:" + hashlib.sha256(url.encode()).hexdigest()[:12]
        media = MediaRef("photo", BufferedInputFile(png, filename="subscription-qr.png"), key=key)
        back = [nav_button(t(ctx.lang, "btn_back"), seeds.CONNECT)]
        return View(text=t(ctx.lang, "qr_caption"), media=media, keyboard=[back])

    # ------------------------------------------------------------------ devices

    def _ttl(self) -> int:
        return max(10, cfg_int(self.deps.config, "DEVICES_CACHE_TTL_S", DEVICES_TTL_S))

    def _devices_view(
        self,
        ctx: ScreenCtx,
        sub: SubInfo,
        devices: Sequence[Mapping[str, Any]] | None,
        *,
        failed: bool = False,
    ) -> View:
        lang = ctx.lang
        limit = sub.device_limit
        values = {
            "count": str(len(devices)) if devices is not None else "…",
            "limit": t(lang, "devices_unlimited")
            if limit == 0
            else (str(limit) if limit is not None else "—"),
            "list": "",
            "note": "",
        }
        top: list[list[InlineKeyboardButton]] = []
        if devices is None:
            values["list"] = t(lang, "devices_unavailable" if failed else "devices_loading")
        elif not devices:
            values["list"] = t(lang, "devices_empty")
        else:
            lines = [
                t(lang, "device_line", n=i + 1, name=device_name(d, lang)) for i, d in enumerate(devices)
            ]
            values["list"] = "\n".join(lines)
            values["note"] = t(lang, "devices_note_delete")
            buttons = [
                nav_button(
                    t(lang, "btn_delete_device", n=i + 1),
                    seeds.DEVICES,
                    "del",
                    f"{i + 1}:{device_fingerprint(str(d.get('hwid') or ''))}",
                    style="danger",
                )
                for i, d in enumerate(devices[:40])
            ]
            top = [buttons[i : i + _PER_ROW] for i in range(0, len(buttons), _PER_ROW)]
            top.append([nav_button(t(lang, "btn_reset_devices"), seeds.DEVICES_RESET)])
            if failed:
                values["note"] += "\n\n" + t(lang, "devices_unavailable")
        top.append([nav_button(t(lang, "btn_refresh"), seeds.DEVICES, "refresh")])
        return screen_view(ctx, seeds.DEVICES, values, top=top)

    async def _live_sub(
        self, ctx: ScreenCtx, *, with_devices: bool
    ) -> tuple[UserStatus | None, SubInfo | None]:
        status = await self.enrich(ctx, with_devices=with_devices)
        sub = status.sub if status is not None else None
        if (
            sub is None
            or sub.link_state != "linked"
            or status is None
            or status.sub_state(now()) == "expired"
        ):
            return status, None
        return status, sub

    async def devices(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        status, sub = await self._live_sub(ctx, with_devices=True)
        if sub is None or status is None:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "no_subscription"))
        cache = status.devices
        from_job = arg in (DEVICES_REFRESHED, DEVICES_FAILED)
        if (cache is None or not cache.fresh(now(), self._ttl())) and not from_job:
            await self._refresh(ctx, sub)
        devices = cache.devices if cache is not None else None
        return self._devices_view(ctx, sub, devices, failed=arg == DEVICES_FAILED)

    async def _refresh(self, ctx: ScreenCtx, sub: SubInfo) -> None:
        if self.deps.fetch_devices is None or sub.panel_user_id is None:
            return
        async with self.deps.db.tx() as conn:
            await enqueue_devices_refresh(
                conn, subscription_id=sub.id, user_id=ctx.user.user_id, chat_id=ctx.chat_id
            )
        if self.watch is not None:
            self.watch.arm(ctx.user.telegram_id)

    async def refresh_devices(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        status, sub = await self._live_sub(ctx, with_devices=True)
        if sub is None or status is None:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "no_subscription"))
        cache = status.devices
        if cache is not None and cache.fresh(now(), REFRESH_MIN_S):
            view = self._devices_view(ctx, sub, cache.devices)  # the list the job has just stored
            view.toast = t(ctx.lang, "devices_fresh")
            return view
        await self._refresh(ctx, sub)
        return Toast(t(ctx.lang, "devices_loading"))

    async def delete_device(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        lang = ctx.lang
        if not isinstance(arg, str) or ":" not in arg:
            return Redirect(seeds.DEVICES)
        num, _, fp = arg.partition(":")
        ids = parse_ids(num, 1)
        actions = self.deps.actions
        if ids is None or actions is None:
            return Redirect(seeds.DEVICES)
        status, sub = await self._live_sub(ctx, with_devices=True)
        if sub is None or status is None:
            return Redirect(seeds.HOME, toast=t(lang, "no_subscription"))
        devices = list(status.devices.devices) if status.devices is not None else []
        index = ids[0] - 1
        if not 0 <= index < len(devices) or device_fingerprint(str(devices[index].get("hwid") or "")) != fp:
            return Toast(t(lang, "devices_gone"), alert=True)
        hwid = str(devices[index].get("hwid"))
        # ``SubscriptionActions.delete_device`` only queues the writer's ``panel.hwid_delete`` job in this
        # transaction (the panel itself is called by the single writer); bound here so the name of the
        # producer is not mistaken for the panel API's mutating ``delete_device``.
        queue_delete = actions.delete_device
        async with self.deps.db.tx() as conn:
            result = await queue_delete(
                conn, sub.id, hwid, user_id=ctx.user.user_id, caused_by=f"user:{ctx.user.user_id}"
            )
            if result.ok:
                del devices[index]
                await store_devices(conn, sub.id, [dict(d) for d in devices])
        if not result.ok:
            return Toast(result.localized(lang), alert=True)
        view = self._devices_view(ctx, sub, devices)
        view.toast = t(lang, "device_deleting")
        return view

    async def devices_reset(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        status, sub = await self._live_sub(ctx, with_devices=True)
        if sub is None or status is None:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "no_subscription"))
        count = len(status.devices.devices) if status.devices is not None else 0
        lang = ctx.lang
        top = [[nav_button(t(lang, "btn_confirm_reset"), seeds.DEVICES, "reset", style="danger")]]
        bottom = [self.back(lang, seeds.DEVICES)]
        return screen_view(ctx, seeds.DEVICES_RESET, {"count": str(count)}, top=top, bottom=bottom)

    async def reset_devices(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        actions = self.deps.actions
        _status, sub = await self._live_sub(ctx, with_devices=False)
        if sub is None or actions is None:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "no_subscription"))
        async with self.deps.db.tx() as conn:
            result = await actions.reset_devices(
                conn, sub.id, user_id=ctx.user.user_id, caused_by=f"user:{ctx.user.user_id}"
            )
            if result.ok:
                await store_devices(conn, sub.id, [])
        if not result.ok:
            return Toast(result.localized(ctx.lang), alert=True)
        view = self._devices_view(ctx, sub, [])
        view.toast = t(ctx.lang, "devices_resetting")
        return view

    # ------------------------------------------------------------------ reissue

    async def reissue(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        lang = ctx.lang
        top = [[nav_button(t(lang, "btn_confirm_reissue"), seeds.REISSUE, "ok", style="danger")]]
        return screen_view(ctx, seeds.REISSUE, top=top, bottom=[self.back(lang, seeds.CONNECT)])

    async def reissue_ok(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        actions = self.deps.actions
        _status, sub = await self._live_sub(ctx, with_devices=False)
        if sub is None or actions is None:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "no_subscription"))
        ref = UiRef(ctx.chat_id, ctx.message_id, now()) if ctx.message_id is not None else None
        async with self.deps.db.tx() as conn:
            result = await actions.reissue_link(
                conn, sub.id, user_id=ctx.user.user_id, caused_by=f"user:{ctx.user.user_id}"
            )
            if result.ok:
                await enqueue_ui_ready(
                    conn, "reissue", subscription_id=sub.id, user_id=ctx.user.user_id, ui_ref=ref
                )
        if not result.ok:
            return Toast(result.localized(ctx.lang), alert=True)
        return screen_view(ctx, seeds.REISSUE_WAIT, bottom=[self.menu(ctx.lang)])
