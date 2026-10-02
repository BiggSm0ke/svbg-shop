"""Billing test kit: real PostgreSQL, the real payment core with the stub provider of ``tests/payments`` (a
signed
webhook protocol), the real subscription lifecycle and panel writer against the fake Remnawave panel, a fake
messenger standing for Telegram, and a catalog snapshot with the owner's plan (06 M1: 179/499/899/1699 ₽ for
30/90/180/360 days, 5 devices included, +19 ₽ per extra device per 30 days, up to 15).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import svbg.billing.tables as bill_tables
import svbg.payments.tables as pay_tables
from svbg.billing import wallet
from svbg.billing.checkout import Draft, TopupResult
from svbg.billing.ports import Notice, UiRef
from svbg.billing.service import Billing
from svbg.catalog.service import CatalogSnapshot, build_snapshot
from svbg.core.clock import now
from svbg.core.crypto import Crypto, generate_key
from svbg.db import schema
from svbg.db.meta import metadata
from svbg.jobs.queue import Job, JobQueue
from svbg.jobs.worker import JobContext, PermanentJobError, RetryJob
from svbg.payments.core import VERIFY_JOB
from svbg.sdk import (
    Capabilities,
    Checkout,
    Manifest,
    MethodKind,
    PaymentIntent,
    PaymentProvider,
    Probe,
    WebhookAuth,
)
from svbg.subscriptions.hooks import EventRelay
from tests.payments.conftest import Env as PayEnv
from tests.payments.conftest import ManualPay, StubConfig, StubPay, build_env, stub_webhook
from tests.subscriptions.kit import SyncEnv, sync_env

__all__ = [
    "BillingEnv",
    "FakeCatalog",
    "FakeMessenger",
    "StarsStub",
    "attach_tables",
    "build_billing_env",
    "detach_tables",
    "owner_plan_rows",
]

BILLING_TABLES = (
    bill_tables.orders,
    bill_tables.order_items,
    bill_tables.wallet_ledger,
    bill_tables.manual_receipts,
)
PAYMENT_TABLES = (pay_tables.payment_instances, pay_tables.payments, pay_tables.payment_events)
_REGISTERED = {
    "billing": "svbg.billing.tables" in schema.TABLE_MODULES,
    "payments": "svbg.payments.tables" in schema.TABLE_MODULES,
}
WALLET_COLUMN_SQL = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_minor BIGINT NOT NULL DEFAULT 0 "
    "CONSTRAINT ck_users_wallet_minor CHECK (wallet_minor >= 0)"
)
WORKER = "billing-test"
PRICES = {30: 17_900, 90: 49_900, 180: 89_900, 360: 169_900}


def _groups() -> list[tuple[str, tuple[Any, ...]]]:
    return [("billing", BILLING_TABLES), ("payments", PAYMENT_TABLES)]


def detach_tables() -> None:
    for name, tables in _groups():
        if _REGISTERED[name]:
            continue
        for table in tables:
            if table.name in metadata.tables:
                metadata.remove(table)


def attach_tables() -> None:
    for _name, tables in _groups():
        for table in tables:
            if table.name not in metadata.tables:
                metadata._add_table(table.name, table.schema, table)


detach_tables()


# ------------------------------------------------------------------------------------------- catalog


def owner_plan_rows(
    squad: str, *, plan_id: int = 1, **overrides: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row: dict[str, Any] = {
        "id": plan_id,
        "code": f"plan{plan_id}",
        "name": {"ru": f"Тариф {plan_id}"},
        "availability": "all",
        "is_trial": False,
        "enabled": True,
        "traffic_bytes": 0,
        "reset_strategy": "NO_RESET",
        "device_limit": 5,
        "squads": [squad],
        "ext_squad": None,
        "panel_tag": None,
        "traffic_on_renew": "reset",
        "devices_on_renew": "keep",
        "device_addon": {"price_minor": 1_900, "per_days": 30, "max_devices": 15},
        "broken_reason": None,
        "sort": plan_id,
        "version": 1,
    }
    row.update(overrides)
    prices = [
        {"plan_id": plan_id, "days": d, "currency": "RUB", "amount_minor": p, "highlight": d == 30}
        for d, p in PRICES.items()
    ]
    return row, prices


@dataclass
class FakeCatalog:
    """``CatalogService`` stand-in: only ``snapshot`` (built by the real ``build_snapshot``)."""

    snapshot: CatalogSnapshot

    @classmethod
    def with_plans(cls, squad: str, plan_ids: Sequence[int] = (1, 2)) -> FakeCatalog:
        plans: list[dict[str, Any]] = []
        prices: list[dict[str, Any]] = []
        for pid in plan_ids:
            row, pr = owner_plan_rows(squad, plan_id=pid)
            plans.append(row)
            prices.extend(pr)
        return cls(build_snapshot(plans, prices, [], version=1))


# ------------------------------------------------------------------------------------------- messenger


@dataclass
class FakeMessenger:
    """Telegram as billing sees it: messages by ``(chat_id, message_id)``; deletions, failures, blocks."""

    messages: dict[tuple[int, int], Notice | str] = field(default_factory=dict)
    edits: list[tuple[UiRef, Notice]] = field(default_factory=list)
    sends: list[tuple[int, Notice]] = field(default_factory=list)
    gone: set[tuple[int, int]] = field(default_factory=set)
    blocked: set[int] = field(default_factory=set)
    fail: int = 0  # the next N calls raise a transient error
    #: Called inside ``edit`` before the message changes — «Telegram is slow»: other jobs may run meanwhile.
    during_edit: list[Callable[[UiRef, Notice], Awaitable[None]]] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1000))

    def show(self, chat_id: int, text: str = "⏳ Оформляю…", at: datetime | None = None) -> UiRef:
        """A message the user path shows (the checkout message)."""
        mid = next(self._ids)
        self.messages[(chat_id, mid)] = text
        return UiRef(chat_id, mid, at or now())

    def text(self, ref: UiRef) -> str:
        msg = self.messages[(ref.chat_id, ref.message_id)]
        return msg if isinstance(msg, str) else msg.text

    def notice(self, ref: UiRef) -> Notice | str:
        return self.messages[(ref.chat_id, ref.message_id)]

    def _maybe_fail(self) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("telegram is unreachable (test)")

    async def edit(self, ref: UiRef, notice: Notice) -> bool:
        self._maybe_fail()
        for hook in list(self.during_edit):
            await hook(ref, notice)
        key = (ref.chat_id, ref.message_id)
        if key in self.gone or key not in self.messages:
            return False
        self.messages[key] = notice
        self.edits.append((ref, notice))
        return True

    async def send(self, telegram_id: int, notice: Notice) -> UiRef | None:
        self._maybe_fail()
        if telegram_id in self.blocked:
            return None
        mid = next(self._ids)
        self.messages[(telegram_id, mid)] = notice
        self.sends.append((telegram_id, notice))
        return UiRef(telegram_id, mid, now())


# ------------------------------------------------------------------------------------------- Stars stub


class StarsStub(PaymentProvider):
    """Telegram Stars shape: XTR only, an in-chat invoice, no webhook (confirmed by
    ``successful_payment``)."""

    manifest = Manifest(
        slug="starsstub",
        title="Stars",
        method_kinds=(MethodKind.STARS,),
        currencies=("XTR",),
        config=StubConfig,
        docs_url="https://stub.example/stars",
        min_minor=1,
    )
    capabilities = Capabilities(
        webhook_auth=WebhookAuth.NONE, webhook=False, redirect=False, in_chat_invoice=True
    )

    async def create(self, intent: PaymentIntent) -> Checkout:
        return Checkout(
            kind="invoice",
            invoice={"payload": intent.payment_id, "currency": "XTR", "amount": intent.amount_minor},
        )

    async def test_credentials(self) -> Probe:
        return Probe(True)


# ------------------------------------------------------------------------------------------- environment


DEFAULT_CONFIG: dict[str, Any] = {
    "CURRENCY": "RUB",
    "WALLET_AUTOCOMPLETE_MINUTES": 60,
    "WALLET_TOPUP_MIN": 10,
    "WALLET_TOPUP_MAX": 100_000,
    "PAY_STARS_RATE": "1",
    "TIMEZONE": "Europe/Moscow",
}


@dataclass
class BillingEnv:
    s: SyncEnv
    pay: PayEnv
    billing: Billing
    catalog: FakeCatalog
    messenger: FakeMessenger
    config: dict[str, Any]
    _tg: itertools.count[int] = field(default_factory=lambda: itertools.count(700_000_001))
    _ops: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def db(self) -> Any:
        return self.s.db

    @property
    def checkout(self) -> Any:
        return self.billing.checkout

    def stub_id(self) -> int:
        return self.pay.inst("stubpay").id

    def stars_id(self) -> int:
        return self.pay.inst("starsstub").id

    def manual_id(self) -> int:
        return self.pay.inst("manualpay").id

    # ------------------------------------------------------------------------------------- seeding

    async def user(self, *, role: str = "user", perms: Sequence[str] = ()) -> int:
        tg = next(self._tg)
        return await self.pay.add_user(tg, role, perms)

    async def telegram_id(self, user_id: int) -> int:
        return int(
            (await self.db.raw("select telegram_id from users where id = $1", user_id))[0]["telegram_id"]
        )

    async def fund(self, user_id: int, amount_minor: int) -> None:
        async with self.db.tx() as conn:
            await wallet.credit(
                conn,
                user_id,
                amount_minor,
                reason="bonus",
                ref_type="test",
                ref_id=f"fund-{next(self._ops)}",
                currency="RUB",
            )

    async def draft(self, user_id: int, days: int = 30, plan_id: int = 1, **kw: Any) -> Draft:
        return await self.checkout.draft_plan(user_id, plan_id, days, **kw)

    async def topup(
        self,
        user_id: int,
        amount_minor: int,
        *,
        parent: int | None = None,
        ui_ref: UiRef | None = None,
        instance_id: int | None = None,
    ) -> TopupResult:
        return await self.checkout.start_topup(
            user_id,
            instance_id=instance_id or self.stub_id(),
            amount_minor=amount_minor,
            parent_order_id=parent,
            ui_ref=ui_ref,
        )

    async def paid_webhook(
        self,
        payment_id: str,
        *,
        amount: str | None = None,
        at: datetime | None = None,
        status: str = "paid",
        test: bool = False,
    ) -> int:
        row = await self.pay.payment(payment_id)
        text = amount if amount is not None else str(Decimal(row["amount_minor"]).scaleb(-2))
        req = stub_webhook(
            status, ext=row["external_id"], order=payment_id, amount=text, at=at or now(), test=test
        )
        return await self.pay.send(req)

    # ------------------------------------------------------------------------------------- reading

    async def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in await self.db.raw(sql, *args)]

    async def order(self, order_id: int) -> dict[str, Any]:
        return (await self.rows("select * from orders where id = $1", order_id))[0]

    async def balance(self, user_id: int) -> int:
        return int(
            (await self.db.raw("select wallet_minor from users where id = $1", user_id))[0]["wallet_minor"]
        )

    async def ledger(self, user_id: int) -> list[dict[str, Any]]:
        return await self.rows("select * from wallet_ledger where user_id = $1 order by id", user_id)

    async def jobs(self, kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "select * from jobs where true"
        args: list[Any] = []
        if kind is not None:
            args.append(kind)
            sql += f" and kind = ${len(args)}"
        if status is not None:
            args.append(status)
            sql += f" and status = ${len(args)}"
        return await self.rows(sql + " order by id", *args)

    async def assert_wallet_invariants(self) -> None:
        """Σ ledger = balance, running sums = balance_after, nothing below zero — for every user."""
        async with self.db.read() as conn:
            assert await wallet.mismatches(conn) == []
        bad = await self.rows(
            "select id, user_id, balance_after, running from ("
            " select id, user_id, balance_after,"
            " sum(amount_minor) over (partition by user_id order by id) as running from wallet_ledger) t"
            " where running <> balance_after or balance_after < 0"
        )
        assert bad == [], bad
        neg = await self.rows("select id from users where wallet_minor < 0")
        assert neg == []

    # ------------------------------------------------------------------------------------- jobs

    def handlers(self) -> dict[str, Any]:
        return {
            **self.s.writer.handlers(),
            **EventRelay(self.s.bus).handlers(),
            **self.billing.handlers(),
            VERIFY_JOB: self.pay.core.verify_job,
        }

    async def drain(
        self, *, rounds: int = 60, make_due: bool = False, kinds: Sequence[str] | None = None
    ) -> list[tuple[Job, str]]:
        """Run due jobs like the worker (sequential, deterministic). A retried job is pushed an hour away."""
        queue = JobQueue(self.db)
        ctx = JobContext(db=self.db, queue=queue, worker_id=WORKER)
        handlers = self.handlers()
        outcomes: list[tuple[Job, str]] = []
        if make_due:
            await self.make_due()
        for _ in range(rounds):
            claimed = await queue.claim("interactive", WORKER, 20) + await queue.claim(
                "background", WORKER, 20
            )
            if not claimed:
                break
            for job in claimed:
                fence = {"worker_id": WORKER, "attempt": job.attempts}
                if kinds is not None and job.kind not in kinds:
                    await queue.release(job.id, **fence)
                    continue
                try:
                    await handlers[job.kind](job, ctx)
                except RetryJob as r:
                    await queue.fail(job.id, str(r), retry_in=max(r.delay, 3600), **fence)
                    outcomes.append((job, "retry"))
                except PermanentJobError as e:
                    await queue.fail(job.id, str(e), permanent=True, **fence)
                    outcomes.append((job, "dead"))
                except Exception as e:
                    await queue.fail(job.id, f"{type(e).__name__}: {e}", retry_in=3600, **fence)
                    outcomes.append((job, "failed"))
                else:
                    await queue.complete(job.id, **fence)
                    outcomes.append((job, "done"))
            if kinds is not None and all(j.kind not in kinds for j in claimed):
                break
        return outcomes

    async def make_due(self) -> None:
        await self.db.raw("update jobs set next_run_at = now() where status = 'ready'")


@asynccontextmanager
async def build_billing_env(
    pg_dsn: str,
    *,
    config: Mapping[str, Any] | None = None,
    has_domain: bool = True,
    test_instances: Sequence[str] = (),
) -> AsyncIterator[BillingEnv]:
    async with sync_env(pg_dsn) as senv:
        await senv.db.raw(WALLET_COLUMN_SQL)
        crypto = Crypto([generate_key()])
        pay = await build_env(
            senv.db,
            crypto,
            has_domain=has_domain,
            providers=(StubPay, ManualPay, StarsStub),
            test_instances=test_instances,
            register_on_paid=False,
        )
        cfg = {**DEFAULT_CONFIG, **dict(config or {})}
        catalog = FakeCatalog.with_plans(senv.squad)
        messenger = FakeMessenger()
        billing = Billing(
            senv.db,
            catalog=catalog,
            payments=pay.core,
            config=lambda: cfg,
            messenger=messenger,
            attention=senv.attention,
        )
        billing.attach()
        env = BillingEnv(senv, pay, billing, catalog, messenger, cfg)
        try:
            yield env
        finally:
            await pay.core.drain()


def notice_json(n: Notice | str) -> str:
    """A compact view of a notice for assertion messages."""
    if isinstance(n, str):
        return n
    return json.dumps({"screen": n.screen, "text": n.text}, ensure_ascii=False)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]
