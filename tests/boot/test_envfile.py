from __future__ import annotations

import pytest

from svbg.boot.envfile import (
    ANNOTATION_PREFIX,
    DUPLICATE_PREFIX,
    INVALID_PREFIX,
    EnvDocument,
    EnvLine,
    quote,
    render_section_header,
    section_title,
    unquote,
)

# ------------------------------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("line", "key", "value"),
    [
        ("A=1", "A", "1"),
        ("A = 1", "A", "1"),
        ("  A=1", "A", "1"),
        ("export A=1", "A", "1"),
        ("export\tA=1", "A", "1"),
        ("export=1", "export", "1"),
        ("A=", "A", ""),
        ("A=   ", "A", ""),
        ("A= # just a comment", "A", ""),
        ("A=value # comment", "A", "value"),
        ("A=value\t# comment", "A", "value"),
        ("A=a#b", "A", "a#b"),
        ("A=#notcomment", "A", "#notcomment"),
        ("A=two words", "A", "two words"),
        ("A=a=b=c", "A", "a=b=c"),
        ('A="x # y" # z', "A", "x # y"),
        ("A='x # y' # z", "A", "x # y"),
        ('A="a\\nb\\t\\"q\\" \\$HOME \\\\ \\x"', "A", 'a\nb\t"q" $HOME \\ \\x'),
        ("A='a\\nb $HOME'", "A", "a\\nb $HOME"),
        ('A=""', "A", ""),
        ("A=''", "A", ""),
        ("A=привет мир", "A", "привет мир"),
        ('A="эмодзи 🚀"', "A", "эмодзи 🚀"),
        ("a.b-c=1", "a.b-c", "1"),
        ('A="x"#tight', "A", "x"),
    ],
)
def test_parse_kv(line: str, key: str, value: str) -> None:
    doc = EnvDocument.parse(line + "\n")
    assert [entry.kind for entry in doc.lines] == ["kv"]
    assert doc.lines[0].key == key
    assert doc.get(key) == value
    assert doc.render() == line + "\n"


@pytest.mark.parametrize(
    ("line", "kind", "key"),
    [
        ("", "blank", None),
        ("   \t", "blank", None),
        ("# comment", "comment", None),
        ("   # indented comment", "comment", None),
        ("just text", "invalid", None),
        ("1A=2", "invalid", None),
        ("export A", "invalid", None),
        ('A="unterminated', "invalid", "A"),
        ("A='unterminated", "invalid", "A"),
        ('A="x" trailing', "invalid", "A"),
        ("A='x'trailing", "invalid", "A"),
    ],
)
def test_parse_other_kinds(line: str, kind: str, key: str | None) -> None:
    doc = EnvDocument.parse(line + "\n")
    assert len(doc.lines) == 1
    assert doc.lines[0].kind == kind
    assert doc.lines[0].key == key
    assert doc.lines[0].raw == line
    assert doc.render() == line + "\n"


def test_multiline_double_quoted_value() -> None:
    text = 'A=1\nCERT="line1\nline2\n  line3" # pem\nB=2\n'
    doc = EnvDocument.parse(text)
    assert [line.kind for line in doc.lines] == ["kv", "kv", "kv"]
    assert doc.get("CERT") == "line1\nline2\n  line3"
    assert doc.as_dict() == {"A": "1", "CERT": "line1\nline2\n  line3", "B": "2"}
    assert doc.render() == text


def test_multiline_single_quoted_value_with_crlf_is_normalized() -> None:
    text = "K='a\r\nb'\r\nZ=1\r\n"
    doc = EnvDocument.parse(text)
    assert doc.get("K") == "a\nb"
    assert doc.newline == "\r\n"
    assert doc.render() == text


def test_unterminated_quote_does_not_swallow_the_rest() -> None:
    text = 'A="oops\nB=2\nC=3\n'
    doc = EnvDocument.parse(text)
    assert [line.kind for line in doc.lines] == ["invalid", "kv", "kv"]
    assert doc.get("A") is None
    assert doc.as_dict() == {"B": "2", "C": "3"}
    assert doc.render() == text


def test_closing_quote_with_garbage_on_later_line_is_invalid() -> None:
    text = 'A="one\ntwo" garbage\nB=2\n'
    doc = EnvDocument.parse(text)
    assert doc.lines[0].kind == "invalid"
    assert doc.get("B") == "2"
    assert doc.render() == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "\r\n",
        "\ufeff",
        "\ufeffA=1\r\n",
        "A=1",
        "A=1\nB=2",
        "A=1\r\nB=2\nC=3\r\n",
        "a\rb\n",
        "\n\n\n",
        "# only comment",
        'A="multi\r\nline"\r\n',
        "A=1\r",
        "\ufeff\ufeffA=1\n",
    ],
)
def test_round_trip_edge_cases(text: str) -> None:
    assert EnvDocument.parse(text).render() == text


