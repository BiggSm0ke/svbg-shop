"""Reports in the admin chat: a rich message where Telegram takes it, the HTML text where it does not."""

# ruff: noqa: F811 - the fixtures come from test_admin_chat_service

from __future__ import annotations

import json
from typing import Any

import pytest

from svbg.services import admin_chat as admin_chat_module
from svbg.services.admin_chat import K_PAYMENTS, K_REPORTS
from svbg.tg.report import Report
from tests.fakes.telegram import FakeTelegram
from tests.services.test_admin_chat_service import (  # noqa: F401 - fixtures
    GROUP,
    OWNER,
    Factory,
    bot,
    db,
    make,
    sends_to,
    started,
    tg,
)


def card() -> Report:
    return (
        Report("💰", "Пополнение 500 ₽")
        .line("Клиент", "Аня")
        .table(["Касса", "Оплат", "Сумма"], [["RollyPay", "2", "500 ₽"]], "lrr")
    )


def rich_calls(tg: FakeTelegram, chat_id: int) -> list[Any]:
    return [c for c in tg.calls_for("sendRichMessage") if c.params.get("chat_id") == chat_id]


def blocks(call: Any) -> list[dict[str, Any]]:
    rich = call.params["rich_message"]
    return (json.loads(rich) if isinstance(rich, str) else rich)["blocks"]


@pytest.fixture
def rich_tg(tg: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> FakeTelegram:
    """The fake learns ``sendRichMessage`` (a message without text, like the real one)."""

    async def send_rich(bot: Any, params: Any) -> Any:
        return tg._new_message(bot, params)

    monkeypatch.setattr(tg, "_m_sendrichmessage", send_rich, raising=False)
    return tg


async def test_report_goes_as_a_rich_message_into_its_topic(make: Factory, rich_tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    result = await svc.post_report(K_PAYMENTS, card(), wait=True)
    assert result is not None and result.delivered and result.thread_id == svc.state(K_PAYMENTS).thread_id
    (call,) = rich_calls(rich_tg, GROUP)
    assert call.params["message_thread_id"] == svc.state(K_PAYMENTS).thread_id
    types = [x["type"] for x in blocks(call)]
    assert types == ["heading", "table", "table"]
    head = blocks(call)[2]["cells"][0]
    assert [(c["align"], c["valign"]) for c in head] == [
        ("left", "middle"),
        ("right", "middle"),
        ("right", "middle"),
    ]
    assert sends_to(rich_tg, GROUP) == [], "no text copy"


async def test_old_server_falls_back_to_text_and_remembers(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    await svc.post_report(K_REPORTS, card(), wait=True)  # the fake has no sendRichMessage: 404
    assert len(rich_calls(tg, GROUP)) == 1
    text = sends_to(tg, GROUP)[-1].params
    assert text["parse_mode"] == "HTML" and text["text"].startswith("💰 <b>Пополнение 500 ₽</b>")
    assert "<pre>" in text["text"]
    await svc.post_report(K_REPORTS, card().line("Ещё", "1"), wait=True)
    assert len(rich_calls(tg, GROUP)) == 1, "no second try for a while"
    assert len(sends_to(tg, GROUP)) == 2
    assert svc.down is False and svc.last_error is None, "a refused rich message is not a chat failure"


async def test_old_server_probe_costs_no_group_slot(make: Factory, tg: FakeTelegram) -> None:
    svc = await started(make())
    await svc.ensure_topics()
    before = len(svc._window.stamps)
    await svc.post_report(K_REPORTS, card(), wait=True)
    assert len(rich_calls(tg, GROUP)) == 1
    assert len(svc._window.stamps) == before + 1, "only the text counts against the group budget"
    assert not svc._rich.ok(OWNER), "no such method: rich is off for every chat, not only this one"


async def test_bad_request_falls_back_and_banner_is_dropped_first(
    make: Factory, rich_tg: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def banner(_bot: Any) -> str:
        return "AgADbanner"

    monkeypatch.setattr(admin_chat_module, "banner_media", banner)
    svc = await started(make())
    await svc.ensure_topics()
    rich_tg.fail_next(
        "400",
        method="sendRichMessage",
        description="Bad Request: not enough rights to send photos to the chat",
    )
    await svc.post_report(K_PAYMENTS, card(), wait=True)
    first, second = rich_calls(rich_tg, GROUP)
    assert blocks(first)[0]["type"] == "photo" and blocks(second)[0]["type"] == "heading"
    assert sends_to(rich_tg, GROUP) == []

    await svc.post_report(K_PAYMENTS, card(), wait=True)
    assert blocks(rich_calls(rich_tg, GROUP)[-1])[0]["type"] == "heading", "the banner stays off here"

    rich_tg.fail_next("400", method="sendRichMessage", description="Bad Request: can't parse rich message")
    await svc.post_report(K_PAYMENTS, card(), wait=True)
    assert sends_to(rich_tg, GROUP)[-1].params["text"].startswith("💰 <b>Пополнение")


async def test_owner_dm_gets_the_topic_as_a_header(make: Factory, rich_tg: FakeTelegram) -> None:
    svc = await started(make(chat=None))
    result = await svc.post_report(K_PAYMENTS, card(), wait=True)
    assert result is not None and set(result.dm) == {OWNER}
    (call,) = rich_calls(rich_tg, OWNER)
    first = blocks(call)[0]
    assert first["type"] == "paragraph" and first["text"]["text"] == "💳 Оплаты и пополнения"


async def test_cards_edit_into_rich(
    make: Factory, rich_tg: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def edit_rich(bot: Any, params: Any) -> Any:
        if "rich_message" in params:
            edits.append(params)
            return True
        return await original(bot, params)

    edits: list[Any] = []
    original = rich_tg._m_editmessagetext
    monkeypatch.setattr(rich_tg, "_m_editmessagetext", edit_rich)
    svc = await started(make())
    await svc.ensure_topics()
    one = await svc.post_report(K_REPORTS, card(), card_ref="day", wait=True)
    two = await svc.post_report(K_REPORTS, card().line("Ещё", "1"), card_ref="day", wait=True)
    assert one is not None and two is not None and one.message_id == two.message_id
    assert len(rich_calls(rich_tg, GROUP)) == 1 and len(edits) == 1
    assert edits[0]["message_id"] == one.message_id
