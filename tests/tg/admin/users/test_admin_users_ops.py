"""Operations of the user card: through services and the writer, role re-check, limits, reasons, audit."""

from __future__ import annotations

import json
from datetime import timedelta

from svbg.core.clock import now
from svbg.tg.admin.users.screens import ACTIONS, SCREEN_CARD, SCREEN_CONFIRM, SCREEN_PLANS
from svbg.tg.ui.codec import encode
from tests.catalog.kit import SQ_NL, add_location, add_plan
from tests.tg.admin.users.kit import ADMIN, CONF_OWNER, OWNER, SUPPORT, USER, UEnv, add_sub


async def paid_until(env: UEnv, sid: int) -> object:
    return (await env.db.raw("select paid_until from subscriptions where id = $1", sid))[0]["paid_until"]


async def open_card(env: UEnv, who: int, tg: int = USER) -> None:
    await env.click(who, encode(SCREEN_CARD, arg=str(env.ids[tg])))


# ------------------------------------------------------------------------------------------- days


async def test_admin_adds_days_with_a_reason(env: UEnv) -> None:
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid, days=10)
    before = await paid_until(env, sid)
    await open_card(env, ADMIN)
    await env.press(ADMIN, "➕ Дни")
    assert "Сколько дней" in env.text
    await env.type(ADMIN, "0")
    assert "отличное от нуля" in env.text
    await env.type(ADMIN, "7")
    assert "Причина" in env.text
    await env.type(ADMIN, "ок")  # too short: the reason is mandatory
    assert "Укажите причину" in env.text
    await env.type(ADMIN, "компенсация за сбой")
    assert "✅ Готово: +7 дн." in env.text
    after = await paid_until(env, sid)
    assert after - before == timedelta(days=7)  # type: ignore[operator]
    audit = [r for r in await env.audit() if r["action"] == "subs.grant"]
    assert len(audit) == 1
    row = audit[0]
    assert (
        row["actor_id"] == env.ids[ADMIN]
        and row["role"] == "admin"
        and row["reason"] == "компенсация за сбой"
    )
    assert row["target"] == f"user:{uid}" and row["details"]["days"] == 7
    assert [j["kind"] for j in await env.jobs() if j["kind"].startswith("panel.")] == ["panel.update"]
    events = await env.db.raw("select kind, source from subscription_events where subscription_id = $1", sid)
    # journaled as granted time from an admin: the LTE period engine sees an «admin» event (05 §2.1.9)
    assert [(e["kind"], e["source"]) for e in events] == [("extended", "admin")]


async def test_negative_days_shorten_the_term(env: UEnv) -> None:
    sid = await add_sub(env.db, env.ids[USER], days=10)
    before = await paid_until(env, sid)
    result = await env.ops.grant_days(ADMIN, env.ids[USER], -3, "ошибка начисления", "a" * 32)
    assert result.ok, result.text
    assert before - await paid_until(env, sid) == timedelta(days=3)  # type: ignore[operator]


async def test_days_over_the_admin_limit_need_the_owner(env: UEnv) -> None:
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid, days=1)
    before = await paid_until(env, sid)
    result = await env.ops.grant_days(ADMIN, uid, 40, "подарок на праздник", "b" * 32)
    assert not result.ok and "только владелец" in result.text
    assert await paid_until(env, sid) == before
    assert [r["action"] for r in await env.audit()] == []
    result = await env.ops.grant_days(OWNER, uid, 40, "подарок на праздник", "c" * 32)
    assert result.ok
    env.settings["ADMIN_GRANT_DAYS_MAX"] = 60  # applies at once, no restart
    result = await env.ops.grant_days(ADMIN, uid, 40, "подарок на праздник", "d" * 32)
    assert result.ok


async def test_repeated_operation_id_is_applied_once(env: UEnv) -> None:
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid, days=1)
    before = await paid_until(env, sid)
    first = await env.ops.grant_days(ADMIN, uid, 2, "двойное нажатие", "e" * 32)
    second = await env.ops.grant_days(ADMIN, uid, 2, "двойное нажатие", "e" * 32)
    assert first.ok and second.ok and second.text == "Уже сделано."
    assert await paid_until(env, sid) - before == timedelta(days=2)  # type: ignore[operator]
    assert len([r for r in await env.audit() if r["action"] == "subs.grant"]) == 1


async def test_days_without_a_subscription(env: UEnv) -> None:
    result = await env.ops.grant_days(ADMIN, env.ids[USER], 3, "компенсация", "f" * 32)
    assert not result.ok and "нет активной подписки" in result.text


