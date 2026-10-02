"""Cutover and rollback helpers for the Bedolaga migration (06 §1.4, §4.4, §4.7, §4.9).

Commands (added to ``python -m svbg`` by :func:`add_commands`)::

    cutover check                      readiness gates (06 §1.4 + shadow exit criterion §4.2 p.4)
    cutover export-rollback --since T0 rollback journal with the «pending» section (06 §4.7 p.2)
    cutover webhook-info               getWebhookInfo of the live token (06 §4.4 step 5)
    cutover delete-webhook --yes       deleteWebhook(drop_pending_updates=false) — the queue is kept
    cutover probe-panel                PATCH-probe of a panel token: 403 = read-only, 404 = can write
    cutover shadow-run                 one shadow reconciliation now (06 §4.4 step 3)
    pay freeze | unfreeze | status     stop issuing NEW invoices; webhooks, reconciler and
                                       ``successful_payment`` keep working (06 §4.7 p.1)

Secrets never come from the command line (``ps`` shows it): the bot token is read from ``--token-file`` or
``SVBG_CUTOVER_BOT_TOKEN``, the panel token from ``--token-file`` / ``SVBG_PANEL_TOKEN`` / ``data/.env``, the
source DSN from ``--source-dsn-file`` / ``SVBG_SHADOW_SOURCE_DSN``. There is **no** code path here that sends
``drop_pending_updates=true`` (06 R2): pending Telegram updates (``successful_payment`` of Stars!) survive
every switch.

Exit codes follow ``svbg.__main__``: 0 ok, 1 failure / red gate, 2 usage, 78 configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import asyncpg

from svbg.core.clock import now

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "FREEZE_KEY",
    "FREEZE_TEXT",
    "GateItem",
    "RollbackExport",
    "add_commands",
    "delete_webhook_keep_queue",
    "export_rollback",
    "gate_check",
    "install_pay_freeze",
    "pay_freeze_guard",
    "pay_freeze_state",
    "set_pay_freeze",
    "webhook_info",
]

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_CONFIG: Final = 78

FREEZE_KEY: Final = "pay.freeze"
FREEZE_TEXT: Final = "Оплата временно недоступна: идут технические работы. Попробуйте чуть позже."
SHADOW_MAX_AGE: Final = timedelta(hours=26)
_ENV_BOT_TOKEN: Final = "SVBG_CUTOVER_BOT_TOKEN"  # noqa: S105 - variable name, not a secret
_ENV_PANEL_TOKEN: Final = "SVBG_PANEL_TOKEN"  # noqa: S105
_ENV_SOURCE_DSN: Final = "SVBG_SHADOW_SOURCE_DSN"
DEFAULT_INSTANCES: Final = ("rollypay", "cryptobot", "stars")


class _Io(Protocol):
    def say(self, text: str = "") -> None: ...

    def warn(self, text: str) -> None: ...


class _Fail(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


def _pg(dsn: str) -> str:
    from svbg.db.engine import normalize_dsn

    return normalize_dsn(dsn)[1]


async def _connect(dsn: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(_pg(dsn))
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: v if isinstance(v, str) else json.dumps(v),
            decoder=json.loads,
            schema="pg_catalog",
        )
    return conn


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _iso(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_iso(v) for v in value]
    return value


async def _has_table(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}"))


# ------------------------------------------------------------------------------------------- pay freeze


async def pay_freeze_state(conn: asyncpg.Connection) -> dict[str, Any] | None:
    """The freeze record when payments are frozen, else ``None``."""
    value = await conn.fetchval("SELECT value FROM config_meta WHERE key = $1", FREEZE_KEY)
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) and value.get("on") else None


async def set_pay_freeze(
    dsn: str, on: bool, *, reason: str = "", actor_id: int | None = None
) -> dict[str, Any]:
    """Freeze / unfreeze issuing new invoices (idempotent). Audited in ``admin_audit`` in the same
    transaction."""
    conn = await _connect(dsn)
    try:
        async with conn.transaction():
            current = await pay_freeze_state(conn)
            if on and current is not None:
                return current
            value = {"on": on, "since": now().isoformat(), "reason": reason or None, "actor_id": actor_id}
            await conn.execute(
                "INSERT INTO config_meta (key, value, updated_at) VALUES ($1, $2::jsonb, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                FREEZE_KEY,
                value,
            )
            if on or current is not None:
                await conn.execute(
                    "INSERT INTO admin_audit (actor_id, action, target, reason, details) "
                    "VALUES ($1, $2, 'payments', $3, $4::jsonb)",
                    actor_id,
                    "pay.freeze" if on else "pay.unfreeze",
                    reason or None,
                    {"source": "cli"},
                )
            return value
    finally:
        await conn.close()


async def pay_freeze_guard(conn: AsyncConnection, user_id: int) -> str | None:
    """``PaymentCore.spend_guard`` hook: refuses a NEW invoice while frozen (one indexed SELECT).

    Webhooks, the reconciler and ``successful_payment`` of already approved Stars invoices never pass through
    a spend guard, so money in flight is still credited (06 §4.7 p.1)."""
    import sqlalchemy as sa

    value = await conn.scalar(sa.text("SELECT value FROM config_meta WHERE key = :k"), {"k": FREEZE_KEY})
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, Mapping) and value.get("on"):
        return FREEZE_TEXT
    return None


def install_pay_freeze(payment_core: Any) -> None:
    """Integration: ``install_pay_freeze(deps.payments)`` registers :func:`pay_freeze_guard`."""
    payment_core.spend_guard(pay_freeze_guard)


# -------------------------------------------------------------------------------------- export-rollback


@dataclass(slots=True)
class RollbackExport:
    since: datetime
    exported_at: datetime
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    pending_db_count: int = 0
    frozen: dict[str, Any] | None = None
    missing_tables: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def pending_matches(self) -> bool:
        return len(self.pending) == self.pending_db_count

    def as_json(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "exported_at": self.exported_at.isoformat(),
            "pay_freeze": _iso(self.frozen),
            "counts": {k: len(v) for k, v in self.sections.items()} | {"pending": len(self.pending)},
            "pending_db_count": self.pending_db_count,
            "pending_matches": self.pending_matches,
            "missing_tables": self.missing_tables,
            "sections": _iso(self.sections),
            "pending": _iso(self.pending),
        }

    def summary(self) -> list[str]:
        names = {
            "payments_paid": "оплаты",
            "wallet": "движения кошелька",
            "paid_until": "изменения сроков",
            "referral_days": "реферальные дни",
            "lte_blocks": "блоки LTE",
            "ip_guard_blocks": "блоки IP Guard",
            "new_users": "новые пользователи",
            "new_subscriptions": "новые подписки",
        }
        lines = [f"{names.get(k, k)}: {len(v)}" for k, v in self.sections.items()]
        imported = sum(1 for p in self.pending if p.get("is_imported"))
        lines.append(
            f"незакрытые счета (pending): {len(self.pending)} (импортированных из Bedolaga: {imported}); "
            f"в базе pending: {self.pending_db_count}"
        )
        return lines


_SECTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "payments_paid",
        "payments",
        """
        SELECT p.id, i.slug AS instance, p.external_id, p.merchant_ref, p.user_id, u.telegram_id,
               p.amount_minor, p.currency, p.paid_amount_minor, p.paid_currency, p.status, p.is_imported,
               p.order_id, p.created_at, p.paid_at
        FROM payments p JOIN payment_instances i ON i.id = p.instance_id
        LEFT JOIN users u ON u.id = p.user_id
        WHERE p.paid_at >= $1 AND p.status IN ('paid', 'refunded')
        ORDER BY p.paid_at, p.id
        """,
    ),
    (
        "wallet",
        "wallet_ledger",
        """
        SELECT l.id, l.user_id, u.telegram_id, l.amount_minor, l.currency, l.balance_after, l.reason,
               l.ref_type, l.ref_id, l.note, l.created_at
        FROM wallet_ledger l LEFT JOIN users u ON u.id = l.user_id
        WHERE l.created_at >= $1 AND l.reason <> 'import_opening'
        ORDER BY l.id
        """,
    ),
    (
        "paid_until",
        "subscription_events",
        """
        SELECT e.id, e.subscription_id, s.user_id, u.telegram_id, s.panel_user_id, s.panel_username, e.kind,
               e.source, e.delta_seconds, e.old_expire, e.new_expire, e.ref_type, e.ref_id, e.ts
        FROM subscription_events e JOIN subscriptions s ON s.id = e.subscription_id
        LEFT JOIN users u ON u.id = s.user_id
        WHERE e.ts >= $1 AND e.source <> 'import'
          AND (e.new_expire IS NOT NULL OR e.old_expire IS NOT NULL OR e.delta_seconds IS NOT NULL)
        ORDER BY e.id
        """,
    ),
    (
        "referral_days",
        "referral_rewards",
        """
        SELECT r.id, r.user_id, u.telegram_id, r.referred_user_id, r.side, r.kind, r.status, r.days,
               r.subscription_id, r.granted_at
        FROM referral_rewards r LEFT JOIN users u ON u.id = r.user_id
        WHERE r.status = 'granted' AND r.granted_at >= $1
        ORDER BY r.id
        """,
    ),
    (
        "lte_blocks",
        "lte_blocks",
        """
        SELECT b.id, b.subscription_id, s.panel_user_id, b.group_id, b.status, b.reason, b.mode,
               b.created_at, b.applied_at, b.released_at, b.release_reason
        FROM lte_blocks b LEFT JOIN subscriptions s ON s.id = b.subscription_id
        WHERE b.created_at >= $1 OR b.released_at >= $1
        ORDER BY b.id
        """,
    ),
    (
        "ip_guard_blocks",
        "ip_guard_blocks",
        """
        SELECT b.id, b.subscription_id, s.panel_user_id, b.user_id, b.status, b.reason, b.blocked_at,
               b.frozen_seconds, b.unblocked_at, b.new_paid_until, b.closed_at
        FROM ip_guard_blocks b LEFT JOIN subscriptions s ON s.id = b.subscription_id
        WHERE b.blocked_at >= $1 OR b.unblocked_at >= $1 OR b.closed_at >= $1
        ORDER BY b.id
        """,
    ),
    (
        "new_users",
        "users",
        """
        SELECT id, telegram_id, username, first_name, language, wallet_minor, created_at
        FROM users WHERE created_at >= $1 ORDER BY id
        """,
    ),
    (
        "new_subscriptions",
        "subscriptions",
        """
        SELECT s.id, s.user_id, u.telegram_id, s.link_state, s.panel_user_id, s.panel_username, s.paid_until,
               s.is_trial, s.created_at
        FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id
        WHERE s.created_at >= $1 ORDER BY s.id
        """,
    ),
)

_PENDING_SQL: Final = """
SELECT p.id, i.slug AS instance, p.external_id, p.merchant_ref, p.user_id, u.telegram_id, p.amount_minor,
       p.currency, p.expires_at, p.is_imported, p.order_id, p.created_at