def test_newline_and_bom_detection() -> None:
    doc = EnvDocument.parse("\ufeffA=1\r\nB=2\n")
    assert doc.bom is True
    assert doc.newline == "\r\n"
    assert doc.lines[0].raw == "A=1"
    plain = EnvDocument.parse("A=1\n")
    assert plain.bom is False
    assert plain.newline == "\n"
    assert EnvDocument.parse("").newline == "\n"


def test_get_keys_as_dict_last_occurrence_wins() -> None:
    doc = EnvDocument.parse("A=1\nB=2\nA=3\n")
    assert doc.get("A") == "3"
    assert doc.get("missing") is None
    assert doc.keys() == ["A", "B"]
    assert doc.as_dict() == {"B": "2", "A": "3"}


def test_invalid_lines_listed() -> None:
    doc = EnvDocument.parse("A=1\nnope\nB='x\n")
    assert [line.raw for line in doc.invalid_lines()] == ["nope", "B='x"]


# ------------------------------------------------------------------------------------------ quoting


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "abc"),
        ("123:AAbb-cc_dd", "123:AAbb-cc_dd"),
        ("https://x.y/p?q", '"https://x.y/p?q"'),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("a,b,c", "a,b,c"),
        ("", '""'),
        ("two words", '"two words"'),
        ("a#b", '"a#b"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("multi\nline", '"multi\\nline"'),
        ("cr\rlf", '"cr\\rlf"'),
        ("$HOME", '"\\$HOME"'),
        ("кириллица", '"кириллица"'),
        ("'single'", "\"'single'\""),
    ],
)
def test_quote(value: str, expected: str) -> None:
    assert quote(value) == expected
    assert unquote(expected) == value


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("plain", "plain"),
        ("  plain  ", "plain"),
        ("plain # c", "plain"),
        (" # c", ""),
        ("#x", "#x"),
        ('"a\\nb" # c', "a\nb"),
        ("'lit\\n'", "lit\\n"),
        ('"unknown \\q escape"', "unknown \\q escape"),
    ],
)
def test_unquote(raw: str, value: str) -> None:
    assert unquote(raw) == value


@pytest.mark.parametrize("raw", ['"open', "'open", '"x" y', '"ends with backslash\\"', '"x\\'])
def test_unquote_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError, match="quote"):
        unquote(raw)


# ------------------------------------------------------------------------------------------ set


def test_set_replaces_in_place_and_keeps_everything_else() -> None:
    text = "# top\nA=1\n# about B\nB=2 # inline\n\n# tail\n"
    doc = EnvDocument.parse(text)
    doc.set("B", "3")
    assert doc.render() == "# top\nA=1\n# about B\nB=3 # inline\n\n# tail\n"


def test_set_keeps_export_spacing_and_quote_style() -> None:
    doc = EnvDocument.parse("export  A = 'x'  # c\nB=\"y\"\nC=z\n")
    doc.set("A", "new")
    doc.set("B", "plain")
    doc.set("C", "needs quotes")
    assert doc.render() == 'export  A = \'new\'  # c\nB="plain"\nC="needs quotes"\n'


def test_set_single_quote_style_falls_back_to_double_when_needed() -> None:
    doc = EnvDocument.parse("A='x'\n")
    doc.set("A", "it's\nmulti")
    assert doc.render() == 'A="it\'s\\nmulti"\n'
    assert EnvDocument.parse(doc.render()).get("A") == "it's\nmulti"


def test_set_same_value_keeps_raw_formatting() -> None:
    doc = EnvDocument.parse("A='abc'   # keep\n")
    doc.set("A", "abc")
    assert doc.render() == "A='abc'   # keep\n"


def test_set_empty_value_on_bare_line_stays_bare() -> None:
    doc = EnvDocument.parse("A=1 # note\n")
    doc.set("A", "")
    assert doc.render() == "A= # note\n"
    assert EnvDocument.parse(doc.render()).get("A") == ""


def test_set_value_on_empty_line_with_comment() -> None:
    doc = EnvDocument.parse("A= # note\n")
    doc.set("A", "5")
    assert doc.render() == "A=5 # note\n"


def test_set_multiline_entry_in_place() -> None:
    doc = EnvDocument.parse('X=0\nCERT="a\nb" # pem\nY=1\n')
    doc.set("CERT", "c\nd")
    assert doc.render() == 'X=0\nCERT="c\\nd" # pem\nY=1\n'


