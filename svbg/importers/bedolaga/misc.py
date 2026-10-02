"""History, dedup seeds, pages and the owner's to-do lists (06 §2.4.5, §2.10).

* ``transactions`` → ``legacy_transactions`` (read-only history; never into ``wallet_ledger``). The two
legs of a
  site payment — «+ Оплата на сайте (RollyPay), заказ tc_…» and «− Продление «…» через сайт» — share a
  ``pair_key`` (the ``tc_…`` reference, or the nearest top-up of the same user and amount within ±10 min) and
  only the top-up counts as revenue, so the site revenue is not doubled;
* ``sent_notifications`` of subscriptions ending after ``T0 − 3 days`` → ``notification_log``
(``status='sent'``,
  anchor = the imported ``paid_until``) so expiry reminders are not sent again after the switch (R12, С10);
* ``faq_pages`` (+ ``faq_settings``), ``service_rules``, ``public_offers``, ``privacy_policies`` → ``pages``
  ``faq`` / ``rules`` / ``offer`` / ``privacy`` (06 §1.2, §2.10): Telegram HTML → text + Bot API entities
  (:func:`html_to_entities`), one page per kind with a block per language. A page is written only while it is
  the importer's (or the untouched seeded placeholder); a page the owner edited in the bot is left alone and
  reported, a text longer than one message is reported (``page_too_long``) and not cut;
* owner's lists (no rows written): paid but undelivered ``guest_purchases`` (blocking: settle before T0,
  06 §4.1 п.9), live unclaimed ``discount_offers`` (an offer per owner's decision), role assignments
  (``user_roles`` → Owner / Admin / Support by owner's decision).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.content.model import MAX_ENTITIES, MAX_TEXT, ContentError, parse_text_blocks
from svbg.importers import legacy_transactions
from svbg.importers.bedolaga.plan import SOURCE, chunks
from svbg.pages.service import SYSTEM_PAGES
from svbg.pages.tables import page_versions, pages
from svbg.services.notify_user import epoch_anchor, notification_log
from svbg.subscriptions.tables import subscriptions

if TYPE_CHECKING:
    from datetime import datetime

    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["PAGE_SOURCES", "check", "html_to_entities", "notification_kind", "run"]

_TC: Final = re.compile(r"\btc_[A-Za-z0-9]+")
PAIR_WINDOW: Final = timedelta(minutes=10)


def notification_kind(kind: str | None, days_before: int | None) -> str | None:
    """Bedolaga ``notification_type`` (+ ``days_before``) → our ``notification_log.kind``."""
    k = str(kind or "").lower()
    if "trial" in k:
        return "trial_ending"
    if "expired" in k:
        return "expired"
    if ("expir" in k or "remind" in k or "renew" in k) and days_before:
        return f"expiring_{int(days_before) * 24}h"
    return None


async def run(ctx: Ctx) -> None:
    await _transactions(ctx)
    await _notifications(ctx)
    await _pages(ctx)
    await _owner_lists(ctx)


async def _transactions(ctx: Ctx) -> None:
    rep = ctx.report
    rows = await ctx.src.rows(
        "transactions",
        [
            "id",
            "user_id",
            "type",
            "amount_kopeks",
            "description",
            "payment_method",
            "external_id",
            "is_completed",
            "created_at",
            "completed_at",
        ],
    )
    pair: dict[int, str] = {}
    revenue: dict[int, bool] = {}
    topups: dict[tuple[int, int], list[tuple[datetime, str]]] = {}
    for r in rows:
        m = _TC.search(str(r["description"] or ""))
        if m:
            pair[int(r["id"])] = m.group(0)
            if str(r["type"] or "").lower() == "deposit" and r["created_at"] is not None:
                topups.setdefault((int(r["user_id"]), abs(int(r["amount_kopeks"] or 0))), []).append(
                    (r["created_at"], m.group(0))
                )
    for r in rows:
        tid = int(r["id"])
        desc = str(r["description"] or "").lower()
        is_site_purchase = str(r["type"] or "").lower() != "deposit" and "сайт" in desc
        if is_site_purchase and tid not in pair and r["created_at"] is not None:
            for at, key in topups.get((int(r["user_id"]), abs(int(r["amount_kopeks"] or 0))), []):
                if abs(at - r["created_at"]) <= PAIR_WINDOW:
                    pair[tid] = key
                    break
        if is_site_purchase and tid in pair:
            revenue[tid] = False
    values: list[dict[str, Any]] = [
        {
            "source": SOURCE,
            "legacy_id": str(r["id"]),
            "user_id": r["user_id"],
            "type": str(r["type"] or "unknown"),
            "amount_minor": int(r["amount_kopeks"] or 0),
            "currency": ctx.cfg.currency,
            "description": r["description"],
            "payment_method": r["payment_method"],
            "external_id": r["external_id"],
            "is_completed": r["is_completed"],
            "pair_key": pair.get(int(r["id"])),
            "counts_as_revenue": revenue.get(int(r["id"]), True),
            "created_at": r["created_at"],
            "completed_at": r["completed_at"],
        }
        for r in rows
    ]
    inserted = 0
    lt = legacy_transactions
    mutable = ("is_completed", "completed_at", "pair_key", "counts_as_revenue", "description", "external_id")
    for chunk in chunks(values, 1000):
        stmt = pg_insert(lt).values(chunk)
        changed = sa.or_(*(lt.c[c].is_distinct_from(stmt.excluded[c]) for c in mutable))
        res = await ctx.conn.execute(
            stmt.on_conflict_do_update(
                constraint="uq_legacy_transactions_source_legacy_id",
                set_={c: stmt.excluded[c] for c in mutable},
                where=changed,  # a re-run follows the source (a top-up completed later) without churn
            ).returning(lt.c.id, sa.literal_column("xmax = 0").label("inserted"))
        )
        got = res.all()
        inserted += sum(1 for r in got if r.inserted)
        rep.inc("misc", "transactions_refreshed", sum(1 for r in got if not r.inserted))
    rep.set("misc", "transactions_source", len(rows))
    rep.inc("misc", "transactions_imported", inserted)
    rep.set("misc", "site_pairs", sum(1 for v in revenue.values() if v is False))


async def _eligible_notifications(ctx: Ctx) -> list[dict[str, Any]]:
    rows = await ctx.src.rows(
        "sent_notifications",
        ["id", "user_id", "subscription_id", "notification_type", "days_before", "created_at"],
    )
    floor = ctx.t0 - timedelta(days=ctx.cfg.notify_window_days)
    sids = sorted({int(r["subscription_id"]) for r in rows} & set(ctx.subs))
    info: dict[int, tuple[Any, Any]] = {}
    for chunk in chunks(sids, 5000):
        for sid, uid, paid in (
            await ctx.conn.execute(
                sa.select(subscriptions.c.id, subscriptions.c.user_id, subscriptions.c.paid_until).where(
                    subscriptions.c.id.in_(chunk)
                )
            )
        ).all():
            info[int(sid)] = (uid, paid)
    out = []
    for r in rows:
        sid = int(r["subscription_id"])
        src = ctx.source_subs.get(sid)
        if sid not in info or src is None or src["end_date"] is None or src["end_date"] <= floor:
            continue
        kind = notification_kind(r["notification_type"], r["days_before"])
        if kind is None:
            ctx.report.inc("misc", "notifications_unmapped")
            continue
        uid, paid = info[sid]
        out.append(
            {
                "target": f"sub:{sid}",
                "kind": kind,
                "anchor": epoch_anchor(paid),
                "user_id": uid,
                "subscription_id": sid,
                "payload": {"legacy_id": int(r["id"])},
                "status": "sent",
                "reason": "import:bedolaga",
                "created_at": r["created_at"] or ctx.t0,
                "sent_at": r["created_at"] or ctx.t0,
            }
        )
    return out


async def _notifications(ctx: Ctx) -> None:
    values = await _eligible_notifications(ctx)
    seeded = 0
    for chunk in chunks(values, 1000):
        res = await ctx.conn.execute(
            pg_insert(notification_log)
            .values(chunk)
            .on_conflict_do_nothing()
            .returning(notification_log.c.id)
        )
        seeded += len(res.all())
    ctx.report.set("misc", "notifications_eligible", len(values))
    ctx.report.inc("misc", "notifications_seeded", seeded)


async def check(ctx: Ctx) -> None:
    """С10: every reminder Bedolaga already sent for a live subscription is in ``notification_log``;
    С1 (history part): every transaction is in ``legacy_transactions``."""
    want = await _eligible_notifications(ctx)
    keys = {(v["target"], v["kind"], v["anchor"]) for v in want}
    present = 0
    targets = sorted({k[0] for k in keys})
    for chunk in chunks(targets, 5000):
        rows = (
            await ctx.conn.execute(
                sa.select(
                    notification_log.c.target, notification_log.c.kind, notification_log.c.anchor
                ).where(notification_log.c.target.in_(chunk))
            )
        ).all()
        present += sum(1 for r in rows if (r[0], r[1], r[2]) in keys)
    ctx.report.check("C10", present == len(keys), expected=len(keys), present=present)
    have = int(
        await ctx.conn.scalar(
            sa.select(sa.func.count())
            .select_from(legacy_transactions)
            .where(legacy_transactions.c.source == SOURCE)
        )
        or 0
    )
    ctx.report.part(
        "C1", "legacy_transactions", expected=ctx.report.get("misc", "transactions_source"), present=have
    )


# ------------------------------------------------------------------------------------------- HTML → entities

_SIMPLE: Final = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "tg-spoiler": "spoiler",
}
_BLOCK_BREAK: Final = frozenset({"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"})
_URL_OK: Final = re.compile(r"^(?:https?|tg)://\S+$", re.IGNORECASE)


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(slots=True)
class _Open:
    tag: str
    start: int
    entity: dict[str, Any] | None


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0
        self.stack: list[_Open] = []
        self.entities: list[dict[str, Any]] = []

    def _text(self, text: str) -> None:
        if text:
            self.parts.append(text)
            self.pos += _u16(text)

    def _close(self, item: _Open) -> None:
        if item.entity is not None and self.pos > item.start:
            self.entities.append({**item.entity, "offset": item.start, "length": self.pos - item.start})

    def handle_data(self, data: str) -> None:
        self._text(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._text("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "br":
            self._text("\n")
            return
        entity: dict[str, Any] | None = None
        if tag in _SIMPLE:
            entity = {"type": _SIMPLE[tag]}
        elif tag == "span" and "tg-spoiler" in a.get("class", ""):
            entity = {"type": "spoiler"}
        elif tag == "a":
            url = a.get("href", "").strip()
            entity = {"type": "text_link", "url": url} if _URL_OK.match(url) else None
        elif tag == "pre":
            entity = {"type": "pre"}
        elif tag == "code":
            outer = self.stack[-1] if self.stack else None
            if outer is not None and outer.tag == "pre" and outer.entity is not None:
                lang = a.get("class", "").removeprefix("language-").strip()
                if lang:
                    outer.entity["language"] = lang
            else:
                entity = {"type": "code"}
        elif tag == "blockquote":
            entity = {"type": "expandable_blockquote" if "expandable" in a else "blockquote"}
        elif tag in _BLOCK_BREAK and self.parts and not self.parts[-1].endswith("\n"):
            self._text("\n")
        self.stack.append(_Open(tag, self.pos, entity))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag != tag:
                continue
            for item in reversed(self.stack[i:]):
                self._close(item)
            del self.stack[i:]
            if tag in _BLOCK_BREAK:
                self._text("\n")
            return


def html_to_entities(html: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Telegram-flavoured HTML (Bedolaga ``parse_mode=HTML``) → ``(text, entities)`` with UTF-16 offsets.

    Supported: ``b/strong i/em u/ins s/strike/del tg-spoiler span.tg-spoiler a[href] code pre(+language)
    blockquote[expandable] br``; ``p/div/li/h*`` become line breaks; other tags are dropped (their text is
    kept); a link with a non-http(s)/tg URL keeps only its text. Surrounding whitespace is trimmed."""
    p = _Html()
    p.feed(str(html or ""))
    p.close()
    for item in reversed(p.stack):  # unclosed tags end with the text
        p._close(item)
    text = "".join(p.parts)
    stripped = text.strip()
    lead = _u16(text[: len(text) - len(text.lstrip())])
    size = _u16(stripped)
    out = []
    for e in p.entities:
        start = max(0, e["offset"] - lead)
        end = min(e["offset"] + e["length"] - lead, size)
        if end > start:
            out.append({**e, "offset": start, "length": end - start})
    out.sort(key=lambda e: (e["offset"], -e["length"]))
    return stripped, out


