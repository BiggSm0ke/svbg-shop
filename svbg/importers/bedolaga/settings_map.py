"""Bedolaga settings → SvBG Shop settings (06 §3; 05 §2.1.17, §2.2.10).

Input: the Bedolaga ``.env`` (557 keys on the owner's production), the ``system_settings`` rows (key = the
same env name; the environment wins over the row, 06 §3.1), the LTE section overrides ``wlq_settings``
(they win over both, as in the owner's engine) and the ``required_channels`` rows.

Output — a :class:`SettingsImportPlan`, nothing is written while it is built:

* ``changes`` — values for our registry keys, applied as ONE batch ``SettingsService.apply(source="import")``
  (one ``batch_id`` → undo with one button);
* ``deferred`` — values that must wait for a step of the T0 runbook (06 §4.4): cash desks are switched on
  after the Caddy routes (step 8), the Telegram mode/public URL with the token (step 9), IP Guard and LTE
  after the final import (step 12; LTE starts in ``shadow`` and imported blocks stay);
* ``catalog`` — prices and plan limits (prices go to ``plan_prices``, never to settings);
* ``topics`` — existing admin topic ids, written to ``admin_topics`` only when the owner reuses them;
* ``cdn_nodes`` — ``IP_GUARD_EXCLUDED_NODE_UUIDS`` → ``ip_guard_nodes.cdn``;
* ``not_transferred`` — every other source key with the reason («не перенесено»). Every source key ends up
  in exactly one of these places (checked by :meth:`SettingsImportPlan.unaccounted`).

Hard rules: the Bedolaga ``BOT_TOKEN`` and the panel token are never copied (06 §3.2); a masked value of a
dump is never imported as a secret; Bedolaga's own maintenance switch (set at T−20 min, 06 §4.4 step 2) is not
carried over; ``WEBHOOK_DROP_PENDING_UPDATES`` is dropped (the update queue is never flushed).
"""

from __future__ import annotations

import json
import re
import zoneinfo
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.catalog.model import DeviceAddon
from svbg.catalog.repo import Actor, create_plan, delete_price, set_price, update_plan
from svbg.catalog.tables import RESET_STRATEGIES, locations, plan_prices, plans
from svbg.core.settings import values as setting_values
from svbg.core.settings.service import Change
from svbg.importers.envmap import (
    NotTransferred,
    PlannedChange,
    SourceValue,
    ValueParseError,
    effective_values,
    is_masked,
    parse_env,
    to_bool,
    to_int,
    to_int_list,
    to_str_list,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.settings.service import SettingsService

__all__ = [
    "GIB",
    "BedolagaSettingsSource",
    "CatalogImport",
    "SettingsApplyResult",
    "SettingsImportPlan",
    "TopicImport",
    "apply_catalog",
    "apply_cdn_nodes",
    "apply_settings",
    "apply_topics",
    "build_plan",
    "diff_against",
    "read_source_tables",
]

GIB: Final = 1024**3  # Bedolaga tariff traffic is in GiB (06 §2.1)
#: The paid plan: the data importer (``bedolaga/catalog.py``) finds it through
#: ``legacy_id_map('bedolaga', 'plan', 'classic')`` first, then by the code ``bedolaga_classic``.
#: :func:`apply_catalog` looks it up the same way (then the preset ``standard``) and writes the
#: ``legacy_id_map`` row, so whichever runs first creates or picks the plan and the other one only aligns it
#: (never two paid plans).
PLAN_CODES: Final = ("bedolaga_classic", "standard")
PLAN_MAP: Final = ("bedolaga", "plan", "classic")  # legacy_id_map (source, entity, old_id) of the paid plan
TRIAL_CODE: Final = "trial"
_HHMM_RE: Final = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
_TAG_RE: Final = re.compile(r"[A-Z0-9_]{1,16}")
_PRICE_RE: Final = re.compile(r"PRICE_(\d{1,4})_DAYS")

# --------------------------------------------------------------------------------------- reasons (RU)

_R: Final = {
    "bot_token": "боевой токен вводится только в окне T0 (06 §4.4 шаг 9); до этого бот работает на тестовом",
    "panel_token": "токен панели не копируется: для shadow выпускается read-only токен, для прода — новый "
    "со скоупами (06 §3.2)",
    "masked": "значение замаскировано в выгрузке — введите его в мастере",
    "empty": "пусто в Bedolaga — оставлено значение SvBG по умолчанию",
    "new_secret": "секрет генерируется заново (06 §3.2)",
    "drop_pending": "сброс очереди апдейтов запрещён (06 §0 п.5)",
    "maintenance": "техработы Bedolaga перед остановкой (06 §4.4 шаг 2) не переносятся",
    "dead": "мёртвый ключ Bedolaga (06 §3.4)",
    "subsystem": "выключенная подсистема/инфраструктура Bedolaga (06 §3.4)",
    "cash_off": "касса выключена в Bedolaga — ключи не переносятся (06 §3.4)",
    "cash_unsupported": "касса ВКЛЮЧЕНА в Bedolaga, но в SvBG её нет — счета этой кассы не будут "
    "приниматься (вопрос владельцу)",
    "referral_money": "денежная рефералка/вывод не переносятся (06 §1.3)",
    "replaced_import": "заменено структурой импорта и правилами SvBG (06 §2.5)",
    "core_behaviour": "поведение ядра SvBG, не настройка",
    "jobs": "механика планировщика SvBG (jobs/Scheduler)",
    "logo": "логотип загружается файлом до T0 (media.start): URL кабинета Bedolaga умрёт после остановки",
    "traffic_watch": "мониторинг трафика — v1.x, отложено (06 §1.2)",
    "unknown": "нет соответствия в SvBG",
    "no_registry": "в реестре SvBG нет настройки {key} — не перенесено (просьба к интеграции)",
    "tickets": "тикетов и NaloGO нет (06 §3.2)",
    "one_plan": "в SvBG один план, смены тарифа нет",
    "sync": "заменено сверкой SvBG раз в REMNAWAVE_SYNC_MINUTES (по умолчанию 60 мин)",
    "delete_mode": "автоудаление неактивных в SvBG не заложено — вопрос владельцу (06 §6)",
    "route": "маршрут задаётся SvBG ({route})",
    "texts": "тексты Bedolaga не переносятся (06 §2.1, «Строки»)",
    "data_whitelist": "переносится данными: белый список → ip_guard_exempt (импорт IP Guard)",
    "data_packs": "пакеты LTE — данные модуля LTE (импорт состояния LTE)",
    "lte_dropped": "не переносится: в SvBG нет аварийного удержания/снятия по выключению (06 §3.2)",
    "lte_kill": "в Bedolaga включён аварийный выключатель LTE — блоки не применялись; проверьте перед "
    "LTE_ENFORCE=on",
    "constant": "константа SvBG",
    "period_off": "период не продаётся (нет в AVAILABLE_SUBSCRIPTION_PERIODS)",
    "price_zero": "цена 0 — период не продаётся",
    "currency": "валюта кассы {cur} ≠ RUB — проверьте вручную",
    "per_cash_min": "лимит кассы в SvBG задаёт плагин (payment_instances.min_minor/max_minor), не настройка",
    "rolly_base": "адрес API RollyPay задаётся в настройках кассы SvBG, у Bedolaga он другой",
    "display": "название кассы — фиксированная строка плагина",
    "reports_chat": "отчёты идут в тему «Отчёты» единой админ-группы; чат отчётов Bedolaga отличается",
    "backup_chat": "бэкапы идут в тему «Бэкапы» единой админ-группы; чат бэкапов Bedolaga отличается",
    "backup_interval": "в SvBG бэкап раз в сутки в BACKUP_AT",
    "channel_many": "в SvBG один обязательный канал: взят первый активный",
    "channel_id": "ID канала «{v}» не число — укажите канал в мастере",
    "template": "шаблон имени панели «{v}» не вида <префикс>{{telegram_id}}",
    "unchanged_key": "учтено при расчёте других настроек",
    "russian_only": "бот работает только на русском, настройки языка не переносятся",
    "renewal": "периоды продления отличаются от периодов покупки — в SvBG они общие",
    "sales_mode": "режим продаж «{v}» не classic — в SvBG один план",
    "traffic_mode": "режим выбора трафика «{v}» не fixed — в SvBG трафик задаётся планом",
    "support_mode": "режим поддержки «{v}»: тикетов в SvBG нет, используется ссылка",
    "admin_chat_off": "уведомления админам выключены в Bedolaga — ADMIN_CHAT_ID не задан",
    "lte_scope": "неизвестный ENFORCE_SCOPE «{v}» — оставлен shadow",
}

# Dead keys and disabled subsystems (06 §3.4): exact names and prefixes.
_DEAD: Final = frozenset({
    "VERSION_CHECK_INTERVAL_H4OURS", "CHANNEL_SUB_ID", "CHANNEL_LINK", "REMNAWAVE_THROTTLE_SQUAD_UUID",
    "DISABLE_TOPUP_BUTTONS", "YOOKASSA_QUICK_AMOUNT_SELECTION_ENABLED", "APP_CONFIG_PATH",
    "PAYPEAR_PAYMENT_LIFETIME_MINUTES",
})  # fmt: skip
_DEAD_PREFIXES: Final = ("MODEM_", "VERSION_CHECK_")
_SUBSYSTEM_EXACT: Final = frozenset({
    "TRAFFIC_PACKAGES_CONFIG", "ENABLE_AUTOPAY", "DEBUG", "SQLITE_PATH", "REDIS_URL", "CART_TTL_SECONDS",
    "LOCALES_PATH", "ACTIVATE_BUTTON_VISIBLE", "SUBSCRIPTION_RENEWAL_BALANCE_THRESHOLD_KOPEKS",
    "EMAIL_DATE_FORMAT", "ENABLE_DEEP_LINKS", "APP_CONFIG_CACHE_TTL", "DISABLE_WEB_PAGE_PREVIEW",
    "BOT_USERNAME", "DEFAULT_AUTOPAY_ENABLED", "DEFAULT_AUTOPAY_DAYS_BEFORE",
})  # fmt: skip
_SUBSYSTEM_PREFIXES: Final = (
    "CABINET_", "SMTP_", "TEST_EMAIL", "WEB_API_", "MINIAPP_", "HAPP_", "GRACE_", "SIMPLE_SUBSCRIPTION_",
    "TRAFFIC_TOPUP_", "BASE_PROMO_GROUP_", "AUTOPAY_", "NALOGO_", "BLACKLIST_", "BAN_SYSTEM_",
    "SERVER_STATUS_", "SUPPORT_TICKET_", "LOG_", "DATABASE_", "POSTGRES_", "CONTESTS_", "TARIFF_SWITCH_",
    "WHEEL_", "POLL_", "NEWS_", "LANDING_", "EMAIL_", "APPLE_", "DONUT_", "COUPON",
)  # fmt: skip
#: Bedolaga cash desks without a SvBG plugin (``*_payments`` tables of the schema dump).
_OTHER_CASH: Final = (
    "YOOKASSA", "PLATEGA", "HELEKET", "TABPAY", "MULENPAY", "CISPAY", "WATA", "PAYPEAR", "FREEKASSA",
    "ETOPLATEZHI", "OVERPAY", "PARITYPAY", "SEVERPAY", "LAVA", "PAL24", "RIOPAY", "AURAPAY", "JUPITER",
    "ANTILOPAY", "CLOUDPAYMENTS", "KASSA_AI", "TRIBUTE", "DIGISELLER",
)  # fmt: skip
_JOB_KEYS: Final = ("MONITORING_", "NOTIFICATION_")

#: ``WEBHOOK_NOTIFY_*`` (12 switches of panel webhook notifications) → our ``NOTIFY_USER_*`` by keyword.
_NOTIFY_WORDS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("EXPIRING", "EXPIRES", "EXPIRE_SOON", "BEFORE_EXPIR"), "NOTIFY_USER_EXPIRING"),
    (("EXPIRED",), "NOTIFY_USER_EXPIRED"),
    (("FIRST_CONNECT",), "NOTIFY_USER_FIRST_CONNECTED"),
    (("DEVICE", "HWID"), "NOTIFY_USER_DEVICES"),
    (("REVOK",), "NOTIFY_USER_REVOKED"),
    (("TRAFFIC", "BANDWIDTH", "LIMITED"), "NOTIFY_USER_TRAFFIC"),
)

