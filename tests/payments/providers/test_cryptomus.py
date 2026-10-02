"""Cryptomus plugin: the shared TestKit plus the family vectors (``cryptomus_family.py``) and
Cryptomus-specific checks from ``docs/providers/cryptomus.md``."""

from __future__ import annotations

import pytest

from svbg.payments.providers.cryptomus import API_URL, WEBHOOK_IP, Cryptomus
from tests.fakes.cryptomus import FakeCryptomus
from tests.payments.providers.cryptomus_family import *  # noqa: F403 - the shared suite runs per provider
from tests.payments.providers.cryptomus_family import Family, provider


@pytest.fixture
def fam() -> Family:
    return Family(
        provider=Cryptomus, fake=FakeCryptomus, api_url=API_URL, webhook_ip=WEBHOOK_IP, order_id_max=100
    )


def test_cryptomus_constants(fam: Family) -> None:
    assert API_URL == "https://api.cryptomus.com" and WEBHOOK_IP == "91.227.144.54"
    manifest = Cryptomus.manifest
    assert manifest.slug == "cryptomus" and manifest.docs_url.startswith("https://doc.cryptomus.com/")
    fields = manifest.config.fields()
    assert fields["api_key"].is_secret and not fields["merchant_id"].is_secret
    assert WEBHOOK_IP in (fields["allowed_ips"].where or "")
    assert all("Cryptomus" in (f.where or "Cryptomus") for f in fields.values())


async def test_base_url_override(fam: Family) -> None:
    from svbg.payments.testkit import CountingHttp

    http = CountingHttp(FakeCryptomus(base_url="https://proxy.example"))
    await provider(fam, http, base_url="https://proxy.example/").test_credentials()
    assert http.calls[0].url == "https://proxy.example/v1/payment/services"