Block = tuple[str, list[dict[str, Any]]]


def _join(blocks: list[Block], sep: str = "\n\n") -> Block:
    text, ents = "", []
    for t, es in blocks:
        if not t:
            continue
        if text:
            text += sep
        shift = _u16(text)
        ents.extend({**e, "offset": e["offset"] + shift} for e in es)
        text += t
    return text, ents


def _titled(title: str | None, html: str | None) -> Block:
    body = html_to_entities(html)
    head = " ".join(str(title or "").split())
    if not head:
        return body
    return _join([(head, [{"type": "bold", "offset": 0, "length": _u16(head)}]), body], sep="\n")


# ----------------------------------------------------------------------------------------------- pages

#: page code → (kind, default title).
PAGE_SOURCES: Final = {
    "faq": ("faq", "❓ Вопросы и ответы"),
    "rules": ("rules", "📜 Правила"),
    "offer": ("offer", "📄 Оферта"),
    "privacy": ("custom", "🔒 Политика конфиденциальности"),
}
_SEEDED: Final = {code: {"ru": {"text": text}} for code, _kind, _title, text in SYSTEM_PAGES}
_LANG: Final = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?")


def _lang_key(value: Any) -> str | None:
    code = str(value or "ru").strip().lower().replace("_", "-")
    return code if _LANG.fullmatch(code) else None