# IP Guard: Bedolaga name → ours (05 §2.2.10, ``svbg.ext.ip_guard.config``).
_IP_GUARD: Final[Mapping[str, tuple[str, str]]] = {
    "IP_GUARD_WARN_IPS": ("IP_GUARD_WARN_IPS", "int"),
    "IP_GUARD_BLOCK_IPS": ("IP_GUARD_BLOCK_IPS", "int"),
    "IP_GUARD_WINDOW_MINUTES": ("IP_GUARD_WINDOW_MINUTES", "int"),
    "IP_GUARD_BLOCK_MIN_SUBNETS": ("IP_GUARD_MIN_SUBNETS", "int"),
    "IP_GUARD_BLOCK_CONFIRM_LIVE_IPS": ("IP_GUARD_CONFIRM_LIVE_IPS", "int"),
    "IP_GUARD_BLOCK_CONFIRM_CHECKS": ("IP_GUARD_CONFIRM_CHECKS", "int"),
    "IP_GUARD_SHARED_IP_USERS": ("IP_GUARD_SHARED_IP_USERS", "int"),
    "IP_GUARD_IGNORE_CIDRS": ("IP_GUARD_IGNORE_CIDRS", "list"),
    "IP_GUARD_NOTIFY_USER": ("IP_GUARD_NOTIFY_USER", "bool"),
    "IP_GUARD_UNBLOCK_GRACE_MINUTES": ("IP_GUARD_UNBLOCK_GRACE_MINUTES", "int"),
    "IP_GUARD_WARN_COOLDOWN_MINUTES": ("IP_GUARD_WARN_COOLDOWN_MINUTES", "int"),
    "IP_GUARD_IPV6_PREFIX": ("IP_GUARD_IPV6_PREFIX", "int"),
    "IP_GUARD_SUSTAINED_BLOCK_CHECKS": ("IP_GUARD_SUSTAINED_CHECKS", "int"),
    "IP_GUARD_MAX_BLOCKS_PER_RUN": ("IP_GUARD_MAX_BLOCKS_PER_RUN", "int"),
    "IP_GUARD_MAX_BLOCKS_PER_HOUR": ("IP_GUARD_MAX_BLOCKS_PER_HOUR", "int"),
    "IP_GUARD_QUARANTINE_MAX_MINUTES": ("IP_GUARD_QUARANTINE_MAX_MINUTES", "int"),
    "IP_GUARD_MAX_WARNINGS_PER_RUN": ("IP_GUARD_MAX_WARNINGS_PER_RUN", "int"),
    "IP_GUARD_PIN_MESSAGES": ("IP_GUARD_PIN_MESSAGES", "bool"),
    "IP_GUARD_NODE_CONCURRENCY": ("IP_GUARD_NODE_CONCURRENCY", "int"),
}

# LTE: Bedolaga WL_QUOTA_* (env/system_settings) and the wlq_settings override key → ours (05 §2.1).
_LTE: Final[Mapping[str, tuple[str, str, str | None]]] = {
    "WL_QUOTA_WARN_PERCENT": ("LTE_WARN_PERCENT", "int", "warn_percent"),
    "WL_QUOTA_USER_QUIET_HOURS": ("LTE_QUIET_HOURS", "str", "user_quiet_hours"),
    "WL_QUOTA_NOTIFY_USER": ("LTE_NOTIFY_USER", "bool", None),  # + wlq notify_user_off (inverted)
    "WL_QUOTA_TOPUP_ENABLED": ("LTE_TOPUP_ENABLED", "bool", None),  # + wlq topup_kill_switch
    "WL_QUOTA_TOPUP_MIN_COVERAGE_HOURS": ("LTE_TOPUP_MIN_COVERAGE_HOURS", "int", None),
    "WL_QUOTA_ADMIN_NOTIFY_BLOCKS": ("LTE_ADMIN_NOTIFY_BLOCKS", "bool", "admin_notify_blocks"),
    "WL_QUOTA_ADMIN_NOTIFY_TOPUPS": ("LTE_ADMIN_NOTIFY_TOPUPS", "bool", "admin_notify_topups"),
    "WL_QUOTA_RENEWAL_GRACE_HOURS": ("LTE_RENEWAL_GRACE_HOURS", "int", "renewal_grace_hours"),
    "WL_QUOTA_ROLLOVER_MIN_REMAINING_HOURS": (
        "LTE_ROLLOVER_MIN_REMAINING_HOURS", "int", "rollover_min_remaining_hours"),
    "WL_QUOTA_MAX_NEW_BLOCKS_PER_CYCLE": ("LTE_MAX_NEW_BLOCKS_PER_CYCLE", "int", "max_new_blocks_per_cycle"),
    "WL_QUOTA_QUARANTINE_NEW_BLOCKS_PER_CYCLE": (
        "LTE_QUARANTINE_NEW_BLOCKS", "int", "quarantine_new_blocks_per_cycle"),
    "WL_QUOTA_MAX_BLOCKED_SHARE_PERCENT": ("LTE_MAX_BLOCKED_SHARE_PCT", "int", "max_blocked_share_percent"),
    "WL_QUOTA_SANITY_MAX_MBPS": ("LTE_SANITY_MAX_MBPS", "int", None),
    "WL_QUOTA_BOUNDARY_SNAP_SECONDS": ("LTE_BOUNDARY_SNAP_S", "int", None),
    "WL_QUOTA_WRITE_LAG_SECONDS": ("LTE_WRITE_LAG_S", "int", None),
    "WL_QUOTA_MAX_CATCHUP_DAYS": ("LTE_MAX_CATCHUP_DAYS", "int", None),
    "WL_QUOTA_GB_BYTES": ("LTE_GB_BYTES", "int", None),
}  # fmt: skip
#: wlq_settings keys consumed by the special rules (not by ``_LTE``).
_WLQ_SPECIAL: Final = frozenset({"notify_user_off", "topup_kill_switch", "kill_switch"})
#: Owner's LTE settings that the SvBG LTE module (``svbg.ext.lte.service.SETTINGS``) does not have: env/system
#: key, the ``wlq_settings`` override key, reason («не перенесено»).
_LTE_NO_ANALOG: Final[tuple[tuple[str, str | None, str], ...]] = (
    ("WL_QUOTA_ADMIN_SUMMARY_TIME", "admin_summary_time",
     "отдельной сводки LTE в SvBG нет: итоги LTE — в ежедневном отчёте (REPORT_DAILY_AT)"),
    ("WL_QUOTA_PANEL_DESCRIPTION_ENABLED", None,
     "счётчика LTE в описании пользователя панели в SvBG нет — описания не переписываются"),
    ("WL_QUOTA_PANEL_DESCRIPTION_FORMAT", None,
     "счётчика LTE в описании пользователя панели в SvBG нет — описания не переписываются"),
    ("WL_QUOTA_NEW_NODE_TWIN_DELAY_MINUTES", "new_node_twin_delay_minutes",
     "задержки двойника новой ноды в SvBG нет: двойники — данные модуля LTE (lte_twins)"),
)  # fmt: skip

# --------------------------------------------------------------------------------------- data classes


@dataclass(slots=True)
class BedolagaSettingsSource:
    """What the importer read: ``.env`` (+ its line numbers), ``system_settings``, ``wlq_settings``,
    ``required_channels``. Build it with :meth:`from_env_text` / :meth:`from_env_file` +
    :func:`read_source_tables`."""

    env: Mapping[str, str] = field(default_factory=dict)
    env_lines: Mapping[str, int] = field(default_factory=dict)
    system_settings: Mapping[str, str | None] = field(default_factory=dict)
    wlq_settings: Mapping[str, Any] = field(default_factory=dict)
    required_channels: Sequence[Mapping[str, Any]] = ()

    @classmethod
    def from_env_text(cls, text: str, **tables: Any) -> BedolagaSettingsSource:
        parsed = parse_env(text)
        return cls(env=parsed.values, env_lines=parsed.lines, **tables)

    @classmethod
    def from_env_file(cls, path: str | Path, **tables: Any) -> BedolagaSettingsSource:
        return cls.from_env_text(Path(path).read_text(encoding="utf-8-sig"), **tables)


@dataclass(slots=True)
class CatalogImport:
    """The paid plan and the trial plan as Bedolaga sold them (classic mode, 06 §3.2 → ``plans`` data)."""

    currency: str = "RUB"
    prices: dict[int, int] = field(default_factory=dict)  # days → minor units
    device_limit: int | None = None
    addon_price_minor: int | None = None  # per extra device per 30 days
    addon_max_devices: int | None = None
    traffic_bytes: int | None = None
    reset_strategy: str | None = None
    traffic_on_renew: str | None = None
    devices_on_renew: str | None = None
    panel_tag: str | None = None
    trial_device_limit: int | None = None
    trial_traffic_bytes: int | None = None
    trial_panel_tag: str | None = None
    sources: set[str] = field(default_factory=set)

    @property
    def device_addon(self) -> DeviceAddon | None:
        if not self.addon_price_minor:
            return None
        return DeviceAddon(self.addon_price_minor, 30, self.addon_max_devices, self.currency)

    def as_dict(self) -> dict[str, Any]:
        addon = self.device_addon
        return {
            "currency": self.currency,
            "prices": {str(d): p for d, p in sorted(self.prices.items())},
            "device_limit": self.device_limit,
            "device_addon": None if addon is None else addon.to_json(),
            "traffic_bytes": self.traffic_bytes,
            "reset_strategy": self.reset_strategy,
            "traffic_on_renew": self.traffic_on_renew,
            "devices_on_renew": self.devices_on_renew,
            "panel_tag": self.panel_tag,
            "trial": {
                "device_limit": self.trial_device_limit,
                "traffic_bytes": self.trial_traffic_bytes,
                "panel_tag": self.trial_panel_tag,
            },
        }


