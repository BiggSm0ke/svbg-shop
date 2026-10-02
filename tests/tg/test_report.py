"""Report cards: rich blocks (tables with align/valign), the HTML fallback and monospace widths."""

from __future__ import annotations

import html
import re

import pytest

from svbg.tg.report import (
    MAX_HTML,
    PRE_WIDTH,
    Report,
    RichGate,
    b,
    cell_width,
    code,
    link,
    money,
    num,
    pre_table,
)

NB = "\N{NO-BREAK SPACE}"
THIN = "\N{NARROW NO-BREAK SPACE}"


def _pre_lines(text: str) -> list[str]:
    blocks = re.findall(r"<pre>(.*?)</pre>", text, re.S)
    return [line for block in blocks for line in html.unescape(block).split("\n")]


def sample() -> Report:
    return (
        Report("💰", "Пополнение 1 500 ₽")
        .line("Клиент", ["Аня (@anya, id ", code(123), ")"])
        .line("Способ", "СБП")
        .line("Пусто", "")
        .section("По кассам")
        .table(
            ["Касса", "Оплат", "Сумма"],
            [["RollyPay", num(12), money(358800, "RUB")], ["Карта <тест>", num(3), money(897000, "RUB")]],
            "lrr",
            total=["Итого", num(15), money(1255800, "RUB")],
        )
        .footer("собрано 02.10 09:00")
    )


def test_rich_shape() -> None:
    rich = sample().rich(banner="AgADbanner", header="💳 Оплаты")
    blocks = rich.model_dump(mode="json", exclude_none=True)["blocks"]
    assert [x["type"] for x in blocks] == [
        "photo",
        "paragraph",
        "heading",
        "table",
        "heading",
        "table",
        "footer",
    ]
    assert blocks[0]["photo"] == {"type": "photo", "media": "AgADbanner"}
    assert blocks[1]["text"] == {"type": "italic", "text": "💳 Оплаты"}
    assert blocks[2] == {"type": "heading", "text": "💰 Пополнение 1 500 ₽", "size": 3}

    kv = blocks[3]
    assert kv["is_compact"] is True and "is_striped" not in kv
    (who_key, who), (way_key, way) = kv["cells"]
    assert who_key == {"align": "left", "valign": "middle", "text": "Клиент"}
    assert who["text"] == ["Аня (@anya, id ", {"type": "code", "text": "123"}, ")"]
    assert way["text"] == {"type": "bold", "text": "СБП"}, "plain values are bold"
    assert way_key["text"] == "Способ"

    table = blocks[5]
    assert table["is_compact"] is True and table["is_striped"] is True
    head, first, _, total = table["cells"]
    assert [(c["text"], c["align"], c["valign"], c["is_header"]) for c in head] == [
        ("Касса", "left", "middle", True),
        ("Оплат", "right", "middle", True),
        ("Сумма", "right", "middle", True),
    ]
    assert [c["text"] for c in first] == ["RollyPay", "12", f"3{THIN}588{NB}₽"]
    assert all("is_header" not in c for c in first)
    assert total[2]["text"] == {"type": "bold", "text": f"12{THIN}558{NB}₽"}
    assert blocks[6] == {"type": "footer", "text": "собрано 02.10 09:00"}


def test_rich_without_banner_and_long_tables() -> None:
    rep = Report("📊", "Отчёт").table(["Кто", "Шт"], [[f"u{n}", n] for n in range(30)], max_rows=5)
    blocks = rep.rich().model_dump(mode="json", exclude_none=True)["blocks"]
    assert blocks[0]["type"] == "heading"
    cells = blocks[1]["cells"]
    assert len(cells) == 1 + 5 + 1
    assert cells[-1] == [
        {"align": "left", "valign": "middle", "colspan": 2, "text": {"type": "italic", "text": "… и ещё 25"}}
    ]


def test_rich_refuses_past_the_block_limit() -> None:
    rep = Report("📊", "Много")
    for n in range(30):
        rep.table(["a", "b"], [[str(i), str(i)] for i in range(19)])
    with pytest.raises(ValueError, match="too big"):
        rep.rich()
    assert rep.html().startswith("📊 <b>Много</b>"), "the text form still works"


def test_html_fallback() -> None:
    text = sample().html(header="💳 Оплаты")
    assert text.startswith("<i>💳 Оплаты</i>\n💰 <b>Пополнение 1 500 ₽</b>\n\n")
    assert "Клиент: Аня (@anya, id <code>123</code>)\nСпособ: <b>СБП</b>\n\n" in text
    assert "Пусто" not in text, "empty values are skipped"
    assert "<b>По кассам</b>\n<pre>" in text, "a section title sticks to its table"
    assert "Карта &lt;тест&gt;" in text
    assert text.endswith("<i>собрано 02.10 09:00</i>")
    lines = _pre_lines(text)
    assert lines == [
        "Касса        Оплат    Сумма",
        "──────────── ───── ────────",
        f"RollyPay        12  3{NB}588{NB}₽",
        f"Карта <тест>     3  8{NB}970{NB}₽",
        "──────────── ───── ────────",
        f"Итого           15 12{NB}558{NB}₽",
    ], "thin spaces become no-break spaces in monospace"
    assert all(cell_width(line) <= PRE_WIDTH for line in lines)