def _ordered(rows: list[dict[str, Any]], order: str) -> dict[str, list[Block]]:
    by_lang: dict[str, list[Block]] = {}
    for r in sorted(rows, key=lambda r: (r[order] or 0, r["id"])):
        lang = _lang_key(r["language"])
        if lang and r["is_active"] is not False:
            by_lang.setdefault(lang, []).append(_titled(r["title"], r["content"]))
    return by_lang


async def _page_sources(ctx: Ctx) -> dict[str, tuple[dict[str, Block], bool]]:
    """page code → ({lang: (text, entities)}, enabled)."""
    src = ctx.src
    out: dict[str, tuple[dict[str, Block], bool]] = {}
    faq = _ordered(
        await src.rows("faq_pages", ["id", "language", "title", "content", "display_order", "is_active"]),
        "display_order",
    )
    if faq:
        on = {
            _lang_key(r["language"]): bool(r["is_enabled"])
            for r in await src.rows("faq_settings", ["id", "language", "is_enabled"])
        }
        out["faq"] = ({lang: _join(b) for lang, b in faq.items()}, any(on.get(lang, True) for lang in faq))
    rules = _ordered(
        await src.rows("service_rules", ["id", "order", "title", "content", "is_active", "language"]), "order"
    )
    if rules:
        out["rules"] = ({lang: _join(b) for lang, b in rules.items()}, True)
    for code, table in (("offer", "public_offers"), ("privacy", "privacy_policies")):
        blocks: dict[str, Block] = {}
        enabled = False
        # the latest row per language wins
        for r in await src.rows(table, ["id", "language", "content", "is_enabled"]):
            lang = _lang_key(r["language"])
            if lang and str(r["content"] or "").strip():
                blocks[lang] = html_to_entities(r["content"])
                enabled = enabled or bool(r["is_enabled"])
        if blocks:
            out[code] = (blocks, enabled)
    return out


