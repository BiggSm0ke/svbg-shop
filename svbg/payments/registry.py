"""Provider catalog, live payment instances, their HTTP/KV runtime and hot reconfiguration (07 §4.3, D13).

* :class:`ProviderCatalog` — plugin classes by ``Manifest.slug``. A plugin that fails the static part of the
  TestKit (:func:`static_problems`: e.g. a weak webhook scheme without ``fetch_status``) is refused.
* :class:`InstanceRegistry` — rows of ``payment_instances`` turned into live providers, kept in memory: every
  lookup on the hot path (webhook route, the user's «Оплатить» buttons) is a dict access, 0 SQL. Config,
  webhook token and proxy are stored encrypted (``enc:v1:``) and decrypted only here.
* :class:`InstanceHttp` — ``ctx.http`` of one instance: an aiohttp session through the instance's proxy
  (``socks5://``, ``socks5h://``, ``http://``), bounded timeouts and response size, a request counter.
* :class:`PaymentInstanceComponent` — the settings :class:`~svbg.core.component.Component` ``payments.<slug>``
  for the keys ``PAY_<SLUG>_*`` (:func:`instance_setting_defs`): ``probe`` runs ``test_credentials`` of a
  *candidate* instance through the *candidate* proxy, ``reconfigure`` persists and swaps atomically; when it
  fails, the settings service rolls the keys back (automatic rollback).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets as _secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import aiohttp
import sqlalchemy as sa
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError, ProxyTimeoutError

from svbg.core.component import HealthReport, ProbeError, fix_setting
from svbg.core.log import mask, register_secret
from svbg.core.settings.registry import Apply, SettingDef
from svbg.payments.tables import payment_instances
from svbg.sdk.config import ConfigError, ConfigField, ConfigModel
from svbg.sdk.context import HttpClient, HttpResponse, PluginContext
from svbg.sdk.payments import (
    SDK_VERSION,
    Capabilities,
    Manifest,
    PaymentProvider,
    ProviderError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.crypto import Crypto
    from svbg.db.engine import Database

__all__ = [
    "WEBHOOK_PATH",
    "InstanceHttp",
    "InstanceRegistry",
    "InstanceSpec",
    "LiveInstance",
    "PaymentInstanceComponent",
    "PluginRejectedError",
    "ProviderCatalog",
    "instance_setting_defs",
    "setting_key",
    "spec_from_settings",
    "static_problems",
]

log = logging.getLogger("svbg.payments")

WEBHOOK_PATH: Final = "/webhooks/pay/{instance_id}/{token}"
MIN_TOKEN_LEN: Final = 32
HTTP_TIMEOUT_S: Final = 15.0
HTTP_MAX_BODY: Final = 2 * 1024 * 1024
PROBE_TIMEOUT_S: Final = 20.0
DEGRADED_AFTER_FAILURES: Final = 3
_SLUG_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PROXY_SCHEMES: Final = ("http://", "https://", "socks4://", "socks5://", "socks5h://")
_USER_AGENT: Final = "SvBG-Shop"

# Owner-facing texts (Russian).
_T: Final = {
    "timeout": "касса не ответила за {s:g} с",
    "network": "нет связи с кассой ({kind})",
    "proxy": "прокси не отвечает или отклонил подключение",
    "too_large": "касса вернула слишком большой ответ",
    "probe_failed": "проверка ключей не прошла: {msg}",
    "probe_timeout": "касса не ответила на проверку за {s:g} с",
    "unknown_provider": "неизвестная платёжка «{provider}»",
    "config": "проверьте поля: {fields}",
    "decrypt": "ключи не расшифровываются — вероятно, сменился SECRET_KEY; введите ключи заново",
    "broken": "Платёжка «{slug}» не загружена: {error}",
    "disabled": "Платёжка выключена",
    "ok": "Работает",
    "degraded": "Последние запросы к кассе не прошли: {error}",
    "proxy_format": "ожидался адрес прокси вида socks5://user:pass@host:1080 или http://host:3128",
}


class PluginRejectedError(Exception):
    """A plugin does not meet the SDK contract (static TestKit checks)."""

    def __init__(self, slug: str, problems: Sequence[str]) -> None:
        super().__init__(f"{slug}: " + "; ".join(problems))
        self.slug = slug
        self.problems = list(problems)


def _overrides(cls: type, name: str) -> bool:
    return getattr(cls, name, None) is not getattr(PaymentProvider, name, None)


def static_problems(cls: type) -> list[str]:
    """Contract violations detectable without running the plugin (part of the TestKit, 07 §4.1)."""
    problems: list[str] = []
    if not (isinstance(cls, type) and issubclass(cls, PaymentProvider)):
        return ["not a PaymentProvider subclass"]
    manifest = getattr(cls, "manifest", None)
    caps = getattr(cls, "capabilities", None)
    if not isinstance(manifest, Manifest):
        problems.append("manifest is missing or not a Manifest")
    elif manifest.sdk != SDK_VERSION:
        problems.append(f"built for SDK {manifest.sdk}, core speaks {SDK_VERSION}")
    if not isinstance(caps, Capabilities):
        problems.append("capabilities are missing or not Capabilities")
        return problems
    if caps.webhook and caps.webhook_auth.is_weak and not caps.fetch_status:
        problems.append(f"weak webhook authentication ({caps.webhook_auth}) without fetch_status")
    if caps.webhook and not _overrides(cls, "parse_webhook"):
        problems.append("capabilities.webhook is set but parse_webhook is not implemented")
    if caps.fetch_status and not _overrides(cls, "fetch_status"):
        problems.append("capabilities.fetch_status is set but fetch_status is not implemented")
    if caps.refund and not _overrides(cls, "refund"):
        problems.append("capabilities.refund is set but refund is not implemented")
    if not caps.webhook and not caps.fetch_status and caps.redirect:
        problems.append("a redirect provider needs webhooks or fetch_status to learn about payments")
    return problems


class ProviderCatalog:
    """Plugin classes by slug."""

    def __init__(self, providers: Sequence[type[PaymentProvider]] = ()) -> None:
        self._items: dict[str, type[PaymentProvider]] = {}
        for cls in providers:
            self.register(cls)

    def register(self, cls: type[PaymentProvider]) -> type[PaymentProvider]:
        problems = static_problems(cls)
        slug = getattr(getattr(cls, "manifest", None), "slug", getattr(cls, "__name__", "?"))
        if problems:
            raise PluginRejectedError(str(slug), problems)
        if slug in self._items and self._items[slug] is not cls:
            raise ValueError(f"provider {slug!r} is already registered")
        self._items[slug] = cls
        return cls

    def get(self, slug: str) -> type[PaymentProvider] | None:
        return self._items.get(slug)

    def slugs(self) -> list[str]:
        return list(self._items)

    def __contains__(self, slug: object) -> bool:
        return slug in self._items

    def __len__(self) -> int:
        return len(self._items)


# --------------------------------------------------------------------------------------------- runtime


class ClosableHttp(HttpClient, Protocol):
    requests: int

    async def close(self) -> None: ...


def check_proxy_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not text.lower().startswith(_PROXY_SCHEMES):
        raise ValueError(_T["proxy_format"])
    return text


class InstanceHttp:
    """``ctx.http`` of one instance (see module docstring). Safe to share between concurrent calls."""

    def __init__(
        self,
        proxy_url: str | None = None,
        *,
        timeout: float = HTTP_TIMEOUT_S,
        max_body: int = HTTP_MAX_BODY,
    ) -> None:
        self._proxy = check_proxy_url(proxy_url)
        self._timeout = timeout
        self._max_body = max_body
        self._session: aiohttp.ClientSession | None = None
        self.requests = 0
        self.failures_in_row = 0
        self.last_error: str | None = None
        if self._proxy:
            register_secret(self._proxy)

    def _make_session(self) -> aiohttp.ClientSession:
        connector: aiohttp.BaseConnector
        if self._proxy and self._proxy.lower().startswith(("socks4://", "socks5://", "socks5h://")):
            connector = ProxyConnector.from_url(self._proxy, rdns=self._proxy.lower().startswith("socks5h"))
        else:
            connector = aiohttp.TCPConnector(limit=8)
        return aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            headers={"User-Agent": _USER_AGENT},
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json: Any = None,
        data: bytes | str | Mapping[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request bound of the HttpClient protocol
    ) -> HttpResponse:
        if self._session is None or self._session.closed:
            self._session = self._make_session()
        limit = timeout if timeout is not None else self._timeout
        http_proxy = self._proxy if self._proxy and self._proxy.lower().startswith("http") else None
        self.requests += 1
        try:
            async with (
                asyncio.timeout(limit),
                self._session.request(
                    method.upper(),
                    url,
                    headers=dict(headers or {}),
                    params=dict(params or {}),
                    json=json,
                    data=data,
                    proxy=http_proxy,
                    allow_redirects=False,
                ) as resp,
            ):
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > self._max_body:
                        raise ProviderError(_T["too_large"], retryable=True)  # noqa: TRY301
                result = HttpResponse(resp.status, bytes(body), {k: v for k, v in resp.headers.items()})
        except ProviderError as exc:
            self._failed(exc.human)
            raise
        except TimeoutError:
            self._failed(_T["timeout"].format(s=limit))
            raise ProviderError(_T["timeout"].format(s=limit), retryable=True) from None
        except (ProxyError, ProxyConnectionError, ProxyTimeoutError):
            self._failed(_T["proxy"])
            raise ProviderError(_T["proxy"], retryable=True) from None
        except (aiohttp.ClientError, OSError) as exc:
            human = _T["network"].format(kind=type(exc).__name__)
            self._failed(human)
            raise ProviderError(human, retryable=True) from None
        if result.status >= 500:
            self._failed(f"HTTP {result.status}")
        else:
            self.failures_in_row = 0
        return result

    def _failed(self, human: str) -> None:
        self.failures_in_row += 1
        self.last_error = mask(human)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()


class DbKeyValue:
    """``ctx.kv``: the ``payment_instances.kv`` JSON object of one instance."""

    def __init__(self, db: Database, instance_id: int) -> None:
        self._db = db
        self._id = instance_id

    async def get(self, key: str) -> Any | None:
        async with self._db.read() as conn:
            row = (
                await conn.execute(
                    sa.select(payment_instances.c.kv[key]).where(payment_instances.c.id == self._id)
                )
            ).first()
        return None if row is None else row[0]

    async def set(self, key: str, value: Any) -> None:
        blob = json.dumps({key: value}, ensure_ascii=False)
        async with self._db.tx() as conn:
            await conn.execute(
                sa.text("UPDATE payment_instances SET kv = kv || CAST(:blob AS jsonb) WHERE id = :id"),
                {"blob": blob, "id": self._id},
            )

    async def delete(self, key: str) -> None:
        async with self._db.tx() as conn:
            await conn.execute(
                sa.text("UPDATE payment_instances SET kv = kv - CAST(:key AS text) WHERE id = :id"),
                {"key": key, "id": self._id},
            )


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """Desired configuration of one instance (from settings or the provider wizard)."""

    slug: str
    provider: str
    enabled: bool = False
    is_test: bool = False
    config: Mapping[str, Any] = field(default_factory=dict)
    proxy_url: str | None = None
    title: str | None = None
    method_kinds: tuple[str, ...] | None = None
    sort: int = 100


@dataclass(slots=True)
class LiveInstance:
    """A loaded instance: its row (decrypted) plus the plugin object."""

    id: int
    slug: str
    provider_slug: str
    title: str
    enabled: bool
    is_test: bool
    method_kinds: tuple[str, ...]
    currencies: tuple[str, ...]
    min_minor: int | None
    max_minor: int | None
    sort: int
    webhook_token: str
    proxy_url: str | None
    provider: PaymentProvider
    http: ClosableHttp
    config: ConfigModel

    @property
    def manifest(self) -> Manifest:
        return self.provider.manifest

    @property
    def caps(self) -> Capabilities:
        return self.provider.capabilities

    def accepts(self, currency: str, amount_minor: int | None = None) -> bool:
        if currency.upper() not in self.currencies:
            return False
        if amount_minor is None:
            return True
        # Below the minimum the caller rounds the top-up up (07 §4.5 п.2), so only the maximum excludes.
        return self.max_minor is None or amount_minor <= self.max_minor

    def customer_ref(self, user_id: int) -> str:
        """Opaque, stable per (instance, user); reveals nothing without the instance's webhook token."""
        return hmac.new(self.webhook_token.encode(), f"user:{user_id}".encode(), hashlib.sha256).hexdigest()[
            :24
        ]

    def token_matches(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(self.webhook_token.encode(), token.encode())


HttpFactory = Callable[[str | None], ClosableHttp]


class InstanceRegistry:
    """Live instances in memory (see module docstring). Mutations go through :meth:`save` / :meth:`load`."""

    def __init__(
        self,
        db: Database,
        crypto: Crypto,
        catalog: ProviderCatalog,
        *,
        public_url: Callable[[], str | None] = lambda: None,
        http_factory: HttpFactory | None = None,
    ) -> None:
        self._db = db
        self._crypto = crypto
        self.catalog = catalog
        self._public_url = public_url
        self._http_factory: HttpFactory = http_factory or InstanceHttp
        self._by_id: dict[int, LiveInstance] = {}
        self._by_slug: dict[str, LiveInstance] = {}
        self._broken: dict[str, str] = {}  # slug -> owner-facing reason
        self._lock = asyncio.Lock()

    # ---- reading (no I/O)

    def get(self, instance_id: int) -> LiveInstance | None:
        return self._by_id.get(instance_id)

    def by_slug(self, slug: str) -> LiveInstance | None:
        return self._by_slug.get(slug)

    def all(self) -> list[LiveInstance]:
        return sorted(self._by_id.values(), key=lambda i: (i.sort, i.id))

    def broken(self) -> dict[str, str]:
        return dict(self._broken)

    def available(
        self, currency: str, amount_minor: int | None = None, method_kind: str | None = None
    ) -> list[LiveInstance]:
        """Enabled instances that can take this payment, in display order."""
        return [
            inst
            for inst in self.all()
            if inst.enabled
            and inst.accepts(currency, amount_minor)
            and (method_kind is None or method_kind in inst.method_kinds)
        ]

    def method_kinds(self, currency: str, amount_minor: int | None = None) -> list[str]:
        """Distinct method kinds for the «Пополнить» buttons, in display order (provider names hidden)."""
        seen: list[str] = []
        for inst in self.available(currency, amount_minor):
            for kind in inst.method_kinds:
                if kind not in seen:
                    seen.append(kind)
        return seen

    def webhook_url(self, inst: LiveInstance) -> str | None:
        return self._webhook_url(inst.id, inst.webhook_token, inst.caps)

    def _webhook_url(self, instance_id: int, token: str, caps: Capabilities) -> str | None:
        base = (self._public_url() or "").rstrip("/")
        if not base or not caps.webhook:
            return None
        return base + WEBHOOK_PATH.format(instance_id=instance_id, token=token)

    # ---- building

    def _decrypt(self, value: str | None) -> str | None:
        return None if value is None else self._crypto.decrypt(value)

    def _build(
        self,
        *,
        instance_id: int,
        spec: InstanceSpec,
        webhook_token: str,
        currencies: Sequence[str] | None = None,
        min_minor: int | None = None,
        max_minor: int | None = None,
    ) -> LiveInstance:
        cls = self.catalog.get(spec.provider)
        if cls is None:
            raise ConfigError({"provider": _T["unknown_provider"].format(provider=spec.provider)})
        config = cls.manifest.config.parse(spec.config)
        for value in config.secret_values():
            register_secret(value)
        register_secret(webhook_token)
        http = self._http_factory(check_proxy_url(spec.proxy_url))
        partial = LiveInstance(
            id=instance_id,
            slug=spec.slug,
            provider_slug=cls.manifest.slug,
            title=spec.title or cls.manifest.title,
            enabled=spec.enabled,
            is_test=spec.is_test,
            method_kinds=tuple(spec.method_kinds or (k.value for k in cls.manifest.method_kinds)),
            currencies=tuple(currencies or cls.manifest.currencies),
            min_minor=min_minor if min_minor is not None else cls.manifest.min_minor,
            max_minor=max_minor if max_minor is not None else cls.manifest.max_minor,
            sort=spec.sort,
            webhook_token=webhook_token,
            proxy_url=spec.proxy_url,
            provider=None,  # type: ignore[arg-type] - set right below (the context needs the instance)
            http=http,
            config=config,
        )
        ctx = PluginContext(
            http=http,
            log=logging.getLogger(f"svbg.payments.{spec.slug}"),
            kv=DbKeyValue(self._db, instance_id),
            instance_id=instance_id,
            slug=spec.slug,
            is_test=spec.is_test,
            webhook_url=self._webhook_url(instance_id, webhook_token, cls.capabilities),
        )
        partial.provider = cls(config, ctx)
        return partial

    def candidate(self, spec: InstanceSpec) -> LiveInstance:
        """A throw-away instance for ``probe`` (not registered; the caller closes ``http``)."""
        current = self._by_slug.get(spec.slug)
        token = current.webhook_token if current else _secrets.token_urlsafe(32)
        return self._build(instance_id=current.id if current else 0, spec=spec, webhook_token=token)

    async def load(self) -> None:
        """(Re)load every row from the database. A row that cannot be built is reported in :meth:`broken`."""
        async with self._db.read() as conn:
            rows = (await conn.execute(sa.select(payment_instances))).mappings().all()
        async with self._lock:
            fresh: dict[int, LiveInstance] = {}
            broken: dict[str, str] = {}
            for row in rows:
                try:
                    fresh[row["id"]] = self._from_row(row)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others
                    broken[row["slug"]] = _describe(exc)
                    log.warning("payment instance %s not loaded: %s", row["slug"], _describe(exc))
            old = list(self._by_id.values())
            self._by_id = fresh
            self._by_slug = {i.slug: i for i in fresh.values()}
            self._broken = broken
        for inst in old:
            await inst.http.close()

    def _from_row(self, row: Mapping[str, Any]) -> LiveInstance:
        config = json.loads(self._decrypt(row["config"]) or "{}")
        spec = InstanceSpec(
            slug=row["slug"],
            provider=row["provider"],
            enabled=row["enabled"],
            is_test=row["is_test"],
            config=config,
            proxy_url=self._decrypt(row["proxy_url"]),
            title=row["title"],
            method_kinds=tuple(row["method_kinds"]) or None,
            sort=row["sort"],
        )
        return self._build(
            instance_id=row["id"],
            spec=spec,
            webhook_token=self._decrypt(row["webhook_token"]) or "",
            currencies=tuple(row["currencies"]) or None,
            min_minor=row["min_minor"],
            max_minor=row["max_minor"],
        )

    async def save(self, spec: InstanceSpec) -> LiveInstance:
        """Validate, persist (encrypted) and swap atomically. Raises ``ConfigError`` / database errors and
        leaves the running instance untouched in that case."""
        if not _SLUG_RE.fullmatch(spec.slug):
            raise ConfigError({"slug": "латиница, цифры и _, до 32 символов"})
        async with self._lock:
            current = self._by_slug.get(spec.slug)
            token = current.webhook_token if current else _secrets.token_urlsafe(32)
            probe = self._build(instance_id=current.id if current else 0, spec=spec, webhook_token=token)
            await probe.http.close()
            values = {
                "provider": probe.provider_slug,
                "title": probe.title,
                "enabled": spec.enabled,
                "is_test": spec.is_test,
                "method_kinds": list(probe.method_kinds),
                "currencies": list(probe.currencies),
                "min_minor": probe.min_minor,
                "max_minor": probe.max_minor,
                "sort": spec.sort,
                "config": self._crypto.encrypt(json.dumps(probe.config.as_dict(), ensure_ascii=False)),
                "proxy_url": self._crypto.encrypt(spec.proxy_url) if spec.proxy_url else None,
            }
            async with self._db.tx() as conn:
                instance_id = await self._upsert(conn, spec.slug, values, token)
            inst = self._build(
                instance_id=instance_id,
                spec=spec,
                webhook_token=token,
                currencies=probe.currencies,
                min_minor=probe.min_minor,
                max_minor=probe.max_minor,
            )
            self._by_id[instance_id] = inst
            self._by_slug[spec.slug] = inst
            self._broken.pop(spec.slug, None)
        if current is not None:
            await current.http.close()
        return inst

    async def _upsert(self, conn: AsyncConnection, slug: str, values: Mapping[str, Any], token: str) -> int:
        updated = (
            await conn.execute(
                sa.update(payment_instances)
                .where(payment_instances.c.slug == slug)
                .values(**values, updated_at=sa.func.now())
                .returning(payment_instances.c.id)
            )
        ).scalar()
        if updated is not None:
            return int(updated)
        inserted = await conn.execute(
            sa.insert(payment_instances)
            .values(slug=slug, webhook_token=self._crypto.encrypt(token), **values)
            .returning(payment_instances.c.id)
        )
        return int(inserted.scalar_one())

    async def set_enabled(self, slug: str, enabled: bool) -> bool:
        """Switch an existing instance on/off without touching its keys. False if there is no such row."""
        async with self._lock:
            async with self._db.tx() as conn:
                found = (
                    await conn.execute(
                        sa.update(payment_instances)
                        .where(payment_instances.c.slug == slug)
                        .values(enabled=enabled, updated_at=sa.func.now())
                        .returning(payment_instances.c.id)
                    )
                ).scalar()
            inst = self._by_slug.get(slug)
            if inst is not None:
                inst.enabled = enabled
        return found is not None

    async def close(self) -> None:
        for inst in list(self._by_id.values()):
            await inst.http.close()


def _describe(exc: BaseException, provider: type[PaymentProvider] | None = None) -> str:
    if isinstance(exc, ConfigError):
        titles = {n: f.title for n, f in provider.manifest.config.fields().items()} if provider else {}
        return _T["config"].format(
            fields=", ".join(f"{titles.get(k, k)} — {v}" for k, v in exc.errors.items())
        )
    if type(exc).__name__ == "CryptoError":
        return _T["decrypt"]
    return mask(f"{type(exc).__name__}: {exc}")[:300]


# --------------------------------------------------------------------------------------------- settings


def setting_key(slug: str, suffix: str) -> str:
    """``PAY_<SLUG>_<SUFFIX>``."""
    return f"PAY_{slug.upper()}_{suffix.upper()}"


_FIELD_TYPES: Final[Mapping[str, Any]] = {
    "str": str,
    "secret": "secret",
    "url": "url",
    "int": int,
    "float": float,
    "bool": bool,
    "enum": "enum",
}


def _proxy_validator(value: Any) -> None:
    check_proxy_url(str(value))


def instance_setting_defs(
    slug: str,
    provider: type[PaymentProvider],
    *,
    providers: Sequence[str] | None = None,
    section: str = "payments",
) -> list[SettingDef]:
    """Settings ``PAY_<SLUG>_*`` of one instance (07 §3.2): all RELOAD of component ``payments.<slug>``."""
    title = provider.manifest.title
    common = {"apply": Apply.RELOAD, "component": f"payments.{slug}", "owner_only": True, "tags": (slug,)}
    defs = [
        SettingDef(
            setting_key(slug, "ENABLED"),
            bool,
            False,
            section,
            f"«{title}»: включить",
            f"Показывать способ оплаты через «{title}». Ключи проверяются перед включением.",
            **common,
        ),
        SettingDef(
            setting_key(slug, "PROVIDER"),
            "enum",
            provider.manifest.slug,
            section,
            f"«{title}»: платёжка",
            "Какой провайдер обслуживает этот инстанс (второй инстанс того же провайдера — другой SLUG).",
            choices=tuple(providers or (provider.manifest.slug,)),
            advanced=True,
            **common,
        ),
        SettingDef(
            setting_key(slug, "TEST_MODE"),
            bool,
            False,
            section,
            f"«{title}»: тестовый режим",
            "Тестовая касса: принимает только тестовые платежи; боевые события отклоняются, и наоборот.",
            advanced=True,
            **common,
        ),
        SettingDef(
            setting_key(slug, "PROXY_URL"),
            "secret",
            None,
            section,
            f"«{title}»: исходящий прокси",
            "Если бот стоит за границей, а касса режет зарубежные IP: socks5://user:pass@host:1080 или "
            "http://host:3128. Пусто — без прокси.",
            nullable=True,
            advanced=True,
            validator=_proxy_validator,
            **common,
        ),
    ]
    for fld in provider.manifest.config.fields().values():
        defs.append(_field_def(slug, title, fld, section, common))
    return defs


def _field_def(
    slug: str, title: str, fld: ConfigField, section: str, common: Mapping[str, Any]
) -> SettingDef:
    description = fld.description or fld.title
    if fld.where:
        description = f"{description} Где взять: {fld.where}."
    return SettingDef(
        setting_key(slug, fld.env_suffix),
        _FIELD_TYPES[fld.kind],
        fld.default,
        section,
        f"«{title}»: {fld.title}",
        description,
        nullable=fld.default is None,
        choices=fld.choices,
        min=fld.min,
        max=fld.max,
        advanced=fld.advanced,
        hint=fld.where,
        validator=_pattern_validator(fld.pattern) if fld.pattern else None,
        **common,
    )


def _pattern_validator(pattern: str) -> Callable[[Any], None]:
    """The field's ``pattern`` checked when the key is saved (the bot / ``.env``), not only at apply time."""
    compiled = re.compile(pattern)

    def check(value: Any) -> None:
        if value is None or value == "":
            return  # emptiness is ``nullable`` / ``required``'s business
        if not compiled.fullmatch(str(value)):
            raise ValueError("неверный формат")

    return check


def spec_from_settings(slug: str, cfg: Mapping[str, Any], catalog: ProviderCatalog) -> InstanceSpec:
    """The instance spec described by the ``PAY_<SLUG>_*`` keys of a settings snapshot."""
    provider = cfg.get(setting_key(slug, "PROVIDER")) or slug
    cls = catalog.get(str(provider))
    raw: dict[str, Any] = {}
    if cls is not None:
        for name, fld in cls.manifest.config.fields().items():
            raw[name] = cfg.get(setting_key(slug, fld.env_suffix))
    return InstanceSpec(
        slug=slug,
        provider=str(provider),
        enabled=bool(cfg.get(setting_key(slug, "ENABLED"), False)),
        is_test=bool(cfg.get(setting_key(slug, "TEST_MODE"), False)),
        config=raw,
        proxy_url=cfg.get(setting_key(slug, "PROXY_URL")) or None,
    )


class PaymentInstanceComponent:
    """Settings component ``payments.<slug>`` (see module docstring)."""

    def __init__(
        self, registry: InstanceRegistry, slug: str, *, probe_timeout: float = PROBE_TIMEOUT_S
    ) -> None:
        self._registry = registry
        self.slug = slug
        self.name = f"payments.{slug}"
        self._probe_timeout = probe_timeout

    def _spec(self, cfg: Mapping[str, Any]) -> InstanceSpec:
        return spec_from_settings(self.slug, cfg, self._registry.catalog)

    async def probe(self, candidate: Mapping[str, Any]) -> None:
        spec = self._spec(candidate)
        if not spec.enabled:
            return  # keys may be filled in step by step; they are checked when the instance is switched on
        try:
            inst = self._registry.candidate(spec)
        except ConfigError as exc:
            raise ProbeError(
                _describe(exc, self._registry.catalog.get(spec.provider)),
                fix_action=fix_setting(setting_key(self.slug, "ENABLED")),
            ) from None
        except ValueError as exc:
            raise ProbeError(str(exc)) from None
        try:
            async with asyncio.timeout(self._probe_timeout):
                result = await inst.provider.test_credentials()
        except TimeoutError:
            raise ProbeError(_T["probe_timeout"].format(s=self._probe_timeout)) from None
        except ProviderError as exc:
            raise ProbeError(_T["probe_failed"].format(msg=exc.human)) from None
        finally:
            await inst.http.close()
        if not result.ok:
            raise ProbeError(_T["probe_failed"].format(msg=result.message or "касса отклонила ключи"))

    async def reconfigure(self, cfg: Mapping[str, Any]) -> None:
        spec = self._spec(cfg)
        existing = self._registry.by_slug(self.slug)
        if not spec.enabled:
            if existing is not None and existing.enabled:
                await self._registry.set_enabled(self.slug, False)
            if existing is None or _same_config(existing, spec):
                return
            try:  # keep keys in sync while switched off, but never fail the switch-off because of them
                await self._registry.save(spec)
            except ConfigError:
                return
            return
        await self._registry.save(spec)

    async def health(self) -> HealthReport:
        broken = self._registry.broken().get(self.slug)
        if broken is not None:
            return HealthReport.down(
                _T["broken"].format(slug=self.slug, error=broken),
                fix_action=fix_setting(setting_key(self.slug, "ENABLED")),
            )
        inst = self._registry.by_slug(self.slug)
        if inst is None or not inst.enabled:
            return HealthReport.disabled(_T["disabled"])
        http = inst.http
        failures = getattr(http, "failures_in_row", 0)
        if failures >= DEGRADED_AFTER_FAILURES:
            return HealthReport.degraded(
                _T["degraded"].format(error=getattr(http, "last_error", None) or "—"),
                fix_action=fix_setting(setting_key(self.slug, "PROXY_URL")),
                requests=http.requests,
            )
        return HealthReport.ok(_T["ok"], requests=http.requests, is_test=inst.is_test)


def _same_config(inst: LiveInstance, spec: InstanceSpec) -> bool:
    cls = type(inst.provider)
    try:
        wanted = cls.manifest.config.parse(spec.config)
    except ConfigError:
        return True  # incomplete keys of a disabled instance: nothing to persist
    return (
        wanted == inst.config
        and spec.is_test == inst.is_test
        and (spec.proxy_url or None) == (inst.proxy_url or None)
        and spec.provider == inst.provider_slug
    )
