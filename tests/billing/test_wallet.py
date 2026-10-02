from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from svbg.billing import wallet
from svbg.domain.wallet_rules import WalletRuleError
from tests.billing.kit import BillingEnv


async def test_credit_is_idempotent_by_key(env: BillingEnv) -> None:
    uid = await env.user()
    async with env.db.tx() as conn:
        first = await wallet.credit(
            conn, uid, 500, reason="bonus", ref_type="campaign", ref_id="c1", currency="RUB"
        )
        again = await wallet.credit(
            conn, uid, 500, reason="bonus", ref_type="campaign", ref_id="c1", currency="RUB"
        )
        other = await wallet.credit(
            conn, uid, 300, reason="bonus", ref_type="campaign", ref_id="c2", currency="RUB"
        )
    assert first is not None and first.balance_after == 500
    assert again is None
    assert other is not None and other.balance_after == 800
    assert await env.balance(uid) == 800
    await env.assert_wallet_invariants()


async def test_debit_is_a_cas_and_never_goes_negative(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 1_000)
    async with env.db.tx() as conn:
        assert (
            await wallet.debit(
                conn, uid, 1_001, reason="purchase", ref_type="order", ref_id=1, currency="RUB"
            )
        ) is None
        entry = await wallet.debit(
            conn, uid, 1_000, reason="purchase", ref_type="order", ref_id=2, currency="RUB"
        )
        assert entry is not None and entry.balance_after == 0 and entry.amount_minor == -1_000
        assert (
            await wallet.debit(conn, uid, 1, reason="purchase", ref_type="order", ref_id=3, currency="RUB")
        ) is None
    assert await env.balance(uid) == 0
    with pytest.raises(ValueError):
        async with env.db.tx() as conn:
            await wallet.debit(conn, uid, 0, reason="purchase", ref_type="order", ref_id=4, currency="RUB")
    with pytest.raises(WalletRuleError):
        async with env.db.tx() as conn:
            await wallet.credit(conn, uid, 5, reason="purchase", ref_type="order", ref_id=5, currency="RUB")
    await env.assert_wallet_invariants()


async def test_parallel_debits_of_one_balance_take_it_once(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 17_900)

    async def buy(n: int) -> bool:
        async with env.db.tx() as conn:
            got = await wallet.debit(
                conn, uid, 17_900, reason="purchase", ref_type="order", ref_id=n, currency="RUB"
            )
            await asyncio.sleep(0.01)
        return got is not None

    results = await asyncio.gather(*(buy(n) for n in range(8)))
    assert results.count(True) == 1
    assert await env.balance(uid) == 0
    await env.assert_wallet_invariants()


async def test_same_key_in_two_racing_transactions_never_credits_twice(env: BillingEnv) -> None:
    uid = await env.user()

    async def credit() -> str:
        try:
            async with env.db.tx() as conn:
                got = await wallet.credit(
                    conn, uid, 700, reason="topup", ref_type="payment", ref_id="p-1", currency="RUB"
                )
                await asyncio.sleep(0.02)
            return "credited" if got else "noop"
        except IntegrityError:
            return "conflict"

    outcomes = await asyncio.gather(*(credit() for _ in range(6)))
    assert outcomes.count("credited") == 1, outcomes
    assert await env.balance(uid) == 700
    assert len(await env.ledger(uid)) == 1
    await env.assert_wallet_invariants()


async def test_credit_many_one_operation_two_users_rerun_adds_nothing(env: BillingEnv) -> None:
    a, b = await env.user(), await env.user()
    async with env.db.tx() as conn:
        first = await wallet.credit_many(
            conn,
            [b, a, a],
            1_000,
            reason="import_opening",
            ref_type="import_run",
            ref_id="run-1",
            currency="RUB",
        )
    async with env.db.tx() as conn:
        again = await wallet.credit_many(
            conn,
            [a, b],
            1_000,
            reason="import_opening",
            ref_type="import_run",
            ref_id="run-1",
            currency="RUB",
        )
    assert [e.user_id for e in first] == sorted([a, b])
    assert again == []
    assert await env.balance(a) == await env.balance(b) == 1_000
    await env.assert_wallet_invariants()


async def test_take_takes_what_is_there(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 300)
    async with env.db.tx() as conn:
        await wallet.lock_user(conn, uid)
        assert (
            await wallet.take(
                conn, uid, 500, reason="chargeback", ref_type="payment", ref_id="x", currency="RUB"
            )
            == 300
        )
        assert (
            await wallet.take(
                conn, uid, 500, reason="chargeback", ref_type="payment", ref_id="y", currency="RUB"
            )
            == 0
        )
    assert await env.balance(uid) == 0
    await env.assert_wallet_invariants()


async def test_admin_adjust_writes_audit_and_respects_zero(env: BillingEnv) -> None:
    uid = await env.user()
    admin = await env.user(role="admin", perms=["wallet.adjust"])
    async with env.db.tx() as conn:
        up = await wallet.adjust(
            conn, uid, 1_000, op_id="op-1", actor_id=admin, role="admin", reason="компенсация", currency="RUB"
        )
        dup = await wallet.adjust(
            conn, uid, 1_000, op_id="op-1", actor_id=admin, role="admin", reason="компенсация", currency="RUB"
        )
        too_much = await wallet.adjust(
            conn, uid, -2_000, op_id="op-2", actor_id=admin, role="admin", reason="ошибка", currency="RUB"
        )
        down = await wallet.adjust(
            conn, uid, -400, op_id="op-3", actor_id=admin, role="admin", reason="ошибка", currency="RUB"
        )
    assert up is not None and dup is None and too_much is None
    assert down is not None and down.balance_after == 600
    audit = await env.rows(
        "select action, amount_minor, reason from admin_audit where action = 'wallet.adjust' order by id"
    )
    assert [(r["amount_minor"], r["reason"]) for r in audit] == [(1_000, "компенсация"), (-400, "ошибка")]
    with pytest.raises(ValueError):
        async with env.db.tx() as conn:
            await wallet.adjust(
                conn, uid, 5, op_id="op-4", actor_id=admin, role="admin", reason=" ", currency="RUB"
            )
    await env.assert_wallet_invariants()


async def test_mismatch_report_finds_a_tampered_balance(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 100)
    await env.db.raw("update users set wallet_minor = 150 where id = $1", uid)
    async with env.db.read() as conn:
        bad = await wallet.mismatches(conn)
        assert bad == [{"user_id": uid, "wallet_minor": 150, "ledger": 100}]
        assert await wallet.mismatches(conn, [uid + 1000]) == []


async def test_balance_check_constraint_is_the_last_line(env: BillingEnv) -> None:
    uid = await env.user()
    with pytest.raises(Exception, match="wallet_minor"):
        await env.db.raw("update users set wallet_minor = -1 where id = $1", uid)