def test_set_duplicates_updates_last_and_comments_out_earlier() -> None:
    doc = EnvDocument.parse("A=1\nB=2\nA=3\nC=4\n")
    doc.set("A", "9")
    assert doc.render() == f"{DUPLICATE_PREFIX}A=1\nB=2\nA=9\nC=4\n"
    assert doc.as_dict() == {"B": "2", "A": "9", "C": "4"}


def test_set_multiline_duplicate_becomes_single_comment_line() -> None:
    doc = EnvDocument.parse('A="x\ny"\nA=2\n')
    doc.set("A", "3")
    assert doc.render() == f'{DUPLICATE_PREFIX}A="x\\ny"\nA=3\n'
    reparsed = EnvDocument.parse(doc.render())
    assert [line.kind for line in reparsed.lines] == ["comment", "kv"]


def test_set_replaces_broken_line_for_key() -> None:
    doc = EnvDocument.parse('A=1\nB="broken\nC=3\n')
    doc.set("B", "fixed")
    assert doc.render() == "A=1\nB=fixed\nC=3\n"


def test_set_appends_new_key_at_end_of_file_with_comments() -> None:
    doc = EnvDocument.parse("A=1\n\n")
    doc.set("NEW", "v", comment=["What it does.", "", "# already a comment"])
    assert doc.render() == "A=1\n# What it does.\n#\n# already a comment\nNEW=v\n\n"


def test_set_appends_to_file_without_trailing_newline() -> None:
    doc = EnvDocument.parse("A=1")
    doc.set("B", "2")
    assert doc.render() == "A=1\nB=2\n"


def test_set_appends_to_empty_document_using_crlf_of_doc() -> None:
    doc = EnvDocument.parse("\ufeffX=1\r\n")
    doc.set("Y", "2")
    assert doc.render() == "\ufeffX=1\r\nY=2\r\n"
    empty = EnvDocument.parse("")
    empty.set("A", "1")
    assert empty.render() == "A=1\n"


def test_set_appends_to_named_section() -> None:
    first = render_section_header("Первая")
    second = render_section_header("Вторая")
    text = f"{first}\nA=1\n\n{second}\nB=2\n"
    doc = EnvDocument.parse(text)
    doc.set("A2", "x", comment=["a2"], section="Первая")
    doc.set("B2", "y", section=second)  # the full header line works too
    doc.set("A3", "z", section="первая")  # case-insensitive
    assert doc.render() == f"{first}\nA=1\n# a2\nA2=x\nA3=z\n\n{second}\nB=2\nB2=y\n"


def test_set_creates_missing_section_at_end() -> None:
    doc = EnvDocument.parse("A=1\n")
    doc.set("B", "2", comment=["bee"], section="Новая")
    header = render_section_header("Новая")
    assert doc.render() == f"A=1\n\n{header}\n# bee\nB=2\n"
    again = EnvDocument.parse(doc.render())
    again.set("C", "3", section="Новая")
    assert again.render().endswith("B=2\nC=3\n")


def test_set_rejects_bad_keys_and_values() -> None:
    doc = EnvDocument.parse("")
    for key in ["", "1A", "A B", "A\nB=1", "A=B", "a.b"]:
        with pytest.raises(ValueError, match=r"invalid .env key"):
            doc.set(key, "x")
    with pytest.raises(TypeError):
        doc.set("A", 1)  # type: ignore[arg-type]
    assert doc.lines == []


def test_set_comment_cannot_inject_lines() -> None:
    doc = EnvDocument.parse("")
    doc.set("A", "1", comment=["first\nEVIL=1\r\nsecond"])
    reparsed = EnvDocument.parse(doc.render())
    assert reparsed.as_dict() == {"A": "1"}


def test_set_value_cannot_inject_lines() -> None:
    doc = EnvDocument.parse("A=1\n")
    doc.set("A", "x\nEVIL=1")
    reparsed = EnvDocument.parse(doc.render())
    assert reparsed.as_dict() == {"A": "x\nEVIL=1"}


# ------------------------------------------------------------------------------------------ remove


def test_remove_drops_all_occurrences_and_warnings() -> None:
    text = f'# keep\nA=1\nB=2\n{ANNOTATION_PREFIX}bad\nA=3\nA="broken\nC=4\n'
    doc = EnvDocument.parse(text)
    doc.remove("A")
    assert doc.render() == "# keep\nB=2\nC=4\n"
    doc.remove("missing")
    assert doc.render() == "# keep\nB=2\nC=4\n"


# ------------------------------------------------------------------------------------------ annotations


