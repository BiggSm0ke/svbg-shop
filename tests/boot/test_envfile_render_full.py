from __future__ import annotations

import pytest

from svbg.boot.envfile import (
    DUPLICATE_PREFIX,
    INVALID_PREFIX,
    UNRECOGNIZED_TITLE,
    EnvDocument,
    RenderKey,
    render_full,
    render_section_header,
)

SECTIONS = [("boot", "Запуск"), ("sales", "Продажи и триал"), ("pay", "Баланс и оплата")]
HEADER = ["═" * 60, "SvBG Shop — все настройки.", "═" * 60]
RULE = "# " + "═" * 60


def make_keys(**overrides: str) -> list[RenderKey]:
    values = {"BOT_TOKEN": "123:abc", "TRIAL_DAYS": "3", "TRIAL_AUDIENCE": "all", "WALLET_MIN": "60"}
    values.update(overrides)
    return [
        RenderKey("BOT_TOKEN", values["BOT_TOKEN"], ["Токен бота."], "boot"),
        RenderKey(
            "TRIAL_DAYS", values["TRIAL_DAYS"], ["Длительность триала, дней.", "0 — выкл. ⚡"], "sales"
        ),
        RenderKey("TRIAL_AUDIENCE", values["TRIAL_AUDIENCE"], ["Кто может взять триал."], "sales"),
        RenderKey("WALLET_MIN", values["WALLET_MIN"], ["Минуты."], "pay"),
    ]


def h(title: str) -> str:
    return render_section_header(title)


CANONICAL = (
    f"{RULE}\n# SvBG Shop — все настройки.\n{RULE}\n\n"
    f"{h('Запуск')}\n# Токен бота.\nBOT_TOKEN=123:abc\n\n"
    f"{h('Продажи и триал')}\n# Длительность триала, дней.\n# 0 — выкл. ⚡\nTRIAL_DAYS=3\n"
    "# Кто может взять триал.\nTRIAL_AUDIENCE=all\n\n"
    f"{h('Баланс и оплата')}\n# Минуты.\nWALLET_MIN=60\n"
)


def full(existing: str | None = None, **overrides: str) -> str:
    doc = None if existing is None else EnvDocument.parse(existing)
    return render_full(SECTIONS, make_keys(**overrides), HEADER, doc)


def test_canonical_render() -> None:
    assert full() == CANONICAL


def test_render_is_a_fixed_point() -> None:
    assert full(CANONICAL) == CANONICAL
    changed = full(CANONICAL, TRIAL_DAYS="7")
    assert changed == CANONICAL.replace("TRIAL_DAYS=3", "TRIAL_DAYS=7")
    assert full(changed, TRIAL_DAYS="7") == changed


def test_values_are_quoted() -> None:
    text = full(TRIAL_AUDIENCE="a b", BOT_TOKEN="")
    doc = EnvDocument.parse(text)
    assert doc.get("TRIAL_AUDIENCE") == "a b"
    assert doc.get("BOT_TOKEN") == ""
    assert 'TRIAL_AUDIENCE="a b"' in text


def test_sections_without_keys_are_skipped_and_unknown_section_appended() -> None:
    keys = [RenderKey("A", "1", [], "sales"), RenderKey("Z", "2", ["zz"], "plugin.x")]
    text = render_full(SECTIONS, keys, [])
    assert text == f"{h('Продажи и триал')}\nA=1\n\n{h('plugin.x')}\n# zz\nZ=2\n"
    assert render_full(SECTIONS, keys, [], EnvDocument.parse(text)) == text


def test_empty_render() -> None:
    assert render_full([], [], []) == ""


def test_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        render_full(SECTIONS, [RenderKey("A", "1", [], "boot"), RenderKey("A", "2", [], "boot")], [])
    with pytest.raises(ValueError, match=r"invalid .env key"):
        render_full(SECTIONS, [RenderKey("A\nB", "1", [], "boot")], [])
    with pytest.raises(ValueError, match="header"):
        render_full([("x", "# not a section")], [RenderKey("A", "1", [], "x")], [])


def test_existing_keeps_newline_and_bom() -> None:
    existing = "\ufeff" + CANONICAL.replace("\n", "\r\n")
    out = full(existing, TRIAL_DAYS="5")
    assert out == "\ufeff" + CANONICAL.replace("TRIAL_DAYS=3", "TRIAL_DAYS=5").replace("\n", "\r\n")


def test_our_comments_are_regenerated() -> None:
    old = CANONICAL.replace("# Кто может взять триал.", "# Старое описание\n# в две строки")
    assert full(old) == CANONICAL


def test_header_is_regenerated() -> None:
    old = CANONICAL.replace("# SvBG Shop — все настройки.", "# SvBG Shop — старый заголовок.")
    assert full(old) == CANONICAL


def test_foreign_comments_preserved_at_position() -> None:
    owner = CANONICAL.replace(
        "TRIAL_DAYS=3\n",
        "TRIAL_DAYS=3\n\n# Моя заметка: триал подняли 01.10\n# вторая строка\n\n",
    )
    out = full(owner, TRIAL_DAYS="7")
    assert out == owner.replace("TRIAL_DAYS=3", "TRIAL_DAYS=7")
    assert full(out, TRIAL_DAYS="7") == out


