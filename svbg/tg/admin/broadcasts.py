"""«📣 Рассылки» — compose, audience, test, start and control broadcasts (07 §2.4.5, 04 §9.1).

Screens (code-defined on the :class:`~svbg.tg.ui.router.ScreenRouter`):

* ``bc`` — the list: «➕ Новая рассылка» and the last broadcasts;
* ``bc.c`` — the card: message summary, buttons, audience with the recipient count, options (📌 pin,
  🔕 silent, 🗑 delete after N h), «👁 Тест себе», «🚀 Отправить»; for a running one — progress and
  «Пауза» / «Продолжить» / «Остановить»;
* ``bc.s`` — audience presets + «✍️ Своё условие» (the visibility DSL as JSON, compiled to SQL);
* ``bc.ok`` — confirmation with the recipient count (04 §9.1: «подтверждение с числом получателей»).

Input: the admin **sends or forwards** the ready message (any formatting, Premium emoji, spoilers, media) —
:func:`svbg.broadcasts.message.normalize` keeps the source and a normalized copy; the button list and the
DSL are typed as messages too. The waiting state lives in ``ui_state.awaiting`` (``{"kind": "bc", …}``) and
is handled by this module's aiogram router, which runs before the screen router.

Access: owner, or admin with the ``broadcast`` permission — checked on every screen, action and message.
Arguments from callbacks are re-validated (callback data can be forged).
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from svbg.broadcasts.message import (
    ComposeError,
    describe,
    has_custom_emoji,
    normalize,
    parse_buttons,
)
from svbg.broadcasts.repo import Audience, AudienceConfig, Broadcast, BroadcastRepo
from svbg.broadcasts.segments import PRESETS
from svbg.broadcasts.sender import (
    CARD_SCREEN,
    CONTROL_SCREEN,
    STATUS_LABELS,
    BroadcastSender,
    Outcome,
    progress_text,
)
from svbg.broadcasts.service import Actor, BroadcastError, BroadcastService
from svbg.content.defaults import HOME
from svbg.core import clock
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View

if TYPE_CHECKING:
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "ACTIONS",
    "PERM",
    "SCREEN_CARD",
    "SCREEN_CONFIRM",
    "SCREEN_LIST",
    "SCREEN_SEGMENT",
    "SEND_PERM",
    "BroadcastScreens",
    "setup",
]

log = logging.getLogger("svbg.tg.admin.broadcasts")

PERM: Final = "broadcast"
#: Start / resume to the audience (integration decision C4); drafts, preview and «Тест себе» need only PERM.
SEND_PERM: Final = "broadcast.send"
#: Actions that send to the audience.
_SEND_ACTIONS: Final = frozenset({"go", "resume"})
SCREEN_LIST: Final = "bc"
SCREEN_CARD: Final = CARD_SCREEN
SCREEN_SEGMENT: Final = "bc.s"
SCREEN_CONFIRM: Final = "bc.ok"
ACTIONS: Final = CONTROL_SCREEN
AWAIT_KIND: Final = "bc"
AWAIT_TTL: Final = timedelta(minutes=15)
RECENT: Final = 8
_ID_RE: Final = re.compile(r"^\d{1,12}$")
_DSL_MAX: Final = 4000

_T: Final[dict[str, str]] = {
    "list_title": "📣 <b>Рассылки</b>",
    "list_hint": (
        "Нажмите «Новая рассылка» и пришлите или перешлите готовое сообщение — форматирование, "
        "премиум-эмодзи, спойлеры и медиа сохранятся. Перед отправкой будет тест себе и число получателей."
    ),
    "list_empty": "Рассылок пока не было.",
    "new": "➕ Новая рассылка",
    "menu": "🏠 Меню",
    "to_list": "⬅️ К рассылкам",
    "to_card": "⬅️ К рассылке",
    "cancel": "✖️ Отмена",
    "compose": (
        "📣 <b>Новая рассылка</b>\n\nПришлите или перешлите сюда готовое сообщение: текст, фото, GIF, видео, "
        "файл, аудио или голосовое. Форматирование, премиум-эмодзи и спойлеры сохранятся."
    ),
    "replace": "✏️ <b>Новое сообщение для рассылки #{id}</b>\n\nПришлите или перешлите его сюда.",
    "buttons": (
        "🔘 <b>Кнопки рассылки #{id}</b>\n\n"
        "Одна строка — один ряд, кнопки в ряду — через <code>;;</code>\n"
        "Кнопка: <code>Текст | действие</code> или <code>Текст | действие | цвет</code> "
        "(синяя, зелёная, красная).\n"
        "Действие: ссылка, <code>screen:код</code>, <code>system:имя</code>, <code>deeplink:код</code>, "
        "<code>copy:текст</code>.\n\n"
        "Пример:\n<code>🛒 Купить | screen:buy | зелёная\nНаш канал | https://t.me/channel ;; "
        "Баланс | screen:bal</code>\n\nПремиум-эмодзи в начале текста станет значком кнопки."
    ),
    "dsl": (
        "✍️ <b>Своё условие для рассылки #{id}</b>\n\nПришлите JSON условия, как у кнопок конструктора. "
        'Например:\n<code>{{"all": [{{"sub": "active"}}, {{"days_left": {{"lte": 3}}}}]}}</code>\n\n'
        "Доступно: sub, days_left, balance_minor, has_paid, plan, lang, role, channel_member."
    ),
    "error": "⚠️ {error}\n\nПопробуйте ещё раз или нажмите «Отмена».",
    "not_found": "Рассылка не найдена",
    "not_draft": "Рассылка уже запущена — менять её нельзя",
    "created": "Черновик создан",
    "test": "👁 Тест себе",
    "test_ok": "Отправил вам точную копию ✓",
    "test_blocked": "Не удалось: вы заблокировали бота?",
    "test_failed": "Telegram не принял сообщение: {error}",
    "btn": "🔘 Кнопки",
    "nobtn": "🧹 Убрать кнопки",
    "seg": "👥 Получатели",
    "replace_btn": "✏️ Заменить сообщение",
    "send": "🚀 Отправить · {n}",
    "delete": "🗑 Удалить черновик",
    "deleted": "Черновик удалён",
    "clone": "📋 Копия в черновик",
    "refresh": "🔄 Обновить",
    "pause": "⏸ Пауза",
    "resume": "▶️ Продолжить",
    "stop": "⏹ Остановить",
    "paused": "Пауза ✓",
    "resumed": "Продолжаем ✓",
    "stopped": "Рассылка остановлена",
    "started": "🚀 Рассылка запущена",
    "saved": "Сохранено ✓",
    "seg_title": "👥 <b>Получатели рассылки #{id}</b>",
    "seg_hint": (
        "Выберите сегмент. Всегда исключаются заблокировавшие бота, забаненные и отключившие "
        "«акции и новости»."
    ),
    "seg_dsl": "✍️ Своё условие",
    "seg_nodsl": "🧹 Убрать условие",
    "seg_count": "Получателей: {n}",
    "confirm": (
        "🚀 <b>Отправить рассылку #{id}?</b>\n\nПолучателей: <b>{n}</b> ({seg}).\n"
        "Темп ~{rate} сообщений в секунду — займёт около {eta}.\n\nОтменить уже отправленное нельзя."
    ),
    "confirm_yes": "✅ Да, отправить {n}",
    "no_recipients": "Нет получателей: измените сегмент",
    "premium": (
        "✨ Есть премиум-эмодзи: их увидят, только если у владельца бота есть Telegram Premium "
        "или у бота username с Fragment. Проверьте тестом себе."
    ),
}


class _Stop(Exception):
    def __init__(self, result: HandlerResult) -> None:
        self.result = result


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _eta(n: int, rate: float) -> str:
    seconds = n / rate
    if seconds < 90:
        return "минуту"
    return f"{round(seconds / 60)} мин"


def _segment_title(segment: Any) -> str:
    seg = segment if isinstance(segment, dict) else {}
    preset = PRESETS.get(str(seg.get("preset", "all")))
    title = preset.title if preset is not None else "Все"
    return f"{title} + своё условие" if seg.get("dsl") else title


def _preview(content: Any, limit: int = 300) -> str:
    text = str(content.get("text") or "") if isinstance(content, dict) else ""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


class BroadcastScreens:
    def __init__(self, router: ScreenRouter, service: BroadcastService) -> None:
        self.router = router
        self.service = service
        self.repo = service.repo
        self._installed = False

    # ------------------------------------------------------------ registration

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        r = self.router
        guard: dict[str, Any] = {"required_role": "admin", "perm": PERM}
        for code, fn in (
            (SCREEN_LIST, self._list_screen),
            (SCREEN_CARD, self._card_screen),
            (SCREEN_SEGMENT, self._segment_screen),
            (SCREEN_CONFIRM, self._confirm_screen),
        ):
            r.screen(code, **guard)(self._wrap(fn))
        actions: dict[str, Callable[[ScreenCtx, Any], Awaitable[HandlerResult]]] = {
            "new": self._a_new,
            "repl": self._a_replace,
            "btn": self._a_buttons,
            "nobtn": self._a_no_buttons,
            "dsl": self._a_dsl,
            "nodsl": self._a_no_dsl,
            "seg": self._a_segment,
            "opt": self._a_option,
            "test": self._a_test,
            "go": self._a_go,
            "pause": self._a_pause,
            "resume": self._a_resume,
            "stop": self._a_stop,
            "del": self._a_delete,
            "clone": self._a_clone,
            "cancel": self._a_cancel,
        }
        send_guard: dict[str, Any] = {"required_role": "admin", "perm": SEND_PERM}
        for name, fn in actions.items():
            r.action(ACTIONS, name, **(send_guard if name in _SEND_ACTIONS else guard))(self._wrap(fn))

    @staticmethod
    def _wrap(
        fn: Callable[[ScreenCtx, Any], Awaitable[Any]],
    ) -> Callable[[ScreenCtx, Any], Awaitable[Any]]:
        async def run(ctx: ScreenCtx, arg: Any) -> Any:
            try:
                return await fn(ctx, arg)
            except _Stop as stop:
                return stop.result
            except BroadcastError as e:
                return Toast(str(e), alert=True)

        run.__name__ = getattr(fn, "__name__", "broadcast_handler")
        return run

    def aiogram_router(self, name: str = "svbg-broadcasts") -> Router:
        """``/broadcast`` + the admin's messages while a broadcast input is awaited."""
        router = Router(name=name)

        async def on_command(message: Message) -> None:
            if not await self.handle_command(message):
                raise SkipHandler

        async def on_message(message: Message) -> None:
            if not await self.handle_message(message):
                raise SkipHandler

        router.message.register(on_command, Command("broadcast", "broadcasts"))
        router.message.register(on_message)
        return router

    # ------------------------------------------------------------ helpers

    @staticmethod
    def allowed(user: UserCtx | None) -> bool:
        return user is not None and user.at_least("admin") and user.has_perm(PERM)

    @staticmethod
    def _actor(user: UserCtx) -> Actor:
        return Actor(user.user_id, user.role)

    @staticmethod
    def _id(arg: Any) -> int:
        if isinstance(arg, str):
            head = arg.split(":", 1)[0]
            if _ID_RE.match(head):
                return int(head)
        raise _Stop(Toast(_T["not_found"]))

    async def _get(self, arg: Any, *, screen: bool = True) -> Broadcast:
        bc = await self.repo.get(self._id(arg))
        if bc is None:
            raise _Stop(Redirect(SCREEN_LIST, toast=_T["not_found"]) if screen else Toast(_T["not_found"]))
        return bc

    async def _draft(self, arg: Any) -> Broadcast:
        bc = await self._get(arg, screen=False)
        if bc.status != "draft":
            raise _Stop(Toast(_T["not_draft"], alert=True))
        return bc

    async def _await(self, ctx: ScreenCtx, step: str, bid: int | None) -> None:
        state = {
            "kind": AWAIT_KIND,
            "v": 1,
            "step": step,
            "bid": bid,
            "exp": (clock.now() + AWAIT_TTL).isoformat(),
        }
        await self.router.ui_state.set_awaiting(ctx.user.user_id, state)

    @staticmethod
    def _cancel_row(bid: int | None) -> list[InlineKeyboardButton]:
        return [nav_button(_T["cancel"], ACTIONS, "cancel", str(bid) if bid else None)]

    # ------------------------------------------------------------ screens

    async def _list_screen(self, ctx: ScreenCtx, _arg: Any) -> View:
        items = await self.repo.recent(RECENT)
        lines = [_T["list_title"], "", _T["list_hint"]]
        if not items:
            lines += ["", _T["list_empty"]]
        rows: list[list[InlineKeyboardButton]] = [[nav_button(_T["new"], ACTIONS, "new", style="primary")]]
        for bc in items:
            icon = STATUS_LABELS.get(bc.status, "").split(" ", 1)[0]
            label = str(bc.content.get("text") or describe(bc.content)).replace("\n", " ")
            label = label[:32] + ("…" if len(label) > 32 else "")
            rows.append([nav_button(f"{icon} #{bc.id} · {label}", SCREEN_CARD, arg=str(bc.id))])
        rows.append([nav_button(_T["menu"], HOME)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _card_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._card(await self._get(arg))

    def _card(self, bc: Broadcast, *, note: str | None = None) -> View:
        arg = str(bc.id)
        if bc.status == "draft":
            head = f"📣 <b>Рассылка #{bc.id}</b> · {STATUS_LABELS['draft']}"
        else:
            head = progress_text(bc)
        lines = [head, "", f"Сообщение: {html.escape(describe(bc.content))}"]
        preview = _preview(bc.content)
        if preview:
            lines.append(f"<blockquote>{preview}</blockquote>")
        if has_custom_emoji(bc.content):
            lines.append(_T["premium"])
        lines.append(f"Кнопки: {len(bc.buttons) or 'нет'}")
        lines.append(f"Получатели: <b>{html.escape(_segment_title(bc.segment))}</b> — ≈ {_fmt(bc.total)}")
        opts = [
            f"📌 закрепить: {'да' if bc.pin else 'нет'}",
            f"🔕 без звука: {'да' if bc.silent else 'нет'}",
            f"🗑 удалить: {f'через {bc.delete_after_h} ч' if bc.delete_after_h else 'нет'}",
        ]
        lines.append("Опции: " + " · ".join(opts))
        if note:
            lines += ["", note]
        rows: list[list[InlineKeyboardButton]] = []
        if bc.status == "draft":
            rows.append(
                [nav_button(_T["test"], ACTIONS, "test", arg), nav_button(_T["btn"], ACTIONS, "btn", arg)]
            )
            if bc.buttons:
                rows.append([nav_button(_T["nobtn"], ACTIONS, "nobtn", arg)])
            rows.append([nav_button(_T["seg"], SCREEN_SEGMENT, arg=arg)])
            rows.append(
                [
                    nav_button(f"📌 {'✓' if bc.pin else '✗'}", ACTIONS, "opt", f"{arg}:pin"),
                    nav_button(f"🔕 {'✓' if bc.silent else '✗'}", ACTIONS, "opt", f"{arg}:silent"),
                    nav_button(
                        f"🗑 {f'{bc.delete_after_h} ч' if bc.delete_after_h else '✗'}",
                        ACTIONS,
                        "opt",
                        f"{arg}:delete",
                    ),
                ]
            )
            rows.append([nav_button(_T["replace_btn"], ACTIONS, "repl", arg)])
            rows.append(
                [nav_button(_T["send"].format(n=_fmt(bc.total)), SCREEN_CONFIRM, arg=arg, style="success")]
            )
            rows.append([nav_button(_T["delete"], ACTIONS, "del", arg, style="danger")])
        else:
            if bc.status == "running":
                rows.append(
                    [
                        nav_button(_T["pause"], ACTIONS, "pause", f"{arg}:c"),
                        nav_button(_T["stop"], ACTIONS, "stop", f"{arg}:c", style="danger"),
                    ]
                )
            elif bc.status == "paused":
                rows.append(
                    [
                        nav_button(_T["resume"], ACTIONS, "resume", f"{arg}:c", style="success"),
                        nav_button(_T["stop"], ACTIONS, "stop", f"{arg}:c", style="danger"),
                    ]
                )
            if bc.status in ("running", "paused"):
                rows.append([nav_button(_T["refresh"], SCREEN_CARD, arg=arg)])
            rows.append(
                [nav_button(_T["test"], ACTIONS, "test", arg), nav_button(_T["clone"], ACTIONS, "clone", arg)]
            )
        rows.append([nav_button(_T["to_list"], SCREEN_LIST)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _segment_screen(self, ctx: ScreenCtx, arg: Any) -> View:
        return self._segment_view(await self._get(arg))

    def _segment_view(self, bc: Broadcast) -> View:
        current = str(bc.segment.get("preset", "all"))
        a = str(bc.id)
        lines = [_T["seg_title"].format(id=bc.id), "", _T["seg_hint"]]
        if bc.segment.get("dsl"):
            dsl = json.dumps(bc.segment["dsl"], ensure_ascii=False)
            lines += ["", f"Своё условие: <code>{html.escape(dsl[:500])}</code>"]
        lines += ["", _T["seg_count"].format(n=_fmt(bc.total))]
        rows = [
            [nav_button(f"{'✅ ' if code == current else ''}{p.title}", ACTIONS, "seg", f"{a}:{code}")]
            for code, p in PRESETS.items()
        ]
        rows.append([nav_button(_T["seg_dsl"], ACTIONS, "dsl", a)])
        if bc.segment.get("dsl"):
            rows.append([nav_button(_T["seg_nodsl"], ACTIONS, "nodsl", a)])
        rows.append([nav_button(_T["to_card"], SCREEN_CARD, arg=a)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def _confirm_screen(self, ctx: ScreenCtx, arg: Any) -> View | Redirect:
        bc = await self._get(arg)
        if bc.status != "draft":
            return Redirect(SCREEN_CARD, str(bc.id), toast=_T["not_draft"])
        n = await self.repo.count(bc.segment)
        if n == 0:
            return Redirect(SCREEN_CARD, str(bc.id), toast=_T["no_recipients"])
        rate = 12.5 if bc.pin else 25.0
        text = _T["confirm"].format(
            id=bc.id,
            n=_fmt(n),
            seg=html.escape(_segment_title(bc.segment)),
            rate=int(rate),
            eta=_eta(n, rate),
        )
        rows = [
            [nav_button(_T["confirm_yes"].format(n=_fmt(n)), ACTIONS, "go", str(bc.id), style="success")],
            [nav_button(_T["to_card"], SCREEN_CARD, arg=str(bc.id))],
        ]
        return View(text=text, parse_mode="HTML", keyboard=rows)

    # ------------------------------------------------------------ actions: input

    async def _a_new(self, ctx: ScreenCtx, _arg: Any) -> View:
        await self._await(ctx, "compose", None)
        return View(text=_T["compose"], parse_mode="HTML", keyboard=[self._cancel_row(None)])

    async def _a_replace(self, ctx: ScreenCtx, arg: Any) -> View:
        bc = await self._draft(arg)
        await self._await(ctx, "compose", bc.id)
        return View(
            text=_T["replace"].format(id=bc.id), parse_mode="HTML", keyboard=[self._cancel_row(bc.id)]
        )

    async def _a_buttons(self, ctx: ScreenCtx, arg: Any) -> View:
        bc = await self._draft(arg)
        await self._await(ctx, "buttons", bc.id)
        return View(
            text=_T["buttons"].format(id=bc.id), parse_mode="HTML", keyboard=[self._cancel_row(bc.id)]
        )

    async def _a_dsl(self, ctx: ScreenCtx, arg: Any) -> View:
        bc = await self._draft(arg)
        await self._await(ctx, "dsl", bc.id)
        return View(text=_T["dsl"].format(id=bc.id), parse_mode="HTML", keyboard=[self._cancel_row(bc.id)])

    async def _a_cancel(self, ctx: ScreenCtx, arg: Any) -> Redirect:
        await self.router.ui_state.set_awaiting(ctx.user.user_id, None)
        if isinstance(arg, str) and _ID_RE.match(arg):
            return Redirect(SCREEN_CARD, arg)
        return Redirect(SCREEN_LIST)

    # ------------------------------------------------------------ actions: draft

    async def _a_no_buttons(self, ctx: ScreenCtx, arg: Any) -> View:
        bc = await self.service.set_buttons(self._id(arg), [])
        if bc is None:
            raise _Stop(Toast(_T["not_draft"], alert=True))
        return self._with_toast(self._card(bc), _T["saved"])

    async def _a_segment(self, ctx: ScreenCtx, arg: Any) -> View:
        parts = arg.split(":", 1) if isinstance(arg, str) else []
        if len(parts) != 2 or parts[1] not in PRESETS:
            raise _Stop(Toast(_T["not_found"]))
        bc = await self._draft(parts[0])
        segment: dict[str, Any] = {"preset": parts[1]}
        if bc.segment.get("dsl"):
            segment["dsl"] = bc.segment["dsl"]
        bc = await self.service.set_segment(bc.id, segment)
        return self._with_toast(self._card(bc), _T["seg_count"].format(n=_fmt(bc.total)))

    async def _a_no_dsl(self, ctx: ScreenCtx, arg: Any) -> View:
        bc = await self._draft(arg)
        bc = await self.service.set_segment(bc.id, {"preset": bc.segment.get("preset", "all")})
        return self._with_toast(self._segment_view(bc), _T["seg_count"].format(n=_fmt(bc.total)))

    async def _a_option(self, ctx: ScreenCtx, arg: Any) -> View:
        parts = arg.split(":", 1) if isinstance(arg, str) else []
        if len(parts) != 2 or parts[1] not in ("pin", "silent", "delete"):
            raise _Stop(Toast(_T["not_found"]))
        bc = await self._draft(parts[0])
        changed = await self.service.toggle(bc, parts[1])
        if changed is None:
            raise _Stop(Toast(_T["not_draft"], alert=True))
        return self._with_toast(self._card(changed), _T["saved"])

    @staticmethod
    def _with_toast(view: View, toast: str) -> View:
        view.toast = toast
        return view

    async def _a_test(self, ctx: ScreenCtx, arg: Any) -> Toast:
        bc = await self._get(arg, screen=False)
        result = await self.service.test_send(bc, ctx.chat_id, ctx.lang)
        if result.outcome is Outcome.SENT:
            return Toast(_T["test_ok"])
        if result.outcome is Outcome.BLOCKED:
            return Toast(_T["test_blocked"], alert=True)
        return Toast(_T["test_failed"].format(error=result.error or "ошибка"), alert=True)

    async def _a_delete(self, ctx: ScreenCtx, arg: Any) -> Redirect:
        bc = await self._draft(arg)
        if not await self.repo.delete_draft(bc.id):
            raise _Stop(Toast(_T["not_draft"], alert=True))
        return Redirect(SCREEN_LIST, toast=_T["deleted"])

    async def _a_clone(self, ctx: ScreenCtx, arg: Any) -> View:
        new_id = await self.repo.clone(self._id(arg), ctx.user.user_id)
        if new_id is None:
            raise _Stop(Toast(_T["not_found"]))
        bc = await self.service.repo.get(new_id)
        if bc is None:  # pragma: no cover - deleted between two statements
            raise _Stop(Toast(_T["not_found"]))
        bc = await self.service.set_segment(new_id, bc.segment)  # fresh recipient count
        return self._with_toast(self._card(bc), _T["created"])

    # ------------------------------------------------------------ actions: run

    async def _a_go(self, ctx: ScreenCtx, arg: Any) -> Redirect:
        bid = self._id(arg)
        await self.service.start(bid, self._actor(ctx.user), ctx.chat_id)
        return Redirect(SCREEN_CARD, str(bid), toast=_T["started"])

    async def _control(
        self,
        ctx: ScreenCtx,
        arg: Any,
        op: Callable[[int, Actor], Awaitable[Broadcast]],
        toast: str,
    ) -> HandlerResult:
        bid = self._id(arg)
        bc = await op(bid, self._actor(ctx.user))
        if isinstance(arg, str) and arg.endswith(":c"):  # pressed on the card: show the new state
            view = self._card(bc)
            view.toast = toast
            return view
        return Toast(toast)  # pressed on the progress message: it is refreshed by the service/sender

    async def _a_pause(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._control(ctx, arg, self.service.pause, _T["paused"])

    async def _a_resume(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._control(ctx, arg, self.service.resume, _T["resumed"])

    async def _a_stop(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await self._control(ctx, arg, self.service.stop, _T["stopped"])

    # ------------------------------------------------------------ messages

    async def handle_command(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        user = await self._load(message)
        if not self.allowed(user):
            return False
        assert user is not None
        await self.router.ui_state.set_awaiting(user.user_id, None)
        await self.router.show(user, message.chat.id, SCREEN_LIST, new=True)
        return True

    async def _load(self, message: Message) -> UserCtx | None:
        assert message.from_user is not None
        try:
            return await self.router.user_loader(message.from_user)
        except Exception:
            log.exception("user loader failed for a broadcast message")
            return None

    async def handle_message(self, message: Message) -> bool:
        """The admin's input for an awaited broadcast step. ``False`` = not ours (other handlers go on)."""
        if message.from_user is None or message.chat.type != "private" or message.from_user.is_bot:
            return False
        user = await self._load(message)
        if not self.allowed(user):
            return False
        assert user is not None
        state = (await self.router.ui_state.get(user.user_id)).awaiting
        if not isinstance(state, dict) or state.get("kind") != AWAIT_KIND:
            return False
        step, bid = state.get("step"), state.get("bid")
        exp = state.get("exp")
        try:
            expired = not isinstance(exp, str) or clock.now() >= _parse_ts(exp)
        except ValueError:
            expired = True
        if expired or step not in ("compose", "buttons", "dsl") or not (bid is None or isinstance(bid, int)):
            await self.router.ui_state.set_awaiting(user.user_id, None)
            return False
        text = message.text
        if text is not None and text.startswith("/"):
            await self.router.ui_state.set_awaiting(user.user_id, None)
            if text.split(maxsplit=1)[0].split("@", 1)[0].lower() == "/cancel":
                target = (SCREEN_CARD, str(bid)) if bid else (SCREEN_LIST, None)
                await self.router.show(user, message.chat.id, target[0], target[1], new=True)
                return True
            return False
        try:
            bid = await self._apply(user, message, str(step), bid)
        except (ComposeError, BroadcastError) as e:
            await self._reply_error(message.chat.id, str(e), bid)
            return True
        if bid is None:
            return True
        await self.router.ui_state.set_awaiting(user.user_id, None)
        await self.router.show(user, message.chat.id, SCREEN_CARD, str(bid), new=True)
        return True

    async def _apply(self, user: UserCtx, message: Message, step: str, bid: int | None) -> int | None:
        if step == "compose":
            content = normalize(message)
            if bid is None:
                return await self.service.create(
                    self._actor(user), chat_id=message.chat.id, message_id=message.message_id, content=content
                )
            if not await self.service.replace_message(
                bid, chat_id=message.chat.id, message_id=message.message_id, content=content
            ):
                raise BroadcastError(_T["not_draft"])
            return bid
        if bid is None:
            return None
        if message.text is None:
            raise ComposeError("Нужен текст.")
        if step == "buttons":
            entities = [e.model_dump(mode="json", exclude_none=True) for e in message.entities or ()]
            buttons = parse_buttons(message.text, entities)
            if not await self.service.set_buttons(bid, buttons):
                raise BroadcastError(_T["not_draft"])
            return bid
        if len(message.text) > _DSL_MAX:
            raise ComposeError("Слишком длинное условие.")
        try:
            dsl = json.loads(message.text)
        except ValueError:
            raise ComposeError('Это не JSON. Пример: {"sub": "active"}') from None
        if not isinstance(dsl, dict):
            raise ComposeError("Условие должно быть JSON-объектом {…}.")
        bc = await self.repo.get(bid)
        if bc is None or bc.status != "draft":
            raise BroadcastError(_T["not_draft"])
        await self.service.set_segment(bid, {"preset": bc.segment.get("preset", "all"), "dsl": dsl})
        return bid

    async def _reply_error(self, chat_id: int, error: str, bid: int | None) -> None:
        method = SendMessage(
            chat_id=chat_id,
            text=_T["error"].format(error=html.escape(error)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[self._cancel_row(bid)]),
        )
        try:
            await self.router.transport.call(method, chat_id=chat_id)
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.warning("cannot reply to the broadcast input: %s", type(e).__name__)


def _parse_ts(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        raise ValueError("naive timestamp")
    return value


# ------------------------------------------------------------------------------------------- entry point


class _Deps(Protocol):
    @property
    def db(self) -> Any: ...

    @property
    def notifier(self) -> Any: ...


def build(
    router: ScreenRouter,
    db: Any,
    notifier: Any,
    *,
    config: Callable[[], AudienceConfig] = AudienceConfig,
    **sender_kw: Any,
) -> tuple[BroadcastScreens, BroadcastSender]:
    """Wire repo → sender → service → screens (also used by tests)."""
    repo = BroadcastRepo(db, Audience(config))
    sender = BroadcastSender(
        db, notifier, repo=repo, bot_username=lambda: router.transport.bot_username, **sender_kw
    )
    screens = BroadcastScreens(router, BroadcastService(repo, sender))
    screens.install()
    return screens, sender


def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``).

    Job handlers (``broadcast.run``, ``broadcast.cleanup``) are registered through ``deps.register_job``
    when the app provides it; otherwise they are only logged as missing (integration step).
    """
    settings = getattr(deps, "settings", None)

    def config() -> AudienceConfig:
        try:
            current = settings.current() if settings is not None else {}
            lang = current["DEFAULT_LANGUAGE"] or "ru"
            channel = current["REQUIRED_CHANNEL_ID"]
        except (KeyError, AttributeError, RuntimeError, TypeError):
            return AudienceConfig()
        cid = channel if isinstance(channel, int) and not isinstance(channel, bool) and channel != 0 else None
        return AudienceConfig(default_lang=str(lang), channel_id=cid)

    screens, sender = build(router, deps.db, deps.notifier, config=config)
    register = getattr(deps, "register_job", None)
    if callable(register):
        for kind, handler in sender.handlers().items():
            register(kind, handler)
    else:
        log.error("broadcast job handlers are not registered: AppDeps.register_job is missing")
    return screens.aiogram_router()
