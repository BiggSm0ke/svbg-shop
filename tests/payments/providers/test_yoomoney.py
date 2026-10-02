"""ЮMoney quickpay plugin: the shared TestKit plus protocol vectors from ``docs/providers/yoomoney.md``."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import pytest

from svbg.payments.providers.yoomoney import (
    DEFAULT_BASE_URL,
    YooMoney,
    legacy_sha1,
    sign,
    signing_string,
)
from svbg.payments.registry import InstanceHttp
from svbg.payments.testkit import (
    CountingHttp,
    KitFailure,
    check_core,
    check_plugin,
    check_poll_budget,
    check_static,
    make_context,
    make_provider,
)
from svbg.sdk import (
    ConfigError,
    HttpResponse,
    MethodKind,
    PaymentIntent,
    PaymentState,
    ProviderError,
    WebhookAuth,
    WebhookIgnored,
    WebhookRejected,
    WebhookRequest,
)
from tests.dbkit import CountingDatabase
from tests.fakes.yoomoney import (
    FAKE_BASE_URL,
    NOTIFICATION_SECRET,
    WALLET,
    FakeYooMoney,
    as_request,
    notification,
)
from tests.fakes.yoomoney import sign as fake_sign
from tests.payments.providers.conftest import HarnessFactory, outcomes, payment_row

CONFIG = {"wallet": WALLET, "notification_secret": NOTIFICATION_SECRET, "base_url": FAKE_BASE_URL}
PID = "0190f5d2-7b1e-7c3a-9d4e-5f6a7b8c9d0e"
#: Vectors from the official page (legacy sha1 example) reused for the HMAC string.
DOC_SECRET = "01234567890ABCDEF01234567890"
DOC_PARAMS = {
    "notification_type": "p2p-incoming",
    "operation_id": "1234567",
    "amount": "300.00",
    "withdraw_amount": "301.50",
    "currency": "643",
    "datetime": "2011-07-01T09:00:00.000+04:00",
    "sender": "41001XXXXXXXX",
    "codepro": "false",
    "label": "YM.label.12345",
    "unaccepted": "false",
}


class YooVectors:
    """:class:`~svbg.payments.testkit.WebhookVectors` of ЮMoney (independent signing code).

    ЮMoney notifies only about incoming transfers: there is no «expired» or «chargeback» notification. For
    those states the vector sends a signed transfer on hold (``unaccepted=true`` → ``processing``), which the
    core ignores."""

    def webhook(
        self,
        state: PaymentState,
        *,
        payment_id: str,
        external_id: str,
        amount: str,
        currency: str,
        signed_at: datetime,
        test: bool = False,
    ) -> WebhookRequest:
        assert currency == "RUB"
        params = notification(
            label=payment_id,
            withdraw_amount=amount,
            amount=amount,
            operation_id=external_id,
            at=signed_at,
            test=test,
            unaccepted=state is not PaymentState.PAID,
        )
        return as_request(params)

    def tamper(self, req: WebhookRequest) -> WebhookRequest:
        return WebhookRequest(
            body=req.body.replace(b"withdraw_amount=", b"withdraw_amount=1"), headers=req.headers
        )

    def strip_auth(self, req: WebhookRequest) -> WebhookRequest:
        body = "&".join(p for p in req.body.decode().split("&") if not p.startswith("sign="))
        return WebhookRequest(body=body.encode(), headers=req.headers)


def provider(http: CountingHttp | None = None, *, is_test: bool = False, **config: Any) -> YooMoney:
    p = make_provider(YooMoney, {**CONFIG, **config}, http=http, is_test=is_test)
    assert isinstance(p, YooMoney)
    return p


def intent(**kw: Any) -> PaymentIntent:
    values: dict[str, Any] = {
        "payment_id": PID,
        "amount_minor": 17_900,
        "currency": "RUB",
        "description": "Пополнение баланса на 179 ₽",
        "customer_ref": "c0ffee",
    }
    values.update(kw)
    return PaymentIntent(**values)


def note(**kw: Any) -> WebhookRequest:
    kw.setdefault("label", PID)
    kw.setdefault("withdraw_amount", "179.00")
    return as_request(notification(**kw))


# ------------------------------------------------------------------------------------------- TestKit


def test_static_contract() -> None:
    check_static(YooMoney)
    caps = YooMoney.capabilities
    assert caps.webhook_auth is WebhookAuth.SIGNATURE and caps.replay_window_s is None
    assert not caps.fetch_status and not caps.batch_status and not caps.refund and caps.redirect
    assert YooMoney.manifest.method_kinds == (MethodKind.CARD, MethodKind.WALLET)
    assert YooMoney.manifest.currencies == ("RUB",)
    assert all(f.where for f in YooMoney.manifest.config.fields().values() if not f.advanced)


async def test_testkit_plugin_level() -> None:
    await check_plugin(YooMoney, CONFIG, YooVectors())


async def test_testkit_core_level(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    """Everything passes except the two vectors ЮMoney cannot express (no expiry / chargeback
    notifications — documented in docs/providers/yoomoney.md)."""
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(FakeYooMoney()))
    with pytest.raises(KitFailure) as err:
        await check_core(harness, YooVectors())
    assert err.value.failures == [
        "expired webhook did not expire the payment",
        "chargeback did not mark the payment refunded",
    ]
    assert len(harness.credited) == 3 and harness.refunded == []
    assert {"bad_signature", "applied", "mismatch", "test_rejected"} <= set(await outcomes(db))


@pytest.mark.parametrize("domain", [True, False], ids=["domain", "no-domain"])
async def test_testkit_poll_budget_is_zero(make_harness: HarnessFactory, domain: bool) -> None:
    """No status API without OAuth: the reconciler never asks ЮMoney anything (D14 budget trivially met)."""
    desk = FakeYooMoney()
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(desk), has_domain=domain)
    assert await check_poll_budget(harness, domain=domain) == 0
    assert desk.requests == [] and harness.http.requests == 0


# ---------------------------------------------------------------------------------- signature vectors


def test_signing_string_and_hmac_known_answer() -> None:
    """HMAC computed with OpenSSL: ``openssl dgst -sha256 -hmac <secret>`` over the string below."""
    expected_string = (
        "amount=300.00&codepro=false&currency=643&datetime=2011-07-01T09%3A00%3A00.000%2B04%3A00"
        "&label=YM.label.12345&notification_type=p2p-incoming&operation_id=1234567&sender=41001XXXXXXXX"
        "&unaccepted=false&withdraw_amount=301.50"
    )
    expected = "1e5dbeaf47938c42ceed1e4d07569cb5a718392eb0d33ddd07b5c18f36974360"
    assert signing_string(DOC_PARAMS) == expected_string
    assert signing_string({**DOC_PARAMS, "sign": "x"}) == expected_string
    assert sign(DOC_SECRET, DOC_PARAMS) == expected == fake_sign(DOC_SECRET, DOC_PARAMS)


def test_legacy_sha1_matches_the_official_example() -> None:
    """The example of the official notifications page (SHA-1 of ``…&secret&label``)."""
    assert legacy_sha1(DOC_SECRET, DOC_PARAMS) == "a2ee4a9195f4a90e893cff4f62eeba0b662321f9"


async def test_values_with_spaces_plus_cyrillic_and_empty_fields() -> None:
    """Form encoding (``+`` for spaces) must not change what is signed: values are re-encoded per RFC 3986."""
    params = notification(
        label=PID, withdraw_amount="179.00", extra={"comment": "оплата VPN + 1 мес", "sender": ""}
    )
    assert "comment=%D0%BE" in urlencode(list(params.items()))
    event = await provider().parse_webhook(as_request(params))
    assert event.state is PaymentState.PAID and event.payment_id == PID


async def test_every_parameter_is_covered_by_the_signature() -> None:
    params = notification(label=PID, withdraw_amount="179.00")
    plugin = provider()
    for key in params:
        if key == "sign":
            continue
        forged = {**params, key: params[key] + "0"}
        with pytest.raises(WebhookRejected) as err:
            await plugin.parse_webhook(as_request(forged))
        assert err.value.status == 401, key
    with pytest.raises(WebhookRejected):  # an added parameter is signed too
        await plugin.parse_webhook(as_request({**params, "extra": "1"}))


async def test_upper_case_sign_is_accepted_wrong_secret_is_not() -> None:
    params = notification(label=PID, withdraw_amount="179.00")
    event = await provider().parse_webhook(as_request({**params, "sign": params["sign"].upper()}))
    assert event.state is PaymentState.PAID
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(note(secret="someone-else"))
    assert err.value.status == 401


async def test_legacy_sha1_only_when_enabled() -> None:
    legacy = notification(label=PID, withdraw_amount="179.00", with_sign=False, with_sha1=True)
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(as_request(legacy))
    assert err.value.status == 401
    event = await provider(allow_legacy_sha1=True).parse_webhook(as_request(legacy))
    assert event.state is PaymentState.PAID
    # with both present, ``sign`` decides: a broken sign is not rescued by a valid sha1_hash
    both = notification(label=PID, withdraw_amount="179.00", with_sha1=True)
    with pytest.raises(WebhookRejected):
        await provider(allow_legacy_sha1=True).parse_webhook(as_request({**both, "sign": "0" * 64}))


async def test_unparseable_bodies_are_400() -> None:
    params = notification(label=PID, withdraw_amount="179.00")
    dup = urlencode([*params.items(), ("label", PID)]).encode()
    for body in (dup, b"\xff\xfe=1", b"a=1&&&=&"):
        with pytest.raises(WebhookRejected) as err:
            await provider().parse_webhook(WebhookRequest(body=body))
        assert err.value.status in (400, 401)
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=dup))
    assert err.value.status == 400
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(WebhookRequest(body=b""))
    assert err.value.status == 401


# ------------------------------------------------------------------------------------- body parsing


@pytest.mark.parametrize("withdraw", ["179", "179.00", "179.0", "179.000"])
async def test_amount_forms_are_the_same_decimal(withdraw: str) -> None:
    event = await provider().parse_webhook(note(withdraw_amount=withdraw, amount="173.63"))
    assert event.amount == Decimal("179") and event.currency == "RUB"
    assert event.summary["amount"] == "173.63" and event.summary["withdraw_amount"] == withdraw


async def test_event_amount_is_withdraw_amount_not_the_credited_amount() -> None:
    event = await provider().parse_webhook(note(withdraw_amount="179.00", amount="173.63"))
    assert event.amount == Decimal("179.00")
    assert event.external_id == "1234567890" and event.payment_id == PID and not event.is_test
    assert event.paid_at is not None and event.paid_at.tzinfo is not None


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"amount": "180.00"}, 400),  # credited more than charged
        ({"withdraw_amount": "abc"}, 400),
        ({"withdraw_amount": "-5"}, 400),
        ({"amount": "x"}, 400),
        ({"currency": "840"}, 400),
        ({"operation_id": ""}, 400),
        ({"operation_id": "9" * 201}, 400),
    ],
    ids=[
        "amount>withdraw",
        "withdraw-text",
        "withdraw-negative",
        "amount-text",
        "usd",
        "no-op",
        "op-too-long",
    ],
)
async def test_malformed_authentic_bodies_are_400(overrides: dict[str, str], status: int) -> None:
    params = notification(label=PID, withdraw_amount="179.00", with_sign=False)
    params.update(overrides)
    params["sign"] = fake_sign(NOTIFICATION_SECRET, params)
    with pytest.raises(WebhookRejected) as err:
        await provider().parse_webhook(as_request(params))
    assert err.value.status == status


async def test_missing_withdraw_amount_gives_no_amount() -> None:
    params = notification(label=PID, withdraw_amount="179.00", with_sign=False)
    del params["withdraw_amount"]
    params["sign"] = fake_sign(NOTIFICATION_SECRET, params)
    event = await provider().parse_webhook(as_request(params))
    assert event.amount is None  # the core turns this into «mismatch» (no status API to ask)


async def test_labels() -> None:
    plugin = provider()
    upper = await plugin.parse_webhook(note(label=PID.upper()))
    assert upper.payment_id == PID
    for label in ("", "order-42", "tc_abc"):
        with pytest.raises(WebhookIgnored) as err:
            await plugin.parse_webhook(note(label=label))
        assert err.value.response.status == 200


async def test_settings_page_test_button_is_acknowledged() -> None:
    """«Протестировать» on the ЮMoney settings page: signed, no label → 200 without any change."""
    params = notification(label="", withdraw_amount="100.00", operation_id="test-notification", test=True)
    with pytest.raises(WebhookIgnored) as err:
        await provider().parse_webhook(as_request(params))
    assert err.value.reason == "test notification" and err.value.response.status == 200
    with pytest.raises(WebhookRejected):  # but a wrong secret shows up as 401 during the owner's test
        await provider().parse_webhook(as_request({**params, "sign": "0" * 64}))


async def test_test_flag_with_label() -> None:
    event = await provider().parse_webhook(note(test=True))
    assert event.is_test
    event = await provider().parse_webhook(note(operation_id="test-notification"))
    assert event.is_test


@pytest.mark.parametrize("field", ["unaccepted", "codepro"])
async def test_held_transfers_are_processing(field: str) -> None:
    params = notification(label=PID, withdraw_amount="179.00", with_sign=False)
    params[field] = "true"
    params["sign"] = fake_sign(NOTIFICATION_SECRET, params)
    event = await provider().parse_webhook(as_request(params))
    assert event.state is PaymentState.PROCESSING and event.paid_at is None


async def test_other_notification_types_are_ignored() -> None:
    with pytest.raises(WebhookIgnored):
        await provider().parse_webhook(note(notification_type="outgoing-transfer"))


async def test_summary_has_no_sender_and_ack_is_ok() -> None:
    event = await provider().parse_webhook(note(notification_type="p2p-incoming"))
    assert "sender" not in event.summary and "sign" not in event.summary
    resp = provider().ack(event)
    assert resp.status == 200 and resp.body == b"OK"


# ------------------------------------------------------------------------------------------- create


async def test_create_posts_the_form_and_returns_the_redirect() -> None:
    desk = FakeYooMoney()
    http = CountingHttp(desk)
    checkout = await provider(http).create(intent(return_url="https://t.me/svbg_bot"))
    assert checkout.kind == "url" and checkout.external_id is None
    assert checkout.pay_url is not None and checkout.pay_url.startswith(
        "https://yoomoney.fake/transfer/quickpay?"
    )
    call = http.calls[0]
    assert call.method == "POST" and call.url == f"{FAKE_BASE_URL}/quickpay/confirm" and call.json is None
    assert call.data == {
        "receiver": WALLET,
        "quickpay-form": "button",
        "paymentType": "AC",
        "sum": "179.00",
        "label": PID,
        "successURL": "https://t.me/svbg_bot",
    }
    assert "c0ffee" not in json.dumps(desk.forms) and "Пополнение" not in json.dumps(desk.forms)


@pytest.mark.parametrize(
    ("hint", "config_type", "expected"),
    [(None, "AC", "AC"), (None, "PC", "PC"), (MethodKind.WALLET, "AC", "PC"), (MethodKind.CARD, "PC", "AC")],
)
async def test_payment_type(hint: MethodKind | None, config_type: str, expected: str) -> None:
    desk = FakeYooMoney()
    await provider(CountingHttp(desk), payment_type=config_type).create(intent(method_hint=hint))
    assert desk.forms[0]["paymentType"] == expected and "successURL" not in desk.forms[0]


@pytest.mark.parametrize(
    ("status", "retryable", "words"),
    [
        (500, True, "недоступен"),
        (429, True, "недоступен"),
        (400, False, "HTTP 400"),
        (405, False, "HTTP 405"),
    ],
)
async def test_create_errors(status: int, retryable: bool, words: str) -> None:
    desk = FakeYooMoney()
    desk.fail_with = status
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(desk)).create(intent())
    assert err.value.retryable is retryable and words in err.value.human


async def test_unknown_wallet_page_is_not_a_link() -> None:
    with pytest.raises(ProviderError) as err:
        await provider(CountingHttp(FakeYooMoney(wallet="4100100000000000"))).create(intent())
    assert "кошел" in err.value.human and not err.value.retryable


async def test_relative_and_error_locations() -> None:
    def answer(location: str) -> CountingHttp:
        return CountingHttp(lambda _c: HttpResponse(302, b"", {"location": location}))

    checkout = await provider(answer("/transfer/quickpay?requestId=abc")).create(intent())
    assert checkout.pay_url == f"{FAKE_BASE_URL}/transfer/quickpay?requestId=abc"
    with pytest.raises(ProviderError):
        await provider(answer("/quickpay/error?reason=receiver")).create(intent())
    with pytest.raises(ProviderError):
        await provider(answer("javascript:alert(1)")).create(intent())


async def test_non_rub_is_refused_without_a_request() -> None:
    http = CountingHttp(FakeYooMoney())
    with pytest.raises(ProviderError):
        await provider(http).create(intent(currency="USD"))
    assert http.requests == 0


# -------------------------------------------------------------------------------------- config / probe


async def test_test_credentials_is_offline() -> None:
    http = CountingHttp(FakeYooMoney())
    probe = await provider(http).test_credentials()
    assert probe.ok and "Протестировать" in probe.message and http.requests == 0
    plugin = YooMoney(YooMoney.manifest.config.parse(CONFIG), make_context("yoomoney", webhook_url=None))
    probe = await plugin.test_credentials()
    assert not probe.ok and "публичного адреса" in probe.message


def test_config_validation_and_secrets() -> None:
    plugin = provider(base_url=None)
    assert plugin.config.base_url == DEFAULT_BASE_URL and plugin.config.payment_type == "AC"
    assert NOTIFICATION_SECRET not in repr(plugin.config)
    assert plugin.config.secret_values() == [NOTIFICATION_SECRET]
    assert plugin.config.allow_legacy_sha1 is False
    for wallet in ("12345", "41001abc", "5100118888888888"):
        with pytest.raises(ConfigError) as err:
            YooMoney.manifest.config.parse({**CONFIG, "wallet": wallet})
        assert "wallet" in err.value.errors


# ------------------------------------------------------------------------------ through the real core


async def test_fee_retries_and_late_notification(make_harness: HarnessFactory, db: CountingDatabase) -> None:
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(FakeYooMoney()))
    pid = await harness.pending()
    params = notification(label=pid, withdraw_amount="179.00", amount="173.63", operation_id="op-1")
    # ЮMoney retries the same notification (+10 min, +60 min): one credit
    codes = await asyncio.gather(*(harness.send(as_request(params)) for _ in range(3)))
    assert set(codes) == {200} and len(harness.credited) == 1
    row = await payment_row(db, pid)
    assert row["status"] == "paid" and row["external_id"] == "op-1" and row["paid_amount_minor"] == 17_900
    # a second transfer with the same label (the user paid twice) is not credited again
    again = notification(label=pid, withdraw_amount="179.00", operation_id="op-2")
    assert await harness.send(as_request(again)) == 200
    assert len(harness.credited) == 1


async def test_wrong_or_missing_amount_is_mismatch(make_harness: HarnessFactory) -> None:
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(FakeYooMoney()))
    short = await harness.pending()
    assert await harness.send(note(label=short, withdraw_amount="178.99", operation_id="op-s")) == 200
    assert await harness.status(short) == "mismatch"
    missing = await harness.pending()
    params = notification(label=missing, withdraw_amount="179.00", operation_id="op-m", with_sign=False)
    del params["withdraw_amount"]
    params["sign"] = fake_sign(NOTIFICATION_SECRET, params)
    assert await harness.send(as_request(params)) == 200
    assert await harness.status(missing) == "mismatch"
    assert harness.credited == []


async def test_payment_after_a_failed_checkout_still_counts(make_harness: HarnessFactory) -> None:
    desk = FakeYooMoney()
    desk.fail_with = 503
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(desk))
    from svbg.payments.core import CheckoutError

    with pytest.raises(CheckoutError):
        await harness.core.create_payment(
            user_id=harness.user_id,
            instance_id=harness.instance.id,
            amount_minor=17_900,
            currency="RUB",
            description="x",
        )
    async with harness.db.read() as conn:
        import sqlalchemy as sa

        pid = (await conn.execute(sa.text("select id from payments"))).scalar_one()
    assert await harness.status(pid) == "failed"
    assert await harness.send(note(label=pid, operation_id="op-late")) == 200
    assert await harness.status(pid) == "paid"


async def test_test_instance_accepts_test_notifications(make_harness: HarnessFactory) -> None:
    harness = await make_harness(YooMoney, CONFIG, http=CountingHttp(FakeYooMoney()), is_test=True)
    pid = await harness.pending()
    assert await harness.send(note(label=pid, test=True, operation_id="op-t")) == 200
    assert await harness.status(pid) == "paid"


async def test_end_to_end_over_real_http_and_route(
    make_harness: HarnessFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Real aiohttp client (InstanceHttp, no redirect following) against the fake ЮMoney server, then the
    notification POSTed as a form to ``/webhooks/pay/{id}/{token}`` over a socket."""
    from aiohttp import web

    from svbg.web.routes.payments import payment_routes

    async with FakeYooMoney() as ym:
        http = InstanceHttp()
        try:
            harness = await make_harness(YooMoney, {**CONFIG, "base_url": ym.base_url}, http=http)
            result = await harness.core.create_payment(
                user_id=harness.user_id,
                instance_id=harness.instance.id,
                amount_minor=25_000,
                currency="RUB",
                description="Пополнение баланса",
            )
            assert result.checkout.pay_url is not None and "requestId=" in result.checkout.pay_url
            transfer = ym.transfer_for(result.payment_id)
            assert transfer.form["sum"] == "250.00"
            app = web.Application()
            app.add_routes(payment_routes(harness.core))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
                inst = harness.instance
                url = f"http://127.0.0.1:{port}/webhooks/pay/{inst.id}/{inst.webhook_token}"
                with caplog.at_level(logging.DEBUG):
                    params = ym.pay(transfer.request_id)
                    assert await ym.send(url, params) == (200, "OK")
                    assert await ym.send(url, {**params, "sign": "0" * 64}) == (401, "rejected")
                assert await harness.status(result.payment_id) == "paid"
                assert NOTIFICATION_SECRET not in caplog.text
            finally:
                await runner.cleanup()
        finally:
            await http.close()


async def test_parse_is_fast() -> None:
    import time

    plugin = provider()
    reqs = [note(operation_id=str(i), label=str(uuid.uuid4())) for i in range(300)]
    started = time.perf_counter()
    for req in reqs:
        await plugin.parse_webhook(req)
    assert (time.perf_counter() - started) * 1000 / len(reqs) < 5


async def test_paid_at_is_operation_time() -> None:
    at = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)
    assert (await provider().parse_webhook(note(at=at))).paid_at == at
    msk = (await provider().parse_webhook(note(extra={"datetime": "2026-10-02T12:30:00.000+03:00"}))).paid_at
    assert msk == at
