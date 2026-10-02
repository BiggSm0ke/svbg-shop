"""Do Premium emoji work for this bot? The probe of 07 §2.4.1.

Button icons (``icon_custom_emoji_id``) and ``custom_emoji`` entities in texts are shown only if the bot's
owner has Telegram Premium or the bot has a username bought on Fragment. Telegram does not report an error
otherwise: it silently sends the message without them. So the bot checks:

1. **probe** — send a service message to an owner with a ``custom_emoji`` entity and a button with
   ``icon_custom_emoji_id`` (the icon just chosen in the button wizard, or a reference id from the content);
2. **compare** — in the ``Message`` the Bot API returned, the entity with the same ``custom_emoji_id`` must
   still be in ``entities`` and the button must still carry ``icon_custom_emoji_id``; otherwise
   → ``stripped``; the service message is deleted afterwards;
3. **result** — ``ok | stripped | unknown`` with the time of the check is stored in ``config_meta``
   (:data:`META_KEY`) and shown in «Состояние» and in the button wizard. :meth:`PremiumService.tick` (hourly)
   repeats the probe once a day and right away when the bot token changes (Premium may end).

A failed attempt (the send failed, no emoji to check, no bot) does **not** erase what is known: the last
result of the same bot and token and its ``checked_at`` stay, only ``attempted_at`` and ``detail`` change,
and the next try comes after :data:`RETRY_AFTER` (an hour), not a day. :meth:`PremiumService.tick` tries the
owners one by one until a send goes through (an owner who never started the bot answers 403).

Telegram-agnostic: the actual send/delete is a :class:`ProbeSender` (``svbg.tg.admin.content.probe``).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core import clock
from svbg.core.tables import config_meta

if TYPE_CHECKING:
    from svbg.content.store import ContentSnapshot
    from svbg.db.engine import Database

__all__ = [
    "META_KEY",
    "RECHECK_AFTER",
    "RETRY_AFTER",
    "STATUSES",
    "PremiumService",
    "PremiumState",
    "ProbeEcho",
    "ProbeSender",
    "evaluate",
    "load_state",
    "probe_message",
    "reference_emoji",
    "token_fingerprint",
]

log = logging.getLogger("svbg.content.premium")

Status = Literal["ok", "stripped", "unknown"]
STATUSES: Final[tuple[Status, ...]] = ("ok", "stripped", "unknown")
META_KEY: Final = "content.premium_emoji"
RECHECK_AFTER: Final = timedelta(days=1)
RETRY_AFTER: Final = timedelta(hours=1)
#: Shown in place of the Premium emoji (a custom emoji entity must cover an emoji).
FALLBACK_EMOJI: Final = "⭐"
PROBE_PREFIX: Final = "🔎 Служебная проверка премиум-эмодзи (сообщение сейчас удалится): "


def token_fingerprint(token: str | None) -> str | None:
    """A short one-way fingerprint of the bot token (to notice a token change; never the token itself)."""
    if not token:
        return None
    return hashlib.sha256(b"svbg:premium:" + token.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PremiumState:
    status: Status = "unknown"
    checked_at: datetime | None = None
    bot_id: int | None = None
    token_fp: str | None = None
    emoji_id: str | None = None
    detail: str | None = None  # why the last attempt failed (``None`` after a successful probe)
    attempted_at: datetime | None = None  # the last attempt, successful or not

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "bot_id": self.bot_id,
            "token_fp": self.token_fp,
            "emoji_id": self.emoji_id,
            "detail": self.detail,
            "attempted_at": self.attempted_at.isoformat() if self.attempted_at else None,
        }

    @property
    def failed_last(self) -> bool:
        """The last attempt did not produce a result."""
        return self.attempted_at is not None and (
            self.checked_at is None or self.attempted_at > self.checked_at
        )

    @classmethod
    def from_json(cls, raw: Any) -> PremiumState:
        if not isinstance(raw, Mapping):
            return cls()
        status = raw.get("status")
        bot_id = raw.get("bot_id")
        return cls(
            status=status if status in STATUSES else "unknown",
            checked_at=_dt(raw.get("checked_at")),
            attempted_at=_dt(raw.get("attempted_at")),
            bot_id=bot_id if isinstance(bot_id, int) and not isinstance(bot_id, bool) else None,
            token_fp=raw.get("token_fp") if isinstance(raw.get("token_fp"), str) else None,
            emoji_id=raw.get("emoji_id") if isinstance(raw.get("emoji_id"), str) else None,
            detail=raw.get("detail") if isinstance(raw.get("detail"), str) else None,
        )

    @property
    def label(self) -> str:
        """One line for «Состояние» and the button wizard."""
        when = f" (проверено {self.checked_at:%d.%m %H:%M} UTC)" if self.checked_at else ""
        if self.status == "ok":
            return f"✅ премиум-эмодзи работают{when}"
        if self.status == "stripped":
            return f"⚠️ премиум-эмодзи не показываются{when}"
        return f"❔ премиум-эмодзи не проверены{when}"

    @property
    def warning(self) -> str | None:
        """The honest warning of the wizard when icons will not show."""
        if self.status != "stripped":
            return None
        return (
            "⚠️ Telegram сейчас убирает премиум-эмодзи из сообщений бота: у владельца нет Telegram Premium "
            "и у бота нет username с Fragment. Иконка кнопки не покажется, а в тексте останется обычный "
            "эмодзи-заменитель. Сохранить можно — всё заработает, когда появится Premium."
        )


def _dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class ProbeEcho:
    """What the Bot API returned for the probe message (plain JSON-like data)."""

    message_id: int | None
    entities: Sequence[Mapping[str, Any]]
    reply_markup: Mapping[str, Any] | None


class ProbeSender(Protocol):
    async def send(
        self, chat_id: int, text: str, entities: list[dict[str, Any]], emoji_id: str
    ) -> ProbeEcho: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def probe_message(emoji_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Text and entities of the probe: the fallback emoji covered by a ``custom_emoji`` entity."""
    text = PROBE_PREFIX + FALLBACK_EMOJI
    entity = {
        "type": "custom_emoji",
        "offset": _utf16(PROBE_PREFIX),
        "length": _utf16(FALLBACK_EMOJI),
        "custom_emoji_id": emoji_id,
    }
    return text, [entity]


