"""Stage 4 end to end — referral days (05 §2.3): the invitee takes the trial → +days to both sides."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest

from svbg.core.settings import Change
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_stage2_kit import open_shop, until, until_async
from tests.e2e.test_stage3_kit import chat, tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg


async def test_invitee_trial_gives_days_to_both_sides(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        app = shop.app
        assert app.settings is not None
        await app.settings.apply([Change("REFERRAL_ENABLED", "true")], source="bot", actor_id=None)
        snap = app.settings.current()
        inviter_days, invitee_days = int(snap["REFERRAL_INVITER_DAYS"]), int(snap["REFERRAL_INVITEE_DAYS"])
        assert snap["REFERRAL_MODE"] == "days" and snap["REFERRAL_TRIGGER"] == "trial_or_paid"

        # the inviter has a paid month and shares the link from «🤝 Пригласить»
        ivan = chat(shop, 5_801)
        await ivan.start()
        await shop.fund(5_801, 17_900)
        await ivan.press("Подписка", expect="📱 Подписка")
        await ivan.press("Купить подписку", expect="Выберите срок")
        await ivan.press("1 мес.", expect="Спишем с баланса")
        await ivan.press("Оплатить 179")
        await ivan.wait_text("✅ Оплачено! Подписка", timeout=20)
        ivan_id = await shop.user_id(5_801)
        before = (await shop.rows("select paid_until from subscriptions where user_id = $1", ivan_id))[0]
        await ivan.start()
        await ivan.press("Пригласить", expect="start=r_")
        found = re.search(r"start=(r_[A-Za-z0-9_-]+)", ivan.text())
        assert found is not None
        payload = found[1]

        # the invitee comes by the link and takes the trial
        olga = chat(shop, 5_802)
        await olga.start(payload)
        olga_id = await shop.user_id(5_802)
        refs = await shop.rows("select referrer_id from referrals where referred_user_id = $1", olga_id)
        assert [r["referrer_id"] for r in refs] == [ivan_id]
        await olga.start()
        await olga.press("Попробовать бесплатно")
        await olga.wait_text("Готово! Пробный период до", timeout=20)

        async def rewarded() -> bool:
            rows = await shop.rows("select side, status, days from referral_rewards order by side")
            return [(r["side"], r["status"]) for r in rows] == [
                ("invitee", "granted"),
                ("inviter", "granted"),
            ]

        await until_async(rewarded, timeout=30, what="both referral rewards granted")
        rewards = {r["side"]: r["days"] for r in await shop.rows("select side, days from referral_rewards")}
        assert rewards == {"invitee": invitee_days, "inviter": inviter_days}
        after = (await shop.rows("select paid_until from subscriptions where user_id = $1", ivan_id))[0]
        assert after["paid_until"] - before["paid_until"] == timedelta(days=inviter_days)
        trial = (
            await shop.rows(
                "select is_trial, paid_until, created_at from subscriptions where user_id = $1", olga_id
            )
        )[0]
        assert trial["is_trial"]
        trial_days = int(snap["TRIAL_DAYS"]) if "TRIAL_DAYS" in snap else 3
        span = trial["paid_until"] - trial["created_at"]
        assert span > timedelta(days=trial_days + invitee_days) - timedelta(hours=1)

        # the panel follows (through the writer)
        def panel_expire() -> Any:
            return next((u["expireAt"] for u in shop.panel.users.values() if u["telegramId"] == 5_801), None)

        def synced() -> bool:
            at = panel_expire()
            return at is not None and abs(at - after["paid_until"]) < timedelta(seconds=1)  # panel: ms

        await until(synced, timeout=20, what="the inviter's days in the panel")