async def test_support_cannot_grant_days_even_bypassing_the_router(env: UEnv) -> None:
    await add_sub(env.db, env.ids[USER])
    result = await env.ops.grant_days(SUPPORT, env.ids[USER], 3, "компенсация", "1" * 32)
    assert result.denied and result.text == "Нет прав"
    audit = await env.audit()
    assert [(r["action"], r["role"]) for r in audit] == [("access_denied", "support")]


async def test_role_revoked_after_the_menu_was_cached(env: UEnv) -> None:
    """The router still believes the cached «admin», the database says «user»: the operation is refused."""
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid, days=5)
    before = await paid_until(env, sid)
    await open_card(env, ADMIN)
    await env.press(ADMIN, "➕ Дни")
    await env.type(ADMIN, "5")
    await env.db.raw("update users set role = 'user', perms = '[]'::jsonb where telegram_id = $1", ADMIN)
    await env.type(ADMIN, "компенсация")
    assert "Нет прав" in env.text
    assert await paid_until(env, sid) == before
    assert await env.jobs() == []


async def test_non_staff_actor_is_refused_without_audit_spam(env: UEnv) -> None:
    result = await env.ops.grant_days(USER, env.ids[USER], 3, "компенсация", "2" * 32)
    assert result.denied
    assert await env.audit() == []


# ------------------------------------------------------------------------------------------- plan


async def test_give_plan_to_a_user_without_subscription(env: UEnv) -> None:
    await add_location(env.db, SQ_NL, "NL")
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await add_plan(env.db, "trial", name="Пробный", is_trial=True)
    await env.catalog.reload()
    uid = env.ids[USER]
    await env.click(ADMIN, encode(SCREEN_PLANS, arg=str(uid)))
    assert "Стандарт" in " ".join(env.labels()) and "Пробный" not in " ".join(env.labels())
    await env.press(ADMIN, "Стандарт")
    assert "«Стандарт»" in env.text
    await env.type(ADMIN, "30")
    await env.type(ADMIN, "тест для блогера")
    assert "✅ Тариф «Стандарт» выдан на 30 дн." in env.text
    subs = await env.db.raw("select plan_id, link_state from subscriptions where user_id = $1", uid)
    assert [(s["plan_id"], s["link_state"]) for s in subs] == [(pid, "pending")]
    assert [j["kind"] for j in await env.jobs() if j["kind"].startswith("panel.")] == ["panel.create"]
    audit = [r for r in await env.audit() if r["action"] == "subs.give_plan"]
    assert audit and audit[0]["reason"] == "тест для блогера" and audit[0]["details"]["plan_id"] == pid


async def test_give_plan_refusals(env: UEnv) -> None:
    trial = await add_plan(env.db, "trial", name="Пробный", is_trial=True)
    std = await add_plan(env.db, "std")
    await env.catalog.reload()
    uid = env.ids[USER]
    assert "Пробный тариф" in (await env.ops.give_plan(ADMIN, uid, trial, 3, "причина", "3" * 32)).text
    assert "не найден" in (await env.ops.give_plan(ADMIN, uid, 99999, 3, "причина", "4" * 32)).text
    assert "только владелец" in (await env.ops.give_plan(ADMIN, uid, std, 90, "причина", "5" * 32)).text
    await env.db.raw("update users set banned_at = now() where id = $1", uid)
    assert "разблокируйте" in (await env.ops.give_plan(ADMIN, uid, std, 3, "причина", "6" * 32)).text
    await env.click(ADMIN, encode(ACTIONS, "plan", f"{uid}:99999"))
    assert env.toasts[-1] == "Пользователь не найден"
    assert await env.jobs() == []


# ------------------------------------------------------------------------------------------- wallet


async def wallet(env: UEnv, tg: int = USER) -> int:
    rows = await env.db.raw("select wallet_minor from users where telegram_id = $1", tg)
    return int(rows[0]["wallet_minor"])


async def test_wallet_credit_and_debit_with_ledger_and_audit(env: UEnv) -> None:
    await add_plan(env.db, "std", prices=((30, 17900), (360, 169900)))
    await env.catalog.reload()
    uid = env.ids[USER]
    await open_card(env, ADMIN)
    await env.press(ADMIN, "💰 Баланс")
    await env.type(ADMIN, "сто")
    assert "Нужна сумма" in env.text
    await env.type(ADMIN, "150")
    await env.type(ADMIN, "возврат за простой")
    assert "✅ Баланс: +150 ₽. Теперь +150 ₽." in env.text
    assert await wallet(env) == 15000
    result = await env.ops.adjust_wallet(ADMIN, uid, -20000, "списание ошибки", "7" * 32)
    assert not result.ok and "меньше" in result.text
    result = await env.ops.adjust_wallet(ADMIN, uid, -5050, "списание ошибки", "8" * 32)
    assert result.ok and await wallet(env) == 9950
    ledger = await env.db.raw("select amount_minor, reason, actor_id, note from wallet_ledger order by id")
    assert [(r["amount_minor"], r["reason"]) for r in ledger] == [
        (15000, "admin_adjust"),
        (-5050, "admin_adjust"),
    ]
    assert ledger[0]["actor_id"] == env.ids[ADMIN] and ledger[0]["note"] == "возврат за простой"
    audit = [r for r in await env.audit() if r["action"] == "wallet.adjust"]
    assert [(r["amount_minor"], r["reason"]) for r in audit] == [
        (15000, "возврат за простой"),
        (-5050, "списание ошибки"),
    ]