def _buttons(markup: Mapping[str, Any] | None) -> Iterable[Mapping[str, Any]]:
    rows = (markup or {}).get("inline_keyboard") or []
    for row in rows:
        for button in row or []:
            if isinstance(button, Mapping):
                yield button


def evaluate(echo: ProbeEcho, emoji_id: str) -> Status:
    """``ok`` when both the entity and the button icon survived, else ``stripped``."""
    entity_ok = any(
        e.get("type") == "custom_emoji" and e.get("custom_emoji_id") == emoji_id for e in echo.entities or ()
    )
    icon_ok = any(b.get("icon_custom_emoji_id") == emoji_id for b in _buttons(echo.reply_markup))
    return "ok" if entity_ok and icon_ok else "stripped"


def reference_emoji(snapshot: ContentSnapshot) -> str | None:
    """A custom emoji id used by the content (a button icon or a ``custom_emoji`` entity), if any."""
    for entry in snapshot.by_id.values():
        for button in entry.screen.buttons:
            if button.icon_custom_emoji_id:
                return button.icon_custom_emoji_id
    for entry in snapshot.by_id.values():
        for block in entry.screen.body.values():
            for e in block.entities:
                if e.get("type") == "custom_emoji" and isinstance(e.get("custom_emoji_id"), str):
                    return str(e["custom_emoji_id"])
    return None


BotInfo = Callable[[], tuple[int | None, str | None]]  # (bot id, token) of the current bot
OwnerChats = Callable[[], Awaitable[Iterable[int]]]


