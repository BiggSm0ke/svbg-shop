"""Admin-chat and owner cards built as reports: the folded block and HTML spans of the builder, IP Guard
cards, the manual receipt card, broadcast progress and the panel import summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from svbg.app import import_summary
from svbg.billing.receipts import ReceiptView
from svbg.broadcasts.sender import progress_report, progress_text
from svbg.ext.ip_guard import texts
from svbg.tg.admin.receipts import receipt_report
from svbg.tg.report import Report, Span, code, from_html

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
WHO = "Иван &lt;3 (@ivan, id <code>501</code>)"


def kinds(report: Report) -> list[str]:
    return [b["type"] for b in report.rich().model_dump(exclude_none=True)["blocks"]]


# ------------------------------------------------------------------------------------------- builder


def test_from_html_keeps_bold_code_links_and_unescapes() -> None:
    assert from_html("plain") == "plain"
    assert from_html(WHO) == ["Иван <3 (@ivan, id ", Span("code", "501"), ")"]
    assert from_html('<b>a <i>b</i></b> <a href="https://x.y">c</a><br>&amp;') == [
        Span("b", "a "),
        Span("i", "b"),
        " ",
        Span("url", "c", "https://x.y"),
        "\n",
        "&",
    ]


def test_details_rich_and_html() -> None:
    rep = Report("🚨", "Ошибка").details("Стек", ["File <a>", "line 2"], mono=True)
    rep.details("IP", [[code("1.2.3.0/24"), " — нода"]]).details("Пусто", ["", " "])
    blocks = rep.rich().model_dump(exclude_none=True)["blocks"]
    assert [b["type"] for b in blocks] == ["heading", "details", "details"]  # an empty one is skipped
    assert blocks[1]["summary"] == "Стек" and blocks[1]["blocks"] == [
        {"type": "pre", "text": "File <a>\nline 2"}
    ]
    assert blocks[2]["blocks"][0]["text"] == [{"type": "code", "text": "1.2.3.0/24"}, " — нода"]
    html = rep.html()
    assert "<blockquote expandable><b>Стек</b>\nFile &lt;a&gt;\nline 2</blockquote>" in html
    assert "<blockquote expandable><b>IP</b>\n<code>1.2.3.0/24</code> — нода</blockquote>" in html
    assert "Стек\nFile <a>" in rep.plain_text()


# ------------------------------------------------------------------------------------------- IP Guard


def block_row(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "reason": "auto",
        "subscription_id": 12,
        "ip_count": 30,
        "subnet_count": 9,
        "live_ip_count": 7,
        "status": "active",
        "events": [{"kind": "dropped", "nodes": 2}, {"kind": "notify", "state": "sent"}],
        "confirmed_at": None,
        "evidence": {"top": [{"key": "1.2.3.0/24", "nodes": ["n1"]}], "total": 3},
    }
    row.update(kw)
    return row


def test_ip_guard_block_card() -> None:
    card = texts.block_card(
        block_row(),
        person=WHO,
        window=10,
        frozen_left=3 * 86400,
        panel_disabled=True,
        by_names={},
        node_names={"n1": "Германия"},
    )
    assert card.headline == "🚨 Подписка заблокирована" and card.subtitle == "блок: авто"
    html = card.html()
    assert "Кто: Иван &lt;3 (@ivan, id <code>501</code>)" in html
    assert "Подписка: <b>№12</b>" in html and "IP за 10 мин: <b>30</b>" in html
    assert "<b>🛡 Действия</b>\nБот: <b>✅ подписка заморожена</b>" in html
    assert (
        "Соединения: <b>✅ разорваны на 2 нодах</b>" in html and "Пользователь: <b>✅ уведомлён</b>" in html
    )
    assert (
        "<blockquote expandable><b>Самые активные IP</b>\n<code>1.2.3.0/24</code> — Германия\n…и ещё 2"
        in html
    )
    assert kinds(card) == ["heading", "paragraph", "table", "heading", "table", "details"]


def test_ip_guard_unblocked_and_closed_cards() -> None:
    common: dict[str, Any] = {"person": WHO, "window": 10, "frozen_left": 0, "panel_disabled": True}
    done = texts.block_card(
        block_row(
            status="unblocked",
            outcome="active",
            frozen_seconds=7200,
            new_paid_until=T0,
            unblock_mode="revoke",
            unblocked_at=T0,
        ),
        by_names={"unblocked": "Аня"},
        **common,
    ).html()
    assert "<b>🔓 Разблокирована</b>\nКто решил: <b>Аня</b>" in done
    assert "Итог: <b>возвращено 2 ч, активна до 02.10.2026 12:00 + новая ссылка</b>" in done
    closed = texts.block_card(block_row(status="closed", closed_at=T0), by_names={}, **common).html()
    assert "<b>🗑 Блок закрыт</b>\nКто решил: <b>система</b>" in closed


def test_ip_guard_alert_anomaly_and_digest_cards() -> None:
    warn = texts.warning_card(
        {"kind": "pool", "reason": "pool", "subscription_id": None, "metrics": {"ip_count": 5}},
        person="аккаунт панели abc",
        window=10,
        thresholds={"warn": 10, "block": 20, "subnets": 3, "live": 4},
        acked_by=None,
    )
    assert warn.headline == "ℹ️ Не блокирую"
    assert "Почему: <b>мало подсетей — похоже на NAT оператора</b>" in warn.html()
    assert "<i>Пороги: предупреждение 10, блок 20, подсетей от 3, живых от 4</i>" in warn.html()
    anomaly = texts.anomaly_card(
        {
            "metrics": {"trigger": "per_run", "trigger_count": 4},
            "quarantine_until": T0 + timedelta(hours=1),
            "members": {"p1": {"ip": 40, "state": "pending"}, "p2": {"ip": 55, "state": "blocked"}},
        },
        now=T0,
        people={"p1": "<b>Оля</b>"},
        params={"max_run": 3},
    )
    assert kinds(anomaly)[-2:] == ["heading", "table"]
    rows = anomaly.rich().model_dump(exclude_none=True)["blocks"][-1]["cells"]
    assert [c["text"] for c in rows[0]] == ["Кто", "IP", "Статус"]
    assert [c["text"] for c in rows[1]] == ["панель p2", "55", "🚫 заблокирован"]  # most IPs first
    assert rows[2][0]["text"] == {"type": "bold", "text": "Оля"}
    digest = texts.digest_card({"members": {"a": {"sub": 7, "ip": 12, "kind": "warn"}}}).html()
    assert digest.startswith("🧾 <b>Ещё 1 предупреждений за проход</b>")
    assert "<pre>Подписка IP Почему" in digest and "№7       12 предупреждение" in digest


# ------------------------------------------------------------------------------------------- receipts


def test_receipt_card_and_decisions() -> None:
    receipt = ReceiptView(
        id=5,
        payment_id="pay-1",
        user_id=9,
        amount_minor=17900,
        currency="RUB",
        file_id=None,
        comment="перевёл <в 12:00>",
        status="submitted",
        card_ref=None,
    )
    card = receipt_report(receipt, WHO)
    html = card.html()
    assert html.startswith("🧾 <b>Чек ручной оплаты</b>")
    assert "Клиент: Иван &lt;3 (@ivan, id <code>501</code>)" in html and "Счёт: <b>179\xa0₽</b>" in html
    assert "Платёж: <code>pay-1</code>" in html and "Комментарий: перевёл &lt;в 12:00&gt;" in html
    assert html.endswith(
        "<i>Проверьте поступление. Если сумма по чеку другая, ответьте на это сообщение суммой из чека.</i>"
    )
    ok = receipt_report(receipt, WHO, outcome="confirmed", by="@anna", value="179 ₽").html()
    assert "<b>✅ Подтверждено</b>\nКто решил: <b>@anna</b>\nСумма по чеку: <b>179 ₽</b>" in ok
    assert "Проверьте поступление" not in ok
    bad = receipt_report(receipt, WHO, outcome="mismatch", by="@anna", value="100 ₽").html()
    assert "<b>⚠️ Сумма не совпала</b>" in bad and bad.endswith("<i>Не зачислено, решите вручную.</i>")
    no = receipt_report(receipt, WHO, outcome="rejected", by="@anna", value="деньги не поступили").html()
    assert "Причина: <b>деньги не поступили</b>" in no
    assert kinds(card) == ["heading", "table", "paragraph"]


# ------------------------------------------------------------------------------------------- broadcast


def test_broadcast_progress() -> None:
    bc = SimpleNamespace(
        id=12, status="running", total=5000, processed=1234, sent=1200, failed=3, blocked=31, pin=False
    )
    text = progress_text(bc, rate=10)  # type: ignore[arg-type]
    assert text.startswith("📣 <b>Рассылка #12</b>\n<i>▶️ идёт</i>\n\n▓▓░░░░░░░░ 24%\n\n")
    assert "Отправлено: <b>1 200 из ~5 000</b>\nОшибок: <b>3</b>\nЗаблокировали бота: <b>31</b>" in text
    assert "Осталось: <b>≈ 7 мин</b>" in text
    paused = progress_report(SimpleNamespace(**{**vars(bc), "status": "paused"}))  # type: ignore[arg-type]
    assert "Осталось" not in paused.html() and "Продолжить" in paused.html()


# ------------------------------------------------------------------------------------------- import


def test_import_summary() -> None:
    report = SimpleNamespace(
        total=1200, subscriptions_created=11, users_created=7, already_linked=3, conflicts=0
    )
    rep = import_summary("apply", report)
    assert rep.headline == "📥 Импорт из панели" and rep.subtitle == "запись"
    assert "Пользователей в панели: <b>1 200</b>\nСоздано подписок: <b>11</b>" in rep.html()
    assert "Конфликтов: <b>0</b>" in rep.html()
    assert import_summary("dry_run", report).subtitle == "проверка без записи"
