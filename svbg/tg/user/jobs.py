"""Background jobs of the user path.

* ``user.ui_ready`` — after the trial or a link reissue: queued in the **same** transaction as the panel job
  with the same ``ordering_key`` (``sub:<id>``), so it runs strictly after it. It turns the message that said
  «Подключаем…» into «✅ Готово + 🔗 Подключиться» (a new message when the old one cannot be edited). While
  the panel user is still not there it re-checks; a panel outage simply keeps it waiting behind the panel job.
* ``user.devices_refresh`` — one ``GET /hwid/devices/{id}`` (the only panel read of the devices screen,
  never on a click), stores the list in ``user_devices`` and, when the user asked a moment ago **and is still
  on that screen** (:class:`DevicesWatch`: nothing was clicked since), re-shows it. When the panel does not
  answer on the last attempt, the waiting user sees «панель временно недоступна» instead of «Загружаю…».
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.billing.ports import UiRef
from svbg.core.clock import now
from svbg.core.tables import users
from svbg.jobs.queue import enqueue
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.remnawave.writer import ordering_key
from svbg.subscriptions.tables import subscriptions
from svbg.tg.ui.context import UserCtx
from svbg.tg.user import seeds
from svbg.tg.user.messenger import link_button, menu_row
from svbg.tg.user.render import plain_view
from svbg.tg.user.tables import user_devices
from svbg.tg.user.texts import fmt_date, fmt_datetime, t

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.content.store import ContentStore
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import Handler, JobContext
    from svbg.tg.user.deps import DeviceFetcher
    from svbg.tg.user.messenger import UserMessenger

__all__ = [
    "DEVICES_JOB",
    "JOB_QUEUE",
    "UI_READY_JOB",
    "DevicesWatch",
    "UserJobs",
    "enqueue_devices_refresh",
    "enqueue_ui_ready",
    "store_devices",
]

log = logging.getLogger("svbg.tg.user.jobs")

JOB_QUEUE: Final = "user"
UI_READY_JOB: Final = "user.ui_ready"
DEVICES_JOB: Final = "user.devices_refresh"
UI_KINDS: Final = ("trial", "reissue")
UI_RECHECK_S: Final = 20.0
UI_MAX_ATTEMPTS: Final = 200  # retries cost nothing while the panel job blocks the queue (FIFO)
#: The devices screen is re-shown after a refresh only if the user asked within this window.
RESHOW_WINDOW: Final = timedelta(seconds=60)
#: ``ScreenRouter.show`` argument of the devices screen: the list was just refreshed / the panel is down.
DEVICES_REFRESHED: Final = "r"
DEVICES_FAILED: Final = "u"

#: Re-show a screen for a user: ``(user, chat_id, screen, arg)`` (``ScreenRouter.show``).
Show = Callable[[UserCtx, int, str, Any], Awaitable[Any]]


async def enqueue_ui_ready(
    conn: AsyncConnection, kind: str, *, subscription_id: int, user_id: int, ui_ref: UiRef | None
) -> int | None:
    """Queue the «ready» message after the subscription's panel jobs (same transaction, same order key)."""
    if kind not in UI_KINDS:
        raise ValueError(f"unknown ui kind {kind!r}")
    payload: dict[str, Any] = {"kind": kind, "sub_id": int(subscription_id), "user_id": int(user_id)}
    if ui_ref is not None:
        payload["ui_ref"] = ui_ref.as_json()
    return await enqueue(
        conn,
        UI_READY_JOB,
        payload,
        queue=JOB_QUEUE,
        lane="interactive",
        ordering_key=ordering_key(subscription_id),
        max_attempts=UI_MAX_ATTEMPTS,
        caused_by=f"user:{int(user_id)}",
    )


async def enqueue_devices_refresh(
    conn: AsyncConnection, *, subscription_id: int, user_id: int, chat_id: int | None, reshow: bool = True
) -> int | None:
    payload: dict[str, Any] = {
        "sub_id": int(subscription_id),
        "user_id": int(user_id),
        "chat_id": chat_id,
        "requested_at": now().isoformat() if reshow else None,
    }
    return await enqueue(
        conn,
        DEVICES_JOB,
        payload,
        queue=JOB_QUEUE,
        lane="interactive",
        dedup_key=f"{DEVICES_JOB}:{int(subscription_id)}",
        max_attempts=3,
        caused_by=f"user:{int(user_id)}",
    )


async def store_devices(
    conn: AsyncConnection, subscription_id: int, devices: list[Mapping[str, Any]]
) -> None:
    stmt = pg_insert(user_devices).values(subscription_id=subscription_id, devices=list(devices))
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[user_devices.c.subscription_id],
            set_={"devices": stmt.excluded.devices, "fetched_at": sa.func.now()},
        )
    )


def _int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentJobError(f"bad payload: {key}")
    return value


