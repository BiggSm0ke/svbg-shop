"""Fake Heleket merchant API — the Cryptomus desk (:mod:`tests.fakes.cryptomus`) with Heleket's host, payment
page and ``order_id`` limit (1–128), per ``docs/providers/heleket.md``."""

from __future__ import annotations

from typing import ClassVar

from tests.fakes.cryptomus import MERCHANT, FakeCryptomus, Invoice, php_encode, sign, signed_body

__all__ = [
    "API_KEY",
    "FAKE_BASE_URL",
    "MERCHANT",
    "FakeHeleket",
    "Invoice",
    "php_encode",
    "sign",
    "signed_body",
]

API_KEY = "hK3yP4ymentKeyHeleketLive0123456789abcdefABCDEFGH"
FAKE_BASE_URL = "https://api.heleket.com"


class FakeHeleket(FakeCryptomus):
    order_id_max: ClassVar[int] = 128
    pay_host: ClassVar[str] = "pay.heleket.com"
    default_base_url: ClassVar[str] = FAKE_BASE_URL
    default_key: ClassVar[str] = API_KEY
    webhook_ip: ClassVar[str] = "31.133.220.8"