@dataclass(frozen=True, slots=True)
class TopicImport:
    kind: str
    chat_id: int
    thread_id: int
    source: str


@dataclass(slots=True)
class SettingsImportPlan:
    changes: list[PlannedChange] = field(default_factory=list)
    deferred: list[PlannedChange] = field(default_factory=list)
    catalog: CatalogImport = field(default_factory=CatalogImport)
    topics: list[TopicImport] = field(default_factory=list)
    cdn_nodes: list[str] = field(default_factory=list)
    not_transferred: list[NotTransferred] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_keys: frozenset[str] = frozenset()
    consumed: set[str] = field(default_factory=set)

    def change(self, key: str) -> PlannedChange | None:
        return next((c for c in self.changes if c.key == key), None)

    def deferred_change(self, key: str) -> PlannedChange | None:
        return next((c for c in self.deferred if c.key == key), None)

    def skipped(self, key: str) -> NotTransferred | None:
        return next((n for n in self.not_transferred if n.key == key), None)

    def unaccounted(self) -> set[str]:
        """Source keys that are neither consumed by a rule nor listed as «не перенесено» (must be empty)."""
        listed = {n.key for n in self.not_transferred}
        return set(self.source_keys) - self.consumed - listed

    def report(self) -> dict[str, Any]:
        """JSON-able summary for ``import_runs.report`` (secret values never included)."""

        def _ch(c: PlannedChange) -> dict[str, Any]:
            return {
                "key": c.key,
                "value": "•••" if c.secret else c.raw,
                "from": list(c.sources),
                **({"note": c.note} if c.note else {}),
            }

        return {
            "changes": [_ch(c) for c in self.changes],
            "deferred": [_ch(c) for c in self.deferred],
            "catalog": self.catalog.as_dict(),
            "topics": [{"kind": t.kind, "from": t.source} for t in self.topics],
            "cdn_nodes": len(self.cdn_nodes),
            "not_transferred": [
                {"key": n.key, "reason": n.reason, "origin": n.origin, "category": n.category}
                for n in sorted(self.not_transferred, key=lambda n: (n.category, n.key))
            ],
            "warnings": list(self.warnings),
            "counts": {
                "source_keys": len(self.source_keys),
                "changes": len(self.changes),
                "deferred": len(self.deferred),
                "not_transferred": len(self.not_transferred),
            },
        }

    def render_not_transferred(self) -> str:
        """Owner-facing list «Не перенесено» (Russian), grouped by reason."""
        groups: dict[str, list[str]] = {}
        for n in sorted(self.not_transferred, key=lambda n: n.key):
            groups.setdefault(n.reason, []).append(n.key)
        lines = ["Не перенесено:"]
        for reason, keys in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"• {reason}: {', '.join(keys)}")
        return "\n".join(lines)


# ------------------------------------------------------------------------------------------ reading


async def read_source_tables(conn: Any) -> dict[str, Any]:
    """Read ``system_settings``, ``wlq_settings``, ``required_channels`` through the importer's read-only
    asyncpg connection (anything with ``fetch``/``fetchval``). Missing tables give empty results.
    Returns keyword arguments for :class:`BedolagaSettingsSource`."""
    out: dict[str, Any] = {"system_settings": {}, "wlq_settings": {}, "required_channels": []}
    if await _has_table(conn, "system_settings"):
        rows = await conn.fetch("SELECT key, value FROM system_settings ORDER BY key, id")
        out["system_settings"] = {str(r["key"]): r["value"] for r in rows}
    if await _has_table(conn, "wlq_settings"):
        rows = await conn.fetch("SELECT key, value FROM wlq_settings ORDER BY key")
        out["wlq_settings"] = {str(r["key"]): _json(r["value"]) for r in rows}
    if await _has_table(conn, "required_channels"):
        rows = await conn.fetch(
            "SELECT id, channel_id, channel_link, title, is_active, sort_order, disable_trial_on_leave,"
            " disable_paid_on_leave FROM required_channels ORDER BY sort_order, id"
        )
        out["required_channels"] = [dict(r) for r in rows]
    return out


