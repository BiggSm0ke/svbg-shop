"""Payment core: invoice creation, the CAS state machine, amount reconciliation, hooks in the same
transaction, trusted sources, manual confirmation with role checks (07 §4, 04 D12, §9.1)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.core.crypto import Crypto
from svbg.jobs.queue import Job
from svbg.jobs.worker import PermanentJobError, RetryJob
from svbg.payments.core import (
    VERIFY_JOB,
    CheckoutError,
    Outcome,
    PaymentRecord,
    PermissionDeniedError,
    SpendDeniedError,
    to_minor,
)
from svbg.payments.registry import InstanceSpec
from svbg.payments.tables import payments
from svbg.sdk import PaymentState, ProviderStatus
from tests.dbkit import CountingDatabase
from tests.payments.conftest import STUB_CONFIG, Env, build_env, stub_webhook

# ------------------------------------------------------------------------------------------ amounts


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("179", "RUB", 17_900),
        ("179.00", "RUB", 17_900),
        ("179.0", "RUB", 17_900),
        ("179.5", "RUB", 17_950),
        ("179.001", "RUB", None),
        ("100", "XTR", 100),
        ("100.5", "XTR", None),
        ("-1", "RUB", None),
        ("1", "ZZZ", None),
    ],
    ids=[
        "int",
        "two-decimals",
        "one-decimal",
        "half",
        "sub-kopeck",
        "stars",
        "half-star",
        "negative",
        "unknown",
    ],
)
def test_to_minor_is_exact(amount: str, currency: str, expected: int | None) -> None:
    assert to_minor(Decimal(amount), currency) == expected


# ------------------------------------------------------------------------------------------ creation


async def test_create_payment_stores_invoice_and_schedules_safety_checks(env: Env) -> None:
    before = datetime.now(UTC)
    result = await env.core.create_payment(
        user_id=env.user_id,
        instance_id=env.inst().id,
        amount_minor=17_900,
        currency="rub",
        description="Пополнение баланса",
    )
    row = await env.payment(result.payment_id)
    assert row["status"] == "pending"
    assert row["external_id"] == result.checkout.external_id == "inv-1"
    assert row["checkout"]["pay_url"] == "https://stub.example/pay/inv-1"
    assert row["currency"] == "RUB" and row["method_kind"] == "sbp"
    assert row["poll_plan"] == "domain"
    assert timedelta(seconds=590) < row["next_check_at"] - before < timedelta(seconds=620)
    sent = env.server.created[0]
    # Only the opaque payment id and a hash leave the bot: no Telegram id, no internal user id.
    assert sent["order"] == result.payment_id
    assert sent["amount"] == "179.00"
    assert str(555_000_001) not in json.dumps(sent) and sent["customer"] != str(env.user_id)
    assert sent["customer"] == env.inst().customer_ref(env.user_id)


async def test_create_without_domain_uses_the_presence_plan(db: CountingDatabase, crypto: Crypto) -> None:
    env = await build_env(db, crypto, has_domain=False)
    touched: list[tuple[str, int, int, str | None]] = []

    class Presence:
        def touch(
            self, payment_id: str, user_id: int, instance_id: int, external_id: str | None = None
        ) -> None:
            touched.append((payment_id, user_id, instance_id, external_id))

        def forget(self, payment_id: str) -> None:
            pass

    env.core.presence = Presence()
    pid = await env.pending()
    row = await env.payment(pid)
    assert row["poll_plan"] == "nodomain"
    assert touched == [(pid, env.user_id, env.inst().id, "inv-1")]


async def test_manual_checkout_is_never_polled(env: Env) -> None:
    result = await env.core.create_payment(
        user_id=env.user_id,
        instance_id=env.inst("manualpay").id,
        amount_minor=50_000,
        currency="RUB",
        description="Перевод",
    )
    row = await env.payment(result.payment_id)
    assert result.checkout.kind == "details" and "2200" in (result.checkout.details or "")
    assert row["poll_plan"] is None and row["next_check_at"] is None


@pytest.mark.parametrize(
    ("amount", "currency", "message"),
    [(5_000, "RUB", "Минимальная сумма"), (17_900, "USD", "не принимает USD")],
    ids=["below-minimum", "currency"],
)
async def test_create_rejects_out_of_limits(env: Env, amount: int, currency: str, message: str) -> None:
    with pytest.raises(CheckoutError) as err:
        await env.core.create_payment(
            user_id=env.user_id,
            instance_id=env.inst().id,
            amount_minor=amount,
            currency=currency,
            description="x",
        )
    assert message in err.value.human
    assert await env.count("payments") == 0


async def test_disabled_instance_cannot_create(env: Env) -> None:
    await env.registry.set_enabled("stubpay", False)
    with pytest.raises(CheckoutError, match="недоступен"):
        await env.pending()


async def test_spend_guard_refuses_before_any_row(env: Env) -> None:
    calls: list[int] = []

    async def frozen(_conn: Any, user_id: int) -> str | None:
        calls.append(user_id)
        return "Подписка заморожена — оплата недоступна"

    env.core.spend_guard(frozen)
    with pytest.raises(SpendDeniedError) as err:
        await env.pending()
    assert err.value.human.startswith("Подписка заморожена")
    assert calls == [env.user_id]
    assert await env.count("payments") == 0
    assert env.server.created == []  # the provider was never called


@pytest.mark.parametrize("failure", ["http-500", "network", "timeout"])
async def test_create_failure_marks_failed_and_late_payment_still_wins(env: Env, failure: str) -> None:
    if failure == "http-500":
        env.server.fail_create = 500
    elif failure == "network":
        env.server.raise_network = True
    else:
        env.core._create_timeout = 0.05
        env.server.delay = 0.5
    with pytest.raises(CheckoutError) as err:
        await env.pending()
    assert err.value.human.startswith("Не получилось создать счёт")
    rows = await env.rows("select * from payments")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["error"]
    pid = rows[0]["id"]
    # The provider created the invoice after all and the user paid: the money is not lost.
    env.server.raise_network = False
    code = await env.send(stub_webhook("paid", ext="inv-late", order=pid))
    assert code == 200
    assert (await env.payment(pid))["status"] == "paid"
    assert [p.id for p in env.credited] == [pid]


# ------------------------------------------------------------------------------------------ state machine


async def test_paid_webhook_credits_once_in_the_same_transaction(env: Env) -> None:
    pid = await env.pending()
    seen_in_tx: list[str] = []

    async def check_status_inside(conn: Any, payment: PaymentRecord) -> None:
        status = (
            await conn.execute(sa.select(payments.c.status).where(payments.c.id == payment.id))
        ).scalar()
        seen_in_tx.append(status)

    env.core.on_paid(check_status_inside)
    req = stub_webhook("paid", ext="inv-1", order=pid)
    assert await env.send(req) == 200
    assert await env.send(req) == 200  # the provider retries the same body
    row = await env.payment(pid)
    assert row["status"] == "paid" and row["paid_amount_minor"] == 17_900 and row["paid_at"] is not None
    assert len(env.credited) == 1 and env.credited[0].status == "paid"
    assert seen_in_tx == ["paid"]  # the hook ran in the transaction that changed the status
    outcomes = [r["outcome"] for r in await env.db.raw("select outcome from payment_events order by id")]
    assert outcomes == ["applied"]  # the duplicate body is not stored twice


async def test_matching_by_external_id_only(env: Env) -> None:
    pid = await env.pending()
    assert await env.send(stub_webhook("paid", ext="inv-1")) == 200
    assert (await env.payment(pid))["status"] == "paid"


@pytest.mark.parametrize("first", ["expired", "canceled"])
async def test_late_payment_wins(env: Env, first: str) -> None:
    pid = await env.pending()
    await env.send(stub_webhook(first, ext="inv-1", order=pid))
    assert (await env.payment(pid))["status"] == first
    await env.send(stub_webhook("paid", ext="inv-1", order=pid, extra={"n": 2}))
    assert (await env.payment(pid))["status"] == "paid"
    assert len(env.credited) == 1


async def test_expiry_never_downgrades_a_paid_payment(env: Env) -> None:
    pid = await env.pending()
    await env.send(stub_webhook("paid", ext="inv-1", order=pid))
    await env.send(stub_webhook("expired", ext="inv-1", order=pid))
    await env.send(stub_webhook("processing", ext="inv-1", order=pid))
    assert (await env.payment(pid))["status"] == "paid"


@pytest.mark.parametrize(
    ("amount", "currency", "reason_part"),
    [
        ("170", "RUB", "пришло 170"),
        ("179", "USD", "пришло"),
        ("179.001", "RUB", "сумма 179.001"),
        (None, "RUB", ""),
    ],
    ids=["less", "currency", "sub-kopeck", "missing"],
)
async def test_amount_or_currency_mismatch_credits_nothing_and_alerts(
    env: Env, amount: str | None, currency: str, reason_part: str
) -> None:
    pid = await env.pending()
    code = await env.send(stub_webhook("paid", ext="inv-1", order=pid, amount=amount, currency=currency))
    await env.core.drain()
    row = await env.payment(pid)
    if amount is None:
        # A strong provider with fetch_status: the amount is read back from the provider by a job.
        assert code == 200 and row["status"] == "pending"
        jobs = await env.rows("select kind, payload from jobs")
        assert [j["kind"] for j in jobs] == [VERIFY_JOB]
        return
    assert row["status"] == "mismatch" and reason_part in row["error"]
    assert env.credited == []
    assert f"payments:mismatch:{pid}" in env.attention.dedup_keys()
    assert env.admin.posts and env.admin.posts[0][0] == "payments" and "не совпала" in env.admin.posts[0][1]
    # A later correct report does not resurrect it: a human decides.
    await env.send(stub_webhook("paid", ext="inv-1", order=pid, extra={"retry": 1}))
    assert (await env.payment(pid))["status"] == "mismatch" and env.credited == []


async def test_chargeback_marks_refunded_runs_hook_and_alerts(env: Env) -> None:
    pid = await env.pending()
    await env.send(stub_webhook("chargeback", ext="inv-1", order=pid))
    assert (await env.payment(pid))["status"] == "pending"  # nothing to take back yet
    await env.send(stub_webhook("paid", ext="inv-1", order=pid))
    await env.send(stub_webhook("chargeback", ext="inv-1", order=pid, extra={"n": 2}))
    await env.core.drain()
    assert (await env.payment(pid))["status"] == "refunded"
    assert [p.id for p in env.refunded] == [pid]
    assert f"payments:refund:{pid}" in env.attention.dedup_keys()


async def test_unknown_payment_is_acknowledged_and_alerted(env: Env) -> None:
    assert await env.send(stub_webhook("paid", ext="nobody")) == 200
    await env.core.drain()
    assert env.credited == []
    assert any(k.startswith("payments:unknown:") for k in env.attention.dedup_keys())
    events = await env.rows("select outcome, accepted from payment_events")
    assert events == [{"outcome": "unknown_payment", "accepted": True}]


async def test_foreign_external_id_cannot_hijack_a_payment(env: Env) -> None:
    pid = await env.pending()  # bound to inv-1
    await env.send(stub_webhook("paid", ext="inv-999", order=pid))
    assert (await env.payment(pid))["status"] == "pending" and env.credited == []


async def test_failing_hook_rolls_everything_back_and_provider_retries(env: Env) -> None:
    pid = await env.pending()
    attempts = 0

    async def flaky(_conn: Any, _payment: PaymentRecord) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("wallet is down")

    env.core.on_paid(flaky)
    req = stub_webhook("paid", ext="inv-1", order=pid)
    assert await env.send(req) == 503
    assert (await env.payment(pid))["status"] == "pending"
    assert await env.count("payment_events") == 0  # dedup row rolled back
    assert await env.send(req) == 200  # the provider's retry of the very same body is processed
    assert (await env.payment(pid))["status"] == "paid"
    assert len(env.credited) == 2 and env.credited[0].id == env.credited[1].id  # first attempt rolled back


async def test_without_on_paid_hook_nothing_is_marked_paid(db: CountingDatabase, crypto: Crypto) -> None:
    env = await build_env(db, crypto, register_on_paid=False)
    pid = await env.pending()
    assert await env.send(stub_webhook("paid", ext="inv-1", order=pid)) == 503
    assert (await env.payment(pid))["status"] == "pending"


async def test_test_event_on_live_instance_is_rejected(env: Env) -> None:
    pid = await env.pending()
    assert await env.send(stub_webhook("paid", ext="inv-1", order=pid, test=True)) == 400
    assert (await env.payment(pid))["status"] == "pending"
    events = await env.rows("select outcome, accepted, summary from payment_events")
    assert events[0]["outcome"] == "test_rejected" and events[0]["accepted"] is False


async def test_test_instance_accepts_test_events(db: CountingDatabase, crypto: Crypto) -> None:
    env = await build_env(db, crypto, test_instances=("stubpay",))
    pid = await env.pending()
    assert (await env.payment(pid))["is_test"] is True
    assert env.server.created[0]["test"] is True
    assert await env.send(stub_webhook("paid", ext="inv-1", order=pid, test=True)) == 200
    assert (await env.payment(pid))["status"] == "paid"


async def test_test_payment_is_never_credited_after_test_mode_is_switched_off(
    db: CountingDatabase, crypto: Crypto
) -> None:
    env = await build_env(db, crypto, test_instances=("stubpay",))
    pid = await env.pending()
    assert (await env.payment(pid))["is_test"] is True
    spec = InstanceSpec(slug="stubpay", provider="stubpay", enabled=True, config=STUB_CONFIG)
    await env.registry.save(spec)  # test mode switched off
    live = env.inst()
    assert not live.is_test
    # The provider's status answer does not say «test» (as RollyPay's did not): the row does.
    paid = ProviderStatus(state=PaymentState.PAID, external_id="inv-1", amount=Decimal("179"), currency="RUB")
    [res] = await env.core.apply_statuses(live, [paid], source="poll")
    assert res.outcome is Outcome.TEST_REJECTED and env.credited == []
    expired = ProviderStatus(state=PaymentState.EXPIRED, external_id="inv-1")
    [res] = await env.core.apply_statuses(live, [expired], source="poll")
    assert res.outcome is Outcome.IGNORED
    wrong = ProviderStatus(state=PaymentState.PAID, external_id="inv-1", amount=Decimal("1"), currency="RUB")
    [res] = await env.core.apply_statuses(live, [wrong], source="poll")
    assert res.outcome is Outcome.TEST_REJECTED
    assert (await env.payment(pid))["status"] == "pending"
    await env.core.drain()
    assert env.attention.raised == []


async def test_lower_case_currency_is_the_same_money(env: Env) -> None:
    pid = await env.pending()
    assert await env.send(stub_webhook("paid", ext="inv-1", order=pid, currency="rub")) == 200
    row = await env.payment(pid)
    assert row["status"] == "paid" and row["paid_currency"] == "RUB" and len(env.credited) == 1


async def test_provider_payment_time_is_kept_within_bounds(env: Env) -> None:
    """The auto-complete window is judged by when the user paid, not by when a check got here."""
    p1, p2, p3 = await env.pending(), await env.pending(), await env.pending()
    await env.db.raw("update payments set created_at = created_at - interval '2 hours'")
    inst = env.inst()

    def paid(ext: str, at: datetime) -> ProviderStatus:
        return ProviderStatus(
            state=PaymentState.PAID, external_id=ext, amount=Decimal("179"), currency="RUB", paid_at=at
        )

    at = datetime.now(UTC) - timedelta(minutes=50)  # paid 50 minutes ago, checked only now
    future = datetime.now(UTC) + timedelta(hours=1)
    before_invoice = (await env.payment(p3))["created_at"] - timedelta(days=1)
    await env.core.apply_statuses(
        inst, [paid("inv-1", at), paid("inv-2", future), paid("inv-3", before_invoice)], source="poll"
    )
    assert (await env.payment(p1))["paid_at"] == at
    assert (await env.payment(p2))["paid_at"] <= datetime.now(UTC)  # never in the future
    row3 = await env.payment(p3)
    assert row3["paid_at"] == row3["created_at"]  # never before the invoice
    assert env.credited[0].paid_at == at  # the hook sees it (billing's window check)


async def test_poll_expiry_without_domain_schedules_one_late_recheck(
    db: CountingDatabase, crypto: Crypto
) -> None:
    env = await build_env(db, crypto, has_domain=False)
    pid = await env.pending()
    expired = ProviderStatus(state=PaymentState.EXPIRED, external_id="inv-1")
    await env.core.apply_statuses(env.inst(), [expired], source="poll")
    await env.core.apply_statuses(env.inst(), [expired], source="fetch")  # already expired: no chain
    jobs = await env.rows("select kind, payload, dedup_key, next_run_at from jobs")
    assert len(jobs) == 1 and jobs[0]["kind"] == VERIFY_JOB
    assert jobs[0]["dedup_key"] == f"{VERIFY_JOB}:late:{pid}"
    assert jobs[0]["next_run_at"] > datetime.now(UTC) + timedelta(hours=5)
    env.server.set("inv-1", "paid", "179.00")
    payload = jobs[0]["payload"]
    await env.core.verify_job(_job(payload if isinstance(payload, dict) else json.loads(payload)), None)  # type: ignore[arg-type]
    assert (await env.payment(pid))["status"] == "paid" and len(env.credited) == 1


async def test_webhook_expiry_schedules_no_late_recheck(env: Env) -> None:
    pid = await env.pending()
    await env.send(stub_webhook("expired", ext="inv-1", order=pid))
    assert await env.count("jobs") == 0  # with a domain the webhook brings a late payment


async def test_stale_and_future_webhooks_are_rejected_and_logged(env: Env) -> None:
    pid = await env.pending()
    now = datetime.now(UTC)
    for at in (now - timedelta(seconds=301), now + timedelta(seconds=400)):
        assert await env.send(stub_webhook("paid", ext="inv-1", order=pid, at=at)) == 401
    assert (await env.payment(pid))["status"] == "pending"
    events = await env.rows("select outcome, accepted, reason from payment_events")
    assert [e["outcome"] for e in events] == ["stale", "stale"]
    # The provider retries with a fresh signature of the same body: accepted (rejections do not dedup).
    assert await env.send(stub_webhook("paid", ext="inv-1", order=pid)) == 200
    assert (await env.payment(pid))["status"] == "paid"


async def test_many_stale_webhooks_raise_one_ntp_alert(env: Env) -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    for n in range(8):
        await env.send(stub_webhook("paid", ext=f"x{n}", at=old))
    await env.core.drain()
    assert env.attention.dedup_keys().count("payments:clock_skew") == 1
    assert "NTP" in env.attention.raised[0][2]


async def test_bad_signature_and_wrong_token(env: Env) -> None:
    pid = await env.pending()
    inst = env.inst()
    bad = stub_webhook("paid", ext="inv-1", order=pid, secret_value="wrong")
    assert await env.send(bad) == 401
    events = await env.rows("select outcome, summary from payment_events")
    assert events == [{"outcome": "bad_signature", "summary": {}}]  # an unauthenticated body is not stored
    good = stub_webhook("paid", ext="inv-1", order=pid)
    assert (await env.core.handle_webhook(inst.id, inst.webhook_token[:-1] + "x", good)).status == 404
    assert (await env.core.handle_webhook(inst.id + 1000, inst.webhook_token, good)).status == 404
    manual = env.inst("manualpay")  # no webhooks at all
    assert (await env.core.handle_webhook(manual.id, manual.webhook_token, good)).status == 404
    assert (await env.payment(pid))["status"] == "pending"


async def test_ping_is_answered_without_state(env: Env) -> None:
    assert await env.send(stub_webhook("paid", extra={"type": "ping"})) == 200
    assert await env.count("payment_events") == 0


async def test_paid_event_publishes_after_commit(env: Env) -> None:
    from svbg.core.bus import Event, EventBus

    bus = EventBus()
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe("payment.paid", handler)
    env.core._bus = bus
    pid = await env.pending()
    await env.send(stub_webhook("paid", ext="inv-1", order=pid))
    await bus.drain()
    assert [e.payload["payment_id"] for e in seen] == [pid]
    assert seen[0].payload["amount_minor"] == 17_900


# ------------------------------------------------------------------------------------------ weak schemes


async def test_weak_webhook_never_changes_state_by_itself(env: Env) -> None:
    pid = await env.pending("weakpay")
    req_body = json.dumps({"id": "inv-1", "status": "paid", "amount": "179", "currency": "RUB"}).encode()
    from svbg.sdk import WebhookRequest
    from tests.payments.conftest import SECRET

    req = WebhookRequest(body=req_body, headers={"X-Secret": SECRET})
    assert await env.send(req, "weakpay") == 200
    assert (await env.payment(pid))["status"] == "pending"  # a forged "paid" would stop here
    jobs = await env.rows("select kind, payload, dedup_key, lane from jobs")
    assert len(jobs) == 1 and jobs[0]["kind"] == VERIFY_JOB and jobs[0]["lane"] == "interactive"
    # The job re-reads the status from the provider: still pending there → still pending here.
    job = _job(jobs[0]["payload"])
    await env.core.verify_job(job, None)  # type: ignore[arg-type]
    assert (await env.payment(pid))["status"] == "pending"
    env.server.set("inv-1", "paid", "179.00")
    await env.core.verify_job(job, None)  # type: ignore[arg-type]
    assert (await env.payment(pid))["status"] == "paid" and len(env.credited) == 1


def _job(payload: dict[str, Any]) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=1,
        queue="default",
        lane="interactive",
        kind=VERIFY_JOB,
        payload=payload,
        attempts=1,
        max_attempts=8,
        ordering_key=None,
        dedup_key=None,
        caused_by=None,
        locked_by=None,
        locked_until=None,
        next_run_at=now,
        created_at=now,
    )


async def test_verify_job_failures(env: Env) -> None:
    pid = await env.pending("weakpay")
    env.server.fail_status = True
    with pytest.raises(RetryJob):
        await env.core.verify_job(_job({"instance_id": env.inst("weakpay").id, "payment_id": pid}), None)  # type: ignore[arg-type]
    with pytest.raises(PermanentJobError):
        await env.core.verify_job(_job({"instance_id": 99_999}), None)  # type: ignore[arg-type]
    with pytest.raises(PermanentJobError):
        await env.core.verify_job(_job({}), None)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------ concurrency


async def test_concurrent_different_webhooks_credit_exactly_once(env: Env) -> None:
    pid = await env.pending()
    reqs = [stub_webhook("paid", ext="inv-1", order=pid, extra={"attempt": n}) for n in range(8)]
    codes = await asyncio.gather(*(env.send(r) for r in reqs))
    assert set(codes) == {200}
    assert len(env.credited) == 1 and (await env.payment(pid))["status"] == "paid"


async def test_property_random_event_orders_keep_money_invariants(env: Env) -> None:
    """Deterministic pseudo-random histories of webhooks and polls (fixed seeds): whatever the order, each
    payment is credited at most once, exactly once iff it ended ``paid`` or ``refunded`` after a paid, never
    with a wrong amount, and ``refunded`` only after ``paid``."""
    import random

    inst = env.inst()
    states = ["created", "processing", "paid", "expired", "canceled", "chargeback", "paid-wrong", "paid"]
    for seed in range(12):
        rng = random.Random(seed)
        pids = [await env.pending() for _ in range(3)]
        rows = [await env.payment(p) for p in pids]
        exts = [r["external_id"] for r in rows]
        ops: list[Any] = []
        for _ in range(16):
            k = rng.randrange(3)
            state = rng.choice(states)
            amount = "170" if state == "paid-wrong" else "179.00" if rng.random() < 0.5 else "179"
            state = "paid" if state == "paid-wrong" else state
            if rng.random() < 0.3:
                status = ProviderStatus(
                    state=PaymentState(state), external_id=exts[k], amount=Decimal(amount), currency="RUB"
                )
                ops.append(env.core.apply_statuses(inst, [status], source="poll"))
            else:
                ops.append(
                    env.send(
                        stub_webhook(
                            state, ext=exts[k], order=pids[k], amount=amount, extra={"r": rng.random()}
                        )
                    )
                )
        await asyncio.gather(*ops)
        for pid in pids:
            row = await env.payment(pid)
            credits = [c for c in env.credited if c.id == pid]
            assert len(credits) <= 1, (seed, pid)
            if row["status"] in ("paid", "refunded"):
                assert len(credits) == 1 and credits[0].paid_amount_minor == 17_900
            else:
                assert credits == []
            if row["status"] == "refunded":
                assert any(r.id == pid for r in env.refunded)
    await env.core.drain()


# ------------------------------------------------------------------------------------------ trusted sources


async def test_credit_external_legacy_is_idempotent(env: Env) -> None:
    inst = env.inst("manualpay")
    first = await env.core.credit_external(
        inst.id,
        user_id=env.user_id,
        external_id="charge-1",
        amount_minor=10_000,
        currency="RUB",
        metadata={"legacy_payload": "balance_1_10000"},
    )
    again = await env.core.credit_external(
        inst.id, user_id=env.user_id, external_id="charge-1", amount_minor=10_000, currency="RUB"
    )
    assert first.credited and first.outcome is Outcome.APPLIED
    assert again.outcome is Outcome.DUPLICATE and not again.credited
    assert len(env.credited) == 1 and env.credited[0].metadata == {"legacy_payload": "balance_1_10000"}


async def test_credit_external_with_our_invoice(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    inst = env.inst("manualpay")
    res = await env.core.credit_external(
        inst.id,
        user_id=env.user_id,
        external_id="tg-charge",
        amount_minor=50_000,
        currency="RUB",
        payment_id=pid,
    )
    assert res.credited and (await env.payment(pid))["external_id"] == "tg-charge"
    res2 = await env.core.credit_external(
        inst.id,
        user_id=env.user_id,
        external_id="tg-charge",
        amount_minor=50_000,
        currency="RUB",
        payment_id=pid,
    )
    assert not res2.credited and len(env.credited) == 1


# ------------------------------------------------------------------------------------------ manual payments


async def test_manual_confirmation_requires_the_permission(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    await env.add_user(901, "user")
    await env.add_user(902, "support", ["payments.confirm"])  # support never confirms money
    await env.add_user(903, "admin", [])
    for tg in (901, 902, 903, 904):  # 904: a group member unknown to the bot
        with pytest.raises(PermissionDeniedError) as err:
            await env.core.confirm_manual(
                pid, actor_telegram_id=tg, owner_ids=set(), paid_amount_minor=50_000
            )
        assert err.value.human == "Нет прав"
    assert (await env.payment(pid))["status"] == "pending" and env.credited == []
    audit = await env.rows("select action, target from admin_audit order by id")
    assert [a["action"] for a in audit] == ["payments.confirm.denied"] * 4
    assert audit[0]["target"] == f"payment:{pid}"


async def test_manual_confirmation_by_admin_is_audited_and_cas_protected(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    admin_a = await env.add_user(911, "admin", ["payments.confirm"])
    await env.add_user(912, "admin", ["*"])
    res = await env.core.confirm_manual(
        pid, actor_telegram_id=911, owner_ids=set(), paid_amount_minor=50_000, reason="чек №15"
    )
    assert res.credited and res.payment is not None and res.payment.confirmed_by == admin_a
    second = await env.core.confirm_manual(
        pid, actor_telegram_id=912, owner_ids=set(), paid_amount_minor=50_000
    )
    assert not second.credited and len(env.credited) == 1
    audit = await env.rows("select actor_id, role, action, amount_minor, reason from admin_audit order by id")
    assert audit[0] == {
        "actor_id": admin_a,
        "role": "admin",
        "action": "payments.confirm",
        "amount_minor": 50_000,
        "reason": "чек №15",
    }
    assert len(audit) == 2


async def test_banned_admin_cannot_confirm_or_reject(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    await env.add_user(931, "admin", ["payments.confirm"])
    await env.db.raw("update users set banned_at = now() where telegram_id = 931")  # banned, role kept
    with pytest.raises(PermissionDeniedError):
        await env.core.confirm_manual(pid, actor_telegram_id=931, owner_ids=set(), paid_amount_minor=50_000)
    with pytest.raises(PermissionDeniedError):
        await env.core.reject_manual(pid, actor_telegram_id=931, owner_ids=set(), reason="нет чека")
    assert (await env.payment(pid))["status"] == "pending" and env.credited == []
    audit = await env.rows("select action from admin_audit order by id")
    assert [a["action"] for a in audit] == ["payments.confirm.denied", "payments.reject.denied"]


async def test_admin_cannot_decide_their_own_manual_payment(env: Env) -> None:
    admin_id = await env.add_user(941, "admin", ["*"])
    own = await env.pending("manualpay", amount_minor=50_000, user_id=admin_id)
    with pytest.raises(PermissionDeniedError):
        await env.core.confirm_manual(own, actor_telegram_id=941, owner_ids=set(), paid_amount_minor=50_000)
    with pytest.raises(PermissionDeniedError):
        await env.core.reject_manual(own, actor_telegram_id=941, owner_ids=set(), reason="передумал")
    assert (await env.payment(own))["status"] == "pending" and env.credited == []
    audit = await env.rows("select actor_id, action, target from admin_audit order by id")
    assert [(a["actor_id"], a["action"]) for a in audit] == [
        (admin_id, "payments.confirm.self"),
        (admin_id, "payments.reject.self"),
    ]
    # Another admin may; the owner may even for their own payment.
    await env.add_user(942, "admin", ["payments.confirm"])
    res = await env.core.confirm_manual(own, actor_telegram_id=942, owner_ids=set(), paid_amount_minor=50_000)
    assert res.credited
    owner_id = await env.add_user(943, "user")
    mine = await env.pending("manualpay", amount_minor=50_000, user_id=owner_id)
    res = await env.core.confirm_manual(
        mine, actor_telegram_id=943, owner_ids={943}, paid_amount_minor=50_000
    )
    assert res.credited and res.payment is not None and res.payment.confirmed_by == owner_id


async def test_manual_confirmation_amount_difference_is_mismatch(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    res = await env.core.confirm_manual(pid, actor_telegram_id=42, owner_ids={42}, paid_amount_minor=45_000)
    assert res.outcome is Outcome.MISMATCH and env.credited == []
    assert (await env.payment(pid))["status"] == "mismatch"


async def test_manual_confirmation_only_for_manual_instances(env: Env) -> None:
    pid = await env.pending()
    with pytest.raises(CheckoutError, match="нельзя подтвердить"):
        await env.core.confirm_manual(pid, actor_telegram_id=42, owner_ids={42}, paid_amount_minor=17_900)
    with pytest.raises(CheckoutError, match="не найден"):
        await env.core.confirm_manual("missing", actor_telegram_id=42, owner_ids={42}, paid_amount_minor=1)


async def test_reject_manual(env: Env) -> None:
    pid = await env.pending("manualpay", amount_minor=50_000)
    await env.add_user(921, "user")
    with pytest.raises(PermissionDeniedError):
        await env.core.reject_manual(pid, actor_telegram_id=921, owner_ids=set(), reason="нет чека")
    with pytest.raises(ValueError, match="reason"):
        await env.core.reject_manual(pid, actor_telegram_id=42, owner_ids={42}, reason=" ")
    assert await env.core.reject_manual(pid, actor_telegram_id=42, owner_ids={42}, reason="чек не найден")
    assert (await env.payment(pid))["status"] == "canceled"
    # The receipt turns up later: a confirmation still wins.
    res = await env.core.confirm_manual(pid, actor_telegram_id=42, owner_ids={42}, paid_amount_minor=50_000)
    assert res.credited


# ------------------------------------------------------------------------------------------ housekeeping


async def test_purge_events_respects_ttl(env: Env) -> None:
    inst = env.inst()
    for days in (100, 91, 10):
        await env.db.raw(
            "insert into payment_events (instance_id, body_sha256, outcome, accepted, received_at) "
            "values ($1, $2, 'applied', true, now() - make_interval(days => $3))",
            inst.id,
            f"{days:064d}",
            days,
        )
    assert await env.core.purge_events() == 2
    left = await env.rows("select body_sha256 from payment_events")
    assert left == [{"body_sha256": f"{10:064d}"}]


async def test_get_returns_record(env: Env) -> None:
    pid = await env.pending()
    rec = await env.core.get(pid)
    assert rec is not None and rec.status == "pending" and rec.amount_minor == 17_900
    assert await env.core.get("nope") is None


async def test_webhook_sql_budget(env: Env) -> None:
    pid = await env.pending()
    mark = env.db.queries
    await env.send(stub_webhook("paid", ext="inv-1", order=pid))
    # dedup insert + CAS update (+ the test hook's nothing); no extra lookups on the happy path
    assert env.db.queries - mark <= 2, env.db.counter.since(mark) if env.db.counter else None
