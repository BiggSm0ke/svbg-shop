"""LTE quotas: user notifications and admin cards (05 §2.1.5).

User notifications are rows of the core ``notification_log`` (``UNIQUE(target, kind, anchor)``) inserted in
the transaction of the decision, with a delivery job ``lte.notify``:

=================  ==========================================  ====================  ==================
kind               when                                        anchor                quiet hours
=================  ==========================================  ====================  ==================
``lte_warn``       once per period and group, ``used ≥ warn%``  ``period:group``      postponed
``lte_exhausted``  only together with a really placed block     ``period:group:blk``  sent at once, silent
``lte_reset``      after a reset, if the past period had a      ``period:group``      postponed
                   warning or a block
=================  ==========================================  ====================  ==================

The text and the «⚡ Докупить трафик LTE» button are decided **at the moment of sending** (the pack may be
unavailable by then: then there is simply no button, Д20). Blocked, banned and frozen users are skipped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.clock import now
from svbg.core.tables import users
from svbg.ext.lte.decide import NOTIFY_EXHAUSTED, NOTIFY_RESET, NOTIFY_WARN, NotifyRequest
from svbg.ext.lte.model import MSK
from svbg.ext.lte.tables import lte_groups, lte_periods
from svbg.jobs.queue import enqueue
from svbg.services.notify_user import notification_log
from svbg.subscriptions.tables import subscriptions
from svbg.tg.report import Report

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from aiogram.types import InlineKeyboardMarkup
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.db.engine import Database
    from svbg.jobs.queue import Job
    from svbg.jobs.worker import JobContext

__all__ = [
    "JOB_KIND",
    "KINDS",
    "T_EN",
    "NotifyConfig",
    "NotifySender",
    "fmt_date",
    "fmt_gb",
    "group_name",
    "load_notified",
    "parse_quiet",
    "quiet_until",
    "record",
    "user_texts",
]

log = logging.getLogger("svbg.ext.lte.notify")

JOB_KIND: Final = "lte.notify"
_QUIET_RE: Final = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")
#: Engine kind → ``notification_log.kind``.
KINDS: Final[Mapping[str, str]] = {
    NOTIFY_WARN: "lte_warn",
    NOTIFY_EXHAUSTED: "lte_exhausted",
    NOTIFY_RESET: "lte_reset",
}
ENGINE_KIND: Final = {v: k for k, v in KINDS.items()}
#: How far back the "already sent in this period" lookup goes (a period is at most a month long).
NOTIFIED_WINDOW: Final = timedelta(days=70)

T: Final[Mapping[str, str]] = {
    "warn": "⚠️ <b>Трафик {group}</b>: осталось {left} ГБ из {limit} ГБ.\n"
    "Когда он закончится, серверы {group} станут недоступны {until}. Остальные серверы продолжат работать.",
    "exhausted": "🚫 <b>Трафик {group} исчерпан</b>.\nСерверы {group} недоступны {until}. Остальные серверы "
    "работают: выберите в приложении другой.",
    "reset": "✅ <b>Трафик {group} обновлён</b>: доступно {limit} ГБ до {reset}.",
    "until_date": "до {date}",
    "until_renew": "до продления подписки",
    "btn_topup": "⚡ Докупить трафик LTE",
    "btn_renew": "🔄 Продлить",
    "btn_connect": "🔗 Подключиться",
}
#: English of :data:`T` (same keys and placeholders).
T_EN: Final[Mapping[str, str]] = {
    "warn": "⚠️ <b>{group} traffic</b>: {left} GB of {limit} GB left.\n"
    "When it runs out, {group} servers will be unavailable {until}. Other servers will keep working.",
    "exhausted": "🚫 <b>{group} traffic is used up</b>.\n{group} servers are unavailable {until}. "
    "Other servers still work: pick another one in the app.",
    "reset": "✅ <b>{group} traffic renewed</b>: {limit} GB available until {reset}.",
    "until_date": "until {date}",
    "until_renew": "until the subscription is renewed",
    "btn_topup": "⚡ Buy more LTE traffic",
    "btn_renew": "🔄 Renew",
    "btn_connect": "🔗 Connect",
}


def user_texts(lang: str | None) -> Mapping[str, str]:
    """:data:`T` in ``lang`` (Russian fallback)."""
    return T_EN if lang == "en" else T


# ------------------------------------------------------------------------------------------- formats


def fmt_gb(value: int | None, gb_bytes: int = 10**9, lang: str | None = "ru") -> str:
    """``12,4`` (one decimal, Russian comma — a point in English, whole numbers without ``,0``)."""
    if value is None:
        return "∞"
    gb = max(0, int(value)) / max(1, int(gb_bytes))
    sep = "." if lang == "en" else ","
    text = f"{gb:.1f}".replace(".", sep)
    return text[:-2] if text.endswith(f"{sep}0") else text


def fmt_date(moment: datetime | None) -> str:
    """``07.10`` in Moscow time."""
    if moment is None:
        return "—"
    return moment.astimezone(MSK).strftime("%d.%m")


def group_name(name: Any, lang: str = "ru") -> str:
    """The user-facing group name (``{"ru": "LTE"}``); never the words «белые списки» / WLQ."""
    if isinstance(name, Mapping):
        value = name.get(lang) or name.get("ru") or next((v for v in name.values() if v), None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:40]
    return "LTE"


def parse_quiet(value: Any) -> tuple[time, time] | None:
    """``"00:00-09:00"`` → ``(00:00, 09:00)``; empty / ``"off"`` → no quiet hours; garbage raises."""
    text = str(value or "").strip()
    if not text or text.lower() in ("off", "нет", "-"):
        return None
    parts = text.split("-", 1)
    found = [_QUIET_RE.match(part.strip()) for part in parts]
    if len(parts) != 2 or not all(found):
        raise ValueError("формат ЧЧ:ММ-ЧЧ:ММ, например 00:00-09:00")
    try:
        a, b = (time(int(m[1]), int(m[2] or 0)) for m in found if m is not None)
    except ValueError:
        raise ValueError("формат ЧЧ:ММ-ЧЧ:ММ, например 00:00-09:00") from None
    return a, b


def quiet_until(at: datetime, quiet: tuple[time, time] | None) -> datetime | None:
    """End of the quiet window (Moscow time) containing ``at``, or ``None`` when ``at`` is outside it."""
    if quiet is None:
        return None
    start, end = quiet
    if start == end:
        return None
    local = at.astimezone(MSK)
    t = local.time()
    inside = start <= t < end if start < end else (t >= start or t < end)
    if not inside:
        return None
    day = local.date()
    if start > end and t >= start:
        day += timedelta(days=1)
    return datetime.combine(day, end, tzinfo=MSK).astimezone(at.tzinfo)


# ------------------------------------------------------------------------------------------ recording


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    enabled: bool = True
    quiet: tuple[time, time] | None = (time(0), time(9))


async def record(
    conn: AsyncConnection,
    req: NotifyRequest,
    *,
    user_id: int | None,
    block_id: int | None = None,
    cfg: NotifyConfig,
    at: datetime | None = None,
) -> int | None:
    """Log the notification and queue its delivery (caller's transaction). ``None``: duplicate or off."""
    if not cfg.enabled or user_id is None:
        return None
    kind = KINDS.get(req.kind)
    if kind is None:
        return None
    at = at or now()
    anchor = f"{req.period_id}:{req.group_id}"
    if req.kind == NOTIFY_EXHAUSTED:
        anchor += f":{block_id or 0}"
    stmt = (
        pg_insert(notification_log)
        .values(
            target=f"sub:{req.subscription_id}",
            kind=kind,
            anchor=anchor,
            user_id=user_id,
            subscription_id=req.subscription_id,
            payload={
                "group_id": req.group_id,
                "period_id": req.period_id,
                "used": req.used_bytes,
                "limit": req.limit_bytes,
                "threshold": req.threshold,
            },
        )
        .on_conflict_do_nothing(index_elements=["target", "kind", "anchor"])
        .returning(notification_log.c.id)
    )
    log_id = (await conn.execute(stmt)).scalar()
    if log_id is None:
        return None
    run_at = None if req.kind == NOTIFY_EXHAUSTED else quiet_until(at, cfg.quiet)
    await enqueue(
        conn,
        JOB_KIND,
        {"log_id": int(log_id)},
        queue="notify",
        lane="background",
        run_at=run_at,
        dedup_key=f"lte.notify:{int(log_id)}",
        max_attempts=5,
        caused_by=f"lte:{kind}:{req.subscription_id}",
    )
    return int(log_id)


async def load_notified(
    conn: AsyncConnection, sids: Sequence[int], *, at: datetime
) -> dict[int, set[tuple[int, int, str]]]:
    """``sid → {(period_id, group_id, engine kind)}`` already logged (sent or pending)."""
    if not sids:
        return {}
    n = notification_log.c
    rows = (
        await conn.execute(
            sa.select(n.subscription_id, n.kind, n.anchor).where(
                n.subscription_id.in_(list(sids)),
                n.kind.in_(list(KINDS.values())),
                n.created_at > at - NOTIFIED_WINDOW,
            )
        )
    ).all()
    out: dict[int, set[tuple[int, int, str]]] = {}
    for sid, kind, anchor in rows:
        parts = str(anchor).split(":")
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        out.setdefault(int(sid), set()).add((int(parts[0]), int(parts[1]), ENGINE_KIND[kind]))
    return out


# --------------------------------------------------------------------------------------------- sending

#: ``availability`` of the pack button at the moment of sending: ``(subscription_id, group_id) → bool``.
TopupCheck = Callable[[int, int], Any]


class NotifySender:
    """``lte.notify`` job: render at sending time, send through the core ``Notifier``."""

    def __init__(
        self,
        db: Database,
        *,
        notifier: Any | None,
        config: Callable[[], NotifyConfig],
        gb_bytes: Callable[[], int] = lambda: 10**9,
        topup_ok: TopupCheck | None = None,
    ) -> None:
        self._db = db
        self._notifier = notifier
        self._config = config
        self._gb = gb_bytes
        self._topup_ok = topup_ok

    async def send_job(self, job: Job, ctx: JobContext) -> None:
        del ctx
        log_id = int(job.payload["log_id"])
        row = await self._load(log_id)
        if row is None or row["status"] != "pending":
            return
        reason = self._skip_reason(row)
        if reason is not None:
            await self._mark(log_id, "skipped", reason)
            return
        text, keyboard = await self.render(row)
        silent = row["kind"] == KINDS[NOTIFY_EXHAUSTED]
        try:
            sent = await self._notifier.send(  # type: ignore[union-attr]
                int(row["telegram_id"]),
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_notification=silent or None,
            )
        except Exception as err:  # noqa: BLE001 - an unknown outcome is never re-sent (no duplicates)
            log.warning("lte: notification %s failed: %s", log_id, type(err).__name__)
            await self._mark(log_id, "skipped", "send_failed")
            return
        await self._mark(
            log_id, "sent" if sent is not None else "skipped", None if sent is not None else "blocked"
        )

    def _skip_reason(self, row: Mapping[str, Any]) -> str | None:
        if self._notifier is None:
            return "no_notifier"
        if not self._config().enabled:
            return "off"
        if row["telegram_id"] is None or row["banned_at"] is not None or row["bot_blocked_at"] is not None:
            return "unreachable"
        if row["hold_kind"] is not None:
            return "frozen"
        if row["kind"] == KINDS[NOTIFY_WARN] and row["period_state"] == "closed":
            return "stale"
        return None

    async def render(self, row: Mapping[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
        from aiogram.types import InlineKeyboardMarkup

        from svbg.billing.texts import lang_of
        from svbg.tg.ui.renderer import nav_button

        lang = lang_of(row.get("language"))
        tx = user_texts(lang)
        payload = row["payload"] or {}
        gb = self._gb()
        group = group_name(row["group_name"], lang)
        limit = payload.get("limit")
        used = int(payload.get("used") or 0)
        deferred = row["period_state"] == "deferred"
        until = (
            tx["until_renew"] if deferred else tx["until_date"].format(date=fmt_date(row["planned_end_at"]))
        )
        kind = ENGINE_KIND.get(str(row["kind"]), NOTIFY_WARN)
        sid, gid = int(row["subscription_id"]), int(payload.get("group_id") or 0)
        rows: list[list[Any]] = []
        topup = False
        if kind != NOTIFY_RESET and self._topup_ok is not None:
            try:
                result = self._topup_ok(sid, gid)
                topup = bool(await result) if hasattr(result, "__await__") else bool(result)
            except Exception:  # noqa: BLE001 - no button rather than no message
                topup = False
        if kind == NOTIFY_WARN:
            left = max(0, int(limit or 0) - used) if limit is not None else 0
            text = tx["warn"].format(
                group=group, left=fmt_gb(left, gb, lang), limit=fmt_gb(limit, gb, lang), until=until
            )
        elif kind == NOTIFY_EXHAUSTED:
            text = tx["exhausted"].format(group=group, until=until)
        else:
            text = tx["reset"].format(
                group=group, limit=fmt_gb(limit, gb, lang), reset=fmt_date(row["planned_end_at"])
            )
        if topup:
            rows.append([nav_button(tx["btn_topup"], "lte_topup", style="success")])
        elif kind == NOTIFY_EXHAUSTED and deferred:
            rows.append([nav_button(tx["btn_renew"], "buy", style="primary")])
        rows.append([nav_button(tx["btn_connect"], "connect")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    async def _load(self, log_id: int) -> Mapping[str, Any] | None:
        n = notification_log.c
        group_id = n.payload["group_id"].astext.cast(sa.BigInteger)
        period_id = n.payload["period_id"].astext.cast(sa.BigInteger)
        q = (
            sa.select(
                n.id,
                n.kind,
                n.status,
                n.payload,
                n.subscription_id,
                users.c.telegram_id,
                users.c.language,
                users.c.banned_at,
                users.c.bot_blocked_at,
                subscriptions.c.hold_kind,
                lte_groups.c.name.label("group_name"),
                lte_periods.c.state.label("period_state"),
                lte_periods.c.planned_end_at,
            )
            .select_from(
                notification_log.join(users, users.c.id == n.user_id)
                .join(subscriptions, subscriptions.c.id == n.subscription_id)
                .outerjoin(lte_groups, lte_groups.c.id == group_id)
                .outerjoin(lte_periods, lte_periods.c.id == period_id)
            )
            .where(n.id == log_id)
        )
        async with self._db.read() as conn:
            return (await conn.execute(q)).mappings().first()

    async def _mark(self, log_id: int, status: str, reason: str | None) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                sa.update(notification_log)
                .where(notification_log.c.id == log_id, notification_log.c.status == "pending")
                .values(status=status, reason=reason, sent_at=now() if status == "sent" else None)
            )


# ----------------------------------------------------------------------------------------- admin cards

CARD_T: Final[Mapping[str, str]] = {
    "block": "🚫 <b>LTE: блок</b> · подписка №{sid}\nИзрасходовано {used} из {limit} ГБ · причина: {reason}",
    "release_all": "✅ <b>LTE: блоки сняты</b> ({n} шт., {reason})",
    "topup": "⚡ <b>LTE: докупка</b> · подписка №{sid}: +{gb} ГБ",
}
REASONS: Final[Mapping[str, str]] = {"quota": "лимит", "unavailable": "недоступно", "manual": "вручную"}


def card_report(kind: str, **kw: Any) -> Report:
    """A card for «🌐 Трафик LTE»: ``block`` (sid, used, limit, reason), ``release_all`` (n, reason),
    ``topup`` (sid, gb). ``used`` / ``limit`` / ``gb`` are already formatted numbers of GB."""
    if kind == "block":
        return (
            Report("🚫", "LTE: блок")
            .line("Подписка", f"№{kw['sid']}")
            .line("Израсходовано", f"{kw['used']} из {kw['limit']} ГБ")
            .line("Причина", str(kw["reason"]))
        )
    if kind == "release_all":
        rep = Report("✅", "LTE: блоки сняты").line("Снято", f"{kw['n']} шт.")
        return rep.line("Причина", str(kw["reason"]))
    if kind == "topup":
        rep = Report("⚡", "LTE: докупка").line("Подписка", f"№{kw['sid']}")
        return rep.line("Добавлено", f"+{kw['gb']} ГБ")
    raise ValueError(f"unknown LTE card {kind!r}")


def card_from_payload(payload: Mapping[str, Any]) -> Report | str | None:
    """The card of a ``lte.card`` job: ``{"card": kind, ...fields}``; older jobs carry ready ``text``."""
    kind = payload.get("card")
    if isinstance(kind, str):
        fields = {k: v for k, v in payload.items() if k not in ("card", "text")}
        try:
            return card_report(kind, **fields)
        except (KeyError, ValueError):
            log.warning("lte: bad card payload %r", kind)
    text = payload.get("text")
    return text[:3000] if isinstance(text, str) and text else None


async def post_cards(admin_chat: Any | None, cards: Iterable[Report | str]) -> None:
    """Best effort: a card that could not be posted never breaks the decision (already committed).

    A :class:`~svbg.tg.report.Report` goes as a report (rich where the chat takes it), a string as HTML."""
    if admin_chat is None:
        return
    for card in cards:
        try:
            if isinstance(card, Report):
                if hasattr(admin_chat, "post_report"):
                    await admin_chat.post_report("lte", card)
                else:
                    await admin_chat.post("lte", card.html(), html=True)
            else:
                await admin_chat.post("lte", card, html=True)
        except Exception:
            log.exception("lte: admin card failed")
