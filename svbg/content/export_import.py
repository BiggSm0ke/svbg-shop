"""Content transfer: ``content.zip`` export and whole import with backup and undo (07 §3.7).

Archive format (``schema_version`` 1, a public surface — D16)::

    content.zip
    ├── content.json   {"format": "svbg-content", "schema_version": 1, "exported_at", "app_version",
    │                   "media": [{"sha256", "kind", "mime", "size", "width", "height", "duration", "file"}],
    │                   "sections": {"screens": {...}, "plans": {...}, <registered sections>…}}
    └── media/<sha256>.<ext>

* **No secrets, no bot-bound data.** ``file_id`` (bound to one bot), editor ids and timestamps are not
  exported; the document is checked for secret-like keys before it is written. Media are referenced by
  ``sha256``, so they survive a move to another install and bot (Telegram ``file_id`` are learned again).
* **Sections** are pluggable (:func:`register_section`): this module ships ``screens`` (screens, buttons,
  their media) and ``plans`` (plans, prices, location titles); promo codes, pages, deep links and LTE packs
  register their own section. Export takes the chosen sections; import applies every section of the archive.
* **Import is whole and atomic** (MVP): the archive is checked completely first (structure, limits, every
  screen/button/condition/plan, media hashes and types) — one problem and nothing changes, with a short
  Russian list of what is wrong. Then media files are written (content-addressed, cleaned like uploads), the
  current content is exported to a backup in ``content-exports/``, and every section is applied in **one
  transaction** together
  with a ``content_audit`` record (``entity='import'``). Stores are reloaded afterwards (``on_applied``).
  :meth:`ContentTransfer.undo` imports that backup back (and takes its own backup, so undo can be undone).
* **Matching.** System screens are matched by ``code`` (their kind stays ``system``), custom screens by
  ``code`` or, without one, by id when that id holds a code-less custom screen (so re-importing an export of
  the same install keeps ids); others are created. Custom screens absent from the archive are deleted;
  system screens are never deleted, and missing system screens/buttons are re-seeded. Buttons that point at
  a screen id absent from the archive are imported switched off (with a warning), never re-pointed at an
  unrelated screen. Plans are matched by ``code``; plans absent from the archive are switched off, not
  deleted (subscriptions refer to them).
* **Limits:** archive size, entry count, total and per-file uncompressed size (zip bombs), JSON size, item
  counts. Archive member names are never used as paths: files are written under their verified hash.
* **Imported media are treated like uploads:** photos, GIFs and videos (public via ``/m/``) go through
  :func:`svbg.content.media.prepare_media` — pixel limits, EXIF/XMP/GPS stripped, video ``udta``/``meta``
  blanked. A cleaned file is stored under its new hash; screens still find it by the archive hash.
* **Export** reads the database in the event loop; building the JSON, the secrets check, file checks and
  writing the zip run in a thread. Auto-named exports (``content-*.zip``) are pruned to the newest
  ``keep_exports``: the caller sends the file right away; backups before imports are pruned separately.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from svbg import __version__
from svbg.catalog.tables import (
    AVAILABILITY,
    DEVICES_ON_RENEW,
    RESET_STRATEGIES,
    TRAFFIC_ON_RENEW,
    locations,
    plan_prices,
    plans,
)
from svbg.content import defaults
from svbg.content.media import (
    EXT_BY_MIME,
    PUBLIC_KINDS,
    MediaError,
    MediaLimits,
    media_rel_path,
    prepare_media,
    resolve_media_path,
    sniff,
    upsert_media_row,
    write_media_file,
)
from svbg.content.model import (
    BUTTON_STYLES,
    MEDIA_KINDS,
    MEDIA_MODES,
    Button,
    ContentError,
    ScreenAction,
    parse_action,
    parse_label,
    parse_text_blocks,
    parse_title,
)
from svbg.content.store import seed_system_screens
from svbg.content.tables import content_audit, media, screen_buttons, screens
from svbg.core import clock
from svbg.tg.ui.conditions import ConditionError, validate_condition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = [
    "ARCHIVE_FORMAT",
    "SCHEMA_VERSION",
    "ArchiveLimits",
    "ContentArchiveError",
    "ContentTransfer",
    "ExportCtx",
    "ExportResult",
    "ImportCtx",
    "ImportResult",
    "Section",
    "ValidateCtx",
    "check_no_secrets",
    "read_archive",
    "register_section",
    "sections",
]

log = logging.getLogger("svbg.content.transfer")

ARCHIVE_FORMAT: Final = "svbg-content"
SCHEMA_VERSION: Final = 1
JSON_NAME: Final = "content.json"
MEDIA_PREFIX: Final = "media/"
AUDIT_ENTITY: Final = "import"
BACKUP_PREFIX: Final = "backup-"
_BACKUP_RE: Final = re.compile(r"^backup-\d{8}-\d{6}-[0-9a-f]{6}\.zip$")
EXPORT_PREFIX: Final = "content-"
_EXPORT_RE: Final = re.compile(r"^content-\d{8}-\d{6}(-[0-9a-f]{6})?\.zip$")
_MEDIA_NAME_RE: Final = re.compile(r"^media/([0-9a-f]{64})\.([a-z0-9]{1,5})$")
_SHA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SYSTEM_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_PLAN_CODE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_PANEL_TAG_RE: Final = re.compile(r"^[A-Z0-9_]{1,16}$")
_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3,5}$")
_SECRET_KEY_RE: Final = re.compile(r"(secret|password|passwd|token|api_?key|private_?key|file_ids?$)", re.I)
_ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_CHUNK: Final = 256 * 1024
_MAX_PROBLEMS: Final = 15
_ADVISORY_KEY: Final = 0x5356_4247_4349  # "SVBGCI": one content import at a time across processes
MAX_ROW_WIDTH: Final = 8


class ContentArchiveError(ValueError):
    """The archive cannot be imported; ``problems`` lists what is wrong (Russian, short)."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        self.message = message
        self.problems = tuple(problems)
        super().__init__(message if not problems else f"{message}: {'; '.join(problems[:3])}")

    def text(self) -> str:
        """Message for the admin: the summary plus up to :data:`_MAX_PROBLEMS` lines."""
        lines = [self.message]
        lines += [f"• {p}" for p in self.problems[:_MAX_PROBLEMS]]
        if len(self.problems) > _MAX_PROBLEMS:
            lines.append(f"… и ещё {len(self.problems) - _MAX_PROBLEMS}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_archive_bytes: int = 512 * 1024 * 1024
    max_json_bytes: int = 20 * 1024 * 1024
    max_entries: int = 5000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_ratio: int = 200  # uncompressed / compressed for entries above 1 MB
    max_screens: int = 2000
    max_buttons_per_screen: int = 100
    max_plans: int = 500
    media: MediaLimits = field(default_factory=MediaLimits)


# ---------------------------------------------------------------- contexts and sections


class ExportCtx:
    """Given to section exporters: media rows by id and the set of media the export references."""

    def __init__(self, media_rows: Mapping[int, Mapping[str, Any]]) -> None:
        self._media = media_rows
        self.used: dict[str, Mapping[str, Any]] = {}

    def media_ref(self, media_id: int | None) -> str | None:
        """``sha256`` of a media row (recorded for the archive), ``None`` if absent."""
        if media_id is None:
            return None
        row = self._media.get(int(media_id))
        if row is None:
            return None
        sha = str(row["sha256"])
        self.used[sha] = row
        return sha


class ValidateCtx:
    """Collects problems (refuse the import) and warnings (import, but tell the admin)."""

    def __init__(self, media_shas: Iterable[str], limits: ArchiveLimits) -> None:
        self.media_shas = frozenset(media_shas)
        self.limits = limits
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def problem(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class ImportCtx:
    """Given to section appliers: new media ids by ``sha256``, the actor and a warnings list."""

    def __init__(self, media_ids: Mapping[str, int], actor: int | None, warnings: list[str]) -> None:
        self.media_ids = media_ids
        self.actor = actor
        self.warnings = warnings

    def media_id(self, sha256: str | None) -> int | None:
        return None if sha256 is None else self.media_ids.get(sha256)


ExportFn = Callable[["AsyncConnection", ExportCtx], Awaitable[Any]]
ValidateFn = Callable[[Any, ValidateCtx], Any]
ApplyFn = Callable[["AsyncConnection", Any, ImportCtx], Awaitable[Mapping[str, int]]]


@dataclass(frozen=True, slots=True)
class Section:
    """One part of ``content.json``. ``validate`` is pure and returns the normalized data for ``apply``."""

    name: str
    title: str
    export: ExportFn
    validate: ValidateFn
    apply: ApplyFn
    order: int = 100  # apply order (screens before sections that point at screens)


_SECTIONS: dict[str, Section] = {}


def register_section(section: Section, *, replace: bool = False) -> None:
    """Add a section (promo, pages, deep links, LTE packs…). Names are ``[a-z_]`` and unique."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", section.name):
        raise ValueError(f"bad section name {section.name!r}")
    if section.name in _SECTIONS and not replace:
        raise ValueError(f"section {section.name!r} is already registered")
    _SECTIONS[section.name] = section


def sections() -> Mapping[str, Section]:
    """Registered sections in apply order."""
    return MappingProxyType(dict(sorted(_SECTIONS.items(), key=lambda kv: (kv[1].order, kv[0]))))


# ---------------------------------------------------------------- helpers


def check_no_secrets(value: Any, path: str = "$") -> None:
    """Raise ``ValueError`` if a mapping key looks like a secret or a bot-bound id (defence for sections)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise ValueError(f"export refused: secret-like key {path}.{key}")
            check_no_secrets(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            check_no_secrets(item, f"{path}[{i}]")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _json_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# ---------------------------------------------------------------- section: screens


async def _export_screens(conn: AsyncConnection, ctx: ExportCtx) -> dict[str, Any]:
    srows = (await conn.execute(sa.select(screens).order_by(screens.c.id))).mappings().all()
    brows = (
        (
            await conn.execute(
                sa.select(screen_buttons).order_by(
                    screen_buttons.c.screen_id,
                    screen_buttons.c.row,
                    screen_buttons.c.sort,
                    screen_buttons.c.id,
                )
            )
        )
        .mappings()
        .all()
    )
    by_screen: dict[int, list[dict[str, Any]]] = {}
    for b in brows:
        by_screen.setdefault(int(b["screen_id"]), []).append(
            {
                "system_key": b["system_key"],
                "row": b["row"],
                "sort": b["sort"],
                "label": b["label"],
                "icon_custom_emoji_id": b["icon_custom_emoji_id"],
                "style": b["style"],
                "action": b["action"],
                "visible_if": b["visible_if"],
                "enabled": b["enabled"],
            }
        )
    out = [
        {
            "id": int(s["id"]),
            "code": s["code"],
            "kind": s["kind"],
            "title": s["title"],
            "body": s["body"],
            "media": ctx.media_ref(s["media_id"]),
            "media_mode": s["media_mode"],
            "enabled": s["enabled"],
            "buttons": by_screen.get(int(s["id"]), []),
        }
        for s in srows
    ]
    return {"screens": out}


def _check_button(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalized button or :class:`ContentError` / :class:`ConditionError`."""
    label = parse_label(raw.get("label"), "label")
    action = parse_action(raw.get("action"), "action")
    style = raw.get("style")
    if style is not None and style not in BUTTON_STYLES:
        raise ContentError("style", "неизвестный цвет")
    row, sort = raw.get("row", 0), raw.get("sort", 0)
    if not _is_int(row) or not 0 <= row < 100 or not _is_int(sort) or abs(sort) > 1_000_000:
        raise ContentError("row", "ряд 0–99 и порядок — целые числа")
    system_key = raw.get("system_key")
    if system_key is not None and (not isinstance(system_key, str) or not _SYSTEM_KEY_RE.match(system_key)):
        raise ContentError("system_key", "некорректный ключ")
    visible_if = raw.get("visible_if")
    if visible_if is not None and not isinstance(visible_if, Mapping):
        raise ContentError("visible_if", "условие должно быть объектом")
    validate_condition(visible_if)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ContentError("enabled", "ожидалось true/false")
    icon = raw.get("icon_custom_emoji_id")
    Button(label=label, action=action, icon_custom_emoji_id=icon, style=style, row=row, sort=sort)
    return {
        "system_key": system_key,
        "row": row,
        "sort": sort,
        "label": dict(label),
        "icon_custom_emoji_id": icon,
        "style": style,
        "action": action.to_json(),
        "visible_if": dict(visible_if) if visible_if is not None else None,
        "enabled": enabled,
    }


def _validate_button(raw: Any, where: str, ctx: ValidateCtx) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        ctx.problem(where, "ожидался объект кнопки")
        return None
    try:
        return _check_button(raw)
    except (ContentError, ConditionError) as e:
        ctx.problem(where, f"{e.path}: {e.message}")
        return None


def _validate_screen(raw: Any, where: str, ctx: ValidateCtx) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        ctx.problem(where, "ожидался объект экрана")
        return None
    sid, code, kind = raw.get("id"), raw.get("code"), raw.get("kind", "custom")
    ok = True
    if not _is_int(sid) or sid <= 0:
        ctx.problem(where, "нет числового id")
        ok = False
    if code is not None and (not isinstance(code, str) or not _CODE_RE.match(code)):
        ctx.problem(where, "некорректный код экрана")
        ok = False
    elif code in defaults.RESERVED_CODES:
        ctx.problem(where, f"код «{code}» зарезервирован")
        ok = False
    if kind not in ("system", "custom") or (kind == "system" and code is None):
        ctx.problem(where, "тип экрана: system (с кодом) или custom")
        ok = False
    try:
        title = parse_title(raw.get("title"), "title")
        parse_text_blocks(raw.get("body"), "body")
    except ContentError as e:
        ctx.problem(where, f"{e.path}: {e.message}")
        ok = False
        title = MappingProxyType({})
    media_sha = raw.get("media")
    if media_sha is not None and (not isinstance(media_sha, str) or not _SHA_RE.match(media_sha)):
        ctx.problem(where, "некорректная ссылка на медиа")
        ok = False
    elif media_sha is not None and media_sha not in ctx.media_shas:
        ctx.warn(f"{where}: медиафайла нет в архиве — экран будет без медиа")
        media_sha = None
    mode = raw.get("media_mode", "attach")
    if mode not in MEDIA_MODES:
        ctx.problem(where, "режим медиа: attach или preview")
        ok = False
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        ctx.problem(where, "enabled: ожидалось true/false")
        ok = False
    raw_buttons = raw.get("buttons", [])
    if not isinstance(raw_buttons, list) or len(raw_buttons) > ctx.limits.max_buttons_per_screen:
        ctx.problem(where, f"кнопки: список до {ctx.limits.max_buttons_per_screen}")
        return None
    buttons: list[dict[str, Any]] = []
    for i, b in enumerate(raw_buttons):
        btn = _validate_button(b, f"{where}, кнопка {i + 1}", ctx)
        if btn is None:
            ok = False
        else:
            buttons.append(btn)
    keys = [b["system_key"] for b in buttons if b["system_key"] is not None]
    if len(keys) != len(set(keys)):
        ctx.problem(where, "системная кнопка повторяется")
        ok = False
    widths: dict[int, int] = {}
    for b in buttons:
        if b["enabled"]:
            widths[b["row"]] = widths.get(b["row"], 0) + 1
    if any(w > MAX_ROW_WIDTH for w in widths.values()):
        ctx.problem(where, f"в ряду больше {MAX_ROW_WIDTH} кнопок")
        ok = False
    if not ok:
        return None
    return {
        "id": sid,
        "code": code,
        "kind": kind,
        "title": dict(title),
        "body": _json_object(raw.get("body")),
        "media": media_sha,
        "media_mode": mode,
        "enabled": enabled,
        "buttons": buttons,
    }


def _validate_screens(data: Any, ctx: ValidateCtx) -> list[dict[str, Any]]:
    items = data.get("screens") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        ctx.problem("экраны", "ожидался список screens")
        return []
    if len(items) > ctx.limits.max_screens:
        ctx.problem("экраны", f"слишком много экранов (максимум {ctx.limits.max_screens})")
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_codes: set[str] = set()
    for i, raw in enumerate(items):
        label = raw.get("code") or raw.get("id") if isinstance(raw, Mapping) else None
        screen = _validate_screen(raw, f"экран {label or i + 1}", ctx)
        if screen is None:
            continue
        if screen["id"] in seen_ids:
            ctx.problem(f"экран {label}", "id повторяется")
        if screen["code"] is not None and screen["code"] in seen_codes:
            ctx.problem(f"экран {label}", "код повторяется")
        seen_ids.add(screen["id"])
        if screen["code"] is not None:
            seen_codes.add(screen["code"])
        out.append(screen)
    for s in out:
        for b in s["buttons"]:
            target = b["action"].get("target") if b["action"].get("type") == "screen" else None
            if isinstance(target, str) and target.isdigit() and int(target) not in seen_ids:
                ctx.warn(
                    f"экран {s['code'] or s['id']}: кнопка ведёт на экран {target}, которого нет — выключена"
                )
    return out


async def _reset_sequence(conn: AsyncConnection, table: str) -> None:
    await conn.execute(
        sa.text(
            f"select setval(pg_get_serial_sequence('{table}', 'id'), "  # fixed table names only
            f"greatest((select coalesce(max(id), 0) from {table}), 1))"
        )
    )


async def _apply_screens(conn: AsyncConnection, data: list[dict[str, Any]], ctx: ImportCtx) -> dict[str, int]:
    existing = (
        (await conn.execute(sa.select(screens.c.id, screens.c.code, screens.c.kind).with_for_update()))
        .mappings()
        .all()
    )
    by_code = {r["code"]: r for r in existing if r["code"] is not None}
    by_id = {int(r["id"]): r for r in existing}

    # 1. decide the target row of every archive screen
    updates: dict[int, dict[str, Any]] = {}  # existing id -> archive screen
    explicit: list[dict[str, Any]] = []  # insert keeping the archive id
    auto: list[dict[str, Any]] = []  # insert with a new id
    for s in data:
        if s["code"] is not None:
            row = by_code.get(s["code"])
            if row is not None:
                updates[int(row["id"])] = s
            else:
                auto.append(s)
            continue
        row = by_id.get(s["id"])
        if row is not None and row["code"] is None and row["kind"] == "custom":
            updates[s["id"]] = s
        elif row is None:
            explicit.append(s)
        else:
            auto.append(s)

    # 2. custom screens that the archive does not contain are removed (buttons cascade)
    doomed = [int(r["id"]) for r in existing if r["kind"] == "custom" and int(r["id"]) not in updates]
    if doomed:
        await conn.execute(sa.delete(screens).where(screens.c.id.in_(doomed)))

    now = clock.now()
    new_id: dict[int, int] = {}  # archive id -> id in this database

    def values(s: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": s["title"],
            "body": s["body"],
            "media_id": ctx.media_id(s["media"]),
            "media_mode": s["media_mode"],
            "enabled": s["enabled"],
            "updated_by": ctx.actor,
            "updated_at": now,
        }

    for sid, s in updates.items():
        await conn.execute(
            sa.update(screens).where(screens.c.id == sid).values(version=screens.c.version + 1, **values(s))
        )
        new_id[s["id"]] = sid
    for s in explicit:
        await conn.execute(
            sa.insert(screens).values(id=s["id"], code=s["code"], kind=s["kind"], version=1, **values(s))
        )
        new_id[s["id"]] = s["id"]
    if explicit:
        await _reset_sequence(conn, "screens")
    for s in auto:
        sid = (
            await conn.execute(
                sa.insert(screens)
                .values(code=s["code"], kind=s["kind"], version=1, **values(s))
                .returning(screens.c.id)
            )
        ).scalar_one()
        new_id[s["id"]] = int(sid)

    # 3. buttons: replaced as a whole on every imported screen
    targets = list(new_id.values())
    if targets:
        await conn.execute(sa.delete(screen_buttons).where(screen_buttons.c.screen_id.in_(targets)))
    button_rows: list[dict[str, Any]] = []
    disabled = 0
    for s in data:
        for b in s["buttons"]:
            action, enabled = dict(b["action"]), b["enabled"]
            if action.get("type") == "screen" and str(action.get("target", "")).isdigit():
                mapped = new_id.get(int(action["target"]))
                if mapped is None:
                    enabled = False
                    disabled += 1
                else:
                    action = ScreenAction(str(mapped)).to_json()
            button_rows.append(
                {
                    "screen_id": new_id[s["id"]],
                    "system_key": b["system_key"],
                    "row": b["row"],
                    "sort": b["sort"],
                    "label": b["label"],
                    "icon_custom_emoji_id": b["icon_custom_emoji_id"],
                    "style": b["style"],
                    "action": action,
                    "visible_if": b["visible_if"] if b["visible_if"] is not None else sa.null(),
                    "enabled": enabled,
                }
            )
    for start in range(0, len(button_rows), 500):
        await conn.execute(sa.insert(screen_buttons).values(button_rows[start : start + 500]))
    missing_media = sum(1 for s in data if s["media"] is not None and ctx.media_id(s["media"]) is None)
    if missing_media:
        ctx.warnings.append(f"медиа не найдено для {missing_media} экр.")
    reseeded = await seed_system_screens(conn)
    return {
        "screens": len(data),
        "created": len(explicit) + len(auto),
        "updated": len(updates),
        "deleted": len(doomed),
        "buttons": len(button_rows),
        "buttons_off": disabled,
        "reseeded": reseeded,
    }


# ---------------------------------------------------------------- section: plans

_PLAN_FIELDS: Final = (
    "code",
    "name",
    "availability",
    "is_trial",
    "enabled",
    "traffic_bytes",
    "reset_strategy",
    "device_limit",
    "squads",
    "ext_squad",
    "panel_tag",
    "traffic_on_renew",
    "devices_on_renew",
    "device_addon",
    "sort",
)


async def _export_plans(conn: AsyncConnection, _ctx: ExportCtx) -> dict[str, Any]:
    prows = (await conn.execute(sa.select(plans).order_by(plans.c.sort, plans.c.id))).mappings().all()
    price_rows = (
        (
            await conn.execute(
                sa.select(plan_prices).order_by(
                    plan_prices.c.plan_id, plan_prices.c.currency, plan_prices.c.days
                )
            )
        )
        .mappings()
        .all()
    )
    prices: dict[int, list[dict[str, Any]]] = {}
    for p in price_rows:
        prices.setdefault(int(p["plan_id"]), []).append(
            {
                "days": p["days"],
                "currency": p["currency"],
                "amount_minor": p["amount_minor"],
                "highlight": p["highlight"],
            }
        )
    lrows = (await conn.execute(sa.select(locations).order_by(locations.c.sort))).mappings().all()
    return {
        "plans": [{**{f: p[f] for f in _PLAN_FIELDS}, "prices": prices.get(int(p["id"]), [])} for p in prows],
        "locations": [
            {
                "squad_uuid": r["squad_uuid"],
                "title": r["title"],
                "flag": r["flag"],
                "sort": r["sort"],
                "panel_name": r["panel_name"],
            }
            for r in lrows
        ],
    }


def _plan_problems(p: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if not isinstance(p.get("name"), Mapping):
        out.append("name: ожидался объект {язык: название}")
    if p.get("availability") not in AVAILABILITY:
        out.append("availability: неизвестное значение")
    for key in ("is_trial", "enabled"):
        if not isinstance(p.get(key), bool):
            out.append(f"{key}: ожидалось true/false")
    if not _is_int(p.get("traffic_bytes")) or p["traffic_bytes"] < 0:
        out.append("traffic_bytes: целое ≥ 0")
    if p.get("reset_strategy") not in RESET_STRATEGIES:
        out.append("reset_strategy: неизвестное значение")
    if p.get("device_limit") is not None and (not _is_int(p["device_limit"]) or p["device_limit"] < 0):
        out.append("device_limit: пусто или целое ≥ 0")
    squads = p.get("squads")
    if not isinstance(squads, list) or not all(isinstance(s, str) and 0 < len(s) <= 64 for s in squads):
        out.append("squads: список идентификаторов")
    elif p.get("enabled") is True and not squads:
        out.append("включённому тарифу нужен хотя бы один сквад")
    for key in ("ext_squad",):
        if p.get(key) is not None and (not isinstance(p[key], str) or len(p[key]) > 64):
            out.append(f"{key}: строка до 64 символов")
    if p.get("panel_tag") is not None and (
        not isinstance(p["panel_tag"], str) or not _PANEL_TAG_RE.match(p["panel_tag"])
    ):
        out.append("panel_tag: A-Z, 0-9, _ до 16 символов")
    if p.get("traffic_on_renew") not in TRAFFIC_ON_RENEW:
        out.append("traffic_on_renew: неизвестное значение")
    if p.get("devices_on_renew") not in DEVICES_ON_RENEW:
        out.append("devices_on_renew: неизвестное значение")
    if not isinstance(p.get("device_addon"), Mapping):
        out.append("device_addon: ожидался объект")
    if not _is_int(p.get("sort", 0)):
        out.append("sort: целое число")
    prices = p.get("prices", [])
    if not isinstance(prices, list):
        return [*out, "prices: ожидался список"]
    seen: set[tuple[int, str]] = set()
    for i, pr in enumerate(prices):
        if not isinstance(pr, Mapping):
            out.append(f"цена {i + 1}: ожидался объект")
            continue
        days, cur, amount = pr.get("days"), pr.get("currency"), pr.get("amount_minor")
        if not _is_int(days) or not 1 <= days <= 3650:
            out.append(f"цена {i + 1}: дней 1–3650")
        if not isinstance(cur, str) or not _CURRENCY_RE.match(cur):
            out.append(f"цена {i + 1}: валюта")
        if not _is_int(amount) or amount <= 0:
            out.append(f"цена {i + 1}: сумма > 0")
        if not isinstance(pr.get("highlight", False), bool):
            out.append(f"цена {i + 1}: highlight true/false")
        if _is_int(days) and isinstance(cur, str):
            if (days, cur) in seen:
                out.append(f"цена {i + 1}: повтор периода")
            seen.add((days, cur))
    return out


def _validate_plans(data: Any, ctx: ValidateCtx) -> dict[str, Any]:
    if not isinstance(data, Mapping) or not isinstance(data.get("plans", []), list):
        ctx.problem("тарифы", "ожидался список plans")
        return {"plans": [], "locations": []}
    items = data.get("plans", [])
    if len(items) > ctx.limits.max_plans:
        ctx.problem("тарифы", f"слишком много тарифов (максимум {ctx.limits.max_plans})")
        return {"plans": [], "locations": []}
    out: list[dict[str, Any]] = []
    codes: set[str] = set()
    trials = 0
    for i, p in enumerate(items):
        code = p.get("code") if isinstance(p, Mapping) else None
        where = f"тариф {code or i + 1}"
        if not isinstance(code, str) or not _PLAN_CODE_RE.match(code):
            ctx.problem(where, "некорректный код")
            continue
        if code in codes:
            ctx.problem(where, "код повторяется")
            continue
        codes.add(code)
        problems = _plan_problems(p)
        for msg in problems:
            ctx.problem(where, msg)
        if problems:
            continue
        trials += bool(p["is_trial"])
        plan = {f: p.get(f) for f in _PLAN_FIELDS}
        plan["sort"] = p.get("sort", 0)
        plan["name"], plan["device_addon"] = dict(p["name"]), dict(p["device_addon"])
        plan["prices"] = [
            {
                "days": pr["days"],
                "currency": pr["currency"],
                "amount_minor": pr["amount_minor"],
                "highlight": pr.get("highlight", False),
            }
            for pr in p.get("prices", [])
        ]
        out.append(plan)
    if trials > 1:
        ctx.problem("тарифы", "пробный тариф может быть только один")
    locs: list[dict[str, Any]] = []
    raw_locs = data.get("locations", [])
    if not isinstance(raw_locs, list):
        ctx.problem("локации", "ожидался список")
        raw_locs = []
    for i, loc in enumerate(raw_locs):
        uuid_ = loc.get("squad_uuid") if isinstance(loc, Mapping) else None
        if not isinstance(uuid_, str) or not 0 < len(uuid_) <= 64:
            ctx.problem(f"локация {i + 1}", "некорректный squad_uuid")
            continue
        flag = loc.get("flag")
        if not isinstance(loc.get("title", {}), Mapping) or (
            flag is not None and (not isinstance(flag, str) or not 0 < len(flag) <= 16)
        ):
            ctx.problem(f"локация {uuid_}", "название или флаг")
            continue
        locs.append(
            {
                "squad_uuid": uuid_,
                "title": dict(loc.get("title") or {}),
                "flag": flag,
                "sort": loc.get("sort", 0) if _is_int(loc.get("sort", 0)) else 0,
                "panel_name": str(loc.get("panel_name") or "")[:256],
            }
        )
    return {"plans": out, "locations": locs}


async def _apply_plans(conn: AsyncConnection, data: dict[str, Any], _ctx: ImportCtx) -> dict[str, int]:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    existing = {
        r.code: int(r.id)
        for r in (await conn.execute(sa.select(plans.c.id, plans.c.code).with_for_update())).all()
    }
    codes = {p["code"] for p in data["plans"]}
    now = clock.now()
    # The trial flag is unique: clear it everywhere first, the archive sets it back on its own trial plan.
    await conn.execute(sa.update(plans).where(plans.c.is_trial).values(is_trial=False))
    stale = [pid for code, pid in existing.items() if code not in codes]
    if stale:
        await conn.execute(
            sa.update(plans)
            .where(plans.c.id.in_(stale), plans.c.enabled)
            .values(enabled=False, version=plans.c.version + 1, updated_at=now)
        )
    created = updated = 0
    ids: list[int] = []
    price_rows: list[dict[str, Any]] = []
    for p in data["plans"]:
        fields = {f: p[f] for f in _PLAN_FIELDS if f != "code"}
        pid = existing.get(p["code"])
        if pid is None:
            pid = int(
                (
                    await conn.execute(
                        sa.insert(plans).values(code=p["code"], **fields).returning(plans.c.id)
                    )
                ).scalar_one()
            )
            created += 1
        else:
            await conn.execute(
                sa.update(plans)
                .where(plans.c.id == pid)
                .values(**fields, version=plans.c.version + 1, updated_at=now)
            )
            updated += 1
        ids.append(pid)
        price_rows += [{"plan_id": pid, **pr} for pr in p["prices"]]
    if ids:
        await conn.execute(sa.delete(plan_prices).where(plan_prices.c.plan_id.in_(ids)))
    if price_rows:
        await conn.execute(sa.insert(plan_prices), price_rows)
    for loc in data["locations"]:
        stmt = pg_insert(locations).values(**loc)
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[locations.c.squad_uuid],
                set_={"title": stmt.excluded.title, "flag": stmt.excluded.flag, "sort": stmt.excluded.sort},
            )
        )
    return {
        "plans": len(data["plans"]),
        "created": created,
        "updated": updated,
        "disabled": len(stale),
        "prices": len(price_rows),
        "locations": len(data["locations"]),
    }


register_section(
    Section("screens", "Экраны и кнопки", _export_screens, _validate_screens, _apply_screens, 10)
)
register_section(Section("plans", "Тарифы и цены", _export_plans, _validate_plans, _apply_plans, 20))


# ---------------------------------------------------------------- archive I/O (blocking; run in a thread)


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    sha256: str
    kind: str
    mime: str
    ext: str
    size: int
    width: int | None
    height: int | None
    duration: int | None
    member: zipfile.ZipInfo | None
    stored_sha: str = ""  # hash of the file actually stored when cleaning changed it; "" = ``sha256``

    @property
    def store_sha(self) -> str:
        return self.stored_sha or self.sha256


@dataclass(frozen=True, slots=True)
class ParsedArchive:
    doc: Mapping[str, Any]
    media: tuple[_ManifestItem, ...]
    sha256: str  # of the archive file
    warnings: tuple[str, ...]


def _opt_int(value: Any) -> int | None:
    return value if _is_int(value) and value > 0 else None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def read_archive(path: Path, limits: ArchiveLimits | None = None) -> ParsedArchive:
    """Open and check ``content.zip`` structure and limits; media bytes are not read here."""
    limits = limits or ArchiveLimits()
    try:
        size = path.stat().st_size
    except OSError:
        raise ContentArchiveError("Файл архива не найден.") from None
    if size > limits.max_archive_bytes:
        raise ContentArchiveError(f"Архив больше {limits.max_archive_bytes // (1024 * 1024)} МБ.")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ContentArchiveError("Это не zip-архив content.zip.") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise ContentArchiveError("В архиве слишком много файлов.")
        total = 0
        members: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.is_dir():
                continue
            total += info.file_size
            if info.file_size > 1024 * 1024 and info.file_size > limits.max_ratio * max(
                info.compress_size, 1
            ):
                raise ContentArchiveError("Архив подозрительно сильно сжат — импорт отклонён.")
            if info.flag_bits & 0x1:
                raise ContentArchiveError("Зашифрованные архивы не поддерживаются.")
            members[info.filename] = info
        if total > limits.max_total_bytes:
            raise ContentArchiveError("Распакованный архив слишком большой.")
        info = members.get(JSON_NAME)
        if info is None:
            raise ContentArchiveError("В архиве нет content.json.")
        if info.file_size > limits.max_json_bytes:
            raise ContentArchiveError("content.json слишком большой.")
        try:
            with zf.open(info) as fh:
                raw = fh.read(limits.max_json_bytes + 1)
            doc = json.loads(raw.decode("utf-8"))
        except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError):
            raise ContentArchiveError("content.json повреждён или не является JSON.") from None
    if not isinstance(doc, dict) or doc.get("format") != ARCHIVE_FORMAT:
        raise ContentArchiveError("Это не архив контента SvBG Shop.")
    version = doc.get("schema_version")
    if not _is_int(version) or version < 1:
        raise ContentArchiveError("В архиве нет версии формата.")
    if version > SCHEMA_VERSION:
        raise ContentArchiveError("Архив сделан более новой версией бота — сначала обновите бота.")
    if not isinstance(doc.get("sections"), dict) or not doc["sections"]:
        raise ContentArchiveError("В архиве нет разделов контента.")
    raw_media = doc.get("media", [])
    if not isinstance(raw_media, list):
        raise ContentArchiveError("Список медиа повреждён.")
    by_sha: dict[str, zipfile.ZipInfo] = {}
    for name, info in members.items():
        m = _MEDIA_NAME_RE.match(name)
        if m is not None:
            by_sha[m.group(1)] = info
    problems: list[str] = []
    warnings: list[str] = []
    items: list[_ManifestItem] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_media):
        sha = raw.get("sha256") if isinstance(raw, Mapping) else None
        kind = raw.get("kind") if isinstance(raw, Mapping) else None
        if not isinstance(sha, str) or not _SHA_RE.match(sha) or kind not in MEDIA_KINDS:
            problems.append(f"медиа {i + 1}: некорректная запись")
            continue
        if sha in seen:
            continue
        seen.add(sha)
        member = by_sha.get(sha)
        if member is None:
            warnings.append(f"медиа {sha[:12]}…: файла нет в архиве")
            continue
        if member.file_size > limits.media.max_bytes(kind):
            problems.append(f"медиа {sha[:12]}…: файл больше допустимого")
            continue
        items.append(
            _ManifestItem(
                sha256=sha,
                kind=kind,
                mime="",
                ext="",
                size=member.file_size,
                width=_opt_int(raw.get("width")),
                height=_opt_int(raw.get("height")),
                duration=_opt_int(raw.get("duration")),
                member=member,
            )
        )
    if problems:
        raise ContentArchiveError("Архив не подходит", problems)
    return ParsedArchive(doc, tuple(items), _file_sha256(path), tuple(warnings))


_ALLOWED_MIME: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "photo": frozenset({"image/jpeg", "image/png", "image/webp"}),
        "animation": frozenset({"image/gif", "video/mp4"}),
        "video": frozenset({"video/mp4", "video/quicktime"}),
    }
)


def _extract_media(
    path: Path, items: Sequence[_ManifestItem], root: Path, limits: MediaLimits | None = None
) -> list[_ManifestItem]:
    """Stream every media member to ``root`` under its verified hash; returns items with mime/ext set."""
    out: list[_ManifestItem] = []
    problems: list[str] = []
    media_limits = _import_media_limits(limits or MediaLimits())
    with zipfile.ZipFile(path) as zf:
        for item in items:
            assert item.member is not None
            try:
                done = _extract_one(zf, item, root, media_limits)
            except ContentArchiveError as e:
                problems.append(e.message)
                continue
            out.append(done)
    if problems:
        raise ContentArchiveError("Медиафайлы в архиве повреждены", problems)
    return out


def _import_media_limits(limits: MediaLimits) -> MediaLimits:
    """Upload limits, except that a clean JPEG is kept at any allowed size (no re-encoding on every undo)."""
    return dataclasses.replace(limits, photo_keep_bytes=max(limits.photo_keep_bytes, limits.photo_max_bytes))


def _clean_public(tmp: Path, item: _ManifestItem, root: Path, limits: MediaLimits) -> _ManifestItem | None:
    """Run a public media file through the upload checks; a changed file is stored under its new hash.

    Returns ``None`` when the bytes stay as they are (the caller moves ``tmp`` into place).
    """
    try:
        prepared = prepare_media(
            tmp.read_bytes(),
            item.kind,
            limits,
            width=item.width,
            height=item.height,
            duration=item.duration,
        )
    except MediaError as e:
        raise ContentArchiveError(f"медиа {item.sha256[:12]}…: {e.message}") from None
    if prepared.sha256 == item.sha256:
        if prepared.width is None:
            return None
        return dataclasses.replace(item, width=prepared.width, height=prepared.height)
    write_media_file(root, prepared.sha256, prepared.ext, prepared.data)
    return _ManifestItem(
        item.sha256,
        item.kind,
        prepared.mime,
        prepared.ext,
        prepared.size,
        prepared.width,
        prepared.height,
        prepared.duration,
        None,
        stored_sha=prepared.sha256,
    )


def _extract_one(zf: zipfile.ZipFile, item: _ManifestItem, root: Path, limits: MediaLimits) -> _ManifestItem:
    assert item.member is not None
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".import-", suffix=".tmp", dir=root)
    tmp = Path(tmp_name)
    h = hashlib.sha256()
    written = 0
    head = b""
    try:
        with os.fdopen(fd, "wb") as out, zf.open(item.member) as src:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if written > item.member.file_size:
                    raise ContentArchiveError(f"медиа {item.sha256[:12]}…: размер не совпал")
                if len(head) < 32:
                    head += chunk[:32]
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != item.sha256:
            raise ContentArchiveError(f"медиа {item.sha256[:12]}…: контрольная сумма не совпала")
        found = sniff(head)
        allowed = _ALLOWED_MIME.get(item.kind)
        if allowed is not None and found.mime not in allowed:
            raise ContentArchiveError(f"медиа {item.sha256[:12]}…: тип файла не подходит")
        if item.kind in PUBLIC_KINDS:
            cleaned = _clean_public(tmp, item, root, limits)
            if cleaned is not None and cleaned.stored_sha:
                return cleaned
            if cleaned is not None:
                item = cleaned
        rel = media_rel_path(item.sha256, found.ext)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)
    except (zipfile.BadZipFile, OSError, MediaError):
        raise ContentArchiveError(f"медиа {item.sha256[:12]}…: не удалось распаковать") from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return _ManifestItem(
        item.sha256, item.kind, found.mime, found.ext, written, item.width, item.height, item.duration, None
    )


def _zip_info(name: str, compress: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = compress
    info.external_attr = 0o644 << 16
    return info


def _write_archive(dest: Path, doc_bytes: bytes, files: Sequence[tuple[str, Path]]) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(6)}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            zf.writestr(_zip_info(JSON_NAME, zipfile.ZIP_DEFLATED), doc_bytes)
            for name, src in files:
                with open(src, "rb") as fh, zf.open(_zip_info(name, zipfile.ZIP_STORED), "w") as out:
                    while chunk := fh.read(_CHUNK):
                        out.write(chunk)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return dest.stat().st_size


def _build_archive(
    dest: Path,
    head: Mapping[str, Any],
    data: Mapping[str, Any],
    used: Mapping[str, Mapping[str, Any]],
    media_root: Path,
) -> tuple[int, int, list[str]]:
    """Manifest, secrets check, JSON and zip (blocking; runs in a thread). Returns size, media, warnings."""
    warnings: list[str] = []
    manifest: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    for sha, row in sorted(used.items()):
        src = resolve_media_path(media_root, row["path"])
        entry = {
            "sha256": sha,
            "kind": row["kind"],
            "mime": row["mime"],
            "size": row["size"],
            "width": row["width"],
            "height": row["height"],
            "duration": row["duration"],
        }
        if src is None or not src.is_file():
            warnings.append(f"медиа {row['id']}: файла нет на диске — в архив не попало")
            manifest.append(entry)
            continue
        ext = EXT_BY_MIME.get(row["mime"] or "", "bin")
        name = f"{MEDIA_PREFIX}{sha}.{ext}"
        manifest.append({**entry, "file": name})
        files.append((name, src))
    check_no_secrets(data)
    doc = {**head, "media": manifest, "sections": data}
    doc_bytes = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1, default=str).encode("utf-8")
    del doc
    return _write_archive(dest, doc_bytes, files), len(files), warnings


# ---------------------------------------------------------------- service


@dataclass(frozen=True, slots=True)
class ExportResult:
    path: Path
    size: int
    sections: tuple[str, ...]
    media: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportResult:
    batch_id: str
    backup: Path
    sections: tuple[str, ...]
    stats: Mapping[str, Mapping[str, int]]
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        """Short Russian summary for the admin."""
        titles = sections()
        parts = []
        for name in self.sections:
            st = self.stats.get(name, {})
            title = titles[name].title if name in titles else name
            main = next(iter(st.values()), 0) if st else 0
            parts.append(f"{title}: {main}")
        text = "✅ Контент загружен — " + ", ".join(parts)
        if self.warnings:
            text += f"\n⚠️ Предупреждений: {len(self.warnings)}"
        return text


ReloadHook = Callable[[], Awaitable[object]]


class ContentTransfer:
    """Export and whole import of content; one import at a time (process lock + advisory lock)."""

    def __init__(
        self,
        db: Database,
        media_root: Path,
        exports_dir: Path,
        *,
        on_applied: Sequence[ReloadHook] = (),
        keep_backups: int | Callable[[], int] = 10,
        keep_exports: int | Callable[[], int] = 3,
        limits: ArchiveLimits | None = None,
    ) -> None:
        self._db = db
        self._media_root = media_root
        self._exports = exports_dir
        self._hooks = list(on_applied)
        self._keep = keep_backups
        self._keep_exports = keep_exports
        self._limits = limits or ArchiveLimits()
        self._lock = asyncio.Lock()

    @property
    def exports_dir(self) -> Path:
        return self._exports

    def add_reload_hook(self, hook: ReloadHook) -> None:
        self._hooks.append(hook)

    # ------------------------------------------------------------ export

    async def export(self, dest: Path | None = None, *, only: Sequence[str] | None = None) -> ExportResult:
        """Write ``content.zip`` (all sections or ``only`` those) to ``dest`` (default: exports dir)."""
        registered = sections()
        names = list(registered) if only is None else [n for n in registered if n in set(only)]
        unknown = set(only or ()) - set(registered)
        if unknown or not names:
            raise ValueError(f"unknown or empty sections: {sorted(unknown) or only}")
        async with self._db.read() as conn:
            media_rows = {int(r["id"]): r for r in (await conn.execute(sa.select(media))).mappings().all()}
            ctx = ExportCtx(media_rows)
            data = {name: await registered[name].export(conn, ctx) for name in names}
        head = {
            "format": ARCHIVE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "exported_at": clock.now().isoformat(),
            "app_version": __version__,
        }
        auto = dest is None
        if dest is None:
            dest = self._exports / f"{EXPORT_PREFIX}{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.zip"
        size, media_count, warnings = await asyncio.to_thread(
            _build_archive, dest, head, data, ctx.used, self._media_root
        )
        if auto:
            await asyncio.to_thread(self._prune, _EXPORT_RE, self._keep_exports, dest)
        log.info("content exported: %s (%d bytes, %d media)", dest.name, size, media_count)
        return ExportResult(dest, size, tuple(names), media_count, tuple(warnings))

    # ------------------------------------------------------------ import

    def _new_backup_path(self) -> Path:
        return self._exports / f"{BACKUP_PREFIX}{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.zip"

    def _prune_backups(self, protect: Path | None = None) -> None:
        self._prune(_BACKUP_RE, self._keep, protect)

    def _prune(
        self, pattern: re.Pattern[str], keep_setting: int | Callable[[], int], protect: Path | None = None
    ) -> None:
        """Delete all but the newest ``keep`` files of ``pattern``, never ``protect`` (blocking I/O)."""
        keep = keep_setting if isinstance(keep_setting, int) else keep_setting()
        keep = max(int(keep), 1)
        found: list[tuple[int, str, Path]] = []
        try:
            for path in self._exports.iterdir():
                if pattern.match(path.name):
                    with contextlib.suppress(OSError):
                        found.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:
            return
        found.sort()  # by modification time: names written within one second differ only by a random tag
        for _mtime, _name, old in found[:-keep]:
            if protect is not None and old == protect:
                continue
            with contextlib.suppress(OSError):
                old.unlink()

    def _validate(self, parsed: ParsedArchive) -> tuple[dict[str, Any], list[str]]:
        registered = sections()
        raw_sections = parsed.doc["sections"]
        unknown = [n for n in raw_sections if n not in registered]
        ctx = ValidateCtx((m.sha256 for m in parsed.media), self._limits)
        ctx.warnings += parsed.warnings
        for name in unknown:
            ctx.warn(f"раздел «{name}» этой версией бота не поддерживается — пропущен")
        normalized = {
            name: section.validate(raw_sections[name], ctx)
            for name, section in registered.items()
            if name in raw_sections
        }
        if ctx.problems:
            raise ContentArchiveError("Архив не подходит — ничего не изменено", ctx.problems)
        if not normalized:
            raise ContentArchiveError("В архиве нет разделов, которые знает этот бот.")
        return normalized, ctx.warnings

    async def import_archive(self, source: Path, *, actor: int | None = None) -> ImportResult:
        """Check ``source`` fully, back up the current content, apply every section in one transaction."""
        async with self._lock:
            parsed = await asyncio.to_thread(read_archive, source, self._limits)
            normalized, warnings = await asyncio.to_thread(self._validate, parsed)
            # media first: a refused file stops the import before a backup is even taken
            extracted = await asyncio.to_thread(
                _extract_media, source, parsed.media, self._media_root, self._limits.media
            )
            backup = await self.export(self._new_backup_path())
            warnings += [f"резервная копия: {w}" for w in backup.warnings]
            batch_id = uuid.uuid4().hex
            registered = sections()
            try:
                stats = await self._apply(
                    parsed,
                    normalized=normalized,
                    extracted=extracted,
                    batch_id=batch_id,
                    backup=backup.path,
                    actor=actor,
                    warnings=warnings,
                )
            except DBAPIError as e:
                log.warning("content import rejected by the database: %s", type(e.orig).__name__)
                raise ContentArchiveError(
                    "База отклонила данные архива — ничего не изменено", [str(e.orig).splitlines()[0][:200]]
                ) from None
            log.info("content imported: batch %s, sections %s", batch_id, sorted(normalized))
            await self._run_hooks()
            await asyncio.to_thread(self._prune_backups, backup.path)
            return ImportResult(
                batch_id,
                backup.path,
                tuple(n for n in registered if n in normalized),
                MappingProxyType(stats),
                tuple(warnings),
            )

    async def _apply(
        self,
        parsed: ParsedArchive,
        *,
        normalized: Mapping[str, Any],
        extracted: Sequence[_ManifestItem],
        batch_id: str,
        backup: Path,
        actor: int | None,
        warnings: list[str],
    ) -> dict[str, Mapping[str, int]]:
        registered = sections()
        stats: dict[str, Mapping[str, int]] = {}
        async with self._db.tx() as conn:
            await conn.execute(sa.select(sa.func.pg_advisory_xact_lock(_ADVISORY_KEY)))
            media_ids: dict[str, int] = {}
            for item in extracted:
                mid, _created = await upsert_media_row(
                    conn,
                    kind=item.kind,
                    sha256=item.store_sha,
                    path=media_rel_path(item.store_sha, item.ext),
                    mime=item.mime,
                    size=item.size,
                    width=item.width,
                    height=item.height,
                    duration=item.duration,
                )
                media_ids[item.sha256] = mid
            ictx = ImportCtx(MappingProxyType(media_ids), actor, warnings)
            for name, section in registered.items():
                if name in normalized:
                    stats[name] = dict(await section.apply(conn, normalized[name], ictx))
            await conn.execute(
                sa.insert(content_audit).values(
                    batch_id=batch_id,
                    entity=AUDIT_ENTITY,
                    entity_id=backup.name,
                    old={"backup": backup.name},
                    new={
                        "archive_sha256": parsed.sha256,
                        "sections": sorted(normalized),
                        "stats": stats,
                        "media": len(media_ids),
                    },
                    actor=actor,
                )
            )
        return stats

    async def _run_hooks(self) -> None:
        for hook in self._hooks:
            try:
                await hook()
            except Exception:  # isolation: one store failing to reload must not hide the import
                log.exception("content import: reload hook failed")

    async def backup_of(self, batch_id: str) -> Path | None:
        """The backup taken before import ``batch_id`` (``None`` if unknown or already pruned)."""
        if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            return None
        async with self._db.read() as conn:
            name = await conn.scalar(
                sa.select(content_audit.c.entity_id)
                .where(content_audit.c.batch_id == batch_id, content_audit.c.entity == AUDIT_ENTITY)
                .limit(1)
            )
        if not isinstance(name, str) or not _BACKUP_RE.match(name):
            return None
        path = self._exports / name
        return path if path.is_file() else None

    async def undo(self, batch_id: str, *, actor: int | None = None) -> ImportResult:
        """Return the content to its state before import ``batch_id`` (that itself is undoable)."""
        path = await self.backup_of(batch_id)
        if path is None:
            raise ContentArchiveError("Резервная копия для отмены не найдена.")
        return await self.import_archive(path, actor=actor)
