"""Content edits of the constructor: one transaction per change, CAS by ``screens.version``, audit, undo.

Every change of a screen or of its buttons (07 §2.4.1, §3.7):

1. runs in **one** transaction that locks the screen row (``FOR UPDATE``) and compares its ``version`` with
   the version the editor saw (``expected_version``) — two admins never overwrite each other: the second
   one gets :class:`StaleError` and re-opens the fresh screen;
2. bumps ``screens.version`` and writes a ``content_audit`` **batch** (``batch_id``): the full old and new row
   of every screen and button it touched, plus one ``note`` row (``entity='note'``, ``entity_id`` = screen id)
   with a human summary for «История»;
3. after the commit reloads the in-memory snapshot (:meth:`ContentStore.reload`, typically a few ms), so every
   user sees the change on the next click. A failed reload never turns a committed change into an error: the
   result says ``live=False`` and the reload is retried in the background until it succeeds.

:meth:`ContentEditor.undo` reverts a batch within :data:`UNDO_WINDOW` (10 minutes) if nothing changed the
screen after it (its version is still the one the batch produced); the undo is itself an audited, undoable
batch. Undos of one batch are serialized (advisory lock), an undone creation is refused while something
links to the screen, and an undone deletion is refused when its code is taken meanwhile.

A screen holds at most :data:`MAX_BUTTONS` buttons (disabled ones included: the «✏️» keyboard shows them
all, and Telegram rejects a keyboard over 100 buttons).

Validation reuses the content model (labels, actions, styles, icons, texts with entities) and the condition
compiler, so whatever is saved here renders. Errors are :class:`EditError` with a short Russian ``message``.
Rights are checked by the caller (``svbg.tg.admin.content``) — this module trusts ``actor``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.content import defaults
from svbg.content.banner import (
    CAPTION_LIMIT,
    META_KEY,
    active_banner_id,
    banner_id,
    banner_mode,
    banner_sha256,
)
from svbg.content.model import (
    MAX_TEXT,
    MEDIA_MODES,
    Button,
    ContentError,
    ScreenAction,
    parse_action,
    parse_label,
    parse_text_blocks,
    parse_title,
)
from svbg.content.tables import content_audit, media, screen_buttons, screens
from svbg.core import clock
from svbg.core.tables import config_meta
from svbg.tg.ui.conditions import ConditionError, compile_condition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.content.store import ContentStore
    from svbg.db.engine import Database

__all__ = [
    "CAPTION_LIMIT",
    "MAX_BUTTONS",
    "MAX_ROW_WIDTH",
    "UNDO_WINDOW",
    "ContentEditor",
    "EditError",
    "EditResult",
    "HistoryItem",
    "NotFoundError",
    "StaleError",
    "migrate_default_banner",
    "retire_system_buttons",
    "utf16_len",
]

log = logging.getLogger("svbg.content.editing")

UNDO_WINDOW: Final = timedelta(minutes=10)
MAX_ROW_WIDTH: Final = 8
MAX_ROW: Final = 99
MENU_ROW: Final = 9  # the seeded «🏠 Меню» row; new buttons go above it
#: Buttons per screen, disabled ones included. Telegram takes ≤ 100 buttons per keyboard; the rest is left
#: for the «✏️» service row (3) and the code-made rows of system screens.
MAX_BUTTONS: Final = 90
#: A finished broadcast still links to a screen from users' chats for this long.
BROADCAST_REF_WINDOW: Final = timedelta(days=30)
RELOAD_RETRY: Final[tuple[float, ...]] = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
NOTE: Final = "note"
BANNER_SEEDED: Final = "🖼 Заглушка по умолчанию"
BANNER_REMOVED: Final = "🖼 Заглушка убрана со всех экранов"
BANNER_RESTORED: Final = "🖼 Заглушка возвращена"

_SCREEN_FIELDS: Final = ("code", "kind", "title", "body", "media_id", "media_mode", "enabled", "version")
_BUTTON_FIELDS: Final = (
    "screen_id",
    "system_key",
    "row",
    "sort",
    "label",
    "icon_custom_emoji_id",
    "style",
    "action",
    "visible_if",
    "enabled",
)
_BUTTON_CHANGES: Final = frozenset(
    {"label", "icon_custom_emoji_id", "style", "action", "visible_if", "enabled", "row", "sort"}
)
_JSON_NULLABLE: Final = frozenset({"visible_if"})


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class EditError(ValueError):
    """A refused change; ``message`` is shown to the admin as is."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class StaleError(EditError):
    """The screen changed since the editor opened it (another admin saved first)."""

    def __init__(self, screen_id: int, current: int) -> None:
        super().__init__(
            "stale", "Экран успел изменить другой админ — открыл свежую версию, повторите правку."
        )
        self.screen_id = screen_id
        self.current = current


class NotFoundError(EditError):
    def __init__(self, what: str = "Экран") -> None:
        super().__init__("not_found", f"{what} не найден — возможно, его уже удалили.")


@dataclass(frozen=True, slots=True)
class EditResult:
    batch_id: str
    screen_id: int | None
    version: int | None  # the screen's version after the change (None when the screen was deleted)
    created_id: int | None = None  # new screen / button id
    summary: str = ""
    live: bool = True  # False: saved, but the snapshot reload failed — it is retried in the background
    warnings: tuple[str, ...] = ()
    bulk: bool = False  # one batch over many screens (the default banner): no single screen to go back to
    changed: int = 0  # screens changed by a bulk batch


@dataclass(frozen=True, slots=True)
class HistoryItem:
    batch_id: str
    ts: datetime
    actor: int | None
    summary: str
    undone: bool
    is_undo: bool

    def undoable(self, now: datetime, window: timedelta = UNDO_WINDOW) -> bool:
        return not self.undone and now - self.ts <= window


# ------------------------------------------------------------------------------------------------ helpers


