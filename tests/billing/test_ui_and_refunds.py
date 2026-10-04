"""The purchase message without row locks (Telegram is slow, jobs overlap), the keyless re-check, notices in
the user's language, partial refunds."""

from __future__ import annotations

from datetime import timedelta

import pytest

from svbg.billing.crediting import refund_share
from svbg.billing.kinds import UI_JOB, UI_PROGRESS_JOB
from svbg.billing.ports import Notice, UiRef
from svbg.billing.texts import SCREEN_CONNECTING, SCREEN_PAID
from svbg.core.clock import now
from svbg.jobs.queue import JobQueue
from svbg.remnawave.writer import ordering_key
from tests.billing.kit import BillingEnv


async def _paid(env: BillingEnv) -> tuple[int, int, UiRef]:
    uid = await env.user()
    await env.fund(uid, 20_000)
    draft = await env.draft(uid)
    ref = env.messenger.show(await env.telegram_id(uid))
    assert (await env.checkout.pay(draft.order_id, uid, ui_ref=ref)).outcome == "paid"
    return uid, draft.order_id, ref


async def _all_but_ui(env: BillingEnv) -> None:
    """Fulfill and the panel jobs; the two UI jobs stay queued."""
    kinds = [k for k in env.handlers() if k not in (UI_JOB, UI_PROGRESS_JOB)]
    await env.drain(kinds=kinds)


def _screens(env: BillingEnv) -> list[str]:
    return [n.screen for _ref, n in env.messenger.edits]


async def test_telegram_is_called_without_a_lock_on_the_order(env: BillingEnv) -> None:
    _uid, order_id, ref = await _paid(env)
    probes: list[str] = []

    async def lock_is_free(_ref: UiRef, notice: Notice) -> None:
        # a payment webhook for the same purchase would lock the row right now: it must not wait
        await env.db.raw("select id from orders where id = $1 for update nowait", order_id)
        probes.append(notice.screen)

    env.messenger.during_edit.append(lock_is_free)
    await env.drain()
    assert probes == [SCREEN_PAID]
    assert (await env.order(order_id))["ui_stage"] == "done"
    assert env.messenger.notice(ref).screen == SCREEN_PAID  # type: ignore[union-attr]


async def test_final_message_wins_when_progress_edit_lands_last(env: BillingEnv) -> None:
    _uid, order_id, ref = await _paid(env)
    await _all_but_ui(env)
    fired: list[bool] = []

    async def final_job_meanwhile(_ref: UiRef, notice: Notice) -> None:
        if notice.screen == SCREEN_CONNECTING and not fired:
            fired.append(True)
            await env.drain(kinds=[UI_JOB])  # the final message is shown while «подключаем…» is in flight

    env.messenger.during_edit.append(final_job_meanwhile)
    await env.drain(make_due=True, kinds=[UI_PROGRESS_JOB])
    assert fired
    # the late «подключаем…» overwrote the final message for a moment; the progress job noticed and repaired
    assert _screens(env) == [SCREEN_PAID, SCREEN_CONNECTING, SCREEN_PAID]
    assert env.messenger.notice(ref).screen == SCREEN_PAID  # type: ignore[union-attr]
    assert (await env.order(order_id))["ui_stage"] == "done"


async def test_final_message_wins_when_it_lands_after_progress(env: BillingEnv) -> None:
    _uid, order_id, ref = await _paid(env)
    await _all_but_ui(env)
    await env.make_due()
    fired: list[bool] = []

    async def progress_job_meanwhile(_ref: UiRef, notice: Notice) -> None:
        if notice.screen == SCREEN_PAID and not fired:
            fired.append(True)
            await env.drain(kinds=[UI_PROGRESS_JOB])

    env.messenger.during_edit.append(progress_job_meanwhile)
    await env.drain(kinds=[UI_JOB])
    assert fired and _screens(env)[-1] == SCREEN_PAID
    assert env.messenger.notice(ref).screen == SCREEN_PAID  # type: ignore[union-attr]
    assert (await env.order(order_id))["ui_stage"] == "done"


