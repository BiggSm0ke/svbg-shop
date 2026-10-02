"""Squads of a plan: checkboxes, «Применить к N текущим подписчикам?» (Да / Нет), compare-and-set, repair."""

from __future__ import annotations

import pytest

from svbg.catalog.locations import BROKEN_PREFIX, attention_key
from svbg.catalog.squads_job import KIND, ApplySquadsJob
from svbg.jobs.queue import JobQueue
from svbg.jobs.worker import JobContext
from svbg.tg.admin.plans import ACTIONS, SCREEN_CARD
from svbg.tg.ui.codec import decode, encode
from tests.catalog.kit import (
    ADMIN,
    OWNER,
    SQ_DE,
    SQ_FI,
    SQ_NL,
    PEnv,
    add_location,
    add_plan,
    add_sub,
    build_penv,
)
from tests.dbkit import CountingDatabase


@pytest.fixture
async def env(db: CountingDatabase) -> PEnv:
    penv = await build_penv(db)
    await penv.staff()
    await add_location(db, SQ_NL, "NL", sort=1)
    await add_location(db, SQ_DE, "DE", sort=2)
    await penv.catalog.reload()
    return penv


async def catalog_jobs(env: PEnv) -> list[dict[str, object]]:
    return [dict(r) for r in await env.db.raw("select * from jobs where kind = $1 order by id", KIND)]


async def audit(env: PEnv) -> list[dict[str, object]]:
    return [dict(r) for r in await env.db.raw("select * from admin_audit order by id")]


async def open_squads(env: PEnv, who: int, pid: int) -> None:
    await env.click(who, encode(SCREEN_CARD, arg=str(pid)))
    await env.press(who, "Сквады")