class DevicesWatch:
    """Who waits on the devices screen for the list being loaded (in memory, like the screen lock).

    :meth:`arm` is called by the screen that queued the refresh and remembers the user's latest update (the
    click itself, :meth:`UserDirectory.activity`); any later click or message means they moved on, and the job
    must not pull them back to the devices screen.
    """

    def __init__(
        self,
        activity: Callable[[int], int | None],
        *,
        clock: Callable[[], float] = time.monotonic,
        window: timedelta = RESHOW_WINDOW,
        limit: int = 10_000,
    ) -> None:
        self._activity = activity
        self._clock = clock
        self._window = window.total_seconds()
        self._limit = limit
        self._armed: dict[int, tuple[int | None, float]] = {}

    def arm(self, telegram_id: int | None) -> None:
        if telegram_id is None:
            return
        if len(self._armed) >= self._limit:
            self._armed.pop(next(iter(self._armed)))
        self._armed.pop(telegram_id, None)
        self._armed[telegram_id] = (self._activity(telegram_id), self._clock())

    def watching(self, telegram_id: int) -> bool:
        """The user asked within the window and has done nothing since."""
        entry = self._armed.get(telegram_id)
        if entry is None:
            return False
        mark, at = entry
        if self._clock() - at > self._window:
            return False
        return self._activity(telegram_id) == mark

    def disarm(self, telegram_id: int) -> None:
        self._armed.pop(telegram_id, None)


class UserJobs:
    def __init__(
        self,
        db: Database,
        *,
        messenger: UserMessenger,
        content: ContentStore | None = None,
        tz: Callable[[], str] = lambda: "Europe/Moscow",
        fetch_devices: DeviceFetcher | None = None,
        show: Show | None = None,
        watch: DevicesWatch | None = None,
    ) -> None:
        self._db = db
        self._messenger = messenger
        self._content = content
        self._tz = tz
        self._fetch = fetch_devices
        self._show = show
        self._watch = watch

    def handlers(self) -> dict[str, Handler]:
        return {UI_READY_JOB: self.ui_ready_job, DEVICES_JOB: self.devices_job}

    async def _facts(self, sub_id: int) -> Mapping[str, Any] | None:
        async with self._db.read() as conn:
            return (
                (
                    await conn.execute(
                        sa.select(
                            subscriptions.c.user_id,
                            subscriptions.c.link_state,
                            subscriptions.c.subscription_url,
                            subscriptions.c.paid_until,
                            subscriptions.c.is_trial,
                            subscriptions.c.panel_user_id,
                            users.c.telegram_id,
                            users.c.bot_blocked_at,
                        )
                        .select_from(subscriptions.join(users, users.c.id == subscriptions.c.user_id))
                        .where(subscriptions.c.id == sub_id)
                    )
                )
                .mappings()
                .first()
            )

    # ------------------------------------------------------------------------------------------ ui ready

    async def ui_ready_job(self, job: Job, ctx: JobContext) -> None:
        p = job.payload
        kind = p.get("kind")
        if kind not in UI_KINDS:
            raise PermanentJobError(f"bad payload: kind {kind!r}")
        sub_id = _int(p, "sub_id")
        row = await self._facts(sub_id)
        if row is None or row["link_state"] in ("closed", "panel_missing"):
            return
        if row["link_state"] != "linked" or not row["subscription_url"]:
            raise RetryJob(UI_RECHECK_S, "пользователь панели ещё не готов")
        lang = "ru"
        url = str(row["subscription_url"])
        user = UserCtx(int(row["user_id"]), telegram_id=row["telegram_id"])
        tz = self._tz()
        until = fmt_datetime(row["paid_until"], tz) if row["is_trial"] else fmt_date(row["paid_until"], tz)
        code = seeds.TRIAL_DONE if kind == "trial" else seeds.REISSUE_DONE
        view = plain_view(
            user,
            self._content,
            code,
            {"until": until},
            top=[[link_button(t(lang, "btn_connect"), url)]],
            bottom=[menu_row(lang)],
        )
        ref = UiRef.from_json(p.get("ui_ref"))
        await self._messenger.deliver(ref, row["telegram_id"], view)

    # ------------------------------------------------------------------------------------------ devices

    async def devices_job(self, job: Job, ctx: JobContext) -> None:
        if self._fetch is None:
            return
        p = job.payload
        sub_id = _int(p, "sub_id")
        row = await self._facts(sub_id)
        if row is None or row["link_state"] != "linked" or row["panel_user_id"] is None:
            return
        try:
            devices = await self._fetch(int(row["panel_user_id"]))
        except Exception as exc:  # noqa: BLE001 - any panel failure: retry later, the cache stays
            if job.is_last_attempt:  # no more tries: say so instead of «Загружаю…» forever
                await self._reshow(p, row, DEVICES_FAILED)
            raise RetryJob(10.0, f"панель не ответила: {type(exc).__name__}") from None
        async with self._db.tx() as conn:
            await store_devices(conn, sub_id, [d.as_json() for d in devices])
        await self._reshow(p, row, DEVICES_REFRESHED)

    async def _reshow(self, p: Mapping[str, Any], row: Mapping[str, Any], arg: str) -> None:
        """Redraw the devices screen for a user who asked a moment ago and is still looking at it."""
        requested = p.get("requested_at")
        chat_id = p.get("chat_id")
        telegram_id = row["telegram_id"]
        if self._show is None or not isinstance(requested, str) or not isinstance(chat_id, int):
            return
        try:
            at = datetime.fromisoformat(requested)
        except ValueError:
            return
        if now() - at > RESHOW_WINDOW or telegram_id is None:
            return
        if self._watch is not None:
            if not self._watch.watching(int(telegram_id)):
                return  # the user went elsewhere: do not pull them back
            self._watch.disarm(int(telegram_id))
        user = UserCtx(int(row["user_id"]), telegram_id=telegram_id)
        try:
            await self._show(user, chat_id, seeds.DEVICES, arg)
        except Exception:  # noqa: BLE001 - the list is stored; showing it again is a convenience
            log.warning("could not re-show the devices of subscription %s", p.get("sub_id"))
