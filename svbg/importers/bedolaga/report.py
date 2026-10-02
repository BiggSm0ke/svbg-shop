"""Import report: per-stage counters, issue lists for the owner (dry-run §4.2 п.1) and the checks of 06 §4.3
that the importer itself can prove (С1–С5, С10, С12). С6–С9 and С11 need the panel writer / modules / a test
bot and are filled by the shadow run (``svbg.importers.shadow``).

The report is plain JSON (stored in ``import_runs.report``); :func:`render_text` turns it into the short
admin message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

__all__ = ["ISSUE_CAP", "Report", "render_text"]

#: At most this many examples per issue kind are stored (the total is always counted).
ISSUE_CAP: Final = 200

#: Issues that block the cut-over (money or identity at risk); the rest are informational.
BLOCKING: Final = frozenset(
    {
        "user_id_taken",
        "user_telegram_taken",
        "subscription_id_taken",
        "subscription_conflict",
        "panel_user_taken",
        "negative_balance",
        "wallet_adjust_blocked",
        "payment_user_missing",
        "payment_instance_missing",
        "live_invoice_unresolved",
        "subscription_user_missing",
        "payment_paid_twice",
        "payment_paid_conflict",
        "guest_purchase_undelivered",
        "user_restricted",
        "module_missing",
    }
)

_TITLES: Final = {
    "users": "Пользователи",
    "subscriptions": "Подписки",
    "wallet": "Кошельки",
    "payments": "Оплаты",
    "promo": "Промокоды",
    "ads": "Кампании",
    "referral": "Рефералы",
    "catalog": "Каталог",
    "misc": "Прочее",
    "modules": "Модули",
}


@dataclass(slots=True)
class Report:
    run_id: int
    mode: str
    t0: datetime
    source: str = "bedolaga"
    alembic_version: str | None = None
    stage: str | None = None
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    issues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    issue_totals: dict[str, int] = field(default_factory=dict)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Source ids left out by the owner's rules (``import_overrides``): shadow excludes them from С1.
    skipped: dict[str, list[int]] = field(default_factory=dict)
    finished: bool = False
    error: str | None = None

    def inc(self, stage: str, key: str, n: int = 1) -> None:
        if n:
            bucket = self.counts.setdefault(stage, {})
            bucket[key] = bucket.get(key, 0) + n

    def set(self, stage: str, key: str, n: int) -> None:
        self.counts.setdefault(stage, {})[key] = n

    def get(self, stage: str, key: str) -> int:
        return self.counts.get(stage, {}).get(key, 0)

    def issue(self, kind: str, **details: Any) -> None:
        self.issue_totals[kind] = self.issue_totals.get(kind, 0) + 1
        bucket = self.issues.setdefault(kind, [])
        if len(bucket) < ISSUE_CAP:
            bucket.append({k: _plain(v) for k, v in details.items()})

    def skip(self, entity: str, source_id: int) -> None:
        bucket = self.skipped.setdefault(entity, [])
        if source_id not in bucket:
            bucket.append(source_id)

    def check(self, code: str, ok: bool | None, **details: Any) -> None:
        """``ok=None`` — not checkable by the importer (shadow fills it)."""
        self.checks[code] = {"ok": ok, **{k: _plain(v) for k, v in details.items()}}

    def part(self, code: str, name: str, *, expected: int, present: int) -> None:
        """One line of a multi-entity check (С1): ``expected == present``."""
        c = self.checks.setdefault(code, {"ok": True})
        c[name] = {"expected": expected, "present": present}
        c["ok"] = c.get("ok") is not False and expected == present

    @property
    def blocking(self) -> dict[str, int]:
        return {k: v for k, v in self.issue_totals.items() if k in BLOCKING}

    @property
    def green(self) -> bool:
        """Every importer check passed and nothing blocks the cut-over."""
        return not self.blocking and all(c.get("ok") is not False for c in self.checks.values())

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "source": self.source,
            "t0": self.t0.isoformat(),
            "alembic_version": self.alembic_version,
            "stage": self.stage,
            "counts": self.counts,
            "issues": self.issues,
            "issue_totals": self.issue_totals,
            "checks": self.checks,
            "skipped": self.skipped,
            "blocking": self.blocking,
            "green": self.green,
            "finished": self.finished,
            "error": self.error,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    return value


def render_text(report: Report | dict[str, Any]) -> str:
    """Short plain-text summary for the admin chat («⚙️ Система»)."""
    data = report.as_json() if isinstance(report, Report) else report
    mode = {"dry_run": "пробный прогон", "shadow": "теневой импорт", "apply": "импорт"}.get(
        str(data.get("mode")), str(data.get("mode"))
    )
    lines = [f"Импорт Bedolaga #{data.get('run_id')} — {mode}"]
    for stage, counters in (data.get("counts") or {}).items():
        body = ", ".join(f"{k} {v}" for k, v in counters.items())
        lines.append(f"• {_TITLES.get(stage, stage)}: {body}")
    checks = data.get("checks") or {}
    if checks:
        marks = " ".join(
            f"{code}{'✅' if c.get('ok') else ('➖' if c.get('ok') is None else '❌')}"
            for code, c in sorted(
                checks.items(), key=lambda kv: int(kv[0][1:]) if kv[0][1:].isdigit() else 99
            )
        )
        lines.append(f"Сверка: {marks}")
    totals = data.get("issue_totals") or {}
    if totals:
        lines.append("Требует разбора: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    if data.get("error"):
        lines.append(f"Ошибка: {data['error']}")
    lines.append("Готово к переключению ✅" if data.get("green") else "К переключению не готово ❌")
    return "\n".join(lines)
