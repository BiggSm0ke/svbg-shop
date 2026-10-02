"""Built-in payment plugins (07 §4.4): waves A, B, C and D — 27 plugins.

Every module here imports only :mod:`svbg.sdk` (checked by ``tests/payments/providers/test_isolation.py``);
the origin of each protocol is recorded in ``docs/providers/PROVENANCE.md``. Integration registers them with::

    ProviderCatalog(BUILTIN_PROVIDERS)

Order of :data:`BUILTIN_PROVIDERS` is the display order (wave A first, then B, then C–D). ``cashera``,
``jupiter`` and ``donut`` wait for documentation and have no plugin yet.
"""

from __future__ import annotations

from typing import Final

from svbg.payments.providers.antilopay import Antilopay
from svbg.payments.providers.aurapay import AuraPay
from svbg.payments.providers.cispay import CisPay
from svbg.payments.providers.cloudpayments import CloudPayments
from svbg.payments.providers.cryptobot import CryptoBot
from svbg.payments.providers.cryptomus import Cryptomus
from svbg.payments.providers.etoplatezhi import Etoplatezhi
from svbg.payments.providers.freekassa import Freekassa
from svbg.payments.providers.heleket import Heleket
from svbg.payments.providers.lava import Lava
from svbg.payments.providers.manual import ManualTransfer
from svbg.payments.providers.mulenpay import MulenPay
from svbg.payments.providers.overpay import Overpay
from svbg.payments.providers.pal24 import Pal24
from svbg.payments.providers.paritypay import ParityPay
from svbg.payments.providers.paypear import PayPear
from svbg.payments.providers.platega import Platega
from svbg.payments.providers.riopay import RioPay
from svbg.payments.providers.robokassa import Robokassa
from svbg.payments.providers.rollypay import RollyPay
from svbg.payments.providers.severpay import SeverPay
from svbg.payments.providers.stars import TelegramStars
from svbg.payments.providers.tabpay import TabPay
from svbg.payments.providers.tribute import Tribute
from svbg.payments.providers.wata import Wata
from svbg.payments.providers.yookassa import YooKassa
from svbg.payments.providers.yoomoney import YooMoney
from svbg.sdk import PaymentProvider

__all__ = [
    "BUILTIN_PROVIDERS",
    "Antilopay",
    "AuraPay",
    "CisPay",
    "CloudPayments",
    "CryptoBot",
    "Cryptomus",
    "Etoplatezhi",
    "Freekassa",
    "Heleket",
    "Lava",
    "ManualTransfer",
    "MulenPay",
    "Overpay",
    "Pal24",
    "ParityPay",
    "PayPear",
    "Platega",
    "RioPay",
    "Robokassa",
    "RollyPay",
    "SeverPay",
    "TabPay",
    "TelegramStars",
    "Tribute",
    "Wata",
    "YooKassa",
    "YooMoney",
]

#: Display order: wave A, wave B, waves C–D.
BUILTIN_PROVIDERS: Final[tuple[type[PaymentProvider], ...]] = (
    # wave A
    RollyPay,
    TelegramStars,
    CryptoBot,
    ManualTransfer,
    # wave B
    YooKassa,
    Platega,
    Freekassa,
    YooMoney,
    Robokassa,
    Cryptomus,
    Heleket,
    Wata,
    MulenPay,
    # waves C–D
    Pal24,
    Lava,
    Tribute,
    CloudPayments,
    RioPay,
    SeverPay,
    PayPear,
    Overpay,
    AuraPay,
    Etoplatezhi,
    Antilopay,
    CisPay,
    TabPay,
    ParityPay,
)
