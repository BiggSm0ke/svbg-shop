"""The «Пригласить» screen data (one SQL), reward lines from the same rules, the QR code."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from svbg.referral.qr import qr_png
from svbg.referral.rules import Rules, Trigger
from svbg.referral.service import ReferralService
from tests.referral.kit import Env, add_sub, add_user, bind, grant_rows


async def test_code_is_created_once_in_one_statement(env: Env) -> None:
    uid = await add_user(env.db)
    await add_user(env.db)  # warm the pool
    before = env.db.queries
    view = await env.svc.invite_view(uid)
    assert env.db.queries - before == 1
    assert view.enabled and view.code and len(view.code) == 10
    assert view.link == f"https://t.me/svbg_shop_bot?start=r_{view.code}"
    again = await env.svc.invite_view(uid)
    assert again.code == view.code
    assert view.share_url is not None
    query = parse_qs(urlparse(view.share_url).query)
    assert query["url"] == [view.link]
    assert query["text"] == ["🎁 Оформи подписку по этой ссылке и получишь 7 дн. в подарок!"]


async def test_counters(env: Env) -> None:
    uid = await add_user(env.db)
    await grant_rows(env.db, uid, 3)  # three friends, rewarded (14 days each)
    friends = await env.db.raw("select referred_user_id from referrals where referrer_id = $1", uid)
    await add_sub(env.db, friends[0]["referred_user_id"])
    stranger = await add_user(env.db)
    await bind(env.db, stranger, uid)
    view = await env.svc.invite_view(uid)
    assert (view.invited, view.subscribed, view.conversion, view.days_earned) == (4, 1, 25, 42)
    assert "👥 Приглашено: <b>4</b>\nОформили подписку: <b>1</b> (25%)" in view.lines


async def test_code_collision_is_retried(env: Env) -> None:
    first, second = await add_user(env.db), await add_user(env.db)
    codes = iter(["samecode01", "samecode01", "othercode2"])
    env.svc._code_factory = lambda: next(codes)  # type: ignore[method-assign]
    assert (await env.svc.invite_view(first)).code == "samecode01"
    assert (await env.svc.invite_view(second)).code == "othercode2"


async def test_off_costs_nothing(env: Env) -> None:
    env.cfg["REFERRAL_ENABLED"] = False
    uid = await add_user(env.db)
    before = env.db.queries
    view = await env.svc.invite_view(uid, "en")
    assert env.db.queries == before
    assert not view.enabled and view.lines == ("Реферальная программа сейчас выключена.",)


async def test_no_bot_username_no_link(env: Env) -> None:
    env.svc._bot_username = lambda: None  # type: ignore[method-assign]
    view = await env.svc.invite_view(await add_user(env.db))
    assert view.code and view.link is None and view.share_url is None


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        (
            Rules(enabled=True),
            [
                "🎁 <b>Как работают награды</b>",
                "• Вы получаете <b>+14 дн.</b> за каждого приглашённого, который попробует бесплатно или "
                "оформит подписку",
                "• Приглашённый получает <b>+7 дн.</b>",
                "• Не больше 20 наград за 30 дней",
            ],
        ),
        (
            Rules(enabled=True, trigger=Trigger.REGISTER, cap_30d=0, invitee_days=0),
            [
                "🎁 <b>Как работают награды</b>",
                "• Вы получаете <b>+14 дн.</b> за каждого, кто перейдёт по вашей ссылке",
            ],
        ),
        (
            Rules.from_config({"REFERRAL_ENABLED": True, "REFERRAL_MODE": "percent", "REFERRAL_PERCENT": 15}),
            [
                "🎁 <b>Как работают награды</b>",
                "• С каждой покупки приглашённых вам на баланс приходит <b>15%</b>",
            ],
        ),
    ],
)
def test_reward_lines_follow_the_rules(rules: Rules, expected: list[str]) -> None:
    assert ReferralService.reward_lines(rules, "ru") == expected


def test_reward_lines_are_russian_whatever_language_is_passed() -> None:
    lines = ReferralService.reward_lines(Rules(enabled=True, trigger=Trigger.PAID), "en-US")
    assert lines == ReferralService.reward_lines(Rules(enabled=True, trigger=Trigger.PAID))
    assert lines[1].startswith("• Вы получаете <b>+14 дн.</b>")


def test_qr_png() -> None:
    png = qr_png("https://t.me/svbg_shop_bot?start=r_abc")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert qr_png("https://t.me/svbg_shop_bot?start=r_abc") is png  # cached
    for bad in ("javascript:alert(1)", "http://x", "https://" + "a" * 600, ""):
        with pytest.raises(ValueError, match="QR"):
            qr_png(bad)
