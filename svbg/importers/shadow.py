"""Shadow mode of the Bedolaga migration (06 §4.2–4.3, 02 §7.4 p.3): read, compare, never write.

What a shadow pass does (daily on the stand, and once more at T−5 min of the runbook, 06 §4.4 step 3):

1. **Token probe** (06 §4.1 p.3): ``PATCH /api/users {"id": MAX(id) + 10⁹}`` with no other field. A read-only
   token gets **403** (the scope guard runs before the user lookup) — the only answer that lets shadow run.
   **404** means the token can write: the pass stops, «Требует внимания» says «введён полный токен», nothing
   else is called. The probe never names a real panel user. The same probe checks the full token at T0, where
   404 is the norm (:attr:`TokenProbe.writable`).
2. The importer runs in ``shadow`` mode against a fresh source (an injected port: the importer owns the data
   mapping; this module only reconciles).
3. **Reconciliation С1–С12** (06 §4.3) between the source (Bedolaga PostgreSQL, its own read-only
   connection: ``default_transaction_read_only=on`` + a ``REPEATABLE READ READ ONLY`` snapshot), the target
   (our database, also a read-only snapshot) and the panel (one ``users/stream`` pass with the read-only
   token). С6 is «what the writer WOULD write»: every field the writer manages (``expireAt``, ``status``,
   ``activeInternalSquads`` after module substitutions, ``hwidDeviceLimit``, ``trafficLimitBytes``,
   ``trafficLimitStrategy``, ``tag``, ``externalSquadUuid``) is compared with the panel; the target is an
   empty list. Nothing is enqueued and no mutating panel method is ever called from here.
4. The report is kept in ``config_meta`` (``shadow.last`` + a short ``shadow.history`` for the «3 green days
   in a row» exit criterion, 06 §4.2 p.4) and a summary goes to the «⚙️ Система» topic (the owner's test
   chat on the stand).

Every check is ``ok`` or ``fail`` (or ``error`` if it could not run); a check with nothing to compare (no LTE
blocks in the source, say) is ``ok`` with a note. A target table that is missing while the source has data
is a failure: the owner module is not installed on the stand.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final, Protocol

import asyncpg

from svbg.core.clock import now
from svbg.remnawave.contributors import Substitution, forward, same_squads
from svbg.remnawave.errors import ErrorKind, RemnawaveError

if TYPE_CHECKING:
    from svbg.remnawave.api import RemnawaveApi
    from svbg.remnawave.models import PanelUser

__all__ = [
    "CHECK_CODES",
    "HISTORY_KEY",
    "LAST_KEY",
    "PROBE_OFFSET",
    "Check",
    "PanelIndex",
    "Reconciliation",
    "ShadowConfig",
    "ShadowReport",
    "ShadowService",
    "TokenProbe",
    "deeplink_resolver",
    "green_streak",
    "import_verdict",
    "load_history",
    "load_last",
    "open_readonly",
    "probe_token",
    "ref_payload",
    "save_report",
]

log = logging.getLogger("svbg.importers.shadow")

CHECK_CODES: Final = tuple(f"C{i}" for i in range(1, 13))
#: The probe id is far above any real panel id (06 §4.1 p.3: ``MAX(id)`` from ``stream`` + 10⁹).
PROBE_OFFSET: Final = 10**9
LAST_KEY: Final = "shadow.last"
HISTORY_KEY: Final = "shadow.history"
HISTORY_MAX: Final = 60
SAMPLE: Final = 20  # problem lines kept per check
OPS_SAMPLE: Final = 50  # writer operations kept in the stored report
STREAK_DAYS: Final = 3
EXPIRE_TOLERANCE: Final = timedelta(seconds=1)  # same as the projection
PAID_UNTIL_TOLERANCE: Final = timedelta(minutes=5)  # 06 §2.2 «расхождение больше 5 мин»
LTE_TOLERANCE_BYTES: Final = 50_000_000  # 06 §4.3 С7: ±1 % or ±50 MB
RP_LATE_WINDOW: Final = timedelta(hours=48)  # 06 §2.4.3: RollyPay «expired» may still turn «paid»
CB_LIVE_WINDOW: Final = timedelta(hours=24)  # CryptoBot invoice lifetime (env:242)
REF_DEFER_WINDOW: Final = timedelta(hours=168)
REF_LIMIT_WINDOW: Final = timedelta(days=30)
NOTIFY_PAST: Final = timedelta(days=3)  # 06 §2.10: sent_notifications of subs with end_date > T0 − 3 d
NOTIFY_AHEAD: Final = timedelta(days=7)  # С10: «истекающих в ближайшие 7 дней»
WHITELIST_KEY: Final = "IP_GUARD_WHITELIST_PANEL_USER_IDS"
_SPLIT_RE: Final = re.compile(r"[\s,;]+")

_TITLES: Final = {
    "C1": "Число строк по сущностям",
    "C2": "Кошельки: Σ и поштучно",
    "C3": "Оплаты paid по кассам",
    "C4": "Незакрытые счета → pending с заказом",
    "C5": "Связь подписок с панелью",
    "C6": "Что записал бы writer",
    "C7": "LTE: блоки и расход",
    "C8": "IP Guard: блоки, заморозка, белый список",
    "C9": "Рефералка: лимит 30 дней и deferred",
    "C10": "notification_log засеян",
    "C11": "Deep-link'и",
    "C12": "trial_used у всех со строкой subscriptions",
    "C0": "Импорт: без блокирующих проблем",
}

#: Writer-managed fields → ``overrides`` key (02 §6.3, same names as ``svbg.remnawave.writer``).
_OVERRIDE_KEY: Final = {
    "expire": "expire",
    "status": "status",
    "squads": "squads",
    "device_limit": "device_limit",
    "traffic": "traffic_bytes",
    "strategy": "reset_strategy",
    "tag": "tag",
    "ext_squad": "ext_squad",
}


# ---------------------------------------------------------------------------------------------- config


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """What the reconciliation needs to know about this installation (all have sane defaults)."""

    #: Bedolaga desk → our ``payment_instances.slug`` (06 §2.4.2).
    instances: Mapping[str, str] = field(
        default_factory=lambda: {
            "rollypay": "rollypay",
            "cryptobot": "cryptobot",
            "platega": "platega_legacy",
            "stars": "stars",
        }
    )
    #: Source ids excluded by the owner's import rules (``import_overrides: skip``), per entity.
    exclude: Mapping[str, frozenset[int]] = field(default_factory=dict)
    #: ``IP_GUARD_WHITELIST_PANEL_USER_IDS`` from the Bedolaga ``.env``; ``None`` → ``system_settings``.
    ip_guard_whitelist: str | None = None
    ref_sample: int = 20
    timezone: str = "Europe/Moscow"
    #: Optional deep-link resolver of the stand (С11): ``code → kind`` (``"ad"``, ``"ref"``) or ``None``.
    resolve_link: Callable[[str], Awaitable[str | None]] | None = None

    def excluded(self, entity: str) -> frozenset[int]:
        return frozenset(self.exclude.get(entity, ()))


# ----------------------------------------------------------------------------------------- token probe


@dataclass(frozen=True, slots=True)
class TokenProbe:
    """Result of the harmless write probe (06 §4.1 p.3)."""

    verdict: str  # read_only | writable | auth | unknown
    status: int | None
    code: str | None
    probe_id: int
    at: datetime
    detail: str = ""

    @property
    def read_only(self) -> bool:
        return self.verdict == "read_only"

    @property
    def writable(self) -> bool:
        return self.verdict == "writable"

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "status": self.status,
            "code": self.code,
            "probe_id": self.probe_id,
            "at": self.at.isoformat(),
            "detail": self.detail,
        }

    def text(self) -> str:
        if self.read_only:
            return "токен панели только для чтения (PATCH → 403)"
        if self.writable:
            return "токен панели может писать (PATCH → 404)"
        if self.verdict == "auth":
            return "панель отклонила токен (401)"
        return f"проба токена не дала ответа: {self.detail or self.status}"


async def probe_token(api: RemnawaveApi, *, max_panel_id: int) -> TokenProbe:
    """``PATCH /api/users {"id": max_panel_id + 10⁹}`` — nothing else in the body, never a real user id.

    403 → ``read_only`` (shadow may run); 404 → ``writable`` (normal for the full token at T0, a stop
    for shadow); 401 → ``auth``; anything else → ``unknown`` (treated as «not proven read-only»).
    """
    from svbg.remnawave.transport import Lane

    probe_id = max(int(max_panel_id), 0) + PROBE_OFFSET
    body = json.dumps({"id": probe_id}).encode()
    at = now()
    try:
        raw = await api.transport.request(
            "PATCH",
            "/users",
            json_body=body,
            idempotent=False,
            user_scoped=True,
            scope="users:update",
            lane=Lane.INTERACTIVE,
        )
    except RemnawaveError as err:
        if err.status == 403 and err.kind is ErrorKind.FORBIDDEN_SCOPE:
            return TokenProbe("read_only", 403, err.code, probe_id, at)
        if err.kind is ErrorKind.NOT_FOUND or err.status == 404:
            return TokenProbe("writable", err.status, err.code, probe_id, at)
        if err.kind is ErrorKind.AUTH:
            return TokenProbe("auth", err.status, err.code, probe_id, at, err.message)
        return TokenProbe("unknown", err.status, err.code, probe_id, at, err.message)
    # 2xx: the panel accepted a PATCH of a user that cannot exist — certainly not a read-only token.
    return TokenProbe("writable", getattr(raw, "status", 200), None, probe_id, at, "PATCH принят панелью")


# ------------------------------------------------------------------------------------------ panel index


@dataclass(slots=True)
class PanelIndex:
    """All panel users from one ``users/stream`` pass (06 §2.2 p.1): ``id → user``."""

    users: dict[int, PanelUser] = field(default_factory=dict)

    @classmethod
    async def load(cls, api: RemnawaveApi, *, size: int = 500) -> PanelIndex:
        index = cls()
        async for page in api.iter_users(size):
            for user in page.users:
                index.users[int(user.id)] = user
        return index

    @property
    def max_id(self) -> int:
        return max(self.users, default=0)

    def get(self, panel_user_id: int | None) -> PanelUser | None:
        return None if panel_user_id is None else self.users.get(int(panel_user_id))


# ----------------------------------------------------------------------------------------------- report


@dataclass(slots=True)
class Check:
    code: str
    title: str = ""
    ok: bool = True
    error: str | None = None
    summary: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    problem_count: int = 0

    def __post_init__(self) -> None:
        if not self.title:
            self.title = _TITLES.get(self.code, self.code)

    def fail(self, text: str) -> None:
        self.ok = False
        self.problem_count += 1
        if len(self.problems) < SAMPLE:
            self.problems.append(text)

    @property
    def status(self) -> str:
        if self.error is not None:
            return "error"
        return "ok" if self.ok else "fail"

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "facts": _jsonable(self.facts),
            "problems": list(self.problems),
            "problem_count": self.problem_count,
        }


@dataclass(slots=True)
class ShadowReport:
    as_of: datetime
    started_at: datetime
    finished_at: datetime | None = None
    trigger: str = "manual"
    probe: TokenProbe | None = None
    import_result: dict[str, Any] | None = None
    checks: list[Check] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    ops_total: int = 0
    blocked: str | None = None
    streak: int = 0

    @property
    def green(self) -> bool:
        codes = {c.code for c in self.checks}
        return (
            self.blocked is None and codes >= set(CHECK_CODES) and all(c.status == "ok" for c in self.checks)
        )

    @property
    def red_codes(self) -> list[str]:
        return [c.code for c in self.checks if c.status != "ok"]

    def check(self, code: str) -> Check | None:
        return next((c for c in self.checks if c.code == code), None)

    def as_json(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "trigger": self.trigger,
            "green": self.green,
            "blocked": self.blocked,
            "probe": self.probe.as_json() if self.probe else None,
            "import": _jsonable(self.import_result) if self.import_result is not None else None,
            "checks": [c.as_json() for c in self.checks],
            "ops_total": self.ops_total,
            "ops": _jsonable(self.ops[:OPS_SAMPLE]),
            "streak": self.streak,
        }

    def summary_text(self, tz: tzinfo | None = None) -> str:
        zone = tz or UTC
        at = self.as_of.astimezone(zone).strftime("%d.%m %H:%M")
        lines = [f"🌓 Shadow-сверка Bedolaga · {at}"]
        if self.blocked:
            lines.append(f"⛔ Не выполнена: {self.blocked}")
            return "\n".join(lines)
        if self.green:
            lines.append(
                f"✅ Все проверки С1–С12 зелёные · зелёных дней подряд: {self.streak} из {STREAK_DAYS}"
            )
        else:
            red = ", ".join(c.replace("C", "С") for c in self.red_codes)
            lines.append(f"❌ Красные: {red}")
        for c in self.checks:
            mark = {"ok": "✅", "fail": "❌", "error": "⚠️"}[c.status]
            text = c.error if c.error else c.summary
            lines.append(f"{c.code.replace('C', 'С')} {mark} {c.title}: {text}"[:300])
            if c.status != "ok":
                lines.extend(f"   · {p}"[:200] for p in c.problems[:3])
        lines.append(f"Writer: {self.ops_total} операций (цель — 0); в панель ничего не записано")
        return "\n".join(lines)[:4000]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(v) for v in items]
    return value


# ------------------------------------------------------------------------------------------- database


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: v if isinstance(v, str) else json.dumps(v),
            decoder=json.loads,
            schema="pg_catalog",
        )


@contextlib.asynccontextmanager
async def open_readonly(dsn: str, *, name: str = "svbg-shadow") -> Any:
    """A connection that cannot write (server-side default) inside one ``REPEATABLE READ READ ONLY``
    snapshot: every query of the pass sees the same moment of the database."""
    conn = await asyncpg.connect(
        dsn,
        server_settings={"default_transaction_read_only": "on", "application_name": name},
    )
    try:
        await _init_conn(conn)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            yield conn
    finally:
        await conn.close()


def _pg_dsn(dsn: str) -> str:
    from svbg.db.engine import normalize_dsn

    return normalize_dsn(dsn)[1]


async def save_report(conn: asyncpg.Connection, report: ShadowReport, tz: tzinfo) -> None:
    """``shadow.last`` = the report, ``shadow.history`` += one line (kept short)."""
    await _init_conn(conn)
    payload = report.as_json()
    day = report.as_of.astimezone(tz).date().isoformat()
    entry = {
        "date": day,
        "at": report.as_of.isoformat(),
        "green": report.green,
        "red": report.red_codes,
        "blocked": report.blocked,
        "ops": report.ops_total,
        "trigger": report.trigger,
    }
    async with conn.transaction():
        rows = await conn.fetch("SELECT value FROM config_meta WHERE key = $1 FOR UPDATE", HISTORY_KEY)
        history = list(rows[0]["value"] or []) if rows else []
        history.append(entry)
        history = history[-HISTORY_MAX:]
        upsert = (
            "INSERT INTO config_meta (key, value, updated_at) VALUES ($1, $2::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        )
        await conn.execute(upsert, LAST_KEY, payload)
        await conn.execute(upsert, HISTORY_KEY, history)


async def load_history(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    await _init_conn(conn)
    value = await conn.fetchval("SELECT value FROM config_meta WHERE key = $1", HISTORY_KEY)
    return list(value or [])


async def load_last(conn: asyncpg.Connection) -> dict[str, Any] | None:
    await _init_conn(conn)
    value = await conn.fetchval("SELECT value FROM config_meta WHERE key = $1", LAST_KEY)
    return dict(value) if value else None


def green_streak(history: Sequence[Mapping[str, Any]]) -> int:
    """Consecutive calendar days (ending with the latest day that has a run) on which every run was green."""
    days: dict[str, bool] = {}
    for entry in history:
        day = str(entry.get("date") or "")
        if not day:
            continue
        days[day] = days.get(day, True) and bool(entry.get("green"))
    if not days:
        return 0
    streak = 0
    cursor = max(date.fromisoformat(d) for d in days)
    while days.get(cursor.isoformat()):
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# -------------------------------------------------------------------------------------- reconciliation


def _fmt(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if value is not None else "—"


def _close(a: datetime | None, b: datetime | None, tol: timedelta) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def _epoch_anchor(value: datetime | None) -> str:
    return "0" if value is None else str(int(value.timestamp()))


def _notify_base(kind: str) -> str:
    """Bedolaga ``sent_notifications.notification_type`` → our base kind (``svbg.services.notify_user``)."""
    k = kind.lower()
    if "trial" in k:
        return "trial_ending"
    if "expired" in k:
        return "expired"
    return "expiring"


def _target_base(kind: str) -> str:
    return "expiring" if kind.startswith("expiring_") else kind


def _frozen_seconds(row: Mapping[str, Any]) -> int:
    """06 §2.8 / 05 §2.2.4: ``(0 if zeroed else max(0, E0 − blocked_at)) + credited_seconds``."""
    credited = int(row["credited_seconds"] or 0)
    if row["zeroed_during_block"]:
        return credited
    e0 = row["end_date_at_block"] if row["owner_kind"] == "bot_sub" else row["panel_expire_at_block"]
    if e0 is None:
        return credited
    return max(0, int((e0 - row["blocked_at"]).total_seconds())) + credited


def _parse_whitelist(raw: str | None) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    bad: list[str] = []
    for token in _SPLIT_RE.split(raw or ""):
        if not token:
            continue
        if token.isdigit() and int(token) > 0:
            ids.append(int(token))
        else:
            bad.append(token)
    return ids, bad


@dataclass(frozen=True, slots=True)
class _Desk:
    name: str
    table: str
    key: str
    target_key: str
    paid: str
    amount: str
    #: Rows the importer can carry over at all (``payments.amount_minor > 0``); the rest is its report.
    carried: str = "true"


_DESKS: Final = (
    _Desk(
        "rollypay",
        "rollypay_payments",
        "order_id",
        "merchant_ref",
        "x.is_paid",
        "x.amount_kopeks",
        "coalesce(x.amount_kopeks, 0) > 0",
    ),
    #: CryptoBot: the credited kopeks come from the linked ``transactions`` row (06 §2.1); without one the
    #: amount is in the asset — counted, but left out of the ruble sums.
    _Desk(
        "cryptobot", "cryptobot_payments", "invoice_id", "external_id", "x.status = 'paid'", "t.amount_kopeks"
    ),
    _Desk(
        "platega",
        "platega_payments",
        "correlation_id",
        "external_id",
        "coalesce(x.is_paid, false)",
        "x.amount_kopeks",
        "coalesce(x.amount_kopeks, 0) > 0",
    ),
)


def ref_payload(code: str) -> str:
    """The ``start`` payload of an old Bedolaga referral link: ``ref<code>`` (06 M7); a code already stored
    with the prefix is the payload itself."""
    return code if code.lower().startswith("ref") else f"ref{code}"


def deeplink_resolver(
    find_ad: Callable[[str], Awaitable[Any]],
    find_referrer: Callable[[str], Awaitable[Any]] | None = None,
) -> Callable[[str], Awaitable[str | None]]:
    """С11 resolver over the stand's real router rules (:mod:`svbg.deeplinks`): an exact ``ad_links`` match
    first, then ``ref…`` / prefixes. ``find_ad`` — ``DeepLinkService.find_ad``; ``find_referrer(code)`` —
    the owner of a referral code (``None`` → unknown). Returns ``"ad"``, ``"ref"``, another kind or ``None``.
    """
    from svbg.deeplinks import codec

    async def resolve(payload: str) -> str | None:
        if await find_ad(payload) is not None:
            return "ad"
        parsed = codec.parse(payload)
        if parsed is None:
            return None
        if parsed.kind in (codec.Kind.REF, codec.Kind.LEGACY_REF):
            if find_referrer is None:
                return "ref"
            codes = [parsed.raw, parsed.value] if parsed.kind is codec.Kind.LEGACY_REF else [parsed.value]
            for code in codes:
                if await find_referrer(code) is not None:
                    return "ref"
            return None
        return str(parsed.kind.value)

    return resolve


#: Desk tables whose rows keep a ``deleted`` user in the import (same list as the importer).
_PAYER_TABLES: Final = ("rollypay_payments", "cryptobot_payments", "platega_payments")


class Reconciliation:
    """С1–С12 over one source snapshot, one target snapshot and one panel index (06 §4.3)."""

    def __init__(
        self,
        src: asyncpg.Connection,
        dst: asyncpg.Connection,
        panel: PanelIndex | None,
        *,
        as_of: datetime,
        config: ShadowConfig | None = None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.panel = panel
        self.as_of = as_of
        self.cfg = config or ShadowConfig()
        self.ops: list[dict[str, Any]] = []
        self._tables: dict[tuple[str, str], bool] = {}
        self._user_ids: list[int] | None = None
        self._sub_ids: list[int] | None = None
        self._left_out: dict[str, int] = {}

    # ------------------------------------------------------------------------------------- plumbing

    async def _has(self, side: str, table: str) -> bool:
        key = (side, table)
        if key not in self._tables:
            conn = self.src if side == "src" else self.dst
            self._tables[key] = bool(
                await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{table}")
            )
        return self._tables[key]

    async def _need(self, check: Check, side: str, *tables: str) -> bool:
        missing = [t for t in tables if not await self._has(side, t)]
        if missing:
            where = "источнике" if side == "src" else "базе бота"
            check.fail(f"в {where} нет таблиц: {', '.join(missing)}")
        return not missing

    async def expected_user_ids(self) -> list[int]:
        """Users the importer must carry over (06 §2.3, «money first»): a ``deleted`` user is left out only
        without a subscription, without a balance and without a single row in a payment desk table."""
        if self._user_ids is None:
            payers = [
                f"NOT EXISTS (SELECT 1 FROM {t} x WHERE x.user_id = u.id)"
                for t in _PAYER_TABLES
                if await self._has("src", t)
            ]
            no_money = " AND ".join(["coalesce(u.balance_kopeks, 0) = 0", *payers])
            rows = await self.src.fetch(
                f"""
                SELECT u.id FROM users u
                WHERE NOT (lower(coalesce(u.status, '')) = 'deleted'
                           AND {no_money}
                           AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = u.id))
                ORDER BY u.id
                """
            )
            skip = self.cfg.excluded("users")
            self._user_ids = [int(r["id"]) for r in rows if int(r["id"]) not in skip]
        return self._user_ids

    async def expected_sub_ids(self) -> list[int]:
        if self._sub_ids is None:
            rows = await self.src.fetch("SELECT id FROM subscriptions ORDER BY id")
            skip = self.cfg.excluded("subscriptions")
            self._sub_ids = [int(r["id"]) for r in rows if int(r["id"]) not in skip]
        return self._sub_ids

    async def run(self) -> list[Check]:
        steps: list[tuple[str, Callable[[Check], Awaitable[None]]]] = [
            ("C1", self.c1_counts),
            ("C2", self.c2_wallets),
            ("C3", self.c3_paid),
            ("C4", self.c4_open_invoices),
            ("C5", self.c5_linking),
            ("C6", self.c6_writer_plan),
            ("C7", self.c7_lte),
            ("C8", self.c8_ip_guard),
            ("C9", self.c9_referral),
            ("C10", self.c10_notifications),
            ("C11", self.c11_deeplinks),
            ("C12", self.c12_trial),
        ]
        checks: list[Check] = []
        for code, fn in steps:
            check = Check(code)
            try:
                async with self.src.transaction(), self.dst.transaction():  # savepoints: one failure ≠ all
                    await fn(check)
            except Exception as exc:  # a broken check is reported, the others still run
                log.exception("shadow check %s failed", code)
                check.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            checks.append(check)
        return checks

    # ------------------------------------------------------------------------------------------- С1

    async def c1_counts(self, check: Check) -> None:
        parts: list[str] = []
        users = await self.expected_user_ids()
        found = {
            int(r["id"])
            for r in await self.dst.fetch("SELECT id FROM users WHERE id = ANY($1::bigint[])", users)
        }
        self._compare_sets(check, "пользователи", set(users), found, parts)

        subs = await self.expected_sub_ids()
        found = {
            int(r["id"])
            for r in await self.dst.fetch("SELECT id FROM subscriptions WHERE id = ANY($1::bigint[])", subs)
        }
        self._compare_sets(check, "подписки", set(subs), found, parts)

        for desk in _DESKS:
            if not await self._has("src", desk.table):
                continue
            rows = await self._desk_rows(desk, paid_only=False)
            src_keys = {k for k, _ in rows}
            dst_keys = await self._target_payment_keys(desk.name, desk.target_key)
            if dst_keys is None:
                if src_keys:
                    check.fail(f"оплаты {desk.name}: нет кассы «{self.cfg.instances.get(desk.name)}» в боте")
                continue
            self._compare_sets(check, f"оплаты {desk.name}", src_keys, dst_keys, parts)

        await self._c1_promo(check, parts)
        await self._c1_ads(check, parts)
        await self._c1_referrals(check, parts, set(users))
        check.summary = "; ".join(parts)

    def _compare_sets(self, check: Check, name: str, src: set[Any], dst: set[Any], parts: list[str]) -> None:
        missing = sorted(src - dst, key=str)
        check.facts[name] = {"source": len(src), "target": len(src & dst), "missing": len(missing)}
        parts.append(f"{name} {len(src & dst)}/{len(src)}")
        if missing:
            check.fail(f"{name}: нет в боте {len(missing)}: {', '.join(map(str, missing[:10]))}")

    async def _target_payment_keys(self, desk: str, column: str) -> set[str] | None:
        slug = self.cfg.instances.get(desk)
        if slug is None or not await self._has("dst", "payments"):
            return None
        exists = await self.dst.fetchval("SELECT 1 FROM payment_instances WHERE slug = $1", slug)
        if not exists:
            return None
        rows = await self.dst.fetch(
            f"SELECT p.{column} AS k FROM payments p JOIN payment_instances i ON i.id = p.instance_id "
            f"WHERE i.slug = $1 AND p.is_imported AND p.{column} IS NOT NULL",
            slug,
        )
        return {str(r["k"]) for r in rows}

    async def _c1_promo(self, check: Check, parts: list[str]) -> None:
        """Codes and uses equal the source minus what the importer reports as left out (``promo_group``
        codes, a code the owner already took; uses of those codes and of users not carried over)."""
        if not await self._has("src", "promocodes"):
            return
        skip_codes = self.cfg.excluded("promocodes")
        codes_by_id = {
            int(r["id"]): str(r["code"]) for r in await self.src.fetch("SELECT id, code FROM promocodes")
        }
        src_codes = {code for pid, code in codes_by_id.items() if pid not in skip_codes}
        if not codes_by_id:
            return
        if not await self._need(check, "dst", "promocodes", "promo_uses"):
            return
        dst_codes = {
            str(r[0]) for r in await self.dst.fetch("SELECT code FROM promocodes WHERE source = 'import'")
        }
        self._compare_sets(check, "промокоды", src_codes, dst_codes, parts)
        if skip_codes & set(codes_by_id):
            check.facts["промокоды"]["excluded"] = len(skip_codes & set(codes_by_id))
        users = set(await self.expected_user_ids())
        skip_uses = self.cfg.excluded("promocode_uses")
        src_uses: Counter[str] = Counter()
        for r in await self.src.fetch("SELECT id, promocode_id, user_id FROM promocode_uses"):
            pid = int(r["promocode_id"])
            if (
                int(r["id"]) in skip_uses
                or pid in skip_codes
                or pid not in codes_by_id
                or r["user_id"] is None
                or int(r["user_id"]) not in users
            ):
                continue
            src_uses[codes_by_id[pid]] += 1
        dst_uses = {
            str(r["code"]): int(r["n"])
            for r in await self.dst.fetch(
                "SELECT p.code, count(*) AS n FROM promo_uses u JOIN promocodes p ON p.id = u.promo_id "
                "WHERE u.source = 'import' GROUP BY p.code"
            )
        }
        total_src, total_dst = sum(src_uses.values()), sum(dst_uses.get(c, 0) for c in src_uses)
        check.facts["использования промокодов"] = {"source": total_src, "target": total_dst}
        parts.append(f"использования {total_dst}/{total_src}")
        for code in sorted(set(src_uses) | (set(dst_uses) & src_codes)):
            if dst_uses.get(code, 0) != src_uses.get(code, 0):
                check.fail(
                    f"использования «{code}»: источник {src_uses.get(code, 0)}, бот {dst_uses.get(code, 0)}"
                )

    async def _c1_ads(self, check: Check, parts: list[str]) -> None:
        """Campaigns and registrations per campaign, counted like the importer: the first registration of a
        user (first touch), else his ``pending_campaign_slug``; in the bot only the imported attachments
        (``source='import'``) — test clicks on the stand are not registrations of the source."""
        if not await self._has("src", "advertising_campaigns"):
            return
        skip = self.cfg.excluded("campaigns")
        rows = await self.src.fetch("SELECT id, start_parameter FROM advertising_campaigns")
        camps = {str(r["start_parameter"]) for r in rows if int(r["id"]) not in skip}
        if not rows:
            return
        if not await self._need(check, "dst", "ad_links", "ad_link_users"):
            return
        codes = {str(r[0]) for r in await self.dst.fetch("SELECT code FROM ad_links WHERE source = 'import'")}
        self._compare_sets(check, "кампании", camps, codes, parts)
        users = set(await self.expected_user_ids())
        first: dict[int, str] = {}
        if await self._has("src", "advertising_campaign_registrations"):
            for r in await self.src.fetch(
                """
                SELECT r.user_id, c.id AS campaign_id, c.start_parameter
                FROM advertising_campaign_registrations r JOIN advertising_campaigns c ON c.id = r.campaign_id
                ORDER BY r.user_id, r.created_at NULLS LAST, r.id
                """
            ):
                if int(r["campaign_id"]) not in skip:
                    first.setdefault(int(r["user_id"]), str(r["start_parameter"]))
        pending = 0
        for r in await self.src.fetch(
            "SELECT id, pending_campaign_slug FROM users WHERE pending_campaign_slug IS NOT NULL "
            "AND pending_campaign_slug <> ''"
        ):
            uid = int(r["id"])
            if uid not in first and str(r["pending_campaign_slug"]) in camps:
                first[uid] = str(r["pending_campaign_slug"])
                pending += 1
        src_counts = Counter(code for uid, code in first.items() if uid in users)
        dst_counts = {
            str(r["code"]): int(r["n"])
            for r in await self.dst.fetch(
                "SELECT l.code, count(*) AS n FROM ad_link_users u JOIN ad_links l ON l.id = u.ad_link_id "
                "WHERE u.source = 'import' GROUP BY l.code"
            )
        }
        parts.append(
            f"регистрации {sum(dst_counts.get(c, 0) for c in src_counts)}/{sum(src_counts.values())}"
        )
        check.facts["регистрации"] = {
            "source": dict(src_counts),
            "target": dst_counts,
            "pending_slug": pending,
        }
        for code in sorted(set(src_counts) | (set(dst_counts) & camps)):
            if dst_counts.get(code, 0) != src_counts.get(code, 0):
                check.fail(
                    f"регистрации «{code}»: источник {src_counts.get(code, 0)} (first-touch), "
                    f"бот {dst_counts.get(code, 0)}"
                )

    async def _c1_referrals(self, check: Check, parts: list[str], users: set[int]) -> None:
        pairs = {
            (int(r["id"]), int(r["referred_by_id"]))
            for r in await self.src.fetch(
                "SELECT id, referred_by_id FROM users WHERE referred_by_id IS NOT NULL AND referred_by_id <> "
                "id"
            )
            if int(r["id"]) in users and int(r["referred_by_id"]) in users
        }
        if not pairs:
            return
        if not await self._need(check, "dst", "referrals"):
            return
        found = {
            (int(r[0]), int(r[1]))
            for r in await self.dst.fetch("SELECT referred_user_id, referrer_id FROM referrals")
        }
        self._compare_sets(check, "реферальные связи", pairs, found, parts)

    # ------------------------------------------------------------------------------------------- С2

    async def _imported_user_ids(self) -> list[int]:
        """Every user an import ever carried over (``legacy_id_map``), whether this source still has him."""
        if not await self._has("dst", "legacy_id_map"):
            return []
        return [
            int(r[0])
            for r in await self.dst.fetch(
                "SELECT old_id FROM legacy_id_map WHERE source = 'bedolaga' AND entity = 'user' "
                "AND old_id ~ '^[0-9]+$'"
            )
        ]

    async def c2_wallets(self, check: Check) -> None:
        """Σ and per user: ``wallet_minor`` = ``balance_kopeks``; the import's own entries (one
        ``import_opening`` at most + ``import_adjust`` deltas of the later runs) sum to the balance — an
        opening left from a day the user still had money is fine once the adjustments brought it to 0.
        Users imported earlier but no longer carried over (``legacy_id_map``) must sum to their source
        balance (0 when gone): a stale opening is money nobody reconciles."""
        users = await self.expected_user_ids()
        expected = set(users)
        orphans = [uid for uid in await self._imported_user_ids() if uid not in expected]
        ids = sorted(expected | set(orphans))
        src = {
            int(r["id"]): int(r["b"])
            for r in await self.src.fetch(
                "SELECT id, coalesce(balance_kopeks, 0) AS b FROM users WHERE id = ANY($1::int[])", ids
            )
        }
        dst_rows = await self.dst.fetch(
            """
            SELECT u.id, u.wallet_minor,
                   coalesce((SELECT sum(l.amount_minor) FROM wallet_ledger l WHERE l.user_id = u.id), 0)
                       AS ledger,
                   coalesce((SELECT sum(l.amount_minor) FROM wallet_ledger l
                             WHERE l.user_id = u.id AND l.reason IN ('import_opening', 'import_adjust')
                               AND l.ref_type = 'import_run'), 0) AS imported,
                   (SELECT count(*) FROM wallet_ledger l
                     WHERE l.user_id = u.id AND l.reason = 'import_opening') AS openings
            FROM users u WHERE u.id = ANY($1::bigint[])
            """,
            ids,
        )
        dst = {int(r["id"]): r for r in dst_rows}
        sum_src = sum(b for uid, b in src.items() if uid in expected)
        sum_dst = sum(int(r["wallet_minor"]) for r in dst_rows if int(r["id"]) in expected)
        check.facts.update(
            {"sum_source": sum_src, "sum_target": sum_dst, "users": len(expected), "orphans": len(orphans)}
        )
        negative = sorted(uid for uid, b in src.items() if b < 0)
        for uid in negative:
            check.fail(f"пользователь {uid}: отрицательный баланс в источнике {src[uid]} — ручное решение")
        mismatches = 0
        for uid in ids:
            balance = src.get(uid, 0)
            row = dst.get(uid)
            if row is None:
                if balance and uid in expected:
                    check.fail(f"пользователь {uid}: баланс {balance} не перенесён (нет пользователя)")
                    mismatches += 1
                continue
            wallet, imported = int(row["wallet_minor"]), int(row["imported"])
            gone = "" if uid in expected else " (больше не переносится)"
            if uid not in negative and (wallet != max(balance, 0) or imported != max(balance, 0)):
                mismatches += 1
                check.fail(
                    f"пользователь {uid}{gone}: источник {balance}, кошелёк {wallet}, "
                    f"записи импорта {imported}"
                )
            if int(row["ledger"]) != wallet:
                check.fail(f"пользователь {uid}: Σ wallet_ledger {row['ledger']} ≠ wallet_minor {wallet}")
            if int(row["openings"]) > 1:
                check.fail(f"пользователь {uid}: записей import_opening {row['openings']}, допустима одна")
            if balance > 0 and int(row["openings"]) == 0:
                check.fail(f"пользователь {uid}: баланс {balance} без записи import_opening")
        if sum_src != sum_dst:
            check.fail(f"Σ источника {sum_src} ≠ Σ кошельков {sum_dst}")
        check.facts["mismatches"] = mismatches
        check.summary = f"Σ {sum_dst}/{sum_src} коп., расхождений {mismatches}"

    # ------------------------------------------------------------------------------------------- С3

    async def _desk_rows(self, desk: _Desk, *, paid_only: bool) -> list[tuple[str, int | None]]:
        """``(key, amount)`` of the rows the importer must carry over: the owner is an imported user (06 §2.3:
        a ``deleted`` user without subscription and money is not imported, nor are his payments) and the
        amount is positive. The rows left out are counted in ``facts`` — the importer reports them itself."""
        join = "LEFT JOIN transactions t ON t.id = x.transaction_id" if desk.amount.startswith("t.") else ""
        where = f"({desk.carried})" + (f" AND ({desk.paid})" if paid_only else "")
        rows = await self.src.fetch(
            f"SELECT x.{desk.key} AS k, x.user_id AS u, {desk.amount} AS a FROM {desk.table} x {join} "
            f"WHERE {where}"
        )
        users = set(await self.expected_user_ids())
        out = [(str(r["k"]), None if r["a"] is None else int(r["a"])) for r in rows if r["u"] in users]
        self._left_out[desk.name] = len(rows) - len(out)
        return out

    async def c3_paid(self, check: Check) -> None:
        parts: list[str] = []
        for desk in _DESKS:
            if not await self._has("src", desk.table):
                continue
            src = dict(await self._desk_rows(desk, paid_only=True))
            await self._compare_paid(check, desk.name, desk.target_key, src, parts)
            if self._left_out.get(desk.name):
                check.facts.setdefault(desk.name, {})["left_out"] = self._left_out[desk.name]
        if await self._has("src", "transactions"):
            users = set(await self.expected_user_ids())
            stars = await self.src.fetch(
                "SELECT external_id AS k, amount_kopeks AS a, user_id AS u FROM transactions "
                "WHERE external_id IS NOT NULL AND payment_method ILIKE '%star%' AND amount_kopeks > 0 "
                "AND coalesce(is_completed, true)"
            )
            src_stars: dict[str, int | None] = {str(r["k"]): int(r["a"]) for r in stars if r["u"] in users}
            if src_stars:
                await self._compare_paid(check, "stars", "external_id", src_stars, parts, sums=False)
        check.summary = "; ".join(parts) or "оплат нет"

    async def _compare_paid(
        self,
        check: Check,
        desk: str,
        column: str,
        src: Mapping[str, int | None],
        parts: list[str],
        *,
        sums: bool = True,
    ) -> None:
        """Count of every paid row; ruble sums over the rows whose source amount is known."""
        slug = self.cfg.instances.get(desk)
        if not src:
            parts.append(f"{desk} 0")
            return
        if slug is None or not await self.dst.fetchval(
            "SELECT 1 FROM payment_instances WHERE slug = $1", slug
        ):
            check.fail(f"{desk}: {len(src)} оплат в источнике, кассы «{slug}» в боте нет")
            return
        rows = await self.dst.fetch(
            f"SELECT p.{column} AS k, p.status, coalesce(p.paid_amount_minor, p.amount_minor) AS a "
            "FROM payments p JOIN payment_instances i ON i.id = p.instance_id "
            f"WHERE i.slug = $1 AND p.is_imported AND p.{column} = ANY($2::text[])",
            slug,
            list(src),
        )
        dst = {str(r["k"]): r for r in rows}
        paid = {k: int(r["a"]) for k, r in dst.items() if r["status"] == "paid"}
        known = {k: a for k, a in src.items() if a is not None}
        n_src, s_src = len(src), sum(known.values())
        n_dst, s_dst = len(paid), sum(a for k, a in paid.items() if k in known)
        check.facts[desk] = {
            "count_source": n_src,
            "count_target": n_dst,
            "sum_source": s_src,
            "sum_target": s_dst,
            "amount_unknown": n_src - len(known),
        }
        parts.append(f"{desk} {n_dst}/{n_src}" + (f" ({s_dst}/{s_src} коп.)" if sums else ""))
        for key in sorted(src):
            row = dst.get(key)
            if row is None:
                check.fail(f"{desk} {key}: оплата не перенесена")
            elif row["status"] != "paid":
                check.fail(f"{desk} {key}: статус в боте {row['status']}, в источнике paid")
            elif sums and key in known and int(row["a"]) != known[key]:
                check.fail(f"{desk} {key}: сумма {row['a']} ≠ {known[key]}")
        if n_src != n_dst or (sums and s_src != s_dst):
            check.fail(f"{desk}: итог {n_dst}/{s_dst} ≠ источник {n_src}/{s_src}")

    # ------------------------------------------------------------------------------------------- С4

    async def c4_open_invoices(self, check: Check) -> None:
        live: list[tuple[str, str, str]] = []  # (desk, key column in target, key)
        if await self._has("src", "rollypay_payments"):
            rows = await self.src.fetch(
                """
                SELECT order_id FROM rollypay_payments
                WHERE NOT is_paid AND (status IN ('pending', 'created', 'processing')
                                       OR (status = 'expired' AND created_at > $1))
                """,
                self.as_of - RP_LATE_WINDOW,
            )
            live += [("rollypay", "merchant_ref", str(r[0])) for r in rows]
        if await self._has("src", "cryptobot_payments"):
            rows = await self.src.fetch(
                "SELECT invoice_id FROM cryptobot_payments WHERE status = 'active' AND created_at > $1",
                self.as_of - CB_LIVE_WINDOW,
            )
            live += [("cryptobot", "external_id", str(r[0])) for r in rows]
        applied = await self._applied()
        ok = 0
        for desk, column, key in live:
            slug = self.cfg.instances.get(desk)
            row = await self.dst.fetchrow(
                "SELECT p.status, p.order_id, p.next_check_at, p.poll_plan, o.kind, "
                "o.status AS order_status FROM payments p JOIN payment_instances i ON i.id = p.instance_id "
                "LEFT JOIN orders o ON o.id = p.order_id "
                f"WHERE i.slug = $1 AND p.{column} = $2",
                slug,
                key,
            )
            if row is None:
                check.fail(f"{desk} {key}: незакрытый счёт не импортирован")
            elif row["status"] != "pending":
                check.fail(f"{desk} {key}: статус {row['status']}, нужен pending")
            elif row["order_id"] is None or row["kind"] is None:
                check.fail(f"{desk} {key}: pending без заказа")
            elif row["kind"] == "topup" and row["order_status"] != "awaiting_payment":
                check.fail(f"{desk} {key}: заказ в статусе {row['order_status']}")
            elif applied and (row["poll_plan"] is None or row["next_check_at"] is None):
                check.fail(f"{desk} {key}: после apply счёт никто не опрашивает (нет плана reconciler'а)")
            else:
                ok += 1
        polled = 0
        if not applied:
            # Before T0 the invoice is Bedolaga's: the stand's poller (it reads every instance, disabled ones
            # too) must not ask the cash desk, or a payment is credited here and in Bedolaga (06 §4.9).
            polled = int(
                await self.dst.fetchval(
                    "SELECT count(*) FROM payments WHERE is_imported AND status = 'pending' "
                    "AND (next_check_at IS NOT NULL OR poll_plan IS NOT NULL)"
                )
                or 0
            )
            if polled:
                check.fail(f"до T0 стенд опросил бы кассу по {polled} импортированным счетам Bedolaga")
        check.facts.update({"live": len(live), "imported_pending": ok, "polled_before_t0": polled})
        check.summary = f"{ok}/{len(live)} живых счетов — pending с заказом"

    async def _applied(self) -> bool:
        """``config_meta['import.bedolaga.applied']``: the final import (T0) happened."""
        return bool(
            await self.dst.fetchval("SELECT 1 FROM config_meta WHERE key = 'import.bedolaga.applied'")
        )

    # ------------------------------------------------------------------------------------------- С5

    async def _target_subs(self) -> list[asyncpg.Record]:
        subs = await self.expected_sub_ids()
        return await self.dst.fetch(
            """
            SELECT s.*, u.telegram_id AS owner_telegram_id
            FROM subscriptions s LEFT JOIN users u ON u.id = s.user_id
            WHERE s.id = ANY($1::bigint[]) OR s.user_id IS NULL
            ORDER BY s.id
            """,
            subs,
        )

    async def c5_linking(self, check: Check) -> None:
        rows = await self._target_subs()
        states = Counter(str(r["link_state"]) for r in rows)
        check.facts["states"] = dict(states)
        cross: dict[int, int] = {}
        if await self._has("src", "wlq_subjects"):
            cross = {
                int(r[0]): int(r[1])
                for r in await self.src.fetch(
                    "SELECT subscription_id, panel_user_id FROM wlq_subjects "
                    "WHERE subscription_id IS NOT NULL AND state = 'active'"
                )
            }
        linked_panel: set[int] = set()
        for r in rows:
            sid, state = int(r["id"]), str(r["link_state"])
            if state not in ("linked", "panel_missing"):
                check.fail(f"подписка {sid}: состояние {state} (не связана с панелью)")
                continue
            if state != "linked":
                continue
            pid = int(r["panel_user_id"])
            linked_panel.add(pid)
            if sid in cross and cross[sid] != pid:
                check.fail(f"подписка {sid}: панель {pid}, а wlq_subjects говорит {cross[sid]} (conflict)")
            if self.panel is None:
                continue
            user = self.panel.get(pid)
            if user is None:
                check.fail(f"подписка {sid}: пользователя панели {pid} нет (panel_missing не отмечен)")
                continue
            tg = r["owner_telegram_id"]
            if tg is not None and user.telegram_id is not None and int(user.telegram_id) != int(tg):
                check.fail(f"подписка {sid}: telegramId панели {user.telegram_id} ≠ {tg}")
            if r["panel_short_uuid"] and r["panel_short_uuid"] != user.short_uuid:
                check.fail(f"подписка {sid}: shortUuid в боте ≠ панели")
        conflicts: list[int] = []
        if await self._has("dst", "legacy_id_map"):
            # The importer stores a conflicting subscription as ``panel_missing`` (never linked
            # automatically); its link verdict stays in legacy_id_map until the owner's rule resolves it.
            conflicts = [
                int(r[0])
                for r in await self.dst.fetch(
                    "SELECT old_id FROM legacy_id_map WHERE source = 'bedolaga' AND entity = 'subscription' "
                    "AND data->>'link' = 'conflict' AND old_id = ANY($1::text[]) ORDER BY old_id::bigint",
                    [str(i) for i in await self.expected_sub_ids()],
                )
            ]
        for sid in conflicts:
            check.fail(f"подписка {sid}: конфликт связи с панелью не разобран (правило link / skip)")
        check.facts["conflicts"] = len(conflicts)
        total = len(rows)
        missing = states.get("panel_missing", 0) - len(conflicts)
        linked = states.get("linked", 0)
        if self.panel is not None:
            check.facts["unowned_panel"] = len(set(self.panel.users) - linked_panel)
        check.summary = f"linked {linked}/{total - missing} (panel_missing {missing})"

    # ------------------------------------------------------------------------------------------- С6

    async def c6_writer_plan(self, check: Check) -> None:
        if self.panel is None:
            check.fail("панель не прочитана: план writer'а не построен")
            return
        rows = [r for r in await self._target_subs() if r["link_state"] == "linked"]
        subs: dict[int, list[Substitution]] = defaultdict(list)
        if await self._has("dst", "panel_squad_substitutions"):
            for s in await self.dst.fetch(
                "SELECT subscription_id, base_squad_uuid, substitute_squad_uuid, owner_module "
                "FROM panel_squad_substitutions ORDER BY id"
            ):
                subs[int(s[0])].append(Substitution(s[1], s[2], s[3]))
        ops = 0
        for r in rows:
            user = self.panel.get(r["panel_user_id"])
            if user is None:
                continue  # С5 reports it
            for op in self.plan_for(r, user, subs.get(int(r["id"]), [])):
                ops += 1
                self.ops.append(op)
                check.fail(
                    f"подписка {op['sub_id']}: {op['op']} {op['field']} бот={op['desired']} "
                    f"панель={op['panel']}"
                )
        by_field = Counter(op["field"] for op in self.ops)
        check.facts.update({"subscriptions": len(rows), "operations": ops, "by_field": dict(by_field)})
        check.summary = f"{ops} операций на {len(rows)} подписках"

    @staticmethod
    def plan_for(
        row: Mapping[str, Any], user: PanelUser, subs: Sequence[Substitution]
    ) -> list[dict[str, Any]]:
        """Operations the writer would send to make the panel equal the bot's desired state."""
        overrides = {k for k in dict(row["overrides"] or {}) if not str(k).startswith("_")}
        sid = int(row["id"])
        out: list[dict[str, Any]] = []

        def add(fld: str, desired: Any, panel: Any, op: str = "update") -> None:
            out.append(
                {
                    "sub_id": sid,
                    "panel_user_id": int(row["panel_user_id"]),
                    "op": op,
                    "field": fld,
                    "desired": _jsonable(desired),
                    "panel": _jsonable(panel),
                    "hold": row["hold_kind"],
                    "disabled_reason": row["disabled_reason"],
                }
            )

        def managed(fld: str) -> bool:
            return _OVERRIDE_KEY[fld] not in overrides

        target = row["desired_expire_at"] if row["desired_expire_at"] is not None else row["paid_until"]
        if managed("expire") and target is not None and not _close(target, user.expire_at, EXPIRE_TOLERANCE):
            add("expire", target, user.expire_at)
        if managed("status"):
            disabled = user.status == "DISABLED"
            if row["desired_status"] == "disabled" and not disabled:
                add("status", "DISABLED", user.status, "disable")
            elif row["desired_status"] == "active" and disabled:
                add("status", "ACTIVE", user.status, "enable")
        if managed("squads"):
            desired = list(row["desired_squads"] or [])
            if not desired:
                add("squads", [], user.squad_uuids, "invalid")  # never sent empty (02 §8.1 p.11)
            else:
                expected = forward(desired, subs)
                if not same_squads(expected, user.squad_uuids):
                    add("squads", sorted(expected), sorted(user.squad_uuids))
        if managed("device_limit") and row["desired_device_limit"] != user.hwid_device_limit:
            add("device_limit", row["desired_device_limit"], user.hwid_device_limit)
        if (
            managed("traffic")
            and row["desired_traffic_bytes"] is not None
            and int(row["desired_traffic_bytes"]) != int(user.traffic_limit_bytes)
        ):
            add("traffic", row["desired_traffic_bytes"], user.traffic_limit_bytes)
        if (
            managed("strategy")
            and row["desired_reset_strategy"] is not None
            and row["desired_reset_strategy"] != user.traffic_limit_strategy
        ):
            add("strategy", row["desired_reset_strategy"], user.traffic_limit_strategy)
        if managed("tag") and (row["desired_tag"] or None) != (user.tag or None):
            add("tag", row["desired_tag"], user.tag)
        if managed("ext_squad") and (row["desired_ext_squad"] or None) != (user.external_squad_uuid or None):
            add("ext_squad", row["desired_ext_squad"], user.external_squad_uuid)
        return out

    # ------------------------------------------------------------------------------------------- С7

    async def c7_lte(self, check: Check) -> None:
        if not await self._has("src", "wlq_blocks"):
            check.summary = "в источнике нет LTE — нечего сверять"
            return
        blocks = await self.src.fetch(
            """
            SELECT b.panel_user_id, b.group_id, s.subscription_id
            FROM wlq_blocks b JOIN wlq_subjects s ON s.id = b.subject_id
            WHERE b.status IN ('pending_apply', 'active') AND b.mode = 'enforce'
            """
        )
        twins = {
            str(r["panel_uuid"]): str(r["base_squad_uuid"])
            for r in await self.src.fetch(
                "SELECT panel_uuid, base_squad_uuid FROM wlq_squads WHERE kind = 'twin' AND panel_uuid IS "
                "NOT NULL"
            )
        }
        usage = await self._lte_source_usage()
        if not blocks and not usage and not twins:
            check.summary = "активных LTE-блоков и периодов нет — нечего сверять"
            return
        if not await self._need(check, "dst", "lte_blocks", "lte_periods", "lte_period_usage"):
            return
        src_ids = {int(r["panel_user_id"]) for r in blocks}
        dst_rows = await self.dst.fetch(
            "SELECT s.id, s.panel_user_id FROM lte_blocks b JOIN subscriptions s ON s.id = b.subscription_id "
            "WHERE b.status = 'active'"
        )
        dst_ids = {int(r["panel_user_id"]) for r in dst_rows if r["panel_user_id"] is not None}
        sub_of = {int(r["panel_user_id"]): int(r["id"]) for r in dst_rows if r["panel_user_id"] is not None}
        for pid in sorted(src_ids - dst_ids):
            check.fail(f"LTE-блок панели {pid}: нет в боте")
        for pid in sorted(dst_ids - src_ids):
            check.fail(f"LTE-блок панели {pid}: в боте есть, в источнике нет")
        dst_twins = {
            str(r[0]): str(r[1])
            for r in await self.dst.fetch(
                "SELECT substitute_squad_uuid, base_squad_uuid FROM panel_squad_twins"
            )
        }
        for twin, base in sorted(twins.items()):
            if dst_twins.get(twin) != base:
                check.fail(f"двойник {twin}: в боте нет обратной карты на {base}")
        subst = {
            int(r[0])
            for r in await self.dst.fetch(
                "SELECT subscription_id FROM panel_squad_substitutions WHERE substitute_squad_uuid = "
                "ANY($1::text[])",
                list(twins),
            )
        }
        for pid in sorted(src_ids):
            if pid in sub_of and sub_of[pid] not in subst:
                check.fail(f"LTE-блок панели {pid}: нет замены сквада база→двойник у подписки {sub_of[pid]}")
            if self.panel is not None:
                user = self.panel.get(pid)
                if user is None or not set(user.squad_uuids) & set(twins):
                    check.fail(f"LTE-блок панели {pid}: в панели нет сквада-двойника")
        dst_usage = {
            int(r["panel_user_id"]): int(r["used"])
            for r in await self.dst.fetch(
                """
                SELECT s.panel_user_id, sum(u.used_bytes) AS used
                FROM lte_periods p JOIN lte_period_usage u ON u.period_id = p.id
                JOIN subscriptions s ON s.id = p.subscription_id
                WHERE p.state IN ('open', 'deferred') AND s.panel_user_id IS NOT NULL
                GROUP BY s.panel_user_id
                """
            )
        }
        drift = 0
        for pid, used in sorted(usage.items()):
            got = dst_usage.get(pid, 0)
            if abs(got - used) > max(used // 100, LTE_TOLERANCE_BYTES):
                drift += 1
                check.fail(f"LTE расход панели {pid}: бот {got} Б, источник {used} Б")
        check.facts.update(
            {"blocks_source": len(src_ids), "blocks_target": len(dst_ids), "usage_drift": drift}
        )
        check.summary = f"блоки {len(src_ids & dst_ids)}/{len(src_ids)}, расход вне допуска: {drift}"

    async def _lte_source_usage(self) -> dict[int, int]:
        if not (await self._has("src", "wlq_periods") and await self._has("src", "wlq_period_usage")):
            return {}
        rows = await self.src.fetch(
            """
            SELECT s.panel_user_id, sum(u.used_bytes) AS used
            FROM wlq_periods p JOIN wlq_period_usage u ON u.period_id = p.id
            JOIN wlq_subjects s ON s.id = p.subject_id
            WHERE p.state IN ('open', 'deferred')
            GROUP BY s.panel_user_id
            """
        )
        return {int(r["panel_user_id"]): int(r["used"] or 0) for r in rows}

    # ------------------------------------------------------------------------------------------- С8

    async def c8_ip_guard(self, check: Check) -> None:
        holds_patched = [op for op in self.ops if op["hold"] and op["field"] == "expire"]
        enables = [op for op in self.ops if op["disabled_reason"] == "ip_guard" and op["op"] == "enable"]
        for op in holds_patched:
            check.fail(f"подписка {op['sub_id']} с hold: writer записал бы expireAt — запрещено")
        for op in enables:
            check.fail(f"подписка {op['sub_id']}: writer включил бы аккаунт с блоком IP Guard (R5)")
        blocks: list[asyncpg.Record] = []
        if await self._has("src", "ip_guard_blocks"):
            blocks = await self.src.fetch(
                """
                SELECT b.id, b.panel_user_id, b.subscription_id, b.owner_kind, b.blocked_at,
                       b.end_date_at_block,
                       b.panel_expire_at_block, b.credited_seconds, b.zeroed_during_block, s.end_date
                FROM ip_guard_blocks b LEFT JOIN subscriptions s ON s.id = b.subscription_id
                WHERE b.status = 'active' ORDER BY b.id
                """
            )
        whitelist_raw = self.cfg.ip_guard_whitelist
        if whitelist_raw is None and await self._has("src", "system_settings"):
            whitelist_raw = await self.src.fetchval(
                "SELECT value FROM system_settings WHERE key = $1", WHITELIST_KEY
            )
        wl_ids, wl_bad = _parse_whitelist(whitelist_raw)
        if not blocks and not wl_ids:
            check.facts.update({"blocks": 0, "whitelist": 0, "writer_expire_on_hold": len(holds_patched)})
            check.summary = "активных блоков и белого списка нет — нечего сверять"
            return
        if not await self._need(check, "dst", "ip_guard_blocks", "ip_guard_exempt"):
            return
        dst_blocks = {
            int(r["panel_user_id"]): r
            for r in await self.dst.fetch(
                """
                SELECT s.panel_user_id, b.frozen_seconds, s.id AS sub_id, s.hold_kind, s.disabled_reason,
                       s.desired_status, s.paid_until
                FROM ip_guard_blocks b JOIN subscriptions s ON s.id = b.subscription_id
                WHERE b.status = 'active' AND s.panel_user_id IS NOT NULL
                """
            )
        }
        matched = 0
        for b in blocks:
            pid = int(b["panel_user_id"])
            row = dst_blocks.get(pid)
            if row is None:
                check.fail(f"IP Guard блок {b['id']} (панель {pid}): нет активного блока в боте")
                continue
            matched += 1
            want = _frozen_seconds(b)
            if int(row["frozen_seconds"]) != want:
                check.fail(f"IP Guard панель {pid}: frozen_seconds {row['frozen_seconds']} ≠ {want}")
            if row["hold_kind"] != "ip_guard" or row["disabled_reason"] != "ip_guard":
                check.fail(
                    f"IP Guard панель {pid}: hold_kind={row['hold_kind']}, reason={row['disabled_reason']}"
                )
            if row["desired_status"] != "disabled":
                check.fail(f"IP Guard панель {pid}: desired_status={row['desired_status']}")
            user = self.panel.get(pid) if self.panel is not None else None
            if self.panel is not None and (user is None or user.status != "DISABLED"):
                check.fail(f"IP Guard панель {pid}: аккаунт в панели не DISABLED")
            paid = row["paid_until"]
            candidates = [b["end_date"], user.expire_at if user is not None else None]
            if not any(_close(paid, c, PAID_UNTIL_TOLERANCE) for c in candidates if c is not None):
                check.fail(f"IP Guard панель {pid}: paid_until {_fmt(paid)} сдвинут (ожидался срок §2.2)")
        exempt = {
            int(r[0])
            for r in await self.dst.fetch(
                "SELECT s.panel_user_id FROM ip_guard_exempt e JOIN subscriptions s ON s.id = "
                "e.subscription_id "
                "WHERE e.until IS NULL AND s.panel_user_id IS NOT NULL"
            )
        }
        known = {
            int(r[0])
            for r in await self.dst.fetch(
                "SELECT panel_user_id FROM subscriptions WHERE panel_user_id = ANY($1::bigint[])", wl_ids
            )
        }
        unresolved = [i for i in wl_ids if i not in known]
        for pid in wl_ids:
            if pid in known and pid not in exempt:
                check.fail(f"белый список: панель {pid} не в ip_guard_exempt")
        check.facts.update(
            {
                "blocks": len(blocks),
                "matched": matched,
                "whitelist": len(wl_ids),
                "whitelist_unresolved": unresolved,
                "whitelist_invalid": wl_bad,
                "writer_expire_on_hold": len(holds_patched),
            }
        )
        note = f", не найдены в боте: {len(unresolved)}" if unresolved or wl_bad else ""
        check.summary = (
            f"блоки {matched}/{len(blocks)}, PATCH expireAt по hold: {len(holds_patched)}, "
            f"белый список {len(wl_ids) - len(unresolved)}/{len(wl_ids)}{note}"
        )

    # ------------------------------------------------------------------------------------------- С9

    async def c9_referral(self, check: Check) -> None:
        if not await self._has("src", "referral_earnings"):
            check.summary = "в источнике нет referral_earnings"
            return
        since = self.as_of - REF_LIMIT_WINDOW
        # Same reading as the importer (svbg.importers.bedolaga.referral_days): one side per (invitee, side),
        # self markers dropped, a granted marker wins over skips, a skip is deferred only by its last time.
        src_limit = {
            int(r[0]): int(r[1])
            for r in await self.src.fetch(
                "SELECT user_id, count(*) FROM (SELECT user_id, referral_id, min(created_at) AS at "
                "FROM referral_earnings WHERE reason = 'referral_days_inviter' AND user_id <> referral_id "
                "GROUP BY user_id, referral_id) g WHERE at > $1 GROUP BY user_id",
                since,
            )
        }
        src_sides = {
            str(r[0]): int(r[1])
            for r in await self.src.fetch(
                "SELECT reason, count(DISTINCT referral_id) FROM referral_earnings "
                "WHERE reason IN ('referral_days_inviter', 'referral_days_invitee') "
                "AND user_id <> referral_id "
                "GROUP BY reason"
            )
        }
        src_deferred = int(
            await self.src.fetchval(
                "SELECT count(*) FROM (SELECT referral_id, "
                "reason IN ('referral_days_inviter', 'referral_days_inviter_skipped') AS inviter_side, "
                "bool_or(reason IN ('referral_days_inviter', 'referral_days_invitee')) AS granted, "
                "max(created_at) FILTER (WHERE reason IN ('referral_days_inviter_skipped', "
                "'referral_days_invitee_skipped')) AS skipped "
                "FROM referral_earnings WHERE reason IN ('referral_days_inviter', "
                "'referral_days_inviter_skipped', 'referral_days_invitee', 'referral_days_invitee_skipped') "
                "AND user_id <> referral_id GROUP BY 1, 2) s "
                "WHERE NOT granted AND skipped + $2::interval > $1",
                self.as_of,
                REF_DEFER_WINDOW,
            )
            or 0
        )
        if not src_limit and not src_sides and not src_deferred:
            check.summary = "реферальных дней в источнике нет"
            return
        if not await self._need(check, "dst", "referral_rewards"):
            return
        dst_limit = {
            int(r[0]): int(r[1])
            for r in await self.dst.fetch(
                "SELECT user_id, count(*) FROM referral_rewards WHERE side = 'inviter' AND kind = 'days' "
                "AND status = 'granted' AND granted_at > $1 GROUP BY user_id",
                since,
            )
        }
        for uid in sorted(set(src_limit) | set(dst_limit)):
            if src_limit.get(uid, 0) != dst_limit.get(uid, 0):
                check.fail(
                    f"пригласивший {uid}: за 30 дней источник {src_limit.get(uid, 0)}, бот "
                    f"{dst_limit.get(uid, 0)}"
                )
        dst_sides = {
            str(r[0]): int(r[1])
            for r in await self.dst.fetch(
                "SELECT side, count(*) FROM referral_rewards WHERE kind = 'days' AND status = 'granted' "
                "GROUP BY side"
            )
        }
        for side in ("inviter", "invitee"):
            want = src_sides.get(f"referral_days_{side}", 0)
            if dst_sides.get(side, 0) != want:
                check.fail(f"granted {side}: бот {dst_sides.get(side, 0)}, маркеров {want}")
        dst_deferred = int(
            await self.dst.fetchval(
                "SELECT count(*) FROM referral_rewards WHERE kind = 'days' AND status = 'deferred'"
            )
            or 0
        )
        if dst_deferred != src_deferred:
            check.fail(f"deferred: бот {dst_deferred}, источник {src_deferred}")
        check.facts.update(
            {"inviters_30d": len(src_limit), "deferred_source": src_deferred, "deferred_target": dst_deferred}
        )
        check.summary = (
            f"пригласивших с наградами за 30 дн. {len(src_limit)}, deferred {dst_deferred}/{src_deferred}"
        )

    # ------------------------------------------------------------------------------------------ С10

    async def c10_notifications(self, check: Check) -> None:
        if not await self._has("src", "sent_notifications"):
            check.summary = "в источнике нет sent_notifications"
            return
        rows = await self.src.fetch(
            """
            SELECT n.subscription_id, n.notification_type
            FROM sent_notifications n JOIN subscriptions s ON s.id = n.subscription_id
            WHERE s.end_date > $1 AND s.end_date <= $2
            """,
            self.as_of - NOTIFY_PAST,
            self.as_of + NOTIFY_AHEAD,
        )
        want = {(int(r[0]), _notify_base(str(r[1]))) for r in rows}
        if not want:
            check.summary = "напоминаний по подпискам у срока нет"
            return
        subs = sorted({sid for sid, _ in want})
        paid = {
            int(r[0]): r[1]
            for r in await self.dst.fetch(
                "SELECT id, paid_until FROM subscriptions WHERE id = ANY($1::bigint[])", subs
            )
        }
        have = {
            (int(r["subscription_id"]), _target_base(str(r["kind"])), str(r["anchor"]))
            for r in await self.dst.fetch(
                "SELECT subscription_id, kind, anchor FROM notification_log WHERE subscription_id = "
                "ANY($1::bigint[])",
                subs,
            )
        }
        ok = 0
        for sid, base in sorted(want):
            anchor = _epoch_anchor(paid.get(sid))
            if (sid, base, anchor) in have:
                ok += 1
            else:
                check.fail(f"подписка {sid}: нет notification_log «{base}» с якорем paid_until")
        check.facts.update({"expected": len(want), "seeded": ok})
        check.summary = f"засеяно {ok}/{len(want)}"

    # ------------------------------------------------------------------------------------------ С11

    async def c11_deeplinks(self, check: Check) -> None:
        camps: list[asyncpg.Record] = []
        if await self._has("src", "advertising_campaigns"):
            camps = await self.src.fetch(
                "SELECT start_parameter, is_active FROM advertising_campaigns ORDER BY id"
            )
        users = set(await self.expected_user_ids())
        codes = [
            r
            for r in await self.src.fetch(
                "SELECT id, referral_code FROM users WHERE referral_code IS NOT NULL AND referral_code <> '' "
                "ORDER BY id"
            )
            if int(r["id"]) in users
        ]
        n = max(self.cfg.ref_sample, 0)
        step = max(len(codes) // n, 1) if n else 1
        sample = codes[::step][:n]
        if not camps and not sample:
            check.summary = "кодов нет"
            return
        ok = 0
        if camps and await self._need(check, "dst", "ad_links"):
            have = {str(r[0]) for r in await self.dst.fetch("SELECT code FROM ad_links")}
            for r in camps:
                code = str(r["start_parameter"])
                if code not in have:
                    check.fail(f"?start={code}: кампании нет среди ad_links")
                elif not r["is_active"] or await self._resolves(check, code, "ad"):
                    ok += 1  # a stopped campaign only has to exist: its link opens /start without a bonus
        if sample and await self._need(check, "dst", "referral_codes"):
            owners = {
                str(r[0]): int(r[1])
                for r in await self.dst.fetch(
                    "SELECT code, user_id FROM referral_codes WHERE code = ANY($1::text[])",
                    [str(r["referral_code"]) for r in sample],
                )
            }
            for r in sample:
                code, uid = str(r["referral_code"]), int(r["id"])
                if owners.get(code) != uid:
                    check.fail(f"реф-код {code}: в боте у {owners.get(code)}, нужен {uid}")
                elif await self._resolves(check, ref_payload(code), "ref"):
                    ok += 1
        total = len(camps) + len(sample)
        check.facts.update({"campaigns": len(camps), "ref_sample": len(sample), "ok": ok})
        manual = "" if self.cfg.resolve_link else " (открыть на тестовом боте — вручную)"
        check.summary = f"{ok}/{total} кодов найдены{manual}"

    async def _resolves(self, check: Check, payload: str, kind: str) -> bool:
        if self.cfg.resolve_link is None:
            return True
        got = await self.cfg.resolve_link(payload)
        if got != kind:
            check.fail(f"?start={payload}: роутер распознал как {got}, нужно {kind}")
            return False
        return True

    # ------------------------------------------------------------------------------------------ С12

    async def c12_trial(self, check: Check) -> None:
        users = set(await self.expected_user_ids())
        with_subs = [
            (int(r[0]), r[1])
            for r in await self.src.fetch(
                "SELECT DISTINCT u.id, u.telegram_id FROM users u JOIN subscriptions s ON s.user_id = u.id "
                "ORDER BY u.id"
            )
            if int(r[0]) in users
        ]
        ids = [uid for uid, _ in with_subs]
        grants = await self.dst.fetch(
            "SELECT user_id, telegram_id FROM trial_grants WHERE user_id = ANY($1::bigint[]) "
            "OR telegram_id = ANY($2::bigint[])",
            ids,
            [int(tg) for _, tg in with_subs if tg is not None],
        )
        by_user = {int(r[0]) for r in grants if r[0] is not None}
        by_tg = {int(r[1]) for r in grants if r[1] is not None}
        ok = 0
        for uid, tg in with_subs:
            if uid in by_user or (tg is not None and int(tg) in by_tg):
                ok += 1
            else:
                check.fail(f"пользователь {uid}: есть подписка, но нет отметки «триал использован»")
        check.facts.update({"users_with_subscription": len(with_subs), "marked": ok})
        check.summary = f"{ok}/{len(with_subs)}"


# ---------------------------------------------------------------------------------------------- service


class _Attention(Protocol):
    async def raise_item(self, dedup_key: str, severity: str, title: str, body: str, **kw: Any) -> Any: ...

    async def resolve(self, dedup_key: str) -> Any: ...


#: ``await importer(mode="shadow", source_dsn=dsn)`` → the importer's report (``import_runs.report``).
ImporterPort = Callable[..., Awaitable[Mapping[str, Any] | None]]
PostFn = Callable[[str, str], Awaitable[Any]]

ATTN_TOKEN: Final = "shadow:token_writable"  # noqa: S105 - attention dedup key, not a secret
ATTN_RED: Final = "shadow:red"
_TXT_PROBE_TITLE = "Shadow остановлен: введён полный токен панели"
_TXT_PROBE_BODY = (
    "Проба PATCH вернула не 403: {probe}. В теневом режиме бот работает только с токеном «только чтение», "
    "иначе писатель может изменить панель Bedolaga. Создайте в панели токен со скоупами только read "
    "(06 §4.1 п.3) и замените REMNAWAVE_TOKEN. Shadow не запускается, пока токен не заменён."
)
_TXT_RED_TITLE = "Shadow-сверка Bedolaga: есть расхождения"
_TXT_RED_BODY = "Красные проверки: {codes}. Подробности — в теме «Система» и в отчёте shadow."


class ShadowService:
    """One shadow pass = probe → importer (shadow) → С1–С12 → store → post. Never writes to the panel."""

    def __init__(
        self,
        *,
        target_dsn: str,
        source_dsn: Callable[[], str | None] | str | None,
        api: Callable[[], RemnawaveApi | None],
        importer: ImporterPort | None = None,
        post: PostFn | None = None,
        attention: _Attention | None = None,
        config: ShadowConfig | Callable[[], ShadowConfig] | None = None,
        stop_writer: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        """``stop_writer(reason)`` — the stand's writer kill switch, called when the probe proves the panel
        token can write (06 §4.1 p.3: «стенд останавливает writer-путь … не запускает shadow»)."""
        self._target_dsn = _pg_dsn(target_dsn)
        self._source = source_dsn
        self._api = api
        self._importer = importer
        self._post = post
        self._attention = attention
        self._config = config
        self._stop_writer = stop_writer

    def config(self) -> ShadowConfig:
        cfg = self._config
        if callable(cfg):
            return cfg()
        return cfg or ShadowConfig()

    def _source_dsn(self) -> str | None:
        value = self._source() if callable(self._source) else self._source
        return _pg_dsn(value) if value else None

    def register(self, scheduler: Any, *, at: time = time(6, 0), tz: str | None = None) -> None:
        """Daily pass (06 §4.2 p.3). Default 06:00 owner time: away from 00:00–00:10 and ~03:00 (06 §4.4)."""
        scheduler.daily("importers.shadow", at, tz or self.config().timezone, self.tick, timeout_s=3600)

    async def tick(self) -> None:
        await self.run("daily")

    async def run(self, trigger: str = "manual", *, as_of: datetime | None = None) -> ShadowReport:
        cfg = self.config()
        zone = _zone(cfg.timezone)
        report = ShadowReport(as_of=as_of or now(), started_at=now(), trigger=trigger)
        try:
            await self._run(report, cfg)
        except Exception as exc:
            log.exception("shadow pass failed")
            report.blocked = report.blocked or f"сбой: {type(exc).__name__}: {str(exc)[:200]}"
        report.finished_at = now()
        conn = await asyncpg.connect(self._target_dsn)
        try:
            await save_report(conn, report, zone)
            report.streak = green_streak(await load_history(conn))
            await conn.execute(
                "UPDATE config_meta SET value = jsonb_set(value, '{streak}', to_jsonb($2::int)) WHERE key = "
                "$1",
                LAST_KEY,
                report.streak,
            )
        finally:
            await conn.close()
        await self._announce(report, zone)
        return report

    async def _run(self, report: ShadowReport, cfg: ShadowConfig) -> None:
        api = self._api()
        if api is None:
            report.blocked = "панель не подключена (REMNAWAVE_URL / токен)"
            return
        source = self._source_dsn()
        if not source:
            report.blocked = "не задан DSN источника (копия БД Bedolaga)"
            return
        panel = await PanelIndex.load(api)
        report.probe = await probe_token(api, max_panel_id=panel.max_id)
        if not report.probe.read_only:
            report.blocked = f"токен панели не только для чтения: {report.probe.text()}"
            if self._attention is not None:
                await self._attention.raise_item(
                    ATTN_TOKEN, "error", _TXT_PROBE_TITLE, _TXT_PROBE_BODY.format(probe=report.probe.text())
                )
            if self._stop_writer is not None:
                try:
                    await self._stop_writer(report.blocked)
                except Exception:  # the alert below still goes out
                    log.exception("shadow: writer not stopped")
            return
        if self._attention is not None:
            with contextlib.suppress(Exception):
                await self._attention.resolve(ATTN_TOKEN)
        import_check: Check | None = None
        if self._importer is not None:
            result = await self._importer(mode="shadow", source_dsn=source)
            report.import_result = dict(result or {})
            cfg = _with_import_excludes(cfg, report.import_result)
            import_check = import_verdict(report.import_result)
        async with (
            open_readonly(source, name="svbg-shadow-source") as src,
            open_readonly(self._target_dsn, name="svbg-shadow-target") as dst,
        ):
            rec = Reconciliation(src, dst, panel, as_of=report.as_of, config=cfg)
            report.checks = await rec.run()
            report.ops = rec.ops
            report.ops_total = len(rec.ops)
        if import_check is not None:
            report.checks.insert(0, import_check)

    async def _announce(self, report: ShadowReport, zone: tzinfo) -> None:
        if self._post is not None:
            try:
                await self._post("system", report.summary_text(zone))
            except Exception:
                log.exception("shadow: summary not posted")
        if self._attention is None or report.blocked:
            return
        try:
            if report.green:
                await self._attention.resolve(ATTN_RED)
            else:
                codes = ", ".join(c.replace("C", "С") for c in report.red_codes)
                await self._attention.raise_item(
                    ATTN_RED, "warn", _TXT_RED_TITLE, _TXT_RED_BODY.format(codes=codes)
                )
        except Exception:
            log.exception("shadow: attention not updated")


def import_verdict(result: Mapping[str, Any]) -> Check:
    """С0: the importer's own verdict. Its blocking problems (``subscription_conflict``, ``user_id_taken``,
    ``payment_paid_twice``, ``guest_purchase_undelivered``, ``module_missing``…) and its red checks are
    invisible to С1–С12 (a conflict is stored as ``panel_missing``, a taken id simply is not imported), so
    a shadow day is green only when the import itself is (06 §4.3 С5: «− разобранные conflict»)."""
    check = Check("C0", title="Импорт: без блокирующих проблем")
    blocking = result.get("blocking")
    if not result.get("finished") or result.get("error"):
        check.fail(f"импорт не завершён: {result.get('error') or 'нет итога'}")
    if not isinstance(blocking, Mapping):
        check.fail("в отчёте импорта нет списка блокирующих проблем")
        blocking = {}
    for kind, n in sorted(blocking.items()):
        examples = (result.get("issues") or {}).get(kind) or []
        sample = "; ".join(json.dumps(e, ensure_ascii=False, default=str)[:120] for e in examples[:2])
        check.fail(f"{kind}: {n}" + (f" — {sample}" if sample else ""))
    red = sorted(code for code, c in (result.get("checks") or {}).items() if (c or {}).get("ok") is False)
    for code in red:
        check.fail(f"проверка импорта {code} красная")
    if result.get("green") is False and not blocking and not red and check.ok:
        check.fail("импорт не зелёный")
    check.facts.update({"run_id": result.get("run_id"), "blocking": dict(blocking), "red": red})
    check.summary = f"импорт #{result.get('run_id')}: " + (
        "без блокирующих проблем" if check.ok else f"блокирующих видов {len(blocking)}, красных {len(red)}"
    )
    return check


def _zone(name: str) -> tzinfo:
    from svbg.jobs.scheduler import resolve_tz

    try:
        return resolve_tz(name)
    except (ValueError, LookupError, OSError):
        return UTC


def _with_import_excludes(cfg: ShadowConfig, result: Mapping[str, Any]) -> ShadowConfig:
    """Ids the importer skipped by the owner's rules (``report["skipped"] = {"users": [...], ...}``)."""
    skipped = result.get("skipped")
    if not isinstance(skipped, Mapping):
        return cfg
    merged: dict[str, frozenset[int]] = {k: frozenset(v) for k, v in cfg.exclude.items()}
    for entity, ids in skipped.items():
        if isinstance(ids, Iterable) and not isinstance(ids, (str, bytes)):
            extra = {int(i) for i in ids if isinstance(i, int) or str(i).isdigit()}
            merged[str(entity)] = merged.get(str(entity), frozenset()) | extra
    from dataclasses import replace

    return replace(cfg, exclude=merged)
