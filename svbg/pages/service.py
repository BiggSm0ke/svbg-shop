"""Pages: FAQ, rules, offer, consent and custom pages (04 §5 ``pages``, 07 §2.5): text with entities and
versions.

All pages live in memory (:meth:`PageService.load`; every write of this service refreshes its page), so
showing a page costs **no SQL**. The four system pages are seeded switched off with a placeholder text; the
owner writes them in the bot (a message with any Telegram formatting — entities are kept as is).

Consent: the ``consent`` page, when switched on and published («запросить согласие»), must be accepted once
per published version; :meth:`PageService.needs_consent` costs 0 SQL when no consent is required, one read
otherwise.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from svbg.content.model import ContentError, TextBlock, parse_text_blocks, parse_title
from svbg.core.tables import admin_audit
from svbg.pages.tables import page_consents, page_versions, pages

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database

__all__ = ["DEFAULT_LANG", "SYSTEM_PAGES", "Page", "PageError", "PageService", "PageVersion"]

log = logging.getLogger("svbg.pages")

DEFAULT_LANG: Final = "ru"
CODE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
MAX_PAGES: Final = 50
#: ``(code, kind, title, placeholder text)`` of the seeded pages, in display order.
SYSTEM_PAGES: Final = (
    ("faq", "faq", "❓ Вопросы и ответы", "Здесь будут ответы на частые вопросы."),
    ("rules", "rules", "📜 Правила", "Здесь будут правила сервиса."),
    ("offer", "offer", "📄 Оферта", "Здесь будет текст публичной оферты."),
    ("consent", "consent", "✅ Согласие", "Пользуясь ботом, вы соглашаетесь с правилами сервиса и офертой."),
)
_ORDER: Final = {code: i for i, (code, *_rest) in enumerate(SYSTEM_PAGES)}


class PageError(ValueError):
    """A refused page change; ``str(error)`` is a short Russian message for the owner."""


@dataclass(frozen=True, slots=True)
class Page:
    id: int
    code: str
    kind: str
    title: Mapping[str, str]
    body: Mapping[str, TextBlock]
    raw_body: Mapping[str, Any]
    enabled: bool
    version: int
    consent_version: int | None
    updated_at: datetime | None = None

    @property
    def system(self) -> bool:
        return self.kind != "custom"

    def title_for(self, _lang: str | None = None) -> str:
        """The Russian title (else any); the argument, an old language, is ignored."""
        return self.title.get(DEFAULT_LANG) or next(iter(self.title.values()), self.code)

    def block_for(self, _lang: str | None = None) -> TextBlock | None:
        """The Russian text (else any); old ``en`` texts stay in the database unused."""
        return self.body.get(DEFAULT_LANG) or next(iter(self.body.values()), None)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Page:
        raw = dict(row["body"] or {})
        try:
            body = parse_text_blocks(raw)
        except ContentError as e:  # a damaged row never breaks the bot: the page shows as empty
            log.warning("page %s has a broken body: %s", row["code"], e)
            body = MappingProxyType({})
        try:
            title = parse_title(row["title"] or {})
        except ContentError:
            title = MappingProxyType({})
        return cls(
            id=int(row["id"]),
            code=str(row["code"]),
            kind=str(row["kind"]),
            title=title,
            body=body,
            raw_body=MappingProxyType(raw),
            enabled=bool(row["enabled"]),
            version=int(row["version"]),
            consent_version=row["consent_version"],
            updated_at=row.get("updated_at"),
        )


@dataclass(frozen=True, slots=True)
class PageVersion:
    version: int
    created_at: datetime
    actor: int | None
    preview: str


def _entities_json(entities: Sequence[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entities or ():
        if hasattr(e, "model_dump"):
            raw = e.model_dump(exclude_none=True, mode="json")
        elif isinstance(e, Mapping):
            raw = {k: v for k, v in e.items() if v is not None}
        else:
            continue
        out.append(raw)
    return out


class PageService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._pages: dict[str, Page] = {}

    # ------------------------------------------------------------------------------------------ reads

    async def load(self) -> int:
        """Seed the system pages (once) and load every page into memory."""
        async with self.db.tx() as conn:
            for code, kind, title, text in SYSTEM_PAGES:
                new = (
                    await conn.execute(
                        pg_insert(pages)
                        .values(
                            code=code,
                            kind=kind,
                            title={DEFAULT_LANG: title},
                            body={DEFAULT_LANG: {"text": text}},
                        )
                        .on_conflict_do_nothing(index_elements=[pages.c.code])
                        .returning(pages.c.id, pages.c.title, pages.c.body)
                    )
                ).first()
                if new is not None:
                    await conn.execute(
                        sa.insert(page_versions).values(
                            page_id=new.id, version=1, title=new.title, body=new.body
                        )
                    )
            rows = (await conn.execute(sa.select(pages))).mappings().all()
        self._pages = {str(r["code"]): Page.from_row(r) for r in rows}
        return len(self._pages)

    def get(self, code: str) -> Page | None:
        return self._pages.get(code) if isinstance(code, str) else None

    def all(self) -> list[Page]:
        return sorted(self._pages.values(), key=lambda p: (_ORDER.get(p.code, len(_ORDER)), p.code))

    def consent_page(self) -> Page | None:
        page = next((p for p in self._pages.values() if p.kind == "consent"), None)
        if page is None or not page.enabled or page.consent_version is None:
            return None
        return page

    async def needs_consent(self, user_id: int) -> Page | None:
        """The consent page the user still has to accept, or ``None`` (0 SQL when consent is off)."""
        page = self.consent_page()
        if page is None:
            return None
        async with self.db.read() as conn:
            accepted = await conn.scalar(
                sa.select(page_consents.c.version).where(
                    page_consents.c.user_id == user_id, page_consents.c.page_id == page.id
                )
            )
        return page if accepted is None or accepted < (page.consent_version or 0) else None

    async def accept(self, user_id: int, code: str, version: int) -> bool:
        """Record the user's consent to ``version`` (only the currently required one counts)."""
        page = self.consent_page()
        if page is None or page.code != code or version != page.consent_version:
            return False
        stmt = pg_insert(page_consents).values(user_id=user_id, page_id=page.id, version=version)
        stmt = stmt.on_conflict_do_update(
            index_elements=[page_consents.c.user_id, page_consents.c.page_id],
            set_={"version": version, "accepted_at": sa.func.now()},
        )
        async with self.db.tx() as conn:
            await conn.execute(stmt)
        return True

    async def versions(self, code: str, limit: int = 10) -> list[PageVersion]:
        page = self._require(code)
        async with self.db.read() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(page_versions)
                        .where(page_versions.c.page_id == page.id)
                        .order_by(page_versions.c.version.desc())
                        .limit(max(1, limit))
                    )
                )
                .mappings()
                .all()
            )
        out = []
        for r in rows:
            body = r["body"] or {}
            block = (
                body.get(DEFAULT_LANG) or next(iter(body.values()), {}) if isinstance(body, Mapping) else {}
            )
            text = block.get("text", "") if isinstance(block, Mapping) else str(block)
            out.append(
                PageVersion(int(r["version"]), r["created_at"], r["actor"], " ".join(str(text).split())[:60])
            )
        return out

    # ----------------------------------------------------------------------------------------- writes

    def _require(self, code: str) -> Page:
        page = self.get(code)
        if page is None:
            raise PageError("Страница не найдена")
        return page

    async def _audit(
        self,
        conn: AsyncConnection,
        actor: tuple[int | None, str | None],
        action: str,
        page: str,
        details: Any,
    ) -> None:
        await conn.execute(
            sa.insert(admin_audit).values(
                actor_id=actor[0], role=actor[1], action=action, target=f"page:{page}", details=details
            )
        )

    async def _write(
        self,
        page: Page,
        actor: tuple[int | None, str | None],
        action: str,
        *,
        expected_version: int | None = None,
        new_version: bool = True,
        **values: Any,
    ) -> Page:
        """Update one page (compare-and-set on ``version``), store the version, audit; refresh memory."""
        cond = [pages.c.id == page.id]
        if expected_version is not None:
            cond.append(pages.c.version == expected_version)
        extra: dict[str, Any] = {"version": pages.c.version + 1} if new_version else {}
        async with self.db.tx() as conn:
            row = (
                (
                    await conn.execute(
                        sa.update(pages)
                        .where(*cond)
                        .values(**values, **extra, updated_by=actor[0], updated_at=sa.func.now())
                        .returning(pages)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PageError("Страницу уже изменили — откройте её заново")
            if new_version:
                await conn.execute(
                    sa.insert(page_versions).values(
                        page_id=page.id,
                        version=row["version"],
                        title=row["title"],
                        body=row["body"],
                        actor=actor[0],
                    )
                )
            await self._audit(conn, actor, action, page.code, {"version": row["version"]})
        updated = Page.from_row(row)
        self._pages[updated.code] = updated
        return updated

    async def save_text(
        self,
        code: str,
        lang: str,
        text: str,
        entities: Sequence[Any] | None,
        actor: tuple[int | None, str | None],
        *,
        expected_version: int | None = None,
    ) -> Page:
        """Replace the text of one language (entities kept as Telegram sent them)."""
        page = self._require(code)
        body = {**page.raw_body, lang: {"text": text, "entities": _entities_json(entities)}}
        try:
            parse_text_blocks(body)
        except ContentError as e:
            raise PageError(f"Текст не подходит: {e.message}") from None
        return await self._write(page, actor, "page.edit", expected_version=expected_version, body=body)

    async def set_title(self, code: str, lang: str, title: str, actor: tuple[int | None, str | None]) -> Page:
        page = self._require(code)
        value = title.strip()
        if not value or len(value) > 64:
            raise PageError("Название: от 1 до 64 символов")
        return await self._write(page, actor, "page.title", title={**page.title, lang: value})

    async def set_enabled(self, code: str, enabled: bool, actor: tuple[int | None, str | None]) -> Page:
        page = self._require(code)
        values: dict[str, Any] = {"enabled": enabled}
        if enabled and page.kind == "consent" and page.consent_version is None:
            values["consent_version"] = page.version  # switching consent on publishes the current text
        return await self._write(
            page, actor, "page.enable" if enabled else "page.disable", new_version=False, **values
        )

    async def request_consent(self, code: str, actor: tuple[int | None, str | None]) -> Page:
        """Everybody accepts the current text again (after a real change of the terms)."""
        page = self._require(code)
        if page.kind != "consent":
            raise PageError("Согласие запрашивается только на странице согласия")
        return await self._write(page, actor, "page.consent", new_version=False, consent_version=page.version)

    async def restore(self, code: str, version: int, actor: tuple[int | None, str | None]) -> Page:
        page = self._require(code)
        async with self.db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(page_versions.c.title, page_versions.c.body).where(
                        page_versions.c.page_id == page.id, page_versions.c.version == version
                    )
                )
            ).first()
        if row is None:
            raise PageError("Такой версии нет")
        return await self._write(page, actor, "page.restore", title=row.title, body=row.body)

    async def create(self, code: str, title: str, actor: tuple[int | None, str | None]) -> Page:
        value = code.strip().lower() if isinstance(code, str) else ""
        if not CODE_RE.fullmatch(value):
            raise PageError("Код страницы: латиница, цифры и «_», до 24 символов, с буквы")
        if len(self._pages) >= MAX_PAGES:
            raise PageError(f"Не больше {MAX_PAGES} страниц")
        name = title.strip()
        if not name or len(name) > 64:
            raise PageError("Название: от 1 до 64 символов")
        body = {DEFAULT_LANG: {"text": name}}
        try:
            async with self.db.tx() as conn:
                row = (
                    (
                        await conn.execute(
                            sa.insert(pages)
                            .values(
                                code=value,
                                kind="custom",
                                title={DEFAULT_LANG: name},
                                body=body,
                                updated_by=actor[0],
                            )
                            .returning(pages)
                        )
                    )
                    .mappings()
                    .one()
                )
                await conn.execute(
                    sa.insert(page_versions).values(
                        page_id=row["id"], version=1, title=row["title"], body=body, actor=actor[0]
                    )
                )
                await self._audit(conn, actor, "page.create", value, {})
        except IntegrityError:
            raise PageError("Такая страница уже есть") from None
        page = Page.from_row(row)
        self._pages[page.code] = page
        return page

    async def delete(self, code: str, actor: tuple[int | None, str | None]) -> None:
        page = self._require(code)
        if page.system:
            raise PageError("Системную страницу можно только выключить")
        async with self.db.tx() as conn:
            await conn.execute(sa.delete(pages).where(pages.c.id == page.id))
            await self._audit(conn, actor, "page.delete", page.code, {})
        self._pages.pop(page.code, None)