async def test_waiting_for_the_panel_user_does_not_hold_the_subscription_queue(env: BillingEnv) -> None:
    env.s.panel.inject("503", times=None)
    _uid, order_id, ref = await _paid(env)
    await env.drain()
    await env.db.raw("update jobs set status = 'dead' where queue = 'panel' and status = 'ready'")
    await env.drain(make_due=True)
    assert env.messenger.notice(ref).screen == SCREEN_CONNECTING  # type: ignore[union-attr]
    sub_id = (await env.order(order_id))["subscription_id"]
    queue = JobQueue(env.db)
    job_id = await queue.enqueue(
        "test.after", {}, queue="panel", lane="interactive", ordering_key=ordering_key(sub_id)
    )
    claimed = await queue.claim("interactive", "probe", 20)
    assert job_id in [j.id for j in claimed]  # e.g. a ban's panel.disable is not stuck behind the UI


# ------------------------------------------------------------------------------------------- language


async def test_notices_are_russian_even_for_an_old_english_user(env: BillingEnv) -> None:
    uid, order_id, ref = await _paid(env)
    await env.db.raw("update users set language = 'en' where id = $1", uid)  # left from the old bot
    await env.drain()
    shown = env.messenger.notice(ref)
    assert isinstance(shown, Notice) and shown.text.startswith("✅ Оплачено!")
    assert shown.buttons[0][0].text == "🔗 Подключиться" and shown.buttons[1][0].text == "Меню"
    top = await env.topup(uid, 20_000)
    assert await env.paid_webhook(top.payment_id) == 200
    await env.drain()
    [(_chat, credited)] = env.messenger.sends
    assert "Зачислено" in credited.text
    assert (await env.order(order_id))["status"] == "fulfilled"


# ------------------------------------------------------------------------------------------- refunds


@pytest.mark.parametrize(
    ("credited", "paid", "refund", "taken"),
    [
        (20_000, 20_000, None, 20_000),
        (20_000, 20_000, 20_000, 20_000),
        (20_000, 20_000, 25_000, 20_000),
        (20_000, 20_000, 5_000, 5_000),
        (20_000, 20_000, 0, 0),
        (18_000, 120, 1, 150),  # Stars: 1 of 120 ⭐ refunded = 150 kopecks, rounded up
        (10_000, 3, 1, 3_334),
    ],
)
def test_refund_share(credited: int, paid: int, refund: int | None, taken: int) -> None:
    assert refund_share(credited, paid, refund) == taken


async def test_partial_refund_takes_only_the_refunded_part(env: BillingEnv) -> None:
    uid = await env.user()
    top = await env.topup(uid, 20_000)
    assert await env.paid_webhook(top.payment_id) == 200
    record = await env.pay.core.get(top.payment_id)
    assert record is not None
    for _ in range(2):  # a repeated report changes nothing
        async with env.db.tx() as conn:
            await env.billing.crediting.on_refunded(conn, record, refund_minor=5_000)
    assert await env.balance(uid) == 15_000
    ledger = [(r["reason"], r["amount_minor"], r["note"]) for r in await env.ledger(uid)]
    assert ledger[-1] == ("chargeback", -5_000, "частичный возврат")
    await env.drain()
    assert not [k for k in await env.s.attention_keys() if k.startswith("billing:refund_amount")]
    await env.assert_wallet_invariants()


async def test_refund_without_an_amount_takes_all_and_tells_the_owner(env: BillingEnv) -> None:
    uid = await env.user()
    top = await env.topup(uid, 20_000)
    assert await env.paid_webhook(top.payment_id) == 200
    assert await env.paid_webhook(top.payment_id, status="refunded", at=now() + timedelta(seconds=1)) == 200
    assert await env.balance(uid) == 0
    await env.drain()
    assert f"billing:refund_amount:{top.payment_id}" in await env.s.attention_keys()
