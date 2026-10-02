"""One-time owner link: ``https://t.me/<bot>?start=setup_<code>`` (03 §7.1, 04 §9.1).

* The host CLI (``svbg owner-link``) calls :func:`create_owner_link`: a random code (192 bits) is generated,
  only its SHA-256 is stored in ``config_meta['owner_link']`` with a 1 hour expiry; a new link replaces the
  previous one. The code itself is printed once and never stored or logged.
* The bot handles ``/start setup_<code>`` in a private chat (:class:`OwnerSetup`): in **one transaction** the
  row is locked, the hash compared in constant time, the expiry checked and the row deleted (single use),
  the user is created/updated with ``role='owner'`` and an ``admin_audit`` record ``owner.claim`` is written.
  Then the Telegram id is added to ``OWNER_IDS`` (best effort, through the settings pipeline), the user cache
  is invalidated and the new owner gets a welcome screen.

A wrong code does not consume the link; attempts are rate-limited per Telegram user.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import re
import secrets
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa
from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.methods import SendMessage
from aiogram.types import Message
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from svbg.core import clock
from svbg.core.settings.service import Change
from svbg.core.tables import admin_audit, config_meta, users
from svbg.db.meta import JSONB
from svbg.tg.ui.context import UserCtx
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import View

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from svbg.core.settings.service import SettingsService
    from svbg.tg.ui.router import ScreenCtx, ScreenRouter

__all__ = [
    "DEEP_LINK_PREFIX",
    "LINK_TTL",
    "META_KEY",
    "SCREEN_WELCOME",
    "OwnerClaim",
    "OwnerLink",
    "OwnerLinkError",
    "OwnerSetup",
    "code_from_payload",
    "create_owner_link",
    "owner_link_url",
    "redeem_owner_code",
    "setup",
]

log = logging.getLogger("svbg.tg.setup")

META_KEY: Final = "owner_link"
DEEP_LINK_PREFIX: Final = "setup_"
LINK_TTL: Final = timedelta(hours=1)
SCREEN_WELCOME: Final = "setup.welcome"
CODE_BYTES: Final = 24  # 32 url-safe characters; "setup_" + code fits the 64-char start parameter
_CODE_RE: Final = re.compile(r"[A-Za-z0-9_-]{16,58}")
_USERNAME_RE: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
MAX_ATTEMPTS: Final = 5
MAX_TRACKED_USERS: Final = 10_000  # rate-limiter entries kept in memory (LRU)
ATTEMPT_WINDOW_S: Final = 600.0

_T: Final[dict[str, str]] = {
    "invalid": (
        "Ссылка владельца недействительна: она устарела, уже использована или заменена новой.\n\n"
        "Создайте новую на сервере командой: svbg owner-link"
    ),
    "busy": "Слишком много попыток. Подождите несколько минут.",
    "error": "Не получилось: база данных не отвечает. Попробуйте ещё раз через минуту.",
    "welcome": (
        "👑 <b>Готово! Теперь вы владелец бота.</b>\n\n"
        "Откройте ⚙️ Настройки: подключите Remnawave, платёжки и админ-чат. "
        "Все настройки сохраняются в базе и в файле .env и применяются без перезапуска.\n\n"
        "Ссылка была одноразовой и больше не работает."
    ),
    "settings": "⚙️ Настройки",
    "menu": "🏠 Меню",
}

Reason = Literal["invalid", "expired"]


class OwnerLinkError(Exception):
    """The code is wrong, expired, already used or replaced."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


class _Database(Protocol):
    def tx(self) -> AbstractAsyncContextManager[Any]: ...


@dataclass(frozen=True, slots=True)
class OwnerLink:
    code: str  # secret: show once, never log
    payload: str  # "setup_<code>" — the /start parameter
    url: str | None  # https://t.me/<bot>?start=setup_<code> (None if the bot username is unknown)
    expires_at: datetime

    def __repr__(self) -> str:  # never leak the code through a repr in logs
        return f"OwnerLink(url={'…' if self.url else None}, expires_at={self.expires_at.isoformat()})"


@dataclass(frozen=True, slots=True)
class OwnerClaim:
    user_id: int
    telegram_id: int
    previous_role: str | None  # None: the user did not exist


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _jsonb(value: Any) -> sa.ColumnElement[Any]:
    """JSON sent as text and cast on the server (independent of driver JSON codecs)."""
    return sa.cast(sa.literal(json.dumps(value, ensure_ascii=False), sa.Text), JSONB)


def owner_link_url(bot_username: str | None, code: str) -> str | None:
    if not bot_username or not _USERNAME_RE.fullmatch(bot_username.lstrip("@")):
        return None
    return f"https://t.me/{bot_username.lstrip('@')}?start={DEEP_LINK_PREFIX}{code}"


def code_from_payload(payload: str | None) -> str | None:
    """The code from a ``/start`` parameter ``setup_<code>``; ``None`` for anything else."""
    if not payload or not payload.startswith(DEEP_LINK_PREFIX):
        return None
    code = payload[len(DEEP_LINK_PREFIX) :]
    return code if _CODE_RE.fullmatch(code) else None