async def _pages(ctx: Ctx) -> None:
    rep = ctx.report
    sources = await _page_sources(ctx)
    if not sources:
        return
    current = {
        str(r["code"]): dict(r)
        for r in (await ctx.conn.execute(sa.select(pages).where(pages.c.code.in_(list(sources))))).mappings()
    }
    mapped = await ctx.mapped("page")
    remember: list[tuple[str, int, dict[str, Any]]] = []
    for code, (found, enabled) in sources.items():
        rep.inc("misc", "pages_source")
        blocks = found
        too_long = {
            lang: len(text)
            for lang, (text, ents) in blocks.items()
            if len(text) > MAX_TEXT or len(ents) > MAX_ENTITIES
        }
        if too_long:
            rep.issue("page_too_long", page=code, chars=too_long, limit=MAX_TEXT)
            blocks = {lang: b for lang, b in blocks.items() if lang not in too_long}
            if not blocks:
                continue
        body = {lang: ({"text": t, "entities": e} if e else {"text": t}) for lang, (t, e) in blocks.items()}
        try:
            parse_text_blocks(body)
        except ContentError as exc:
            rep.issue("page_invalid", page=code, reason=str(exc)[:200])
            continue
        kind, title = PAGE_SOURCES[code]
        row = current.get(code)
        if row is None:
            page_id = int(
                await ctx.conn.scalar(
                    sa.insert(pages)
                    .values(code=code, kind=kind, title={"ru": title}, body=body, enabled=enabled)
                    .returning(pages.c.id)
                )
            )
            await ctx.conn.execute(
                sa.insert(page_versions).values(page_id=page_id, version=1, title={"ru": title}, body=body)
            )
            remember.append((code, page_id, {"version": 1}))
            rep.inc("misc", "pages_created")
            continue
        prev = mapped.get(code)
        ours = prev is not None and int(prev[1].get("version", 0)) == int(row["version"])
        seeded = prev is None and int(row["version"]) == 1 and row["body"] == _SEEDED.get(code)
        if not (ours or seeded):
            rep.issue("page_changed_locally", page=code, version=int(row["version"]))
            continue
        if row["body"] == body and bool(row["enabled"]) == enabled:
            rep.inc("misc", "pages_unchanged")
            continue
        new = (
            await ctx.conn.execute(
                sa.update(pages)
                .where(pages.c.id == row["id"], pages.c.version == row["version"])
                .values(body=body, enabled=enabled, version=pages.c.version + 1, updated_at=sa.func.now())
                .returning(pages.c.version, pages.c.title)
            )
        ).first()
        if new is None:  # pragma: no cover - the row was read in this transaction
            rep.issue("page_changed_locally", page=code, version=int(row["version"]))
            continue
        await ctx.conn.execute(
            sa.insert(page_versions).values(page_id=row["id"], version=int(new[0]), title=new[1], body=body)
        )
        remember.append((code, int(row["id"]), {"version": int(new[0])}))
        rep.inc("misc", "pages_updated")
    await ctx.remember("page", remember)


