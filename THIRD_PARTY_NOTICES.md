# Third-party notices

SvBG Shop includes code adapted from the following third-party projects.

## Remnashop

- Project: Remnashop — <https://github.com/snoups/remnashop>
- License: MIT
- Used in:
  - `svbg/payments/providers/stars.py` — adapted from `src/infrastructure/payment_gateways/telegram_stars.py`
    (Telegram Stars invoice parameters: `XTR` currency, empty provider token, a single price, the payment id
    as the invoice payload);
  - `svbg/payments/providers/cryptobot.py` — adapted from `src/infrastructure/payment_gateways/cryptopay.py`
    (Crypto Pay `createInvoice` with fiat invoices, `bot_invoice_url`, webhook signature
    `HMAC_SHA256(key = SHA256(token), body)`).

  Both were rewritten for the SvBG payment SDK (`svbg.sdk`): status mapping, batch status checks, test
  mode / testnet, error handling and texts are SvBG's own.

  Wave B (adapted from `src/infrastructure/payment_gateways/<file>` of the same project):
  - `svbg/payments/providers/yookassa.py` — from `yookassa.py` (YooKassa API v3 payment creation, receipt
    parameters, notification handling);
  - `svbg/payments/providers/platega.py` — from `platega.py` (`transaction/process`, `X-MerchantId` /
    `X-Secret` headers, callback);
  - `svbg/payments/providers/freekassa.py` — from `freekassa.py` (SCI form link and MD5 signatures, API order
    creation, notification signature);
  - `svbg/payments/providers/yoomoney.py` — from `yoomoney.py` (quickpay form parameters, notification fields);
  - `svbg/payments/providers/robokassa.py` — from `robokassa.py` (payment link, `SignatureValue` for the
    payment link and the Result URL);
  - `svbg/payments/providers/cryptomus.py` — from `cryptomus.py` (invoice creation, request and webhook
    signatures);
  - `svbg/payments/providers/heleket.py` — from `heleket.py` (same scheme as Cryptomus);
  - `svbg/payments/providers/wata.py` — from `wata.py` (payment links, webhook RSA signature);
  - `svbg/payments/providers/mulenpay.py` — from `mulen_pay.py` (payment creation and its signature, callback
    fields).

  All wave B ports were checked against the providers' official documentation (2026-10-02) and rewritten for
  `svbg.sdk`: mandatory status re-checks, Decimal amounts, signature checks, status mapping, test modes,
  error handling and texts are SvBG's own.

```
MIT License

Copyright (c) 2024 snoups

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
