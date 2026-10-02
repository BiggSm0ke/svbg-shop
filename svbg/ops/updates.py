"""Update check (04 §10): GitHub Releases every 12 h → one message per new version into «⚙️ Система».

The network goes through an injectable ``fetcher(url) -> JSON`` (tests use a fake; production uses
:func:`github_fetcher`, aiohttp with a timeout and a response size cap). Nothing is downloaded or installed:
the message tells the owner to run ``svbg update`` on the host (that command backs up first).

Release notes: the section whose heading contains «Для владельца» is shown (owner-facing changes); without
it, the beginning of the notes. A new major version gets a warning to read the instructions first.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import svbg
from svbg.core import clock
from svbg.core.log import mask
from svbg.ops.settings import opt
from svbg.ops.state import K_UPDATES, MetaState

if TYPE_CHECKING:
    from svbg.db.engine import Database

__all__ = [
    "INTERVAL_S",
    "Fetcher",
    "Release",
    "UpdateChecker",
    "UpdateStatus",
    "describe",
    "github_fetcher",
    "owner_notes",
    "parse_version",
    "pick_release",
]

log = logging.getLogger("svbg.ops.updates")

INTERVAL_S: Final = 12 * 3600
_API: Final = "https://api.github.com/repos/{repo}/releases?per_page=20"
_MAX_BODY: Final = 2 * 1024 * 1024
_NOTES_MAX: Final = 1500
_VERSION_RE: Final = re.compile(
    r"^v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,6}))?(?:[-.]?(a|alpha|b|beta|rc|pre)\.?(\d{1,4})?)?$", re.IGNORECASE
)
_PRE_RANK: Final = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "pre": 2, "rc": 2}
_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.*)$")

#: ``await fetcher(url)`` → the decoded JSON of the GitHub API response.
Fetcher = Callable[[str], Awaitable[Any]]
Version = tuple[int, int, int, int, int]  # major, minor, patch, pre-rank (3 = final), pre-number


def parse_version(tag: str) -> Version | None:
    m = _VERSION_RE.match(str(tag).strip())
    if m is None:
        return None
    pre = m.group(4)
    rank = _PRE_RANK[pre.lower()] if pre else 3
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), rank, int(m.group(5) or 0)


def owner_notes(body: str | None, limit: int = _NOTES_MAX) -> str:
    """The «Для владельца» section of release notes (Markdown), else the beginning; plain text, trimmed."""
    text = (body or "").replace("\r\n", "\n").strip()
    lines = text.split("\n")
    picked: list[str] | None = None
    level = 0
    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if picked is None:
            if m and "для владельца" in m.group(2).lower():
                picked, level = [], len(m.group(1))
            continue
        if m and len(m.group(1)) <= level:
            break
        picked.append(line)
    chosen = "\n".join(picked).strip() if picked else text
    chosen = re.sub(r"\n{3,}", "\n\n", chosen)
    return chosen if len(chosen) <= limit else chosen[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    version: Version
    name: str
    url: str
    notes: str
    prerelease: bool


def pick_release(payload: Any, current: str, *, prereleases: bool) -> Release | None:
    """The newest release newer than ``current`` (drafts never; pre-releases only when allowed)."""
    mine = parse_version(current)
    if mine is None or not isinstance(payload, list):
        return None
    best: Release | None = None
    for item in payload:
        if not isinstance(item, Mapping) or item.get("draft"):
            continue
        pre = bool(item.get("prerelease"))
        if pre and not prereleases:
            continue
        tag = str(item.get("tag_name") or "")
        version = parse_version(tag)
        if version is None or version <= mine or (version[3] < 3 and not prereleases):
            continue
        if best is None or version > best.version:
            url = str(item.get("html_url") or "")
            best = Release(
                tag=tag,
                version=version,
                name=str(item.get("name") or tag)[:100],
                url=url if url.startswith("https://github.com/") else "",
                notes=owner_notes(str(item.get("body") or "")),
                prerelease=pre,
            )
    return best


async def github_fetcher(url: str, *, limit_s: float = 20.0, proxy: str | None = None) -> Any:
    """GET ``url`` from the GitHub API (JSON, ≤ 2 MB). Raises ``RuntimeError`` with a short reason."""
    import aiohttp

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"svbg-shop/{svbg.__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=limit_s)) as session,
            session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp,
        ):
            if resp.status != 200:
                raise RuntimeError(f"GitHub ответил {resp.status}")
            body = await resp.content.read(_MAX_BODY + 1)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub недоступен ({type(exc).__name__})") from None
    if len(body) > _MAX_BODY:
        raise RuntimeError("слишком большой ответ GitHub")
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("GitHub вернул не JSON") from None


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    state: str  # off | no_repo | current | available | error
    current: str
    latest: str | None = None
    notified: bool = False
    error: str | None = None


#: ``post(text)`` into the «⚙️ Система» topic (Telegram HTML).
Post = Callable[[str], Awaitable[Any]]


class UpdateChecker:
    def __init__(
        self,
        db: Database,
        *,
        settings: Callable[[], Mapping[str, Any]],
        post: Post,
        fetcher: Fetcher | None = None,
        current_version: str = svbg.__version__,
        state: MetaState | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._settings = settings
        self._post = post
        self._fetcher = fetcher or github_fetcher
        self._current = current_version
        self._state = state or MetaState(db)
        self._timeout = timeout
        self.last: UpdateStatus | None = None

    async def tick(self) -> None:
        """Scheduler entry point (every 12 h): never raises for network problems."""
        await self.check()

    async def check(self, *, force: bool = False) -> UpdateStatus:
        """Check now. ``force``: run even with ``UPDATE_CHECK=false`` and repeat the message if seen."""
        snap = self._settings()
        if not force and not opt(snap, "UPDATE_CHECK"):
            self.last = UpdateStatus("off", self._current)
            return self.last
        repo = opt(snap, "UPDATE_REPO")
        if not repo:
            self.last = UpdateStatus("no_repo", self._current)
            return self.last
        now = clock.now().isoformat()
        try:
            async with asyncio.timeout(self._timeout):
                payload = await self._fetcher(_API.format(repo=repo))
        except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
            reason = mask(str(exc) or type(exc).__name__)[:200]
            log.warning("update check failed: %s", reason)
            await self._state.merge(K_UPDATES, {"checked_at": now, "error": reason})
            self.last = UpdateStatus("error", self._current, error=reason)
            return self.last
        release = pick_release(payload, self._current, prereleases=bool(opt(snap, "UPDATE_PRERELEASES")))
        if release is None:
            await self._state.merge(K_UPDATES, {"checked_at": now, "error": None, "latest": None})
            self.last = UpdateStatus("current", self._current)
            return self.last
        seen = (await self._state.get(K_UPDATES)).get("notified")
        notify = force or seen != release.tag
        if notify:
            await self._post(self.render(release))
        values: dict[str, Any] = {"checked_at": now, "error": None, "latest": release.tag}
        if notify:
            values["notified"] = release.tag
        await self._state.merge(K_UPDATES, values)
        self.last = UpdateStatus("available", self._current, release.tag, notified=notify)
        return self.last

    def render(self, release: Release) -> str:
        e = html.escape
        mine = parse_version(self._current)
        lines = [f"🆕 Вышла версия <b>{e(release.tag)}</b> (у вас {e(self._current)})"]
        if release.prerelease:
            lines.append("Это предварительная версия (beta).")
        if mine is not None and release.version[0] > mine[0]:
            lines.append(
                "⚠️ Новая мажорная версия: перед обновлением прочитайте инструкцию в описании релиза."
            )
        if release.notes:
            lines += ["", "<b>Для владельца:</b>", f"<blockquote expandable>{e(release.notes)}</blockquote>"]
        lines += [
            "",
            "Обновить на сервере: <code>svbg update</code> — перед обновлением бот сам сделает бэкап.",
        ]
        if release.url:
            lines.append(f'<a href="{e(release.url, quote=True)}">Описание релиза</a>')
        return "\n".join(lines)


def describe(status: UpdateStatus | None) -> str:
    """One line for the ops screen."""
    if status is None:
        return "ещё не проверялось"
    texts: Mapping[str, str] = {
        "off": "проверка выключена (UPDATE_CHECK)",
        "no_repo": "не задан репозиторий релизов (UPDATE_REPO)",
        "current": f"у вас последняя версия {status.current}",
        "available": f"доступна {status.latest} (у вас {status.current})",
        "error": f"не удалось проверить: {status.error}",
    }
    return texts.get(status.state, status.state)
