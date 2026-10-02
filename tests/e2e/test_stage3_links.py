"""Stage 3 end to end — deep links (07 §2.4.4, stage3-contracts «Acceptance»).

* an ``l_<code>`` short link with a promo code posted in the channel: a new user → the required channel
  check → the plan with the discount at checkout → paid with the discount, the use and the hit recorded;
* old Bedolaga links keep working: a bare campaign code and ``ref<code>``.
"""

from __future__ import annotations

import pytest

from svbg.core.settings import Change
from svbg.deeplinks.model import Intent
from svbg.promo.service import Actor
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import open_shop, until_async
from tests.e2e.test_stage3_kit import tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg

CHANNEL = -1_009_000_000_077


async def test_short_link_with_a_channel_promo_reaches_the_checkout(
    start_app: StartApp, app_env: AppEnv
) -> None:
    extra: dict[str, str | None] = {
        "REQUIRED_CHANNEL_ID": str(CHANNEL),
        "REQUIRED_CHANNEL_URL": "https://t.me/svbg_channel",
        "CHANNEL_REQUIRED_FOR": "all",
    }
    async with open_shop(start_app, app_env, extra_env=extra) as shop:
        app = shop.app
        assert app.promo is not None and app.deeplinks is not None
        owner = shop.person(OWNER_ID)
        await owner.start()
        promo = await app.promo.create(
            Actor(await shop.user_id(OWNER_ID), "owner", OWNER_ID),
            kind="percent",
            code="CHAN20",
            values={"percent": 20},
        )
        link = await app.deeplinks.create_link(
            Intent(plan="standard", promo="CHAN20"), title="Пост в канале", actor=None
        )

        # a new user who is not in the channel yet
        shop.tg.chat_members[(CHANNEL, 5_301)] = "left"
        vera = shop.person(5_301)
        await vera.start(f"l_{link.code}")
        await vera.wait_text("Подпишитесь")
        shop.tg.chat_members[(CHANNEL, 5_301)] = "member"
        await vera.press(
            "Я подписался", expect="Выберите срок", timeout=20
        )  # the link resumes: the plan screen
        await shop.fund(5_301, 20_000)
        await vera.press("1 мес.", expect="CHAN20")
        assert "−20 %" in vera.text()
        uid = await shop.user_id(5_301)
        pay = next(b for b in vera.buttons() if "Оплатить" in str(b.get("text")))
        await vera.click(str(pay["callback_data"]), expect="✅ Оплачено")

        order = (await shop.rows("select total_minor, status, snapshot from orders where user_id = $1", uid))[
            0
        ]
        assert order["status"] == "fulfilled" and order["total_minor"] < 17_900

        async def used() -> bool:
            rows = await shop.rows("select count(*) as n from promo_uses where promo_id = $1", promo.id)
            return rows[0]["n"] == 1

        await until_async(used, what="promo use recorded")
        hits = await shop.rows(
            "select h.kind, h.is_new from deeplink_hits h where h.link_id = $1 and h.user_id = $2",
            link.id,
            uid,
        )
        assert hits == [{"kind": "link", "is_new": True}]
        uses = await shop.rows("select uses from deeplinks where id = $1", link.id)
        assert uses[0]["uses"] == 1
        await shop.assert_wallet_invariants()


async def test_old_bedolaga_campaign_and_referral_links(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env) as shop:
        app = shop.app
        assert app.settings is not None and app.ads is not None
        await app.settings.apply([Change("REFERRAL_ENABLED", "true")], source="bot", actor_id=None)
        inviter = shop.person(5_310)
        await inviter.start()
        inviter_id = await shop.user_id(5_310)
        await shop.db.raw(
            "insert into referral_codes (user_id, code, source) values ($1, 'Bd7xK2', 'import')", inviter_id
        )
        await shop.db.raw(
            "insert into ad_links (code, title, source, legacy_id)"
            " values ('vk_spring', 'VK весна', 'import', '17')"
        )
        await app.ads.load()

        # a bare campaign code from Bedolaga (no prefix) → the ad tag
        camp = shop.person(5_311)
        await camp.start("vk_spring")
        assert "Привет" in camp.text()
        tagged = await shop.rows(
            "select l.code from ad_link_users u join ad_links l on l.id = u.ad_link_id where u.user_id = $1",
            await shop.user_id(5_311),
        )
        assert [r["code"] for r in tagged] == ["vk_spring"]

        # ref<code> (Bedolaga's referral form, no underscore) → the referrer
        friend = shop.person(5_312)
        await friend.start("refBd7xK2")
        friend_id = await shop.user_id(5_312)

        async def bound() -> bool:
            rows = await shop.rows("select referrer_id from referrals where referred_user_id = $1", friend_id)
            return [r["referrer_id"] for r in rows] == [inviter_id]

        await until_async(bound, what="the legacy referral link binds the inviter")
        kinds = await shop.rows("select kind from deeplink_hits order by id")
        assert [r["kind"] for r in kinds] == ["ad_code", "legacy_ref"]
