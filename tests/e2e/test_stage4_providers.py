"""Stage 4 end to end — every payment plugin in the assembled application.

For each of the 27 built-in plugins: the instance is created by settings (``PAY_<SLUG>_*``, applied hot —
no restart), a payment is opened through the real core (the plugin talks to its fake cash desk), the desk
marks it paid and its webhook — signed like the provider signs it — goes through the real route
``/webhooks/pay/{instance}/{token}`` of the app's web server; the payment ends ``paid`` and the money is on
the user's balance (weak-signature plugins credit after the core re-reads the status from the desk).

Telegram Stars and the manual transfer have no webhook (Telegram / an admin confirms them): their instances
are created the same way and confirmed by their own path (``successful_payment`` / the receipt card).
The desks answer in process (``CountingHttp`` routed by host) — the same fakes as the plugins' own tests.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession

from svbg.core.settings import Change
from svbg.payments import registry as pay_registry
from svbg.payments.providers import BUILTIN_PROVIDERS
from svbg.payments.registry import setting_key
from svbg.payments.testkit import CountingHttp
from svbg.sdk import PaymentState, WebhookRequest
from tests.e2e.conftest import AppEnv, StartApp
from tests.e2e.test_stage2_kit import Shop, open_shop, until_async
from tests.e2e.test_stage3_kit import tg  # noqa: F401 - the ``tg`` fixture

pytestmark = pytest.mark.pg

AMOUNT = 17_900
#: The app is reached directly (no reverse proxy): the owner adds the address to the desk's IP allowlist.
LOCAL = "127.0.0.1"
#: Desks that authenticate by source address behind a trusted proxy (127.0.0.1 is one by default): the
#: proxy passes the desk's address in ``X-Forwarded-For``.
FORWARDED = {"yookassa": "185.71.76.1", "paypear": "158.160.85.101"}


def _t(slug: str) -> Any:
    return importlib.import_module(f"tests.payments.providers.test_{slug}")


def _f(slug: str) -> Any:
    return importlib.import_module(f"tests.fakes.{slug}")


@dataclass
class Opened:
    payment_id: str
    ext: str | None
    pay_url: str | None


Hook = Callable[[Any, Opened], Any]  # (desk, opened) → WebhookRequest | ("form", params)


def vec(vectors: Any) -> Hook:
    def build(_desk: Any, o: Opened) -> WebhookRequest:
        return vectors.webhook(
            PaymentState.PAID,
            payment_id=o.payment_id,
            external_id=o.ext or o.payment_id,
            amount="179",
            currency="RUB",
            signed_at=datetime.now(UTC),
        )

    return build


def _cryptomus(slug: str) -> tuple[Any, dict[str, Any], Hook]:
    fam = importlib.import_module("tests.payments.providers.cryptomus_family")
    fake = _f(slug)
    desk_cls = fake.FakeHeleket if slug == "heleket" else fake.FakeCryptomus
    desk = desk_cls()
    config = {"merchant_id": fam.MERCHANT, "api_key": desk_cls.default_key}

    def hook(d: Any, o: Opened) -> WebhookRequest:
        assert o.ext is not None
        d.set_status(o.ext, "paid")
        return fam.body_for(desk_cls.default_key, uuid=o.ext, order_id=o.payment_id)

    return desk, config, hook


def cases() -> Iterator[tuple[str, Any, dict[str, Any], Hook]]:
    """``(slug, desk, config, hook)`` per webhook plugin; the hook marks the payment paid on the desk and
    returns the provider's webhook (or ``("form", params)`` for the form-posting desks)."""
    yield (
        "rollypay",
        _f("rollypay").FakeRollyPay(),
        _t("rollypay").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "paid"),
            d.webhook(o.ext),
        )[1],
    )
    yield (
        "cryptobot",
        _f("cryptobot").FakeCryptoBot(),
        {**_t("cryptobot").CONFIG, "base_url": _f("cryptobot").FAKE_BASE_URL},
        lambda d, o: (
            d.pay(int(o.ext)),
            d.webhook(int(o.ext)),
        )[1],
    )
    yield (
        "yookassa",
        _f("yookassa").FakeYooKassa(),
        _t("yookassa").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "succeeded"),
            d.webhook(o.ext),
        )[1],
    )
    yield (
        "platega",
        _f("platega").FakePlatega(),
        _t("platega").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "CONFIRMED"),
            d.callback(o.ext),
        )[1],
    )
    yield (
        "freekassa",
        _f("freekassa").FakeFreekassa(),
        {**_t("freekassa").DIRECT, "allowed_ips": LOCAL},
        lambda d, o: (
            setattr(d.open_form(o.pay_url), "status", 1),
            d.notification(o.payment_id),
        )[1],
    )
    yield (
        "yoomoney",
        _f("yoomoney").FakeYooMoney(),
        _t("yoomoney").CONFIG,
        lambda d, o: (
            "form",
            d.pay(d.transfer_for(o.payment_id).request_id),
        ),
    )
    yield (
        "robokassa",
        _f("robokassa").FakeRobokassa(),
        _t("robokassa").CONFIG,
        lambda d, o: (
            d.open_link(o.pay_url),
            ("form", d.pay(o.ext)),
        )[1],
    )
    for slug in ("cryptomus", "heleket"):
        desk, config, hook = _cryptomus(slug)
        yield slug, desk, config, hook
    yield "wata", _f("wata").FakeWata(), _t("wata").CONFIG, lambda d, o: d.webhook(d.pay(o.payment_id))
    mulen = _t("mulenpay")
    yield (
        "mulenpay",
        _f("mulenpay").FakeMulenPay(),
        mulen.CONFIG,
        lambda d, o: (
            d.set_status(int(o.ext), "paid"),
            vec(mulen.MulenVectors())(d, o),
        )[1],
    )
    yield "pal24", _f("pal24").FakePal24(), _t("pal24").CONFIG, lambda d, o: d.payment_postback(d.pay(o.ext))
    yield (
        "lava",
        _f("lava").FakeLava(),
        _t("lava").CONFIG,
        lambda d, o: (
            setattr(d.invoices[o.ext], "status", "success"),
            d.webhook(d.invoices[o.ext]),
        )[1],
    )
    trib = _t("tribute")
    yield "tribute", _f("tribute").FakeTribute(), trib.CONFIG, vec(trib.TributeVectors())
    cloud = _t("cloudpayments")
    yield (
        "cloudpayments",
        _f("cloudpayments").FakeCloudPayments(),
        cloud.CONFIG,
        vec(cloud.CloudPaymentsVectors()),
    )
    yield (
        "riopay",
        _f("riopay").FakeRioPay(),
        _t("riopay").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "COMPLETED"),
            d.webhook(o.ext),
        )[1],
    )
    yield (
        "severpay",
        _f("severpay").FakeSeverPay(),
        _t("severpay").CONFIG,
        lambda d, o: (
            d.set_status(int(o.ext), "success"),
            d.webhook(int(o.ext)),
        )[1],
    )
    yield (
        "paypear",
        _f("paypear").FakePayPear(),
        _t("paypear").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "CONFIRMED"),
            d.webhook(o.ext),
        )[1],
    )
    yield (
        "overpay",
        _f("overpay").FakeOverpay(),
        _t("overpay").CONFIG,
        lambda d, o: (
            d.pay_card(o.ext),
            d.signed(d.transaction_body(o.ext)),
        )[1],
    )
    yield (
        "aurapay",
        _f("aurapay").FakeAuraPay(),
        _t("aurapay").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "PAID"),
            d.webhook(o.ext),
        )[1],
    )
    eto = _t("etoplatezhi")
    yield (
        "etoplatezhi",
        _f("etoplatezhi").FakeEtoplatezhi(),
        eto.CONFIG,
        lambda d, o: (
            d.open_form(o.pay_url, status="success"),
            vec(eto.EtoplatezhiVectors())(d, o),
        )[1],
    )
    yield (
        "antilopay",
        _f("antilopay").FakeAntilopay(callback_key="project"),
        {**_t("antilopay").DIRECT, "allowed_ips": LOCAL},
        lambda d, o: d.callback(d.pay(o.payment_id)),
    )
    yield (
        "cispay",
        _f("cispay").FakeCisPay(),
        _t("cispay").CONFIG,
        lambda d, o: d.webhook(d.pay(o.payment_id)),
    )
    yield (
        "tabpay",
        _f("tabpay").FakeTabPay(),
        _t("tabpay").CONFIG,
        lambda d, o: (
            d.set_status(o.ext, "SUCCESS"),
            d.webhook(o.ext),
        )[1],
    )
    yield (
        "paritypay",
        _f("paritypay").FakeParityPay(),
        _t("paritypay").DIRECT,
        lambda d, o: (
            d.set_status(o.ext, "PAID"),
            d.notification(o.ext),
        )[1],
    )