FROM payments p JOIN payment_instances i ON i.id = p.instance_id
LEFT JOIN users u ON u.id = p.user_id
WHERE p.status = 'pending'
ORDER BY p.created_at, p.id
"""


async def export_rollback(dsn: str, since: datetime, *, out: Path | None = None) -> RollbackExport:
    """Everything the new bot changed since ``since`` (T0) + ALL pending invoices at the moment of export.

    One ``REPEATABLE READ`` snapshot, so the ``pending`` section and its control count are the same moment.
    """
    if since.tzinfo is None:
        raise ValueError("since: нужна дата с часовым поясом (например 2026-10-02T01:00:00+03:00)")
    conn = await _connect(dsn)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            result = RollbackExport(since=since.astimezone(UTC), exported_at=now())
            for name, table, sql in _SECTIONS:
                if not await _has_table(conn, table):
                    result.missing_tables.append(table)
                    result.sections[name] = []
                    continue
                result.sections[name] = [dict(r) for r in await conn.fetch(sql, since)]
            result.pending = [dict(r) for r in await conn.fetch(_PENDING_SQL)]
            result.pending_db_count = int(
                await conn.fetchval("SELECT count(*) FROM payments WHERE status = 'pending'") or 0
            )
            result.frozen = await pay_freeze_state(conn)
    finally:
        await conn.close()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(result.as_json(), ensure_ascii=False, indent=1)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # personal data: owner-only
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        result.path = out
    return result


# ------------------------------------------------------------------------------------------------ gates


@dataclass(frozen=True, slots=True)
class GateItem:
    name: str
    ok: bool
    detail: str
    manual: bool = False


_MANUAL_GATES: Final = (
    "§1.4 п.1: сценарии чек-листа §4.8 пройдены на тестовом токене против копии БД и панели (read-only)",
    "§1.4 п.2: выбран режим LTE (а) полный перенос или (б) состояние + теневой учёт — согласие владельца "
    "письменно",
    "§1.4 п.3: выбран режим IP Guard (минимум hold + разблокировка кнопкой; автоблок — по решению)",
    "§1.4 п.4: получены дампы system_settings, wlq_settings, wlq_kv без секретов и свежая копия payments.db",
    "§1.4 п.5: владелец ответил на вопросы §6 с пометкой «блокирует»",
    "§4.1 п.9: хвосты Bedolaga дожаты (IP Guard, wlq_commands/backfill pending, Pay held, подарки)",
    # Site payment is dropped (stage-4b contract): payments.db is NOT imported, the bot does not know tc_….
    "§4.5: сервис оплаты со страницы подписки (если был) остановлен вместе с Bedolaga; в payments.db нет открытых tc_ (created ≤ 48 ч, held, "
    "paid_extend_failed) — закрыты вручную; поздний вебхук по tc_ бот не зачислит (UNKNOWN_PAYMENT)",
)


def _import_gate(result: Any) -> GateItem:
    """The last shadow pass ran the importer and it had no blocking problem (conflicts, taken ids,
    undelivered gifts, missing owner modules…): С1–С12 alone cannot see those (06 §4.3 С5)."""
    name = "импорт: без блокирующих проблем"
    if not isinstance(result, Mapping) or not result:
        return GateItem(name, False, "в последнем shadow импорт не запускался")
    blocking = result.get("blocking")
    if not isinstance(blocking, Mapping):
        return GateItem(name, False, f"импорт #{result.get('run_id')}: нет итога (blocking)")
    if blocking:
        listed = ", ".join(f"{k} {v}" for k, v in sorted(blocking.items()))
        return GateItem(name, False, f"импорт #{result.get('run_id')}: {listed}")
    if not result.get("green"):
        return GateItem(name, False, f"импорт #{result.get('run_id')}: проверки импорта красные")
    return GateItem(name, True, f"импорт #{result.get('run_id')}: зелёный")


async def gate_check(
    dsn: str, *, instances: Sequence[str] = DEFAULT_INSTANCES, at: datetime | None = None
) -> list[GateItem]:
    """Automatic gates from the database of the stand + the manual ones as reminders (``manual=True``)."""
    from svbg.importers.shadow import STREAK_DAYS, green_streak, load_history, load_last

    moment = at or now()
    items: list[GateItem] = []
    conn = await _connect(dsn)
    try:
        history = await load_history(conn)
        last = await load_last(conn)
        streak = green_streak(history)
        items.append(
            GateItem(
                "shadow: зелёных дней подряд",
                streak >= STREAK_DAYS,
                f"{streak} из {STREAK_DAYS} (06 §4.2 п.4)",
            )
        )
        if last is None:
            items.append(GateItem("shadow: последний отчёт", False, "shadow ещё не запускался"))
        else:
            at_last = datetime.fromisoformat(str(last["as_of"]))
            fresh = moment - at_last <= SHADOW_MAX_AGE
            red = [c["code"] for c in last.get("checks", []) if c.get("status") != "ok"]
            ok = bool(last.get("green")) and fresh
            why = "зелёный" if last.get("green") else (last.get("blocked") or f"красные: {', '.join(red)}")
            items.append(
                GateItem(
                    "shadow: последний отчёт",
                    ok,
                    f"{at_last:%Y-%m-%d %H:%M} UTC, {why}" + ("" if fresh else ", старше 26 ч"),
                )
            )
            probe = last.get("probe") or {}
            items.append(
                GateItem(
                    "панель: shadow работал на read-only токене",
                    probe.get("verdict") == "read_only",
                    f"проба PATCH: {probe.get('status')} ({probe.get('verdict', 'нет данных')})",
                )
            )
            items.append(
                GateItem(
                    "writer: план shadow пуст",
                    int(last.get("ops_total") or 0) == 0,
                    f"операций: {last.get('ops_total')}",
                )
            )
            items.append(_import_gate(last.get("import")))
        queued = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE queue = 'panel' AND status IN ('ready', 'running')"
        )
        items.append(GateItem("очередь панели пуста", not queued, f"задач: {queued}"))
        slugs = {
            str(r[0])
            for r in await conn.fetch(
                "SELECT slug FROM payment_instances WHERE slug = ANY($1::text[])", list(instances)
            )
        }
        missing = [s for s in instances if s not in slugs]
        items.append(
            GateItem(
                "кассы заведены (можно enabled=false до T0)",
                not missing,
                "все: " + ", ".join(instances) if not missing else "нет: " + ", ".join(missing),
            )
        )
        frozen = await pay_freeze_state(conn)
        items.append(
            GateItem(
                "оплата не заморожена",
                frozen is None,
                "freeze выключен" if frozen is None else f"заморожена с {frozen.get('since')}",
            )
        )
        imported = 0
        if await _has_table(conn, "settings_audit"):
            imported = int(
                await conn.fetchval("SELECT count(*) FROM settings_audit WHERE source = 'import'") or 0
            )
        items.append(
            GateItem(
                "настройки Bedolaga импортированы", imported > 0, f"записей импорта настроек: {imported}"
            )
        )
    finally:
        await conn.close()
    items.extend(GateItem(text, False, "подтвердите вручную", manual=True) for text in _MANUAL_GATES)
    return items


# --------------------------------------------------------------------------------------------- telegram


def _bot(token: str, api_url: str | None) -> Any:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    session = AiohttpSession(api=TelegramAPIServer.from_base(api_url)) if api_url else AiohttpSession()
    return Bot(token, session=session)


def _info_json(info: Any) -> dict[str, Any]:
    err_at = getattr(info, "last_error_date", None)
    return {
        "url": info.url or "",
        "pending_update_count": int(info.pending_update_count or 0),
        "last_error_message": info.last_error_message,
        "last_error_date": err_at.isoformat() if isinstance(err_at, datetime) else err_at,
        "allowed_updates": list(info.allowed_updates or []),
    }


async def webhook_info(token: str, *, api_url: str | None = None) -> dict[str, Any]:
    bot = _bot(token, api_url)
    try:
        return _info_json(await bot.get_webhook_info())
    finally:
        await bot.session.close()


async def delete_webhook_keep_queue(
    token: str, *, api_url: str | None = None, expect_url: str | None = None
) -> dict[str, Any]:
    """``deleteWebhook(drop_pending_updates=False)`` — the update queue (up to 24 h) stays for the new bot.

    ``expect_url``: refuse when the webhook is not the one the owner confirmed (it changed meanwhile)."""
    bot = _bot(token, api_url)
    try:
        before = _info_json(await bot.get_webhook_info())
        if not before["url"]:
            return {"before": before, "after": before, "deleted": False}
        if expect_url is not None and before["url"] != expect_url:
            raise _Fail(f"webhook сейчас {before['url']}, а подтверждён {expect_url}: ничего не удалено")
        await bot.delete_webhook(drop_pending_updates=False)
        after = _info_json(await bot.get_webhook_info())
        return {"before": before, "after": after, "deleted": True}
    finally:
        await bot.session.close()


# ------------------------------------------------------------------------------------------- CLI helpers


def _boot(environ: Mapping[str, str]) -> Any:
    from svbg.core.settings import BootstrapError, read_bootstrap
    from svbg.core.settings.bootstrap import default_env_path

    try:
        return read_bootstrap(default_env_path(environ), environ)
    except BootstrapError as exc:
        raise _Fail(str(exc), EXIT_CONFIG) from None


def _dsn(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    dsn = getattr(args, "dsn", None)
    if dsn:
        return str(dsn)
    boot = _boot(environ)
    if not boot.database_url:
        raise _Fail(f"DATABASE_URL не задан: впишите его в {boot.env_path}", EXIT_CONFIG)
    return str(boot.database_url)


def _read_secret_file(path: str, what: str) -> str:
    try:
        value = Path(path).read_text("utf-8").strip()
    except OSError as exc:
        raise _Fail(f"{what}: файл не читается ({exc.strerror or type(exc).__name__})", EXIT_USAGE) from None
    if not value:
        raise _Fail(f"{what}: файл пуст", EXIT_USAGE)
    return value


def _secret(
    args: argparse.Namespace, environ: Mapping[str, str], attr: str, env: str, what: str
) -> str | None:
    path = getattr(args, attr, None)
    if path:
        return _read_secret_file(path, what)
    return environ.get(env) or None


def _env_doc_values(environ: Mapping[str, str]) -> dict[str, str]:
    from svbg.boot.envfile import EnvDocument, read_text
    from svbg.core.settings.bootstrap import default_env_path
    from svbg.core.settings.values import SECRET_PLACEHOLDER

    try:
        text = read_text(default_env_path(environ))
    except (OSError, ValueError):
        return {}
    doc = EnvDocument.parse(text or "")
    out: dict[str, str] = {}
    for key in (
        "REMNAWAVE_URL",
        "REMNAWAVE_TOKEN",
        "REMNAWAVE_CADDY_TOKEN",
        "REMNAWAVE_CF_CLIENT_ID",
        "REMNAWAVE_CF_CLIENT_SECRET",
        "REMNAWAVE_COOKIE",
        "REMNAWAVE_ALLOW_PLAIN_HTTP",
        "TELEGRAM_API_URL",
        "TIMEZONE",
    ):
        value = doc.get(key)
        if value and value != SECRET_PLACEHOLDER:
            out[key] = value
    return out


@contextlib.asynccontextmanager
async def _panel_api(args: argparse.Namespace, environ: Mapping[str, str]) -> Any:
    from svbg.remnawave.api import RemnawaveApi
    from svbg.remnawave.transport import Transport, TransportConfig

    values: dict[str, Any] = _env_doc_values(environ)
    if getattr(args, "panel_url", None):
        values["REMNAWAVE_URL"] = args.panel_url
    token = _secret(args, environ, "token_file", _ENV_PANEL_TOKEN, "токен панели")
    if token:
        values["REMNAWAVE_TOKEN"] = token
    if values.get("REMNAWAVE_ALLOW_PLAIN_HTTP"):
        values["REMNAWAVE_ALLOW_PLAIN_HTTP"] = values["REMNAWAVE_ALLOW_PLAIN_HTTP"].lower() in (
            "1",
            "true",
            "yes",
        )
    try:
        cfg = TransportConfig.from_settings(values)
    except ValueError as exc:
        raise _Fail(str(exc), EXIT_CONFIG) from None
    if cfg is None:
        raise _Fail(
            "панель не настроена: --panel-url и --token-file (или REMNAWAVE_* в data/.env)", EXIT_CONFIG
        )
    transport = Transport(cfg)
    try:
        yield RemnawaveApi(transport)
    finally:
        await transport.aclose()


def _parse_since(value: str) -> datetime:
    try:
        since = datetime.fromisoformat(value)
    except ValueError:
        raise _Fail(f"--since: не дата ISO 8601: {value!r}", EXIT_USAGE) from None
    if since.tzinfo is None:
        raise _Fail("--since: укажите часовой пояс, например 2026-10-02T01:00:00+03:00", EXIT_USAGE)
    return since


def _run(fn: Any, args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    try:
        return int(asyncio.run(fn(args, environ, io)))
    except _Fail as exc:
        io.warn(f"❌ {exc}")
        return exc.code
    except (OSError, asyncpg.PostgresError) as exc:
        io.warn(f"❌ {type(exc).__name__}: {exc}")
        return EXIT_FAIL


# ------------------------------------------------------------------------------------------- commands


async def _cmd_check(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    items = await gate_check(_dsn(args, environ))
    io.say("Ворота готовности к переключению (06 §1.4):")
    for item in items:
        mark = "☐" if item.manual else ("✅" if item.ok else "❌")
        io.say(f"{mark} {item.name} — {item.detail}")
    auto_ok = all(i.ok for i in items if not i.manual)
    io.say("Автоматические проверки: " + ("зелёные" if auto_ok else "есть красные — дату не назначать"))
    return EXIT_OK if auto_ok else EXIT_FAIL


async def _cmd_export(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    since = _parse_since(args.since)
    if args.out:
        out = Path(args.out)
    else:
        boot = _boot(environ)
        stamp = now().strftime("%Y%m%d-%H%M%S")
        out = Path(boot.env_path).parent / "rollback" / f"rollback-{stamp}.json"
    result = await export_rollback(_dsn(args, environ), since, out=out)
    io.say(f"✅ Журнал отката: {result.path}")
    for line in result.summary():
        io.say(f"  {line}")
    if result.frozen is None:
        io.warn("⚠️ Оплата не заморожена: сначала svbg pay freeze, иначе появятся новые счета (06 §4.7 п.1)")
    if result.missing_tables:
        io.warn(f"⚠️ Нет таблиц (модуль не установлен): {', '.join(result.missing_tables)}")
    if not result.pending_matches:
        io.warn("❌ Число pending в выгрузке не совпало с базой — повторите выгрузку")
        return EXIT_FAIL
    return EXIT_OK


def _bot_token(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    token = _secret(args, environ, "token_file", _ENV_BOT_TOKEN, "токен бота")
    if not token:
        raise _Fail(f"нужен токен бота: --token-file или {_ENV_BOT_TOKEN}", EXIT_USAGE)
    return token


def _api_url(args: argparse.Namespace, environ: Mapping[str, str]) -> str | None:
    return getattr(args, "api_url", None) or _env_doc_values(environ).get("TELEGRAM_API_URL")


async def _cmd_webhook_info(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    info = await webhook_info(_bot_token(args, environ), api_url=_api_url(args, environ))
    io.say(f"url: {info['url'] or '(не установлен)'}")
    io.say(f"pending_update_count: {info['pending_update_count']}  ← зафиксируйте для сверки")
    if info["last_error_message"]:
        io.say(f"last_error: {info['last_error_message']} ({info['last_error_date']})")
    if info["url"]:
        io.warn(
            "⚠️ Webhook установлен (старый бот?). Снять без потери очереди: "
            "svbg cutover delete-webhook --yes --expect-url <этот url>"
        )
    return EXIT_OK


async def _cmd_delete_webhook(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    if not args.yes:
        raise _Fail("подтвердите --yes: старый бот перестанет получать апдейты", EXIT_USAGE)
    res = await delete_webhook_keep_queue(
        _bot_token(args, environ), api_url=_api_url(args, environ), expect_url=args.expect_url
    )
    if not res["deleted"]:
        io.say("webhook не установлен — удалять нечего")
        return EXIT_OK
    io.say(f"✅ webhook {res['before']['url']} снят (drop_pending_updates=false)")
    io.say(
        f"очередь апдейтов: было {res['before']['pending_update_count']}, "
        f"сейчас {res['after']['pending_update_count']} — не сброшена"
    )
    return EXIT_OK


async def _cmd_probe(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.importers.shadow import PanelIndex, probe_token

    async with _panel_api(args, environ) as api:
        index = await PanelIndex.load(api)
        probe = await probe_token(api, max_panel_id=index.max_id)
    io.say(f"Пользователей в панели: {len(index.users)}; проба PATCH id={probe.probe_id}: {probe.text()}")
    want = args.expect
    if want == "read-only":
        ok = probe.read_only
        io.say(
            "✅ годится для shadow" if ok else "❌ для shadow нужен токен только для чтения (ожидался 403)"
        )
    else:
        ok = probe.writable
        io.say(
            "✅ боевой токен может писать (404 — норма)"
            if ok
            else "❌ у токена нет users:update (ожидался 404) — writer не сможет работать"
        )
    return EXIT_OK if ok else EXIT_FAIL


async def _cmd_shadow_run(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    from svbg.importers.shadow import ShadowConfig, ShadowService

    source = _secret(args, environ, "source_dsn_file", _ENV_SOURCE_DSN, "DSN источника")
    if not source:
        raise _Fail(f"нужен DSN копии БД Bedolaga: --source-dsn-file или {_ENV_SOURCE_DSN}", EXIT_USAGE)
    tz = _env_doc_values(environ).get("TIMEZONE") or "Europe/Moscow"
    async with _panel_api(args, environ) as api:
        service = ShadowService(
            target_dsn=_dsn(args, environ),
            source_dsn=source,
            api=lambda: api,
            config=ShadowConfig(timezone=tz),
        )
        report = await service.run("cli")
    io.say(report.summary_text())
    return EXIT_OK if report.green else EXIT_FAIL


async def _cmd_pay(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
    dsn = _dsn(args, environ)
    if args.pay_command == "status":
        conn = await _connect(dsn)
        try:
            state = await pay_freeze_state(conn)
            pending = await conn.fetchval("SELECT count(*) FROM payments WHERE status = 'pending'")
        finally:
            await conn.close()
        if state is None:
            io.say(f"оплата работает; pending-счетов: {pending}")
        else:
            io.say(f"🧊 новые счета заморожены с {state.get('since')}; pending-счетов: {pending}")
        return EXIT_OK
    on = args.pay_command == "freeze"
    await set_pay_freeze(dsn, on, reason=getattr(args, "reason", "") or "")
    if on:
        io.say(
            "🧊 Новые счета больше не выставляются. Вебхуки касс, reconciler и successful_payment работают."
        )
        io.say("Подождите ≥ 1 минуту, затем: svbg maintenance on; svbg cutover export-rollback --since <T0>")
    else:
        io.say("✅ Выставление счетов снова разрешено")
    return EXIT_OK


def _handler(fn: Any) -> Any:
    def run(args: argparse.Namespace, environ: Mapping[str, str], io: _Io) -> int:
        return _run(fn, args, environ, io)

    return run


def add_commands(sub: Any) -> None:
    """Add ``cutover`` and ``pay`` to the subparsers of ``svbg.__main__.build_parser``."""
    p_cut = sub.add_parser("cutover", help="переезд с Bedolaga: ворота, откат, webhook, проба токена")
    cut = p_cut.add_subparsers(dest="cutover_command", required=True, metavar="action")

    p = cut.add_parser("check", help="ворота готовности (06 §1.4)")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_check))

    p = cut.add_parser("export-rollback", help="журнал изменений после T0 + все pending-счета")
    p.add_argument("--since", required=True, help="T0, ISO 8601 с часовым поясом")
    p.add_argument("--out", help="файл JSON (по умолчанию data/rollback/rollback-<время>.json)")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_export))

    p = cut.add_parser("webhook-info", help="getWebhookInfo боевого токена")
    p.add_argument("--token-file", help=f"файл с токеном бота (или {_ENV_BOT_TOKEN})")
    p.add_argument("--api-url", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_webhook_info))

    p = cut.add_parser("delete-webhook", help="deleteWebhook без сброса очереди апдейтов")
    p.add_argument("--token-file", help=f"файл с токеном бота (или {_ENV_BOT_TOKEN})")
    p.add_argument("--expect-url", help="удалить, только если webhook именно этот (показан webhook-info)")
    p.add_argument("--yes", action="store_true", help="подтверждение")
    p.add_argument("--api-url", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_delete_webhook))

    p = cut.add_parser("probe-panel", help="проба токена панели: 403 = только чтение, 404 = может писать")
    p.add_argument("--panel-url", help="адрес панели (иначе REMNAWAVE_URL из data/.env)")
    p.add_argument("--token-file", help=f"файл с токеном панели (или {_ENV_PANEL_TOKEN} / data/.env)")
    p.add_argument("--expect", choices=["read-only", "write"], default="read-only")
    p.set_defaults(handler=_handler(_cmd_probe))

    p = cut.add_parser("shadow-run", help="shadow-сверка С1–С12 сейчас (без записи в панель)")
    p.add_argument("--source-dsn-file", help=f"файл с DSN копии БД Bedolaga (или {_ENV_SOURCE_DSN})")
    p.add_argument("--panel-url", help="адрес панели (иначе REMNAWAVE_URL из data/.env)")
    p.add_argument("--token-file", help="файл с read-only токеном панели")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_shadow_run))

    p_pay = sub.add_parser("pay", help="заморозка выставления новых счетов (откат, 06 §4.7)")
    pay = p_pay.add_subparsers(dest="pay_command", required=True, metavar="action")
    p = pay.add_parser("freeze", help="не выставлять новые счета")
    p.add_argument("--reason", default="rollback", help="причина (в журнал)")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_pay))
    p = pay.add_parser("unfreeze", help="снова выставлять счета")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_pay))
    p = pay.add_parser("status", help="состояние заморозки и число pending")
    p.add_argument("--dsn", help=argparse.SUPPRESS)
    p.set_defaults(handler=_handler(_cmd_pay))