def test_owner_note_directly_above_our_comment_is_kept() -> None:
    owner = CANONICAL.replace("# Кто может взять триал.", "# заметка владельца\n# Кто может взять триал.")
    assert full(owner) == owner


def test_owner_note_between_our_comment_and_key_moves_above_and_is_stable() -> None:
    owner = CANONICAL.replace("TRIAL_AUDIENCE=all", "# заметка\nTRIAL_AUDIENCE=all")
    out = full(owner)
    assert "# заметка\n# Кто может взять триал.\nTRIAL_AUDIENCE=all" in out
    assert full(out) == out


def test_inline_comment_export_and_quote_style_kept() -> None:
    owner = CANONICAL.replace("TRIAL_DAYS=3", "export TRIAL_DAYS='3'  # моё").replace(
        "WALLET_MIN=60", 'WALLET_MIN="60"'
    )
    out = full(owner, TRIAL_DAYS="10", WALLET_MIN="90")
    assert "export TRIAL_DAYS='10'  # моё\n" in out
    assert 'WALLET_MIN="90"\n' in out
    assert full(out, TRIAL_DAYS="10", WALLET_MIN="90") == out


def test_unknown_key_inside_known_section_stays() -> None:
    owner = CANONICAL.replace("TRIAL_AUDIENCE=all\n", "TRIAL_AUDIENCE=all\n# мой ключ\nMY_FLAG=1\n")
    out = full(owner)
    assert out == owner
    assert UNRECOGNIZED_TITLE not in out


def test_unknown_keys_outside_sections_go_to_unrecognized() -> None:
    owner = "OLD_KEY=1\n# про старый\nOLD_TWO=2\n\n" + CANONICAL
    out = full(owner)
    assert out == CANONICAL + f"\n{h(UNRECOGNIZED_TITLE)}\nOLD_KEY=1\n# про старый\nOLD_TWO=2\n"
    assert full(out) == out


def test_unrecognized_section_contents_are_kept_and_known_keys_move_out() -> None:
    # The last occurrence wins: the entry in "Не распознано" is the live one and moves to its section,
    # the earlier one is commented out as a duplicate and stays where it was.
    existing = CANONICAL + f"\n{h(UNRECOGNIZED_TITLE)}\nFOO=1\nWALLET_MIN=75\n# bar\nBAR=2\n"
    out = full(existing, WALLET_MIN="75")
    pay = f"{DUPLICATE_PREFIX}WALLET_MIN=60\n\n# Минуты.\nWALLET_MIN=75"
    assert out == (
        CANONICAL.replace("# Минуты.\nWALLET_MIN=60", pay)
        + f"\n{h(UNRECOGNIZED_TITLE)}\nFOO=1\n# bar\nBAR=2\n"
    )
    assert full(out, WALLET_MIN="75") == out


def test_hand_written_file_is_converted() -> None:
    hand = "# мой файл\nBOT_TOKEN=999:zzz # from BotFather\nTRIAL_DAYS=1\nCUSTOM=x\n"
    out = full(hand, BOT_TOKEN="999:zzz", TRIAL_DAYS="1")
    doc = EnvDocument.parse(out)
    assert doc.as_dict() == {
        "BOT_TOKEN": "999:zzz",
        "TRIAL_DAYS": "1",
        "TRIAL_AUDIENCE": "all",
        "WALLET_MIN": "60",
        "CUSTOM": "x",
    }
    assert out.startswith(f"{RULE}\n# SvBG Shop — все настройки.\n{RULE}\n\n# мой файл\n\n")
    assert "BOT_TOKEN=999:zzz # from BotFather\n" in out
    assert out.endswith(f"{h(UNRECOGNIZED_TITLE)}\nCUSTOM=x\n")
    assert full(out, BOT_TOKEN="999:zzz", TRIAL_DAYS="1") == out


def test_missing_known_key_is_restored() -> None:
    without = CANONICAL.replace("# Кто может взять триал.\nTRIAL_AUDIENCE=all\n", "")
    assert full(without) == CANONICAL


def test_annotation_kept_above_key() -> None:
    doc = EnvDocument.parse(CANONICAL.replace("TRIAL_DAYS=3", "TRIAL_DAYS=abc"))
    doc.annotate_invalid("TRIAL_DAYS", "значение «abc» отклонено. Применено прежнее: 3")
    out = render_full(SECTIONS, make_keys(), HEADER, doc)
    assert "# 0 — выкл. ⚡\n# ⚠ значение «abc» отклонено. Применено прежнее: 3\nTRIAL_DAYS=3\n" in out
    assert full(out) == out
    cleared = EnvDocument.parse(out)
    cleared.clear_annotations("TRIAL_DAYS")
    assert render_full(SECTIONS, make_keys(), HEADER, cleared) == CANONICAL


def test_broken_line_of_known_key_is_replaced() -> None:
    owner = CANONICAL.replace("TRIAL_DAYS=3", 'TRIAL_DAYS="3')
    assert full(owner) == CANONICAL


