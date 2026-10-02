"""Stage 2 end-to-end kit (helpers only, no tests): the whole application on a real PostgreSQL, the fake
Telegram Bot API, the fake Remnawave panel and the fake cash desks (RollyPay, CryptoBot) as HTTP servers —
nothing real is contacted.

* :func:`open_shop` writes ``.env`` (panel, admin group, RollyPay / Stars / CryptoBot / manual transfer),
  starts :class:`svbg.app.App`, seeds the owner's catalog (06 M1: 179/499/899/1699 ₽, trial plan) on the
  panel's squad and returns a :class:`Shop`;
* :class:`Person` is one Telegram user as the app sees them: ``/start``, presses buttons by label, waits for
  the text of a message (the fake keeps every message's current state, edits included).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from svbg.app import App
from svbg.billing import wallet
from svbg.catalog.preset import seed_owner_preset
from tests.dbkit import CountingDatabase
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, counting_db
from tests.fakes.cryptobot import API_TOKEN as CRYPTO_TOKEN
from tests.fakes.cryptobot import FakeCryptoBot
from tests.fakes.remnawave import FakeRemnawave
from tests.fakes.rollypay import API_KEY, SIGNING_SECRET, FakeRollyPay
from tests.fakes.telegram import FakeTelegram

__all__ = [
    "FAST",
    "GROUP",
    "OWNER_ID",
    "Person",
    "Shop",
    "make_bot_admin",
    "open_shop",
    "until",
    "until_async",
]

GROUP = -1_002_000_000_888
#: Short timings for tests (production defaults are asserted elsewhere).
FAST: dict[str, Any] = {
    "remnawave_kwargs": {
        "probe_timeout": 5.0,
        "close_grace": 0.5,
        "transport_overrides": {"max_attempts": 1, "breaker_cooldown": 0.3, "backoff_base": 0.01},
    },
    "inbox_poll_interval": 0.2,
    "jobs_poll_interval": 0.2,
    "hub_kwargs": {"update_interval": 0.5, "flush_interval": 0.2},
}


async def until(predicate: Callable[[], Any], timeout: float = 15.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not met within {timeout}s")
        await asyncio.sleep(0.03)


async def until_async(predicate: Callable[[], Any], timeout: float = 15.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not met within {timeout}s")
        await asyncio.sleep(0.05)


def _labels(markup: Any) -> list[dict[str, Any]]:
    rows = markup.get("inline_keyboard") or [] if isinstance(markup, dict) else []
    return [b for row in rows for b in row]


@dataclass
class Person:
    """One Telegram user talking to the bot."""

    tg: FakeTelegram
    telegram_id: int
    main: int | None = None  # message id of the main (screen) message

    def follow(self) -> int | None:
        """The screen moved to a new message (text ↔ picture, e.g. a code screen after one with the default
        banner: sent anew, the old one deleted) — the newest message of the chat becomes the main one."""
        if self.main is not None and self.tg.message(self.telegram_id, self.main) is None:
            self.main = self.last_sent() or self.main
        return self.main

    def message(self, message_id: int | None = None) -> dict[str, Any]:
        mid = message_id if message_id is not None else self.follow()
        assert mid is not None, "no main message yet"
        msg = self.tg.message(self.telegram_id, mid)
        assert msg is not None, f"message {mid} is not in the chat"
        return msg

    def text(self, message_id: int | None = None) -> str:
        msg = self.message(message_id)
        return str(msg.get("text") or msg.get("caption") or "").replace(" ", " ")

    def buttons(self, message_id: int | None = None) -> list[dict[str, Any]]:
        return _labels(self.message(message_id).get("reply_markup"))

    def button(self, label: str, message_id: int | None = None) -> dict[str, Any]:
        for b in self.buttons(message_id):
            if label in str(b.get("text", "")).replace(" ", " "):
                return b
        labels = [b.get("text") for b in self.buttons(message_id)]
        raise AssertionError(f"no button {label!r} in {labels}; text: {self.text(message_id)[:300]!r}")

    def last_sent(self, start: int = 0) -> int | None:
        sent = [
            c
            for c in self.tg.calls[start:]
            if c.ok
            and c.method in ("sendMessage", "sendPhoto")
            and c.params.get("chat_id") == self.telegram_id
        ]
        return int(sent[-1].result["message_id"]) if sent else None

    async def start(self, payload: str | None = None) -> int:
        """``/start``: the bot sends a fresh main message; returns its id."""
        begin = len(self.tg.calls)
        self.tg.push_message(self.telegram_id, "/start" if payload is None else f"/start {payload}")
        await until(lambda: self.last_sent(begin), what=f"/start answer to {self.telegram_id}")
        self.main = self.last_sent(begin)
        assert self.main is not None
        return self.main

    async def press(
        self, label: str, *, expect: str | None = None, message_id: int | None = None, timeout: float = 15.0
    ) -> str:
        """Press the button ``label`` on a message (default: the main one); optionally wait for ``expect`` in
        that message. Returns the toast (``answerCallbackQuery`` text) or ``""``."""
        mid = message_id if message_id is not None else self.follow()
        assert mid is not None
        data = self.button(label, mid).get("callback_data")
        assert data, f"button {label!r} has no callback data"
        return await self.click(str(data), message_id=message_id, expect=expect, timeout=timeout)

    async def click(
        self, data: str, *, message_id: int | None = None, expect: str | None = None, timeout: float = 15.0
    ) -> str:
        mid = message_id if message_id is not None else self.follow()
        assert mid is not None
        cq = self.tg.push_callback(self.telegram_id, data, mid)["callback_query"]["id"]
        answer = await self.tg.wait_for(
            "answerCallbackQuery", lambda c: c.params.get("callback_query_id") == cq, timeout
        )
        if expect is not None:  # the main message is followed when the screen moves to a new one
            await self.wait_text(expect, message_id=message_id, timeout=timeout)
        return str(answer.params.get("text") or "")

    async def wait_text(self, needle: str, *, message_id: int | None = None, timeout: float = 15.0) -> str:
        await until(
            lambda: needle in self.text(message_id),
            timeout,
            f"{needle!r} in message {message_id or self.main} (now: {self.text(message_id)[:300]!r})",
        )
        return self.text(message_id)


@dataclass
class Shop:
    app: App
    tg: FakeTelegram
    panel: FakeRemnawave
    desk: FakeRollyPay
    crypto: FakeCryptoBot
    squad: str
    env: AppEnv
    _people: dict[int, Person] = field(default_factory=dict)

    @property
    def db(self) -> CountingDatabase:
        return counting_db(self.app.db)

    def person(self, telegram_id: int) -> Person:
        return self._people.setdefault(telegram_id, Person(self.tg, telegram_id))

    async def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.raw(sql, *args)]

    async def user_id(self, telegram_id: int) -> int:
        rows = await self.rows("select id from users where telegram_id = $1", telegram_id)
        assert rows, f"user {telegram_id} is not registered"
        return int(rows[0]["id"])

    async def fund(self, telegram_id: int, amount_minor: int, ref: str = "e2e") -> None:
        uid = await self.user_id(telegram_id)
        async with self.app.db.tx() as conn:  # type: ignore[union-attr]
            await wallet.credit(
                conn,
                uid,
                amount_minor,
                reason="bonus",
                ref_type="test",
                ref_id=f"{ref}-{uid}",
                currency="RUB",
            )

    async def balance(self, telegram_id: int) -> int:
        rows = await self.rows("select wallet_minor from users where telegram_id = $1", telegram_id)
        return int(rows[0]["wallet_minor"])

    def webhook_url(self, slug: str) -> str:
        assert self.app.pay_instances is not None and self.app.web is not None
        inst = self.app.pay_instances.by_slug(slug)
        assert inst is not None and inst.enabled, f"payment instance {slug} is not enabled"
        return f"http://127.0.0.1:{self.app.web.port}/webhooks/pay/{inst.id}/{inst.webhook_token}"

    async def payments_of(self, telegram_id: int) -> list[dict[str, Any]]:
        uid = await self.user_id(telegram_id)
        return await self.rows("select * from payments where user_id = $1 order by created_at", uid)

    async def assert_wallet_invariants(self) -> None:
        """Σ ledger = balance for every user, running sums never below zero, ≤ 1 waiting purchase each."""
        async with self.app.db.read() as conn:  # type: ignore[union-attr]
            assert await wallet.mismatches(conn) == []
        bad = await self.rows(
            "select id from (select id, balance_after,"
            " sum(amount_minor) over (partition by user_id order by id) as running from wallet_ledger) t"
            " where running <> balance_after or balance_after < 0"
        )
        assert bad == [], bad
        assert await self.rows("select id from users where wallet_minor < 0") == []
        waiting = await self.rows(
            "select user_id from orders where status = 'awaiting_funds' group by user_id having count(*) > 1"
        )
        assert waiting == []


def make_bot_admin(tg: FakeTelegram, token: str, chat_id: int = GROUP) -> None:
    tg.make_admin(
        chat_id,
        tg.bot_id(token),
        can_manage_topics=True,
        can_pin_messages=True,
        can_delete_messages=True,
        can_manage_chat=True,
    )


@asynccontextmanager
async def open_shop(
    start_app: StartApp,
    app_env: AppEnv,
    *,
    admin_chat: bool = True,
    extra_env: dict[str, str | None] | None = None,
    **app_kwargs: Any,
) -> AsyncIterator[Shop]:
    async with (
        FakeRemnawave(webhook_enabled=False) as panel,
        FakeRollyPay() as desk,
        FakeCryptoBot() as crypto,
    ):
        squad = panel.add_internal_squad("NL")
        token = panel.add_token()
        values: dict[str, str | None] = {
            "REMNAWAVE_URL": panel.url,
            "REMNAWAVE_TOKEN": token,
            "SUPPORT_URL": "https://t.me/svbg_support",
            "PAY_ROLLYPAY_ENABLED": "true",
            "PAY_ROLLYPAY_API_KEY": API_KEY,
            "PAY_ROLLYPAY_SIGNING_SECRET": SIGNING_SECRET,
            "PAY_ROLLYPAY_BASE_URL": desk.base_url,
            "PAY_STARS_ENABLED": "true",
            "PAY_CRYPTOBOT_ENABLED": "true",
            "PAY_CRYPTOBOT_API_TOKEN": CRYPTO_TOKEN,
            "PAY_CRYPTOBOT_BASE_URL": crypto.base_url,
            "PAY_MANUAL_ENABLED": "true",
            "PAY_MANUAL_DETAILS": "Карта 2200 0000 0000 0000, получатель Иван И.",
        }
        if admin_chat:
            make_bot_admin(app_env.tg, app_env.token)
            values["ADMIN_CHAT_ID"] = str(GROUP)
        values.update(extra_env or {})
        app_env.write(**values)
        app = await start_app(**{**FAST, **app_kwargs})
        assert app.payments is not None and app.catalog is not None, app.missing_modules
        async with app.db.tx() as conn:  # type: ignore[union-attr]
            await seed_owner_preset(conn, squads=[squad])
        await app.catalog.changed()
        yield Shop(app, app_env.tg, panel, desk, crypto, squad, app_env)
