"""Update check with a fake GitHub: version parsing, release selection, one message per version, failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from svbg.ops.state import K_UPDATES, MetaState
from svbg.ops.updates import (
    UpdateChecker,
    describe,
    owner_notes,
    parse_version,
    pick_release,
)
from tests.dbkit import CountingDatabase

NOTES = """## Что нового
- быстрее

## Для владельца
Перед обновлением ничего делать не нужно.
### Подробности
Тарифы <b>сохраняются</b>.

## Для разработчиков
- рефакторинг
"""


def _rel(tag: str, **kw: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "name": f"SvBG {tag}",
        "html_url": f"https://github.com/owner/svbg-shop/releases/tag/{tag}",
        "body": NOTES,
        "draft": False,
        "prerelease": False,
        **kw,
    }


class MemState:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any]:
        return dict(self.data.get(key, {}))

    async def merge(self, key: str, values: Mapping[str, Any]) -> None:
        self.data.setdefault(key, {}).update(values)


@dataclass
class FakeGitHub:
    payload: Any = field(default_factory=list)
    error: Exception | None = None
    urls: list[str] = field(default_factory=list)

    async def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


@dataclass
class Posts:
    texts: list[str] = field(default_factory=list)

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


def _checker(
    gh: FakeGitHub, posts: Posts, settings: dict[str, Any], *, version: str = "1.2.0", state: Any = None
) -> UpdateChecker:
    return UpdateChecker(
        None,  # type: ignore[arg-type]  # the state is given
        settings=lambda: settings,
        post=posts,
        fetcher=gh,
        current_version=version,
        state=state or MemState(),
    )


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.2.3", (1, 2, 3, 3, 0)),
        ("1.2", (1, 2, 0, 3, 0)),
        ("v2.0.0-rc.1", (2, 0, 0, 2, 1)),
        ("v2.0.0b2", (2, 0, 0, 1, 2)),
        ("release-1", None),
        ("v1.2.3.4", None),
    ],
)
def test_parse_version(tag: str, expected: tuple[int, ...] | None) -> None:
    assert parse_version(tag) == expected


def test_versions_order_prereleases_before_final() -> None:
    assert parse_version("2.0.0-rc.1") < parse_version("2.0.0")  # type: ignore[operator]
    assert parse_version("2.0.0-beta.3") < parse_version("2.0.0-rc.1")  # type: ignore[operator]


def test_owner_notes_section() -> None:
    notes = owner_notes(NOTES)
    assert notes.startswith("Перед обновлением") and "Подробности" in notes
    assert "рефакторинг" not in notes and "быстрее" not in notes
    assert owner_notes("просто текст") == "просто текст"
    assert len(owner_notes("x" * 5000)) == 1500
    assert owner_notes(None) == ""


def test_pick_release_rules() -> None:
    payload = [
        _rel("v1.1.0"),
        _rel("v1.3.0"),
        _rel("v1.4.0", draft=True),
        _rel("v1.5.0-rc.1", prerelease=True),
        _rel("nonsense"),
        "garbage",
        _rel("v1.3.1", html_url="https://evil.example/x"),
    ]
    best = pick_release(payload, "1.2.0", prereleases=False)
    assert best is not None and best.tag == "v1.3.1" and best.url == ""
    pre = pick_release(payload, "1.2.0", prereleases=True)
    assert pre is not None and pre.tag == "v1.5.0-rc.1" and pre.prerelease
    assert pick_release(payload, "9.0.0", prereleases=True) is None
    assert pick_release({"message": "Not Found"}, "1.0.0", prereleases=False) is None
    assert pick_release(payload, "dev", prereleases=False) is None


async def test_notifies_once_per_version() -> None:
    gh, posts = FakeGitHub([_rel("v1.3.0")]), Posts()
    settings = {"UPDATE_REPO": "owner/svbg-shop"}
    state = MemState()
    checker = _checker(gh, posts, settings, state=state)
    status = await checker.check()
    assert status.state == "available" and status.latest == "v1.3.0" and status.notified
    assert gh.urls == ["https://api.github.com/repos/owner/svbg-shop/releases?per_page=20"]
    (text,) = posts.texts
    assert "Вышла версия <b>v1.3.0</b> (у вас 1.2.0)" in text
    assert "svbg update" in text and "<blockquote expandable>" in text
    assert "&lt;b&gt;сохраняются&lt;/b&gt;" in text, "release notes are escaped"
    assert '<a href="https://github.com/owner/svbg-shop/releases/tag/v1.3.0">' in text

    await _checker(gh, posts, settings, state=state).tick()  # a restart: the durable mark is used
    assert len(posts.texts) == 1
    forced = await checker.check(force=True)
    assert forced.notified and len(posts.texts) == 2

    gh.payload = [_rel("v1.3.0"), _rel("v1.4.0")]
    await checker.check()
    assert "v1.4.0" in posts.texts[-1] and len(posts.texts) == 3
    assert describe(checker.last) == "доступна v1.4.0 (у вас 1.2.0)"


async def test_major_version_warning() -> None:
    gh, posts = FakeGitHub([_rel("v2.0.0", body="")]), Posts()
    await _checker(gh, posts, {"UPDATE_REPO": "o/r"}).check()
    assert "мажорная версия" in posts.texts[0] and "blockquote" not in posts.texts[0]


async def test_off_no_repo_current_and_errors() -> None:
    gh, posts = FakeGitHub([_rel("v1.0.0")]), Posts()
    assert (await _checker(gh, posts, {"UPDATE_CHECK": False, "UPDATE_REPO": "o/r"}).check()).state == "off"
    assert (await _checker(gh, posts, {}).check()).state == "no_repo", "no repository by default"
    current = await _checker(gh, posts, {"UPDATE_REPO": "o/r"}).check()
    assert current.state == "current" and describe(current) == "у вас последняя версия 1.2.0"
    assert gh.urls == ["https://api.github.com/repos/o/r/releases?per_page=20"]

    state = MemState()
    gh.error = RuntimeError("GitHub ответил 403")
    failed = await _checker(gh, posts, {"UPDATE_REPO": "o/r"}, state=state).check()
    assert failed.state == "error" and failed.error == "GitHub ответил 403"
    assert state.data[K_UPDATES]["error"] == "GitHub ответил 403"
    gh.error = TimeoutError()
    assert (await _checker(gh, posts, {"UPDATE_REPO": "o/r"}).check()).state == "error"
    assert posts.texts == []
    assert describe(None) == "ещё не проверялось"


@pytest.mark.pg
async def test_state_persists_in_config_meta(pg_dsn: str) -> None:
    from tests.dbkit import open_db

    db: CountingDatabase
    async with open_db(pg_dsn) as db:
        state = MetaState(db)
        gh, posts = FakeGitHub([_rel("v1.3.0")]), Posts()
        await _checker(gh, posts, {"UPDATE_REPO": "o/r"}, state=state).check()
        await _checker(gh, posts, {"UPDATE_REPO": "o/r"}, state=MetaState(db)).check()
        assert len(posts.texts) == 1
        saved = await state.get(K_UPDATES)
        assert saved["notified"] == "v1.3.0" and saved["latest"] == "v1.3.0" and saved["error"] is None
        await state.merge(K_UPDATES, {"extra": 1})
        assert (await state.get(K_UPDATES))["notified"] == "v1.3.0", "merge keeps other fields"