def test_annotate_invalid_is_idempotent_and_replaces_old_warning() -> None:
    doc = EnvDocument.parse("# about\nTRIAL_DAYS=abc\n")
    doc.annotate_invalid("TRIAL_DAYS", "значение «abc» отклонено")
    doc.annotate_invalid("TRIAL_DAYS", "значение «abc» отклонено")
    assert doc.render() == "# about\n# ⚠ значение «abc» отклонено\nTRIAL_DAYS=abc\n"
    doc.annotate_invalid("TRIAL_DAYS", "другая\nошибка")
    assert doc.render() == "# about\n# ⚠ другая ошибка\nTRIAL_DAYS=abc\n"
    once = doc.render()
    reparsed = EnvDocument.parse(once)
    reparsed.annotate_invalid("TRIAL_DAYS", "другая ошибка")
    assert reparsed.render() == once


def test_annotate_survives_set_and_clear_removes_it() -> None:
    doc = EnvDocument.parse("A=bad\r\n")
    doc.annotate_invalid("A", "oops")
    doc.set("A", "3")
    assert doc.render() == "# ⚠ oops\r\nA=3\r\n"
    doc.clear_annotations("A")
    assert doc.render() == "A=3\r\n"


def test_annotate_broken_line_and_missing_key() -> None:
    doc = EnvDocument.parse('A="broken\n')
    doc.annotate_invalid("A", "незакрытая кавычка")
    assert doc.render() == '# ⚠ незакрытая кавычка\nA="broken\n'
    with pytest.raises(KeyError):
        doc.annotate_invalid("NOPE", "x")


# ------------------------------------------------------------------------------------------ sections


@pytest.mark.parametrize(
    ("line", "title"),
    [
        ("# ── Продажи и триал ───────── (бот: ⚙️ → 📦)", "Продажи и триал"),
        ("# ── Remnawave ──", "Remnawave"),
        ("#──Tight──", "Tight"),
        ("# ──── Many dashes ────", "Many dashes"),
        ("# ── No closing rule", "No closing rule"),
        ("# ───────────────", None),
        ("# plain comment", None),
        ("A=1", None),
    ],
)
def test_section_title(line: str, title: str | None) -> None:
    assert section_title(line) == title


def test_render_section_header() -> None:
    header = render_section_header("Запуск")
    assert header.startswith("# ── Запуск ─")
    assert len(header) == 72
    assert section_title(header) == "Запуск"
    assert render_section_header("# ── Custom ──") == "# ── Custom ──"
    assert section_title(render_section_header("a\nb")) == "a b"


def test_env_line_defaults() -> None:
    line = EnvLine("blank", "")
    assert line.key is None
    assert line.value is None
    assert line.eol is None


# ------------------------------------------------------------------------------------------ safety


def test_dangling_quote_cannot_swallow_new_lines() -> None:
    # Without protection the appended `NEW="# x"` would close the dangling quote of BROKEN with a valid
    # trailer ("# x"...), turning "\nNEW=" into BROKEN's value and losing NEW.
    doc = EnvDocument.parse('BROKEN="abc\n')
    doc.set("NEW", "# x")
    rendered = doc.render()
    assert rendered == f'{INVALID_PREFIX}BROKEN="abc\nNEW="# x"\n'
    assert EnvDocument.parse(rendered).as_dict() == {"NEW": "# x"}


def test_dangling_quote_left_alone_when_harmless() -> None:
    doc = EnvDocument.parse('BROKEN="abc\nA=1\n')
    doc.set("A", "2")
    assert doc.render() == 'BROKEN="abc\nA=2\n'


def test_dangling_quote_protected_on_remove_and_annotate() -> None:
    # A's quote cannot close at "it's" (garbage after it); once B is removed, the quote at the end of
    # C's line would close it and swallow C.
    doc = EnvDocument.parse("A='x\nB=it's here\nC=1 # c'\n")
    assert doc.as_dict() == {"B": "it's here", "C": "1"}
    doc.remove("B")
    assert doc.render() == f"{INVALID_PREFIX}A='x\nC=1 # c'\n"
    assert EnvDocument.parse(doc.render()).as_dict() == {"C": "1"}

    other = EnvDocument.parse("A='x\nB=1\n")
    other.annotate_invalid("B", "плохое значение '")
    assert other.render() == f"{INVALID_PREFIX}A='x\n# ⚠ плохое значение '\nB=1\n"
    assert EnvDocument.parse(other.render()).as_dict() == {"B": "1"}


def test_line_with_trailing_cr_keeps_it_when_lines_are_appended() -> None:
    doc = EnvDocument.parse("# note\r")
    doc.set("A", "1")
    rendered = doc.render()
    assert rendered == "# note\r\r\nA=1\n"
    assert EnvDocument.parse(rendered).lines[0].raw == "# note\r"


def test_parse_is_linear_on_many_broken_quotes() -> None:
    text = 'A="\n' * 20000
    doc = EnvDocument.parse(text)
    assert doc.render() == text
    assert len(doc.lines) == 10000  # pairs of lines form a valid multi-line value each
    assert doc.get("A") == "\nA="