# ------------------------------------------------------------------------------------------- owner lists

_UNDELIVERED: Final = frozenset({"paid", "pending_activation", "delivery_failed", "failed_delivery"})


async def _owner_lists(ctx: Ctx) -> None:
    """Rows that are settled by hand (06 §2.10, §4.1 п.9): reported, never written."""
    rep, t0 = ctx.report, ctx.t0
    for g in await ctx.src.rows(
        "guest_purchases",
        ["id", "status", "amount_kopeks", "currency", "paid_at", "delivered_at", "buyer_user_id", "is_gift"],
    ):
        rep.inc("misc", "guest_purchases_source")
        if str(g["status"] or "").lower() in _UNDELIVERED and g["delivered_at"] is None:
            rep.issue(
                "guest_purchase_undelivered",
                guest_purchase_id=g["id"],
                status=g["status"],
                amount_kopeks=g["amount_kopeks"],
                buyer_user_id=g["buyer_user_id"],
                paid_at=g["paid_at"],
            )
    for o in await ctx.src.rows(
        "discount_offers",
        ["id", "user_id", "discount_percent", "bonus_amount_kopeks", "expires_at", "claimed_at", "is_active"],
    ):
        live = o["expires_at"] is not None and o["expires_at"] > t0
        if o["is_active"] and o["claimed_at"] is None and live:
            rep.issue(
                "discount_offer_open",
                offer_id=o["id"],
                user_id=o["user_id"],
                percent=o["discount_percent"],
                bonus_kopeks=o["bonus_amount_kopeks"],
                expires_at=o["expires_at"],
            )
    names = {int(r["id"]): str(r["name"]) for r in await ctx.src.rows("admin_roles", ["id", "name"])}
    for r in await ctx.src.rows("user_roles", ["id", "user_id", "role_id", "is_active", "expires_at"]):
        if r["is_active"] is False or (r["expires_at"] is not None and r["expires_at"] <= t0):
            continue
        role = names.get(int(r["role_id"]), str(r["role_id"]))
        rep.issue("role_to_assign", user_id=r["user_id"], role=role)
