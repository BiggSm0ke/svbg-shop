"""Declarative provider configuration for payment plugins (SDK 1.0-beta, 07 §4.1, D13).

A plugin describes its settings once::

    class RollyPayConfig(ConfigModel):
        api_key = secret("API-ключ кассы", where="Кабинет RollyPay → API → «Ключ API»")
        signing_secret = secret("Секрет подписи вебхуков", where="Кабинет RollyPay → Вебхуки")
        base_url = url("Адрес API", default="https://api.rollypay.io/api/v1", advanced=True)

and the core turns every field into a setting ``PAY_<SLUG>_<FIELD>`` (title, description, «где взять», secret
flag, default, range) for the bot's settings screen, the ``.env`` mirror and the provider wizard. Values
reach the plugin as a validated, typed instance: ``self.config.api_key``.

Field kinds: ``str``, ``secret``, ``url``, ``int``, ``float``, ``bool``, ``enum``. ``repr`` never shows
secrets.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Self
from urllib.parse import urlsplit

__all__ = [
    "FIELD_KINDS",
    "ConfigError",
    "ConfigField",
    "ConfigModel",
    "check_url",
    "choice",
    "flag",
    "integer",
    "number",
    "secret",
    "text",
    "url",
]

FIELD_KINDS: Final = frozenset({"str", "secret", "url", "int", "float", "bool", "enum"})
_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")
_TRUE: Final = frozenset({"1", "true", "yes", "on", "да", "вкл"})
_FALSE: Final = frozenset({"0", "false", "no", "off", "нет", "выкл"})
_MAX_TEXT: Final = 4096
_MASK: Final = "***"

_M: Final = {
    "required": "обязательное поле",
    "type": "неверный тип значения",
    "int": "ожидалось целое число",
    "float": "ожидалось число",
    "bool": "ожидалось true или false",
    "choice": "допустимые значения: {choices}",
    "min": "не меньше {min:g}",
    "max": "не больше {max:g}",
    "url": "ожидался адрес вида https://… (http:// — только для localhost)",
    "url_host": "этот адрес нельзя использовать: служебный адрес сети",
    "too_long": "слишком длинное значение (больше {n} символов)",
    "pattern": "неверный формат",
    "unknown": "неизвестное поле",
}


class ConfigError(ValueError):
    """Invalid provider configuration. ``errors`` maps field name → owner-facing reason (Russian)."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One configuration field. ``where`` tells the owner where to get the value (shown in the wizard)."""

    kind: str
    title: str
    description: str = ""
    where: str | None = None
    default: Any = None
    required: bool = True
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    advanced: bool = False
    name: str = ""  # set by ConfigModel

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise TypeError(f"unknown config field kind {self.kind!r}")
        if not self.title.strip():
            raise ValueError("config field title must not be empty")
        if self.kind == "enum" and not self.choices:
            raise ValueError("enum field needs choices")

    @property
    def is_secret(self) -> bool:
        return self.kind == "secret"

    @property
    def env_suffix(self) -> str:
        """Suffix of the setting key: ``PAY_<SLUG>_<SUFFIX>``."""
        return self.name.upper()

    def coerce(self, raw: Any) -> Any:
        """Typed value of ``raw`` (``.env`` text or a typed value); ``ValueError`` with a Russian text."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if self.default is not None:
                return self.default
            if self.required:
                raise ValueError(_M["required"])
            return None
        kind = self.kind
        if kind in ("str", "secret", "url", "enum"):
            if not isinstance(raw, str):
                raise ValueError(_M["type"])
            value = raw.strip()
            if len(value) > _MAX_TEXT:
                raise ValueError(_M["too_long"].format(n=_MAX_TEXT))
            if kind == "url":
                check_url(value)
            if kind == "enum" and value not in (self.choices or ()):
                raise ValueError(_M["choice"].format(choices=", ".join(self.choices or ())))
            if self.pattern is not None and not re.fullmatch(self.pattern, value):
                raise ValueError(_M["pattern"])
            return value
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            text_value = str(raw).strip().lower()
            if text_value in _TRUE:
                return True
            if text_value in _FALSE:
                return False
            raise ValueError(_M["bool"])
        if kind == "int":
            if isinstance(raw, bool):
                raise ValueError(_M["int"])
            try:
                number_value: float = int(str(raw).strip())
            except ValueError:
                raise ValueError(_M["int"]) from None
        else:  # float
            if isinstance(raw, bool):
                raise ValueError(_M["float"])
            try:
                number_value = float(str(raw).strip().replace(",", "."))
            except ValueError:
                raise ValueError(_M["float"]) from None
            if math.isnan(number_value) or math.isinf(number_value):
                raise ValueError(_M["float"])
        if self.min is not None and number_value < self.min:
            raise ValueError(_M["min"].format(min=self.min))
        if self.max is not None and number_value > self.max:
            raise ValueError(_M["max"].format(max=self.max))
        return number_value


#: Host names that only ever mean cloud metadata services (credential theft through SSRF).
_METADATA_HOSTS: Final = frozenset({"metadata", "metadata.google.internal", "metadata.goog", "instance-data"})


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_loopback(host: str) -> bool:
    ip = _ip(host)
    if ip is not None:
        return ip.is_loopback
    return host == "localhost" or host.endswith(".localhost")


def check_url(value: str) -> str:
    """An address a plugin may send its secrets to: ``https://`` (``http://`` only to this machine — a typo
    must not send an API key in clear text), a host, no credentials inside, never a link-local / metadata
    address (``169.254.169.254``, ``fe80::…``). ``ValueError`` with a Russian text."""
    if not re.fullmatch(r"https?://[^\s]+", value):
        raise ValueError(_M["url"])
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
        _ = parts.port  # an invalid port raises here
    except ValueError:
        raise ValueError(_M["url"]) from None
    if not host or parts.username is not None or parts.password is not None:
        raise ValueError(_M["url"])
    if parts.scheme == "http" and not _is_loopback(host):
        raise ValueError(_M["url"])
    ip = _ip(host)
    if host in _METADATA_HOSTS or (
        ip is not None
        and not ip.is_loopback
        and (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)
    ):
        raise ValueError(_M["url_host"])
    return value


def _field(kind: str, title: str, **kw: Any) -> Any:
    return ConfigField(kind, title, **kw)


def text(title: str, description: str = "", **kw: Any) -> Any:
    """A plain text field."""
    return _field("str", title, description=description, **kw)


def secret(title: str, description: str = "", **kw: Any) -> Any:
    """A secret (API key, signing secret): encrypted at rest, masked in logs and in the UI."""
    return _field("secret", title, description=description, **kw)


def url(title: str, description: str = "", **kw: Any) -> Any:
    """An ``https://`` address (``http://`` only for localhost), see :func:`check_url`."""
    return _field("url", title, description=description, **kw)


