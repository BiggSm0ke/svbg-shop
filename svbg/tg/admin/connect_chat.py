"""«🔔 Админ-чат»: connecting the admin supergroup, topic switches (07 §2.4.2) and the module entry point.

Connecting (owner only — ``ADMIN_CHAT_ID`` is an owner setting):

* «👥 Выбрать группу» sends a reply keyboard with a ``request_chat`` button (``chat_is_forum=true``,
  ``chat_has_username=false`` — private groups only, ``bot_administrator_rights`` with ``can_manage_topics``,
  ``can_pin_messages``, ``can_delete_messages``): Telegram lets the owner pick a forum group and adds the bot
  as an admin with these rights; the shared chat arrives as a ``chat_shared`` message;
* «🔢 Ввести ID» — a form for the group id (``-100…``).

Either way the chat is probed (private forum supergroup, bot admin with «управление темами»; a human error
and «🔄 Проверить снова» otherwise), stored through the settings pipeline (``ADMIN_CHAT_ID``, audited,
mirrored to ``.env``) and the bot creates the topics itself. The screen then lists the topics with switches
(✅/⬜) and, for a disabled topic, where its messages go (↪️ «Система» / 🚫 не отправлять).

:func:`setup` is the ``setup(router, deps)`` entry point for ``svbg.app``: it builds the
:class:`~svbg.services.admin_chat.AdminChatService` (unless the app passes one as ``deps.admin_chat``),
registers it as the ``admin_chat`` component, makes the error hub report into «🚨 Ошибки» (owner DMs stay the
fallback without a chat), installs the report buttons and the «Требует внимания» relay.
"""

from __future__ import annotations

