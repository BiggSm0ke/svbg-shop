"""Fulfill exactly once and the purchase message: «✅ Оплачено … 🔗 Подключиться», panel outages, lost
messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from svbg.billing.kinds import UI_JOB, UI_PROGRESS_JOB
from svbg.billing.ports import Notice, UiRef
from svbg.billing.texts import SCREEN_CONNECTING, SCREEN_PAID, SCREEN_REFUNDED
from svbg.jobs.queue import JobQueue
from tests.billing.kit import BillingEnv


async def _paid(env: BillingEnv, *, days: int = 30) -> tuple[int, int, UiRef]:
    uid = await env.user()
    await env.fund(uid, 200_000)
    draft = await env.draft(uid, days=days)
    ref = env.messenger.show(await env.telegram_id(uid))
    assert (await env.checkout.pay(draft.order_id, uid, ui_ref=ref)).outcome == "paid"
    return uid, draft.order_id, ref


def _screen(env: BillingEnv, ref: UiRef) -> str:
    shown = env.messenger.notice(ref)
    return shown.screen if isinstance(shown, Notice) else shown


async def test_fulfill_creates_the_subscription_and_announces_it(env: BillingEnv) -> None:
    uid, order_id, ref = await _paid(env, days=90)
    await env.drain()
    order = await env.order(order_id)
    assert order["status"] == "fulfilled" and order["subscription_id"] is not None
    sub = (await env.rows("select * from subscriptions where id = $1", order["subscription_id"]))[0]
    assert sub["user_id"] == uid and sub["plan_id"] == 1
    assert (sub["paid_until"] - order["fulfilled_at"]).days in (89, 90)
    items = await env.rows("select status from order_items where order_id = $1", order_id)
    assert {i["status"] for i in items} == {"fulfilled"}
    names = [e.name for e in env.s.events]
    assert "order.fulfilled" in names and "subscription.term_changed" in names
    fulfilled = next(e for e in env.s.events if e.name == "order.fulfilled")
    assert fulfilled.payload["order_id"] == order_id and fulfilled.payload["total_minor"] == 49_900
    assert _screen(env, ref) == SCREEN_PAID
    # a renewal of the same plan adds to the end of the term, same message flow
    renew = await env.draft(uid)
    ref2 = env.messenger.show(await env.telegram_id(uid))
    await env.checkout.pay(renew.order_id, uid, ui_ref=ref2)
    await env.drain()
    sub2 = (await env.rows("select paid_until from subscriptions where id = $1", order["subscription_id"]))[0]
    assert (sub2["paid_until"] - sub["paid_until"]).days == 30
    assert _screen(env, ref2) == SCREEN_PAID


async def test_fulfill_is_exactly_once(env: BillingEnv) -> None:
    _uid, order_id, _ref = await _paid(env)
    f = env.billing.fulfiller
    assert await f.fulfill(order_id) == "fulfilled"
    assert await f.fulfill(order_id) == "fulfilled"
    # a duplicate job (e.g. re-enqueued after the first was done) changes nothing
    async with env.db.tx() as conn:
        from svbg.billing.kinds import enqueue_fulfill

        await enqueue_fulfill(conn, order_id)
    await env.drain()
    events = await env.rows("select kind from subscription_events where ref_id = $1", str(order_id))
    assert [e["kind"] for e in events] == ["purchase_new"]
    assert len(await env.jobs(UI_JOB)) == 1


async def test_panel_down_shows_connecting_then_the_same_message_gets_the_button(env: BillingEnv) -> None:
    env.s.panel.inject("503", times=None)
    _uid, order_id, ref = await _paid(env)
    await env.drain()
    order = await env.order(order_id)
    assert order["status"] == "fulfilled"  # the money part is done whatever the panel does
    assert order["ui_stage"] is None and env.messenger.text(ref) == "⏳ Оформляю…"
    # the UI job waits behind the panel job (same ordering key); the progress job speaks up
    await env.drain(make_due=True, kinds=[UI_PROGRESS_JOB])
    assert _screen(env, ref) == SCREEN_CONNECTING
    assert "подключаем" in env.messenger.text(ref)
    assert (await env.order(order_id))["ui_stage"] == "connecting"
    env.s.panel.clear_faults()
    await env.drain(make_due=True)
    order = await env.order(order_id)
    assert order["ui_stage"] == "done"
    assert _screen(env, ref) == SCREEN_PAID and env.messenger.sends == []


async def test_progress_never_overwrites_the_final_message(env: BillingEnv) -> None:
    _uid, _order_id, ref = await _paid(env)
    await env.drain()
    assert _screen(env, ref) == SCREEN_PAID
    await env.drain(make_due=True)  # the delayed progress job runs after the final message
    assert _screen(env, ref) == SCREEN_PAID
    assert [j["status"] for j in await env.jobs(UI_PROGRESS_JOB)] == ["done"]


async def test_dead_panel_job_keeps_connecting_until_it_is_retried(env: BillingEnv) -> None:
    env.s.panel.inject("503", times=None)
    _uid, order_id, ref = await _paid(env)
    await env.drain()
    await env.db.raw("update jobs set status = 'dead' where queue = 'panel' and status = 'ready'")
    await env.drain(make_due=True)
    assert _screen(env, ref) == SCREEN_CONNECTING
    first, recheck = await env.jobs(UI_JOB)
    # the job under the subscription's ordering key is done (it no longer parks the queue of «sub:<id>»);
    # a keyless re-check waits for the panel user
    assert first["status"] == "done" and first["ordering_key"] is not None
    assert recheck["status"] == "ready" and recheck["ordering_key"] is None
    await env.drain(make_due=True)
    recheck = (await env.jobs(UI_JOB))[1]
    assert recheck["status"] == "ready" and "не готов" in (recheck["last_error"] or "")
    env.s.panel.clear_faults()
    queue = JobQueue(env.db)
    for job in await env.jobs(status="dead"):
        await queue.retry_dead(job["id"])
    await env.drain(make_due=True)
    assert (await env.order(order_id))["ui_stage"] == "done"
    assert _screen(env, ref) == SCREEN_PAID


async def test_deleted_message_gets_a_new_one_and_the_ref_moves(env: BillingEnv) -> None:
    uid, order_id, ref = await _paid(env)
    env.messenger.gone.add((ref.chat_id, ref.message_id))
    await env.drain()
    [(chat, notice)] = env.messenger.sends
    assert chat == await env.telegram_id(uid) and notice.screen == SCREEN_PAID
    order = await env.order(order_id)
    assert order["ui_ref"]["message_id"] != ref.message_id


async def test_transient_telegram_failure_is_retried(env: BillingEnv) -> None:
    _uid, order_id, ref = await _paid(env)
    env.messenger.fail = 1
    outcomes = await env.drain()
    assert any(job.kind == UI_JOB and how == "failed" for job, how in outcomes)
    assert (await env.order(order_id))["ui_stage"] is None
    await env.drain(make_due=True)
    assert (await env.order(order_id))["ui_stage"] == "done" and _screen(env, ref) == SCREEN_PAID


async def test_blocked_user_does_not_loop(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 20_000)
    draft = await env.draft(uid)
    await env.checkout.pay(draft.order_id, uid)  # no message to edit
    env.messenger.blocked.add(await env.telegram_id(uid))
    await env.drain()
    assert (await env.order(draft.order_id))["ui_stage"] == "done"
    assert env.messenger.sends == []


async def test_refused_by_the_subscription_service_refunds(env: BillingEnv) -> None:
    uid = await env.user()
    await env.fund(uid, 100_000)
    first = await env.draft(uid)
    await env.checkout.pay(first.order_id, uid)
    await env.drain()
    addon = await env.checkout.draft_devices(uid, 10)  # 5 + 10 = 15: allowed when priced
    ref = env.messenger.show(await env.telegram_id(uid))
    assert (await env.checkout.pay(addon.order_id, uid, ui_ref=ref)).outcome == "paid"
    await env.db.raw("update subscriptions set extra_devices = 3 where user_id = $1", uid)  # meanwhile
    before = await env.balance(uid)
    await env.drain()
    order = await env.order(addon.order_id)
    assert order["status"] == "canceled" and order["note"].startswith("refused")
    assert await env.balance(uid) == before + addon.quote.total_minor
    assert _screen(env, ref) == SCREEN_REFUNDED
    assert "вернулись на баланс" in env.messenger.text(ref)
    sub = (await env.rows("select extra_devices from subscriptions where user_id = $1", uid))[0]
    assert sub["extra_devices"] == 3  # the partial change was rolled back
    assert await env.rows(
        "select id from attention_items where dedup_key = $1", f"billing:fulfill_failed:{addon.order_id}"
    )
    await env.assert_wallet_invariants()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def fulfill(self, conn: Any, order: Mapping[str, Any], item: Mapping[str, Any]) -> None:
        self.calls.append((int(order["id"]), str(item["type"])))


async def test_module_items_are_fulfilled_after_the_term(env: BillingEnv) -> None:
    rec = _Recorder()
    env.billing.fulfiller.register_item("lte_pack", rec)
    with pytest.raises(ValueError):
        env.billing.fulfiller.register_item("plan_period", rec)
    _uid, order_id, _ref = await _paid(env)
    await env.db.raw(
        "insert into order_items (order_id, position, type, amount_minor) values ($1, 5, 'lte_pack', 0)",
        order_id,
    )
    await env.drain()
    assert rec.calls == [(order_id, "lte_pack")]
    assert {
        r["status"] for r in await env.rows("select status from order_items where order_id = $1", order_id)
    } == {"fulfilled"}
    # an item type nobody handles refunds the order instead of taking money for nothing
    uid2, order2, _ = await _paid(env)
    await env.db.raw(
        "insert into order_items (order_id, position, type, amount_minor) values ($1, 5, 'mystery', 0)",
        order2,
    )
    await env.drain()
    assert (await env.order(order2))["status"] == "canceled"
    assert await env.balance(uid2) == 200_000


class _KindRecorder:
    def __init__(self, sid: int | None) -> None:
        self.sid = sid
        self.calls: list[int] = []

    async def apply(self, conn: Any, order: Mapping[str, Any], items: Any) -> int:
        from svbg.subscriptions.lifecycle import SubscriptionError

        self.calls.append(int(order["id"]))
        if self.sid is None:
            raise SubscriptionError("lte_period_changed", "Пакет не нужен.")
        return self.sid


async def test_module_order_kind_is_applied_instead_of_a_term(env: BillingEnv) -> None:
    """X4 ``addon_lte``: the module's kind handler applies the order; a refusal refunds it."""
    with pytest.raises(ValueError):
        env.billing.fulfiller.register_kind("renew", _KindRecorder(1))
    uid, order_id, _ref = await _paid(env)
    sid = await env.s.new_sub(None)
    await env.db.raw("update subscriptions set user_id = $1 where id = $2", uid, sid)
    await env.db.raw("update orders set kind = 'addon_lte' where id = $1", order_id)
    kind = _KindRecorder(sid)
    env.billing.fulfiller.register_kind("addon_lte", kind)
    await env.drain()
    order = await env.order(order_id)
    assert kind.calls == [order_id]
    assert (order["status"], order["subscription_id"]) == ("fulfilled", sid)
    assert len(await env.rows("select id from subscriptions where user_id = $1", uid)) == 1  # no plan term

    uid2, order2, _ = await _paid(env)
    await env.db.raw("update orders set kind = 'addon_lte' where id = $1", order2)
    env.billing.fulfiller.register_kind("addon_lte", _KindRecorder(None))
    await env.drain()
    assert (await env.order(order2))["status"] == "canceled"
    assert await env.balance(uid2) == 200_000
    await env.assert_wallet_invariants()


