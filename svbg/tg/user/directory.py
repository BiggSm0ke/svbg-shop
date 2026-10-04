"""User directory: Telegram user → :class:`UserCtx` with an LRU cache (the hot path of every click).

* :meth:`UserDirectory.load` is the ``user_loader`` of the screen router: a cache hit costs no SQL; a
  miss is one ``INSERT … ON CONFLICT … RETURNING`` that registers the user, refreshes the name and
  ``last_seen_at`` and clears ``bot_blocked_at`` (the user talks to the bot again).
* The effective role is ``owner`` for ids in ``OWNER_IDS`` (settings, applied hot) or the stored role.
* Banned users get ``None`` (the router then ignores the update).
* :meth:`activity` — a per-process, strictly increasing number of the user's latest update (any click or
  message passes :meth:`load`), in memory: a background job uses it to tell whether the user did anything
  since they opened the screen it is about to redraw.
* :meth:`owner_ids` = ``OWNER_IDS`` ∪ users with ``role='owner'`` (cached briefly); used for owner DMs and
  for the "bot is being set up" gate.

No business logic here — only identity and caching.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.tables import users
from svbg.tg.ui.context import USER_ROLES, UserCtx

if TYPE_CHECKING:
    from aiogram.types import User as TgUser

    from svbg.core.settings.store import DatabaseLike

__all__ = ["SUPPORTED_LANGS", "UserDirectory"]

log = logging.getLogger("svbg.tg.user")

SUPPORTED_LANGS: Final = frozenset({"ru", "en"})
_MAX_NAME: Final = 256


class _SettingsSource(Protocol):
    def current(self) -> Mapping[str, Any]: ...


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:_MAX_NAME] or None


def _perms(raw: Any) -> frozenset[str]:
    if isinstance(raw, list):
        return frozenset(str(p) for p in raw if isinstance(p, str))
    return frozenset()


class UserDirectory:
    def __init__(
        self,
        db: DatabaseLike,
        settings: _SettingsSource,
        *,
        cache_size: int = 20_000,
        ttl: float = 60.0,
        owners_ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_size < 1 or ttl <= 0 or owners_ttl <= 0:
            raise ValueError("cache_size, ttl and owners_ttl must be positive")
        self._db = db
        self._settings = settings
        self._cache: OrderedDict[int, tuple[float, UserCtx | None]] = OrderedDict()
        self._cache_size = cache_size
        self._ttl = ttl
        self._owners_ttl = owners_ttl
        self._clock = clock
        self._db_owners: frozenset[int] = frozenset()
        self._db_owners_at: float | None = None
        self._owners_lock = asyncio.Lock()
        self._seen: OrderedDict[int, int] = OrderedDict()
        self._seq = itertools.count(1)
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------ settings-derived values

    def _setting(self, key: str, default: Any) -> Any:
        try:
            value = self._settings.current()[key]
        except (KeyError, RuntimeError):
            return default
        return default if value is None else value

    def configured_owner_ids(self) -> frozenset[int]:
        raw = self._setting("OWNER_IDS", [])
        return frozenset(int(x) for x in raw if isinstance(x, int) and not isinstance(x, bool))

    def _lang(self, stored: str | None) -> str:
        if stored in SUPPORTED_LANGS:
            return str(stored)
        default = self._setting("DEFAULT_LANGUAGE", "ru")
        return default if default in SUPPORTED_LANGS else "ru"

    # ------------------------------------------------------------------ cache

    def invalidate(self, telegram_id: int | None = None) -> None:
        """Forget one user (role/perms/ban changed) or everybody (``None``, e.g. OWNER_IDS changed)."""
        if telegram_id is None:
            self._cache.clear()
            self._db_owners_at = None
        else:
            self._cache.pop(telegram_id, None)

    def _cached(self, telegram_id: int) -> tuple[bool, UserCtx | None]:
        entry = self._cache.get(telegram_id)
        if entry is None:
            return False, None
        expires, ctx = entry
        if expires < self._clock():
            del self._cache[telegram_id]
            return False, None
        self._cache.move_to_end(telegram_id)
        return True, ctx

    def _remember(self, telegram_id: int, ctx: UserCtx | None) -> None:
        self._cache[telegram_id] = (self._clock() + self._ttl, ctx)
        self._cache.move_to_end(telegram_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------ loading

    async def load(self, tg_user: TgUser) -> UserCtx | None:
        """``UserCtx`` for a Telegram user (registers new users). ``None`` for bots and banned users."""
        if tg_user.is_bot:
            return None
        self._touch(tg_user.id)
        hit, ctx = self._cached(tg_user.id)
        if hit:
            self.hits += 1
            return ctx
        self.misses += 1
        ctx = await self._fetch(tg_user)
        self._remember(tg_user.id, ctx)
        return ctx

    async def _fetch(self, tg_user: TgUser) -> UserCtx | None:
        ins = pg_insert(users).values(
            telegram_id=tg_user.id,
            username=_clip(tg_user.username),
            first_name=_clip(tg_user.first_name),
            last_seen_at=sa.func.now(),
        )
        stmt = ins.on_conflict_do_update(
            index_elements=[users.c.telegram_id],
            set_={
                "username": ins.excluded.username,
                "first_name": ins.excluded.first_name,
                "last_seen_at": sa.func.now(),
                "bot_blocked_at": None,
            },
        ).returning(
            users.c.id,
            users.c.role,
            users.c.perms,
            users.c.language,
            users.c.banned_at,
            users.c.captcha_passed_at,
            users.c.staff_role_id,
            sa.literal_column("(xmax = 0)").label("inserted"),
        )
        async with self._db.tx() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        if row is None:  # pragma: no cover - INSERT … RETURNING always returns a row
            return None
        if row["banned_at"] is not None:
            return None
        role = str(row["role"]) if row["role"] in USER_ROLES else "user"
        if tg_user.id in self.configured_owner_ids():
            role = "owner"
        if row["inserted"]:
            log.info("new user %s registered", row["id"])
        return UserCtx(
            user_id=int(row["id"]),
            telegram_id=tg_user.id,
            role=role,
            perms=_perms(row["perms"]),
            lang=self._lang(row["language"]),
            is_new=bool(row["inserted"]),
            currency=str(self._setting("CURRENCY", "RUB")),
            captcha_passed=row["captcha_passed_at"] is not None,
            staff_role=None if row["staff_role_id"] is None or role == "owner" else int(row["staff_role_id"]),
        )

    def _touch(self, telegram_id: int) -> None:
        self._seen[telegram_id] = next(self._seq)
        self._seen.move_to_end(telegram_id)
        while len(self._seen) > self._cache_size:
            self._seen.popitem(last=False)

    def activity(self, telegram_id: int) -> int | None:
        """The number of the user's latest update in this process (``None``: not seen lately). A larger number
        means a later update; equal numbers mean nothing happened in between."""
        return self._seen.get(telegram_id)

    def peek(self, telegram_id: int) -> UserCtx | None:
        """The cached context of a Telegram user without any SQL (``None`` when not cached or expired)."""
        hit, ctx = self._cached(telegram_id)
        return ctx if hit else None

    async def set_language(self, user_id: int, telegram_id: int | None, lang: str) -> bool:
        """Store the user's language (one SQL) and drop the cached context. ``False`` for an unknown code."""
        if lang not in SUPPORTED_LANGS:
            return False
        async with self._db.tx() as conn:
            await conn.execute(sa.update(users).where(users.c.id == user_id).values(language=lang))
        if telegram_id is not None:
            self.invalidate(telegram_id)
        return True

    async def mark_captcha_passed(self, user_id: int, telegram_id: int | None) -> bool:
        """Store that the user passed the entry captcha (the first time only) and drop the cached context.
        ``True`` when this call stored it (``False``: it was already stored)."""
        stmt = (
            sa.update(users)
            .where(users.c.id == user_id, users.c.captcha_passed_at.is_(None))
            .values(captcha_passed_at=sa.func.now())
        )
        async with self._db.tx() as conn:
            stored = (await conn.execute(stmt)).rowcount == 1
        if telegram_id is not None:
            self.invalidate(telegram_id)
        return stored

    # ------------------------------------------------------------------ owners

    async def owner_ids(self) -> frozenset[int]:
        """``OWNER_IDS`` plus users promoted to owner in the database (cached for ``owners_ttl``)."""
        configured = self.configured_owner_ids()
        now = self._clock()
        if self._db_owners_at is None or now - self._db_owners_at > self._owners_ttl:
            async with self._owners_lock:
                if self._db_owners_at is None or self._clock() - self._db_owners_at > self._owners_ttl:
                    await self._refresh_db_owners()
        return configured | self._db_owners

    async def _refresh_db_owners(self) -> None:
        stmt = sa.select(users.c.telegram_id).where(
            users.c.role == "owner", users.c.telegram_id.is_not(None), users.c.banned_at.is_(None)
        )
        try:
            async with self._db.read() as conn:
                rows = (await conn.execute(stmt)).all()
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot read owners from the database: %s", type(exc).__name__)
            return  # keep the previous list; retry on the next call
        self._db_owners = frozenset(int(r[0]) for r in rows)
        self._db_owners_at = self._clock()

    async def has_owner(self) -> bool:
        return bool(await self.owner_ids())

    # ------------------------------------------------------------------ delivery feedback

    async def mark_blocked(self, chat_id: int | str) -> None:
        """Notifier hook: the user blocked the bot (403). Private chats only."""
        if not isinstance(chat_id, int) or chat_id <= 0:
            return
        stmt = (
            sa.update(users)
            .where(users.c.telegram_id == chat_id, users.c.bot_blocked_at.is_(None))
            .values(bot_blocked_at=sa.func.now())
        )
        try:
            async with self._db.tx() as conn:
                await conn.execute(stmt)
        except (sa.exc.SQLAlchemyError, OSError) as exc:
            log.warning("cannot mark chat as blocked: %s", type(exc).__name__)
        self.invalidate(chat_id)
