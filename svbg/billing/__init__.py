"""Billing (07 §4.5): orders with a frozen price, the wallet with its ledger, «Оплатить» from the balance,
top-ups with auto-complete of the waiting purchase, exactly-once fulfill and the purchase message.

* :mod:`svbg.billing.tables` — ``orders``, ``order_items``, ``wallet_ledger``, ``manual_receipts``;
* :mod:`svbg.billing.wallet` — CAS debit / idempotent credit (one statement each);
* :mod:`svbg.billing.checkout` — drafts, pay, top-ups, the sweeper;
* :mod:`svbg.billing.crediting` — the payment core's ``on_paid`` / ``on_refunded`` (auto-complete);
* :mod:`svbg.billing.fulfill` — jobs ``billing.fulfill`` / ``billing.ui`` / notices;
* :mod:`svbg.billing.receipts` — manual payments (receipt card, confirm / reject);
* :mod:`svbg.billing.service` — :class:`~svbg.billing.service.Billing`, the facade the app wires.

Import submodules explicitly (``svbg.billing.service`` pulls the payment core and the subscriptions).
"""