async def create_owner_link(
    db: _Database, *, bot_username: str | None = None, ttl: timedelta = LINK_TTL
) -> OwnerLink:
    """Generate a new one-time owner code (replacing any previous one) and store only its hash."""
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    code = secrets.token_urlsafe(CODE_BYTES)
    now = clock.now()
    expires_at = now + ttl
    value = {"hash": _digest(code), "created_at": now.isoformat(), "expires_at": expires_at.isoformat()}
    stmt = _upsert_meta(META_KEY, value, now)
    async with db.tx() as conn:
        await conn.execute(stmt)
    log.info("owner link created, valid until %s", expires_at.isoformat())
    return OwnerLink(code, DEEP_LINK_PREFIX + code, owner_link_url(bot_username, code), expires_at)


def _upsert_meta(key: str, value: Any, ts: datetime) -> sa.Executable:
    stmt = pg_insert(config_meta).values(key=key, value=_jsonb(value), updated_at=ts)
    return stmt.on_conflict_do_update(
        index_elements=[config_meta.c.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )


def _parse_meta(text: str | None) -> tuple[str, datetime] | None:
    if text is None:
        return None
    try:
        data = json.loads(text)
        digest, expires = data["hash"], datetime.fromisoformat(data["expires_at"])
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(digest, str) or expires.tzinfo is None:
        return None
    return digest, expires


async def redeem_owner_code(
    db: _Database,
    code: str,
    *,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    language: str | None = None,
) -> OwnerClaim:
    """Consume the code and make ``telegram_id`` an owner in one transaction (:class:`OwnerLinkError`)."""
    if not _CODE_RE.fullmatch(code):
        raise OwnerLinkError("invalid")
    digest = _digest(code)
    claim: OwnerClaim | None = None
    async with db.tx() as conn:
        row = (
            await conn.execute(
                sa.select(sa.cast(config_meta.c.value, sa.Text).label("value"))
                .where(config_meta.c.key == META_KEY)
                .with_for_update()
            )
        ).first()
        meta = _parse_meta(None if row is None else row[0])
        if meta is None or not hmac.compare_digest(meta[0], digest):
            raise OwnerLinkError("invalid")  # a wrong code does not consume the link
        await conn.execute(sa.delete(config_meta).where(config_meta.c.key == META_KEY))
        if meta[1] > clock.now():
            claim = await _make_owner(conn, telegram_id, username, first_name, language)
    if claim is None:  # expired: the row deletion above is committed before reporting
        raise OwnerLinkError("expired")
    log.info(
        "owner link redeemed: user %s is now an owner (was %s)", claim.user_id, claim.previous_role or "new"
    )
    return claim


async def _make_owner(
    conn: Any, telegram_id: int, username: str | None, first_name: str | None, language: str | None
) -> OwnerClaim:
    existing = (
        await conn.execute(
            sa.select(users.c.id, users.c.role).where(users.c.telegram_id == telegram_id).with_for_update()
        )
    ).first()
    previous: str | None = None
    if existing is None:
        user_id = (
            await conn.execute(
                sa.insert(users)
                .values(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    language=language,
                    role="owner",
                )
                .returning(users.c.id)
            )
        ).scalar()
    else:
        user_id, previous = existing[0], existing[1]
        await conn.execute(sa.update(users).where(users.c.id == user_id).values(role="owner"))
    await conn.execute(
        sa.insert(admin_audit).values(
            actor_id=user_id,
            role="owner",
            action="owner.claim",
            target=f"user:{user_id}",
            reason="одноразовая ссылка владельца",
            details=_jsonb({"telegram_id": telegram_id, "previous_role": previous}),
        )
    )
    return OwnerClaim(int(user_id), telegram_id, previous)


Invalidate = Callable[[int], Awaitable[None] | None]


class OwnerSetup:
    """Handles ``/start setup_<code>`` and shows the welcome screen.

    ``invalidate_user(telegram_id)`` drops the user from the app's LRU user cache after the role change;
    ``settings`` (optional) gets the new owner's id appended to ``OWNER_IDS``.
    """

    def __init__(
        self,
        db: _Database,
        router: ScreenRouter,
        *,
        settings: SettingsService | None = None,
        invalidate_user: Invalidate | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        attempt_window_s: float = ATTEMPT_WINDOW_S,
    ) -> None:
        self.db = db
        self.router = router
        self.settings = settings
        self.invalidate_user = invalidate_user
        self.max_attempts = max_attempts
        self.attempt_window_s = attempt_window_s
        self._attempts: OrderedDict[int, deque[float]] = OrderedDict()
        self._installed = False

    def install(self) -> None:
        """Register the welcome screen on the router (idempotent)."""
        if self._installed:
            return
        self._installed = True
        self.router.screen(SCREEN_WELCOME, required_role="owner")(self._welcome)

    async def _welcome(self, ctx: ScreenCtx, arg: Any) -> View:
        from svbg.tg.admin.settings import SCREEN_ROOT

        return View(
            text=_T["welcome"],
            parse_mode="HTML",
            keyboard=[[nav_button(_T["settings"], SCREEN_ROOT)], [nav_button(_T["menu"], self.router.home)]],
        )

    def aiogram_router(self, name: str = "svbg-owner-setup") -> Router:
        """``/start setup_<code>`` in private chats. Include it before the generic ``/start`` handler."""
        router = Router(name=name)

        async def on_start(message: Message) -> None:
            if not await self.handle_start(message):
                raise SkipHandler

        router.message.register(on_start, CommandStart(deep_link=True))
        return router

    def _allow_attempt(self, telegram_id: int) -> bool:
        """Sliding-window limit per Telegram user. The table is an LRU (most recent attempt last) with a hard
        size limit, so a flood of distinct accounts costs O(1) per attempt and bounded memory."""
        now = clock.monotonic()
        attempts = self._attempts
        window = attempts.get(telegram_id)
        if window is None:
            window = attempts[telegram_id] = deque()
        else:
            attempts.move_to_end(telegram_id)
        while window and now - window[0] > self.attempt_window_s:
            window.popleft()
        if len(window) >= self.max_attempts:
            return False
        window.append(now)
        # Oldest entries first: drop the ones whose window has expired, then enforce the hard limit.
        while attempts:
            oldest_id, oldest = next(iter(attempts.items()))
            if oldest_id == telegram_id:
                break
            if len(attempts) <= MAX_TRACKED_USERS and oldest and now - oldest[-1] <= self.attempt_window_s:
                break
            attempts.popitem(last=False)
        return True

    async def _say(self, chat_id: int, text: str) -> None:
        try:
            await self.router.transport.call(SendMessage(chat_id=chat_id, text=text), chat_id=chat_id)
        except (TelegramAPIError, OSError, TimeoutError) as e:
            log.warning("owner setup reply failed: %s", type(e).__name__)

    async def handle_start(self, message: Message) -> bool:
        """Handle ``/start setup_<code>``; ``False`` if the message is not an owner link."""
        tg_user = message.from_user
        if tg_user is None or message.chat.type != "private" or not message.text:
            return False
        parts = message.text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else None
        if not payload or not payload.startswith(DEEP_LINK_PREFIX):
            return False
        chat_id = message.chat.id
        if not self._allow_attempt(tg_user.id):
            await self._say(chat_id, _T["busy"])
            return True
        code = code_from_payload(payload)
        if code is None:
            await self._say(chat_id, _T["invalid"])
            return True
        try:
            claim = await redeem_owner_code(
                self.db,
                code,
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                language=tg_user.language_code,
            )
        except OwnerLinkError as e:
            log.info("owner link rejected for telegram user %s: %s", tg_user.id, e.reason)
            await self._say(chat_id, _T["invalid"])
            return True
        except (SQLAlchemyError, OSError) as e:
            log.warning("owner link: database error %s", type(e).__name__)
            await self._say(chat_id, _T["error"])
            return True
        self._attempts.pop(tg_user.id, None)
        await self._add_to_owner_ids(claim)
        await self._invalidate(tg_user.id)
        await self._greet(tg_user, chat_id, claim)
        return True

    async def _add_to_owner_ids(self, claim: OwnerClaim) -> None:
        if self.settings is None:
            return
        try:
            current = list(self.settings.current().get("OWNER_IDS") or [])
            if claim.telegram_id in current:
                return
            result = await self.settings.apply(
                [Change("OWNER_IDS", [*current, claim.telegram_id])], source="system", actor_id=claim.user_id
            )
        except Exception:
            log.exception("owner link: could not update OWNER_IDS")
            return
        if result.rejected:
            log.warning("owner link: OWNER_IDS not updated: %s", "; ".join(result.rejected.values()))

    async def _invalidate(self, telegram_id: int) -> None:
        if self.invalidate_user is None:
            return
        try:
            res = self.invalidate_user(telegram_id)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("owner link: user cache invalidation failed")

    async def _greet(self, tg_user: TgUser, chat_id: int, claim: OwnerClaim) -> None:
        user: UserCtx | None = None
        try:
            user = await self.router.user_loader(tg_user)
        except Exception:
            log.exception("owner link: user loader failed after the claim")
        if user is None:
            user = UserCtx(claim.user_id, telegram_id=claim.telegram_id, role="owner", lang="ru")
        elif user.role != "owner":  # a stale cache must not hide the welcome screen
            user = replace(user, role="owner")
        await self.router.show(user, chat_id, SCREEN_WELCOME, new=True)


class _Deps(Protocol):
    @property
    def db(self) -> Any: ...

    @property
    def settings(self) -> SettingsService: ...

    @property
    def users(self) -> Any: ...  # has ``invalidate(telegram_id)``


def setup(router: ScreenRouter, deps: _Deps) -> Router:
    """Module entry point for ``svbg.app``: the welcome screen and the ``/start setup_<code>`` handler."""
    owner = OwnerSetup(
        deps.db,
        router,
        settings=deps.settings,
        invalidate_user=getattr(deps.users, "invalidate", None),
    )
    owner.install()
    return owner.aiogram_router()