class RoutingHttp(CountingHttp):
    """``ctx.http`` of every instance: requests go to the fake desk that owns the URL's host."""

    def __init__(self, real: Any) -> None:
        super().__init__()
        self.desks: dict[str, CountingHttp] = {}
        self.real = real  # hosts that are not fakes in process (the stage-2 desks of ``open_shop``)

    def add(self, desk: Any, config: dict[str, Any], provider: Any) -> None:
        urls = [v for v in config.values() if isinstance(v, str) and v.startswith("http")]
        for name in ("DEFAULT_BASE_URL", "API_URL", "BASE_URL"):
            mod = importlib.import_module(provider.__module__)
            if isinstance(getattr(mod, name, None), str):
                urls.append(getattr(mod, name))
        for attr in ("base_url", "api_url"):
            try:
                value = getattr(desk, attr, None)
            except AssertionError:  # a server-only property of a desk that is not started
                continue
            if isinstance(value, str):
                urls.append(value)
        for url in urls:
            self.desks.setdefault(urlsplit(url).netloc, CountingHttp(desk))

    async def request(self, method: str, url: str, **kw: Any) -> Any:
        host = urlsplit(url).netloc
        desk = self.desks.get(host)
        if desk is None:
            return await self.real.request(method, url, **kw)
        return await desk.request(method, url, **kw)

    async def close(self) -> None:
        return None