async def test_frozen_at_fulfill_is_held_then_refunded_on_decision(env: BillingEnv) -> None:
    uid, order_id, _ref = await _paid(env)
    sid = await env.s.new_sub(None)
    await env.db.raw(
        "update subscriptions set user_id = $1, hold_kind = 'admin', hold_since = now() where id = $2",
        uid,
        sid,
    )
    await env.drain()
    assert (await env.order(order_id))["status"] == "held"
    assert await env.balance(uid) == 200_000 - 17_900
    owner_tg = await env.telegram_id(await env.user(role="owner"))
    resolve = env.billing.fulfiller.resolve_held
    res = await resolve(order_id, "refund", actor_telegram_id=owner_tg)
    assert (res.action, res.refunded_minor) == ("refund", 17_900)
    assert await env.balance(uid) == 200_000
    assert (await resolve(order_id, "refund", actor_telegram_id=owner_tg)).action == "noop"
    with pytest.raises(ValueError):
        await resolve(order_id, "steal", actor_telegram_id=owner_tg)
    await env.assert_wallet_invariants()


async def test_bad_jobs_go_dead_not_loop(env: BillingEnv) -> None:
    from svbg.billing.kinds import FULFILL_JOB, NOTICE_JOB

    queue = JobQueue(env.db)
    await queue.enqueue(FULFILL_JOB, {"order_id": "x"}, queue="billing")
    await queue.enqueue(FULFILL_JOB, {"order_id": 10**9}, queue="billing")
    await queue.enqueue(NOTICE_JOB, {"type": "weird", "user_id": 1, "currency": "RUB"}, queue="billing")
    await queue.enqueue(NOTICE_JOB, {"type": "credited"}, queue="billing")
    outcomes = await env.drain()
    assert [how for _job, how in outcomes] == ["dead"] * 4
