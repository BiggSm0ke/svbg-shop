"""IP Guard settings (05 §2.2.6): declarations for the registry and the typed parameters built from a
snapshot. Every key is HOT: the next pass reads the new value; the module switch starts/stops collection at
once (the extension host applies it).

Cross-key rules are enforced by clamping, never by refusing a click: ``warn ≤ block``,
``min_subnets ≤ block``,``confirm_live ≤ block``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from svbg.core.settings.registry import Apply, SettingDef
from svbg.ext.api import enabled_setting

__all__ = [
    "K_AUTO_BLOCK",
    "K_ENABLED",
    "SECTION",
    "SETTINGS",
    "CollectorParams",
    "DecisionParams",
    "Params",
    "parse_cidrs",
]

SECTION: Final = ("ip_guard", "🛡 IP Guard")
K_ENABLED: Final = "IP_GUARD_ENABLED"
K_AUTO_BLOCK: Final = "IP_GUARD_AUTO_BLOCK"
K_WARN_IPS: Final = "IP_GUARD_WARN_IPS"
K_BLOCK_IPS: Final = "IP_GUARD_BLOCK_IPS"
K_WINDOW: Final = "IP_GUARD_WINDOW_MINUTES"
K_MIN_SUBNETS: Final = "IP_GUARD_MIN_SUBNETS"
K_CONFIRM_LIVE: Final = "IP_GUARD_CONFIRM_LIVE_IPS"
K_CONFIRM_CHECKS: Final = "IP_GUARD_CONFIRM_CHECKS"
K_SHARED_USERS: Final = "IP_GUARD_SHARED_IP_USERS"
K_IGNORE_CIDRS: Final = "IP_GUARD_IGNORE_CIDRS"
K_NOTIFY_USER: Final = "IP_GUARD_NOTIFY_USER"
K_GRACE: Final = "IP_GUARD_UNBLOCK_GRACE_MINUTES"
K_WARN_COOLDOWN: Final = "IP_GUARD_WARN_COOLDOWN_MINUTES"
K_EVIDENCE_TTL: Final = "IP_GUARD_EVIDENCE_TTL_DAYS"
K_IPV6_PREFIX: Final = "IP_GUARD_IPV6_PREFIX"
K_SUSTAINED: Final = "IP_GUARD_SUSTAINED_CHECKS"
K_MAX_PER_RUN: Final = "IP_GUARD_MAX_BLOCKS_PER_RUN"
K_MAX_PER_HOUR: Final = "IP_GUARD_MAX_BLOCKS_PER_HOUR"
K_QUARANTINE_MAX: Final = "IP_GUARD_QUARANTINE_MAX_MINUTES"
K_MAX_WARNINGS: Final = "IP_GUARD_MAX_WARNINGS_PER_RUN"
K_PIN: Final = "IP_GUARD_PIN_MESSAGES"
K_CONCURRENCY: Final = "IP_GUARD_NODE_CONCURRENCY"

_S: Final = SECTION[0]
_TAGS: Final = ("ip guard", "антиабуз", "ip", "слив")


def parse_cidrs(values: Any) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """``["10.0.0.0/8", "2001:db8::/32", "1.2.3.4"]`` → networks; invalid items are skipped."""
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if not isinstance(values, list | tuple):
        return ()
    for item in values:
        try:
            out.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            continue
    return tuple(out)


def _cidr_list(value: Any) -> None:
    for item in value or []:
        try:
            ipaddress.ip_network(str(item).strip(), strict=False)
        except ValueError:
            raise ValueError(f"«{str(item)[:40]}» — не адрес и не сеть (пример: 203.0.113.0/24)") from None


def _int(  # noqa: PLR0917 - a compact table of settings
    key: str, default: int, lo: int, hi: int, title: str, desc: str, *, advanced: bool = False
) -> SettingDef:
    return SettingDef(
        key,
        int,
        default,
        _S,
        title,
        desc,
        apply=Apply.HOT,
        min=lo,
        max=hi,
        advanced=advanced,
        owner_only=True,
        tags=_TAGS,
    )


def _bool(key: str, default: bool, title: str, desc: str, *, advanced: bool = False) -> SettingDef:
    return SettingDef(
        key, bool, default, _S, title, desc, apply=Apply.HOT, advanced=advanced, owner_only=True, tags=_TAGS
    )


SETTINGS: Final[tuple[SettingDef, ...]] = (
    enabled_setting(
        "ip_guard",
        "IP Guard",
        "Раз в минуту собирает IP подключений с нод и предупреждает о раздаче ссылки. Нужен токен панели со "
        "скоупами connections.",
        section=_S,
    ),
    _bool(
        K_AUTO_BLOCK,
        False,
        "Автоблок",
        "Блокировать без админа, когда раздача подтверждена. Включайте после недели калибровки: "
        "пока выключен, "
        "приходят только предупреждения, блок — кнопкой.",
    ),
    _int(
        K_WARN_IPS, 20, 2, 10_000, "Порог предупреждения, IP", "Столько разных IP за окно — карточка админам."
    ),
    _int(K_BLOCK_IPS, 25, 2, 10_000, "Порог блока, IP", "Столько разных IP за окно — кандидат на блок."),
    _int(K_WINDOW, 10, 2, 60, "Окно, минут", "За какое время считаются разные IP."),
    _int(
        K_MIN_SUBNETS,
        10,
        0,
        10_000,
        "Минимум подсетей",
        "Меньше разных подсетей /24 — похоже на NAT оператора, не блокируем. 0 — не проверять.",
    ),
    _int(
        K_CONFIRM_LIVE,
        10,
        0,
        10_000,
        "Живых IP для подтверждения",
        "Сколько IP должно быть онлайн прямо сейчас. 0 — не проверять.",
    ),
    _int(K_CONFIRM_CHECKS, 2, 1, 5, "Проходов подряд", "Сколько полных проходов подряд нужно для блока."),
    _int(
        K_SHARED_USERS,
        5,
        2,
        1000,
        "Общий IP: пользователей",
        "IP, который за проход виден у стольких пользователей, никому не считается и не рвётся.",
    ),
    SettingDef(
        K_IGNORE_CIDRS,
        "list[str]",
        [],
        _S,
        "Не учитывать сети",
        "IP и сети, которые никогда не считаются (свои прокси, офис).",
        apply=Apply.HOT,
        validator=_cidr_list,
        owner_only=True,
        tags=_TAGS,
        hint="203.0.113.0/24, 2001:db8::/32",
    ),
    _bool(K_NOTIFY_USER, True, "Уведомлять пользователя", "Сообщать пользователю о блоке и разблокировке."),
    _int(
        K_GRACE,
        30,
        0,
        1440,
        "Пауза после разблокировки, мин",
        "Столько минут после разблокировки не блокируем.",
    ),
    _int(
        K_WARN_COOLDOWN,
        360,
        10,
        10_080,
        "Повтор предупреждений, мин",
        "Не чаще одного предупреждения на подписку за это время.",
    ),
    _int(
        K_EVIDENCE_TTL,
        180,
        1,
        3650,
        "Хранить IP блоков, дней",
        "Потом список IP в блоке стирается, остаются только счётчики.",
    ),
    _int(
        K_IPV6_PREFIX, 64, 32, 128, "IPv6: префикс", "IPv6 считается по сетям этого размера.", advanced=True
    ),
    _int(
        K_SUSTAINED,
        20,
        0,
        1000,
        "Блок без живых IP после проходов",
        "Высокий W столько проходов подряд — блок даже без живых IP. 0 — выключено.",
        advanced=True,
    ),
    _int(
        K_MAX_PER_RUN,
        3,
        1,
        100,
        "Предохранитель: блоков за проход",
        "Больше кандидатов за проход или за окно — автоблоки останавливаются (карантин).",
        advanced=True,
    ),
    _int(K_MAX_PER_HOUR, 10, 1, 1000, "Предохранитель: блоков в час", "Больше — карантин.", advanced=True),
    _int(
        K_QUARANTINE_MAX,
        60,
        5,
        1440,
        "Карантин не дольше, мин",
        "Карантин продлевается, пока срабатывает предохранитель, но не дольше.",
        advanced=True,
    ),
    _int(
        K_MAX_WARNINGS,
        5,
        1,
        50,
        "Карточек за проход",
        "Остальные предупреждения приходят одной сводкой.",
        advanced=True,
    ),
    _bool(K_PIN, True, "Закреплять карточки", "Закреплять карточки блоков в теме «Антиабуз».", advanced=True),
    _int(K_CONCURRENCY, 4, 1, 16, "Нод параллельно", "Сколько нод опрашивать одновременно.", advanced=True),
)

_DEFAULTS: Final[Mapping[str, Any]] = {d.key: d.default for d in SETTINGS}


def _get_int(cfg: Mapping[str, Any], key: str) -> int:
    value = _get(cfg, key)
    if isinstance(value, bool) or not isinstance(value, int):
        return int(_DEFAULTS[key])
    return value


def _get(cfg: Mapping[str, Any], key: str) -> Any:
    try:
        return cfg[key]
    except (KeyError, RuntimeError, TypeError):
        return _DEFAULTS.get(key)


def _get_bool(cfg: Mapping[str, Any], key: str) -> bool:
    value = _get(cfg, key)
    return value if isinstance(value, bool) else bool(_DEFAULTS[key])


@dataclass(frozen=True, slots=True)
class DecisionParams:
    """Thresholds of the decider (owner defaults: 20/25 IP, ≥10 subnets, ≥10 live, 2 full passes)."""

    warn_ips: int = 20
    block_ips: int = 25
    confirm_checks: int = 2
    confirm_live_ips: int = 10
    min_subnets: int = 10
    window_minutes: int = 10
    warn_cooldown_minutes: int = 360
    max_blocks_per_run: int = 3
    max_blocks_per_hour: int = 10
    max_warnings_per_run: int = 5
    sustained_block_checks: int = 20
    quarantine_max_minutes: int = 60
    whitelist: frozenset[int] = frozenset()  # panel user ids


@dataclass(frozen=True, slots=True)
class CollectorParams:
    window_minutes: int = 10
    ipv6_prefix: int = 64
    ignore_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    shared_ip_users: int = 5
    node_concurrency: int = 4
    job_timeout_s: float = 20.0
    interval_s: float = 60.0
    max_tracked_keys: int = 200_000


@dataclass(frozen=True, slots=True)
class Params:
    """Everything one pass needs, taken once from a settings snapshot."""

    decision: DecisionParams
    collector: CollectorParams
    auto_block: bool = False
    notify_user: bool = True
    grace_minutes: int = 30
    evidence_ttl_days: int = 180
    pin: bool = True

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any], *, whitelist: frozenset[int] = frozenset()) -> Params:
        block = _get_int(cfg, K_BLOCK_IPS)
        warn = min(_get_int(cfg, K_WARN_IPS), block)
        window = _get_int(cfg, K_WINDOW)
        decision = DecisionParams(
            warn_ips=warn,
            block_ips=block,
            confirm_checks=max(1, min(5, _get_int(cfg, K_CONFIRM_CHECKS))),
            confirm_live_ips=min(_get_int(cfg, K_CONFIRM_LIVE), block),
            min_subnets=min(_get_int(cfg, K_MIN_SUBNETS), block),
            window_minutes=window,
            warn_cooldown_minutes=_get_int(cfg, K_WARN_COOLDOWN),
            max_blocks_per_run=_get_int(cfg, K_MAX_PER_RUN),
            max_blocks_per_hour=_get_int(cfg, K_MAX_PER_HOUR),
            max_warnings_per_run=_get_int(cfg, K_MAX_WARNINGS),
            sustained_block_checks=_get_int(cfg, K_SUSTAINED),
            quarantine_max_minutes=_get_int(cfg, K_QUARANTINE_MAX),
            whitelist=whitelist,
        )
        collector = CollectorParams(
            window_minutes=window,
            ipv6_prefix=_get_int(cfg, K_IPV6_PREFIX),
            ignore_cidrs=parse_cidrs(_get(cfg, K_IGNORE_CIDRS)),
            shared_ip_users=_get_int(cfg, K_SHARED_USERS),
            node_concurrency=_get_int(cfg, K_CONCURRENCY),
        )
        return cls(
            decision=decision,
            collector=collector,
            auto_block=_get_bool(cfg, K_AUTO_BLOCK),
            notify_user=_get_bool(cfg, K_NOTIFY_USER),
            grace_minutes=_get_int(cfg, K_GRACE),
            evidence_ttl_days=_get_int(cfg, K_EVIDENCE_TTL),
            pin=_get_bool(cfg, K_PIN),
        )