def test_pre_shrinks_cyrillic_and_emoji_to_a_phone() -> None:
    rows = [
        ["Тариф", "Шт", "Сумма"],
        ["Год безлимит семейный на пятерых", "3", "8 970 ₽"],
        ["🔥 Акция недели", "41", "0 ₽"],
    ]
    out = pre_table(rows, ["left", "right", "right"])
    assert out is not None
    lines = html.unescape(out.removeprefix("<pre>").removesuffix("</pre>")).split("\n")
    assert max(cell_width(line) for line in lines) == PRE_WIDTH
    assert lines[2].startswith("Год безлимит семейны…  3") and lines[2].endswith(" 3 8 970 ₽")
    assert lines[3].startswith("🔥 Акция недели ")
    widths = {cell_width(line.rstrip()) for line in lines[1:]}
    assert widths == {PRE_WIDTH}, "every row lines up at the right edge"


def test_too_wide_tables_turn_into_lines() -> None:
    rep = Report("📈", "Продажи").table(
        ["Тариф", "Новые", "Продления", "Выручка"],
        [["Месяц", "12", "40", "12 345 678 ₽"], ["Год", "1", "2", "999 999 ₽"]],
    )
    text = rep.html()
    assert "<pre>" not in text
    assert "<b>Месяц</b> · Новые 12 · Продления 40 · Выручка 12 345 678 ₽" in text
    two = Report("📈", "Топ").table(["Реклама", "Пришло"], [["канал <1>", "15"]]).html()
    assert "канал &lt;1&gt;: <b>15</b>" in two and "<pre>" not in two


def test_html_respects_the_limit() -> None:
    rep = Report("🧾", "Длинный")
    for n in range(40):
        rep.section(f"Раздел {n}").table(
            ["Кто", "Шт", "Сумма"], [[f"имя {i}" * 3, i, "1 ₽"] for i in range(30)]
        )
    text = rep.html()
    assert len(text.encode("utf-16-le")) // 2 <= MAX_HTML
    assert text.startswith("🧾 <b>Длинный</b>") and text.endswith("…")
    assert text.count("<pre>") == text.count("</pre>"), "only whole parts are dropped"


def test_inline_spans_and_numbers() -> None:
    rep = Report("🔗", "Ссылки").text(["см. ", link("панель", "https://x.test/?a=1&b=2"), " и ", b("жирное")])
    assert '<a href="https://x.test/?a=1&amp;b=2">панель</a> и <b>жирное</b>' in rep.html()
    para = rep.rich().model_dump(mode="json", exclude_none=True)["blocks"][1]
    assert para["text"][1] == {"type": "url", "text": "панель", "url": "https://x.test/?a=1&b=2"}
    assert num(1234567) == f"1{THIN}234{THIN}567" and num(-1500) == f"−1{THIN}500" and num(12) == "12"
    assert money(100, "XTR") == f"100{NB}⭐"
    assert money(123456, "USD").endswith("$") or "$" in money(123456, "USD")


def test_cell_widths() -> None:
    assert cell_width("Привет") == 6
    assert cell_width("₽…─") == 3
    assert cell_width("🔥") == 2 and cell_width("⭐") == 2
    assert cell_width("👍🏽") == 2, "a skin tone adds nothing"
    assert cell_width("👨‍👩‍👧") == 2, "a ZWJ family is one glyph"
    assert cell_width("🇷🇺") == 2
    assert cell_width("й") == 1, "a combining breve adds nothing"
    assert cell_width("❤️") == 2


def test_table_validation() -> None:
    with pytest.raises(ValueError, match="cells"):
        Report("x", "y").table(["a", "b"], [["1"]])
    with pytest.raises(ValueError, match="align"):
        Report("x", "y").table(["a", "b"], [["1", "2"]], "lx")
    assert Report("x", "y").table(["a"], []).parts == [], "an empty table is not shown"


def test_rich_gate_remembers_for_a_while() -> None:
    now = [0.0]
    gate = RichGate(ttl=60, clock=lambda: now[0])
    assert gate.ok(1) and gate.banner_ok(1)
    gate.off(1, "Bad Request: unknown method")
    gate.banner_off(2)
    assert not gate.ok(1) and gate.ok(2) and not gate.banner_ok(2)
    now[0] = 61
    assert gate.ok(1) and gate.banner_ok(2)
