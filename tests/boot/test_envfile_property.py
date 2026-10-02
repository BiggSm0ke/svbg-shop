"""Seeded randomized property tests (hypothesis is not installed)."""

from __future__ import annotations

import random

import pytest

from svbg.boot.envfile import INVALID_PREFIX, EnvDocument, RenderKey, quote, render_full, unquote

SEED = 20261001

KEYS = ["A", "B", "TRIAL_DAYS", "BOT_TOKEN", "x_y", "export", "K9", "a.b", "DUP"]
ATOMS = [
    "",
    " ",
    "\t",
    "abc",
    "123",
    "#",
    " #",
    "# c",
    "=",
    "==",
    '"',
    "'",
    "\\",
    '\\"',
    "\\n",
    "$HOME",
    "${X}",
    "привет",
    "🚀",
    "é",
    "\u00a0",
    "\u2028",
    "\x0b",
    "\x0c",
    "\r",
    "\ufeff",
    "a b",
    "http://x/y?z=1&w",
    "`cmd`",
]
NEWLINES = ["\n", "\r\n", "\r", ""]


def random_value_text(rng: random.Random) -> str:
    return "".join(rng.choice(ATOMS) for _ in range(rng.randint(0, 5)))


def random_line(rng: random.Random) -> str:
    kind = rng.randint(0, 11)
    key = rng.choice(KEYS)
    prefix = rng.choice(["", "", "export ", "  ", "\t", "export\t"])
    eq = rng.choice(["=", "=", " = ", "= ", " ="])
    if kind == 0:
        return ""
    if kind == 1:
        return rng.choice(["#", "# ", "  # "]) + random_value_text(rng)
    if kind == 2:
        return f"{prefix}{key}{eq}{random_value_text(rng)}"
    if kind == 3:
        return f'{prefix}{key}{eq}"{random_value_text(rng)}"{rng.choice(["", " # c", "x", "  "])}'
    if kind == 4:
        return f"{prefix}{key}{eq}'{random_value_text(rng)}'{rng.choice(['', ' # c', 'x'])}"
    if kind == 5:  # multi-line double-quoted value
        inner = rng.choice(["\n", "\r\n"]).join(random_value_text(rng) for _ in range(rng.randint(1, 3)))
        return f'{key}="{inner}"'
    if kind == 6:  # multi-line single-quoted value
        inner = "\n".join(random_value_text(rng) for _ in range(rng.randint(1, 3)))
        return f"{key}='{inner}'"
    if kind == 7:  # unterminated quotes
        return f"{key}={rng.choice(['"', "'"])}{random_value_text(rng)}"
    if kind == 8:
        return f"{key}={quote(random_value_text(rng))}"
    if kind == 9:
        return "# ── Section " + str(rng.randint(0, 3)) + " ──────"
    if kind == 10:
        return random_value_text(rng)  # garbage
    return f"{key}{eq}{rng.choice(['', ' # only comment', '#x', 'a#b'])}"


def random_document(rng: random.Random) -> str:
    parts: list[str] = []
    if rng.random() < 0.2:
        parts.append("\ufeff")
    style = rng.choice(["\n", "\r\n", "mixed"])
    for _ in range(rng.randint(0, 12)):
        parts.append(random_line(rng))
        parts.append(rng.choice(NEWLINES) if style == "mixed" else style)
    if parts and rng.random() < 0.3:
        parts.pop()  # no trailing newline
    return "".join(parts)


def test_parse_render_is_byte_identical() -> None:
    rng = random.Random(SEED)
    for _ in range(4000):
        text = random_document(rng)
        doc = EnvDocument.parse(text)
        assert doc.render() == text, repr(text)
        # every multi-character run is accounted for exactly once
        assert "".join(line.raw for line in doc.lines).count("\n") <= text.count("\n")


def test_parse_random_unicode_soup_round_trips() -> None:
    rng = random.Random(SEED + 1)
    alphabet = "AB=#'\"\\ \t\r\n\ufeffxé🚀\u2028$export"
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert EnvDocument.parse(text).render() == text, repr(text)


