"""What billing needs from the outside world, as small protocols (billing never imports aiogram).

* :class:`Messenger` — edit or send a user's chat message. The user path implements it over the notifier and
  the screen engine: ``notice.screen`` is a system screen id (content may override the text), ``notice.text``
  and ``notice.buttons`` are the built-in fallback, ``Button.action`` is a logical action the user path
  maps to its callback data (``menu``, ``reorder``, ``topup``…).
* :class:`AttentionPort` — «Требует внимания» (``AttentionService.raise_item``).

:class:`UiRef` is the stored address of a purchase message (``orders.ui_ref``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = ["AttentionPort", "Button", "Messenger", "Notice", "UiRef"]


@dataclass(frozen=True, slots=True)
class UiRef:
    """A chat message: where it is and when it was sent (Telegram stops editing old messages)."""

    chat_id: int
    message_id: int
    at: datetime

    def as_json(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "message_id": self.message_id, "at": self.at.isoformat()}

    @classmethod
    def from_json(cls, value: Any) -> UiRef | None:
        """``None`` for a missing or damaged value (a new message is sent instead of an edit)."""
        if not isinstance(value, Mapping):
            return None
        try:
            at = datetime.fromisoformat(str(value["at"]))
            chat_id, message_id = value["chat_id"], value["message_id"]
            if isinstance(chat_id, bool) or isinstance(message_id, bool):
                return None
            return cls(int(chat_id), int(message_id), at if at.tzinfo else at.replace(tzinfo=UTC))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Button:
    """One inline button: exactly one of ``url`` / ``web_app`` / ``action``."""

    text: str
    url: str | None = None
    web_app: str | None = None
    action: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if sum(x is not None for x in (self.url, self.web_app, self.action)) != 1:
            raise ValueError("a button needs exactly one of url / web_app / action")


@dataclass(frozen=True, slots=True)
class Notice:
    """A message to show: system screen id + parameters, with a Russian fallback text and buttons."""

    screen: str
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)


class Messenger(Protocol):
    async def edit(self, ref: UiRef, notice: Notice) -> bool:
        """Edit the message in place. ``False``: it cannot be edited (deleted, too old) — billing sends a
        new one. Transient failures (network, Telegram 5xx) raise; the job retries."""
        ...

    async def send(self, telegram_id: int, notice: Notice) -> UiRef | None:
        """Send a new message; ``None`` when the user blocked the bot (nothing to retry)."""
        ...


class AttentionPort(Protocol):
    async def raise_item(
        self, dedup_key: str, severity: str, title: str, body: str = "", fix_action: str | None = None
    ) -> Any: ...