async def test_wallet_limit_is_the_most_expensive_plan_by_default(env: UEnv) -> None:
    uid = env.ids[USER]
    result = await env.ops.adjust_wallet(ADMIN, uid, 100, "без тарифов", "9" * 32)
    assert not result.ok and "только владелец" in result.text  # no plans: only the owner
    await add_plan(env.db, "std", prices=((30, 17900), (360, 169900)))
    await env.catalog.reload()
    assert (await env.ops.adjust_wallet(ADMIN, uid, 169900, "лимит", "a1" * 16)).ok
    assert not (await env.ops.adjust_wallet(ADMIN, uid, 169901, "сверх лимита", "a2" * 16)).ok
    assert (await env.ops.adjust_wallet(OWNER, uid, 500000, "сверх лимита", "a3" * 16)).ok
    env.settings["ADMIN_WALLET_ADJUST_MAX"] = 10_000  # whole rubles
    assert (await env.ops.adjust_wallet(ADMIN, uid, 900000, "новый лимит", "a4" * 16)).ok
    assert await wallet(env) == 169900 + 500000 + 900000


async def test_wallet_zero_and_duplicate(env: UEnv) -> None:
    uid = env.ids[USER]
    assert "отличное от нуля" in (await env.ops.adjust_wallet(OWNER, uid, 0, "ноль", "b1" * 16)).text
    assert (await env.ops.adjust_wallet(OWNER, uid, 500, "бонус", "b2" * 16)).ok
    again = await env.ops.adjust_wallet(OWNER, uid, 500, "бонус", "b2" * 16)
    assert again.ok and again.text == "Уже сделано."
    assert await wallet(env) == 500


async def test_configured_owner_without_a_row_is_an_owner(env: UEnv) -> None:
    result = await env.ops.adjust_wallet(CONF_OWNER, env.ids[USER], 100, "бонус", "b3" * 16)
    assert result.ok
    audit = [r for r in await env.audit() if r["action"] == "wallet.adjust"]
    assert audit[0]["actor_id"] is None and audit[0]["role"] == "owner"


# ------------------------------------------------------------------------------------------- block


async def test_ban_and_unban(env: UEnv) -> None:
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid)
    await open_card(env, ADMIN)
    await env.press(ADMIN, "🚫 Заблокировать")
    await env.type(ADMIN, "мошенничество с чеками")
    assert "✅ Пользователь заблокирован" in env.text
    assert (await env.db.raw("select banned_at from users where id = $1", uid))[0]["banned_at"] is not None
    jobs = await env.jobs("panel.disable")
    assert (
        len(jobs) == 1 and jobs[0]["payload"]["reason"] == "BOT_BAN" and jobs[0]["payload"]["sub_id"] == sid
    )
    assert env.directory.invalidated == [USER]
    assert (await env.ops.ban(ADMIN, uid, "ещё раз")).text == "Пользователь уже заблокирован."
    await env.press(ADMIN, "✅ Разблокировать")
    await env.type(ADMIN, "разобрались")
    assert "✅ Пользователь разблокирован" in env.text
    jobs = await env.jobs("panel.enable")
    assert len(jobs) == 1 and jobs[0]["payload"]["only_reason"] == "BOT_BAN"
    assert [r["action"] for r in await env.audit()] == ["user.ban", "user.unban"]
    assert env.directory.invalidated == [USER, USER]


async def test_ban_refusals(env: UEnv) -> None:
    assert (await env.ops.ban(ADMIN, env.ids[ADMIN], "сам себя")).text == "Себя заблокировать нельзя."
    assert (await env.ops.ban(ADMIN, env.ids[OWNER], "владелец")).text == "Владельца заблокировать нельзя."
    assert "только владелец" in (await env.ops.ban(ADMIN, env.ids[SUPPORT], "сотрудник")).text
    assert (await env.ops.ban(OWNER, env.ids[SUPPORT], "сотрудник")).ok
    assert "Укажите причину" in (await env.ops.ban(OWNER, env.ids[USER], " ")).text
    assert (
        await env.ops.unban(OWNER, env.ids[USER], "не блокирован")
    ).text == "Пользователь не заблокирован."
    assert (await env.ops.ban(SUPPORT, env.ids[USER], "нет прав")).denied


