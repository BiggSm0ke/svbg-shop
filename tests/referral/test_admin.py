"""Admin read models: overview counters, dry run after an import, the daily report section."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.referral.kit import Env, add_purchase, add_sub, add_trial, add_user, bind, grant_rows


async def test_overview_dry_run_and_report(env: Env) -> None:
    inviter = await add_user(env.db)
    await grant_rows(env.db, inviter, 2)
    trial_only, paid, idle = await add_user(env.db), await add_user(env.db), await add_user(env.db)
    for friend in (trial_only, paid, idle):
        await bind(env.db, friend, inviter)
    await add_trial(env.db, trial_only)
    await add_purchase(env.db, await add_sub(env.db, paid))
    # trial_or_paid: two pairs would be rewarded now; paid: one; register: all three.
    assert await env.svc.dry_run() == 2
    env.cfg["REFERRAL_TRIGGER"] = "paid"
    assert await env.svc.dry_run() == 1
    env.cfg["REFERRAL_TRIGGER"] = "register"
    assert await env.svc.dry_run() == 3
    overview = await env.svc.overview()
    assert overview == {"pairs": 5, "granted": 2, "days": 28, "deferred": 0, "expired": 0, "money": 0}
    since = datetime.now(UTC) - timedelta(days=2)
    assert await env.svc.report_lines(since) == ["🤝 Рефералка: выдано 28 дн. (2 награды)"]
    assert await env.svc.report_lines(datetime.now(UTC)) == []


def test_plural() -> None:
    from svbg.referral.service import _AWARDS, _plural

    assert [_plural(n, _AWARDS) for n in (1, 2, 5, 11, 12, 21, 22, 25, 101, 111)] == [
        "награда",
        "награды",
        "наград",
        "наград",
        "наград",
        "награда",
        "награды",
        "наград",
        "награда",
        "наград",
    ]