def _row_dict(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {f: row[f] for f in fields}


def _db_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """JSON ``None`` of nullable JSONB columns must become SQL NULL, not the JSON value ``null``."""
    return {k: (sa.null() if v is None and k in _JSON_NULLABLE else v) for k, v in values.items()}


def _jsonb(value: Any) -> Any:
    return sa.null() if value is None else value


def _content_error(e: ContentError | ConditionError) -> EditError:
    return EditError("invalid", f"Не сохранено: {e.message} ({e.path})")


def _check_button(values: Mapping[str, Any]) -> Button:
    """Parse the button as the snapshot will (labels, action, style, icon, condition)."""
    try:
        button = Button.from_row(values)
        cond = values.get("visible_if")
        if cond is not None:
            compile_condition(cond if isinstance(cond, Mapping) else {"": cond})
    except (ContentError, ConditionError) as e:
        raise _content_error(e) from None
    return button


def _caption_problem(body: Mapping[str, Any]) -> str | None:
    """A language whose text is too long for a media caption (attach mode), if any."""
    for lang, block in body.items():
        text = block.get("text", "") if isinstance(block, Mapping) else str(block)
        if utf16_len(text) > CAPTION_LIMIT:
            return lang
    return None


def _refs_text(refs: Sequence[str]) -> str:
    return "; ".join(refs[:5]) + (f" и ещё {len(refs) - 5}" if len(refs) > 5 else "")


class _Audit:
    """Collects the rows of one ``content_audit`` batch and writes them in the caller's transaction."""

    def __init__(self, conn: AsyncConnection, batch_id: str, actor: int | None) -> None:
        self.conn = conn
        self.batch_id = batch_id
        self.actor = actor
        self.rows: list[dict[str, Any]] = []

    def add(self, entity: str, entity_id: int | str, old: Any, new: Any) -> None:
        self.rows.append(
            {
                "batch_id": self.batch_id,
                "entity": entity,
                "entity_id": str(entity_id),
                "old": _jsonb(old),
                "new": _jsonb(new),
                "actor": self.actor,
            }
        )

    async def flush(self) -> None:
        if self.rows:
            await self.conn.execute(sa.insert(content_audit), self.rows)
            self.rows.clear()


# ------------------------------------------------------------------------------------------------ banner


async def _is_banner_media(conn: AsyncConnection, media_id: int | None) -> bool:
    if media_id is None:
        return False
    sha = (await conn.execute(sa.select(media.c.sha256).where(media.c.id == media_id))).scalar_one_or_none()
    return sha is not None and sha == banner_sha256()


async def _put_banner(
    audit: _Audit, media_id: int, *, preview_ok: bool, summary: str, system_only: bool
) -> tuple[int, list[str]]:
    """The banner on every screen without a picture (``system_only``: system screens only), audited in
    ``audit``'s batch. Returns ``(changed, skipped)``: skipped screens have a text over the caption limit
    while ``PUBLIC_URL`` is not set. A screen with a picture of its own is never selected."""
    conn = audit.conn
    query = sa.select(screens).where(screens.c.media_id.is_(None))
    if system_only:
        query = query.where(screens.c.kind == "system")
    rows = (await conn.execute(query.order_by(screens.c.id).with_for_update())).mappings().all()
    changed = 0
    skipped: list[str] = []
    for old in rows:
        mode = banner_mode(old["body"], preview_ok)
        if mode is None:
            skipped.append(str(old["code"] or f"#{old['id']}"))
            continue
        new = (
            (
                await conn.execute(
                    sa.update(screens)
                    .where(screens.c.id == old["id"], screens.c.media_id.is_(None))
                    .values(
                        media_id=media_id,
                        media_mode=mode,
                        version=screens.c.version + 1,
                        updated_by=audit.actor,
                        updated_at=sa.func.now(),
                    )
                    .returning(screens)
                )
            )
            .mappings()
            .one()
        )
        audit.add("screen", old["id"], _row_dict(old, _SCREEN_FIELDS), _row_dict(new, _SCREEN_FIELDS))
        audit.add(NOTE, old["id"], None, {"summary": summary, "bulk": True})
        changed += 1
    await audit.flush()
    return changed, skipped


def _skipped_text(skipped: Sequence[str]) -> str:
    return (
        f"без заглушки остались {_refs_text(list(skipped))}: текст длиннее {CAPTION_LIMIT} символов, а "
        "PUBLIC_URL не задан (с ним заглушка покажется превью-ссылкой)."
    )


async def migrate_default_banner(conn: AsyncConnection, media_id: int, *, preview_ok: bool) -> int | None:
    """Once per installation (``config_meta['content.default_banner_v1']``): the default banner on every
    system screen without a picture of its own, as one audited batch (no actor). Returns how many screens got
    it; ``None`` — already done. The flag row is claimed first in the same transaction, so two processes
    starting together migrate once and a failure leaves no flag behind."""
    claimed = (
        await conn.execute(
            pg_insert(config_meta)
            .values(key=META_KEY, value={"media_id": media_id}, updated_at=sa.func.now())
            .on_conflict_do_nothing(index_elements=[config_meta.c.key])
            .returning(config_meta.c.key)
        )
    ).first()
    if claimed is None:
        return None
    audit = _Audit(conn, uuid.uuid4().hex, None)
    changed, skipped = await _put_banner(
        audit, media_id, preview_ok=preview_ok, summary=BANNER_SEEDED, system_only=True
    )
    await conn.execute(
        sa.update(config_meta)
        .where(config_meta.c.key == META_KEY)
        .values(
            value={
                "media_id": media_id,
                "sha256": banner_sha256(),
                "screens": changed,
                "skipped": skipped,
                "batch_id": audit.batch_id if changed else None,
                "at": clock.now().isoformat(),
            },
            updated_at=sa.func.now(),
        )
    )
    if skipped:
        log.warning("default banner: %s", _skipped_text(skipped))
    log.info("default banner: put on %d screen(s) without a picture", changed)
    return changed


RETIRED_SUMMARY: Final = "🧹 Убраны старые кнопки сотрудников (всё теперь в «🛠 Админка»)"


async def retire_system_buttons(
    conn: AsyncConnection, retired: Sequence[tuple[str, defaults.SeedButton]] | None = None
) -> int:
    """Delete system buttons that are no longer seeded, but only rows still equal to their old seed (label,
    action, condition): a button the owner edited stays. One audited batch per screen (no actor), so
    «↩️ Отменить» in the constructor's history brings them back. Returns how many rows were deleted."""
    retired = defaults.RETIRED_SYSTEM_BUTTONS if retired is None else retired
    by_screen: dict[str, list[defaults.SeedButton]] = {}
    for code, seed in retired:
        by_screen.setdefault(code, []).append(seed)
    deleted = 0
    for code, seeds in by_screen.items():
        screen = (
            (await conn.execute(sa.select(screens).where(screens.c.code == code).with_for_update()))
            .mappings()
            .first()
        )
        if screen is None:
            continue
        keys = [s.system_key for s in seeds]
        rows = (
            (
                await conn.execute(
                    sa.select(screen_buttons)
                    .where(screen_buttons.c.screen_id == screen["id"], screen_buttons.c.system_key.in_(keys))
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        wanted = {s.system_key: s for s in seeds}
        stale = [
            r
            for r in rows
            if (seed := wanted.get(str(r["system_key"]))) is not None
            and r["label"] == dict(seed.label)
            and r["action"] == dict(seed.action)
            and (r["visible_if"] or None) == (dict(seed.visible_if) if seed.visible_if is not None else None)
        ]
        if not stale:
            continue
        audit = _Audit(conn, uuid.uuid4().hex, None)
        for r in stale:
            await conn.execute(sa.delete(screen_buttons).where(screen_buttons.c.id == r["id"]))
            audit.add("button", r["id"], _row_dict(r, _BUTTON_FIELDS), None)
        new = (
            (
                await conn.execute(
                    sa.update(screens)
                    .where(screens.c.id == screen["id"])
                    .values(version=screens.c.version + 1, updated_at=sa.func.now())
                    .returning(screens)
                )
            )
            .mappings()
            .one()
        )
        audit.add("screen", screen["id"], _row_dict(screen, _SCREEN_FIELDS), _row_dict(new, _SCREEN_FIELDS))
        audit.add(NOTE, screen["id"], None, {"summary": RETIRED_SUMMARY})
        await audit.flush()
        deleted += len(stale)
        log.info("content: retired %d old system button(s) of «%s»", len(stale), code)
    return deleted


RELAYOUT_SUMMARY: Final = (
    "🧭 Главное меню по-новому: «📱 Подписка» вместо «Купить», «Продлить» и «Устройства»"
)


def _same_as_seed(row: Mapping[str, Any], seed: defaults.SeedButton) -> bool:
    return (
        row["label"] == dict(seed.label)
        and row["action"] == dict(seed.action)
        and (row["visible_if"] or None) == (dict(seed.visible_if) if seed.visible_if is not None else None)
        and int(row["row"]) == seed.row
        and int(row["sort"]) == seed.sort
        and (row["style"] or None) == seed.style
        and (row["icon_custom_emoji_id"] or None) == seed.icon_custom_emoji_id
        and row["enabled"] is not False
    )


async def relayout_system_buttons(
    conn: AsyncConnection,
    moves: Sequence[tuple[str, defaults.SeedButton, defaults.SeedButton | None]] | None = None,
) -> int:
    """Move system buttons whose seed changed to the new seed (``None``: delete), but only rows still equal to
    their old seed in every field the constructor edits: a button the owner touched stays as it is. One
    audited batch per screen, so «↩️ Отменить» in the constructor's history brings the old layout back.
    Returns how many rows changed."""
    moves = defaults.RELAYOUT_SYSTEM_BUTTONS if moves is None else moves
    by_screen: dict[str, dict[str, tuple[defaults.SeedButton, defaults.SeedButton | None]]] = {}
    for code, old, new in moves:
        by_screen.setdefault(code, {})[old.system_key] = (old, new)
    changed = 0
    for code, wanted in by_screen.items():
        screen = (
            (await conn.execute(sa.select(screens).where(screens.c.code == code).with_for_update()))
            .mappings()
            .first()
        )
        if screen is None:
            continue
        rows = (
            (
                await conn.execute(
                    sa.select(screen_buttons)
                    .where(
                        screen_buttons.c.screen_id == screen["id"],
                        screen_buttons.c.system_key.in_(list(wanted)),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        stale = [
            (r, wanted[str(r["system_key"])][1])
            for r in rows
            if _same_as_seed(r, wanted[str(r["system_key"])][0])
        ]
        if not stale:
            continue
        audit = _Audit(conn, uuid.uuid4().hex, None)
        for r, new in stale:
            if new is None:
                await conn.execute(sa.delete(screen_buttons).where(screen_buttons.c.id == r["id"]))
                audit.add("button", r["id"], _row_dict(r, _BUTTON_FIELDS), None)
                continue
            after = (
                (
                    await conn.execute(
                        sa.update(screen_buttons)
                        .where(screen_buttons.c.id == r["id"])
                        .values(
                            label=dict(new.label),
                            action=dict(new.action),
                            visible_if=dict(new.visible_if) if new.visible_if is not None else sa.null(),
                            row=new.row,
                            sort=new.sort,
                            style=new.style,
                            icon_custom_emoji_id=new.icon_custom_emoji_id,
                        )
                        .returning(screen_buttons)
                    )
                )
                .mappings()
                .one()
            )
            audit.add("button", r["id"], _row_dict(r, _BUTTON_FIELDS), _row_dict(after, _BUTTON_FIELDS))
        new_screen = (
            (
                await conn.execute(
                    sa.update(screens)
                    .where(screens.c.id == screen["id"])
                    .values(version=screens.c.version + 1, updated_at=sa.func.now())
                    .returning(screens)
                )
            )
            .mappings()
            .one()
        )
        audit.add(
            "screen", screen["id"], _row_dict(screen, _SCREEN_FIELDS), _row_dict(new_screen, _SCREEN_FIELDS)
        )
        audit.add(NOTE, screen["id"], None, {"summary": RELAYOUT_SUMMARY})
        await audit.flush()
        changed += len(stale)
        log.info("content: %d system button(s) of «%s» moved to the new layout", len(stale), code)
    return changed


# ------------------------------------------------------------------------------------------------ editor

BatchFn = Callable[["AsyncConnection", dict[str, Any], _Audit], Awaitable[dict[str, Any] | None]]
ReloadHook = Callable[[], Awaitable[Any]]


class ContentEditor:
    """Write side of the constructor. ``store`` (and extra hooks) are reloaded after every change."""

    def __init__(
        self,
        db: Database,
        store: ContentStore | None = None,
        *,
        undo_window: timedelta = UNDO_WINDOW,
        preview_available: Callable[[], bool] | None = None,
        reload_retry: Sequence[float] = RELOAD_RETRY,
    ) -> None:
        """``preview_available``: does the «превью-ссылка» media mode work now (``PUBLIC_URL`` is set)?
        Without it the media goes as an attachment, so caption limits apply to preview screens too.
        ``None`` — assume it works."""
        self._db = db
        self._store = store
        self.undo_window = undo_window
        self._preview_available = preview_available
        self._reload_retry = tuple(reload_retry) or RELOAD_RETRY
        self._retry_task: asyncio.Task[None] | None = None
        self._hooks: list[ReloadHook] = []

    def add_reload_hook(self, hook: ReloadHook) -> None:
        self._hooks.append(hook)

    @property
    def reload_pending(self) -> bool:
        """A failed snapshot reload is being retried in the background."""
        return self._retry_task is not None and not self._retry_task.done()

    async def _reload(self) -> bool:
        """Reload the snapshot after a commit; ``False``: it failed and is retried in the background."""
        live = True
        if self._store is not None:
            try:
                await self._store.reload()
            except Exception:  # the change is committed: never report it as a failed edit
                log.exception("content edit: snapshot reload failed; retrying in the background")
                self._schedule_reload()
                live = False
        for hook in self._hooks:
            try:
                await hook()
            except Exception:  # isolation: another store must not hide a committed change
                log.exception("content edit: reload hook failed")
        return live

    def _schedule_reload(self) -> None:
        if self.reload_pending:
            return
        self._retry_task = asyncio.get_running_loop().create_task(
            self._retry_reload(), name="content-snapshot-reload"
        )

    async def _retry_reload(self) -> None:
        assert self._store is not None
        attempt = 0
        while True:
            await asyncio.sleep(self._reload_retry[min(attempt, len(self._reload_retry) - 1)])
            attempt += 1
            try:
                await self._store.reload()
            except Exception as e:  # noqa: BLE001 - keep trying; a restart also reloads
                log.warning("content snapshot reload retry %d failed: %s", attempt, type(e).__name__)
                continue
            log.info("content snapshot reloaded after %d retries", attempt)
            return

    async def aclose(self) -> None:
        """Stop a pending background reload (shutdown)."""
        task, self._retry_task = self._retry_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def preview_ok(self) -> bool:
        """The «превью-ссылка» media mode works now (``PUBLIC_URL`` is set)."""
        if self._preview_available is None:
            return True
        try:
            return bool(self._preview_available())
        except Exception:  # noqa: BLE001 - unknown → be strict
            return False

    def as_attachment(self, mode: str | None) -> bool:
        """Media of this mode goes as an attachment (caption ≤ 1024): ``attach``, or ``preview`` without
        ``PUBLIC_URL``."""
        return mode == "attach" or (mode == "preview" and not self.preview_ok())

    def _caption_error(self, mode: str | None, lang: str | None = None) -> EditError:
        which = f" ({lang})" if lang else ""
        if mode == "preview":
            return EditError(
                "caption_too_long",
                f"PUBLIC_URL не задан — «превью-ссылка» не работает, медиа уходит вложением, а подпись — до "
                f"{CAPTION_LIMIT} символов. Текст{which} длиннее: сократите его или задайте PUBLIC_URL.",
            )
        return EditError(
            "caption_too_long",
            f"У экрана медиа-вложение: подпись — до {CAPTION_LIMIT} символов, а текст{which} длиннее. "
            "Сократите текст или переключите режим медиа на «превью-ссылку» (там до 4096).",
        )

    # ------------------------------------------------------------ core

    async def _lock_screen(self, conn: AsyncConnection, screen_id: int) -> dict[str, Any]:
        row = (
            (await conn.execute(sa.select(screens).where(screens.c.id == screen_id).with_for_update()))
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError()
        return dict(row)

    async def _buttons(self, conn: AsyncConnection, screen_id: int) -> list[dict[str, Any]]:
        rows = (
            (
                await conn.execute(
                    sa.select(screen_buttons)
                    .where(screen_buttons.c.screen_id == screen_id)
                    .order_by(screen_buttons.c.row, screen_buttons.c.sort, screen_buttons.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    async def _run(
        self,
        screen_id: int,
        expected_version: int | None,
        actor: int | None,
        summary: str,
        fn: BatchFn,
        *,
        created: Callable[[], int | None] | None = None,
        warnings: Sequence[str] = (),
    ) -> EditResult:
        """``warnings`` (filled by ``fn``) go to the result after the commit."""
        batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            old = await self._lock_screen(conn, screen_id)
            if expected_version is not None and old["version"] != expected_version:
                raise StaleError(screen_id, int(old["version"]))
            audit = _Audit(conn, batch, actor)
            changes = await fn(conn, old, audit) or {}
            new_row = (
                (
                    await conn.execute(
                        sa.update(screens)
                        .where(screens.c.id == screen_id)
                        .values(
                            version=screens.c.version + 1,
                            updated_by=actor,
                            updated_at=sa.func.now(),
                            **changes,
                        )
                        .returning(screens)
                    )
                )
                .mappings()
                .one()
            )
            audit.add("screen", screen_id, _row_dict(old, _SCREEN_FIELDS), _row_dict(new_row, _SCREEN_FIELDS))
            audit.add(NOTE, screen_id, None, {"summary": summary})
            await audit.flush()
        live = await self._reload()
        log.info("content: screen %s v%s by %s — %s", screen_id, new_row["version"], actor, summary)
        return EditResult(
            batch,
            screen_id,
            int(new_row["version"]),
            created() if created else None,
            summary=summary,
            live=live,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------ screens

    async def set_text(
        self,
        screen_id: int,
        lang: str,
        text: str,
        entities: Sequence[Mapping[str, Any]] | None,
        *,
        expected_version: int | None,
        actor: int | None,
    ) -> EditResult:
        """Body text of ``lang`` with its entities (offsets in UTF-16, as Telegram sent them)."""
        if not text.strip():
            raise EditError("empty", "Пустой текст не сохранён — пришлите сообщение с текстом.")
        if utf16_len(text) > MAX_TEXT:
            raise EditError("too_long", f"Текст длиннее {MAX_TEXT} символов — сократите его.")
        block = {"text": text, "entities": [dict(e) for e in entities or ()]}
        try:
            parse_text_blocks({lang: block})
        except ContentError as e:
            raise _content_error(e) from None

        warnings: list[str] = []

        async def fn(conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            changes: dict[str, Any] = {}
            if (
                old["media_id"] is not None
                and self.as_attachment(old["media_mode"])
                and utf16_len(text) > CAPTION_LIMIT
            ):
                if not await _is_banner_media(conn, old["media_id"]):
                    raise self._caption_error(old["media_mode"])
                # the placeholder never blocks a text: a link preview when it works, else it goes away
                if self.preview_ok():
                    changes["media_mode"] = "preview"
                    warnings.append(
                        f"Текст длиннее {CAPTION_LIMIT} символов — заглушка теперь показывается "
                        "превью-ссылкой."
                    )
                else:
                    changes["media_id"] = None
                    warnings.append(
                        f"Текст длиннее {CAPTION_LIMIT} символов, а PUBLIC_URL не задан — заглушка с экрана "
                        "убрана."
                    )
            body = dict(old["body"] or {})
            body[lang] = block
            changes["body"] = body
            return changes

        return await self._run(screen_id, expected_version, actor, f"Текст ({lang})", fn, warnings=warnings)

    async def remove_text(
        self, screen_id: int, lang: str, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        async def fn(_conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            body = dict(old["body"] or {})
            if lang not in body:
                raise EditError("no_lang", "Такого перевода нет.")
            if len(body) == 1:
                raise EditError("last_lang", "Это единственный текст экрана — его можно только заменить.")
            del body[lang]
            return {"body": body}

        return await self._run(screen_id, expected_version, actor, f"Удалён текст ({lang})", fn)

    async def set_media(
        self,
        screen_id: int,
        media_id: int | None,
        *,
        expected_version: int | None,
        actor: int | None,
        summary: str | None = None,
    ) -> EditResult:
        async def fn(conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            if media_id is not None:
                kind = (
                    await conn.execute(sa.select(media.c.kind).where(media.c.id == media_id))
                ).scalar_one_or_none()
                if kind is None:
                    raise NotFoundError("Файл")
                if kind not in ("photo", "animation", "video"):
                    raise EditError("bad_kind", "На экран можно поставить фото, GIF или видео.")
                if self.as_attachment(old["media_mode"]):
                    lang = _caption_problem(old["body"] or {})
                    if lang is not None:
                        raise self._caption_error(old["media_mode"], lang)
            return {"media_id": media_id}

        text = summary or ("Медиа убрано" if media_id is None else "Новое медиа")
        return await self._run(screen_id, expected_version, actor, text, fn)

    async def set_media_mode(
        self, screen_id: int, mode: str, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        if mode not in MEDIA_MODES:
            raise EditError("bad_mode", "Неизвестный режим медиа.")

        async def fn(_conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            if self.as_attachment(mode) and old["media_id"] is not None:
                lang = _caption_problem(old["body"] or {})
                if lang is not None:
                    if mode == "preview":
                        raise self._caption_error(mode, lang)
                    raise EditError(
                        "caption_too_long",
                        f"Текст ({lang}) длиннее {CAPTION_LIMIT} символов — во вложении он не поместится. "
                        "Сократите текст или оставьте «превью-ссылку».",
                    )
            return {"media_mode": mode}

        label = "вложение" if mode == "attach" else "превью-ссылка"
        return await self._run(screen_id, expected_version, actor, f"Режим медиа: {label}", fn)

    async def set_title(
        self, screen_id: int, title: Mapping[str, str], *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        try:
            parsed = dict(parse_title(dict(title)))
        except ContentError as e:
            raise _content_error(e) from None
        if not any(v.strip() for v in parsed.values()):
            raise EditError("empty", "Название не может быть пустым.")

        async def fn(_conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            merged = dict(old["title"] or {})
            merged.update(parsed)
            return {"title": merged}

        return await self._run(screen_id, expected_version, actor, "Переименован", fn)

    async def set_screen_enabled(
        self, screen_id: int, enabled: bool, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        async def fn(_conn: AsyncConnection, old: dict[str, Any], _audit: _Audit) -> dict[str, Any]:
            if not enabled and old["kind"] == "system":
                raise EditError("system", "Системный экран нельзя выключить — без него сломается путь.")
            return {"enabled": enabled}

        return await self._run(
            screen_id, expected_version, actor, "Экран включён" if enabled else "Экран выключен", fn
        )

    async def create_screen(
        self,
        title: str,
        *,
        code: str | None = None,
        lang: str = "ru",
        actor: int | None,
        menu_target: str = defaults.HOME,
    ) -> EditResult:
        """A new custom screen with its title as the text and a «🏠 Меню» button; it shows the default banner
        while the banner is on (:func:`svbg.content.banner.banner_active`)."""
        title = " ".join(title.split())
        if not title:
            raise EditError("empty", "Название не может быть пустым.")
        if code is not None:
            code = code.lower()
            if code in defaults.RESERVED_CODES or not _valid_code(code):
                raise EditError(
                    "bad_code",
                    "Код экрана — латиница в нижнем регистре, цифры и «_», начинается с буквы, "
                    "до 32 символов.",
                )
        title_map = {lang: title[:256]}
        body = {lang: {"text": title[:MAX_TEXT], "entities": []}}
        menu = {
            "row": MENU_ROW,
            "sort": 0,
            "label": {"ru": "🏠 Меню", "en": "🏠 Menu"},
            "action": {"type": "screen", "target": menu_target},
            "enabled": True,
        }
        batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            if code is not None:
                taken = (
                    await conn.execute(sa.select(screens.c.id).where(screens.c.code == code))
                ).scalar_one_or_none()
                if taken is not None:
                    raise EditError("code_taken", f"Код «{code}» уже занят другим экраном.")
            values: dict[str, Any] = {
                "code": code,
                "kind": "custom",
                "title": title_map,
                "body": body,
                "updated_by": actor,
            }
            banner = await active_banner_id(conn)
            mode = banner_mode(body, self.preview_ok()) if banner is not None else None
            if banner is not None and mode is not None:
                values.update(media_id=banner, media_mode=mode)
            row = (
                (await conn.execute(sa.insert(screens).values(**values).returning(screens))).mappings().one()
            )
            sid = int(row["id"])
            brow = (
                (
                    await conn.execute(
                        sa.insert(screen_buttons).values(screen_id=sid, **menu).returning(screen_buttons)
                    )
                )
                .mappings()
                .one()
            )
            audit = _Audit(conn, batch, actor)
            audit.add("screen", sid, None, _row_dict(row, _SCREEN_FIELDS))
            audit.add("button", brow["id"], None, _row_dict(brow, _BUTTON_FIELDS))
            audit.add(NOTE, sid, None, {"summary": f"Создан экран «{title[:64]}»"})
            await audit.flush()
        live = await self._reload()
        return EditResult(batch, sid, int(row["version"]), sid, summary="Создан экран", live=live)

    async def screen_references(
        self,
        conn: AsyncConnection,
        screen_id: int,
        code: str | None,
        warnings: list[str] | None = None,
    ) -> list[str]:
        """Where the screen is linked from (each blocks a deletion): buttons of other screens, enabled deep
        links, buttons of broadcasts not finished or finished within :data:`BROADCAST_REF_WINDOW`.

        Disabled deep links (they may be switched on again) go to ``warnings``.
        """
        targets = [str(screen_id)] + ([code] if code else [])
        refs: list[str] = []
        rows = (
            await conn.execute(
                sa.select(
                    screen_buttons.c.id, screen_buttons.c.label, screens.c.code, screens.c.id.label("sid")
                )
                .join(screens, screens.c.id == screen_buttons.c.screen_id)
                .where(
                    screen_buttons.c.screen_id != screen_id,
                    screen_buttons.c.action["type"].astext == "screen",
                    screen_buttons.c.action["target"].astext.in_(targets),
                )
                .order_by(screen_buttons.c.id)
            )
        ).all()
        for r in rows:
            label = next(iter((r.label or {}).values()), "") if isinstance(r.label, Mapping) else ""
            refs.append(f"кнопка «{label}» на экране {r.code or r.sid}")
        if await _table_exists(conn, "deeplinks"):
            from svbg.deeplinks.tables import deeplinks

            links = (
                await conn.execute(
                    sa.select(deeplinks.c.code, deeplinks.c.enabled)
                    .where(deeplinks.c.intent["screen"].astext.in_(targets))
                    .order_by(deeplinks.c.id)
                )
            ).all()
            refs.extend(f"ссылка l_{r.code}" for r in links if r.enabled)
            if warnings is not None:
                warnings.extend(f"выключенная ссылка l_{r.code}" for r in links if not r.enabled)
        if await _table_exists(conn, "broadcasts"):
            since = clock.now() - BROADCAST_REF_WINDOW
            casts = (
                await conn.execute(
                    sa.text(
                        "SELECT id FROM broadcasts "
                        "WHERE (status IN ('draft', 'running', 'paused') "
                        "       OR coalesce(finished_at, updated_at) >= :since) "
                        "  AND EXISTS (SELECT 1 FROM jsonb_array_elements(buttons) AS b "
                        "              WHERE b -> 'action' ->> 'type' = 'screen' "
                        "                AND b -> 'action' ->> 'target' = ANY(:targets)) "
                        "ORDER BY id"
                    ),
                    {"since": since, "targets": targets},
                )
            ).all()
            refs.extend(f"кнопка рассылки #{r.id}" for r in casts)
        return refs

    async def references(self, screen_id: int, code: str | None) -> tuple[list[str], list[str]]:
        """``(blocking references, warnings)`` of :meth:`screen_references` — for the delete confirmation."""
        warnings: list[str] = []
        async with self._db.read() as conn:
            refs = await self.screen_references(conn, screen_id, code, warnings)
        return refs, warnings

    async def delete_screen(
        self, screen_id: int, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        """Delete a custom screen nothing links to (buttons go with it; undo restores both)."""
        batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            old = await self._lock_screen(conn, screen_id)
            if expected_version is not None and old["version"] != expected_version:
                raise StaleError(screen_id, int(old["version"]))
            if old["kind"] == "system":
                raise EditError("system", "Системный экран удалить нельзя — его можно только изменить.")
            warnings: list[str] = []
            refs = await self.screen_references(conn, screen_id, old["code"], warnings)
            if refs:
                raise EditError(
                    "referenced", f"На экран ведут: {_refs_text(refs)}. Сначала уберите эти ссылки."
                )
            audit = _Audit(conn, batch, actor)
            for b in await self._buttons(conn, screen_id):
                audit.add("button", b["id"], _row_dict(b, _BUTTON_FIELDS), None)
            audit.add("screen", screen_id, _row_dict(old, _SCREEN_FIELDS), None)
            title = next(iter((old["title"] or {}).values()), "") or (old["code"] or str(screen_id))
            audit.add(NOTE, screen_id, None, {"summary": f"Удалён экран «{title[:64]}»"})
            await conn.execute(sa.delete(screens).where(screens.c.id == screen_id))
            await audit.flush()
        live = await self._reload()
        return EditResult(batch, screen_id, None, summary="Удалён экран", live=live, warnings=tuple(warnings))

    # ------------------------------------------------------------ buttons

    async def _lock_button(self, conn: AsyncConnection, button_id: int) -> dict[str, Any]:
        row = (
            (
                await conn.execute(
                    sa.select(screen_buttons).where(screen_buttons.c.id == button_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError("Кнопка")
        return dict(row)

    async def screen_of_button(self, button_id: int) -> int:
        async with self._db.read() as conn:
            sid = (
                await conn.execute(
                    sa.select(screen_buttons.c.screen_id).where(screen_buttons.c.id == button_id)
                )
            ).scalar_one_or_none()
        if sid is None:
            raise NotFoundError("Кнопка")
        return int(sid)

    @staticmethod
    def _check_width(buttons: Sequence[Mapping[str, Any]], row: int) -> None:
        width = sum(1 for b in buttons if b["row"] == row and b["enabled"])
        if width > MAX_ROW_WIDTH:
            raise EditError(
                "row_full",
                f"В одном ряду Telegram показывает не больше {MAX_ROW_WIDTH} кнопок — выберите другой ряд.",
            )

    async def add_button(
        self,
        screen_id: int,
        *,
        label: Mapping[str, str],
        action: Mapping[str, Any] | str,
        expected_version: int | None,
        actor: int | None,
        row: int | None = None,
        style: str | None = None,
        icon_custom_emoji_id: str | None = None,
        visible_if: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> EditResult:
        try:
            parsed_action = parse_action(action)
            labels = dict(parse_label(dict(label)))
        except ContentError as e:
            raise _content_error(e) from None
        created: list[int] = []

        async def fn(conn: AsyncConnection, _old: dict[str, Any], audit: _Audit) -> None:
            buttons = await self._buttons(conn, screen_id)
            if len(buttons) >= MAX_BUTTONS:
                raise EditError(
                    "too_many",
                    f"На экране уже {len(buttons)} кнопок — больше {MAX_BUTTONS} Telegram не покажет "
                    "(выключенные тоже считаются). Удалите лишние кнопки.",
                )
            target_row = row
            if target_row is None:
                below = [b["row"] for b in buttons if b["row"] < MENU_ROW]
                target_row = max(below, default=-1) + 1
                if target_row >= MENU_ROW:
                    target_row = max((b["row"] for b in buttons), default=-1) + 1
            if not 0 <= target_row <= MAX_ROW:
                raise EditError("bad_row", f"Ряд — число от 0 до {MAX_ROW}.")
            sort = max((b["sort"] for b in buttons if b["row"] == target_row), default=-1) + 1
            values = {
                "screen_id": screen_id,
                "system_key": None,
                "row": target_row,
                "sort": sort,
                "label": labels,
                "icon_custom_emoji_id": icon_custom_emoji_id,
                "style": style,
                "action": parsed_action.to_json(),
                "visible_if": dict(visible_if) if visible_if is not None else None,
                "enabled": enabled,
            }
            _check_button(values)
            self._check_width([*buttons, values], target_row)
            values.pop("system_key")
            new = (
                (
                    await conn.execute(
                        sa.insert(screen_buttons).values(**_db_values(values)).returning(screen_buttons)
                    )
                )
                .mappings()
                .one()
            )
            created.append(int(new["id"]))
            audit.add("button", new["id"], None, _row_dict(new, _BUTTON_FIELDS))

        name = next((v for v in labels.values() if v.strip()), "")
        return await self._run(
            screen_id,
            expected_version,
            actor,
            f"Добавлена кнопка «{name[:48]}»",
            fn,
            created=lambda: created[0] if created else None,
        )

    async def update_button(
        self,
        button_id: int,
        *,
        expected_version: int | None,
        actor: int | None,
        summary: str | None = None,
        **changes: Any,
    ) -> EditResult:
        """Change fields of a button: ``label`` (merged by language), ``icon_custom_emoji_id``, ``style``,
        ``action``, ``visible_if``, ``enabled``, ``row``, ``sort``."""
        unknown = set(changes) - _BUTTON_CHANGES
        if unknown:
            raise ValueError(f"unknown button fields: {sorted(unknown)}")
        screen_id = await self.screen_of_button(button_id)

        async def fn(conn: AsyncConnection, _old: dict[str, Any], audit: _Audit) -> None:
            old = await self._lock_button(conn, button_id)
            if old["screen_id"] != screen_id:  # moved meanwhile (cannot happen today; be safe)
                raise StaleError(screen_id, -1)
            values = dict(old)
            if "label" in changes:
                merged = dict(old["label"] or {})
                for lang, text in dict(changes["label"]).items():
                    if text is None or not str(text).strip():
                        merged.pop(lang, None)
                    else:
                        merged[lang] = text
                values["label"] = merged
            if "action" in changes:
                if old["system_key"] is not None:
                    raise EditError(
                        "system",
                        "У системной кнопки действие менять нельзя — её можно переименовать, перекрасить, "
                        "переставить или скрыть.",
                    )
                try:
                    values["action"] = parse_action(changes["action"]).to_json()
                except ContentError as e:
                    raise _content_error(e) from None
            for key in ("icon_custom_emoji_id", "style", "enabled", "row", "sort"):
                if key in changes:
                    values[key] = changes[key]
            if "visible_if" in changes:
                cond = changes["visible_if"]
                values["visible_if"] = dict(cond) if cond else None
            if not 0 <= int(values["row"]) <= MAX_ROW:
                raise EditError("bad_row", f"Ряд — число от 0 до {MAX_ROW}.")
            _check_button(values)
            buttons = [b for b in await self._buttons(conn, screen_id) if b["id"] != button_id]
            if values["enabled"] and (values["row"] != old["row"] or not old["enabled"]):
                self._check_width([*buttons, values], int(values["row"]))
            update = {k: values[k] for k in _BUTTON_FIELDS if k not in ("screen_id", "system_key")}
            new = (
                (
                    await conn.execute(
                        sa.update(screen_buttons)
                        .where(screen_buttons.c.id == button_id)
                        .values(**_db_values(update))
                        .returning(screen_buttons)
                    )
                )
                .mappings()
                .one()
            )
            audit.add("button", button_id, _row_dict(old, _BUTTON_FIELDS), _row_dict(new, _BUTTON_FIELDS))

        text = summary or "Кнопка: " + ", ".join(sorted(changes))
        return await self._run(screen_id, expected_version, actor, text, fn)

    async def move_button(
        self, button_id: int, direction: str, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        """``left``/``right`` inside the row, ``up``/``down`` to the neighbouring row (sorts renumbered)."""
        if direction not in ("left", "right", "up", "down"):
            raise ValueError(f"unknown direction {direction!r}")
        screen_id = await self.screen_of_button(button_id)

        async def fn(conn: AsyncConnection, _old: dict[str, Any], audit: _Audit) -> None:
            buttons = await self._buttons(conn, screen_id)
            me = next((b for b in buttons if b["id"] == button_id), None)
            if me is None:
                raise NotFoundError("Кнопка")
            rows: dict[int, list[dict[str, Any]]] = {}
            for b in buttons:
                rows.setdefault(b["row"], []).append(b)
            old_state = {b["id"]: (b["row"], b["sort"]) for b in buttons}
            row = rows[me["row"]]
            idx = row.index(me)
            if direction in ("left", "right"):
                j = idx - 1 if direction == "left" else idx + 1
                if not 0 <= j < len(row):
                    raise EditError("edge", "Кнопка уже с краю ряда.")
                row[idx], row[j] = row[j], row[idx]
            else:
                new_row = me["row"] + (-1 if direction == "up" else 1)
                if not 0 <= new_row <= MAX_ROW:
                    raise EditError("edge", "Дальше двигать некуда.")
                row.remove(me)
                target = rows.setdefault(new_row, [])
                target.append(me)
                me["row"] = new_row
                if me["enabled"]:
                    self._check_width(target, new_row)
            for r, items in rows.items():
                for i, b in enumerate(items):
                    b["row"], b["sort"] = r, i
            for b in buttons:
                if old_state[b["id"]] == (b["row"], b["sort"]):
                    continue
                old_full = await self._lock_button(conn, b["id"])
                new = (
                    (
                        await conn.execute(
                            sa.update(screen_buttons)
                            .where(screen_buttons.c.id == b["id"])
                            .values(row=b["row"], sort=b["sort"])
                            .returning(screen_buttons)
                        )
                    )
                    .mappings()
                    .one()
                )
                audit.add(
                    "button", b["id"], _row_dict(old_full, _BUTTON_FIELDS), _row_dict(new, _BUTTON_FIELDS)
                )

        arrows = {"left": "←", "right": "→", "up": "↑", "down": "↓"}
        return await self._run(screen_id, expected_version, actor, f"Кнопка сдвинута {arrows[direction]}", fn)

    async def delete_button(
        self, button_id: int, *, expected_version: int | None, actor: int | None
    ) -> EditResult:
        screen_id = await self.screen_of_button(button_id)

        async def fn(conn: AsyncConnection, _old: dict[str, Any], audit: _Audit) -> None:
            old = await self._lock_button(conn, button_id)
            if old["system_key"] is not None:
                raise EditError(
                    "system", "Системную кнопку удалить нельзя — без неё сломается путь. Её можно выключить."
                )
            await conn.execute(sa.delete(screen_buttons).where(screen_buttons.c.id == button_id))
            audit.add("button", button_id, _row_dict(old, _BUTTON_FIELDS), None)

        return await self._run(screen_id, expected_version, actor, "Кнопка удалена", fn)

    # ------------------------------------------------------------ default banner (bulk)

    async def remove_banner(self, *, actor: int | None) -> EditResult:
        """«🖼 Заглушка: убрать со всех экранов» — one transaction and one audited, undoable batch. Only the
        screens that show the default banner change; the owner's pictures are never touched."""
        batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            mid = await banner_id(conn)
            rows = (
                []
                if mid is None
                else (
                    await conn.execute(
                        sa.select(screens)
                        .where(screens.c.media_id == mid)
                        .order_by(screens.c.id)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise EditError("nothing", "Заглушки нет ни на одном экране.")
            new_rows = {
                int(r["id"]): r
                for r in (
                    await conn.execute(
                        sa.update(screens)
                        .where(screens.c.id.in_([r["id"] for r in rows]), screens.c.media_id == mid)
                        .values(
                            media_id=None,
                            version=screens.c.version + 1,
                            updated_by=actor,
                            updated_at=sa.func.now(),
                        )
                        .returning(screens)
                    )
                )
                .mappings()
                .all()
            }
            audit = _Audit(conn, batch, actor)
            for old in rows:
                new = new_rows[int(old["id"])]
                audit.add("screen", old["id"], _row_dict(old, _SCREEN_FIELDS), _row_dict(new, _SCREEN_FIELDS))
                audit.add(NOTE, old["id"], None, {"summary": BANNER_REMOVED, "bulk": True})
            await audit.flush()
        live = await self._reload()
        log.info("content: default banner removed from %d screen(s) by %s", len(rows), actor)
        return EditResult(batch, None, None, summary=BANNER_REMOVED, live=live, bulk=True, changed=len(rows))

    async def restore_banner(self, media_id: int, *, actor: int | None) -> EditResult:
        """«🖼 Вернуть заглушку» — the banner (media ``media_id``) on every screen without a picture of its
        own, system and own ones; one transaction, one audited, undoable batch. A screen whose text does not
        fit a caption gets it as a link preview with ``PUBLIC_URL``, else stays without it (``warnings``)."""
        batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            if not await _is_banner_media(conn, media_id):
                raise NotFoundError("Файл заглушки")
            audit = _Audit(conn, batch, actor)
            changed, skipped = await _put_banner(
                audit, media_id, preview_ok=self.preview_ok(), summary=BANNER_RESTORED, system_only=False
            )
            if not changed:
                if skipped:
                    raise EditError("nothing", "Заглушку ставить некуда: " + _skipped_text(skipped))
                raise EditError("nothing", "Заглушку ставить некуда — у всех экранов уже есть картинка.")
        live = await self._reload()
        log.info("content: default banner restored on %d screen(s) by %s", changed, actor)
        note = _skipped_text(skipped)
        warnings = (note[:1].upper() + note[1:],) if skipped else ()
        return EditResult(
            batch,
            None,
            None,
            summary=BANNER_RESTORED,
            live=live,
            warnings=warnings,
            bulk=True,
            changed=changed,
        )

    # ------------------------------------------------------------ history & undo

    async def history(self, screen_id: int, *, limit: int = 10) -> list[HistoryItem]:
        """Latest batches that touched the screen (newest first)."""
        async with self._db.read() as conn:
            notes = (
                await conn.execute(
                    sa.select(
                        content_audit.c.batch_id,
                        content_audit.c.ts,
                        content_audit.c.actor,
                        content_audit.c.new,
                    )
                    .where(content_audit.c.entity == NOTE, content_audit.c.entity_id == str(screen_id))
                    .order_by(content_audit.c.id.desc())
                    .limit(limit)
                )
            ).all()
            batches = [n.batch_id for n in notes]
            undone: set[str] = set()
            if batches:
                rows = (
                    await conn.execute(
                        sa.select(content_audit.c.new["undo_of"].astext).where(
                            content_audit.c.entity == NOTE,
                            content_audit.c.new["undo_of"].astext.in_(batches),
                        )
                    )
                ).all()
                undone = {r[0] for r in rows}
        return [
            HistoryItem(
                n.batch_id,
                n.ts,
                n.actor,
                str((n.new or {}).get("summary") or "изменение"),
                n.batch_id in undone,
                bool((n.new or {}).get("undo_of")),
            )
            for n in notes
        ]

    async def undo(self, batch_id: str, *, actor: int | None) -> EditResult:
        """Revert batch ``batch_id`` (within the undo window, if nothing changed the screen after it)."""
        new_batch = uuid.uuid4().hex
        async with self._db.tx() as conn:
            # one undo of a batch at a time: a concurrent second one waits, then sees «уже отменено»
            await conn.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext("content_undo:" + batch_id)))
            )
            rows = (
                (
                    await conn.execute(
                        sa.select(content_audit)
                        .where(content_audit.c.batch_id == batch_id)
                        .order_by(content_audit.c.id)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise EditError("unknown", "Это изменение не найдено.")
            if clock.now() - rows[0]["ts"] > self.undo_window:
                minutes = int(self.undo_window.total_seconds() // 60)
                raise EditError("expired", f"Отменить можно только в течение {minutes} минут.")
            done = (
                await conn.execute(
                    sa.select(content_audit.c.id)
                    .where(content_audit.c.entity == NOTE, content_audit.c.new["undo_of"].astext == batch_id)
                    .limit(1)
                )
            ).first()
            if done is not None:
                raise EditError("undone", "Это изменение уже отменено.")
            note = next((r for r in rows if r["entity"] == NOTE), None)
            note_new = (note or {}).get("new") or {}
            summary = str(note_new.get("summary") or "изменение")
            bulk = bool(note_new.get("bulk"))  # one batch over many screens (the default banner)
            screen_rows = [r for r in rows if r["entity"] == "screen"]
            if not screen_rows:
                raise EditError("unknown", "Это изменение нельзя отменить отсюда.")
            screen_id = int(screen_rows[0]["entity_id"])
            produced: dict[int, Mapping[str, Any] | None] = {}
            for r in screen_rows:  # what the batch left behind, per screen (its last row wins)
                produced[int(r["entity_id"])] = r["new"]
            for sid, made in produced.items():
                await self._check_undo_screen(conn, sid, made, screen_rows, bulk=bulk)
            audit = _Audit(conn, new_batch, actor)
            version: int | None = None
            for r in reversed(rows):
                entity, old, new = r["entity"], r["old"], r["new"]
                eid = int(r["entity_id"]) if entity != NOTE else 0
                if entity == "screen":
                    version = await self._undo_screen(conn, audit, eid, old, new, actor)
                elif entity == "button":
                    await self._undo_button(conn, audit, eid, old, new)
            undo_note: dict[str, Any] = {"summary": f"↩️ Отменено: {summary}", "undo_of": batch_id}
            if bulk:
                for sid in produced:
                    audit.add(NOTE, sid, None, {**undo_note, "bulk": True})
            else:
                audit.add(NOTE, screen_id, None, undo_note)
            await audit.flush()
        live = await self._reload()
        if bulk:
            return EditResult(
                new_batch,
                None,
                None,
                summary=f"Отменено: {summary}",
                live=live,
                bulk=True,
                changed=len(produced),
            )
        return EditResult(new_batch, screen_id, version, summary=f"Отменено: {summary}", live=live)

    async def _check_undo_screen(
        self,
        conn: AsyncConnection,
        sid: int,
        produced: Mapping[str, Any] | None,
        screen_rows: Sequence[Mapping[str, Any]],
        *,
        bulk: bool,
    ) -> None:
        """CAS of an undo: the screen must still be exactly what the batch left behind (locked)."""
        current = (
            (await conn.execute(sa.select(screens).where(screens.c.id == sid).with_for_update()))
            .mappings()
            .first()
        )
        if produced is None:
            if current is not None:
                raise EditError("changed", "Экран уже создан заново — отмена невозможна.")
        elif current is None or current["version"] != produced.get("version"):
            which = f" «{produced.get('code') or sid}»" if bulk else ""
            raise EditError(
                "changed",
                f"После этого экран{which} уже меняли — отмена затёрла бы чужие правки. Откройте историю.",
            )
        if current is not None and any(r["old"] is None for r in screen_rows if int(r["entity_id"]) == sid):
            # undoing a creation deletes the screen: nothing may link to it by now
            refs = await self.screen_references(conn, sid, current["code"])
            if refs:
                raise EditError(
                    "referenced",
                    f"На экран уже ведут: {_refs_text(refs)}. Сначала уберите эти ссылки — "
                    "иначе кнопки будут вести в никуда.",
                )

    async def _undo_screen(  # noqa: PLR0917 - private helper of undo()
        self,
        conn: AsyncConnection,
        audit: _Audit,
        sid: int,
        old: Mapping[str, Any] | None,
        new: Mapping[str, Any] | None,
        actor: int | None,
    ) -> int | None:
        fields = [f for f in _SCREEN_FIELDS if f != "version"]
        if old is None:  # the batch created the screen: remove it (its buttons were removed just before)
            cur = (await conn.execute(sa.select(screens).where(screens.c.id == sid))).mappings().first()
            if cur is not None:
                await conn.execute(sa.delete(screens).where(screens.c.id == sid))
                audit.add("screen", sid, _row_dict(cur, _SCREEN_FIELDS), None)
            return None
        if new is None:  # the batch deleted it: put it back with the same id
            values = {f: old.get(f) for f in fields}
            if (await conn.execute(sa.select(screens.c.id).where(screens.c.id == sid))).first() is not None:
                raise EditError("changed", "Экран уже восстановлен.")
            code = values.get("code")
            if code is not None:
                taken = (
                    await conn.execute(sa.select(screens.c.id).where(screens.c.code == code))
                ).scalar_one_or_none()
                if taken is not None:
                    raise EditError(
                        "code_taken",
                        f"Код «{code}» уже занят другим экраном (#{taken}) — удалённый экран не вернуть. "
                        "Удалите или переименуйте новый экран и повторите.",
                    )
            row = (
                (
                    await conn.execute(
                        sa.insert(screens)
                        .values(id=sid, version=int(old.get("version") or 1) + 1, updated_by=actor, **values)
                        .returning(screens)
                    )
                )
                .mappings()
                .one()
            )
            audit.add("screen", sid, None, _row_dict(row, _SCREEN_FIELDS))
            return int(row["version"])
        cur = (await conn.execute(sa.select(screens).where(screens.c.id == sid))).mappings().one()
        row = (
            (
                await conn.execute(
                    sa.update(screens)
                    .where(screens.c.id == sid)
                    .values(
                        version=screens.c.version + 1,
                        updated_by=actor,
                        updated_at=sa.func.now(),
                        **{f: old.get(f) for f in fields if f not in ("code", "kind")},
                    )
                    .returning(screens)
                )
            )
            .mappings()
            .one()
        )
        audit.add("screen", sid, _row_dict(cur, _SCREEN_FIELDS), _row_dict(row, _SCREEN_FIELDS))
        return int(row["version"])

    async def _undo_button(
        self,
        conn: AsyncConnection,
        audit: _Audit,
        bid: int,
        old: Mapping[str, Any] | None,
        new: Mapping[str, Any] | None,
    ) -> None:
        cur = (
            (await conn.execute(sa.select(screen_buttons).where(screen_buttons.c.id == bid)))
            .mappings()
            .first()
        )
        if old is None:  # created by the batch → delete
            if cur is not None:
                await conn.execute(sa.delete(screen_buttons).where(screen_buttons.c.id == bid))
                audit.add("button", bid, _row_dict(cur, _BUTTON_FIELDS), None)
            return
        values = {f: old.get(f) for f in _BUTTON_FIELDS}
        if new is None or cur is None:  # deleted by the batch → re-insert with the same id
            row = (
                (
                    await conn.execute(
                        sa.insert(screen_buttons)
                        .values(id=bid, **_db_values(values))
                        .returning(screen_buttons)
                    )
                )
                .mappings()
                .one()
            )
            audit.add("button", bid, None, _row_dict(row, _BUTTON_FIELDS))
            return
        values.pop("screen_id")
        values.pop("system_key")
        row = (
            (
                await conn.execute(
                    sa.update(screen_buttons)
                    .where(screen_buttons.c.id == bid)
                    .values(**_db_values(values))
                    .returning(screen_buttons)
                )
            )
            .mappings()
            .one()
        )
        audit.add("button", bid, _row_dict(cur, _BUTTON_FIELDS), _row_dict(row, _BUTTON_FIELDS))

    async def last_undoable(self, screen_id: int) -> HistoryItem | None:
        """The newest batch of the screen that can still be undone (for «↩️ Отменить»)."""
        items = await self.history(screen_id, limit=1)
        now = clock.now()
        if items and items[0].undoable(now, self.undo_window) and not items[0].is_undo:
            return items[0]
        return None


async def _table_exists(conn: AsyncConnection, name: str) -> bool:
    """The table is migrated (a tree or a database without that module has nothing linking here)."""
    return (await conn.execute(sa.select(sa.func.to_regclass(name)))).scalar_one_or_none() is not None


def _valid_code(code: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_]{0,31}", code) is not None


def screen_action_target(action: Any) -> str | None:
    """Target of a ``screen:`` action (for «➡️ Перейти» in the button editor)."""
    try:
        parsed = parse_action(action)
    except ContentError:
        return None
    return parsed.target if isinstance(parsed, ScreenAction) else None