def setting_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def post(url: str, req: WebhookRequest, forwarded: str | None = None) -> int:
    headers = {k: v for k, v in req.headers.items()} if hasattr(req.headers, "items") else dict(req.headers)
    headers.pop("Host", None)
    if forwarded is not None:
        headers["X-Forwarded-For"] = forwarded
    async with (
        ClientSession() as session,
        session.post(url, data=req.body, headers=headers, params=dict(req.query)) as resp,
    ):
        await resp.read()
        return resp.status


async def balance_of(shop: Shop, uid: int) -> int:
    return int((await shop.rows("select wallet_minor from users where id = $1", uid))[0]["wallet_minor"])


async def test_every_plugin_hot_instance_and_webhook_to_the_balance(
    start_app: StartApp, app_env: AppEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pay_registry.InstanceHttp()
    routing = RoutingHttp(real)
    monkeypatch.setattr(pay_registry, "InstanceHttp", lambda *_a, **_k: routing)
    table = list(cases())
    by_slug = {cls.manifest.slug: cls for cls in BUILTIN_PROVIDERS}
    assert {s for s, *_ in table} | {"stars", "manual"} == set(by_slug), "a plugin is not covered"
    for slug, desk, config, _hook in table:
        routing.add(desk, config, by_slug[slug])

    async with open_shop(start_app, app_env) as shop:
        app = shop.app
        assert app.settings is not None and app.payments is not None and app.web is not None
        # PUBLIC_URL: plugins that need their webhook address (ЮMoney) — hot as well
        await app.settings.apply([Change("PUBLIC_URL", app.web.url)], source="bot", actor_id=None)
        person = shop.person(5_901)
        await person.start()
        uid = await shop.user_id(5_901)
        failures: dict[str, str] = {}
        for slug, desk, config, hook in table:
            cls = by_slug[slug]
            fields = cls.manifest.config.fields()
            changes = [Change(setting_key(slug, "ENABLED"), "true")]
            changes += [
                Change(setting_key(slug, fields[k].env_suffix), setting_text(v)) for k, v in config.items()
            ]
            try:
                await app.settings.apply(changes, source="bot", actor_id=None)
                inst = app.pay_instances.by_slug(slug)  # type: ignore[union-attr]
                assert inst is not None and inst.enabled, "the instance is not enabled"
                before = await balance_of(shop, uid)
                result = await app.payments.create_payment(
                    user_id=uid,
                    instance_id=inst.id,
                    amount_minor=AMOUNT,
                    currency="RUB",
                    description="Пополнение",
                )
                opened = Opened(result.payment_id, result.checkout.external_id, result.checkout.pay_url)
                url = f"{app.web.url}/webhooks/pay/{inst.id}/{inst.webhook_token}"
                made = hook(desk, opened)
                if isinstance(made, tuple):
                    status = (await desk.send(url, made[1]))[0]
                else:
                    status = await post(url, made, FORWARDED.get(slug))
                assert status == 200, f"webhook answered {status}"

                async def credited() -> bool:
                    row = await shop.rows("select status from payments where id = $1", opened.payment_id)
                    return row[0]["status"] == "paid" and await balance_of(shop, uid) == before + AMOUNT

                await until_async(credited, timeout=20, what=f"{slug}: paid and credited")
            except Exception as exc:  # collect every plugin's result, then fail once
                failures[slug] = f"{type(exc).__name__}: {exc}"[:300]
                print("PROVIDER-FAIL", slug, " ".join(failures[slug].split()))
        assert not failures, " || ".join(f"{k}: {v}" for k, v in failures.items())
        assert len(table) == 25
        for slug in ("stars", "manual"):  # no webhook: confirmed by Telegram / an admin (stage-2 e2e)
            inst = app.pay_instances.by_slug(slug)  # type: ignore[union-attr]
            assert inst is not None and inst.enabled
        await shop.assert_wallet_invariants()
    await real.close()