# ------------------------------------------------------------------------------------------- devices, link


async def test_support_resets_devices_after_confirmation(env: UEnv) -> None:
    uid = env.ids[USER]
    sid = await add_sub(env.db, uid)
    await open_card(env, SUPPORT)
    await env.press(SUPPORT, "📱 Сбросить устройства")
    assert "Сбросить все устройства" in env.text
    assert await env.jobs() == []
    await env.press(SUPPORT, "✅ Да")
    assert "✅ Устройства сброшены" in env.text
    jobs = await env.jobs("panel.hwid_reset")
    assert len(jobs) == 1 and jobs[0]["payload"]["sub_id"] == sid
    # staff are not slowed down by the user's cooldown
    assert (await env.ops.reset_devices(SUPPORT, uid)).ok
    assert [r["action"] for r in await env.audit()] == ["user.devices_reset", "user.devices_reset"]


async def test_reissue_link(env: UEnv) -> None:
    uid = env.ids[USER]
    await add_sub(env.db, uid)
    await env.click(ADMIN, encode(SCREEN_CONFIRM, arg=f"ri:{uid}"))
    assert "Перевыпустить ссылку" in env.text
    await env.press(ADMIN, "✅ Да")
    assert "Ссылка перевыпущена" in env.text
    assert len(await env.jobs("panel.revoke")) == 1


async def test_device_actions_need_a_linked_subscription(env: UEnv) -> None:
    uid = env.ids[USER]
    assert "нет активной подписки" in (await env.ops.reissue(ADMIN, uid)).text
    await add_sub(env.db, uid, link_state="pending")
    assert "ещё не подключена" in (await env.ops.reset_devices(ADMIN, uid)).text
    await env.click(ADMIN, encode(SCREEN_CONFIRM, arg=f"xx:{uid}"))
    assert "Админка" in env.text
    assert await env.jobs() == []


# ------------------------------------------------------------------------------------------- message


async def test_message_to_the_user(env: UEnv) -> None:
    uid = env.ids[USER]
    await open_card(env, SUPPORT)
    await env.press(SUPPORT, "✉️ Написать")
    await env.type(SUPPORT, "Здравствуйте! <b>Проверьте</b> настройки")
    assert "✅ Сообщение отправлено" in env.text
    chat, text, mode = env.notifier.sent[-1]
    assert chat == USER and mode == "HTML" and "&lt;b&gt;Проверьте&lt;/b&gt;" in text
    env.notifier.blocked.add(USER)
    result = await env.ops.message(SUPPORT, uid, "ещё раз")
    assert not result.ok and "заблокировал бота" in result.text
    env.notifier.fail = TimeoutError()
    assert "Не удалось" in (await env.ops.message(SUPPORT, uid, "и ещё")).text
    outcomes = [r["details"]["outcome"] for r in await env.audit() if r["action"] == "user.message"]
    assert outcomes == ["sent", "blocked", "failed"]
    assert (await env.ops.message(SUPPORT, uid, "   ")).text == "Пустое сообщение."


async def test_message_refused_for_non_staff(env: UEnv) -> None:
    result = await env.ops.message(USER, env.ids[USER], "привет")
    assert result.denied and env.notifier.sent == []


# ------------------------------------------------------------------------------------------- misc


async def test_form_cancel_returns_to_the_card(env: UEnv) -> None:
    await add_sub(env.db, env.ids[USER])
    await open_card(env, ADMIN)
    await env.press(ADMIN, "💰 Баланс")
    await env.press(ADMIN, "Отмена")
    assert "Отменено." in env.text and "Иван" in env.text


async def test_frozen_subscription_gets_days_on_unfreeze(env: UEnv) -> None:
    sid = await add_sub(env.db, env.ids[USER], days=5, hold=True)
    before = await paid_until(env, sid)
    result = await env.ops.grant_days(ADMIN, env.ids[USER], 3, "компенсация", "c1" * 16)
    assert result.ok and "после разморозки" in result.text
    assert await paid_until(env, sid) == before
    rows = await env.db.raw("select hold_frozen_seconds from subscriptions where id = $1", sid)
    assert rows[0]["hold_frozen_seconds"] == 3 * 86_400


async def test_audit_details_are_json(env: UEnv) -> None:
    await add_sub(env.db, env.ids[USER])
    await env.ops.grant_days(OWNER, env.ids[USER], 1, "проверка", "d1" * 16)
    row = (await env.audit())[0]
    assert json.loads(json.dumps(row["details"]))["op_id"] == "d1" * 16
    assert row["ts"] <= now()
