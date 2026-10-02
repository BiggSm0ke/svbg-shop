"""In-memory content snapshot with atomic swap (07 §2.7).

:class:`ContentStore` loads screens, buttons and media in one read transaction and builds an immutable
:class:`ContentSnapshot`: screens by id and code, visibility conditions compiled to closures and per-language
keyboard templates (rows already grouped and sorted, labels resolved). A click then needs no SQL for content.

``reload()`` builds the new snapshot completely before swapping a single reference, so concurrent readers
see either the old or the new version, never a mix. Bad rows never break the snapshot: an invalid button
is dropped, a button with an invalid condition is hidden (fail-closed), an invalid screen is skipped; each
case is listed in ``snapshot.problems`` for the admin UI.

The first ``load()`` also deletes system buttons that are no longer seeded while they are untouched
(``defaults.RETIRED_SYSTEM_BUTTONS``: the old staff buttons of ``home``).

With ``media_root`` the first ``load()`` also installs the default banner (:mod:`svbg.content.banner`): the
packaged picture goes to the media directory, new system screens are seeded with it and, once per
installation, every system screen without a picture of its own gets it. A failure there is logged and never
stops the bot.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.content import defaults
from svbg.content.banner import BannerSeed, banner_active, banner_mode, ensure_banner_media
from svbg.content.model import (
    MEDIA_KINDS,
    Button,
    ContentError,
    Media,
    Screen,
    TextBlock,
)
from svbg.content.tables import media, screen_buttons, screens
from svbg.tg.ui.conditions import Condition, ConditionError, compile_condition

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "PLACEHOLDER_RE",
    "ButtonTemplate",
    "ContentSnapshot",
    "ContentStore",
    "KeyboardTemplate",
    "ScreenEntry",
    "build_snapshot",
    "compile_keyboard",
    "seed_system_screens",
]

log = logging.getLogger("svbg.content")

# ``{name}`` placeholders in labels and texts; no format specs, no attribute access, no escapes.
PLACEHOLDER_RE: Final = re.compile(r"\{([a-z_][a-z0-9_]{0,31})\}")


def _never(_ctx: Any) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class ButtonTemplate:
    """A button prepared for one language. ``memo`` is a renderer-owned cache (e.g. built Telegram button)."""

    button: Button
    label: str
    needs_format: bool
    condition: Condition | None  # None: always visible
    memo: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class KeyboardTemplate:
    rows: tuple[tuple[ButtonTemplate, ...], ...]

    @property
    def is_empty(self) -> bool:
        return not self.rows


EMPTY_KEYBOARD: Final = KeyboardTemplate(())


@dataclass(frozen=True, slots=True)
class ScreenEntry:
    screen: Screen
    keyboards: Mapping[str, KeyboardTemplate]
    default_lang: str

    @property
    def code(self) -> str | None:
        return self.screen.code

    @property
    def id(self) -> int:
        return self.screen.id

    def keyboard(self, lang: str) -> KeyboardTemplate:
        kb = self.keyboards.get(lang) or self.keyboards.get(self.default_lang)
        return kb if kb is not None else EMPTY_KEYBOARD

    def text(self, lang: str) -> TextBlock:
        return self.screen.text(lang, self.default_lang)


@dataclass(frozen=True, slots=True)
class ContentSnapshot:
    version: int
    by_id: Mapping[int, ScreenEntry]
    by_code: Mapping[str, ScreenEntry]
    media: Mapping[int, Media]
    problems: tuple[str, ...] = ()
    default_lang: str = "ru"
    build_ms: float = 0.0

    def get_screen(self, code_or_id: str | int) -> ScreenEntry | None:
        if isinstance(code_or_id, int):
            return self.by_id.get(code_or_id)
        if code_or_id.isdigit():
            return self.by_id.get(int(code_or_id)) if len(code_or_id) < 19 else None
        return self.by_code.get(code_or_id)

    def get_media(self, media_id: int | None) -> Media | None:
        return None if media_id is None else self.media.get(media_id)


EMPTY_SNAPSHOT: Final = ContentSnapshot(0, MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))


def _pick_label(labels: Mapping[str, str], lang: str, default_lang: str) -> str:
    text = labels.get(lang) or labels.get(default_lang)
    if not text:
        text = next((t for t in labels.values() if t.strip()), "")
    return text


def compile_keyboard(
    buttons: Sequence[Button],
    lang: str,
    default_lang: str = "ru",
    conditions: Mapping[int, Condition | None] | None = None,
) -> KeyboardTemplate:
    """Group enabled buttons by ``row`` (ordered by ``row, sort``), resolving labels for ``lang``.

    ``conditions`` maps ``id(button)`` to its compiled condition; when absent, ``visible_if`` is compiled
    here (raising :class:`ConditionError` for invalid DSL).
    """
    ordered = sorted(
        (b for b in buttons if b.enabled), key=lambda b: (b.row, b.sort, b.id if b.id is not None else 0)
    )
    rows: list[tuple[ButtonTemplate, ...]] = []
    for _row, group in itertools.groupby(ordered, key=lambda b: b.row):
        templates: list[ButtonTemplate] = []
        for b in group:
            label = _pick_label(b.label, lang, default_lang)
            if not label.strip():
                continue
            if conditions is not None and id(b) in conditions:
                cond = conditions[id(b)]
            else:
                cond = None if b.visible_if is None else compile_condition(b.visible_if)
            templates.append(ButtonTemplate(b, label, PLACEHOLDER_RE.search(label) is not None, cond))
        if templates:
            rows.append(tuple(templates))
    return KeyboardTemplate(tuple(rows))


def _languages(screen: Screen) -> set[str]:
    langs = set(screen.body)
    for b in screen.buttons:
        langs.update(b.label)
    return langs


def _screen_label(row: Mapping[str, Any]) -> str:
    return f"screen {row.get('code') or row.get('id')}"


def _media_from_row(row: Mapping[str, Any]) -> Media:
    kind = row.get("kind")
    if kind not in MEDIA_KINDS:
        raise ContentError("kind", f"unknown media kind {kind!r}")
    file_ids = row.get("file_ids") or {}
    if not isinstance(file_ids, Mapping):
        raise ContentError("file_ids", "expected an object")
    return Media(
        id=int(row["id"]),
        kind=kind,
        sha256=str(row["sha256"]),
        path=row.get("path"),
        mime=row.get("mime"),
        size=row.get("size"),
        width=row.get("width"),
        height=row.get("height"),
        duration=row.get("duration"),
        file_ids=MappingProxyType({str(k): str(v) for k, v in file_ids.items()}),
    )


def build_snapshot(
    screen_rows: Iterable[Mapping[str, Any]],
    button_rows: Iterable[Mapping[str, Any]],
    media_rows: Iterable[Mapping[str, Any]],
    *,
    version: int,
    default_lang: str = "ru",
) -> ContentSnapshot:
    """Pure snapshot builder (no I/O): parse rows, compile conditions, pre-build keyboards."""
    started = time.perf_counter()
    problems: list[str] = []

    media_by_id: dict[int, Media] = {}
    for row in media_rows:
        try:
            m = _media_from_row(row)
        except (ContentError, KeyError, TypeError, ValueError) as e:
            problems.append(f"media {row.get('id')}: {e}")
            continue
        media_by_id[m.id] = m

    buttons_by_screen: dict[int, list[Button]] = {}
    conditions: dict[int, Condition | None] = {}
    for row in button_rows:
        try:
            button = Button.from_row(row)
        except (ContentError, KeyError, TypeError, ValueError) as e:
            problems.append(f"button {row.get('id')} (screen {row.get('screen_id')}): {e}")
            continue
        if button.visible_if is not None:
            try:
                conditions[id(button)] = compile_condition(button.visible_if)
            except ConditionError as e:
                problems.append(f"button {button.id}: condition hidden the button ({e})")
                conditions[id(button)] = _never
        else:
            conditions[id(button)] = None
        buttons_by_screen.setdefault(int(row["screen_id"]), []).append(button)

    by_id: dict[int, ScreenEntry] = {}
    by_code: dict[str, ScreenEntry] = {}
    for row in screen_rows:
        try:
            screen = Screen.from_row(row, buttons_by_screen.get(int(row["id"]), ()))
        except (ContentError, KeyError, TypeError, ValueError) as e:
            problems.append(f"{_screen_label(row)}: {e}")
            continue
        if screen.code in defaults.RESERVED_CODES:
            problems.append(f"{_screen_label(row)}: code is reserved")
            continue
        if screen.media_id is not None and screen.media_id not in media_by_id:
            problems.append(f"{_screen_label(row)}: media {screen.media_id} is missing")
        langs = _languages(screen) | {default_lang}
        keyboards = {
            lang: compile_keyboard(screen.buttons, lang, default_lang, conditions) for lang in sorted(langs)
        }
        entry = ScreenEntry(screen, MappingProxyType(keyboards), default_lang)
        by_id[screen.id] = entry
        if screen.code:
            by_code[screen.code] = entry

    elapsed = (time.perf_counter() - started) * 1000
    return ContentSnapshot(
        version=version,
        by_id=MappingProxyType(by_id),
        by_code=MappingProxyType(by_code),
        media=MappingProxyType(media_by_id),
        problems=tuple(problems),
        default_lang=default_lang,
        build_ms=elapsed,
    )


def _seed_row(s: defaults.SeedScreen, banner: BannerSeed | None) -> dict[str, Any]:
    mode = banner_mode(s.body, banner.preview_ok) if banner is not None else None
    return {
        "code": s.code,
        "kind": "system",
        "title": dict(s.title),
        "body": {lang: dict(block) for lang, block in s.body.items()},
        "media_id": banner.media_id if banner is not None and mode is not None else None,
        "media_mode": mode or s.media_mode,
    }


async def seed_system_screens(
    conn: AsyncConnection,
    seeds: Sequence[defaults.SeedScreen] | None = None,
    *,
    banner: BannerSeed | None = None,
) -> int:
    """Create missing system screens and system buttons. Returns how many rows were inserted.

    ``banner``: the screens created now show the default banner (an attachment, a link preview for a text
    over the caption limit with ``PUBLIC_URL``, else none); existing screens are never touched here.
    """
    seeds = defaults.SYSTEM_SCREENS if seeds is None else seeds
    if not seeds:
        return 0
    inserted = 0
    stmt = pg_insert(screens).values([_seed_row(s, banner) for s in seeds])
    stmt = stmt.on_conflict_do_nothing(index_elements=[screens.c.code]).returning(
        screens.c.id, screens.c.code, screens.c.media_id
    )
    created = (await conn.execute(stmt)).all()
    inserted += len(created)
    bare = [str(r.code) for r in created if r.media_id is None]
    if banner is not None and bare:
        log.warning(
            "content: %d new system screen(s) without the default banner (text over the caption limit, "
            "no PUBLIC_URL): %s",
            len(bare),
            ", ".join(bare),
        )

    codes = [s.code for s in seeds]
    rows = (
        await conn.execute(sa.select(screens.c.id, screens.c.code).where(screens.c.code.in_(codes)))
    ).all()
    ids = {r.code: r.id for r in rows}
    values = [
        {
            "screen_id": ids[s.code],
            "system_key": b.system_key,
            "row": b.row,
            "sort": b.sort,
            "label": dict(b.label),
            "icon_custom_emoji_id": b.icon_custom_emoji_id,
            "style": b.style,
            "action": dict(b.action),
            "visible_if": dict(b.visible_if) if b.visible_if is not None else sa.null(),
        }
        for s in seeds
        if s.code in ids
        for b in s.buttons
    ]
    if values:
        bstmt = (
            pg_insert(screen_buttons)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[screen_buttons.c.screen_id, screen_buttons.c.system_key],
                index_where=screen_buttons.c.system_key.is_not(None),
            )
            .returning(screen_buttons.c.id)
        )
        inserted += len((await conn.execute(bstmt)).all())
    return inserted


_SCREEN_COLS: Final = (
    screens.c.id,
    screens.c.code,
    screens.c.kind,
    screens.c.title,
    screens.c.body,
    screens.c.media_id,
    screens.c.media_mode,
    screens.c.enabled,
    screens.c.version,
    screens.c.updated_by,
    screens.c.updated_at,
)


class ContentStore:
    """Holds the current :class:`ContentSnapshot`; ``reload()`` swaps it atomically.

    ``media_root`` (``DATA_DIR/media``) turns on the default banner; ``preview_available`` says whether
    ``PUBLIC_URL`` is set (a long text can then show the banner as a link preview).
    """

    def __init__(
        self,
        db: Database,
        *,
        default_lang: str = "ru",
        seed: bool = True,
        media_root: Path | None = None,
        preview_available: Callable[[], bool] | None = None,
    ) -> None:
        self._db = db
        self._default_lang = default_lang
        self._seed = seed
        self._media_root = media_root
        self._preview_available = preview_available
        self._snapshot: ContentSnapshot = EMPTY_SNAPSHOT
        self._versions = itertools.count(1)
        self._lock = asyncio.Lock()
        # file_id cache learned after uploads, on top of the snapshot: {(media_id, bot_id): file_id}
        self._file_ids: dict[tuple[int, str], str] = {}

    @property
    def snapshot(self) -> ContentSnapshot:
        return self._snapshot

    @property
    def version(self) -> int:
        return self._snapshot.version

    def get_screen(self, code_or_id: str | int) -> ScreenEntry | None:
        return self._snapshot.get_screen(code_or_id)

    def get_media(self, media_id: int | None) -> Media | None:
        return self._snapshot.get_media(media_id)

    async def load(self) -> ContentSnapshot:
        """First load: seed system screens (if enabled) and the default banner, then build the snapshot."""
        if self._seed:
            media_id = await self._banner_media()
            preview_ok = self._preview_ok()
            async with self._db.tx() as conn:
                banner = None
                if media_id is not None and await banner_active(conn, media_id):
                    banner = BannerSeed(media_id, preview_ok)
                created = await seed_system_screens(conn, banner=banner)
            if created:
                log.info("content: seeded %d system rows", created)
            await self._retire_buttons()
            if media_id is not None:
                await self._migrate_banner(media_id, preview_ok)
        return await self.reload()

    async def _retire_buttons(self) -> None:
        """Old staff buttons of ``home`` that are no longer seeded go away while untouched, untouched home
        buttons take the current layout (both isolated)."""
        from svbg.content.editing import relayout_system_buttons, retire_system_buttons

        try:
            async with self._db.tx() as conn:
                await retire_system_buttons(conn)
        except Exception:  # never blocks the start; retried on the next one
            log.exception("content: retiring old system buttons failed")
        try:
            async with self._db.tx() as conn:
                await relayout_system_buttons(conn)
        except Exception:  # never blocks the start; retried on the next one
            log.exception("content: moving system buttons to the new layout failed")

    def _preview_ok(self) -> bool:
        try:
            return bool(self._preview_available()) if self._preview_available is not None else False
        except Exception:  # noqa: BLE001 - unknown → no link previews
            return False

    async def _banner_media(self) -> int | None:
        """The default banner's media id (installed when needed); ``None``: off or failed (logged)."""
        if self._media_root is None:
            return None
        try:
            return await ensure_banner_media(self._db, self._media_root)
        except Exception:  # the bot works without the placeholder picture
            log.exception("content: the default banner could not be installed")
            return None

    async def _migrate_banner(self, media_id: int, preview_ok: bool) -> None:
        """Once per installation: the banner on every system screen without a picture (isolated)."""
        from svbg.content.editing import migrate_default_banner

        try:
            async with self._db.tx() as conn:
                await migrate_default_banner(conn, media_id, preview_ok=preview_ok)
        except Exception:  # never blocks the start; retried on the next one (no flag is left)
            log.exception("content: the default banner migration failed")

    async def reload(self) -> ContentSnapshot:
        """Re-read content and swap the snapshot. Concurrent calls are serialized."""
        async with self._lock:
            started = time.perf_counter()
            async with self._db.read() as conn:
                screen_rows = (await conn.execute(sa.select(*_SCREEN_COLS))).mappings().all()
                button_rows = (
                    (await conn.execute(sa.select(screen_buttons).order_by(screen_buttons.c.id)))
                    .mappings()
                    .all()
                )
                media_rows = (await conn.execute(sa.select(media))).mappings().all()
            snap = build_snapshot(
                screen_rows,
                button_rows,
                media_rows,
                version=next(self._versions),
                default_lang=self._default_lang,
            )
            self._snapshot = snap  # single reference assignment: readers see old or new, never a mix
            total_ms = (time.perf_counter() - started) * 1000
            if snap.problems:
                log.warning("content v%d: %d problem(s) in content rows", snap.version, len(snap.problems))
            log.debug(
                "content v%d loaded: %d screens, %.1f ms (build %.1f ms)",
                snap.version,
                len(snap.by_id),
                total_ms,
                snap.build_ms,
            )
            return snap

    def file_id(self, media_id: int, bot_id: int | str) -> str | None:
        key = str(bot_id)
        learned = self._file_ids.get((media_id, key))
        if learned is not None:
            return learned
        m = self._snapshot.media.get(media_id)
        return None if m is None else m.file_ids.get(key)

    async def remember_file_id(self, media_id: int, bot_id: int | str, file_id: str) -> None:
        """Cache Telegram's ``file_id`` for a media file after the first upload by this bot."""
        key = str(bot_id)
        if self.file_id(media_id, key) == file_id:
            return
        self._file_ids[(media_id, key)] = file_id
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(media)
                .where(media.c.id == media_id)
                .values(file_ids=media.c.file_ids.op("||")(sa.func.jsonb_build_object(key, file_id)))
            )

    async def forget_file_id(self, media_id: int, bot_id: int | str) -> None:
        """Drop a ``file_id`` Telegram refused (the next send uploads the file again)."""
        key = str(bot_id)
        self._file_ids.pop((media_id, key), None)
        m = self._snapshot.media.get(media_id)
        if m is not None and key in m.file_ids:
            self._file_ids[(media_id, key)] = ""  # hides the snapshot's stale value until the next reload
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(media).where(media.c.id == media_id).values(file_ids=media.c.file_ids.op("-")(key))
            )
