"""Referral program core (05 §2.3, 07 §1.2): binding, days rewards with deferred sides and caps, the basic
percent mode, reports to «🤝 Партнёры» and the data of the «Пригласить» screen.

Flow::

    /start r_<code> ──▶ attach_referrer() ── 1 SQL ──▶ referrals row + job
                          └▶ job referral.attached: welcome messages, bus «user.attached_referrer»,
                             (trigger=register) pair job
    trial.activated / subscription.term_changed (bus, durable relay) ──▶ job referral.pair
    order.fulfilled (bus, durable relay, percent mode)               ──▶ job referral.pct
    hourly sweep: deferred → expired after 168 h, re-check deferred sides, catch missed pairs

Rules:

* **no retro-binding** (always on): a user who already has a subscription, a trial, any wallet movement or a
  referrer is never bound; self-referral and A↔B cycles are refused; the code owner must not be banned;
* **one short transaction per pair**, locks in the fixed order of the purchase path: ``users`` (both, by id)
  → ``subscriptions`` → ``referrals``; the decision is the pure :func:`svbg.referral.rules.decide`;
* days go through :meth:`SubscriptionLifecycle.extend` (``kind='referral'``, ``source='referral'``): a bonus
  in ``subscription_events`` (does not move the LTE reset day, never converts a trial), an expired
  subscription revives as ``now + days``, a frozen one keeps the days in its frozen balance; the panel only
  through the writer's job;
* ``referral_rewards`` with ``UNIQUE(referred_user_id, side) WHERE kind='days'`` makes a reward structural:
  a second worker or a repeated hook cannot grant twice;
* messages are jobs written in the same transaction (one per message: a retry never repeats a sent one);
  only *transitions* notify (a re-check of a deferred side writes and says nothing).

Hot paths: :meth:`attach_referrer` — 1 SQL (the job is inserted by the same statement);
:meth:`invite_view` — 1 SQL; no HTTP anywhere in this module.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.billing import wallet
from svbg.billing.tables import PURCHASE_KINDS as ORDER_PURCHASE_KINDS
from svbg.billing.tables import wallet_ledger
from svbg.core.bus import Event
from svbg.core.clock import now as clock_now
from svbg.core.money import format_money
from svbg.core.tables import users
from svbg.jobs.queue import NOTIFY_CHANNEL, enqueue
from svbg.jobs.tables import jobs as jobs_table
from svbg.jobs.worker import PermanentJobError
from svbg.referral import texts
from svbg.referral.rules import (
    Action,
    Decision,
    InviterStats,
    Mode,
    PairState,
    Rules,
    SideAction,
    SideState,
    Trigger,
    decide,
    percent_reward,
)
from svbg.referral.tables import (
    DAYS_PREDICATE,
    PCT_PREDICATE,
    referral_codes,
    referral_rewards,
    referrals,
)
from svbg.subscriptions.lifecycle import LIVE_STATES, PURCHASE_KINDS, SubscriptionLifecycle
from svbg.subscriptions.tables import subscription_events, subscriptions, trial_grants

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.scheduler import Scheduler
    from svbg.jobs.worker import Handler, JobContext

__all__ = [
    "EVENTS",
    "JOB_ATTACHED",
    "JOB_NOTIFY",
    "JOB_PAIR",
    "JOB_PCT",
    "K_PARTNERS",
    "InviteView",
    "PairOutcome",
    "Poster",
    "ReferralService",
    "UserSender",
    "new_code",
]

log = logging.getLogger("svbg.referral")

JOB_PAIR: Final = "referral.pair"
JOB_PCT: Final = "referral.pct"
JOB_ATTACHED: Final = "referral.attached"
JOB_NOTIFY: Final = "referral.notify"
JOB_QUEUE: Final = "hook"
NOTIFY_QUEUE: Final = "notify"
K_PARTNERS: Final = "partners"
#: Bus events the service listens to (published by the durable relay of :mod:`svbg.subscriptions.hooks`).
EVENTS: Final = ("trial.activated", "subscription.term_changed", "order.fulfilled")
EVENT_ATTACHED: Final = "user.attached_referrer"
#: ``subscription_events.kind`` / ``source`` of referral days.
EVENT_KIND: Final = "referral"
DAY_S: Final = 86_400
SWEEP_EVERY_S: Final = 3600
SWEEP_LIMIT: Final = 1000
#: Pairs attached this recently without any reward row are re-checked by the sweep (a lost hook).
CATCH_UP: Final = timedelta(days=7)
CAP_WINDOW: Final = timedelta(days=30)
#: ``subscription_events.source`` of plans nobody paid for (admin grants, gifts): they never count as
#: «оплатил» for the paid trigger (the same list as LTE ``NON_MONEY_SOURCES``).
NON_MONEY_SOURCES: Final = ("admin", "promo", "referral", "bonus", "compensation", "gift")
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,62}$")
_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LEN: Final = 10


_AWARDS: Final = ("награда", "награды", "наград")


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    """Russian plural: 1 награда, 2 награды, 5 наград (11–14 — «наград»)."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def new_code() -> str:
    """A fresh personal code (31^10 ≈ 8·10^14 values; a collision is retried)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))


class Poster(Protocol):
    """``AdminChatService.post`` subset."""

    async def post(self, kind: str, text: str, *, html: bool = False) -> Any: ...


class UserSender(Protocol):
    """``Notifier.send`` subset: ``None`` when the user blocked the bot (not an error)."""

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> Any: ...


@dataclass(frozen=True, slots=True)
class InviteView:
    """Data of the «Пригласить» screen (one SQL)."""

    enabled: bool
    code: str | None = None
    link: str | None = None
    share_text: str = ""
    share_url: str | None = None
    invited: int = 0
    subscribed: int = 0
    conversion: int = 0  # percent of the invited who got a subscription
    days_earned: int = 0
    money_earned_minor: int = 0
    pending: int = 0
    lines: tuple[str, ...] = ()  # HTML lines for the screen («Как работают награды» + counters)


@dataclass(frozen=True, slots=True)
class PairOutcome:
    """What one pass over a pair did (tests, logs)."""

    referred_user_id: int
    decision: Decision | None = None
    granted: Mapping[str, int] = field(default_factory=dict)  # side → days
    skipped: str | None = None  # why nothing was looked at (no pair, mode off)


@dataclass(frozen=True, slots=True)
class _Person:
    id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None

    @property
    def mention(self) -> str:
        return texts.mention(self.username, self.first_name, self.telegram_id, self.id)

    @property
    def who(self) -> str:
        return texts.admin_who(self.username, self.first_name, self.telegram_id, self.id)


def _person(row: Mapping[str, Any]) -> _Person:
    return _Person(int(row["id"]), row["telegram_id"], row["username"], row["first_name"])


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


Config = Callable[[], Mapping[str, Any]]


class ReferralService:
    """See module docstring. Register :meth:`handlers`, :meth:`install` on the bus and :meth:`schedule`."""

    def __init__(
        self,
        db: Database,
        *,
        config: Config,
        lifecycle: SubscriptionLifecycle | None = None,
        bus: EventBus | None = None,
        poster: Poster | None = None,
        sender: UserSender | None = None,
        overrides: texts.Overrides | None = None,
        bot_username: Callable[[], str | None] = lambda: None,
        timezone: Callable[[], str] = lambda: "Europe/Moscow",
        clock: Callable[[], datetime] = clock_now,
        code_factory: Callable[[], str] = new_code,
    ) -> None:
        self._db = db
        self._config = config
        self._lifecycle = lifecycle or SubscriptionLifecycle()
        self.bus = bus
        self.poster = poster
        self.sender = sender
        self.overrides = overrides
        self._bot_username = bot_username
        self._timezone = timezone
        self._clock = clock
        self._code_factory = code_factory

    # ------------------------------------------------------------------------------------------ settings

    def rules(self) -> Rules:
        try:
            return Rules.from_config(self._config())
        except Exception:  # noqa: BLE001 - a broken snapshot means «off», never a crash of /start
            log.warning("referral: settings unavailable, the program is treated as off")
            return Rules(enabled=False)

    def _tz(self) -> str:
        try:
            return str(self._timezone() or "Europe/Moscow")
        except Exception:  # noqa: BLE001
            return "Europe/Moscow"

    def _cfg_currency(self) -> str:
        try:
            return str(self._config().get("CURRENCY") or "RUB")
        except Exception:  # noqa: BLE001
            return "RUB"

    # ------------------------------------------------------------------------------------------ binding

    async def attach_referrer(self, user_id: int, code: str, *, source: str = "link") -> bool:
        """Bind the owner of ``code`` as the referrer of ``user_id`` (deep-link contract of stage 3).

        ``False`` when the program is off, the code is unknown, it is the user's own code, the user already
        has a referrer, a subscription, a trial or any wallet movement (no retro-binding), or the referrer is
        banned. One statement: the binding, its job and the workers' wake-up.
        """
        rules = self.rules()
        if not rules.enabled or not isinstance(code, str):
            return False
        code = code.strip()
        if not _CODE_RE.match(code) or isinstance(user_id, bool) or not isinstance(user_id, int):
            return False
        at = self._clock()
        uid = sa.literal(user_id, sa.BigInteger)
        rc = referral_codes
        owner = users.alias("owner")
        me = users.alias("me")
        back = referrals.alias("back")
        candidate = (
            sa.select(uid, rc.c.user_id, sa.literal(at, sa.DateTime(timezone=True)), sa.literal(source))
            .select_from(rc.join(owner, owner.c.id == rc.c.user_id))
            .where(
                rc.c.code == code,
                rc.c.user_id != uid,
                owner.c.banned_at.is_(None),
                sa.exists().where(me.c.id == uid, me.c.banned_at.is_(None)),
                ~sa.exists().where(subscriptions.c.user_id == uid),
                ~sa.exists().where(trial_grants.c.user_id == uid),
                ~sa.exists().where(wallet_ledger.c.user_id == uid),
                ~sa.exists().where(back.c.referred_user_id == rc.c.user_id, back.c.referrer_id == uid),
            )
        )
        ins = (
            pg_insert(referrals)
            .from_select(["referred_user_id", "referrer_id", "attached_at", "source"], candidate)
            .on_conflict_do_nothing(index_elements=["referred_user_id"])
            .returning(referrals.c.referred_user_id, referrals.c.referrer_id)
            .cte("ins")
        )
        # The «attached» job goes in the same statement (one round trip on /start); the PK of ``referrals``
        # makes it once per user, so no dedup key is needed. NOTIFY wakes the workers on commit.
        job = (
            sa.insert(jobs_table)
            .from_select(
                ["queue", "lane", "kind", "payload", "max_attempts"],
                sa.select(
                    sa.literal(JOB_QUEUE),
                    sa.literal("interactive"),
                    sa.literal(JOB_ATTACHED),
                    sa.func.jsonb_build_object(
                        "referred_user_id", ins.c.referred_user_id, "referrer_id", ins.c.referrer_id
                    ),
                    sa.literal(5),
                ).select_from(ins),
            )
            .returning(jobs_table.c.id)
            .cte("job")
        )
        stmt = (
            sa.select(ins.c.referrer_id, sa.func.pg_notify(NOTIFY_CHANNEL, "interactive"))
            .select_from(ins)
            .add_cte(job)
        )
        async with self._db.tx() as conn:
            referrer_id = (await conn.execute(stmt)).scalar()
        if referrer_id is None:
            return False
        log.info("referral: user %s attached to %s", user_id, referrer_id)
        return True

    async def _attached_job(self, job: Job, _ctx: JobContext) -> None:
        referred = _int(job.payload.get("referred_user_id"))
        referrer = _int(job.payload.get("referrer_id"))
        if referred is None or referrer is None:
            raise PermanentJobError("повреждённая задача рефералки")
        rules = self.rules()
        async with self._db.tx() as conn:
            if not job.payload.get("after_captcha") and await self._behind_captcha(conn, referred):
                return  # the welcome and the days «за переход» wait for the captcha (captcha_passed)
            people = await self._people(conn, (referred, referrer), lock=False)
            invitee, inviter = people.get(referred), people.get(referrer)
            if invitee is None or inviter is None:
                return
            for user, key, values in self._welcome(rules, invitee, inviter):
                await self._enqueue_message(conn, user, key, values)
            if rules.days_active and rules.trigger is Trigger.REGISTER:
                await self._enqueue_pair(conn, referred)
        if self.bus is not None:
            await self.bus.publish(Event(EVENT_ATTACHED, {"user_id": referred, "referrer_id": referrer}))

    def _captcha_on(self) -> bool:
        """``CAPTCHA_ENABLED`` (a config without the key, as in old setups, means off)."""
        try:
            return bool(self._config().get("CAPTCHA_ENABLED"))
        except Exception:  # noqa: BLE001 - a broken snapshot: do not hold anything back
            return False

    async def _behind_captcha(self, conn: AsyncConnection, user_id: int) -> bool:
        """The invited user still has to pass the entry captcha (a plain user while it is on). A bot that
        never passes it brings its inviter neither a message nor days."""
        if not self._captcha_on():
            return False
        role = await conn.scalar(sa.select(users.c.role).where(users.c.id == user_id))
        return role in (None, "user")

    async def captcha_passed(self, user_id: int) -> bool:
        """The user passed the entry captcha: queue the welcome (and the days «за переход») that waited for
        it. ``False`` when nobody invited them."""
        async with self._db.tx() as conn:
            referrer = await conn.scalar(
                sa.select(referrals.c.referrer_id).where(referrals.c.referred_user_id == user_id)
            )
            if referrer is None:
                return False
            await enqueue(
                conn,
                JOB_ATTACHED,
                {"referred_user_id": user_id, "referrer_id": int(referrer), "after_captcha": True},
                queue=JOB_QUEUE,
                lane="interactive",
                dedup_key=f"ref:att:{user_id}",
                max_attempts=5,
            )
        return True

    @staticmethod
    def _welcome(rules: Rules, invitee: _Person, inviter: _Person) -> list[tuple[int, str, dict[str, Any]]]:
        out: list[tuple[int, str, dict[str, Any]]] = []
        if rules.mode is Mode.PERCENT:
            out.append((invitee.id, "welcome_invitee_plain", {"name": inviter.mention}))
            key = "new_referral_percent" if rules.percent_active else "new_referral_plain"
            out.append((inviter.id, key, {"name": invitee.mention, "percent": rules.percent}))
            return out
        if rules.invitee_days > 0:
            key = {
                Trigger.PAID: "welcome_invitee_paid",
                Trigger.TRIAL_OR_PAID: "welcome_invitee_trial",
                Trigger.REGISTER: "welcome_invitee_register",
            }[rules.trigger]
        else:
            key = "welcome_invitee_plain"
        out.append((invitee.id, key, {"name": inviter.mention, "days": rules.invitee_days}))
        if rules.inviter_days > 0:
            key = "new_referral_register" if rules.trigger is Trigger.REGISTER else "new_referral_days"
        else:
            key = "new_referral_plain"
        out.append((inviter.id, key, {"name": invitee.mention, "days": rules.inviter_days}))
        return out

    # ------------------------------------------------------------------------------------------ events

    def install(self, bus: EventBus) -> Callable[[], None]:
        """Subscribe to the durable domain events; returns the unsubscribe function."""
        if self.bus is None:
            self.bus = bus
        off = [bus.subscribe(name, self.on_event) for name in EVENTS]

        def uninstall() -> None:
            for fn in off:
                fn()

        return uninstall

    async def on_event(self, event: Event) -> None:
        """Queue the work an event causes (never raises: the relay must not loop)."""
        try:
            await self._on_event(event)
        except Exception:  # isolation boundary: a reward problem never breaks the sale
            log.exception("referral: handling %s failed", event.name)

    async def _on_event(self, event: Event) -> None:
        rules = self.rules()
        p = event.payload
        user_id = _int(p.get("user_id"))
        if user_id is None:
            return
        if event.name == "order.fulfilled":
            total = _int(p.get("total_minor")) or 0
            order_id = _int(p.get("order_id"))
            if not rules.percent_active or order_id is None or total <= 0:
                return
            if str(p.get("kind")) not in ORDER_PURCHASE_KINDS:
                return
            async with self._db.tx() as conn:
                await enqueue(
                    conn,
                    JOB_PCT,
                    {
                        "order_id": order_id,
                        "user_id": user_id,
                        "total_minor": total,
                        "currency": str(p.get("currency") or self._cfg_currency()),
                    },
                    queue=JOB_QUEUE,
                    dedup_key=f"ref:pct:{order_id}",
                    max_attempts=10,
                    caused_by=f"order:{order_id}",
                )
            return
        if event.name == "subscription.term_changed" and str(p.get("kind")) == EVENT_KIND:
            return  # our own days: nothing new for any pair
        if not rules.days_active:
            return
        await self.touch(user_id)

    async def touch(self, user_id: int) -> int:
        """«Something changed for ``user_id``»: queue the pair where they are invited and every pair with a
        side deferred for them. Returns the number of pair jobs queued (1 SELECT + the inserts)."""
        own = sa.select(referrals.c.referred_user_id).where(referrals.c.referred_user_id == user_id)
        waiting = sa.select(referral_rewards.c.referred_user_id).where(
            referral_rewards.c.user_id == user_id,
            referral_rewards.c.status == "deferred",
            referral_rewards.c.kind == "days",
        )
        async with self._db.tx() as conn:
            ids = sorted({int(r) for r in (await conn.execute(sa.union(own, waiting))).scalars()})
            for rid in ids:
                await self._enqueue_pair(conn, rid)
        return len(ids)

    @staticmethod
    async def _enqueue_pair(conn: AsyncConnection, referred_user_id: int) -> None:
        await enqueue(
            conn,
            JOB_PAIR,
            {"referred_user_id": referred_user_id},
            queue=JOB_QUEUE,
            lane="background",
            dedup_key=f"ref:{referred_user_id}",
            max_attempts=10,
        )

    # ------------------------------------------------------------------------------------------ pairs

    async def _pair_job(self, job: Job, _ctx: JobContext) -> None:
        referred = _int(job.payload.get("referred_user_id"))
        if referred is None:
            raise PermanentJobError("повреждённая задача рефералки")
        await self.process_pair(referred)

    async def process_pair(self, referred_user_id: int) -> PairOutcome:
        """One pass over the pair of ``referred_user_id`` in one transaction (see module docstring)."""
        rules = self.rules()
        at = self._clock()
        async with self._db.tx() as conn:
            referrer_id = await conn.scalar(
                sa.select(referrals.c.referrer_id).where(referrals.c.referred_user_id == referred_user_id)
            )
            if referrer_id is None:
                return PairOutcome(referred_user_id, skipped="no_pair")
            referrer_id = int(referrer_id)
            if not rules.days_active:
                await self._expire_due(conn, at, referred_user_id)
                return PairOutcome(referred_user_id, skipped="off")
            people = await self._people(conn, (referred_user_id, referrer_id), lock=True)
            invitee, inviter = people.get(referred_user_id), people.get(referrer_id)
            if invitee is None or inviter is None:
                return PairOutcome(referred_user_id, skipped="no_user")
            subs = await self._live_subs(conn, (referred_user_id, referrer_id))
            await conn.execute(
                sa.select(referrals.c.referred_user_id)
                .where(referrals.c.referred_user_id == referred_user_id)
                .with_for_update()
            )
            sides = await self._sides(conn, referred_user_id)
            qualifies, stats = await self._facts(
                conn, rules, referred_user_id, referrer_id, at, captcha_on=self._captcha_on()
            )
            pair = PairState(
                referred_user_id=referred_user_id,
                referrer_id=referrer_id,
                qualifies=qualifies,
                inviter_has_sub=referrer_id in subs,
                invitee_has_sub=referred_user_id in subs,
                sides=sides,
            )
            decision = decide(rules, pair, stats, at)
            if not decision.writes:
                return PairOutcome(referred_user_id, decision)
            granted: dict[str, int] = {}
            until: dict[str, datetime | None] = {}
            for action in decision.actions:
                recipient = referrer_id if action.side == "inviter" else referred_user_id
                if action.action is Action.GRANT:
                    sid = subs[recipient]
                    applied = await self._lifecycle.extend(
                        conn,
                        sid,
                        action.days * DAY_S,
                        source=EVENT_KIND,
                        kind=EVENT_KIND,
                        ref_type="referral",
                        ref_id=f"{referred_user_id}:{action.side}",
                        reason=f"referral {action.side}",
                        caused_by=f"referral:{referred_user_id}",
                        lane="background",
                    )
                    await self._store(conn, action, referred_user_id, recipient, rules, at, sid)
                    granted[action.side] = action.days
                    until[action.side] = applied.new_paid_until
                elif (action.action is Action.DEFER and action.fresh) or action.action in (
                    Action.EXPIRE,
                    Action.DENY,
                ):
                    await self._store(conn, action, referred_user_id, recipient, rules, at, None)
            await self._announce(conn, rules, decision, invitee, inviter, granted, until, stats, at)
        if granted:
            log.info("referral: pair %s→%s granted %s", referrer_id, referred_user_id, granted)
        return PairOutcome(referred_user_id, decision, granted)

    @staticmethod
    async def _people(conn: AsyncConnection, ids: Sequence[int], *, lock: bool) -> dict[int, _Person]:
        stmt = (
            sa.select(users.c.id, users.c.telegram_id, users.c.username, users.c.first_name)
            .where(users.c.id.in_(sorted(set(ids))))
            .order_by(users.c.id)
        )
        if lock:
            stmt = stmt.with_for_update()  # same order as the purchase path: users first, by id
        rows = (await conn.execute(stmt)).mappings().all()
        return {int(r["id"]): _person(r) for r in rows}

    @staticmethod
    async def _live_subs(conn: AsyncConnection, ids: Sequence[int]) -> dict[int, int]:
        """``user_id → subscription id`` of the live subscription with the most time left (locked)."""
        rows = (
            await conn.execute(
                sa.select(subscriptions.c.id, subscriptions.c.user_id, subscriptions.c.paid_until)
                .where(
                    subscriptions.c.user_id.in_(sorted(set(ids))),
                    subscriptions.c.link_state.in_(LIVE_STATES),
                )
                .order_by(subscriptions.c.id)
                .with_for_update()
            )
        ).all()
        best: dict[int, tuple[datetime | None, int]] = {}
        for sid, uid, until in rows:
            current = best.get(int(uid))
            if current is None or (until is not None and (current[0] is None or until > current[0])):
                best[int(uid)] = (until, int(sid))
        return {uid: sid for uid, (_, sid) in best.items()}

    @staticmethod
    async def _sides(conn: AsyncConnection, referred_user_id: int) -> dict[str, SideState]:
        rows = (
            await conn.execute(
                sa.select(
                    referral_rewards.c.side,
                    referral_rewards.c.status,
                    referral_rewards.c.retry_until,
                    referral_rewards.c.reason,
                ).where(
                    referral_rewards.c.referred_user_id == referred_user_id,
                    referral_rewards.c.kind == "days",
                )
            )
        ).all()
        return {str(side): SideState(str(status), retry, reason) for side, status, retry, reason in rows}

    @staticmethod
    async def _facts(
        conn: AsyncConnection,
        rules: Rules,
        referred: int,
        referrer: int,
        at: datetime,
        *,
        captcha_on: bool = False,
    ) -> tuple[bool, InviterStats]:
        """Whether the invited user meets the trigger, and the inviter's granted counters (one SELECT).
        «Сразу за переход» counts only once the invited user passed the entry captcha (while it is on)."""
        rr = referral_rewards
        passed = sa.exists().where(
            users.c.id == referred, sa.or_(users.c.captcha_passed_at.is_not(None), users.c.role != "user")
        )
        paid = sa.exists().where(
            subscription_events.c.subscription_id == subscriptions.c.id,
            subscriptions.c.user_id == referred,
            subscription_events.c.kind.in_(PURCHASE_KINDS),
            subscription_events.c.source.not_in(NON_MONEY_SOURCES),
        )
        trial = sa.exists().where(trial_grants.c.user_id == referred)
        granted = sa.and_(
            rr.c.user_id == referrer, rr.c.side == "inviter", rr.c.kind == "days", rr.c.status == "granted"
        )
        row = (
            await conn.execute(
                sa.select(
                    paid.label("paid"),
                    trial.label("trial"),
                    passed.label("passed"),
                    sa.select(sa.func.count())
                    .where(granted, rr.c.granted_at > at - CAP_WINDOW)
                    .scalar_subquery()
                    .label("g30"),
                    sa.select(sa.func.count()).where(granted).scalar_subquery().label("gall"),
                )
            )
        ).one()
        if rules.trigger is Trigger.REGISTER:
            qualifies = bool(row.passed) or not captcha_on
        elif rules.trigger is Trigger.TRIAL_OR_PAID:
            qualifies = bool(row.paid or row.trial)
        else:
            qualifies = bool(row.paid)
        return qualifies, InviterStats(int(row.g30 or 0), int(row.gall or 0))

    @staticmethod
    async def _store(  # noqa: PLR0917 - flat row
        conn: AsyncConnection,
        action: SideAction,
        referred: int,
        recipient: int,
        rules: Rules,
        at: datetime,
        subscription_id: int | None,
    ) -> None:
        status = {
            Action.GRANT: "granted",
            Action.DEFER: "deferred",
            Action.EXPIRE: "expired",
            Action.DENY: "denied",
        }[action.action]
        values: dict[str, Any] = {
            "status": status,
            "days": action.days if action.action is Action.GRANT else None,
            "subscription_id": subscription_id,
            "trigger": rules.trigger.value,
            "reason": action.reason,
            "granted_at": at if action.action is Action.GRANT else None,
            "retry_until": action.retry_until if action.action is Action.DEFER else None,
        }
        stmt = pg_insert(referral_rewards).values(
            referred_user_id=referred, user_id=recipient, side=action.side, kind="days", **values
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["referred_user_id", "side"],
            index_where=sa.text(DAYS_PREDICATE),
            set_=values,
            where=referral_rewards.c.status == "deferred",  # settled rows never change
        )
        await conn.execute(stmt)

    @staticmethod
    async def _expire_due(conn: AsyncConnection, at: datetime, referred: int | None = None) -> int:
        stmt = (
            sa.update(referral_rewards)
            .where(referral_rewards.c.status == "deferred", referral_rewards.c.retry_until <= at)
            .values(status="expired")
        )
        if referred is not None:
            stmt = stmt.where(referral_rewards.c.referred_user_id == referred)
        result = await conn.execute(stmt)
        return int(result.rowcount or 0)

    async def _announce(  # noqa: PLR0917 - one report per pass
        self,
        conn: AsyncConnection,
        rules: Rules,
        decision: Decision,
        invitee: _Person,
        inviter: _Person,
        granted: Mapping[str, int],
        until: Mapping[str, datetime | None],
        stats: InviterStats,
        at: datetime,
    ) -> None:
        """User messages for granted sides; one admin report (grant) or one warning (fresh deferral)."""
        if "inviter" in granted:
            await self._enqueue_message(
                conn, inviter.id, "granted_inviter", {"days": granted["inviter"], "name": invitee.mention}
            )
        if "invitee" in granted:
            await self._enqueue_message(
                conn, invitee.id, "granted_invitee", {"days": granted["invitee"], "name": inviter.mention}
            )
        fresh = decision.newly_deferred
        if not granted and not fresh:
            return  # expiries and denials are silent
        tz = self._tz()
        a = texts.ADMIN
        lines = [a["granted_title"] if granted else a["deferred_title"]]
        for side, person in (("inviter", inviter), ("invitee", invitee)):
            lines.append(a[side].format(who=person.who))
            action = decision.inviter if side == "inviter" else decision.invitee
            if side in granted:
                lines.append(
                    a["side_granted"].format(days=granted[side], until=texts.fmt_date(until.get(side), tz))
                )
            elif action.action is Action.DEFER:
                cap = rules.cap_30d if action.reason == "cap_30d" else rules.cap_total
                template = texts.REASONS.get(action.reason or "", "{until}")
                reason = template.format(cap=cap, until=texts.fmt_date(action.retry_until, tz))
                lines.append(a["side_waiting"].format(reason=reason))
            else:
                lines.append(a["side_none"])
        lines.append(
            a["trigger"].format(trigger=texts.TRIGGERS.get(rules.trigger.value, rules.trigger.value))
        )
        total = stats.granted_total + (1 if "inviter" in granted else 0)
        lines.append(a["total"].format(total=total))
        lines.append(texts.fmt_date(at, tz))
        await self._enqueue_admin(conn, "\n".join(lines))

    # ------------------------------------------------------------------------------------------ percent

    async def _pct_job(self, job: Job, _ctx: JobContext) -> None:
        p = job.payload
        order_id, user_id, total = _int(p.get("order_id")), _int(p.get("user_id")), _int(p.get("total_minor"))
        currency = p.get("currency")
        if order_id is None or user_id is None or total is None or not isinstance(currency, str):
            raise PermanentJobError("повреждённая задача рефералки")
        await self.reward_percent(order_id, user_id, total, currency)

    async def reward_percent(self, order_id: int, user_id: int, total_minor: int, currency: str) -> int:
        """Percent mode: credit the inviter of ``user_id`` for the paid purchase ``order_id``. Returns the
        credited amount (``0``: no referrer, mode off, duplicate)."""
        rules = self.rules()
        amount = percent_reward(rules, total_minor)
        if amount <= 0:
            return 0
        ref = f"order:{order_id}"
        async with self._db.tx() as conn:
            referrer_id = await conn.scalar(
                sa.select(referrals.c.referrer_id).where(referrals.c.referred_user_id == user_id)
            )
            if referrer_id is None:
                return 0
            referrer_id = int(referrer_id)
            stored = await conn.scalar(
                pg_insert(referral_rewards)
                .values(
                    referred_user_id=user_id,
                    user_id=referrer_id,
                    side="inviter",
                    kind="wallet_pct",
                    status="granted",
                    amount_minor=amount,
                    currency=currency,
                    payment_id=ref,
                    trigger="paid",
                    reason=f"{rules.percent}%",
                    granted_at=self._clock(),
                )
                .on_conflict_do_nothing(
                    index_elements=["payment_id", "side"], index_where=sa.text(PCT_PREDICATE)
                )
                .returning(referral_rewards.c.id)
            )
            if stored is None:
                return 0
            entry = await wallet.credit(
                conn,
                referrer_id,
                amount,
                reason="bonus",
                ref_type="referral",
                ref_id=ref,
                currency=currency,
                note=f"рефералка {rules.percent}%",
            )
            if entry is None:
                raise PermanentJobError("начисление рефералки уже есть в журнале кошелька")
            people = await self._people(conn, (user_id, referrer_id), lock=False)
            invitee, inviter = people.get(user_id), people.get(referrer_id)
            money = format_money(amount, currency)
            if invitee is not None:
                await self._enqueue_message(
                    conn, referrer_id, "granted_percent", {"amount": money, "name": invitee.mention}
                )
            if invitee is not None and inviter is not None:
                a = texts.ADMIN
                text = "\n".join(
                    (
                        a["pct_title"],
                        a["inviter"].format(who=inviter.who),
                        a["pct_line"].format(
                            amount=money, percent=rules.percent, total=format_money(total_minor, currency)
                        ),
                        a["invitee"].format(who=invitee.who),
                        a["pct_order"].format(order=order_id),
                    )
                )
                await self._enqueue_admin(conn, text)
        return amount

    # ------------------------------------------------------------------------------------------ messages

    async def _enqueue_message(
        self, conn: AsyncConnection, user_id: int, key: str, values: Mapping[str, Any]
    ) -> None:
        await enqueue(
            conn,
            JOB_NOTIFY,
            {"user_id": user_id, "key": key, "values": dict(values)},
            queue=NOTIFY_QUEUE,
            lane="background",
            max_attempts=5,
        )

    async def _enqueue_admin(self, conn: AsyncConnection, text: str) -> None:
        await enqueue(
            conn, JOB_NOTIFY, {"admin": text}, queue=NOTIFY_QUEUE, lane="background", max_attempts=5
        )

    async def _notify_job(self, job: Job, _ctx: JobContext) -> None:
        p = job.payload
        admin = p.get("admin")
        if isinstance(admin, str):
            if self.poster is None or not admin.strip():
                return
            await self.poster.post(K_PARTNERS, admin, html=True)
            return
        user_id, key, values = _int(p.get("user_id")), p.get("key"), p.get("values")
        if user_id is None or not isinstance(key, str) or not isinstance(values, Mapping):
            raise PermanentJobError("повреждённое уведомление рефералки")
        if key not in texts.USER:
            raise PermanentJobError(f"неизвестный текст рефералки {key}")
        if self.sender is None:
            log.info("referral: no sender, message %s to user %s dropped", key, user_id)
            return
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(users.c.telegram_id, users.c.bot_blocked_at, users.c.banned_at).where(
                        users.c.id == user_id
                    )
                )
            ).first()
        if (
            row is None
            or row.telegram_id is None
            or row.bot_blocked_at is not None
            or row.banned_at is not None
        ):
            return
        text = texts.render(key, overrides=self.overrides, **dict(values))
        await self.sender.send(int(row.telegram_id), text, parse_mode="HTML")

    # ------------------------------------------------------------------------------------------ jobs

    def handlers(self) -> dict[str, Handler]:
        return {
            JOB_ATTACHED: self._attached_job,
            JOB_PAIR: self._pair_job,
            JOB_PCT: self._pct_job,
            JOB_NOTIFY: self._notify_job,
        }

    def schedule(self, scheduler: Scheduler) -> None:
        scheduler.every("referral.sweep", SWEEP_EVERY_S, self.sweep, jitter_s=60)

    async def sweep(self) -> None:
        await self.sweep_once()

    async def sweep_once(self) -> tuple[int, int]:
        """Hourly: deferred sides past the window → ``expired`` (silently); re-check the other deferred
        sides (a slot under the cap may have freed) and recent pairs without any reward row (a lost hook).
        Returns ``(expired, queued)``."""
        rules = self.rules()
        at = self._clock()
        rr, rf = referral_rewards, referrals
        async with self._db.tx() as conn:
            expired = await self._expire_due(conn, at)
            if not rules.days_active:
                return expired, 0
            deferred = (
                sa.select(rr.c.referred_user_id)
                .where(rr.c.status == "deferred", rr.c.kind == "days")
                .limit(SWEEP_LIMIT)
            )
            has_reward = sa.exists().where(
                rr.c.referred_user_id == rf.c.referred_user_id, rr.c.kind == "days"
            )
            fresh = (
                sa.select(rf.c.referred_user_id)
                .where(rf.c.attached_at > at - CATCH_UP, ~has_reward)
                .limit(SWEEP_LIMIT)
            )
            ids = sorted({int(r) for r in (await conn.execute(sa.union(deferred, fresh))).scalars()})
            for rid in ids:
                await self._enqueue_pair(conn, rid)
        return expired, len(ids)

    # ------------------------------------------------------------------------------------------ screens

    def invite_link(self, code: str) -> str | None:
        bot = self._bot_username()
        if not bot or not _CODE_RE.match(code):
            return None
        return f"https://t.me/{bot.lstrip('@')}?start=r_{code}"

    async def invite_view(self, user_id: int, lang: str | None = "ru") -> InviteView:
        """The «Пригласить» screen: personal code (created on first open), link, share text, counters —
        one SQL. Program off: no SQL at all."""
        rules = self.rules()
        if not rules.enabled:
            return InviteView(False, lines=(texts.render("off", lang, table=texts.SCREEN),))
        row = None
        for _ in range(3):  # a random code collides with probability ~0; retry anyway
            row = await self._invite_row(user_id, self._code_factory())
            if row is not None and row["code"] is not None:
                break
        if row is None or row["code"] is None:
            raise RuntimeError("не удалось выдать реферальный код")
        code = str(row["code"])
        invited, subscribed = int(row["invited"] or 0), int(row["subscribed"] or 0)
        conversion = round(subscribed * 100 / invited) if invited else 0
        link = self.invite_link(code)
        share = self._share_text(rules, lang)
        lines = [*self.reward_lines(rules, lang)]
        stats = texts.render(
            "stats", lang, table=texts.SCREEN, invited=invited, subscribed=subscribed, conversion=conversion
        )
        lines.append(stats)
        pending = int(row["pending"] or 0)
        if pending:
            lines.append(texts.render("pending", lang, table=texts.SCREEN, pending=pending))
        return InviteView(
            enabled=True,
            code=code,
            link=link,
            share_text=share,
            share_url=texts.share_url(link, share) if link else None,
            invited=invited,
            subscribed=subscribed,
            conversion=conversion,
            days_earned=int(row["days"] or 0),
            money_earned_minor=int(row["money"] or 0),
            pending=pending,
            lines=tuple(lines),
        )

    async def _invite_row(self, user_id: int, candidate: str) -> Mapping[str, Any] | None:
        rc, rf, rr = referral_codes, referrals, referral_rewards
        ins = (
            pg_insert(rc)
            .values(user_id=user_id, code=candidate)
            .on_conflict_do_nothing()
            .returning(rc.c.code)
            .cte("ins")
        )
        mine = sa.and_(rr.c.user_id == user_id, rr.c.side == "inviter")
        has_sub = sa.exists().where(subscriptions.c.user_id == rf.c.referred_user_id)
        stmt = sa.select(
            sa.func.coalesce(
                sa.select(ins.c.code).scalar_subquery(),
                sa.select(rc.c.code).where(rc.c.user_id == user_id).scalar_subquery(),
            ).label("code"),
            sa.select(sa.func.count()).where(rf.c.referrer_id == user_id).scalar_subquery().label("invited"),
            sa.select(sa.func.count())
            .where(rf.c.referrer_id == user_id, has_sub)
            .scalar_subquery()
            .label("subscribed"),
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.days), 0))
            .where(mine, rr.c.kind == "days", rr.c.status == "granted")
            .scalar_subquery()
            .label("days"),
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.amount_minor), 0))
            .where(mine, rr.c.kind == "wallet_pct", rr.c.status == "granted")
            .scalar_subquery()
            .label("money"),
            sa.select(sa.func.count())
            .where(rr.c.user_id == user_id, rr.c.status == "deferred")
            .scalar_subquery()
            .label("pending"),
        )
        async with self._db.tx() as conn:
            return (await conn.execute(stmt)).mappings().first()

    @staticmethod
    def reward_lines(rules: Rules, lang: str | None = "ru") -> list[str]:
        """«🎁 Как работают награды» from the same rules the rewards use (one source of numbers)."""
        r = texts.SCREEN
        lines = [texts.render("how_title", lang, table=r)]
        if rules.mode is Mode.PERCENT:
            if rules.percent > 0:
                lines.append(texts.render("percent", lang, table=r, percent=rules.percent))
            return lines
        if rules.inviter_days > 0:
            key = {
                Trigger.PAID: "inviter_paid",
                Trigger.TRIAL_OR_PAID: "inviter_trial",
                Trigger.REGISTER: "inviter_register",
            }[rules.trigger]
            lines.append(texts.render(key, lang, table=r, days=rules.inviter_days))
        if rules.invitee_days > 0:
            key = "invitee_register" if rules.trigger is Trigger.REGISTER else "invitee"
            lines.append(texts.render(key, lang, table=r, days=rules.invitee_days))
        if rules.inviter_days > 0 and rules.cap_30d > 0:
            lines.append(texts.render("cap", lang, table=r, cap=rules.cap_30d))
        return lines

    @staticmethod
    def _share_text(rules: Rules, lang: str | None) -> str:
        r = texts.SCREEN
        if rules.mode is Mode.DAYS and rules.invitee_days > 0:
            key = "share_register" if rules.trigger is Trigger.REGISTER else "share_days"
            return texts.render(key, lang, table=r, days=rules.invitee_days)
        return texts.render("share_plain", lang, table=r)

    # ------------------------------------------------------------------------------------------ admin

    async def overview(self) -> dict[str, int]:
        """Counters for the admin screen and «Состояние» (one SELECT)."""
        rr, rf = referral_rewards, referrals
        days = sa.and_(rr.c.kind == "days")

        def count(*cond: Any) -> Any:
            return sa.select(sa.func.count()).where(*cond).scalar_subquery()

        stmt = sa.select(
            sa.select(sa.func.count()).select_from(rf).scalar_subquery().label("pairs"),
            count(days, rr.c.status == "granted").label("granted"),
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.days), 0))
            .where(days, rr.c.status == "granted")
            .scalar_subquery()
            .label("days"),
            count(days, rr.c.status == "deferred").label("deferred"),
            count(days, rr.c.status == "expired").label("expired"),
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.amount_minor), 0))
            .where(rr.c.kind == "wallet_pct", rr.c.status == "granted")
            .scalar_subquery()
            .label("money"),
        )
        async with self._db.read() as conn:
            row = (await conn.execute(stmt)).mappings().one()
        return {k: int(v or 0) for k, v in row.items()}

    async def dry_run(self) -> int:
        """How many pairs the bot would reward if days mode were on now: pairs without any reward row whose
        invited user meets the trigger (05 §2.3.3 «будет выдано N пар», e.g. right after an import)."""
        rules = self.rules()
        rf, rr = referrals, referral_rewards
        paid = sa.exists().where(
            subscription_events.c.subscription_id == subscriptions.c.id,
            subscriptions.c.user_id == rf.c.referred_user_id,
            subscription_events.c.kind.in_(PURCHASE_KINDS),
            subscription_events.c.source.not_in(NON_MONEY_SOURCES),
        )
        trial = sa.exists().where(trial_grants.c.user_id == rf.c.referred_user_id)
        cond: list[Any] = [
            ~sa.exists().where(rr.c.referred_user_id == rf.c.referred_user_id, rr.c.kind == "days")
        ]
        if rules.trigger is Trigger.PAID:
            cond.append(paid)
        elif rules.trigger is Trigger.TRIAL_OR_PAID:
            cond.append(sa.or_(paid, trial))
        async with self._db.read() as conn:
            return int(await conn.scalar(sa.select(sa.func.count()).select_from(rf).where(*cond)) or 0)

    async def report_lines(self, since: datetime) -> list[str]:
        """Section of the daily report: days and money granted since ``since`` (one SELECT)."""
        rr = referral_rewards
        recent = sa.and_(rr.c.status == "granted", rr.c.granted_at >= since)
        stmt = sa.select(
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.days), 0))
            .where(recent, rr.c.kind == "days")
            .scalar_subquery()
            .label("days"),
            sa.select(sa.func.count()).where(recent, rr.c.kind == "days").scalar_subquery().label("n"),
            sa.select(sa.func.coalesce(sa.func.sum(rr.c.amount_minor), 0))
            .where(recent, rr.c.kind == "wallet_pct")
            .scalar_subquery()
            .label("money"),
        )
        async with self._db.read() as conn:
            row = (await conn.execute(stmt)).one()
        days, n, money = int(row.days or 0), int(row.n or 0), int(row.money or 0)
        if not (days or money):
            return []
        out = [f"🤝 Рефералка: выдано {days} дн. ({n} {_plural(n, _AWARDS)})"] if days else []
        if money:
            out.append(f"🤝 Рефералка: на баланс {format_money(money, self._cfg_currency())}")
        return out


#: Signature of :meth:`ReferralService.attach_referrer` for the deep-link port.
AttachFn = Callable[[int, str], Awaitable[bool]]