def integer(title: str, description: str = "", **kw: Any) -> Any:
    return _field("int", title, description=description, **kw)


def number(title: str, description: str = "", **kw: Any) -> Any:
    return _field("float", title, description=description, **kw)


def flag(title: str, description: str = "", *, default: bool = False, **kw: Any) -> Any:
    return _field("bool", title, description=description, default=default, required=False, **kw)


def choice(title: str, choices: tuple[str, ...], description: str = "", **kw: Any) -> Any:
    return _field("enum", title, description=description, choices=choices, **kw)


class ConfigModel:
    """Base class of a plugin's configuration. Fields are class attributes built with the helpers above."""

    __fields__: ClassVar[dict[str, ConfigField]] = {}
    __slots__ = ("_values",)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fields: dict[str, ConfigField] = {}
        for base in reversed(cls.__mro__[1:]):
            fields.update(getattr(base, "__fields__", {}))
        for name, value in list(vars(cls).items()):
            if isinstance(value, ConfigField):
                if not _NAME_RE.fullmatch(name):
                    raise ValueError(f"{cls.__name__}: invalid config field name {name!r}")
                fields[name] = ConfigField(
                    value.kind, value.title, value.description, value.where, value.default,
                    value.required, value.choices, value.min, value.max, value.pattern, value.advanced, name,
                )  # fmt: skip
                delattr(cls, name)  # attribute access goes through __getattr__ to the validated values
        cls.__fields__ = fields

    def __init__(self, **values: Any) -> None:
        unknown = set(values) - set(self.__fields__)
        if unknown:
            raise ConfigError(dict.fromkeys(sorted(unknown), _M["unknown"]))
        errors: dict[str, str] = {}
        typed: dict[str, Any] = {}
        for name, fld in self.__fields__.items():
            try:
                typed[name] = fld.coerce(values.get(name))
            except ValueError as exc:
                errors[name] = str(exc)
        if errors:
            raise ConfigError(errors)
        object.__setattr__(self, "_values", typed)

    @classmethod
    def fields(cls) -> Mapping[str, ConfigField]:
        return dict(cls.__fields__)

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Self:
        """Build from a mapping; keys may be field names or ``env_suffix`` (``API_KEY``)."""
        values: dict[str, Any] = {}
        by_suffix = {f.env_suffix: n for n, f in cls.__fields__.items()}
        for key, value in raw.items():
            name = key if key in cls.__fields__ else by_suffix.get(str(key).upper(), key)
            values[name] = value
        return cls(**values)

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_values")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("config is read-only")

    def as_dict(self) -> dict[str, Any]:
        """Typed values (secrets included — only for storage, never for logs)."""
        return dict(self._values)

    def secret_values(self) -> list[str]:
        """Non-empty values of secret fields (registered for log masking by the core)."""
        return [
            str(v) for n, v in self._values.items() if self.__fields__[n].is_secret and v not in (None, "")
        ]

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._values == other._values  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash(tuple(sorted((k, repr(v)) for k, v in self._values.items())))

    def __repr__(self) -> str:
        shown = {
            n: (_MASK if self.__fields__[n].is_secret and v not in (None, "") else v)
            for n, v in self._values.items()
        }
        return f"{type(self).__name__}({shown!r})"