async def test_checkboxes_and_save_without_subscribers(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    assert "Выбрано: 1" in env.text
    assert [lb for lb in env.labels() if "NL" in lb or "DE" in lb] == ["✅ NL", "▫️ DE"]
    await env.press(OWNER, "DE")
    assert "Выбрано: 2" in env.text and "✅ DE" in env.labels()
    await env.press(OWNER, "NL")
    await env.press(OWNER, "DE")
    assert "Выбрано: 0" in env.text
    await env.press(OWNER, "Сохранить")
    assert env.toasts[-1] == "Выберите хотя бы одну локацию"
    await env.press(OWNER, "NL")
    await env.press(OWNER, "Сохранить")
    assert env.toasts[-1] == "Без изменений"
    await open_squads(env, OWNER, pid)
    await env.press(OWNER, "DE")
    await env.press(OWNER, "Сохранить")
    assert "Сквады сохранены" in env.text and "Сквады: NL, DE" in env.text
    assert await catalog_jobs(env) == []
    rows = await audit(env)
    assert rows[-1]["action"] == "plan.squads" and rows[-1]["details"]["to"] == [SQ_NL, SQ_DE]  # type: ignore[index]


async def test_yes_applies_to_current_subscribers(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    subs = [await add_sub(env.db, pid) for _ in range(3)]
    manual = await add_sub(env.db, pid, manual=True)
    await add_sub(env.db, pid, link_state="closed")
    await env.catalog.reload()
    await open_squads(env, ADMIN, pid)
    await env.press(ADMIN, "DE")
    await env.press(ADMIN, "Сохранить")
    assert "У тарифа <b>4</b> текущих подписчиков (из них 1 с ручной правкой" in env.text
    assert "Было: NL" in env.text and "Станет: NL, DE" in env.text
    yes = env.button("Да, применить к 3")
    await env.click(ADMIN, yes)
    assert "Применяю новые сквады к 3 подписчикам" in env.text and "Сквады: NL, DE" in env.text
    jobs = await catalog_jobs(env)
    assert len(jobs) == 1
    payload = jobs[0]["payload"]
    assert payload["squads"] == [SQ_NL, SQ_DE] and payload["total"] == 4  # type: ignore[index]
    assert payload["chat_id"] == ADMIN and jobs[0]["queue"] == "panel" and jobs[0]["lane"] == "background"  # type: ignore[index]
    rows = await audit(env)
    assert (
        rows[-1]["action"] == "plan.squads_apply" and rows[-1]["actor_id"] == env.users.by_tg[ADMIN].user_id
    )
    assert rows[-1]["details"] == {"from": [SQ_NL], "to": [SQ_NL, SQ_DE], "subscribers": 4, "manual": 1}
    # the old confirm button again (double click / an old message): the plan version moved on
    await env.click(ADMIN, yes)
    assert env.toasts[-1] == "Тариф уже изменили — откройте его заново"
    assert len(await catalog_jobs(env)) == 1
    # the job itself
    queue = JobQueue(env.db)
    job = next(j for j in await queue.claim("background", "w", 10) if j.kind == KIND)
    await ApplySquadsJob(env.db).run(job, JobContext(db=env.db, queue=queue, worker_id="w"))
    for sid in subs:
        row = (await env.db.raw("select desired_squads from subscriptions where id = $1", sid))[0]
        assert row["desired_squads"] == [SQ_NL, SQ_DE]
    row = (await env.db.raw("select desired_squads from subscriptions where id = $1", manual))[0]
    assert row["desired_squads"] == [SQ_NL]


async def test_no_keeps_current_subscriptions(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    sid = await add_sub(env.db, pid)
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    await env.press(OWNER, "DE")
    await env.press(OWNER, "NL")
    await env.press(OWNER, "Сохранить")
    await env.press(OWNER, "Нет, только новым")
    assert "Текущие подписки оставлены как есть" in env.text and "Сквады: DE" in env.text
    assert await catalog_jobs(env) == []
    assert (await audit(env))[-1]["action"] == "plan.squads_keep"
    row = (await env.db.raw("select desired_squads from subscriptions where id = $1", sid))[0]
    assert row["desired_squads"] == [SQ_NL]
    await env.catalog.reload()
    assert env.catalog.snapshot.plan(pid).squads == (SQ_DE,)  # type: ignore[union-attr]


async def test_cancel_returns_to_the_selection(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await add_sub(env.db, pid)
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    await env.press(OWNER, "DE")
    await env.press(OWNER, "Сохранить")
    await env.press(OWNER, "Отмена")
    assert "Выбрано: 2" in env.text
    assert await catalog_jobs(env) == [] and (await env.catalog.reload()).plan(pid).squads == (SQ_NL,)  # type: ignore[union-attr]


async def test_concurrent_edit_makes_the_confirmation_stale(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await add_sub(env.db, pid)
    await env.catalog.reload()
    await open_squads(env, ADMIN, pid)
    await env.press(ADMIN, "DE")
    await env.press(ADMIN, "Сохранить")
    yes = env.button("Да, применить")
    await env.click(OWNER, encode(ACTIONS, "trf", str(pid)))  # the owner edits the plan meanwhile
    await env.type(OWNER, "50")
    await env.click(ADMIN, yes)
    assert env.toasts[-1] == "Тариф уже изменили — откройте его заново"
    assert await catalog_jobs(env) == []
    assert (await env.catalog.reload()).plan(pid).squads == (SQ_NL,)  # type: ignore[union-attr]


async def test_locations_changed_between_clicks(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    toggle_de = env.button("DE")
    save = env.button("Сохранить")
    await add_location(env.db, SQ_FI, "FI", sort=0)  # shifts every checkbox position
    await env.catalog.reload()
    await env.click(OWNER, toggle_de)
    assert "Список локаций обновился — отметьте заново" in env.text and "Выбрано: 1" in env.text
    await env.click(OWNER, save)
    assert env.toasts[-1] == "Список локаций обновился — отметьте заново"
    assert "Выбрано: 1" in env.text
    assert (await env.catalog.reload()).plan(pid).squads == (SQ_NL,)  # type: ignore[union-attr]


async def test_forged_confirmation_arguments(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await env.catalog.reload()
    sig = env.catalog.snapshot.locations_signature()
    for arg in (f"{pid}:3:{sig}", f"{pid}:zz:{sig}:1", f"{pid}:3:000000:1", f"999:3:{sig}:1", "x"):
        await env.click(OWNER, encode(ACTIONS, "sqy", arg))
    assert await catalog_jobs(env) == []
    assert (await env.catalog.reload()).plan(pid).squads == (SQ_NL,)  # type: ignore[union-attr]
    await env.click(OWNER, encode(ACTIONS, "sqy", f"{pid}:0:{sig}:1"))  # nothing selected
    assert env.toasts[-1] == "Выберите хотя бы одну локацию"


async def test_all_subscribers_manual_enqueues_nothing(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_NL,))
    await add_sub(env.db, pid, manual=True)
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    await env.press(OWNER, "DE")
    await env.press(OWNER, "Сохранить")
    await env.press(OWNER, "Да, применить к 0")
    assert "сквады правили вручную" in env.text and await catalog_jobs(env) == []


async def test_saving_existing_squads_repairs_a_broken_plan(env: PEnv) -> None:
    pid = await add_plan(env.db, "std", squads=(SQ_FI,), broken_reason=BROKEN_PREFIX + "FI")
    await env.attention.raise_item(attention_key(pid), "error", "Тариф скрыт")
    await env.catalog.reload()
    await open_squads(env, OWNER, pid)
    assert "▫️ NL" in env.labels() and not any("FI" in lb for lb in env.labels())  # unknown squad: not offered
    data = env.button("NL")
    arg = decode(data).arg  # type: ignore[union-attr]
    mask = env.catalog.snapshot.mask_of([SQ_NL])
    assert arg.split(":")[1] == f"{mask:x}"
    await env.click(OWNER, data)
    await env.press(OWNER, "Сохранить")
    assert "🟢 В продаже" in env.text and "Скрыт" not in env.text
    rows = await env.db.raw(
        "select resolved_at from attention_items where dedup_key = $1", attention_key(pid)
    )
    assert rows[0]["resolved_at"] is not None


async def test_sync_from_the_squads_screen_returns_there(db: CountingDatabase) -> None:
    from tests.catalog.kit import squad

    env = await build_penv(db)
    await env.staff()
    pid = await add_plan(db, "std", squads=(SQ_NL,), enabled=False)
    await env.catalog.reload()
    env.source.squads = [squad(SQ_NL, "NL"), squad(SQ_DE, "DE", 1)]
    await open_squads(env, OWNER, pid)
    assert "Локаций пока нет" in env.text
    await env.press(OWNER, "Обновить из панели")
    assert "Обновлено: локаций 2" in env.text and "✅ NL" in env.labels() and "▫️ DE" in env.labels()
