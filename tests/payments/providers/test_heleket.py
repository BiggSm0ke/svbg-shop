"""Heleket plugin: the shared TestKit plus the family vectors (``cryptomus_family.py``) and
Heleket-specific checks from ``docs/providers/heleket.md``."""

from __future__ import annotations

import pytest

from svbg.payments.providers.heleket import API_URL, WEBHOOK_IP, Heleket
from tests.fakes.heleket import FakeHeleket
from tests.payments.providers.cryptomus_family import *  # noqa: F403 - the shared suite runs per provider
from tests.payments.providers.cryptomus_family import Family, provider


@pytest.fixture
def fam() -> Family:
    return Family(
        provider=Heleket, fake=FakeHeleket, api_url=API_URL, webhook_ip=WEBHOOK_IP, order_id_max=128
    )


def test_heleket_constants(fam: Family) -> None:
    assert API_URL == "https://api.heleket.com" and WEBHOOK_IP == "31.133.220.8"
    manifest = Heleket.manifest
    assert manifest.slug == "heleket" and manifest.docs_url.startswith("https://doc.heleket.com/")
    fields = manifest.config.fields()
    assert fields["api_key"].is_secret and not fields["merchant_id"].is_secret
    assert WEBHOOK_IP in (fields["allowed_ips"].where or "")
    assert all("Heleket" in (f.where or "Heleket") for f in fields.values())


async def test_base_url_override(fam: Family) -> None:
    from svbg.payments.testkit import CountingHttp

    http = CountingHttp(FakeHeleket(base_url="https://proxy.example"))
    await provider(fam, http, base_url="https://proxy.example/").test_credentials()
    assert http.calls[0].url == "https://proxy.example/v1/payment/services"