async def _has_table(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{name}"))


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _wlq_value(value: Any) -> Any:
    """A ``wlq_settings`` value: ``{"value": x}``, ``{"on": x}`` or ``x`` (owner's ``setting_value``)."""
    value = _json(value)
    if isinstance(value, Mapping):
        if "value" in value:
            return value["value"]
        if "on" in value:
            return value["on"]
    return value


# ------------------------------------------------------------------------------------------ building


class _Ctx:
    def __init__(self, src: BedolagaSettingsSource) -> None:
        self.vals: dict[str, SourceValue] = effective_values(
            ("env", src.env, src.env_lines), ("system_settings", src.system_settings, None)
        )
        self.wlq: dict[str, Any] = dict(src.wlq_settings)
        self.channels = list(src.required_channels)
        self.plan = SettingsImportPlan(
            source_keys=frozenset(self.vals) | frozenset(f"wlq_settings.{k}" for k in self.wlq)
        )

    # ---- reading
    def has(self, key: str) -> bool:
        return key in self.vals

    def get(self, key: str) -> SourceValue | None:
        sv = self.vals.get(key)
        if sv is not None:
            self.plan.consumed.add(key)
        return sv

    def raw(self, key: str) -> str | None:
        sv = self.get(key)
        return None if sv is None or sv.raw is None else sv.raw.strip()

    def present(self, key: str) -> str | None:
        """The value when set and non-empty; an empty value is listed as «пусто» and returns None."""
        sv = self.get(key)
        if sv is None:
            return None
        text = (sv.raw or "").strip()
        if not text:
            self.skip(key, _R["empty"], "skipped")
            return None
        return text

    def conv(self, key: str, fn: Callable[[str | None], Any]) -> Any:
        """``fn(value)``; a bad value is listed as invalid and returns None."""
        text = self.present(key)
        if text is None:
            return None
        try:
            return fn(text)
        except ValueParseError as exc:
            self.skip(key, f"неверное значение: {exc}", "invalid")
            return None

    def origin(self, key: str) -> str:
        sv = self.vals.get(key)
        return sv.origin if sv is not None else ("wlq_settings" if key.startswith("wlq_settings.") else "")

    # ---- results
    def set(self, our: str, raw: Any, *sources: str, secret: bool = False, note: str = "") -> None:
        self.plan.changes = [c for c in self.plan.changes if c.key != our]
        self.plan.changes.append(PlannedChange(our, _text(raw), tuple(sources), secret, note))
        self.plan.consumed.update(sources)

    def defer(self, our: str, raw: Any, *sources: str, note: str) -> None:
        self.plan.deferred.append(PlannedChange(our, _text(raw), tuple(sources), False, note))
        self.plan.consumed.update(sources)

    def skip(self, key: str, reason: str, category: str = "skipped") -> None:
        if self.plan.skipped(key) is None:
            self.plan.not_transferred.append(NotTransferred(key, reason, self.origin(key), category))
        self.plan.consumed.discard(key)

    def note(self, key: str) -> None:
        """Mark a key as used by another rule (no own change)."""
        if key in self.vals and self.plan.skipped(key) is None:
            self.plan.consumed.add(key)

    def secret(self, key: str, our: str, *, note: str = "") -> None:
        text = self.present(key)
        if text is None:
            return
        if is_masked(text):
            self.skip(key, _R["masked"], "secret")
            return
        self.set(our, text, key, secret=True, note=note)

    def warn(self, text: str) -> None:
        if text not in self.plan.warnings:
            self.plan.warnings.append(text)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def build_plan(source: BedolagaSettingsSource) -> SettingsImportPlan:
    """Compute the whole settings import (pure: no I/O)."""
    ctx = _Ctx(source)
    for rule in (
        _boot,
        _support,
        _admin_chat,
        _channel,
        _remnawave,
        _notify,
        _catalog,
        _referral,
        _onboarding_i18n,
        _payments,
        _ops,
        _telegram,
        _ip_guard,
        _lte,
    ):
        rule(ctx)
    _classify_rest(ctx)
    return ctx.plan


# ---- rules


def _boot(ctx: _Ctx) -> None:
    if ctx.get("BOT_TOKEN") is not None:
        ctx.skip("BOT_TOKEN", _R["bot_token"], "secret")
    ids = ctx.conv("ADMIN_IDS", to_int_list)
    if ids:
        ctx.set("OWNER_IDS", ids, "ADMIN_IDS")
    tz = ctx.present("TZ")
    if tz is not None:
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            ctx.skip("TZ", f"неизвестный часовой пояс «{tz}»", "invalid")
        else:
            ctx.set("TIMEZONE", tz, "TZ", note="в Bedolaga ключ мёртвый, но смысл нужен (06 §3.2)")


def _support(ctx: _Ctx) -> None:
    enabled = ctx.conv("SUPPORT_MENU_ENABLED", to_bool) if ctx.has("SUPPORT_MENU_ENABLED") else True
    mode = ctx.raw("SUPPORT_SYSTEM_MODE")
    if mode and mode.lower() not in ("contact", ""):
        ctx.warn(_R["support_mode"].format(v=mode))
    name = ctx.present("SUPPORT_USERNAME")
    if name is None:
        return
    if enabled is False:
        ctx.skip("SUPPORT_USERNAME", "кнопка поддержки выключена в Bedolaga (SUPPORT_MENU_ENABLED=false)")
        return
    if name.lower().startswith(("http://", "https://", "tg://")):
        url = name
    else:
        handle = name.removeprefix("@").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", handle):
            ctx.skip("SUPPORT_USERNAME", f"не похоже на имя Telegram: «{name}»", "invalid")
            return
        url = f"https://t.me/{handle}"
    ctx.set(
        "SUPPORT_URL", url, "SUPPORT_USERNAME", *_present(ctx, "SUPPORT_MENU_ENABLED", "SUPPORT_SYSTEM_MODE")
    )


def _present(ctx: _Ctx, *keys: str) -> tuple[str, ...]:
    return tuple(k for k in keys if ctx.has(k))


def _admin_chat(ctx: _Ctx) -> None:
    enabled = ctx.conv("ADMIN_NOTIFICATIONS_ENABLED", to_bool)
    chat = ctx.conv("ADMIN_NOTIFICATIONS_CHAT_ID", to_int)
    if chat is not None:
        if enabled is False:
            ctx.skip("ADMIN_NOTIFICATIONS_CHAT_ID", _R["admin_chat_off"])
        else:
            ctx.set("ADMIN_CHAT_ID", chat, "ADMIN_NOTIFICATIONS_CHAT_ID",
                    *_present(ctx, "ADMIN_NOTIFICATIONS_ENABLED"))  # fmt: skip
    for key in ("ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID", "ADMIN_NOTIFICATIONS_NALOG_TOPIC_ID"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["tickets"])
    _topic(ctx, "ADMIN_NOTIFICATIONS_TOPIC_ID", "payments", chat)
    # Reports (06 §3.2): time in owner.timezone; the chat must be the admin group.
    rep = ctx.conv("ADMIN_REPORTS_ENABLED", to_bool)
    if rep is not None:
        ctx.set("REPORT_DAILY_ENABLED", rep, "ADMIN_REPORTS_ENABLED")
    _time(ctx, "ADMIN_REPORTS_SEND_TIME", "REPORT_DAILY_AT")
    rchat = ctx.conv("ADMIN_REPORTS_CHAT_ID", to_int)
    if rchat is not None and chat is not None and rchat != chat:
        ctx.skip("ADMIN_REPORTS_CHAT_ID", _R["reports_chat"], "conflict")
        ctx.warn(_R["reports_chat"])
    _topic(ctx, "ADMIN_REPORTS_TOPIC_ID", "reports", chat if rchat is None or rchat == chat else None)
    # Backups.
    on = ctx.conv("BACKUP_AUTO_ENABLED", to_bool)
    if on is not None:
        ctx.set("BACKUP_ENABLED", on, "BACKUP_AUTO_ENABLED")
    if ctx.present("BACKUP_INTERVAL_HOURS") is not None:
        ctx.skip("BACKUP_INTERVAL_HOURS", _R["backup_interval"], "conflict")
    _time(ctx, "BACKUP_TIME", "BACKUP_AT")
    keep = ctx.conv("BACKUP_MAX_KEEP", to_int)
    if keep is not None:
        ctx.set("BACKUP_KEEP", keep, "BACKUP_MAX_KEEP")
    send = ctx.conv("BACKUP_SEND_ENABLED", to_bool)
    if send is not None:
        ctx.set("BACKUP_TO_TELEGRAM", send, "BACKUP_SEND_ENABLED")
    bchat = ctx.conv("BACKUP_SEND_CHAT_ID", to_int)
    if bchat is not None and chat is not None and bchat != chat:
        ctx.skip("BACKUP_SEND_CHAT_ID", _R["backup_chat"], "conflict")
    _topic(ctx, "BACKUP_SEND_TOPIC_ID", "backups", chat if bchat is None or bchat == chat else None)
    ctx.secret("BACKUP_ARCHIVE_PASSWORD", "BACKUP_PASSWORD")
    # Suspicious traffic monitoring: v1.x.
    for key in list(ctx.vals):
        if key == "SUSPICIOUS_NOTIFICATIONS_TOPIC_ID" or key.startswith(("TRAFFIC_FAST_", "TRAFFIC_DAILY_")):
            ctx.get(key)
            ctx.skip(key, _R["traffic_watch"], "deferred")


def _topic(ctx: _Ctx, key: str, kind: str, chat: int | None) -> None:
    thread = ctx.conv(key, to_int)
    if thread is None:
        return
    if chat is None:
        ctx.skip(key, "тема в другом чате или чат уведомлений не задан — бот создаст тему сам", "conflict")
        return
    ctx.plan.topics.append(TopicImport(kind, chat, thread, key))


def _time(ctx: _Ctx, key: str, our: str) -> None:
    text = ctx.present(key)
    if text is None:
        return
    m = _HHMM_RE.fullmatch(text)
    if m is None:
        ctx.skip(key, f"время «{text}» не в формате ЧЧ:ММ", "invalid")
        return
    ctx.set(our, f"{int(m.group(1)):02d}:{m.group(2)}", key)


def _channel(ctx: _Ctx) -> None:
    for dead in ("CHANNEL_SUB_ID", "CHANNEL_LINK"):
        if ctx.get(dead) is not None:
            ctx.skip(dead, _R["dead"] + ": канал берётся из таблицы required_channels", "dead")
    required = ctx.conv("CHANNEL_IS_REQUIRED_SUB", to_bool)
    for_all = ctx.conv("CHANNEL_REQUIRED_FOR_ALL", to_bool)
    trial_off = ctx.conv("CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE", to_bool)
    src = _present(
        ctx, "CHANNEL_IS_REQUIRED_SUB", "CHANNEL_REQUIRED_FOR_ALL", "CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE"
    )
    active = [c for c in ctx.channels if c.get("is_active", True)]
    if not required or not active:
        if required and not active:
            ctx.warn("CHANNEL_IS_REQUIRED_SUB=true, но активного канала в required_channels нет")
        return
    if len(active) > 1:
        ctx.warn(_R["channel_many"])
    row = active[0]
    raw_id = str(row.get("channel_id") or "").strip()
    try:
        channel_id = to_int(raw_id)
    except ValueParseError:
        ctx.warn(_R["channel_id"].format(v=raw_id))
        return
    ctx.set("REQUIRED_CHANNEL_ID", channel_id, *src, note="required_channels")
    link = str(row.get("channel_link") or "").strip()
    if link.startswith("@"):
        link = f"https://t.me/{link[1:]}"
    if link.startswith(("https://", "http://")):
        ctx.set("REQUIRED_CHANNEL_URL", link, *src, note="required_channels")
    ctx.set("CHANNEL_REQUIRED_FOR", "all" if for_all else "trial", *src)
    paid_off = bool(row.get("disable_paid_on_leave"))
    trial_leave = bool(row.get("disable_trial_on_leave", True)) and trial_off is not False
    action = "all" if paid_off else ("trial" if trial_leave else "off")
    ctx.set("CHANNEL_LEAVE_ACTION", action, *src)


def _remnawave(ctx: _Ctx) -> None:
    url = ctx.present("REMNAWAVE_API_URL")
    if url is not None:
        ctx.set("REMNAWAVE_URL", url, "REMNAWAVE_API_URL")
    for key in ("REMNAWAVE_API_KEY", "REMNAWAVE_AUTH_TYPE", "REMNAWAVE_SECRET_KEY", "REMNAWAVE_USERNAME",
                "REMNAWAVE_PASSWORD", "REMNAWAVE_CADDY_TOKEN"):  # fmt: skip
        if ctx.get(key) is not None:
            ctx.skip(key, _R["panel_token"], "secret")
    hook_on = ctx.conv("REMNAWAVE_WEBHOOK_ENABLED", to_bool)
    if hook_on is False:
        if ctx.get("REMNAWAVE_WEBHOOK_SECRET") is not None:
            ctx.skip("REMNAWAVE_WEBHOOK_SECRET", "вебхуки панели выключены в Bedolaga")
    else:
        ctx.secret(
            "REMNAWAVE_WEBHOOK_SECRET",
            "REMNAWAVE_WEBHOOK_SECRET",
            note="режим B: существующий секрет (02 §5.7); ротация — после удаления старого URL",
        )
    if ctx.get("REMNAWAVE_WEBHOOK_PATH") is not None:
        ctx.skip("REMNAWAVE_WEBHOOK_PATH", _R["route"].format(route="/webhooks/remnawave"))
    nodes = ctx.conv("REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS", to_bool)
    if nodes is not None:
        ctx.set("NOTIFY_ADMIN_NODES", nodes, "REMNAWAVE_WEBHOOK_NOTIFY_NODE_CONNECTION_STATUS")
    tpl = ctx.present("REMNAWAVE_USER_USERNAME_TEMPLATE")
    if tpl is not None:
        m = re.fullmatch(r"([A-Za-z0-9_\-]*)\{telegram_id\}", tpl)
        if m is None:
            ctx.skip("REMNAWAVE_USER_USERNAME_TEMPLATE", _R["template"].format(v=tpl), "conflict")
        else:
            ctx.set("PANEL_USERNAME_PREFIX", m.group(1), "REMNAWAVE_USER_USERNAME_TEMPLATE")
    desc = ctx.present("REMNAWAVE_USER_DESCRIPTION_TEMPLATE")
    if desc is not None:
        converted = desc.replace("@{username}", "{username}").replace("{username}", "@{tg_username}")
        ctx.set("PANEL_DESCRIPTION_TEMPLATE", converted, "REMNAWAVE_USER_DESCRIPTION_TEMPLATE",
                note="пишется только при создании пользователя панели")  # fmt: skip
    for key in ("REMNAWAVE_USER_DELETE_MODE", "INACTIVE_USER_DELETE_MONTHS"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["delete_mode"], "conflict")
    for key in ("REMNAWAVE_AUTO_SYNC_ENABLED", "REMNAWAVE_AUTO_SYNC_TIMES"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["sync"])


def _notify(ctx: _Ctx) -> None:
    keys = sorted(k for k in ctx.vals if k.startswith("WEBHOOK_NOTIFY_"))
    master = True
    for key in keys:
        if key in ("WEBHOOK_NOTIFY_USER_ENABLED", "WEBHOOK_NOTIFY_ENABLED"):
            value = ctx.conv(key, to_bool)
            master = master and value is not False
    targets: dict[str, list[tuple[str, bool]]] = {}
    for key in keys:
        if key in ("WEBHOOK_NOTIFY_USER_ENABLED", "WEBHOOK_NOTIFY_ENABLED"):
            continue
        suffix = key.removeprefix("WEBHOOK_NOTIFY_")
        our = next((o for words, o in _NOTIFY_WORDS if any(w in suffix for w in words)), None)
        if our is None:
            ctx.get(key)
            ctx.skip(key, _R["unknown"] + " (уведомление панели без аналога)", "unknown")
            continue
        value = ctx.conv(key, to_bool)
        if value is not None:
            targets.setdefault(our, []).append((key, value))
    for our, items in targets.items():
        on = master and any(v for _, v in items)  # one of the merged switches is on → the message is sent
        ctx.set(our, on, *(k for k, _ in items), *_present(ctx, "WEBHOOK_NOTIFY_USER_ENABLED"))
    hours = ctx.conv("TRIAL_WARNING_HOURS", to_int)
    if hours is not None:
        ctx.set("NOTIFY_TRIAL_ENDING_HOURS", hours, "TRIAL_WARNING_HOURS")


def _gib(ctx: _Ctx, key: str) -> int | None:
    gb = ctx.conv(key, to_int)
    if gb is None:
        return None
    if gb < 0:
        ctx.skip(key, "отрицательный лимит трафика", "invalid")
        return None
    return gb * GIB


def _tag(ctx: _Ctx, key: str) -> str | None:
    text = ctx.present(key)
    if text is None:
        return None
    if not _TAG_RE.fullmatch(text):
        ctx.skip(key, f"тег «{text}» не подходит панели (A–Z, 0–9, _, до 16)", "invalid")
        return None
    return text


def _catalog(ctx: _Ctx) -> None:
    cat = ctx.plan.catalog

    def used(*keys: str) -> None:
        cat.sources.update(k for k in keys if ctx.has(k))
        for k in keys:
            ctx.note(k)

    mode = ctx.raw("SALES_MODE")
    if mode is not None:
        if mode.lower() not in ("classic", ""):
            ctx.skip("SALES_MODE", _R["sales_mode"].format(v=mode), "conflict")
        else:
            used("SALES_MODE")
    # Trial: TRIAL_* only (06 §3.2); devices/traffic go to the trial plan.
    days = ctx.conv("TRIAL_DURATION_DAYS", to_int)
    if days is not None:
        ctx.set("TRIAL_DAYS", days, "TRIAL_DURATION_DAYS")
    carry = ctx.conv("TRIAL_ADD_REMAINING_DAYS_TO_PAID", to_bool)
    if carry is not None:
        ctx.set("TRIAL_CARRY_OVER", carry, "TRIAL_ADD_REMAINING_DAYS_TO_PAID")
    cat.trial_device_limit = ctx.conv("TRIAL_DEVICE_LIMIT", to_int)
    cat.trial_traffic_bytes = _gib(ctx, "TRIAL_TRAFFIC_LIMIT_GB")
    cat.trial_panel_tag = _tag(ctx, "TRIAL_USER_TAG")
    used("TRIAL_DEVICE_LIMIT", "TRIAL_TRAFFIC_LIMIT_GB", "TRIAL_USER_TAG")
    # Paid plan.
    cat.panel_tag = _tag(ctx, "PAID_SUBSCRIPTION_USER_TAG")
    cat.device_limit = ctx.conv("DEFAULT_DEVICE_LIMIT", to_int)
    selection = ctx.conv("DEVICES_SELECTION_ENABLED", to_bool)
    price = ctx.conv("PRICE_PER_DEVICE", to_int)
    cap = ctx.conv("MAX_DEVICES_LIMIT", to_int)
    if selection is not False and price and price > 0:
        cat.addon_price_minor = price
        cat.addon_max_devices = cap if cap and cap > 0 else None
    used(
        "PAID_SUBSCRIPTION_USER_TAG",
        "DEFAULT_DEVICE_LIMIT",
        "DEVICES_SELECTION_ENABLED",
        "PRICE_PER_DEVICE",
        "MAX_DEVICES_LIMIT",
    )
    reset_dev = ctx.conv("RESET_DEVICES_ON_RENEWAL", to_bool)
    if reset_dev is not None:
        cat.devices_on_renew = "reset" if reset_dev else "keep"
    strategy = ctx.present("DEFAULT_TRAFFIC_RESET_STRATEGY")
    if strategy is not None:
        norm = strategy.upper()
        if norm in RESET_STRATEGIES:
            cat.reset_strategy = norm
        else:
            ctx.skip("DEFAULT_TRAFFIC_RESET_STRATEGY", f"неизвестная стратегия «{strategy}»", "invalid")
    reset_traffic = ctx.conv("RESET_TRAFFIC_ON_PAYMENT", to_bool)
    if reset_traffic is not None:
        cat.traffic_on_renew = "reset" if reset_traffic else "keep"
    cat.traffic_bytes = _gib(ctx, "FIXED_TRAFFIC_LIMIT_GB")
    tmode = ctx.raw("TRAFFIC_SELECTION_MODE")
    if tmode is not None and tmode.lower() not in ("fixed", ""):
        ctx.skip("TRAFFIC_SELECTION_MODE", _R["traffic_mode"].format(v=tmode), "conflict")
    used("RESET_DEVICES_ON_RENEWAL", "DEFAULT_TRAFFIC_RESET_STRATEGY", "RESET_TRAFFIC_ON_PAYMENT",
         "FIXED_TRAFFIC_LIMIT_GB", "TRAFFIC_SELECTION_MODE")  # fmt: skip
    # Prices: AVAILABLE_SUBSCRIPTION_PERIODS × PRICE_<N>_DAYS (kopeks 1:1).
    periods = ctx.conv("AVAILABLE_SUBSCRIPTION_PERIODS", to_int_list)
    renewal = ctx.conv("AVAILABLE_RENEWAL_PERIODS", to_int_list)
    price_keys = sorted((int(m.group(1)), k) for k in ctx.vals if (m := _PRICE_RE.fullmatch(k)))
    sellable = set(periods) if periods else {d for d, _ in price_keys}
    for days, key in price_keys:
        amount = ctx.conv(key, to_int)
        if amount is None:
            continue
        if days not in sellable:
            ctx.skip(key, _R["period_off"])
        elif amount <= 0:
            ctx.skip(key, _R["price_zero"])
        elif not 1 <= days <= 3650:
            ctx.skip(key, "период вне 1–3650 дней", "invalid")
        else:
            cat.prices[days] = amount
            used(key)
    if periods:
        missing = sorted(set(periods) - set(cat.prices))
        if missing:
            ctx.warn(f"нет цены для периодов {missing} из AVAILABLE_SUBSCRIPTION_PERIODS — они не продаются")
    if renewal and periods and set(renewal) != set(periods):
        ctx.warn(_R["renewal"])
    used("AVAILABLE_SUBSCRIPTION_PERIODS", "AVAILABLE_RENEWAL_PERIODS")
    rounding = ctx.conv("PRICE_ROUNDING_ENABLED", to_bool)
    if rounding is not None:
        ctx.set("PRICING_ROUNDING", rounding, "PRICE_ROUNDING_ENABLED")


def _referral(ctx: _Ctx) -> None:
    on = ctx.conv("REFERRAL_PROGRAM_ENABLED", to_bool)
    mode = ctx.present("REFERRAL_REWARD_MODE")
    if mode is not None and mode.lower() != "days":
        ctx.skip("REFERRAL_REWARD_MODE", _R["referral_money"] + f" (режим «{mode}»)", "conflict")
        if on:
            # days mode is not what the owner used: do not switch our referral on by the import
            ctx.warn("рефералка Bedolaga в денежном режиме: в SvBG включите режим вручную")
            on = None
    if on is not None:
        ctx.set("REFERRAL_ENABLED", on, "REFERRAL_PROGRAM_ENABLED")
    if mode is not None and mode.lower() == "days":
        ctx.set("REFERRAL_MODE", "days", "REFERRAL_REWARD_MODE")
    for src, our in (
        ("REFERRAL_DAYS_INVITER_DAYS", "REFERRAL_INVITER_DAYS"),
        ("REFERRAL_DAYS_INVITEE_DAYS", "REFERRAL_INVITEE_DAYS"),
        ("REFERRAL_DAYS_INVITER_MAX_PER_MONTH", "REFERRAL_INVITER_CAP_30D"),
        ("REFERRAL_DAYS_INVITER_MAX_TOTAL", "REFERRAL_INVITER_CAP_TOTAL"),
    ):
        value = ctx.conv(src, to_int)
        if value is not None:
            ctx.set(our, value, src)
    trigger = ctx.present("REFERRAL_DAYS_TRIGGER")
    if trigger is not None:
        mapped = {"any": "trial_or_paid", "trial_or_paid": "trial_or_paid", "paid": "paid",
                  "register": "register", "registration": "register"}.get(trigger.lower())  # fmt: skip
        if mapped is None:
            ctx.skip("REFERRAL_DAYS_TRIGGER", f"неизвестный триггер «{trigger}»", "invalid")
        else:
            ctx.set("REFERRAL_TRIGGER", mapped, "REFERRAL_DAYS_TRIGGER")
    if ctx.get("REFERRAL_DAYS_RETRY_SKIPPED_HOURS") is not None:
        ctx.skip("REFERRAL_DAYS_RETRY_SKIPPED_HOURS", _R["constant"] + " (168 ч)")
    for key in [k for k in ctx.vals if k.startswith("REFERRAL_DAYS_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            ctx.skip(key, _R["replaced_import"])
    for key in [k for k in ctx.vals if k.startswith("REFERRAL_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            ctx.skip(key, _R["referral_money"])


def _onboarding_i18n(ctx: _Ctx) -> None:
    skip_code = ctx.conv("SKIP_REFERRAL_CODE", to_bool)
    if skip_code is not None:
        ctx.set("ONBOARDING_ASK_REFERRAL_CODE", not skip_code, "SKIP_REFERRAL_CODE")
    skip_rules = ctx.conv("SKIP_RULES_ACCEPT", to_bool)
    if skip_rules is not None:
        ctx.set("ONBOARDING_RULES", "off" if skip_rules else "on", "SKIP_RULES_ACCEPT")
    for key in ("DEFAULT_LANGUAGE", "AVAILABLE_LANGUAGES", "LANGUAGE_SELECTION_ENABLED"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["russian_only"])


def _cash_enabled(ctx: _Ctx, key: str) -> bool | None:
    return ctx.conv(key, to_bool) if ctx.has(key) else None


def _payments(ctx: _Ctx) -> None:
    step8 = "включить на шаге 8 runbook T0 (06 §4.4), после переключения маршрутов Caddy"
    # Telegram Stars.
    stars = _cash_enabled(ctx, "TELEGRAM_STARS_ENABLED")
    if stars:
        ctx.defer("PAY_STARS_ENABLED", True, "TELEGRAM_STARS_ENABLED", note=step8)
    elif stars is False:
        ctx.set("PAY_STARS_ENABLED", False, "TELEGRAM_STARS_ENABLED")
    rate = ctx.present("TELEGRAM_STARS_RATE_RUB")
    if rate is not None and stars is not False:
        ctx.set("PAY_STARS_RATE", rate.replace(",", "."), "TELEGRAM_STARS_RATE_RUB")
    elif rate is not None:
        ctx.skip("TELEGRAM_STARS_RATE_RUB", _R["cash_off"])
    # CryptoBot.
    crypto = _cash_enabled(ctx, "CRYPTOBOT_ENABLED")
    crypto_keys = [k for k in ctx.vals if k.startswith("CRYPTOBOT_") and k != "CRYPTOBOT_ENABLED"]
    if crypto is False:
        ctx.set("PAY_CRYPTOBOT_ENABLED", False, "CRYPTOBOT_ENABLED")
        for key in crypto_keys:
            ctx.get(key)
            ctx.skip(key, _R["cash_off"], "dead")
    else:
        if crypto:
            ctx.defer("PAY_CRYPTOBOT_ENABLED", True, "CRYPTOBOT_ENABLED", note=step8)
        ctx.secret("CRYPTOBOT_API_TOKEN", "PAY_CRYPTOBOT_API_TOKEN")
        test = ctx.conv("CRYPTOBOT_TESTNET", to_bool)
        if test is not None:
            ctx.set("PAY_CRYPTOBOT_TEST_MODE", test, "CRYPTOBOT_TESTNET")
        assets = ctx.conv("CRYPTOBOT_ASSETS", to_str_list)
        if assets:
            ctx.set("PAY_CRYPTOBOT_ACCEPTED_ASSETS", [a.upper() for a in assets], "CRYPTOBOT_ASSETS")
        hours = ctx.conv("CRYPTOBOT_INVOICE_EXPIRES_HOURS", to_int)
        if hours is not None:
            ctx.set("PAY_CRYPTOBOT_INVOICE_HOURS", hours, "CRYPTOBOT_INVOICE_EXPIRES_HOURS")
        base = ctx.present("CRYPTOBOT_BASE_URL")
        if base is not None:
            ctx.set("PAY_CRYPTOBOT_BASE_URL", base, "CRYPTOBOT_BASE_URL")
        for key, reason in (
            (
                "CRYPTOBOT_WEBHOOK_SECRET",
                "подпись CryptoBot считается от API-токена, отдельный секрет не нужен",
            ),
            ("CRYPTOBOT_WEBHOOK_PATH", _R["route"].format(route="/webhooks/pay/{id}/{token}, 06 §4.4 шаг 8")),
            ("CRYPTOBOT_DEFAULT_ASSET", "покупатель выбирает монету на странице CryptoBot"),
        ):
            if ctx.get(key) is not None:
                ctx.skip(key, reason)
    # RollyPay.
    rolly = _cash_enabled(ctx, "ROLLYPAY_ENABLED")
    rolly_keys = [k for k in ctx.vals if k.startswith("ROLLYPAY_") and k != "ROLLYPAY_ENABLED"]
    if rolly is False:
        ctx.set("PAY_ROLLYPAY_ENABLED", False, "ROLLYPAY_ENABLED")
        for key in rolly_keys:
            ctx.get(key)
            ctx.skip(key, _R["cash_off"], "dead")
    else:
        if rolly:
            ctx.defer("PAY_ROLLYPAY_ENABLED", True, "ROLLYPAY_ENABLED", note=step8)
        ctx.secret("ROLLYPAY_API_KEY", "PAY_ROLLYPAY_API_KEY")
        ctx.secret("ROLLYPAY_SIGNING_SECRET", "PAY_ROLLYPAY_SIGNING_SECRET")
        test = ctx.conv("ROLLYPAY_TEST_MODE", to_bool)
        if test is not None:
            ctx.set("PAY_ROLLYPAY_TEST_MODE", test, "ROLLYPAY_TEST_MODE")
        method = ctx.present("ROLLYPAY_PAYMENT_METHOD")
        if method is not None:
            ctx.set("PAY_ROLLYPAY_PAYMENT_METHOD", method, "ROLLYPAY_PAYMENT_METHOD")
        cur = ctx.present("ROLLYPAY_CURRENCY")
        if cur is not None:
            if cur.upper() == "RUB":
                ctx.note("ROLLYPAY_CURRENCY")
            else:
                ctx.skip("ROLLYPAY_CURRENCY", _R["currency"].format(cur=cur), "conflict")
                ctx.warn(_R["currency"].format(cur=cur))
        _rolly_limits(ctx)
        for key, reason in (
            ("ROLLYPAY_BASE_URL", _R["rolly_base"]),
            ("ROLLYPAY_DISPLAY_NAME", _R["display"]),
            ("ROLLYPAY_RETURN_URL", "страница возврата — бот (t.me/<бот>), задаётся плагином"),
            ("ROLLYPAY_WEBHOOK_PATH", _R["route"].format(route="/webhooks/pay/{id}/{token}")),
        ):
            if ctx.get(key) is not None:
                ctx.skip(key, reason)
    # Other cash desks: no plugin. An enabled one is a money risk → warning.
    for prefix in _OTHER_CASH:
        keys = [k for k in ctx.vals if k.startswith(prefix + "_")]
        if not keys:
            continue
        flag = f"{prefix}_ENABLED"
        enabled = False
        if flag in ctx.vals:
            try:
                enabled = to_bool(ctx.raw(flag))
            except ValueParseError:
                enabled = False
        for key in keys:
            ctx.get(key)
            if enabled:
                ctx.skip(key, _R["cash_unsupported"], "conflict")
            else:
                ctx.skip(key, _R["cash_off"], "dead")
        if enabled:
            ctx.warn(f"касса {prefix} включена в Bedolaga, но в SvBG её нет — проверьте до T0")
    for key in ("SUPPORT_TOPUP_ENABLED",):
        if ctx.get(key) is not None:
            ctx.skip(key, "ручное пополнение — начисление админом в карточке пользователя / manual_receipts")
    if ctx.get("AUTO_PURCHASE_AFTER_TOPUP_ENABLED") is not None:
        ctx.skip("AUTO_PURCHASE_AFTER_TOPUP_ENABLED", _R["core_behaviour"] + " (заказ awaiting_funds)")


def _plugin_limits(slug: str) -> tuple[int | None, int | None]:
    """``(min_minor, max_minor)`` of a payment plugin's manifest (``(None, None)`` when not installed)."""
    try:
        from svbg.payments.providers.rollypay import RollyPay

        manifests = {"rollypay": RollyPay.manifest}
    except ImportError:  # pragma: no cover - the plugin ships with the core
        return None, None
    manifest = manifests.get(slug)
    return (None, None) if manifest is None else (manifest.min_minor, manifest.max_minor)


def _rolly_limits(ctx: _Ctx) -> None:
    """Bedolaga's per-desk RollyPay limits (kopeks). SvBG has them in the plugin (``Manifest.min_minor``,
    17 900 = 179 ₽ on the owner's production): equal → nothing to carry; different → listed + warning,
    since a lower SvBG minimum would issue invoices RollyPay refuses and a higher one refuses top-ups
    Bedolaga took."""
    plugin = dict(zip(("min", "max"), _plugin_limits("rollypay"), strict=True))
    for key, which in (("ROLLYPAY_MIN_AMOUNT_KOPEKS", "min"), ("ROLLYPAY_MAX_AMOUNT_KOPEKS", "max")):
        value = ctx.conv(key, to_int)
        if value is None:
            continue
        ours = plugin[which]
        if ours is not None and ours == value:
            ctx.note(key)
            continue
        if which == "max" and ours is None:
            ctx.skip(key, "у плагина RollyPay нет своего максимума: действует WALLET_TOPUP_MAX")
            continue
        ctx.skip(key, _R["per_cash_min"] + f" (Bedolaga {value}, плагин {ours})", "conflict")
        ctx.warn(f"RollyPay: {key}={value} ≠ ограничению плагина SvBG ({ours}) — проверьте до T0")


def _ops(ctx: _Ctx) -> None:
    auto = ctx.conv("MAINTENANCE_AUTO_ENABLE", to_bool)
    if auto is not None:
        ctx.set("MAINTENANCE_MODE", "auto" if auto else "off", "MAINTENANCE_AUTO_ENABLE")
    if ctx.get("MAINTENANCE_MODE") is not None:
        ctx.skip("MAINTENANCE_MODE", _R["maintenance"])
    msg = ctx.present("MAINTENANCE_MESSAGE")
    if msg is not None:
        ctx.set("MAINTENANCE_MESSAGE", msg, "MAINTENANCE_MESSAGE")
    for key in [k for k in ctx.vals if k.startswith("MAINTENANCE_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            ctx.skip(key, _R["jobs"])
    for key in ("ENABLE_LOGO_MODE", "LOGO_FILE", "MAIN_MENU_MODE", "MAIN_MENU_RICH_LOGO_URL",
                "MAIN_MENU_RICH_ENABLED"):  # fmt: skip
        if ctx.get(key) is not None:
            ctx.skip(key, _R["logo"], "data")
    for key in ("CONNECT_BUTTON_MODE", "HIDE_SUBSCRIPTION_LINK"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["core_behaviour"] + " (06 §3.2)")
    for key in [k for k in ctx.vals if k.startswith(_JOB_KEYS) or k == "ENABLE_NOTIFICATIONS"]:
        ctx.get(key)
        ctx.skip(key, _R["jobs"])


def _telegram(ctx: _Ctx) -> None:
    step9 = "задать на шаге 9 runbook T0 (06 §4.4) вместе с боевым токеном"
    mode = ctx.present("BOT_RUN_MODE")
    if mode is not None:
        norm = mode.lower()
        if norm in ("webhook", "polling"):
            ctx.defer("BOT_MODE", norm, "BOT_RUN_MODE", note=step9)
        else:
            ctx.skip("BOT_RUN_MODE", f"неизвестный режим «{mode}»", "invalid")
    url = ctx.present("WEBHOOK_URL")
    if url is not None:
        ctx.defer("PUBLIC_URL", url.rstrip("/"), "WEBHOOK_URL",
                  note=step9 + "; проверьте, что это адрес НОВОГО бота (маршрут /tg/{secret})")  # fmt: skip
    for key in ("WEBHOOK_PATH", "WEBHOOK_SECRET_TOKEN"):
        if ctx.get(key) is not None:
            ctx.skip(
                key, _R["new_secret"] if key.endswith("TOKEN") else _R["route"].format(route="/tg/{secret}")
            )
    if ctx.get("WEBHOOK_DROP_PENDING_UPDATES") is not None:
        ctx.skip("WEBHOOK_DROP_PENDING_UPDATES", _R["drop_pending"])
    for key in [k for k in ctx.vals if k.startswith("WEBHOOK_") and not k.startswith("WEBHOOK_NOTIFY_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            ctx.skip(key, _R["subsystem"])


def _ip_guard(ctx: _Ctx) -> None:
    enabled = ctx.conv("IP_GUARD_ENABLED", to_bool)
    if enabled:
        ctx.defer("IP_GUARD_ENABLED", True, "IP_GUARD_ENABLED",
                  note="шаг 12 runbook T0: режим по решению §1.4; IP_GUARD_AUTO_BLOCK остаётся выключенным "
                  "до калибровки (05 §2.2.10)")  # fmt: skip
    elif enabled is False:
        ctx.set("IP_GUARD_ENABLED", False, "IP_GUARD_ENABLED")
    for src, (our, kind) in _IP_GUARD.items():
        if kind == "int":
            value: Any = ctx.conv(src, to_int)
        elif kind == "bool":
            value = ctx.conv(src, to_bool)
        else:
            value = ctx.conv(src, to_str_list)
        if value is not None:
            ctx.set(our, value, src)
    excluded = ctx.conv("IP_GUARD_EXCLUDED_NODE_UUIDS", to_str_list)
    if excluded:
        ctx.plan.cdn_nodes = excluded
    if ctx.get("IP_GUARD_WHITELIST_PANEL_USER_IDS") is not None:
        ctx.skip("IP_GUARD_WHITELIST_PANEL_USER_IDS", _R["data_whitelist"], "data")
    for key in [k for k in ctx.vals if k.startswith("IP_GUARD_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            texts = ("_USER_", "_BUTTON", "_BANNER", "_NOTE", "_PAID", "_PURCHASE_BLOCKED")
            ctx.skip(key, _R["texts"] if any(t in key for t in texts) else _R["constant"])


def _lte(ctx: _Ctx) -> None:
    wlq = {k: _wlq_value(v) for k, v in ctx.wlq.items()}
    used_wlq: set[str] = set()

    def wlq_mark(key: str) -> None:
        used_wlq.add(key)
        ctx.plan.consumed.add(f"wlq_settings.{key}")

    enabled = ctx.conv("WL_QUOTA_ENABLED", to_bool)
    if enabled:
        ctx.defer("LTE_ENABLED", True, "WL_QUOTA_ENABLED",
                  note="шаг 12 runbook T0: после импорта состояния LTE (05 §2.1.17)")  # fmt: skip
    elif enabled is False:
        ctx.set("LTE_ENABLED", False, "WL_QUOTA_ENABLED")
    # Enforcement: Bedolaga off (shadow that RELEASES blocks) / list / all → SvBG shadow | on (06 §3.2).
    scope = ctx.present("WL_QUOTA_ENFORCE_SCOPE")
    lst = ctx.conv("WL_QUOTA_ENFORCE_LIST", to_int_list) if ctx.has("WL_QUOTA_ENFORCE_LIST") else None
    if scope is not None:
        norm = scope.lower()
        if norm not in ("off", "list", "all"):
            ctx.warn(_R["lte_scope"].format(v=scope))
        ctx.set("LTE_ENFORCE", "shadow", "WL_QUOTA_ENFORCE_SCOPE",
                note="импорт всегда начинает с shadow: импортированные блоки остаются (06 §3.2)")  # fmt: skip
        if norm in ("list", "all"):
            ctx.defer("LTE_ENFORCE", "on", "WL_QUOTA_ENFORCE_SCOPE",
                      note="шаг 12 runbook T0: после 1–2 циклов shadow и сверки С7")  # fmt: skip
        if norm == "list":
            ctx.set("LTE_ENFORCE_LIST", lst or [], "WL_QUOTA_ENFORCE_LIST")
        elif lst is not None:
            ctx.set(
                "LTE_ENFORCE_LIST",
                [],
                "WL_QUOTA_ENFORCE_LIST",
                note="ENFORCE_SCOPE≠list: список не действует",
            )
    elif lst is not None:
        ctx.set("LTE_ENFORCE_LIST", lst, "WL_QUOTA_ENFORCE_LIST")
    for key in ("WL_QUOTA_EMERGENCY_HOLD", "WL_QUOTA_RELEASE_ON_DISABLE"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["lte_dropped"])
    # Blocks are never released by a switch (06 §0 п.6): our «при выключении» is pinned to keep, whatever
    # RELEASE_ON_DISABLE said, whenever the owner's LTE section is imported at all.
    lte_src = _present(ctx, "WL_QUOTA_ENABLED", "WL_QUOTA_ENFORCE_SCOPE")
    if lte_src:
        ctx.set("LTE_OFF_ACTION", "keep", *lte_src,
                note="выключение/off не снимает блоки; снять все — аварийной командой (06 §3.2)")  # fmt: skip
    for key, wkey, reason in _LTE_NO_ANALOG:
        if ctx.get(key) is not None:
            ctx.skip(key, reason)
        if wkey is not None and wkey in wlq:
            wlq_mark(wkey)
            ctx.skip(f"wlq_settings.{wkey}", reason)
    # Plain keys: wlq_settings override > env > system_settings.
    for src, (our, kind, wkey) in _LTE.items():
        if wkey is not None and wkey in wlq:
            wlq_mark(wkey)
            value = _convert_wlq(wlq[wkey], kind)
            if value is None:
                ctx.skip(f"wlq_settings.{wkey}", "неверное значение в разделе LTE", "invalid")
            else:
                ctx.note(src)
                ctx.set(our, value, src, f"wlq_settings.{wkey}", note="значение раздела LTE (wlq_settings)")
                continue
        value = _lte_env(ctx, src, kind)
        if value is not None:
            ctx.set(our, value, src)
    # Inverted/override switches.
    if "notify_user_off" in wlq:
        wlq_mark("notify_user_off")
        if bool(wlq["notify_user_off"]):
            ctx.set(
                "LTE_NOTIFY_USER",
                False,
                "wlq_settings.notify_user_off",
                *_present(ctx, "WL_QUOTA_NOTIFY_USER"),
            )
    if "topup_kill_switch" in wlq:
        wlq_mark("topup_kill_switch")
        if bool(_wlq_value(wlq["topup_kill_switch"])):
            ctx.set("LTE_TOPUP_ENABLED", False, "wlq_settings.topup_kill_switch",
                    *_present(ctx, "WL_QUOTA_TOPUP_ENABLED"))  # fmt: skip
    if "kill_switch" in wlq:
        wlq_mark("kill_switch")
        if bool(wlq["kill_switch"]):
            ctx.warn(_R["lte_kill"])
    for key in ("WL_QUOTA_TOPUP_PACKAGES", "WL_QUOTA_TOPUP_PACKAGES_BY_GROUP", "WL_QUOTA_TOPUP_GROUPS"):
        if ctx.get(key) is not None:
            ctx.skip(key, _R["data_packs"], "data")
    for key in [k for k in ctx.vals if k.startswith("WL_QUOTA_")]:
        if key not in ctx.plan.consumed and ctx.plan.skipped(key) is None:
            ctx.get(key)
            ctx.skip(key, _R["constant"] + " / механика модуля LTE")
    for key in ctx.wlq:
        if key not in used_wlq:
            ctx.skip(f"wlq_settings.{key}", "служебная запись раздела LTE (wlq-admin)")


def _lte_env(ctx: _Ctx, key: str, kind: str) -> Any:
    if kind == "int":
        return ctx.conv(key, to_int)
    if kind == "bool":
        return ctx.conv(key, to_bool)
    if kind == "time":
        text = ctx.present(key)
        if text is None:
            return None
        m = _HHMM_RE.fullmatch(text)
        if m is None:
            ctx.skip(key, f"время «{text}» не в формате ЧЧ:ММ", "invalid")
            return None
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ctx.present(key)


def _convert_wlq(value: Any, kind: str) -> Any:
    try:
        if kind == "int":
            if isinstance(value, bool):
                return None
            return int(value)
        if kind == "bool":
            return value if isinstance(value, bool) else to_bool(str(value))
        if kind == "time":
            m = _HHMM_RE.fullmatch(str(value).strip())
            return None if m is None else f"{int(m.group(1)):02d}:{m.group(2)}"
        return None if value is None else str(value)
    except (TypeError, ValueError):
        return None


def _classify_rest(ctx: _Ctx) -> None:
    for key in sorted(ctx.vals):
        if key in ctx.plan.consumed or ctx.plan.skipped(key) is not None:
            continue
        ctx.get(key)
        if key in _DEAD or key.startswith(_DEAD_PREFIXES):
            ctx.skip(key, _R["dead"], "dead")
        elif key in _SUBSYSTEM_EXACT or key.startswith(_SUBSYSTEM_PREFIXES) or _is_payment_text(key):
            ctx.skip(key, _R["subsystem"], "dead")
        elif key.startswith(("SUPPORT_",)):
            ctx.skip(key, _R["tickets"])
        elif key.startswith("TRAFFIC_"):
            ctx.skip(key, _R["traffic_watch"], "deferred")
        else:
            ctx.skip(key, _R["unknown"], "unknown")


def _is_payment_text(key: str) -> bool:
    return key.startswith("PAYMENT_") and key.endswith(("_TEMPLATE", "_DESCRIPTION"))


# ------------------------------------------------------------------------------------------ applying


@dataclass(slots=True)
class SettingsApplyResult:
    batch_id: str | None
    applied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    not_transferred: list[NotTransferred] = field(default_factory=list)
    restart_required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "applied": sorted(self.applied),
            "unchanged": sorted(self.unchanged),
            "not_transferred": [{"key": n.key, "reason": n.reason} for n in self.not_transferred],
            "restart_required": self.restart_required,
        }


def _selected(plan: SettingsImportPlan, include_deferred: bool | Iterable[str]) -> list[PlannedChange]:
    chosen = list(plan.changes)
    if include_deferred is True:
        extra = list(plan.deferred)
    elif include_deferred is False:
        extra = []
    else:
        wanted = set(include_deferred)
        extra = [c for c in plan.deferred if c.key in wanted]
    by_key = {c.key: c for c in chosen}
    for c in extra:  # a deferred value replaces the immediate one (LTE_ENFORCE shadow → on)
        by_key[c.key] = c
    return list(by_key.values())


def _precheck(
    service: SettingsService, items: Sequence[PlannedChange]
) -> tuple[list[PlannedChange], list[NotTransferred]]:
    ok: list[PlannedChange] = []
    bad: list[NotTransferred] = []
    for item in items:
        defn = service.registry.find(item.key)
        src = ",".join(item.sources)
        if defn is None:
            bad.append(NotTransferred(src, _R["no_registry"].format(key=item.key), item.key, "unknown"))
            continue
        if defn.key in service.locked:
            bad.append(NotTransferred(src, f"{defn.key} задан окружением контейнера (LOCKED_KEYS)", item.key,
                                      "conflict"))  # fmt: skip
            continue
        if defn.readonly or defn.file_only:
            bad.append(
                NotTransferred(src, f"{defn.key} меняется только в файле/окружении", item.key, "conflict")
            )
            continue
        try:
            setting_values.coerce(defn, item.raw)
        except setting_values.SettingValueError as exc:
            bad.append(NotTransferred(src, f"{defn.key}: {exc}", item.key, "invalid"))
            continue
        ok.append(item)
    return ok, bad


async def apply_settings(
    service: SettingsService,
    plan: SettingsImportPlan,
    *,
    actor_id: int | None = None,
    include_deferred: bool | Iterable[str] = False,
) -> SettingsApplyResult:
    """Apply the plan's settings as ONE ``apply(source="import")`` batch (undo = one button).

    Keys missing from the registry, locked by the environment or with a value our registry refuses are not
    sent and are reported as «не перенесено». Re-running with the same plan changes nothing (the service
    treats equal values as unchanged).
    """
    ok, bad = _precheck(service, _selected(plan, include_deferred))
    result = SettingsApplyResult(None, not_transferred=bad)
    if not ok:
        return result
    applied = await service.apply([Change(c.key, c.raw) for c in ok], source="import", actor_id=actor_id)
    result.batch_id = applied.batch_id if (applied.applied or applied.rejected) else None
    result.applied = list(applied.applied)
    result.unchanged = list(applied.unchanged)
    result.restart_required = applied.restart_required
    by_key = {c.key: c for c in ok}
    for key, reason in applied.rejected.items():
        item = by_key.get(key)
        src = ",".join(item.sources) if item else key
        result.not_transferred.append(NotTransferred(src, f"{key}: {reason}", key, "invalid"))
    return result


def diff_against(service: SettingsService, plan: SettingsImportPlan) -> list[dict[str, Any]]:
    """Dry-run / shadow: what :func:`apply_settings` WOULD change (nothing is written). Secrets: only whether
    the value differs."""
    snap = service.current()
    out: list[dict[str, Any]] = []
    for item in [*plan.changes, *plan.deferred]:
        defn = service.registry.find(item.key)
        if defn is None:
            out.append({"key": item.key, "status": "no_registry"})
            continue
        try:
            new = setting_values.coerce(defn, item.raw)
        except setting_values.SettingValueError as exc:
            out.append({"key": defn.key, "status": "invalid", "error": str(exc)})
            continue
        old = snap[defn.key]
        status = "same" if old == new else "change"
        row: dict[str, Any] = {"key": defn.key, "status": status, "deferred": item in plan.deferred}
        if not defn.is_secret:
            row["old"] = setting_values.to_text(defn, old)
            row["new"] = setting_values.to_text(defn, new)
        out.append(row)
    return out


# ---- data: catalog, admin topics, CDN nodes


@dataclass(slots=True)
class CatalogApplyResult:
    plan_id: int | None = None
    trial_id: int | None = None
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    prices_set: list[int] = field(default_factory=list)
    prices_removed: list[int] = field(default_factory=list)
    skipped: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.prices_set or self.prices_removed)


async def _plan_row(conn: AsyncConnection, where: sa.ColumnElement[bool]) -> Any:
    return (
        (await conn.execute(sa.select(plans).where(where).order_by(plans.c.id).limit(1))).mappings().first()
    )


async def apply_catalog(
    conn: AsyncConnection,
    cat: CatalogImport,
    *,
    currency: str = "RUB",
    squads: Sequence[str] | None = None,
    trial_squads: Sequence[str] | None = None,
    plan_id: int | None = None,
    actor: Actor | None = None,
) -> CatalogApplyResult:
    """Align the paid plan (``plan_id``, else ``legacy_id_map('bedolaga','plan','classic')``, else code
    ``bedolaga_classic``/``standard``, else created as ``bedolaga_classic``; the chosen id is written to
    ``legacy_id_map`` so the data importer reuses it) with its ``plan_prices`` and the trial plan (SvBG
    grants a trial from the enabled ``is_trial`` plan), in the caller's transaction. Idempotent: an equal
    value is not written again (no version bump on a re-run).

    Prices of the plan in ``currency`` that Bedolaga did not sell are removed (the import is the truth for
    the periods on sale). ``squads`` — for a newly created plan; default: every location present in the panel.
    ``trial_squads`` — for a newly created trial plan (Bedolaga ``server_squads.is_trial_eligible``); default
    ``squads``. Existing plans keep their squads.
    """
    res = CatalogApplyResult()
    if cat.currency != currency:
        res.skipped = f"валюта магазина {currency} ≠ {cat.currency}: цены Bedolaga не переносятся"
        return res
    if squads is None:
        squads = list(
            (
                await conn.execute(
                    sa.select(locations.c.squad_uuid)
                    .where(locations.c.missing_since.is_(None))
                    .order_by(locations.c.sort, locations.c.squad_uuid)
                )
            ).scalars()
        )
    squads = list(dict.fromkeys(squads))
    wanted: dict[str, Any] = {}
    if cat.device_limit is not None:
        wanted["device_limit"] = cat.device_limit
    if cat.traffic_bytes is not None:
        wanted["traffic_bytes"] = cat.traffic_bytes
    if cat.reset_strategy is not None:
        wanted["reset_strategy"] = cat.reset_strategy
    if cat.traffic_on_renew is not None:
        wanted["traffic_on_renew"] = cat.traffic_on_renew
    if cat.devices_on_renew is not None:
        wanted["devices_on_renew"] = cat.devices_on_renew
    if cat.panel_tag is not None:
        wanted["panel_tag"] = cat.panel_tag
    addon = cat.device_addon
    if cat.sources & {"PRICE_PER_DEVICE", "DEVICES_SELECTION_ENABLED"}:
        wanted["device_addon"] = addon
    row = await _plan_row(conn, plans.c.id == plan_id) if plan_id is not None else None
    has_map = bool(await conn.scalar(sa.text("SELECT to_regclass('public.legacy_id_map') IS NOT NULL")))
    if row is None and has_map:
        mapped = await _mapped_plan_id(conn)
        if mapped is not None:
            row = await _plan_row(conn, plans.c.id == mapped)
    for code in PLAN_CODES:
        if row is None:
            row = await _plan_row(conn, plans.c.code == code)
    if row is None and cat.prices:
        plan_id = await create_plan(
            conn,
            name="Подписка",
            code=PLAN_CODES[0],
            availability="all",
            sort=10,
            actor=actor,
            **_on_sale(squads),
            **wanted,
        )
        res.created.append(PLAN_CODES[0])
    elif row is not None:
        plan_id = int(row["id"])
        changes = _plan_diff(row, wanted)
        if changes:
            await update_plan(conn, plan_id, actor=actor, audit_action="plan.import", **changes)
            res.updated.extend(sorted(changes))
    else:
        plan_id = None
    res.plan_id = plan_id
    if plan_id is not None and has_map:
        await _remember_plan(conn, plan_id)
    if plan_id is not None and cat.prices:
        have = {
            int(r.days): int(r.amount_minor)
            for r in (
                await conn.execute(
                    sa.select(plan_prices.c.days, plan_prices.c.amount_minor).where(
                        plan_prices.c.plan_id == plan_id, plan_prices.c.currency == currency
                    )
                )
            ).all()
        }
        for days, amount in sorted(cat.prices.items()):
            if have.get(days) != amount:
                await set_price(conn, plan_id, days=days, amount_minor=amount, currency=currency, actor=actor)
                res.prices_set.append(days)
        for days in sorted(set(have) - set(cat.prices)):
            await delete_price(conn, plan_id, days=days, currency=currency, actor=actor)
            res.prices_removed.append(days)
    # Trial plan (its length is the TRIAL_DAYS setting).
    trial_wanted: dict[str, Any] = {}
    if cat.trial_device_limit is not None:
        trial_wanted["device_limit"] = cat.trial_device_limit
    if cat.trial_traffic_bytes is not None:
        trial_wanted["traffic_bytes"] = cat.trial_traffic_bytes
    if cat.trial_panel_tag is not None:
        trial_wanted["panel_tag"] = cat.trial_panel_tag
    if trial_wanted:
        trow = await _plan_row(conn, plans.c.is_trial.is_(True))
        if trow is None:
            res.trial_id = await create_plan(
                conn,
                name="Пробный",
                code=TRIAL_CODE,
                is_trial=True,
                sort=0,
                **_on_sale(list(dict.fromkeys(trial_squads)) if trial_squads is not None else squads),
                actor=actor,
                **trial_wanted,
            )
            res.created.append(TRIAL_CODE)
        else:
            res.trial_id = int(trow["id"])
            changes = _plan_diff(trow, trial_wanted)
            if changes:
                await update_plan(conn, res.trial_id, actor=actor, audit_action="plan.import", **changes)
                res.updated.extend(f"trial.{k}" for k in sorted(changes))
    return res


async def _mapped_plan_id(conn: AsyncConnection) -> int | None:
    source, entity, old_id = PLAN_MAP
    raw = await conn.scalar(
        sa.text("SELECT new_id FROM legacy_id_map WHERE source = :s AND entity = :e AND old_id = :o"),
        {"s": source, "e": entity, "o": old_id},
    )
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


async def _remember_plan(conn: AsyncConnection, plan_id: int) -> None:
    """``legacy_id_map('bedolaga', 'plan', 'classic') → plan_id`` so the data importer reuses this plan.
    An equal row is not rewritten (idempotent)."""
    source, entity, old_id = PLAN_MAP
    await conn.execute(
        sa.text(
            "INSERT INTO legacy_id_map (source, entity, old_id, new_id, data)"
            " VALUES (:s, :e, :o, :n, CAST(:d AS jsonb))"
            " ON CONFLICT (source, entity, old_id) DO UPDATE"
            " SET new_id = excluded.new_id, updated_at = now()"
            " WHERE legacy_id_map.new_id IS DISTINCT FROM excluded.new_id"
        ),
        {"s": source, "e": entity, "o": old_id, "n": str(plan_id), "d": json.dumps({"by": "settings_map"})},
    )


def _on_sale(squads: Sequence[str]) -> dict[str, Any]:
    """A new plan is on sale only with squads (DB CHECK); without them it is created hidden."""
    return {"enabled": True, "squads": list(squads)} if squads else {"enabled": False}


def _plan_diff(row: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in wanted.items():
        current = row[key]
        if key == "device_addon":
            if (value.to_json() if value is not None else {}) != (current or {}):
                out[key] = value
        elif current != value:
            out[key] = value
    return out


async def apply_topics(conn: AsyncConnection, topics: Iterable[TopicImport]) -> list[str]:
    """Reuse the Bedolaga admin topics (only when the owner chose «переиспользовать», 06 §3.2): upsert
    ``admin_topics(kind, chat_id, thread_id)``. Returns the kinds written (unchanged rows are not)."""
    from svbg.services.admin_chat import CORE_TOPICS
    from svbg.services.tables import admin_topics

    titles = {t.kind: t for t in CORE_TOPICS}
    written: list[str] = []
    for topic in topics:
        spec = titles.get(topic.kind)
        if spec is None:
            continue
        current = (
            await conn.execute(
                sa.select(admin_topics.c.chat_id, admin_topics.c.thread_id).where(
                    admin_topics.c.kind == topic.kind
                )
            )
        ).first()
        if current is not None and (current.chat_id, current.thread_id) == (topic.chat_id, topic.thread_id):
            continue
        stmt = pg_insert(admin_topics).values(
            kind=topic.kind,
            chat_id=topic.chat_id,
            thread_id=topic.thread_id,
            title=spec.title,
            icon=spec.icon,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[admin_topics.c.kind],
            set_={"chat_id": topic.chat_id, "thread_id": topic.thread_id, "updated_at": sa.func.now()},
        )
        await conn.execute(stmt)
        written.append(topic.kind)
    return written


async def apply_cdn_nodes(conn: AsyncConnection, node_uuids: Iterable[str]) -> int | None:
    """``IP_GUARD_EXCLUDED_NODE_UUIDS`` → ``ip_guard_nodes.cdn = true`` (05 §2.2.10 p.4). ``None`` when the
    IP Guard tables are not installed yet. Returns the number of rows changed."""
    uuids = [u for u in dict.fromkeys(node_uuids) if 1 <= len(u) <= 64]
    if not uuids:
        return 0
    exists = await conn.scalar(sa.text("SELECT to_regclass('public.ip_guard_nodes') IS NOT NULL"))
    if not exists:
        return None
    changed = 0
    for uuid in uuids:
        result = await conn.execute(
            sa.text(
                "INSERT INTO ip_guard_nodes (node_uuid, cdn) VALUES (:u, true) "
                "ON CONFLICT (node_uuid) DO UPDATE SET cdn = true, updated_at = now() "
                "WHERE ip_guard_nodes.cdn IS DISTINCT FROM true"
            ),
            {"u": uuid},
        )
        changed += result.rowcount or 0
    return changed