def test_quote_unquote_round_trip() -> None:
    rng = random.Random(SEED + 2)
    for _ in range(3000):
        value = random_value_text(rng) + "".join(chr(rng.randint(0, 0x2FF)) for _ in range(rng.randint(0, 4)))
        quoted = quote(value)
        assert unquote(quoted) == value, repr(value)
        doc = EnvDocument.parse(f"K={quoted}\n")
        assert [line.kind for line in doc.lines] == ["kv"], repr(quoted)
        assert doc.get("K") == value


def test_random_edits_keep_foreign_lines_and_apply_values() -> None:
    rng = random.Random(SEED + 3)
    writable = ["A", "B", "TRIAL_DAYS", "BOT_TOKEN", "NEW_ONE", "DUP"]
    for _ in range(1500):
        text = random_document(rng)
        doc = EnvDocument.parse(text)
        expected = doc.as_dict()
        # A broken assignment may be neutralized into "# (invalid, ignored) <text>" so that our new lines
        # cannot close its dangling quote; its text must still be there.
        untouched_before = [
            (line.raw, line.kind == "invalid" and line.key is not None)
            for line in doc.lines
            if line.key not in writable
        ]
        for _ in range(rng.randint(1, 4)):
            key = rng.choice(writable)
            value = random_value_text(rng)
            doc.set(key, value, comment=["note"], section=rng.choice([None, "Section 1", "Новая"]))
            expected[key] = value
        reparsed = EnvDocument.parse(doc.render())
        assert reparsed.as_dict() == expected, repr(text)
        untouched_after = [
            line.raw.removeprefix(INVALID_PREFIX) if line.raw.startswith(INVALID_PREFIX) else line.raw
            for line in reparsed.lines
            if line.key not in writable
        ]
        # foreign lines survive in order (new comments/headers may be added between them)
        iterator = iter(untouched_after)
        assert all(
            any(other == raw or (dangling and other == raw.strip()) for other in iterator)
            for raw, dangling in untouched_before
        ), repr(text)


def test_annotate_is_idempotent_on_random_documents() -> None:
    rng = random.Random(SEED + 4)
    for _ in range(800):
        doc = EnvDocument.parse(random_document(rng))
        keys = [line.key for line in doc.lines if line.key and line.kind in {"kv", "invalid"}]
        if not keys:
            continue
        key = rng.choice(keys)
        doc.annotate_invalid(key, "плохо")
        once = doc.render()
        doc.annotate_invalid(key, "плохо")
        assert doc.render() == once
        again = EnvDocument.parse(once)
        again.annotate_invalid(key, "плохо")
        assert again.render() == once


@pytest.mark.parametrize("seed", range(5))
def test_render_full_is_a_fixed_point_with_random_owner_edits(seed: int) -> None:
    rng = random.Random(SEED + 100 + seed)
    sections = [("s1", "Первый"), ("s2", "Второй"), ("s3", "Третий")]
    header = ["=" * 40, "Заголовок", "=" * 40]
    for _ in range(150):
        keys = [
            RenderKey(
                f"KEY_{i}",
                random_value_text(rng),
                [random_value_text(rng).replace("\r", " ") or "описание"] * rng.randint(0, 2),
                rng.choice(["s1", "s2", "s3", "extra"]),
            )
            for i in range(rng.randint(1, 8))
        ]
        base = render_full(sections, keys, header)
        assert render_full(sections, keys, header, EnvDocument.parse(base)) == base

        # The owner sprinkles foreign lines; after one re-render the result must be stable and keep them.
        lines = base.split("\n")
        foreign = [f"# owner note {n}" for n in range(rng.randint(0, 3))] + [
            f"OWNER_{n}=v{n}" for n in range(rng.randint(0, 3))
        ]
        for item in foreign:
            # anywhere below our header block (owner lines inside it are covered by dedicated tests)
            lines.insert(rng.randint(len(header), len(lines)), item)
        edited = "\n".join(lines)
        if rng.random() < 0.3:
            edited = edited.replace("\n", "\r\n")
        first = render_full(sections, keys, header, EnvDocument.parse(edited))
        second = render_full(sections, keys, header, EnvDocument.parse(first))
        assert second == first, repr(edited)
        result = EnvDocument.parse(first)
        for item in keys:
            assert result.get(item.key) == item.value
        for item in foreign:
            if item.startswith("OWNER_"):
                key, value = item.split("=")
                assert result.get(key) == value
            else:
                assert item in first
