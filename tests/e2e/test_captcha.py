"""Entry captcha end to end: the whole application, the fake Telegram, the real deep links and promo codes.

A new user opens a short link with a plan and a promo code: the hit is recorded and the link waits behind the
captcha; a wrong tap gives a new question in the same message, any other button still leads to the captcha;
the right tap stores ``captcha_passed_at`` and the link goes on (the plan screen, the promo noted). The owner
and a user who passed it once never see it again.
"""

from __future__ import annotations

from typing import Any

import pytest

from svbg.deeplinks.model import Intent
from svbg.promo.service import Actor
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp
from tests.e2e.test_stage2_kit import Person, open_shop, until
from tests.e2e.test_stage3_kit import tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg

QUESTION = "Нажмите на "


def target(person: Person) -> str:
    text = person.text()
    assert QUESTION in text, text[:300]
    return text.rsplit(QUESTION, 1)[1].strip()


def button(person: Person, label: str) -> dict[str, Any]:
    for b in person.buttons():
        if b.get("text") == label:
            return b
    raise AssertionError(f"no button {label!r} in {[b.get('text') for b in person.buttons()]}")


async def tap(person: Person, label: str, *, expect: str | None = None) -> str:
    return await person.click(str(button(person, label)["callback_data"]), expect=expect)


async def test_new_user_link_waits_behind_the_captcha(start_app: StartApp, app_env: AppEnv) -> None:
    async with open_shop(start_app, app_env, extra_env={"CAPTCHA_ENABLED": "true"}) as shop:
        app = shop.app
        assert app.promo is not None and app.deeplinks is not None
        owner = shop.person(OWNER_ID)
        await owner.start()
        await owner.wait_text("Привет")  # the owner never gets the captcha
        await app.promo.create(
            Actor(await shop.user_id(OWNER_ID), "owner", OWNER_ID),
            kind="percent",
            code="CAPT20",
            values={"percent": 20},
        )
        link = await app.deeplinks.create_link(
            Intent(plan="standard", promo="CAPT20"), title="Пост с капчей", actor=None
        )

        anna = shop.person(5_401)
        await anna.start(f"l_{link.code}")
        await anna.wait_text(QUESTION)
        assert "Проверим, что вы не бот" in anna.text()
        first = target(anna)
        labels = [str(b.get("text")) for b in anna.buttons()]
        assert len(labels) == 6 and first in labels
        uid = await shop.user_id(5_401)
        hits = await shop.rows("select kind from deeplink_hits where user_id = $1", uid)
        assert [h["kind"] for h in hits] == ["link"]  # the link is counted, its intent waits

        # a wrong tap: a toast and another question in the same message
        main = anna.main
        toast = await tap(anna, next(label for label in labels if label != first))
        assert toast == "Не то. Попробуйте ещё раз"
        await until(lambda: target(anna) != first, what="a new question")
        assert anna.main == main

        # any other button (an old menu button, a broadcast) leads to the captcha too
        second = target(anna)
        await anna.click("v1:bal:o")
        assert target(anna) == second and "Баланс:" not in anna.text()

        # the right tap: passed, and the link goes on with its plan and promo code
        toast = await tap(anna, second, expect="Выберите срок")
        assert "CAPT20" in toast
        rows = await shop.rows("select captcha_passed_at from users where id = $1", uid)
        assert rows[0]["captcha_passed_at"] is not None

        # next time straight to the menu
        await anna.start()
        await anna.wait_text("Привет")
        assert QUESTION not in anna.text()