import html
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.methods import SendMessage
from aiogram.types import (
    ChatAdministratorRights,
    InlineKeyboardButton,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from svbg.content import defaults
from svbg.core.component import ProbeError
from svbg.core.settings.service import RESET, Change, SettingsError
from svbg.services.admin_chat import SCREEN, AdminChatService, EnsureReport
from svbg.tg.admin_chat_sink import AdminChatSink, AttentionRelay, ErrorActions
from svbg.tg.ui import texts as ui_texts
from svbg.tg.ui.forms import Field, Form, integer
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Toast, View

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from svbg.core.settings.service import ApplyResult, ApplySource
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter

__all__ = [
    "A_CHECK",
    "A_FALLBACK",
    "A_ID",
    "A_OFF",
    "A_OFF_YES",
    "A_PICK",
    "A_TOGGLE",
    "A_TOPICS",
    "BOT_RIGHTS",
    "CANCEL_TEXT",
    "FORM_ID",
    "REQUEST_ID",
    "SCREEN",
    "ConnectChat",
    "ConnectOutcome",
    "setup",
]

log = logging.getLogger("svbg.tg.admin")

FORM_ID: Final = "achat.id"
REQUEST_ID: Final = 720_201  # request_chat id of «Выбрать группу» (any int32, unique per bot)
A_PICK: Final = "pick"
A_ID: Final = "id"
A_CHECK: Final = "chk"
A_TOPICS: Final = "mk"
A_TOGGLE: Final = "tog"
A_FALLBACK: Final = "fb"
A_OFF: Final = "off"
A_OFF_YES: Final = "offok"
CANCEL_TEXT: Final = "✖️ Не подключать"
_CHAT_ARG_RE: Final = re.compile(r"^-\d{1,19}$")
_OUTCOMES_MAX: Final = 256

BOT_RIGHTS: Final = ChatAdministratorRights(
    is_anonymous=False,
    can_manage_chat=False,
    can_delete_messages=True,
    can_manage_video_chats=False,
    can_restrict_members=False,
    can_promote_members=False,
    can_change_info=False,
    can_invite_users=False,
    can_post_stories=False,
    can_edit_stories=False,
    can_delete_stories=False,
    can_send_welcome_messages=False,
    can_pin_messages=True,
    can_manage_topics=True,
)

_T: Final[dict[str, str]] = {
    "title": "🔔 <b>Админ-чат</b>",
    "off_intro": (
        "Сейчас уведомления приходят вам в личку.\n\n"
        "Подключите супергруппу с темами — бот сам создаст темы «Оплаты», «Ошибки», «Система» и другие "
        "и будет писать каждое уведомление в свою тему."
    ),
    "off_howto": (
        "<b>Как подготовить группу</b>\n"
        "1. Создайте группу и включите в её настройках «Темы».\n"
        "2. Нажмите «Выбрать группу» — Telegram предложит добавить бота администратором с нужными правами. "
        "Или добавьте бота сами (права: управление темами, закрепление и удаление сообщений) "
        "и пришлите ID группы."
    ),
    "on_chat": "Группа: <code>{chat_id}</code>",
    "on_ok": "Состояние: ✅ работает",
    "on_down": "Состояние: ⚠️ недоступна ({reason}) — уведомления идут вам в личку",
    "on_topics": (
        "Темы — нажмите, чтобы включить или выключить. Сообщения выключенной темы: ↪️ в «Систему» "
        "или 🚫 не отправляются."
    ),
    "topic_on": "✅ {label}",
    "topic_off": "⬜ {label}",
    "topic_missing": " ·  нет темы",
    "fb_system": "↪️ в Систему",
    "fb_drop": "🚫 не слать",
    "pick": "👥 Выбрать группу",
    "enter_id": "🔢 Ввести ID",
    "make_topics": "🧱 Создать темы",
    "recheck": "🔄 Проверить снова",
    "other": "👥 Другая группа",
    "off": "🔌 Отключить",
    "back": "⬅️ Назад",
    "off_confirm": (
        "Отключить админ-чат? Уведомления снова будут приходить вам в личку. Темы в группе останутся — "
        "при повторном подключении бот использует их же."
    ),
    "off_yes": "🔌 Да, отключить",
    "cancel": "Отмена",
    "pick_prompt": (
        "Нажмите «👥 Выбрать группу» внизу и выберите группу с темами. Если бота там ещё нет, Telegram "
        "предложит добавить его администратором с нужными правами."
    ),
    "pick_group": "Откройте этот раздел в личном чате с ботом",
    "checking": "Проверяю группу…",
    "cancelled": "Подключение отменено",
    "id_prompt": "Пришлите ID супергруппы — отрицательное число, обычно начинается с -100.",
    "connected": "✅ Группа «{title}» подключена. Темы готовы: {ready} из {total}.",
    "topics_ready": "✅ Темы готовы: {ready} из {total}.",
    "topics_failed": "⚠️ Не созданы темы: {topics} ({reason})",
    "warn_rights": "⚠️ У бота нет прав: {rights}. Карточки не будут закрепляться и удаляться.",
    "failed": "❌ Не удалось подключить группу: {error}",
    "check_failed": "❌ Проверка не прошла: {error}",
    "not_connected": "Админ-чат не подключён",
    "disabled": "🔌 Админ-чат отключён — уведомления снова приходят вам в личку.",
    "off_failed": "❌ Не удалось отключить: {error}",
    "unknown_topic": "Такой темы нет",
}


class _Settings(Protocol):
    async def apply(
        self,
        changes: Sequence[Change],
        *,
        source: ApplySource,
        actor_id: int | None,
        expected_version: int | None = None,
    ) -> ApplyResult: ...


UserLoader = Callable[["TgUser"], Awaitable["UserCtx | None"]]


@dataclass(slots=True)
class ConnectOutcome:
    ok: bool
    chat_id: int | None = None
    title: str | None = None
    error: str | None = None
    report: EnsureReport | None = None
    warnings: tuple[str, ...] = ()
    message: str | None = None  # a ready line instead of the standard ones (disconnect)
    retry_arg: str | None = None  # «Проверить снова» connects this chat (None: re-check the current)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


class ConnectChat:
    """Screens, actions, the ID form and the ``chat_shared`` handler."""

    def __init__(
        self,
        router: ScreenRouter,
        service: AdminChatService,
        *,
        settings: _Settings | None,
        users: UserLoader | None,
    ) -> None:
        self._router = router
        self._service = service
        self._settings = settings
        self._users = users
        self._outcomes: OrderedDict[int, ConnectOutcome] = OrderedDict()

    # ------------------------------------------------------------------ registration

    def install(self) -> None:
        r = self._router
        r.screen(SCREEN, required_role="owner")(self._screen)
        r.action(SCREEN, A_PICK, required_role="owner")(self._pick)
        r.action(SCREEN, A_ID, required_role="owner")(self._enter_id)
        r.action(SCREEN, A_CHECK, required_role="owner")(self._check)
        r.action(SCREEN, A_TOPICS, required_role="owner")(self._make_topics)
        r.action(SCREEN, A_TOGGLE, required_role="owner")(self._toggle)
        r.action(SCREEN, A_FALLBACK, required_role="owner")(self._fallback)
        r.action(SCREEN, A_OFF, required_role="owner")(self._off)
        r.action(SCREEN, A_OFF_YES, required_role="owner")(self._off_yes)
        r.form(
            Form(
                name=FORM_ID,
                fields=(Field("chat_id", _T["id_prompt"], integer(max_value=-1)),),
                on_done=self._form_done,
                required_role="owner",
            )
        )

    def aiogram_router(self) -> Router:
        router = Router(name="svbg-admin-chat")
        router.message.register(self.on_chat_shared, F.chat_shared)
        router.message.register(self.on_cancel, F.text == CANCEL_TEXT)
        return router

    # ------------------------------------------------------------------ connect logic

    async def connect(self, chat_id: int, actor_id: int | None) -> ConnectOutcome:
        """Probe, store ``ADMIN_CHAT_ID`` through the settings pipeline, create the topics."""
        try:
            check = await self._service.check_chat(chat_id)
        except ProbeError as exc:
            return ConnectOutcome(False, chat_id, error=str(exc), retry_arg=str(chat_id))
        if self._settings is not None:
            try:
                result = await self._settings.apply(
                    [Change("ADMIN_CHAT_ID", str(chat_id))], source="bot", actor_id=actor_id
                )
            except SettingsError as exc:
                return ConnectOutcome(False, chat_id, error=str(exc), retry_arg=str(chat_id))
            if not result.ok:
                return ConnectOutcome(
                    False, chat_id, error="; ".join(result.rejected.values()), retry_arg=str(chat_id)
                )
        else:
            await self._service.reconfigure({"ADMIN_CHAT_ID": chat_id})
        self._service.set_chat(chat_id)  # unchanged value: the pipeline did not reconfigure
        log.info("admin chat %s connected by user %s", chat_id, actor_id)
        report = await self._service.ensure_topics()
        return ConnectOutcome(True, chat_id, check.title, report=report, warnings=check.warnings)

    async def _recheck_current(self) -> ConnectOutcome:
        chat_id = self._service.chat_id
        if chat_id is None:
            return ConnectOutcome(False, error=_T["not_connected"])
        try:
            check = await self._service.check_chat(chat_id)
        except ProbeError as exc:
            return ConnectOutcome(False, chat_id, error=str(exc))
        report = await self._service.ensure_topics()
        return ConnectOutcome(True, chat_id, check.title, report=report, warnings=check.warnings)

    def _remember(self, user_id: int, outcome: ConnectOutcome) -> None:
        self._outcomes[user_id] = outcome
        self._outcomes.move_to_end(user_id)
        while len(self._outcomes) > _OUTCOMES_MAX:
            self._outcomes.popitem(last=False)

    # ------------------------------------------------------------------ screen

    def _outcome_lines(self, outcome: ConnectOutcome) -> list[str]:
        if outcome.message is not None:
            return [outcome.message]
        if not outcome.ok:
            key = "check_failed" if outcome.retry_arg is None else "failed"
            return [_T[key].format(error=_esc(outcome.error or "?"))]
        lines: list[str] = []
        report = outcome.report or EnsureReport()
        total = sum(1 for d in self._service.topic_defs() if self._service.state(d.kind).enabled)
        ready = len(report.created) + len(report.existing)
        if outcome.title is not None and outcome.retry_arg is None:
            lines.append(_T["connected"].format(title=_esc(outcome.title), ready=ready, total=total))
        else:
            lines.append(_T["topics_ready"].format(ready=ready, total=total))
        if report.failed:
            titles = [self._topic_title(kind) for kind in report.failed]
            reason = next(iter(report.failed.values()))
            lines.append(_T["topics_failed"].format(topics=_esc(", ".join(titles)), reason=_esc(reason)))
        if outcome.warnings:
            lines.append(_T["warn_rights"].format(rights=_esc(", ".join(outcome.warnings))))
        return lines

    def _topic_title(self, kind: str) -> str:
        defn = self._service.topic(kind)
        return defn.title if defn is not None else kind

    async def _screen(self, ctx: ScreenCtx, arg: Any) -> View:
        outcome = self._outcomes.pop(ctx.user.user_id, None)
        lines = [_T["title"]]
        keyboard: list[list[InlineKeyboardButton]] = []
        if outcome is not None:
            lines += ["", *self._outcome_lines(outcome)]
            if not outcome.ok and outcome.chat_id is not None:
                keyboard.append([nav_button(_T["recheck"], SCREEN, A_CHECK, outcome.retry_arg)])
        svc = self._service
        chat_id = svc.chat_id
        if chat_id is None:
            lines += ["", _T["off_intro"], "", _T["off_howto"]]
            keyboard.append(
                [nav_button(_T["pick"], SCREEN, A_PICK), nav_button(_T["enter_id"], SCREEN, A_ID)]
            )
        else:
            lines += ["", _T["on_chat"].format(chat_id=chat_id)]
            if svc.down:
                lines.append(_T["on_down"].format(reason=_esc(svc.last_error or "?")))
            else:
                lines.append(_T["on_ok"])
            lines += ["", _T["on_topics"]]
            for defn in svc.topic_defs():
                st = svc.state(defn.kind)
                label = _T["topic_on" if st.enabled else "topic_off"].format(label=defn.label)
                if st.enabled and st.thread_in(chat_id) is None:
                    label += _T["topic_missing"]
                row = [nav_button(label, SCREEN, A_TOGGLE, defn.kind)]
                if not st.enabled:
                    fb = _T["fb_drop"] if st.fallback == "drop" else _T["fb_system"]
                    row.append(nav_button(fb, SCREEN, A_FALLBACK, defn.kind))
                keyboard.append(row)
            keyboard.append(
                [nav_button(_T["make_topics"], SCREEN, A_TOPICS), nav_button(_T["recheck"], SCREEN, A_CHECK)]
            )
            keyboard.append([nav_button(_T["other"], SCREEN, A_PICK), nav_button(_T["off"], SCREEN, A_OFF)])
        keyboard.append([nav_button(_T["back"], defaults.SETTINGS_ROOT)])
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=keyboard)

    # ------------------------------------------------------------------ actions

    async def _pick(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if ctx.chat_id <= 0:
            return Toast(_T["pick_group"], alert=True)
        markup = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text=_T["pick"],
                        request_chat=KeyboardButtonRequestChat(
                            request_id=REQUEST_ID,
                            chat_is_channel=False,
                            chat_is_forum=True,
                            chat_has_username=False,  # only private groups: a public one is readable by all
                            bot_administrator_rights=BOT_RIGHTS,
                            request_title=True,
                        ),
                    )
                ],
                [KeyboardButton(text=CANCEL_TEXT)],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await self._router.transport.call(
            SendMessage(chat_id=ctx.chat_id, text=_T["pick_prompt"], reply_markup=markup), chat_id=ctx.chat_id
        )
        return None

    async def _enter_id(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        return await ctx.start_form(FORM_ID)

    async def _form_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        chat_id = data.get("chat_id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return await self._screen(ctx, None)
        self._remember(ctx.user.user_id, await self.connect(chat_id, ctx.user.user_id))
        return await self._screen(ctx, None)

    async def _check(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if isinstance(arg, str) and _CHAT_ARG_RE.match(arg) and int(arg) != self._service.chat_id:
            outcome = await self.connect(int(arg), ctx.user.user_id)
        else:
            outcome = await self._recheck_current()
        self._remember(ctx.user.user_id, outcome)
        return await self._screen(ctx, None)

    async def _make_topics(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if self._service.chat_id is None:
            return Toast(_T["not_connected"])
        report = await self._service.ensure_topics()
        self._remember(ctx.user.user_id, ConnectOutcome(True, self._service.chat_id, report=report))
        return await self._screen(ctx, None)

    async def _toggle(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        kind = arg if isinstance(arg, str) and self._service.topic(arg) is not None else None
        if kind is None:
            return Toast(_T["unknown_topic"])
        st = self._service.state(kind)
        await self._service.set_enabled(kind, not st.enabled)
        return await self._screen(ctx, None)

    async def _fallback(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        kind = arg if isinstance(arg, str) and self._service.topic(arg) is not None else None
        if kind is None:
            return Toast(_T["unknown_topic"])
        st = self._service.state(kind)
        await self._service.set_fallback(kind, "system" if st.fallback == "drop" else "drop")
        return await self._screen(ctx, None)

    async def _off(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if self._service.chat_id is None:
            return Toast(_T["not_connected"])
        return View(
            text=_T["off_confirm"],
            keyboard=[[nav_button(_T["off_yes"], SCREEN, A_OFF_YES), nav_button(_T["cancel"], SCREEN)]],
        )

    async def _off_yes(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        if self._settings is not None:
            try:
                result = await self._settings.apply(
                    [Change("ADMIN_CHAT_ID", RESET)], source="bot", actor_id=ctx.user.user_id
                )
                error = None if result.ok else "; ".join(result.rejected.values())
            except SettingsError as exc:
                error = str(exc)
            if error is not None:
                self._remember(
                    ctx.user.user_id,
                    ConnectOutcome(False, message=_T["off_failed"].format(error=_esc(error))),
                )
                return await self._screen(ctx, None)
        await self._service.reconfigure({"ADMIN_CHAT_ID": None})
        log.info("admin chat disconnected by user %s", ctx.user.user_id)
        self._remember(ctx.user.user_id, ConnectOutcome(True, message=_T["disabled"]))
        return await self._screen(ctx, None)

    # ------------------------------------------------------------------ messages

    async def on_chat_shared(self, message: Message) -> None:
        """``chat_shared`` from «Выбрать группу» (private chat, owner only)."""
        shared = message.chat_shared
        tg_user = message.from_user
        if (
            shared is None
            or shared.request_id != REQUEST_ID
            or message.chat.type != "private"
            or tg_user is None
        ):
            raise SkipHandler
        user = await self._users(tg_user) if self._users is not None else None
        remove = ReplyKeyboardRemove(remove_keyboard=True)
        if user is None or not user.at_least("owner"):
            lang = user.lang if user is not None else tg_user.language_code
            await self._say(message.chat.id, ui_texts.t(lang, "denied"), remove)
            log.info("chat_shared from a non-owner (user %s) ignored", user.user_id if user else "?")
            return
        await self._say(message.chat.id, _T["checking"], remove)
        self._remember(user.user_id, await self.connect(shared.chat_id, user.user_id))
        await self._router.show(user, message.chat.id, SCREEN, new=True)

    async def on_cancel(self, message: Message) -> None:
        if message.chat.type != "private":
            raise SkipHandler
        await self._say(message.chat.id, _T["cancelled"], ReplyKeyboardRemove(remove_keyboard=True))

    async def _say(self, chat_id: int, text: str, markup: ReplyKeyboardRemove | None = None) -> None:
        await self._router.transport.call(
            SendMessage(chat_id=chat_id, text=text, reply_markup=markup), chat_id=chat_id
        )


# ---------------------------------------------------------------------------------------------- setup


class _Deps(Protocol):
    @property
    def db(self) -> Any: ...
    @property
    def notifier(self) -> Any: ...
    @property
    def holder(self) -> Any: ...
    @property
    def settings(self) -> Any: ...
    @property
    def components(self) -> Any: ...
    @property
    def hub(self) -> Any: ...
    @property
    def users(self) -> Any: ...
    @property
    def attention(self) -> Any: ...
    @property
    def bus(self) -> Any: ...

    async def owner_ids(self) -> frozenset[int]: ...


def _admin_chat_setting(settings: Any) -> int | None:
    try:
        value = settings.current()["ADMIN_CHAT_ID"]
    except (KeyError, RuntimeError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _owner_dm_sink(deps: _Deps) -> Any:
    """The stage-0 owner-DM sink: reports go there while no admin chat is connected."""
    try:
        from svbg.app import OwnerDmSink
    except ImportError:  # pragma: no cover - the app module is always present in the package
        return None
    return OwnerDmSink(deps.notifier, deps.owner_ids)


async def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app`` (``setup(router, deps)``)."""
    service: AdminChatService | None = getattr(deps, "admin_chat", None)
    on_stop: Callable[[str, Callable[[], Awaitable[Any]]], None] | None = getattr(deps, "on_stop", None)
    if service is None:
        service = AdminChatService(deps.db, deps.notifier, deps.holder, owners=deps.owner_ids)
        if service.name not in deps.components:
            deps.components.register(service)
        if callable(on_stop):
            on_stop("admin chat", service.stop)
    await service.start()
    service.set_chat(_admin_chat_setting(deps.settings))

    deps.hub.set_sink(AdminChatSink(service, fallback=_owner_dm_sink(deps)))
    actions = ErrorActions(router, deps.hub, service)
    actions.install()
    if callable(on_stop):
        on_stop("admin chat buttons", actions.drain)
    AttentionRelay(service, getattr(deps, "attention", None)).install(deps.bus)

    screens = ConnectChat(router, service, settings=deps.settings, users=getattr(deps.users, "load", None))
    screens.install()
    return screens.aiogram_router()
