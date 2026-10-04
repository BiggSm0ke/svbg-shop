"""Settings registry: every configurable scalar declared once, in code (03 §8.2, 07 §3).

A :class:`SettingDef` carries everything the rest of the system needs: type and default, how a change is
applied (:class:`Apply`), which component must accept it, whether it is a secret or a bootstrap key, the
section of ``.env`` / the settings screen, and Russian texts for the UI and the ``.env`` comments.

There are no migrations for settings: a new key is a new ``SettingDef``; its default lives here. A renamed
key keeps its old names in ``aliases`` (the old name is still read and the line is rewritten).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
import zoneinfo
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Final

from svbg.core.money import CURRENCY_EXPONENT
from svbg.core.settings import values

__all__ = [
    "CAPTCHA_EMOJIS_DEFAULT",
    "EXTENSION_MODULES",
    "MODULES_SECTION",
    "PAYMENTS_SECTION",
    "RETIRED_KEYS",
    "SECTIONS",
    "Apply",
    "Registry",
    "SettingDef",
    "SnapshotCheck",
    "add_module_settings",
    "add_payment_instance",
    "core_registry",
    "full_registry",
    "payment_section",
]


class Apply(enum.Enum):
    """How a changed value takes effect."""

    HOT = "hot"  # read from the snapshot on every use
    RELOAD = "reload"  # a component re-creates its clients (probe before persisting, reconfigure after)
    RESTART = "restart"  # persisted now, effective after a process restart

    @property
    def marker(self) -> str:
        return _APPLY_MARKERS[self]


_APPLY_MARKERS: Final = {Apply.HOT: "⚡", Apply.RELOAD: "🔄", Apply.RESTART: "♻️"}
_KEY_RE: Final = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SECTION_ID_RE: Final = re.compile(r"[a-z][a-z0-9_.]{0,63}")


@dataclass(frozen=True)
class SettingDef:
    """Declaration of one setting. Owner-facing texts are Russian."""

    key: str  # canonical env name, e.g. "TRIAL_DAYS"
    type: type | str  # int, bool, str, float, "secret", "list[str]", "list[int]", "enum", "url", "duration"
    default: Any
    section: str
    title: str
    description: str
    apply: Apply = Apply.HOT
    component: str | None = None  # for RELOAD
    secret: bool = False
    bootstrap: bool = False
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    nullable: bool = False
    advanced: bool = False
    aliases: tuple[str, ...] = ()
    validator: Callable[[Any], None] | None = None
    # Extensions beyond the stage-0 contract (all optional):
    owner_only: bool = False  # only the owner may see/change it in the bot
    file_only: bool = False  # read from .env/environ before the DB; never stored in the DB; not for the bot
    readonly: bool = False  # not changeable through apply() at all (dedicated procedure / compose only)
    in_file: bool = True  # mirrored to .env
    tags: tuple[str, ...] = ()  # search synonyms
    hint: str | None = None  # example / where to get it
    # For the bot's screens (``svbg.core.settings.labels`` has them for the bundled keys;
    # .env keeps raw values):
    choice_labels: Mapping[str, str] | None = None  # enum value → owner-facing label
    presets: tuple[Any, ...] | None = None  # ready values shown as buttons on the card

    @cached_property
    def kind(self) -> str:
        return values.kind_of(self)

    @property
    def is_secret(self) -> bool:
        return self.secret or self.type == "secret"

    @property
    def names(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)

    def default_text(self) -> str:
        return values.to_text(self, self.default)


#: Cross-key check of a candidate snapshot: returns ``{key: owner-facing error}`` for keys to reject.
SnapshotCheck = Callable[[Mapping[str, Any], frozenset[str]], Mapping[str, str]]

#: Parent of the sections that modules add later (X12): «Модули» with LTE, IP Guard … as subsections.
MODULES_SECTION: Final = "modules"
#: Parent of the per-instance subsections ``payments.<slug>`` (07 §3.2: one block per payment instance).
PAYMENTS_SECTION: Final = "payments"

#: Ordered ``.env`` sections / settings screen groups (07 §3.2, without the site payment channel).
#: ``content`` («Контент и медиа») is an addition to the 07 list: media library and content import limits.
SECTIONS: list[tuple[str, str]] = [
    ("boot", "Запуск"),
    ("database", "База данных"),
    ("telegram", "Telegram"),
    ("remnawave", "Remnawave"),
    ("admin_chat", "Админ-чат и уведомления"),
    ("sales", "Продажи и триал"),
    ("wallet", "Баланс и оплата"),
    ("payments", "Платёжки"),
    ("referral", "Рефералка"),
    ("promo", "Промо и ссылки"),
    ("support", "Поддержка"),
    ("broadcast", "Рассылки"),
    ("content", "Контент и медиа"),
    ("reports", "Отчёты и бэкапы"),
    ("logs", "Логи и ошибки"),
    ("modules", "Модули"),
    ("system", "Система"),
]


class Registry:
    """Ordered collection of :class:`SettingDef` with alias resolution. Build once at startup.

    Sections are one level deep: the sections given to the constructor are top-level; a section added later
    is a subsection of ``parent`` (by default «Модули», so a module's own section lands under it, X12) and is
    placed right after its parent's last subsection — the ``.env`` and the settings screen keep that order.
    """

    def __init__(self, sections: Iterable[tuple[str, str]] = SECTIONS) -> None:
        self._sections: dict[str, str] = {}
        self._order: list[str] = []
        self._parents: dict[str, str] = {}  # subsection -> parent
        for sid, title in sections:
            self._insert_section(sid, title, None)
        self._defs: dict[str, SettingDef] = {}
        self._names: dict[str, str] = {}  # key or alias (casefolded upper) -> canonical key
        self._checks: list[SnapshotCheck] = []

    # ---- building

    def add_section(self, sid: str, title: str, *, parent: str | None = MODULES_SECTION) -> None:
        """Add a section. ``parent`` (a top-level section) makes it a subsection; ``None`` — a top-level one
        at the end. Without the default parent «Модули» (a custom section list) the section is top-level."""
        if parent is not None and parent not in self._sections:
            if parent != MODULES_SECTION:
                raise ValueError(f"unknown parent section: {parent!r}")
            parent = None
        if parent is not None and parent in self._parents:
            raise ValueError(f"section {parent!r} is a subsection itself")
        self._insert_section(sid, title, parent)

    def _insert_section(self, sid: str, title: str, parent: str | None) -> None:
        if not _SECTION_ID_RE.fullmatch(sid):
            raise ValueError(f"invalid section id: {sid!r}")
        if sid in self._sections:
            raise ValueError(f"duplicate section: {sid}")
        if not title.strip():
            raise ValueError("section title must not be empty")
        self._sections[sid] = title
        if parent is None:
            self._order.append(sid)
            return
        at = self._order.index(parent) + 1
        while at < len(self._order) and self._parents.get(self._order[at]) == parent:
            at += 1
        self._order.insert(at, sid)
        self._parents[sid] = parent

    def add(self, defn: SettingDef) -> SettingDef:
        """Add a definition. A section ``<parent>.<child>`` that does not exist yet (e.g. a copied
        ``payments.<slug>`` key) is created as a subsection of the top-level ``<parent>``."""
        sections: Mapping[str, str] = self._sections
        parent, dot, child = defn.section.partition(".")
        auto = (
            bool(dot) and defn.section not in sections and parent in sections and parent not in self._parents
        )
        if auto:
            sections = {**sections, defn.section: f"{sections[parent]}: {child}"}
        _check_def(defn, sections)
        for name in defn.names:
            if name in self._names:
                raise ValueError(f"{defn.key}: name {name} is already used by {self._names[name]}")
        if auto:
            self.add_section(defn.section, sections[defn.section], parent=parent)
        self._defs[defn.key] = defn
        for name in defn.names:
            self._names[name] = defn.key
        self.__dict__.pop("fingerprint", None)
        return defn

    def add_check(self, check: SnapshotCheck) -> None:
        """Register a cross-key validation of candidate snapshots (e.g. webhook mode needs a public URL)."""
        self._checks.append(check)

    # ---- reading

    def get(self, key_or_alias: str) -> SettingDef:
        canonical = self.resolve_alias(key_or_alias)
        if canonical is None:
            raise KeyError(f"unknown setting: {key_or_alias}")
        return self._defs[canonical]

    def find(self, key_or_alias: str) -> SettingDef | None:
        canonical = self.resolve_alias(key_or_alias)
        return None if canonical is None else self._defs[canonical]

    def resolve_alias(self, key: str) -> str | None:
        """Canonical key for a key or one of its aliases (case-insensitive); None if unknown."""
        if not isinstance(key, str):
            return None
        return self._names.get(key.strip().upper())

    def all(self) -> list[SettingDef]:
        return list(self._defs.values())

    def keys(self) -> list[str]:
        return list(self._defs)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.resolve_alias(key) is not None

    def __len__(self) -> int:
        return len(self._defs)

    def __iter__(self) -> Iterator[str]:
        """Canonical keys in registration order."""
        return iter(list(self._defs))

    @property
    def sections(self) -> list[tuple[str, str]]:
        """All sections, subsections right after their parent: ``(id, own title)``."""
        return [(sid, self._sections[sid]) for sid in self._order]

    @property
    def env_sections(self) -> list[tuple[str, str]]:
        """:attr:`sections` with the titles of the ``.env`` headers: a module subsection is shown as
        «Модули › <title>» (the parent header itself is omitted by the renderer while it has no keys)."""
        out: list[tuple[str, str]] = []
        for sid in self._order:
            parent = self._parents.get(sid)
            title = self._sections[sid]
            if parent == MODULES_SECTION:
                title = f"{self._sections[parent]} › {title}"
            out.append((sid, title))
        return out

    def section_title(self, sid: str) -> str:
        return self._sections[sid]

    def parent(self, sid: str) -> str | None:
        """Parent of a subsection; ``None`` for a top-level section. ``KeyError`` for an unknown id."""
        if sid not in self._sections:
            raise KeyError(f"unknown section: {sid}")
        return self._parents.get(sid)

    def subsections(self, sid: str) -> list[str]:
        """Subsections of a top-level section, in order."""
        return [s for s in self._order if self._parents.get(s) == sid]

    def top_sections(self) -> list[tuple[str, str]]:
        """Top-level sections only (the root of the settings screen)."""
        return [(sid, self._sections[sid]) for sid in self._order if sid not in self._parents]

    def by_section(self) -> dict[str, list[SettingDef]]:
        """Section id → definitions, in section order (sections without keys included, empty)."""
        out: dict[str, list[SettingDef]] = {sid: [] for sid in self._order}
        for defn in self._defs.values():
            out[defn.section].append(defn)
        return out

    @property
    def checks(self) -> tuple[SnapshotCheck, ...]:
        return tuple(self._checks)

    @cached_property
    def fingerprint(self) -> str:
        """Hash of keys, types and defaults: changes when a release changes the registry."""
        parts = [
            [d.key, d.kind, d.section, d.apply.value, None if d.is_secret else values.to_json(d, d.default)]
            for d in self._defs.values()
        ]
        blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _check_def(defn: SettingDef, sections: Mapping[str, str]) -> None:
    if not _KEY_RE.fullmatch(defn.key):
        raise ValueError(f"invalid setting key: {defn.key!r}")
    for alias in defn.aliases:
        if not _KEY_RE.fullmatch(alias) or alias == defn.key:
            raise ValueError(f"{defn.key}: invalid alias {alias!r}")
    if defn.section not in sections:
        raise ValueError(f"{defn.key}: unknown section {defn.section!r}")
    kind = values.kind_of(defn)  # raises TypeError for unsupported types
    if defn.apply is Apply.RELOAD and not defn.component:
        raise ValueError(f"{defn.key}: RELOAD needs a component")
    if kind == "enum" and not defn.choices:
        raise ValueError(f"{defn.key}: enum needs choices")
    if not defn.title.strip() or not defn.description.strip():
        raise ValueError(f"{defn.key}: title and description are required")
    if defn.default is None:
        if not defn.nullable:
            raise ValueError(f"{defn.key}: default None requires nullable=True")
        return
    # Defaults are checked for type and range only: the custom validator may depend on the environment
    # (e.g. the time zone database), and a default must never stop the process from starting.
    stripped = dataclasses.replace(defn, validator=None) if defn.validator else defn
    try:
        values.coerce(stripped, defn.default)
    except values.SettingValueError as exc:
        raise ValueError(f"{defn.key}: invalid default: {exc}") from None


# --------------------------------------------------------------------------------------------- validators

_BOT_TOKEN_RE: Final = re.compile(r"\d{5,20}:[A-Za-z0-9_-]{30,64}")
_HHMM_RE: Final = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_PROXY_SCHEMES: Final = ("http://", "https://", "socks4://", "socks5://", "socks5h://")
_TZ_RE: Final = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){0,2}|UTC|Etc/[A-Za-z0-9_+\-]+")


def _bot_token(value: Any) -> None:
    if not _BOT_TOKEN_RE.fullmatch(str(value)):
        raise ValueError("это не похоже на токен бота (формат 123456:ABC…, выдаёт @BotFather)")


def _fernet_key(value: Any) -> None:
    from svbg.core.crypto import Crypto, CryptoError

    try:
        Crypto([str(value)])
    except CryptoError:
        raise ValueError("ключ шифрования повреждён (ожидался ключ Fernet, 44 символа)") from None


def _database_url(value: Any) -> None:
    if not str(value).startswith(("postgresql://", "postgresql+asyncpg://", "postgres://")):
        raise ValueError("ожидался адрес вида postgresql://user:password@host:5432/db")


def _proxy(value: Any) -> None:
    if not str(value).lower().startswith(_PROXY_SCHEMES):
        raise ValueError("ожидался адрес прокси вида socks5://user:pass@host:1080 или http://host:3128")


def _time_hhmm(value: Any) -> None:
    if not _HHMM_RE.fullmatch(str(value)):
        raise ValueError("ожидалось время в формате ЧЧ:ММ, например 09:00")


def _timezone(value: Any) -> None:
    name = str(value)
    try:
        zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        if not zoneinfo.available_timezones() and _TZ_RE.fullmatch(name):
            return  # no tz database on this host: accept a well-formed name, the component reports later
        raise ValueError("неизвестный часовой пояс (пример: Europe/Moscow)") from None
    except ValueError:
        raise ValueError("неизвестный часовой пояс (пример: Europe/Moscow)") from None


def _key_names(value: Any) -> None:
    for item in value or []:
        if not _KEY_RE.fullmatch(str(item).upper()):
            raise ValueError(f"недопустимое имя переменной: {item}")


def _webhook_secret(value: Any) -> None:
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", text):
        raise ValueError("секрет: от 16 символов, только латиница, цифры, _ и -")


_PANEL_SECRET_RE: Final = re.compile(r"[A-Za-z0-9]{32,256}")


def _panel_webhook_secret(value: Any) -> None:
    """Remnawave accepts ``WEBHOOK_SECRET_HEADER`` of ≥ 32 Latin letters and digits (02 §5.7)."""
    if not _PANEL_SECRET_RE.fullmatch(str(value)):
        raise ValueError("секрет вебхуков панели: от 32 символов, только латинские буквы и цифры")


def _check_webhook_mode(cfg: Mapping[str, Any], changed: frozenset[str]) -> Mapping[str, str]:
    if cfg.get("BOT_MODE") == "webhook" and not cfg.get("PUBLIC_URL"):
        keys = changed & {"BOT_MODE", "PUBLIC_URL"} or {"BOT_MODE"}
        return dict.fromkeys(keys, "режиму webhook нужен публичный адрес (PUBLIC_URL)")
    return {}


def _check_sub_button_days(cfg: Mapping[str, Any], changed: frozenset[str]) -> Mapping[str, str]:
    blue, red = cfg.get("SUB_BUTTON_BLUE_DAYS"), cfg.get("SUB_BUTTON_RED_DAYS")
    if isinstance(blue, int) and isinstance(red, int) and red >= blue:
        keys = changed & {"SUB_BUTTON_BLUE_DAYS", "SUB_BUTTON_RED_DAYS"} or {"SUB_BUTTON_RED_DAYS"}
        return dict.fromkeys(keys, f"красный порог ({red} дн.) должен быть меньше синего ({blue} дн.)")
    return {}


def _positive_ints(value: Any) -> None:
    for item in value or []:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError("нужны целые положительные числа через запятую, например 179, 499, 899")


_PANEL_PREFIX_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,12}")
#: Placeholders of ``PANEL_DESCRIPTION_TEMPLATE`` (06 §3.2: Bedolaga ``{username}`` → ``@{tg_username}``).
PANEL_DESCRIPTION_FIELDS: Final = ("full_name", "tg_username", "telegram_id", "public_id")
_PLACEHOLDER_RE: Final = re.compile(r"\{([^{}]*)\}")
#: Keys the bot no longer has. An old line in ``.env`` is dropped by the mirror (and by ``svbg env render``),
#: a stored row is ignored; nothing fails at startup. The bot is Russian-only, so the language keys are gone.
RETIRED_KEYS: Final = frozenset({"DEFAULT_LANGUAGE", "I18N_AVAILABLE", "I18N_ASK_ON_START"})


def _panel_prefix(value: Any) -> None:
    """Same rule as ``svbg.subscriptions.service.make_username``: panel username ``{prefix}{tg_id}``."""
    if not _PANEL_PREFIX_RE.fullmatch(str(value)):
        raise ValueError("префикс: 1–12 символов, латиница, цифры, _ и -")


def _description_template(value: Any) -> None:
    text = str(value)
    if len(text) > 200:
        raise ValueError("шаблон длиннее 200 символов (лимит описания в панели)")
    for name in _PLACEHOLDER_RE.findall(text):
        if name not in PANEL_DESCRIPTION_FIELDS:
            allowed = ", ".join("{" + f + "}" for f in PANEL_DESCRIPTION_FIELDS)
            raise ValueError(f"неизвестная подстановка {{{name}}}; можно: {allowed}")


#: The entry captcha's emojis (``svbg.tg.user.captcha``): one of them is the answer, all are the buttons.
CAPTCHA_EMOJIS_DEFAULT: Final = ("🍎", "🥝", "🍓", "🍌", "🍑", "🌶️")
CAPTCHA_EMOJIS_MAX: Final = 12
_EMOJI_MAX_CHARS: Final = 16  # a flag or a family emoji is several code points


def _captcha_emojis(value: Any) -> None:
    items = [str(item) for item in value or []]
    if len(items) < 2:
        raise ValueError("нужно хотя бы 2 значка через запятую, например 🍎, 🍌, 🍓")
    if len(items) > CAPTCHA_EMOJIS_MAX:
        raise ValueError(f"не больше {CAPTCHA_EMOJIS_MAX} значков")
    if len(set(items)) != len(items):
        raise ValueError("значки не должны повторяться")
    for item in items:
        if (
            len(item) > _EMOJI_MAX_CHARS
            or any(ch.isalpha() or ch.isspace() for ch in item)
            or not any(ord(ch) >= 0x2000 for ch in item)
        ):
            raise ValueError(f"«{item}» не похоже на эмодзи")


#: User notification switches (06 M10): key, title, description.
_NOTIFY_TOGGLES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "NOTIFY_USER_EXPIRING",
        "Напоминать о конце подписки",
        "Сообщение пользователю за несколько часов до окончания платной подписки (часы — "
        "NOTIFY_EXPIRING_HOURS).",
    ),
    (
        "NOTIFY_USER_EXPIRED",
        "Сообщать об окончании подписки",
        "Сообщение, когда подписка закончилась, с кнопкой продления.",
    ),
    (
        "NOTIFY_USER_TRIAL_ENDING",
        "Напоминать о конце триала",
        "Сообщение за NOTIFY_TRIAL_ENDING_HOURS ч до конца триала с предложением купить подписку.",
    ),
    (
        "NOTIFY_USER_TRAFFIC",
        "Сообщать о трафике",
        "Сообщение, когда трафик подписки почти или полностью израсходован.",
    ),
    (
        "NOTIFY_USER_FIRST_CONNECTED",
        "Поздравлять с первым подключением",
        "Сообщение после первого подключения к VPN.",
    ),
    (
        "NOTIFY_USER_DEVICES",
        "Сообщать о новых устройствах",
        "Сообщение, когда к подписке подключилось новое устройство.",
    ),
    (
        "NOTIFY_USER_REVOKED",
        "Сообщать о перевыпуске ссылки",
        "Сообщение, когда ссылка подписки перевыпущена и её нужно заменить на устройствах.",
    ),
)


# --------------------------------------------------------------------------------------------- core keys


def core_registry() -> Registry:
    """All core keys known in stages 0–2 (stage 2: sales, wallet, notifications, payment instances)."""
    reg = Registry()
    add = reg.add
    rw = {"apply": Apply.RELOAD, "component": "remnawave", "owner_only": True, "section": "remnawave"}
    # Read on every use (webhook route) / at start (reconciliation schedule): no panel self-test on change.
    rw_hot = {**rw, "apply": Apply.HOT}
    rw_restart = {**rw, "apply": Apply.RESTART}

    # ---- Запуск (bootstrap)
    add(
        SettingDef(
            "BOT_TOKEN",
            "secret",
            None,
            "boot",
            "Токен бота",
            "Токен Telegram-бота от @BotFather. Смена применяется без перезапуска.",
            apply=Apply.RELOAD,
            component="bot",
            bootstrap=True,
            nullable=True,
            owner_only=True,
            validator=_bot_token,
            tags=("токен", "token", "botfather"),
            hint="123456789:AA…",
        )
    )
    add(
        SettingDef(
            "SECRET_KEY",
            "secret",
            None,
            "boot",
            "Ключ шифрования",
            "Ключ шифрования секретов в БД. Сгенерирован при первом запуске. НЕ ТЕРЯЙТЕ и не меняйте "
            "вручную: без него секреты в БД не расшифровать (смена — только командой ротации ключа).",
            bootstrap=True,
            nullable=True,
            owner_only=True,
            file_only=True,
            readonly=True,
            validator=_fernet_key,
            advanced=True,
            tags=("ключ", "шифрование", "fernet"),
        )
    )
    add(
        SettingDef(
            "OWNER_IDS",
            "list[int]",
            [],
            "boot",
            "Владельцы",
            "Telegram ID владельцев через запятую. Пусто — владелец назначается одноразовой ссылкой из лога.",
            bootstrap=True,
            owner_only=True,
            tags=("владелец", "owner", "админ"),
        )
    )
    add(
        SettingDef(
            "LOCKED_KEYS",
            "list[str]",
            [],
            "boot",
            "Заблокированные ключи",
            "Для IaC: ключи, значения которых берутся из окружения контейнера и не меняются в боте.",
            apply=Apply.RESTART,
            bootstrap=True,
            owner_only=True,
            file_only=True,
            advanced=True,
            validator=_key_names,
        )
    )
    add(
        SettingDef(
            "DATA_DIR",
            str,
            "./data",
            "boot",
            "Каталог данных",
            "Каталог с .env, бэкапами и файлами. Задаётся только в docker-compose/окружении.",
            apply=Apply.RESTART,
            bootstrap=True,
            owner_only=True,
            file_only=True,
            readonly=True,
            in_file=False,
            advanced=True,
        )
    )
    # ---- База данных
    add(
        SettingDef(
            "DATABASE_URL",
            "secret",
            None,
            "database",
            "Адрес PostgreSQL",
            "Подключение к PostgreSQL. Смена — через мастер «Перенос БД», вступает в силу после перезапуска.",
            apply=Apply.RESTART,
            bootstrap=True,
            nullable=True,
            owner_only=True,
            validator=_database_url,
            advanced=True,
            tags=("postgres", "бд", "база"),
            hint="postgresql://svbg:пароль@postgres:5432/svbg",
        )
    )
    # ---- Telegram
    add(
        SettingDef(
            "TELEGRAM_PROXY",
            "secret",
            None,
            "telegram",
            "Прокси для Telegram",
            "Прокси для доступа к Telegram с заблокированного сервера. Пусто — напрямую.",
            apply=Apply.RELOAD,
            component="bot",
            bootstrap=True,
            nullable=True,
            owner_only=True,
            validator=_proxy,
            advanced=True,
            tags=("proxy", "socks"),
            hint="socks5://user:pass@host:1080",
        )
    )
    add(
        SettingDef(
            "TELEGRAM_API_URL",
            "url",
            None,
            "telegram",
            "Свой Bot API сервер",
            "Адрес собственного сервера Bot API. Пусто — api.telegram.org.",
            apply=Apply.RELOAD,
            component="bot",
            bootstrap=True,
            nullable=True,
            owner_only=True,
            advanced=True,
        )
    )
    add(
        SettingDef(
            "BOT_MODE",
            "enum",
            "polling",
            "telegram",
            "Режим получения обновлений",
            "polling — бот сам опрашивает Telegram; webhook — Telegram присылает обновления на PUBLIC_URL.",
            apply=Apply.RELOAD,
            component="bot",
            choices=("polling", "webhook"),
            owner_only=True,
            tags=("webhook", "polling"),
        )
    )
    add(
        SettingDef(
            "PUBLIC_URL",
            "url",
            None,
            "telegram",
            "Публичный адрес",
            "Внешний https-адрес бота (для webhook-режима и вебхуков платёжек).",
            apply=Apply.RELOAD,
            component="bot",
            nullable=True,
            owner_only=True,
            tags=("домен", "domain", "url"),
            hint="https://bot.example.com",
        )
    )
    add(
        SettingDef(
            "WEBHOOK_SECRET",
            "secret",
            None,
            "telegram",
            "Секрет вебхука Telegram",
            "Секрет заголовка X-Telegram-Bot-Api-Secret-Token для webhook-режима.",
            apply=Apply.RELOAD,
            component="bot",
            nullable=True,
            owner_only=True,
            validator=_webhook_secret,
            advanced=True,
        )
    )
    # ---- Remnawave
    add(
        SettingDef(
            "REMNAWAVE_URL",
            "url",
            None,
            title="Адрес панели Remnawave",
            description="Адрес панели, например https://panel.example.com или http://remnawave:3000.",
            nullable=True,
            tags=("панель", "panel"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_TOKEN",
            "secret",
            None,
            title="API-токен Remnawave",
            description="Токен из панели: Настройки → API-токены.",
            nullable=True,
            tags=("токен", "token"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_CADDY_TOKEN",
            "secret",
            None,
            title="Токен Caddy",
            description="Заголовок авторизации для панели за Caddy с защитой (если используется).",
            nullable=True,
            advanced=True,
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_COOKIE",
            "secret",
            None,
            title="Cookie панели",
            description="Cookie для панели за нестандартным прокси, формат name=value.",
            nullable=True,
            advanced=True,
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_CF_CLIENT_ID",
            str,
            None,
            title="Cloudflare Access: Client ID",
            description="Для панели за Cloudflare Zero Trust: заголовок CF-Access-Client-Id. "
            "Пусто — не используется.",
            nullable=True,
            advanced=True,
            tags=("cloudflare", "cf"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_CF_CLIENT_SECRET",
            "secret",
            None,
            title="Cloudflare Access: Client Secret",
            description="Пара к REMNAWAVE_CF_CLIENT_ID: заголовок CF-Access-Client-Secret.",
            nullable=True,
            advanced=True,
            tags=("cloudflare", "cf"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_TLS_VERIFY",
            bool,
            True,
            title="Проверять сертификат панели",
            description="Проверка TLS-сертификата панели. Выключайте только для панели во внутренней сети "
            "с самоподписанным сертификатом.",
            advanced=True,
            tags=("tls", "ssl", "сертификат"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_RPS_INTERACTIVE",
            int,
            20,
            title="Темп запросов к панели: действия",
            description="Сколько запросов в секунду бот делает к панели по нажатиям пользователей и админов.",
            min=1,
            max=200,
            advanced=True,
            tags=("rps", "лимит", "темп"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_RPS_BACKGROUND",
            int,
            5,
            title="Темп запросов к панели: фон",
            description="Запросов в секунду для сверки, импорта и массовых операций. Фон уступает действиям.",
            min=1,
            max=100,
            advanced=True,
            tags=("rps", "лимит", "темп", "сверка"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_CONFIRMED_MAJOR",
            int,
            None,
            title="Подтверждённая версия панели",
            description="Новая мажорная версия панели, которую владелец разрешил после проверки "
            "(например 4). Пока не подтверждена, бот с такой панелью работает только на чтение.",
            nullable=True,
            min=1,
            max=99,
            advanced=True,
            tags=("версия", "version"),
            **rw,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_WEBHOOK_SECRET",
            "secret",
            None,
            title="Секрет вебхуков Remnawave",
            description="Совпадает с WEBHOOK_SECRET_HEADER в .env панели (от 32 латинских букв и цифр). "
            "Пусто — вебхуки панели не принимаются, бот сверяется с панелью по расписанию.",
            nullable=True,
            advanced=True,
            validator=_panel_webhook_secret,
            tags=("вебхук", "webhook"),
            **rw_hot,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_WEBHOOK_SECRET_PREVIOUS",
            "secret",
            None,
            title="Прежний секрет вебхуков",
            description="На время смены секрета: вебхуки с прежним секретом тоже принимаются. Очистите после "
            "перезапуска панели с новым секретом. При смене секрета в боте прежний принимается ещё 24 ч сам.",
            nullable=True,
            advanced=True,
            validator=_panel_webhook_secret,
            tags=("вебхук", "webhook", "ротация"),
            **rw_hot,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_SYNC_MINUTES",
            int,
            60,
            title="Полная сверка с панелью (с вебхуками)",
            description="Раз во сколько минут бот сверяет всех пользователей с панелью, когда вебхуки "
            "работают.",
            min=15,
            max=1440,
            advanced=True,
            tags=("сверка", "sync"),
            **rw_restart,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_SYNC_NO_WEBHOOKS_MINUTES",
            int,
            15,
            title="Полная сверка с панелью (без вебхуков)",
            description="То же, когда вебхуки панели не настроены. Быстрая проверка ограниченных (LIMITED) "
            "идёт каждые 5 мин.",
            min=5,
            max=1440,
            advanced=True,
            tags=("сверка", "sync"),
            **rw_restart,
        )
    )
    add(
        SettingDef(
            "REMNAWAVE_ALLOW_PLAIN_HTTP",
            bool,
            False,
            title="Разрешить http:// к внешней панели",
            description="По http:// токен панели уходит без шифрования, поэтому внешний адрес без https "
            "отклоняется. Включайте, только если канал защищён иначе (VPN). Адреса docker-сети "
            "(http://remnawave:3000) работают и без этого.",
            advanced=True,
            tags=("http", "tls", "небезопасно"),
            **rw,
        )
    )
    add(
        SettingDef(
            "CATALOG_LOCATIONS_SYNC_MINUTES",
            int,
            10,
            title="Обновление списка локаций",
            description="Раз во сколько минут бот перечитывает сквады панели в список локаций каталога.",
            min=1,
            max=1440,
            advanced=True,
            tags=("локации", "сквады", "squads", "сверка"),
            **rw_restart,
        )
    )
    add(
        SettingDef(
            "PANEL_USERNAME_PREFIX",
            str,
            "sv_",
            title="Префикс имени в панели",
            description="Имя пользователя в панели — префикс + Telegram ID (sv_123456789, вторая подписка — "
            "sv_123456789_2). Действует для новых подписок; существующие не переименовываются.",
            validator=_panel_prefix,
            advanced=True,
            tags=("username", "имя", "шаблон"),
            hint="user_",
            **rw_hot,
        )
    )
    add(
        SettingDef(
            "PANEL_DESCRIPTION_TEMPLATE",
            str,
            None,
            title="Описание пользователя в панели",
            description="Шаблон описания, которое бот пишет при создании пользователя панели. Подстановки: "
            "{full_name}, {tg_username}, {telegram_id}, {public_id}. Пусто — sv:<номер подписки>.",
            nullable=True,
            validator=_description_template,
            advanced=True,
            tags=("описание", "description", "шаблон"),
            hint="{full_name} @{tg_username}",
            **rw_hot,
        )
    )
    # ---- Админ-чат
    add(
        SettingDef(
            "ADMIN_CHAT_ID",
            int,
            None,
            "admin_chat",
            "Админ-чат",
            "ID супергруппы с темами для уведомлений. Пусто — уведомления в личку владельцам.",
            apply=Apply.RELOAD,
            component="admin_chat",
            nullable=True,
            owner_only=True,
            tags=("чат", "группа", "уведомления"),
        )
    )
    add(
        SettingDef(
            "NOTIFY_ADMIN_NODES",
            bool,
            True,
            "admin_chat",
            "Сообщать о нодах",
            "Сообщение в админ-чат, когда нода панели отключилась или снова подключилась (по вебхукам "
            "Remnawave).",
            owner_only=True,
            tags=("уведомления", "ноды", "nodes"),
        )
    )
    # ---- Продажи и триал
    add(
        SettingDef(
            "TRIAL_DAYS",
            int,
            3,
            "sales",
            "Дней пробного периода",
            "Длительность пробного периода, дней. 0 — триал выключен.",
            min=0,
            max=365,
            tags=("пробный", "trial", "триал"),
        )
    )
    add(
        SettingDef(
            "TRIAL_AUDIENCE",
            "enum",
            "all",
            "sales",
            "Кому доступен триал",
            "Кто может взять триал: all — все, channel_members — только подписчики канала.",
            choices=("all", "channel_members"),
            tags=("пробный", "trial"),
        )
    )
    add(
        SettingDef(
            "TRIAL_CARRY_OVER",
            bool,
            False,
            "sales",
            "Переносить остаток триала",
            "При покупке во время триала неиспользованные дни триала добавляются к оплаченному сроку.",
            advanced=True,
            tags=("пробный", "trial", "остаток"),
        )
    )
    add(
        SettingDef(
            "SUB_BUTTON_BLUE_DAYS",
            int,
            10,
            "sales",
            "Кнопка подписки синеет за N дней до конца",
            "Кнопка «👤 Профиль» в меню показывает, сколько осталось. Пока дней больше этого числа, она "
            "зелёная, меньше — синяя. У пробного периода кнопка всегда красная.",
            min=1,
            max=365,
            tags=("подписка", "кнопка", "цвет", "меню"),
        )
    )
    add(
        SettingDef(
            "SUB_BUTTON_RED_DAYS",
            int,
            3,
            "sales",
            "Кнопка подписки краснеет за N дней до конца",
            "Когда до конца оплаченной подписки остаётся меньше этого числа дней, кнопка «👤 Профиль» "
            "становится красной. Должно быть меньше, чем у синего цвета.",
            min=1,
            max=365,
            tags=("подписка", "кнопка", "цвет", "меню"),
        )
    )
    add(
        SettingDef(
            "REQUIRED_CHANNEL_ID",
            int,
            None,
            "sales",
            "Обязательный канал",
            "ID канала, подписка на который нужна для триала/доступа. Пусто — не требуется.",
            nullable=True,
            tags=("канал", "подписка", "channel"),
        )
    )
    add(
        SettingDef(
            "REQUIRED_CHANNEL_URL",
            "url",
            None,
            "sales",
            "Ссылка на обязательный канал",
            "Ссылка для кнопки «Подписаться» (https://t.me/… или приглашение). Пусто — кнопки нет, "
            "пользователь видит только просьбу подписаться.",
            nullable=True,
            tags=("канал", "подписка", "channel", "ссылка"),
            hint="https://t.me/your_channel",
        )
    )
    add(
        SettingDef(
            "CHANNEL_REQUIRED_FOR",
            "enum",
            "trial",
            "sales",
            "Для чего нужна подписка на канал",
            "trial — канал нужен только для триала; all — без подписки на канал бот не пускает дальше "
            "/start (покупки тоже).",
            choices=("trial", "all"),
            tags=("канал", "подписка", "channel"),
        )
    )
    add(
        SettingDef(
            "CHANNEL_LEAVE_ACTION",
            "enum",
            "trial",
            "sales",
            "Что делать при отписке от канала",
            "off — ничего; trial — отключить триал (вернётся, если пользователь подпишется снова); all — "
            "отключить и платные подписки. Боту нужны права администратора канала.",
            choices=("off", "trial", "all"),
            tags=("канал", "отписка", "channel"),
        )
    )
    add(
        SettingDef(
            "CAPTCHA_ENABLED",
            bool,
            True,
            "sales",
            "Капча при входе",
            "Новый пользователь после /start нажимает на нужный значок среди нескольких, и только потом "
            "попадает в меню. Кто прошёл капчу один раз, больше её не видит. Сотрудники не видят никогда.",
            tags=("капча", "captcha", "боты", "защита"),
        )
    )
    add(
        SettingDef(
            "CAPTCHA_EMOJIS",
            "list[str]",
            list(CAPTCHA_EMOJIS_DEFAULT),
            "sales",
            "Значки капчи",
            "Из них капча выбирает, на какой нажать, и показывает все кнопками в случайном порядке. "
            f"Через запятую, от 2 до {CAPTCHA_EMOJIS_MAX} разных.",
            validator=_captcha_emojis,
            advanced=True,
            tags=("капча", "captcha", "эмодзи"),
            hint=", ".join(CAPTCHA_EMOJIS_DEFAULT),
        )
    )
    add(
        SettingDef(
            "REISSUE_COOLDOWN_MINUTES",
            int,
            10,
            "sales",
            "Пауза между перевыпусками ссылки",
            "Как часто пользователь может перевыпустить ссылку подписки, минут. 0 — без ограничения.",
            min=0,
            max=1440,
            advanced=True,
            tags=("ссылка", "перевыпуск", "кулдаун"),
        )
    )
    add(
        SettingDef(
            "DEVICES_RESET_COOLDOWN_MINUTES",
            int,
            5,
            "sales",
            "Пауза между сбросами устройств",
            "Как часто пользователь может сбросить все устройства, минут. 0 — без ограничения.",
            min=0,
            max=1440,
            advanced=True,
            tags=("устройства", "hwid", "кулдаун"),
        )
    )
    add(
        SettingDef(
            "DEVICES_CACHE_TTL_S",
            int,
            300,
            "sales",
            "Сколько хранить список устройств",
            "Список устройств берётся из панели в фоне и показывается из кеша столько секунд; "
            "кнопка «Обновить» запрашивает заново.",
            min=10,
            max=86_400,
            advanced=True,
            tags=("устройства", "hwid", "кеш"),
        )
    )
    add(
        SettingDef(
            "PRICING_ROUNDING",
            bool,
            False,
            "sales",
            "Округлять цены",
            "Цена после скидок округляется до целых единиц валюты (копейки отбрасываются в пользу "
            "покупателя).",
            advanced=True,
            tags=("цена", "округление", "скидка"),
        )
    )
    add(
        SettingDef(
            "ONBOARDING_ASK_REFERRAL_CODE",
            bool,
            False,
            "sales",
            "Спрашивать код приглашения",
            "При первом /start без ссылки бот предлагает ввести код пригласившего (можно пропустить).",
            advanced=True,
            tags=("онбординг", "реферал", "код"),
        )
    )
    add(
        SettingDef(
            "ONBOARDING_RULES",
            "enum",
            "off",
            "sales",
            "Согласие с правилами при старте",
            "on — новый пользователь подтверждает правила (страница «Правила») до работы с ботом; "
            "off — не спрашивать.",
            choices=("off", "on"),
            advanced=True,
            tags=("онбординг", "правила", "согласие"),
        )
    )
    # ---- Баланс и оплата
    add(
        SettingDef(
            "WALLET_AUTOCOMPLETE_MINUTES",
            int,
            60,
            "wallet",
            "Автозавершение покупки",
            "Сколько минут после создания счёта пополнения покупка завершается автоматически. "
            "Позже деньги остаются на балансе с уведомлением.",
            min=5,
            max=1440,
            tags=("баланс", "кошелёк"),
        )
    )
    add(
        SettingDef(
            "WALLET_TOPUP_MIN",
            int,
            10,
            "wallet",
            "Минимальное пополнение",
            "Самая маленькая сумма пополнения баланса, в валюте магазина (целых единиц). У способа оплаты "
            "может быть свой минимум — тогда действует больший.",
            min=1,
            max=1_000_000,
            tags=("баланс", "пополнение", "минимум"),
        )
    )
    add(
        SettingDef(
            "WALLET_TOPUP_MAX",
            int,
            100_000,
            "wallet",
            "Максимальное пополнение",
            "Самая большая сумма одного пополнения, в валюте магазина (целых единиц).",
            min=1,
            max=100_000_000,
            tags=("баланс", "пополнение", "максимум"),
        )
    )
    add(
        SettingDef(
            "WALLET_TOPUP_PRESETS",
            "list[int]",
            [],
            "wallet",
            "Быстрые суммы пополнения",
            "Кнопки сумм в «Баланс → Пополнить», через запятую, в валюте магазина. Пусто — первые три цены "
            "тарифа.",
            validator=_positive_ints,
            tags=("баланс", "пополнение", "суммы"),
            hint="179, 499, 899",
        )
    )
    add(
        SettingDef(
            "ADMIN_WALLET_ADJUST_MAX",
            int,
            None,
            "wallet",
            "Лимит корректировки баланса админом",
            "Сколько (в валюте магазина, целых единиц) админ может начислить или списать за одну операцию; "
            "больше — только владелец. Пусто — цена самого дорогого тарифа.",
            nullable=True,
            min=0,
            max=100_000_000,
            owner_only=True,
            tags=("баланс", "админ", "лимит"),
        )
    )
    add(
        SettingDef(
            "ADMIN_WALLET_ADJUST_DAY_MAX",
            int,
            None,
            "wallet",
            "Корректировки баланса админом за сутки",
            "Сумма корректировок баланса одного админа за 24 часа (в валюте магазина). Пусто — "
            "3 × ADMIN_WALLET_ADJUST_MAX.",
            nullable=True,
            min=0,
            max=1_000_000_000,
            owner_only=True,
            tags=("баланс", "админ", "лимит"),
        )
    )
    # ---- Платёжки: общие ключи (ключи инстансов PAY_<SLUG>_* добавляются из плагинов, см. ниже)
    add(
        SettingDef(
            "PAY_CLOCK_SKEW_ALERT_COUNT",
            int,
            5,
            "payments",
            "Порог «часы расходятся»",
            "Сколько вебхуков оплат за час можно отклонить из-за устаревшей метки времени, прежде чем бот "
            "попросит проверить NTP на сервере.",
            min=1,
            max=100,
            advanced=True,
            owner_only=True,
            tags=("ntp", "время", "вебхук"),
        )
    )
    # ---- Уведомления пользователям (раздел «Админ-чат и уведомления»)
    for key, title, about in _NOTIFY_TOGGLES:
        add(SettingDef(key, bool, True, "admin_chat", title, about, tags=("уведомления", "notify")))
    add(
        SettingDef(
            "NOTIFY_EXPIRING_HOURS",
            "list[int]",
            [72, 24],
            "admin_chat",
            "Когда напоминать о конце подписки",
            "За сколько часов до окончания платной подписки напомнить пользователю, через запятую.",
            validator=_positive_ints,
            tags=("уведомления", "notify", "напоминание"),
            hint="72, 24",
        )
    )
    add(
        SettingDef(
            "NOTIFY_TRIAL_ENDING_HOURS",
            int,
            2,
            "admin_chat",
            "Когда напоминать о конце триала",
            "За сколько часов до конца триала напомнить пользователю. 0 — не напоминать.",
            min=0,
            max=72,
            tags=("уведомления", "notify", "триал"),
        )
    )
    # ---- Поддержка
    add(
        SettingDef(
            "SUPPORT_URL",
            "url",
            None,
            "support",
            "Ссылка поддержки",
            "Куда ведёт кнопка «Поддержка» (https://t.me/… или сайт). Пусто — кнопка скрыта.",
            nullable=True,
            tags=("поддержка", "support", "помощь"),
            hint="https://t.me/support_username",
        )
    )
    add(
        SettingDef(
            "SUPPORT_MODE",
            "enum",
            "link",
            "support",
            "Режим поддержки",
            "link — кнопка «Поддержка» ведёт по ссылке SUPPORT_URL; tickets — пользователь пишет прямо "
            "в бота, каждому пользователю своя тема в админ-группе (или в SUPPORT_CHAT_ID), ответ в теме "
            "приходит ему; both — и ссылка, и обращения.",
            choices=("link", "tickets", "both"),
            tags=("поддержка", "support", "тикеты", "обращения"),
        )
    )
    add(
        SettingDef(
            "SUPPORT_CHAT_ID",
            int,
            None,
            "support",
            "Группа поддержки",
            "ID отдельной супергруппы с темами для обращений (бот — админ с правом управлять темами). "
            "Пусто — обращения идут в админ-чат. Отдельная группа нужна при большом потоке: у Telegram лимит "
            "20 сообщений в минуту на группу.",
            nullable=True,
            owner_only=True,
            tags=("поддержка", "support", "тикеты", "группа"),
        )
    )
    # ---- Промо и ссылки
    add(
        SettingDef(
            "DEEPLINK_INTENT_TTL_HOURS",
            int,
            24,
            "promo",
            "Сколько ссылка помнит цель",
            "Сколько часов бот помнит цель и промокод из ссылки, пока человек проходит подписку на канал "
            "и согласие с правилами.",
            min=1,
            max=720,
            advanced=True,
            tags=("ссылка", "диплинк", "deeplink"),
        )
    )
    # ---- Контент и медиа
    add(
        SettingDef(
            "MEDIA_PHOTO_MAX_SIDE",
            int,
            2560,
            "content",
            "Размер фото после сжатия",
            "Длинная сторона загруженного фото после сжатия, пикселей.",
            min=320,
            max=4096,
            advanced=True,
            tags=("медиа", "фото", "картинка"),
        )
    )
    add(
        SettingDef(
            "MEDIA_PHOTO_JPEG_QUALITY",
            int,
            85,
            "content",
            "Качество JPEG",
            "Качество сжатия загруженных фото (60 — меньше файл, 95 — лучше картинка).",
            min=60,
            max=95,
            advanced=True,
            tags=("медиа", "фото", "jpeg"),
        )
    )
    add(
        SettingDef(
            "CONTENT_BACKUPS_KEEP",
            int,
            10,
            "content",
            "Резервные копии контента",
            "Сколько резервных копий контента хранить: копия делается перед каждым импортом content.zip.",
            min=1,
            max=100,
            advanced=True,
            tags=("контент", "импорт", "бэкап"),
        )
    )
    # ---- Отчёты
    add(
        SettingDef(
            "REPORT_DAILY_AT",
            str,
            "09:00",
            "reports",
            "Время ежедневного отчёта",
            "Во сколько (ЧЧ:ММ, по часовому поясу TIMEZONE) присылать ежедневный отчёт.",
            validator=_time_hhmm,
            tags=("отчёт", "report"),
        )
    )
    # ---- Логи
    add(
        SettingDef(
            "LOG_LEVEL",
            "enum",
            "INFO",
            "logs",
            "Уровень логов",
            "Подробность логов: DEBUG, INFO, WARNING, ERROR.",
            choices=("DEBUG", "INFO", "WARNING", "ERROR"),
            advanced=True,
            tags=("логи", "log"),
        )
    )
    # ---- Система
    add(
        SettingDef(
            "TIMEZONE",
            str,
            "Europe/Moscow",
            "system",
            "Часовой пояс",
            "Часовой пояс для отчётов и расписаний (IANA, например Europe/Moscow).",
            validator=_timezone,
            tags=("время", "timezone", "tz"),
        )
    )
    add(
        SettingDef(
            "CURRENCY",
            "enum",
            "RUB",
            "system",
            "Валюта по умолчанию",
            "Валюта цен и баланса по умолчанию.",
            choices=tuple(k for k in CURRENCY_EXPONENT if k != "XTR"),
            tags=("валюта", "currency", "рубль"),
        )
    )
    add(
        SettingDef(
            "MAINTENANCE_MODE",
            "enum",
            "auto",
            "system",
            "Техработы",
            "auto — включаются сами, если панель недоступна дольше 3 мин, и снимаются после восстановления; "
            "on — включены вручную; off — никогда. Во время техработ новые покупки и триалы показывают "
            "заглушку, оплаченное выдаётся после восстановления.",
            choices=("auto", "on", "off"),
            owner_only=True,
            tags=("техработы", "maintenance", "обслуживание"),
        )
    )
    add(
        SettingDef(
            "MAINTENANCE_MESSAGE",
            str,
            None,
            "system",
            "Текст техработ",
            "Что видит пользователь во время техработ. Пусто — стандартный текст.",
            nullable=True,
            tags=("техработы", "maintenance", "текст"),
            hint="Технические работы, скоро вернёмся",
        )
    )
    add(
        SettingDef(
            "ADMIN_GRANT_DAYS_MAX",
            int,
            31,
            "system",
            "Лимит выдачи дней админом",
            "Сколько дней подписки админ может выдать или убавить за одну операцию; больше — только "
            "владелец.",
            min=0,
            max=3650,
            owner_only=True,
            tags=("админ", "дни", "лимит"),
        )
    )
    add(
        SettingDef(
            "ADMIN_GRANT_DAYS_DAY_MAX",
            int,
            None,
            "system",
            "Выдача дней админом за сутки",
            "Сколько дней (±) один админ может выдать за 24 часа. Пусто — 3 × ADMIN_GRANT_DAYS_MAX.",
            nullable=True,
            min=0,
            max=36_500,
            owner_only=True,
            tags=("админ", "дни", "лимит"),
        )
    )
    add(
        SettingDef(
            "ENV_LAYOUT",
            "enum",
            "full",
            "system",
            "Вид файла .env",
            "full — все ключи; compact — только заданные (ключи со значениями по умолчанию скрыты).",
            choices=("full", "compact"),
            owner_only=True,
            advanced=True,
        )
    )
    add(
        SettingDef(
            "ENV_SECRETS",
            "enum",
            "plain",
            "system",
            "Секреты в .env",
            "plain — секреты записаны в файл открыто (права 0600); omit — только в БД, в файле заглушка.",
            choices=("plain", "omit"),
            owner_only=True,
            advanced=True,
            tags=("секреты", "secrets"),
        )
    )
    add(
        SettingDef(
            "IMPORT_SOURCE_DSN",
            "secret",
            None,
            "system",
            "База Bedolaga для переезда",
            "Адрес PostgreSQL старого бота (Bedolaga) для импорта и теневой сверки. Только чтение; после "
            "переезда очистите.",
            nullable=True,
            owner_only=True,
            advanced=True,
            validator=_database_url,
            tags=("импорт", "bedolaga", "переезд", "shadow"),
            hint="postgresql://readonly:пароль@old-host:5432/bedolaga",
        )
    )
    _add_payment_instances(reg)
    reg.add_check(_check_webhook_mode)
    reg.add_check(_check_sub_button_days)
    return reg


def payment_section(slug: str) -> str:
    """Section id of one payment instance: subsection ``payments.<slug>`` of «Платёжки»."""
    return f"{PAYMENTS_SECTION}.{slug}"


def add_payment_instance(
    reg: Registry, slug: str, provider: type[Any], *, providers: Iterable[str] | None = None
) -> list[SettingDef]:
    """Add the ``PAY_<SLUG>_*`` keys of one instance in its own subsection «Платёжка «Title» (инстанс slug)»
    (07 §3.2). Used for the built-in catalog and for extra instances (a second slug of a provider) that the
    app reads from ``payment_instances`` before the settings are loaded.
    """
    from svbg.payments.registry import instance_setting_defs

    sid = payment_section(slug)
    if sid not in dict(reg.sections):
        reg.add_section(
            sid, f"Платёжка «{provider.manifest.title}» (инстанс {slug})", parent=PAYMENTS_SECTION
        )
    defs = instance_setting_defs(slug, provider, providers=list(providers or ()) or None, section=sid)
    for defn in defs:
        reg.add(defn)
    return defs


def _add_payment_instances(reg: Registry) -> None:
    """``PAY_<SLUG>_*`` of every built-in provider (07 §3.2): the catalog at the end of «Платёжки» — a
    block per plugin (slug = plugin slug), off until the owner fills it in (``ENABLED=true`` + keys → the
    instance is created through the same ``apply()`` with a probe). All RELOAD of ``payments.<slug>``.

    Imported lazily: the core layer must not import the payment plugins at module level.
    """
    from svbg.payments.providers import BUILTIN_PROVIDERS

    slugs = [cls.manifest.slug for cls in BUILTIN_PROVIDERS]
    for cls in BUILTIN_PROVIDERS:
        add_payment_instance(reg, cls.manifest.slug, cls, providers=slugs)


# --------------------------------------------------------------------------------------------- modules

#: Extension modules with a ``SPEC`` manifest (decision C2/C7 of the stage 3–4a integration requests).
EXTENSION_MODULES: Final = ("svbg.ext.lte.service", "svbg.ext.ip_guard")


def add_module_settings(reg: Registry, host: Any | None = None) -> int:
    """Add the settings of the owner's modules to a core registry; returns the number of keys added.

    * ops (backups, updates, the daily report): ``svbg.ops.settings.OPS_SETTINGS``;
    * referral program: ``svbg.referral.config.SETTINGS`` (no ``SPEC`` manifest, decision C2);
    * extension modules (LTE, IP Guard): ``host.install_settings`` — their sections become subsections of
      «Модули». ``host`` is the app's ``ExtensionHost``; ``None`` loads :data:`EXTENSION_MODULES` here (for
      ``svbg env init`` without a running app; a broken module is skipped, as in the app).

    Idempotent: a definition already in the registry (the very same object) is skipped, so the app may have
    registered some of them itself. A *different* definition under a known name is an error, as everywhere
    in the registry.
    """
    from svbg.ext.api import ExtensionHost, load_specs
    from svbg.ops.settings import OPS_SETTINGS
    from svbg.referral.config import SETTINGS as REFERRAL_SETTINGS

    added = 0
    for defn in (*OPS_SETTINGS, *REFERRAL_SETTINGS):
        if reg.find(defn.key) is defn:
            continue
        reg.add(defn)
        added += 1
    if host is None:
        specs, _errors = load_specs(EXTENSION_MODULES)
        host = ExtensionHost(specs)
    module_defs = [d for name in host.names for d in host.spec(name).settings]
    if not module_defs or any(reg.find(d.key) is not d for d in module_defs):
        added += host.install_settings(reg)
    return added


def full_registry(host: Any | None = None) -> Registry:
    """Core keys + ops + referral + extension modules: every key of the full ``.env`` (07 §3.2)."""
    reg = core_registry()
    add_module_settings(reg, host)
    return reg