def test_broken_unknown_line_is_preserved() -> None:
    owner = CANONICAL.replace("TRIAL_DAYS=3\n", "TRIAL_DAYS=3\nthis is garbage\n")
    assert full(owner) == owner


def test_duplicate_known_key_commented_out() -> None:
    owner = CANONICAL.replace("BOT_TOKEN=123:abc\n", "BOT_TOKEN=123:abc\nTRIAL_DAYS=99\n")
    out = full(owner)
    assert f"BOT_TOKEN=123:abc\n{DUPLICATE_PREFIX}TRIAL_DAYS=99\n" in out
    assert "TRIAL_DAYS=3\n" in out
    assert full(out) == out


def test_key_moved_to_other_section_by_owner_goes_back() -> None:
    owner = CANONICAL.replace("# Минуты.\nWALLET_MIN=60\n", "").replace(
        "BOT_TOKEN=123:abc\n", "BOT_TOKEN=123:abc\n# Минуты.\nWALLET_MIN=61\n"
    )
    assert full(owner, WALLET_MIN="61") == CANONICAL.replace("WALLET_MIN=60", "WALLET_MIN=61")


def test_owner_section_with_own_keys_kept_stale_empty_header_dropped() -> None:
    owner = CANONICAL + f"\n{h('Мои ключи')}\nMINE=1\n\n{h('Старый раздел')}\n"
    out = full(owner)
    assert out == CANONICAL + f"\n{h('Мои ключи')}\nMINE=1\n"
    assert full(out) == out


def test_section_with_only_owner_lines_still_rendered() -> None:
    owner = CANONICAL + f"\n{h('Баланс и оплата')}\n"  # duplicate header, harmless
    assert full(owner) == CANONICAL
    keys = [k for k in make_keys() if k.section != "pay"]
    with_foreign = CANONICAL.replace("WALLET_MIN=60", "WALLET_MIN=60\nPAY_CUSTOM=1")
    out = render_full(SECTIONS, keys, HEADER, EnvDocument.parse(with_foreign))
    # WALLET_MIN is not a known key any more: it stays where it was, as an owner line.
    assert out.endswith(f"{h('Баланс и оплата')}\n# Минуты.\nWALLET_MIN=60\nPAY_CUSTOM=1\n")
    assert render_full(SECTIONS, keys, HEADER, EnvDocument.parse(out)) == out


def test_owner_lines_inside_our_header_are_kept() -> None:
    owner = CANONICAL.replace(
        "# SvBG Shop — все настройки.\n", "# SvBG Shop — все настройки.\n# моя строка\n"
    )
    out = full(owner)
    assert out == CANONICAL.replace(f"{RULE}\n\n", f"{RULE}\n\n# моя строка\n\n", 1)
    assert full(out) == out


def test_owner_comment_split_into_our_comment_is_kept() -> None:
    owner = CANONICAL.replace("# 0 — выкл. ⚡", "# мой комментарий\n# 0 — выкл. ⚡")
    out = full(owner)
    assert "TRIAL_AUDIENCE" in out
    assert "# мой комментарий\n" in out
    assert out.count("# 0 — выкл. ⚡") == 1
    assert full(out) == out


def test_stale_comment_after_owner_line_is_not_eaten() -> None:
    # Our comment for TRIAL_AUDIENCE is missing, the block above it follows the owner's own key: keep it.
    owner = CANONICAL.replace("# Кто может взять триал.\n", "MY=1\n# про мой ключ\n")
    out = full(owner)
    assert "MY=1\n# про мой ключ\n# Кто может взять триал.\nTRIAL_AUDIENCE=all\n" in out
    assert full(out) == out


def test_dangling_owner_line_is_neutralized_not_merged() -> None:
    # The owner's broken line would otherwise swallow our regenerated lines up to the next '"'.
    owner = CANONICAL.replace("TRIAL_DAYS=3\n", 'TRIAL_DAYS=3\nBROKEN="oops\n')
    keys = make_keys(TRIAL_AUDIENCE="# x")  # renders as "# x" in quotes: a valid trailer after a quote
    out = render_full(SECTIONS, keys, HEADER, EnvDocument.parse(owner))
    assert f'{INVALID_PREFIX}BROKEN="oops\n' in out
    parsed = EnvDocument.parse(out)
    assert parsed.get("TRIAL_AUDIENCE") == "# x"
    assert "BROKEN" not in parsed.as_dict()
    assert render_full(SECTIONS, keys, HEADER, parsed) == out


def test_harmless_dangling_owner_line_is_kept() -> None:
    owner = CANONICAL.replace("TRIAL_DAYS=3\n", 'TRIAL_DAYS=3\nBROKEN="oops\n')
    assert full(owner) == owner


def test_multiline_values_survive() -> None:
    owner = CANONICAL.replace("TRIAL_DAYS=3\n", 'TRIAL_DAYS=3\nPEM="a\nb"\n')
    out = full(owner)
    assert EnvDocument.parse(out).get("PEM") == "a\nb"
    assert full(out) == out