class PremiumService:
    """Runs the probe, stores and caches its result."""

    def __init__(
        self,
        db: Database,
        sender: Callable[[], ProbeSender | None],
        *,
        bot: BotInfo,
        owners: OwnerChats | None = None,
        reference: Callable[[], str | None] | None = None,
        recheck_after: timedelta = RECHECK_AFTER,
        retry_after: timedelta = RETRY_AFTER,
    ) -> None:
        self._db = db
        self._sender = sender
        self._bot = bot
        self._owners = owners
        self._reference = reference
        self.recheck_after = recheck_after
        self.retry_after = retry_after
        self._state: PremiumState | None = None

    @property
    def state(self) -> PremiumState:
        """The last known result (``unknown`` until :meth:`load` or a probe)."""
        return self._state or PremiumState()

    async def load(self) -> PremiumState:
        self._state = await load_state(self._db)
        return self._state

    async def _save(self, state: PremiumState) -> None:
        stmt = pg_insert(config_meta).values(key=META_KEY, value=state.to_json(), updated_at=sa.func.now())
        stmt = stmt.on_conflict_do_update(
            index_elements=[config_meta.c.key],
            set_={"value": stmt.excluded.value, "updated_at": sa.func.now()},
        )
        async with self._db.tx() as conn:
            await conn.execute(stmt)
        self._state = state

    def due(self, state: PremiumState | None = None, *, now: datetime | None = None) -> bool:
        """A probe is due: never checked, older than a day, another bot / token since the last check, or an
        hour after a failed attempt."""
        st = state or self.state
        bot_id, token = self._bot()
        if bot_id is None:
            return False
        if st.bot_id != bot_id or st.token_fp != token_fingerprint(token):
            return True
        now = now or clock.now()
        if st.failed_last and st.attempted_at is not None:
            return now - st.attempted_at >= self.retry_after
        if st.checked_at is None:
            return True
        return now - st.checked_at >= self.recheck_after

    def _failed(self, base: Mapping[str, Any], detail: str) -> PremiumState:
        """A failed attempt keeps the last result of the same bot and token (and its ``checked_at``)."""
        prev = self.state
        same = prev.bot_id == base["bot_id"] and prev.token_fp == base["token_fp"]
        return PremiumState(
            prev.status if same else "unknown",
            prev.checked_at if same else None,
            base["bot_id"],
            base["token_fp"],
            base["emoji_id"] or (prev.emoji_id if same else None),
            detail,
            clock.now(),
        )

    async def _attempt(self, chat_id: int, emoji_id: str | None) -> tuple[PremiumState, bool]:
        """``(state, sent)``; ``sent`` is ``False`` when the message could not be sent to ``chat_id``."""
        if self._state is None:
            await self.load()
        bot_id, token = self._bot()
        emoji = emoji_id or (self._reference() if self._reference is not None else None)
        base = {"bot_id": bot_id, "token_fp": token_fingerprint(token), "emoji_id": emoji}
        sender = self._sender()
        sent = True
        if emoji is None:
            state = self._failed(base, "нет эмодзи для проверки: выберите иконку кнопки")
        elif sender is None:
            state = self._failed(base, "бот не подключён")
        else:
            text, entities = probe_message(emoji)
            try:
                echo = await sender.send(chat_id, text, entities, emoji)
            except Exception as e:  # noqa: BLE001 - any failure means "could not check"
                log.warning("premium emoji probe failed: %s", type(e).__name__)
                state = self._failed(base, f"не удалось отправить ({type(e).__name__})")
                sent = False
            else:
                status = evaluate(echo, emoji)
                now = clock.now()
                state = PremiumState(status, now, attempted_at=now, **base)
                if echo.message_id is not None:
                    try:
                        await sender.delete(chat_id, echo.message_id)
                    except Exception as e:  # noqa: BLE001 - the result stands; a leftover message is harmless
                        log.info("could not delete the premium probe: %s", type(e).__name__)
        await self._save(state)
        log.info("premium emoji probe: %s%s", state.status, " (attempt failed)" if state.failed_last else "")
        return state, sent

    async def probe(self, chat_id: int, emoji_id: str | None = None) -> PremiumState:
        """Send the probe to ``chat_id`` and store the result. A failure (no emoji id, no bot, the send
        failed) keeps the last known result and records ``detail`` / ``attempted_at``."""
        state, _sent = await self._attempt(chat_id, emoji_id)
        return state

    async def tick(self) -> PremiumState | None:
        """Scheduler entry (hourly): probe when :meth:`due` — to the owners in turn until one send works."""
        if self._state is None:
            await self.load()
        if not self.due():
            return None
        owners = list(dict.fromkeys(await self._owners())) if self._owners is not None else []
        state: PremiumState | None = None
        for owner in owners:
            state, sent = await self._attempt(owner, None)
            if sent:
                break
        return state


async def load_state(db: Database) -> PremiumState:
    """The stored probe result (for «Состояние»: ``(await load_state(db)).label``)."""
    async with db.read() as conn:
        raw = (
            await conn.execute(sa.select(config_meta.c.value).where(config_meta.c.key == META_KEY))
        ).scalar_one_or_none()
    return PremiumState.from_json(raw)
