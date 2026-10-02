"""Provider catalog, instances (encrypted at rest, in-memory lookups), ``ctx.http`` / ``ctx.kv``, the
``PAY_<SLUG>_*`` settings and hot reconfiguration with probe and automatic rollback (07 §4.3, D13)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from svbg.core.component import ComponentRegistry, Health, ProbeError
from svbg.core.crypto import Crypto, generate_key
from svbg.core.settings.registry import Apply, Registry, core_registry
from svbg.core.settings.service import Change, SettingsService
from svbg.payments.registry import (
    DbKeyValue,
    InstanceHttp,
    InstanceRegistry,
    InstanceSpec,
    PaymentInstanceComponent,
    PluginRejectedError,
    ProviderCatalog,
    check_proxy_url,
    instance_setting_defs,
    setting_key,
    spec_from_settings,
    static_problems,
)
from svbg.payments.testkit import CountingHttp
from svbg.sdk import (
    Capabilities,
    ConfigError,
    Manifest,
    MethodKind,
    Probe,
    ProviderError,
    WebhookAuth,
)
from svbg.web.app import WebServer, build_web_app
from tests.dbkit import CountingDatabase
from tests.payments.conftest import (
    API_KEY,
    SECRET,
    STUB_CONFIG,
    BatchPay,
    Env,
    FakeStubServer,
    ManualPay,
    StubConfig,
    StubPay,
    WeakPay,
)

# ------------------------------------------------------------------------------------------ catalog


class WeakNoFetch(StubPay):
    manifest = Manifest(
        slug="weaknofetch",
        title="Weak",
        method_kinds=(MethodKind.CARD,),
        currencies=("RUB",),
        config=StubConfig,
        docs_url="https://x",
    )
    capabilities = Capabilities(webhook_auth=WebhookAuth.IP_ONLY)


class NoParse(ManualPay):
    manifest = Manifest(
        slug="noparse",
        title="No parse",
        method_kinds=(MethodKind.CARD,),
        currencies=("RUB",),
        config=StubConfig,
        docs_url="https://x",
    )
    capabilities = Capabilities(webhook_auth=WebhookAuth.SIGNATURE, fetch_status=True, refund=True)


def test_static_checks() -> None:
    assert static_problems(StubPay) == []
    assert static_problems(ManualPay) == []
    assert static_problems(BatchPay) == []
    weak = static_problems(WeakNoFetch)
    assert any("weak webhook authentication" in p for p in weak)
    noparse = static_problems(NoParse)
    assert any("parse_webhook" in p for p in noparse)
    assert any("fetch_status is not implemented" in p for p in noparse)
    assert any("refund" in p for p in noparse)
    assert static_problems(int) == ["not a PaymentProvider subclass"]


def test_catalog_refuses_bad_plugins_and_duplicates() -> None:
    cat = ProviderCatalog([StubPay])
    with pytest.raises(PluginRejectedError) as err:
        cat.register(WeakNoFetch)
    assert err.value.slug == "weaknofetch" and err.value.problems
    cat.register(StubPay)  # same class again: no-op
    assert cat.slugs() == ["stubpay"] and "stubpay" in cat and len(cat) == 1

    class Other(StubPay):
        pass

    with pytest.raises(ValueError, match="already registered"):
        cat.register(Other)


def test_manifest_and_capabilities_validation() -> None:
    with pytest.raises(ValueError, match="slug"):
        Manifest(
            slug="Bad Slug",
            title="x",
            method_kinds=(MethodKind.CARD,),
            currencies=("RUB",),
            config=StubConfig,
            docs_url="",
        )
    with pytest.raises(ValueError, match="currencies"):
        Manifest(
            slug="ok",
            title="x",
            method_kinds=(MethodKind.CARD,),
            currencies=("rub",),
            config=StubConfig,
            docs_url="",
        )
    with pytest.raises(ValueError, match="min_minor"):
        Manifest(
            slug="ok",
            title="x",
            method_kinds=(MethodKind.CARD,),
            currencies=("RUB",),
            config=StubConfig,
            docs_url="",
            min_minor=500,
            max_minor=100,
        )
    with pytest.raises(ValueError, match="batch_status"):
        Capabilities(webhook_auth=WebhookAuth.SIGNATURE, batch_status=True)
    with pytest.raises(ValueError, match="replay_window_s"):
        Capabilities(webhook_auth=WebhookAuth.SIGNATURE, replay_window_s=0)


# ------------------------------------------------------------------------------------------ instances


async def test_secrets_are_encrypted_at_rest_and_reload(env: Env, crypto: Crypto) -> None:
    await env.registry.save(
        InstanceSpec(
            slug="stubpay",
            provider="stubpay",
            enabled=True,
            config=STUB_CONFIG,
            proxy_url="socks5://user:pw@proxy.example:1080",
        )
    )
    rows = await env.rows("select * from payment_instances where slug = 'stubpay'")
    raw = json.dumps(rows[0], default=str)
    for plain in (API_KEY, SECRET, "user:pw", env.inst().webhook_token):
        assert plain not in raw
    assert rows[0]["config"].startswith("enc:v1:") and rows[0]["webhook_token"].startswith("enc:v1:")
    fresh = InstanceRegistry(env.db, crypto, env.registry.catalog, http_factory=lambda _p: CountingHttp())
    await fresh.load()
    inst = fresh.by_slug("stubpay")
    assert inst is not None and inst.config.api_key == API_KEY
    assert inst.proxy_url == "socks5://user:pw@proxy.example:1080"
    assert inst.webhook_token == env.inst().webhook_token and len(inst.webhook_token) >= 32
    assert "***" in repr(inst.config) and API_KEY not in repr(inst.config)


async def test_token_survives_resave_and_customer_ref_is_opaque(env: Env) -> None:
    before = env.inst()
    await env.registry.save(
        InstanceSpec(slug="stubpay", provider="stubpay", enabled=True, config=STUB_CONFIG)
    )
    after = env.inst()
    assert after.webhook_token == before.webhook_token and after.id == before.id
    ref = after.customer_ref(env.user_id)
    assert ref == after.customer_ref(env.user_id) and ref != after.customer_ref(env.user_id + 1)
    assert len(ref) == 24 and int(ref, 16) >= 0
    assert ref != env.inst("weakpay").customer_ref(env.user_id)  # keyed per instance
    assert (
        after.token_matches(after.webhook_token)
        and not after.token_matches("")
        and not after.token_matches("x")
    )


async def test_undecryptable_instance_is_reported_broken(env: Env) -> None:
    other = InstanceRegistry(env.db, Crypto([generate_key()]), env.registry.catalog)
    await other.load()
    assert other.get(env.inst().id) is None
    broken = other.broken()
    assert "SECRET_KEY" in broken["stubpay"]
    report = await PaymentInstanceComponent(other, "stubpay").health()
    assert report.status is Health.DOWN and "SECRET_KEY" in report.summary


async def test_unknown_provider_and_bad_config_are_rejected(env: Env) -> None:
    with pytest.raises(ConfigError) as err:
        await env.registry.save(InstanceSpec(slug="x1", provider="nope", config={}))
    assert "неизвестная платёжка" in str(err.value)
    with pytest.raises(ConfigError) as err2:
        await env.registry.save(InstanceSpec(slug="x2", provider="stubpay", config={"api_key": "k"}))
    assert err2.value.errors == {"signing_secret": "обязательное поле"}
    with pytest.raises(ConfigError):
        await env.registry.save(InstanceSpec(slug="Bad-Slug", provider="stubpay", config=STUB_CONFIG))
    assert env.registry.by_slug("x2") is None


async def test_available_and_method_kinds(env: Env) -> None:
    kinds = env.registry.method_kinds("RUB")
    assert kinds == ["sbp", "card", "crypto", "manual"]
    assert [i.slug for i in env.registry.available("RUB", method_kind="card")] == ["stubpay", "weakpay"]
    assert [i.slug for i in env.registry.available("USDT")] == ["batchpay"]
    await env.registry.set_enabled("batchpay", False)
    assert env.registry.available("USDT") == []
    assert "crypto" not in env.registry.method_kinds("RUB")
    assert not await env.registry.set_enabled("missing", True)


async def test_webhook_url(env: Env, crypto: Crypto) -> None:
    inst = env.inst()
    assert (
        env.registry.webhook_url(inst) == f"https://shop.example/webhooks/pay/{inst.id}/{inst.webhook_token}"
    )
    assert inst.provider.ctx.webhook_url == env.registry.webhook_url(inst)
    assert env.registry.webhook_url(env.inst("manualpay")) is None
    no_domain = InstanceRegistry(env.db, crypto, env.registry.catalog)
    await no_domain.load()
    assert no_domain.webhook_url(no_domain.by_slug("stubpay")) is None  # type: ignore[arg-type]
    await no_domain.close()


async def test_kv_store(env: Env) -> None:
    kv = env.inst().provider.ctx.kv
    assert isinstance(kv, DbKeyValue)
    assert await kv.get("rate") is None
    await kv.set("rate", {"usd": "95.5"})
    await kv.set("other", 1)
    assert await kv.get("rate") == {"usd": "95.5"}
    await kv.delete("rate")
    assert await kv.get("rate") is None and await kv.get("other") == 1


# ------------------------------------------------------------------------------------------ ctx.http


@pytest.fixture
async def http_server() -> AsyncIterator[str]:
    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"q": request.query.get("a"), "ua": request.headers.get("User-Agent")})

    async def boom(_request: web.Request) -> web.Response:
        return web.Response(status=502)

    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(1.0)
        return web.Response(text="late")

    async def big(_request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    async def redirect(_request: web.Request) -> web.Response:
        raise web.HTTPFound("http://example.invalid/")

    app = build_web_app(
        [
            web.get("/ok", ok),
            web.get("/boom", boom),
            web.get("/slow", slow),
            web.get("/big", big),
            web.get("/redirect", redirect),
        ]
    )
    server = WebServer("127.0.0.1", 0, app)
    await server.start()
    try:
        yield server.url
    finally:
        await server.stop()


async def test_instance_http_success_errors_and_limits(http_server: str) -> None:
    http = InstanceHttp(timeout=0.3, max_body=1000)
    try:
        resp = await http.request("GET", f"{http_server}/ok", params={"a": "1"})
        assert resp.ok and resp.json() == {"q": "1", "ua": "SvBG-Shop"}
        assert (await http.request("GET", f"{http_server}/redirect")).status == 302  # never followed
        for _ in range(3):
            assert (await http.request("GET", f"{http_server}/boom")).status == 502
        assert http.failures_in_row == 3 and http.last_error == "HTTP 502"
        with pytest.raises(ProviderError) as err:
            await http.request("GET", f"{http_server}/slow")
        assert err.value.retryable and "не ответила" in err.value.human
        with pytest.raises(ProviderError, match="слишком большой"):
            await http.request("GET", f"{http_server}/big")
        with pytest.raises(
            ProviderError, match=r"нет связи|не ответила"
        ):  # refused or (Windows) slow refusal
            await http.request("GET", "http://127.0.0.1:1/never")
        assert http.requests == 8
        assert (await http.request("GET", f"{http_server}/ok")).ok and http.failures_in_row == 0
    finally:
        await http.close()


async def test_instance_http_through_an_unreachable_proxy() -> None:
    for proxy in ("socks5://u:p@127.0.0.1:1", "http://127.0.0.1:1"):
        http = InstanceHttp(proxy, timeout=2.0)
        try:
            with pytest.raises(ProviderError) as err:
                await http.request("GET", "http://example.invalid/")
            assert err.value.retryable
            assert "u:p" not in (http.last_error or "")
        finally:
            await http.close()


def test_proxy_url_validation() -> None:
    assert check_proxy_url(None) is None and check_proxy_url("  ") is None
    assert check_proxy_url(" socks5h://h:1 ") == "socks5h://h:1"
    with pytest.raises(ValueError, match="socks5://"):
        check_proxy_url("ftp://h")


# ------------------------------------------------------------------------------------------ settings


def test_setting_defs_register_in_the_settings_registry() -> None:
    reg = Registry()
    defs = instance_setting_defs("rollypay2", StubPay, providers=("stubpay", "batchpay"))
    for d in defs:
        reg.add(d)
    keys = [d.key for d in defs]
    assert keys == [
        "PAY_ROLLYPAY2_ENABLED",
        "PAY_ROLLYPAY2_PROVIDER",
        "PAY_ROLLYPAY2_TEST_MODE",
        "PAY_ROLLYPAY2_PROXY_URL",
        "PAY_ROLLYPAY2_API_KEY",
        "PAY_ROLLYPAY2_SIGNING_SECRET",
        "PAY_ROLLYPAY2_BASE_URL",
    ]
    by = {d.key: d for d in defs}
    assert all(d.apply is Apply.RELOAD and d.component == "payments.rollypay2" for d in defs)
    assert by["PAY_ROLLYPAY2_API_KEY"].is_secret and by["PAY_ROLLYPAY2_PROXY_URL"].is_secret
    assert not by["PAY_ROLLYPAY2_BASE_URL"].is_secret and by["PAY_ROLLYPAY2_BASE_URL"].default.startswith(
        "https"
    )
    assert "Где взять: кабинет Stub → API" in by["PAY_ROLLYPAY2_API_KEY"].description
    assert by["PAY_ROLLYPAY2_PROVIDER"].choices == ("stubpay", "batchpay")
    assert setting_key("x", "enabled") == "PAY_X_ENABLED"


def test_spec_from_settings() -> None:
    cat = ProviderCatalog([StubPay])
    spec = spec_from_settings(
        "stubpay",
        {
            "PAY_STUBPAY_ENABLED": True,
            "PAY_STUBPAY_API_KEY": "k",
            "PAY_STUBPAY_SIGNING_SECRET": "s",
            "PAY_STUBPAY_PROXY_URL": "",
        },
        cat,
    )
    assert spec.enabled and spec.provider == "stubpay" and spec.proxy_url is None
    assert spec.config == {"api_key": "k", "signing_secret": "s", "base_url": None}


def _snap(**values: Any) -> dict[str, Any]:
    return {f"PAY_STUBPAY_{k.upper()}": v for k, v in values.items()}


async def test_component_probe_uses_the_candidate(env: Env) -> None:
    comp = PaymentInstanceComponent(env.registry, "stubpay", probe_timeout=1.0)
    assert comp.name == "payments.stubpay"
    await comp.probe(_snap(enabled=False, api_key="garbage"))  # off: keys may be incomplete
    with pytest.raises(ProbeError) as err:
        await comp.probe(_snap(enabled=True, api_key="k"))
    assert err.value.human == "проверьте поля: Секрет подписи — обязательное поле"
    with pytest.raises(ProbeError, match="отклонила"):
        await comp.probe(_snap(enabled=True, api_key="wrong", signing_secret=SECRET))
    await comp.probe(_snap(enabled=True, api_key=API_KEY, signing_secret=SECRET))
    env.server.raise_network = True
    with pytest.raises(ProbeError, match="нет связи"):
        await comp.probe(_snap(enabled=True, api_key=API_KEY, signing_secret=SECRET))
    env.server.raise_network = False
    env.server.delay = 2.0
    with pytest.raises(ProbeError, match="не ответила"):
        await comp.probe(_snap(enabled=True, api_key=API_KEY, signing_secret=SECRET))


async def test_component_reconfigure_and_health(env: Env) -> None:
    comp = PaymentInstanceComponent(env.registry, "stubpay")
    old = env.inst()
    await comp.reconfigure(_snap(enabled=True, api_key="new-key", signing_secret=SECRET, test_mode=True))
    new = env.inst()
    assert new is not old and new.config.api_key == "new-key" and new.is_test and new.id == old.id
    assert (await comp.health()).status is Health.OK
    await comp.reconfigure(_snap(enabled=False, api_key="new-key", signing_secret=SECRET, test_mode=True))
    assert not env.inst().enabled
    assert (await comp.health()).status is Health.DISABLED
    await comp.reconfigure(_snap(enabled=False, api_key=""))  # incomplete keys while off: kept as they were
    assert env.inst().config.api_key == "new-key"
    fresh = PaymentInstanceComponent(env.registry, "neverconfigured")
    await fresh.reconfigure({})
    assert env.registry.by_slug("neverconfigured") is None
    env.inst().http.failures_in_row = 3  # type: ignore[attr-defined]
    await comp.reconfigure(_snap(enabled=True, api_key="new-key", signing_secret=SECRET))
    env.inst().http.failures_in_row = 3  # type: ignore[attr-defined]
    env.inst().http.last_error = "HTTP 502"  # type: ignore[attr-defined]
    report = await comp.health()
    assert report.status is Health.DEGRADED and "HTTP 502" in report.summary


async def test_settings_apply_probes_persists_and_rolls_back(env: Env, tmp_path: Path) -> None:
    """The real settings pipeline: wrong keys are refused by the probe; a reconfigure failure rolls back."""
    reg = core_registry()
    for d in instance_setting_defs("stubpay", StubPay):
        reg.add(d)
    components = ComponentRegistry()
    comp = PaymentInstanceComponent(env.registry, "stubpay", probe_timeout=2.0)
    components.register(comp)
    settings = SettingsService(
        env.db, reg, Crypto([generate_key()]), components, environ={}, env_path=tmp_path / ".env"
    )
    await settings.load()
    bad = await settings.apply(
        [
            Change("PAY_STUBPAY_ENABLED", "true"),
            Change("PAY_STUBPAY_API_KEY", "wrong"),
            Change("PAY_STUBPAY_SIGNING_SECRET", SECRET),
        ],
        source="bot",
        actor_id=None,
    )
    assert not bad.ok and "отклонила" in next(iter(bad.rejected.values()))
    assert env.inst().config.api_key == API_KEY  # the running instance is untouched
    env.server.keys.add("key-2")
    good = await settings.apply(
        [
            Change("PAY_STUBPAY_ENABLED", "true"),
            Change("PAY_STUBPAY_API_KEY", "key-2"),
            Change("PAY_STUBPAY_SIGNING_SECRET", SECRET),
        ],
        source="bot",
        actor_id=None,
    )
    assert good.ok, good.rejected
    assert good.reloaded == ["payments.stubpay"]
    assert env.inst().config.api_key == "key-2"

    async def broken_save(_spec: InstanceSpec) -> Any:
        raise RuntimeError("database is down")

    env.registry.save = broken_save  # type: ignore[method-assign]
    env.server.keys.add("key-3")
    res = await settings.apply([Change("PAY_STUBPAY_API_KEY", "key-3")], source="bot", actor_id=None)
    assert not res.ok and "Возвращено прежнее значение" in res.rejected["PAY_STUBPAY_API_KEY"]
    assert settings.current()["PAY_STUBPAY_API_KEY"] == "key-2"
    assert env.inst().config.api_key == "key-2"


async def test_batch_provider_defaults(env: Env) -> None:
    inst = env.inst("batchpay")
    assert inst.caps.batch_status and inst.caps.batch_limit == 50
    assert inst.currencies == ("RUB", "USDT") and inst.min_minor is None


async def test_registry_close_closes_sessions(db: CountingDatabase, crypto: Crypto) -> None:
    created: list[CountingHttp] = []

    def factory(_proxy: str | None) -> CountingHttp:
        h = CountingHttp(FakeStubServer())
        created.append(h)
        return h

    reg = InstanceRegistry(db, crypto, ProviderCatalog([StubPay, WeakPay]), http_factory=factory)
    await reg.save(InstanceSpec(slug="stubpay", provider="stubpay", enabled=True, config=STUB_CONFIG))
    await reg.save(InstanceSpec(slug="stubpay", provider="stubpay", enabled=True, config=STUB_CONFIG))
    assert sum(h.closed for h in created) == 3  # two validation builds + the replaced live instance
    await reg.close()
    assert all(h.closed for h in created)


def test_probe_result_type() -> None:
    assert Probe(True).ok and not Probe(False, "нет").ok


def test_field_pattern_is_checked_when_the_key_is_saved() -> None:
    from svbg.payments.providers import BUILTIN_PROVIDERS

    stars = next(cls for cls in BUILTIN_PROVIDERS if cls.manifest.slug == "stars")
    by = {d.key: d for d in instance_setting_defs("stars", stars)}
    check = by["PAY_STARS_RATE"].validator
    assert check is not None
    check("1.5")
    check(None)
    with pytest.raises(ValueError, match="неверный формат"):
        check("abc")
    assert by["PAY_STARS_ENABLED"].validator is None
