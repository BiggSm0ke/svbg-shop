"""Cash-desk payments (06 §2.4.2–2.4.4, §4.9): history + protection from double crediting + live invoices.

Every row of ``rollypay_payments`` / ``cryptobot_payments`` / ``platega_payments`` and every Stars top-up of
``transactions`` becomes a ``payments`` row (``is_imported=true``) of the instance ``rollypay`` /
``cryptobot``
/ ``platega_legacy`` / ``stars`` with the provider's id in ``external_id``: a late ``paid`` / ``refund`` /
``chargeback`` for an old invoice finds it by ``UNIQUE(instance, external_id)`` — no second credit, a refund
gets context. Imported ``paid`` rows never touch the wallet (the balance is carried over by the opening).

Invoices **alive at T0** (RollyPay ``pending|created|processing`` or ``expired`` ≤ 48 h, CryptoBot ``active``
≤ 24 h) become ``orders(kind='topup', status='awaiting_payment')`` + ``payments(pending)``: a late payment
is credited by the normal flow (webhook or poll), exactly once. The reconciler is armed for them **only by
``apply``** (``poll_plan='domain'``, ``next_check_at=T0``): in ``shadow`` / ``dry_run`` they carry no
schedule, so the stand never asks a cash desk about an invoice that Bedolaga still owns (the poller reads
every instance, disabled ones too) — a paid one would otherwise be credited twice. A live CryptoBot invoice
was issued in its asset (``currency_type=crypto``): the payment keeps the asset amount (that is what the
provider reports and the core reconciles), the top-up order keeps the rubles of the payload.

Missing instances are created **disabled** (history only, keys are entered in the wizard); this needs the
crypto key — without it the run reports ``payment_instance_missing``. A repeated run refreshes the status
of an imported payment while the bot has not changed it (a source invoice that got paid in Bedolaga →
``paid``, its order closed: the money arrives through the balance). A source ``paid`` always wins over
a local ``expired|canceled|failed|pending`` (settled without crediting — otherwise a late report would
credit the balance a second time); against a local ``paid`` / ``mismatch`` / ``refunded`` it blocks the
cut-over.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.billing.tables import orders
from svbg.core.money import exponent
from svbg.importers.bedolaga.plan import chunks
from svbg.payments.tables import LATE_PAYABLE, payment_instances, payments

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["INSTANCES", "check", "run"]

#: slug → (provider plugin, title, method kind).
INSTANCES: Final[Mapping[str, tuple[str, str, str | None]]] = {
    "rollypay": ("rollypay", "RollyPay (СБП)", "sbp"),
    "cryptobot": ("cryptobot", "CryptoBot", "crypto"),
    "platega_legacy": ("platega", "Platega (история)", None),
    "stars": ("stars", "Telegram Stars", "stars"),
}
_ROLLY_LIVE: Final = frozenset({"pending", "created", "processing"})
_KOPEKS_IN_PAYLOAD: Final = re.compile(r"_(\d+)_(\d+)$")
#: Reconciler plan of a live imported invoice once armed (``apply``): webhooks reach the shop after T0.
POLL_PLAN: Final = "domain"


@dataclass(slots=True)
class _Pay:
    key: str  # legacy key "<table>:<id>"
    slug: str
    user_id: int | None
    external_id: str | None
    merchant_ref: str | None
    status: str
    amount_minor: int
    currency: str
    paid_amount_minor: int | None
    paid_currency: str | None
    paid_at: datetime | None
    created_at: datetime | None
    expires_at: datetime | None
    description: str
    metadata: dict[str, Any]
    live: bool = False
    order_minor: int | None = None  # RUB amount of the top-up order of a live invoice
    #: A live invoice whose late payment the core cannot match by itself (owner confirms by hand).
    manual: bool = False


def _asset_minor(amount: Any, asset: Any) -> tuple[int, str]:
    """CryptoBot amount in the invoice asset → exact minor units, when the asset is a currency the payment
    core knows (``svbg.core.money``); ``0`` otherwise (unknown asset, fractional minor units)."""
    code = str(asset or "").strip().upper()
    try:
        exp = exponent(code)
        value = Decimal(str(amount).strip().replace(",", "."))
        scaled = value.scaleb(exp)
    except (ValueError, InvalidOperation):
        return 0, code or "RUB"
    if not scaled.is_finite() or scaled <= 0 or scaled != scaled.to_integral_value():
        return 0, code
    return int(scaled), code


async def _instances(ctx: Ctx) -> None:
    rows = (await ctx.conn.execute(sa.select(payment_instances.c.slug, payment_instances.c.id))).all()
    have = {str(s): int(i) for s, i in rows}
    for slug, (provider, title, kind) in INSTANCES.items():
        if slug in have:
            ctx.instances[slug] = have[slug]
            continue
        crypto = ctx.cfg.crypto
        if crypto is None and not ctx.dry:
            ctx.report.issue("payment_instance_missing", slug=slug)
            continue

        def enc(value: str) -> str:
            return crypto.encrypt(value) if crypto is not None else "enc:v1:dry-run"  # noqa: B023

        new_id = await ctx.conn.scalar(
            sa.insert(payment_instances)
            .values(
                provider=provider,
                slug=slug,
                title=title,
                enabled=False,
                method_kinds=[kind] if kind else [],
                currencies=["XTR"] if slug == "stars" else [ctx.cfg.currency],
                config=enc("{}"),
                webhook_token=enc(secrets.token_urlsafe(32)),
                kv={"created_by": "import:bedolaga"},
            )
            .returning(payment_instances.c.id)
        )
        ctx.instances[slug] = int(new_id)
        ctx.report.inc("payments", "instances_created")


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def _tx_amounts(ctx: Ctx) -> dict[int, int]:
    rows = await ctx.src.rows("transactions", ["id", "amount_kopeks"])
    return {int(r["id"]): int(r["amount_kopeks"] or 0) for r in rows}


async def _collect(ctx: Ctx) -> list[_Pay]:
    t0, cur = ctx.t0, ctx.cfg.currency
    out: list[_Pay] = []
    rolly_live = timedelta(hours=ctx.cfg.rollypay_live_hours)
    for r in await ctx.src.rows(
        "rollypay_payments",
        [
            "id",
            "user_id",
            "order_id",
            "rollypay_payment_id",
            "amount_kopeks",
            "currency",
            "description",
            "status",
            "is_paid",
            "payment_method",
            "paid_at",
            "expires_at",
            "created_at",
            "updated_at",
            "transaction_id",
        ],
    ):
        raw = str(r["status"] or "").lower()
        created = r["created_at"] or t0
        live = False
        if r["is_paid"]:
            status = "paid"
        elif raw in _ROLLY_LIVE or (raw == "expired" and created > t0 - rolly_live):
            status, live = "pending", True
        elif raw in ("canceled", "cancelled"):
            status = "canceled"
        elif raw in ("failed", "error", "rejected"):
            status = "failed"
        else:
            status = "expired"
        amount = int(r["amount_kopeks"] or 0)
        out.append(
            _Pay(
                key=f"rollypay:{r['id']}",
                slug="rollypay",
                user_id=r["user_id"],
                external_id=r["rollypay_payment_id"],
                merchant_ref=r["order_id"],
                status=status,
                amount_minor=amount,
                currency=str(r["currency"] or cur).upper(),
                paid_amount_minor=amount if status == "paid" else None,
                paid_currency=str(r["currency"] or cur).upper() if status == "paid" else None,
                paid_at=(r["paid_at"] or r["updated_at"] or created) if status == "paid" else None,
                created_at=created,
                expires_at=r["expires_at"] or (created + rolly_live if live else None),
                description=str(r["description"] or "")[:300],
                metadata={
                    "legacy": {
                        "table": "rollypay_payments",
                        "id": r["id"],
                        "status": r["status"],
                        "method": r["payment_method"],
                        "transaction_id": r["transaction_id"],
                    }
                },
                live=live,
                order_minor=amount if live else None,
            )
        )
    tx_amount = await _tx_amounts(ctx)
    crypto_live = timedelta(hours=ctx.cfg.cryptobot_live_hours)
    for r in await ctx.src.rows(
        "cryptobot_payments",
        [
            "id",
            "user_id",
            "invoice_id",
            "amount",
            "asset",
            "status",
            "description",
            "payload",
            "paid_at",
            "transaction_id",
            "created_at",
            "updated_at",
        ],
    ):
        raw = str(r["status"] or "").lower()
        created = r["created_at"] or t0
        live = False
        if raw == "paid":
            status = "paid"
        elif raw == "active" and created > t0 - crypto_live:
            status, live = "pending", True
        elif raw == "active":
            status = "expired"
        else:
            status = "expired" if raw in ("expired", "") else "canceled"
        rub = tx_amount.get(int(r["transaction_id"])) if r["transaction_id"] else None
        if rub is None and r["payload"]:
            m = _KOPEKS_IN_PAYLOAD.search(str(r["payload"]))
            rub = int(m.group(2)) if m else None
        asset_amount, asset = _asset_minor(r["amount"], r["asset"])
        amount, currency = rub or 0, cur
        manual = False
        if live and asset_amount > 0:
            # The invoice lives in its asset: a late webhook / getInvoices reports «3.1 USDT», and the core
            # credits only an exact match (amount and currency) — the rubles stay on the top-up order.
            amount, currency = asset_amount, asset
        elif live:
            manual = True  # an asset the core cannot reconcile: a late payment ends in «mismatch»
        if not amount:
            amount, currency = asset_amount, asset
        out.append(
            _Pay(
                key=f"cryptobot:{r['id']}",
                slug="cryptobot",
                user_id=r["user_id"],
                external_id=r["invoice_id"],
                merchant_ref=None,
                status=status,
                amount_minor=amount,
                currency=currency,
                paid_amount_minor=amount if status == "paid" else None,
                paid_currency=currency if status == "paid" else None,
                paid_at=(r["paid_at"] or r["updated_at"] or created) if status == "paid" else None,
                created_at=created,
                expires_at=created + crypto_live if live else None,
                description=str(r["description"] or "")[:300],
                metadata={
                    "legacy": {
                        "table": "cryptobot_payments",
                        "id": r["id"],
                        "status": r["status"],
                        "asset": r["asset"],
                        "amount": r["amount"],
                        "transaction_id": r["transaction_id"],
                    }
                },
                live=live,
                order_minor=rub if live else None,
                manual=manual,
            )
        )
    for r in await ctx.src.rows(
        "platega_payments",
        [
            "id",
            "user_id",
            "correlation_id",
            "platega_transaction_id",
            "amount_kopeks",
            "currency",
            "description",
            "status",
            "is_paid",
            "paid_at",
            "created_at",
            "updated_at",
        ],
    ):
        raw = str(r["status"] or "").lower()
        status = (
            "paid"
            if r["is_paid"]
            else ("canceled" if "cancel" in raw else ("failed" if "fail" in raw else "expired"))
        )
        amount = int(r["amount_kopeks"] or 0)
        created = r["created_at"] or t0
        out.append(
            _Pay(
                key=f"platega:{r['id']}",
                slug="platega_legacy",
                user_id=r["user_id"],
                external_id=r["correlation_id"],
                merchant_ref=r["platega_transaction_id"],
                status=status,
                amount_minor=amount,
                currency=str(r["currency"] or cur).upper(),
                paid_amount_minor=amount if status == "paid" else None,
                paid_currency=str(r["currency"] or cur).upper() if status == "paid" else None,
                paid_at=(r["paid_at"] or r["updated_at"] or created) if status == "paid" else None,
                created_at=created,
                expires_at=None,
                description=str(r["description"] or "")[:300],
                metadata={"legacy": {"table": "platega_payments", "id": r["id"], "status": r["status"]}},
            )
        )
    rate = Decimal(str(ctx.settings.float("TELEGRAM_STARS_RATE_RUB", 1.0)))
    for r in await ctx.src.rows(
        "transactions",
        [
            "id",
            "user_id",
            "type",
            "amount_kopeks",
            "description",
            "payment_method",
            "external_id",
            "is_completed",
            "created_at",
            "completed_at",
        ],
    ):
        # The same rule as the shadow reconciliation (С3): a completed Stars credit with a charge id.
        if "star" not in str(r["payment_method"] or "").lower() or not r["external_id"]:
            continue
        kopeks = int(r["amount_kopeks"] or 0)
        if kopeks <= 0 or r["is_completed"] is False:
            continue
        try:
            stars = int((Decimal(kopeks) / 100 / rate).to_integral_value()) if rate > 0 else 0
        except (InvalidOperation, ZeroDivisionError):
            stars = 0
        created = r["created_at"] or t0
        out.append(
            _Pay(
                key=f"stars:{r['id']}",
                slug="stars",
                user_id=r["user_id"],
                external_id=str(r["external_id"]),
                merchant_ref=None,
                status="paid",
                amount_minor=stars,
                currency="XTR",
                paid_amount_minor=stars,
                paid_currency="XTR",
                paid_at=r["completed_at"] or created,
                created_at=created,
                expires_at=None,
                description=str(r["description"] or "")[:300],
                metadata={"legacy": {"table": "transactions", "id": r["id"]}, "credited_minor": kopeks},
            )
        )
    return out


async def run(ctx: Ctx) -> None:
    rep = ctx.report
    await _instances(ctx)
    items = await _collect(ctx)
    mapped = await ctx.mapped("payment")
    order_map = await ctx.mapped("order")
    new_rows: list[dict[str, Any]] = []
    remember: list[tuple[str, str, dict[str, Any]]] = []
    for p in items:
        rep.inc("payments", f"source_{p.slug}")
        if p.status == "paid":
            rep.inc("payments", f"source_paid_{p.slug}")
        if p.live:
            rep.inc("payments", f"source_live_{p.slug}")
        inst = ctx.instances.get(p.slug)
        if inst is None:
            continue
        if p.user_id is None or int(p.user_id) not in ctx.users:
            kind = "payment_user_missing" if (p.status == "paid" or p.live) else "payment_history_skipped"
            rep.issue(kind, key=p.key, user_id=p.user_id, status=p.status, amount_minor=p.amount_minor)
            continue
        if p.amount_minor <= 0:
            kind = "live_invoice_unresolved" if p.live else "payment_amount_unknown"
            rep.issue(kind, key=p.key, user_id=p.user_id, status=p.status)
            continue
        if p.live and not p.external_id:
            rep.issue(
                "live_invoice_without_external_id", key=p.key, user_id=p.user_id, merchant_ref=p.merchant_ref
            )
        if p.manual:
            legacy = p.metadata.get("legacy") or {}
            rep.issue(
                "live_invoice_asset_manual",
                key=p.key,
                user_id=p.user_id,
                asset=legacy.get("asset"),
                amount=legacy.get("amount"),
            )
        prev = mapped.get(p.key)
        if prev is not None:
            await _refresh(ctx, p, prev, order_map)
            continue
        row = {
            "instance_id": inst,
            "user_id": int(p.user_id),
            "order_id": None,
            "external_id": p.external_id,
            "merchant_ref": p.merchant_ref,
            "status": p.status,
            "amount_minor": p.amount_minor,
            "currency": p.currency,
            "paid_amount_minor": p.paid_amount_minor,
            "paid_currency": p.paid_currency,
            "method_kind": INSTANCES[p.slug][2],
            "description": p.description,
            "is_imported": True,
            "metadata": p.metadata,
            "expires_at": p.expires_at,
            "paid_at": p.paid_at,
            "poll_plan": None,  # armed by _schedule() in apply only
            "next_check_at": None,
            "created_at": p.created_at,
        }
        if p.live:
            await _insert_live(ctx, p, row, order_map, remember)
            continue
        new_rows.append({**row, "_key": p.key})
    for chunk in chunks(new_rows, 500):
        stmt = (
            pg_insert(payments)
            .values([{k: v for k, v in r.items() if k != "_key"} for r in chunk])
            .on_conflict_do_nothing(index_elements=[payments.c.instance_id, payments.c.external_id])
            .returning(payments.c.id, payments.c.instance_id, payments.c.external_id, payments.c.metadata)
        )
        inserted = (await ctx.conn.execute(stmt)).all()
        by_legacy = {_legacy_key(r[3]): str(r[0]) for r in inserted}
        for r in chunk:
            pid = by_legacy.get(r["_key"])
            if pid is None:
                rep.issue("payment_external_id_taken", key=r["_key"], external_id=r["external_id"])
                continue
            remember.append((r["_key"], pid, {"status": r["status"]}))
            rep.inc("payments", f"imported_{r['status']}")
    await ctx.remember("payment", remember)
    await _schedule(ctx, [p.key for p in items if p.live])


async def _schedule(ctx: Ctx, live_keys: Sequence[str]) -> None:
    """Who polls the cash desk about an imported invoice that is still pending.

    * ``apply`` (T0, Bedolaga stopped): the live invoices get the reconciler plan — webhook + poll settle
      a late payment exactly once (06 §2.4.3);
    * ``shadow`` / ``dry_run``: nobody. The invoice is Bedolaga's until T0 and the stand's poller reads every
      instance (disabled ones too): a poll would credit here what Bedolaga credits there. A schedule left
      by an earlier run is removed.
    """
    mapped = await ctx.mapped("payment")
    if ctx.mode == "apply":
        ids = [mapped[k][0] for k in dict.fromkeys(live_keys) if k in mapped]
        values: dict[str, Any] = {"poll_plan": POLL_PLAN, "next_check_at": ctx.t0}
        where = payments.c.status == "pending"
    else:
        ids = [v[0] for v in mapped.values()]
        values = {"poll_plan": None, "next_check_at": None}
        where = sa.and_(
            payments.c.status == "pending",
            sa.or_(payments.c.poll_plan.is_not(None), payments.c.next_check_at.is_not(None)),
        )
    changed = 0
    for chunk in chunks(ids, 5000):
        res = await ctx.conn.execute(
            sa.update(payments)
            .where(payments.c.id.in_(chunk), payments.c.is_imported.is_(True), where)
            .values(**values, updated_at=sa.func.now())
        )
        changed += res.rowcount or 0
    ctx.report.set("payments", "reconciler_armed" if ctx.mode == "apply" else "reconciler_disarmed", changed)


async def _insert_live(
    ctx: Ctx,
    p: _Pay,
    row: dict[str, Any],
    order_map: dict[str, tuple[str, dict[str, Any]]],
    remember: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """A live invoice: the payment first (its external id may be taken), then its top-up order (rubles; the
    payment itself may be in the invoice's crypto asset)."""
    pid = await ctx.conn.scalar(
        pg_insert(payments)
        .values(**row)
        .on_conflict_do_nothing(index_elements=[payments.c.instance_id, payments.c.external_id])
        .returning(payments.c.id)
    )
    if pid is None:
        ctx.report.issue("payment_external_id_taken", key=p.key, external_id=p.external_id)
        return
    if p.order_minor:
        order_id = await _order(ctx, p, order_map)
        await ctx.conn.execute(sa.update(payments).where(payments.c.id == pid).values(order_id=order_id))
    else:
        ctx.report.issue("live_invoice_unresolved", key=p.key, user_id=p.user_id, currency=p.currency)
    remember.append((p.key, str(pid), {"status": p.status}))
    ctx.report.inc("payments", "imported_pending")


def _legacy_key(meta: Mapping[str, Any]) -> str:
    leg = (meta or {}).get("legacy") or {}
    table = str(leg.get("table"))
    prefix = {
        "rollypay_payments": "rollypay",
        "cryptobot_payments": "cryptobot",
        "platega_payments": "platega",
        "transactions": "stars",
    }.get(table, table)
    return f"{prefix}:{leg.get('id')}"


async def _order(ctx: Ctx, p: _Pay, order_map: dict[str, tuple[str, dict[str, Any]]]) -> int:
    prev = order_map.get(p.key)
    if prev is not None:
        return int(prev[0])
    order_id = int(
        await ctx.conn.scalar(
            sa.insert(orders)
            .values(
                user_id=int(p.user_id or 0),
                kind="topup",
                status="awaiting_payment",
                currency=ctx.cfg.currency,
                total_minor=int(p.order_minor or 0),
                snapshot={
                    "legacy": {"source": "bedolaga", "key": p.key, "merchant_ref": p.merchant_ref},
                    "pay_amount_minor": p.amount_minor,
                    "pay_currency": p.currency,
                },
                note="Счёт Bedolaga, выставлен до переезда",
                created_at=p.created_at,
            )
            .returning(orders.c.id)
        )
    )
    await ctx.remember("order", [(p.key, order_id, {})])
    ctx.report.inc("payments", "live_orders")
    return order_id


async def _refresh(ctx: Ctx, p: _Pay, prev: tuple[str, dict[str, Any]], order_map: Mapping[str, Any]) -> None:
    """A repeated run: follow the source status while the bot has not touched the payment. A source
    ``paid`` (Bedolaga credited its balance, which the import carries over) is never left behind a payable
    local status (``expired|canceled|failed|pending``): a late report would credit the money once more."""
    pid, data = prev
    row = (await ctx.conn.execute(sa.select(payments.c.status).where(payments.c.id == pid))).first()
    if row is None:
        ctx.report.issue("payment_vanished", key=p.key, payment_id=pid)
        return
    local, remembered = str(row[0]), data.get("status")
    if p.status == local and (p.status == remembered or p.status != "paid"):
        ctx.report.inc("payments", "unchanged")
        return
    if p.status == remembered and p.status != "paid":
        ctx.report.inc("payments", "unchanged")  # the source did not move; the bot's own change stays
        return
    if local != remembered:
        if local == "paid" and p.status == "paid":
            # Credited by this bot (late webhook / reconciler) AND by Bedolaga (its balance): the wallet
            # holds the money twice — the cut-over is blocked until the owner settles it.
            ctx.report.issue(
                "payment_paid_twice",
                key=p.key,
                payment_id=pid,
                amount_minor=p.amount_minor,
                user_id=p.user_id,
            )
            return
        if p.status == "paid" and local in LATE_PAYABLE:
            # E.g. the stand expired it, then Bedolaga got the late «paid»: settled here without a credit.
            ctx.report.issue("payment_settled_from_source", key=p.key, payment_id=pid, status=local)
        elif p.status == "paid":
            ctx.report.issue(
                "payment_paid_conflict", key=p.key, payment_id=pid, status=local, user_id=p.user_id
            )
            return
        else:
            ctx.report.issue(
                "payment_changed_locally", key=p.key, payment_id=pid, status=local, source=p.status
            )
            return
    values: dict[str, Any] = {
        "status": p.status,
        "paid_amount_minor": p.paid_amount_minor,
        "paid_currency": p.paid_currency,
        "paid_at": p.paid_at,
        "updated_at": sa.func.now(),
    }
    if p.status != "pending":
        values.update(poll_plan=None, next_check_at=None)
    if p.status == "paid":
        # History keeps the credited rubles (С3); a live crypto invoice had kept its asset amount.
        values.update(amount_minor=p.amount_minor, currency=p.currency)
    res = await ctx.conn.execute(
        sa.update(payments).where(payments.c.id == pid, payments.c.status == local).values(**values)
    )
    if not res.rowcount:
        ctx.report.issue("payment_changed_locally", key=p.key, payment_id=pid, status=local, source=p.status)
        return
    if p.key in order_map and p.status != "pending":
        # Paid / expired in Bedolaga: the money (if any) comes with the balance, the top-up order is closed.
        await ctx.conn.execute(
            sa.update(orders)
            .where(orders.c.id == int(order_map[p.key][0]), orders.c.status == "awaiting_payment")
            .values(
                status="expired" if p.status != "paid" else "canceled",
                note="Закрыт в Bedolaga до переезда",
                updated_at=sa.func.now(),
            )
        )
    await ctx.remember("payment", [(p.key, pid, {"status": p.status})])
    ctx.report.inc("payments", "refreshed")


async def check(ctx: Ctx) -> None:
    """С3 (paid per instance: count and sum) and С4 (every live invoice → ``pending`` with an order)."""
    rep = ctx.report
    items = await _collect(ctx)
    keys = await ctx.mapped("payment")
    ids = [v[0] for v in keys.values()]
    found: dict[str, tuple[str, int, int | None, Any]] = {}
    for chunk in chunks(ids, 5000):
        for r in (
            await ctx.conn.execute(
                sa.select(
                    payments.c.id, payments.c.status, payments.c.amount_minor, payments.c.order_id
                ).where(payments.c.id.in_(chunk))
            )
        ).all():
            found[str(r[0])] = (str(r[1]), int(r[2]), r[3], None)
    c3: dict[str, dict[str, int]] = {}
    ok3 = True
    live_total = live_ok = 0
    for p in items:
        eligible = p.user_id is not None and int(p.user_id) in ctx.users and p.amount_minor > 0
        if p.status == "paid":
            b = c3.setdefault(p.slug, {"source_n": 0, "source_sum": 0, "n": 0, "sum": 0, "excluded": 0})
            if not eligible:  # reported (payment_user_missing / payment_amount_unknown)
                b["excluded"] += 1
                continue
            b["source_n"] += 1
            b["source_sum"] += p.amount_minor
            got = found.get(keys.get(p.key, ("", {}))[0])
            if got is not None and got[0] == "paid":
                b["n"] += 1
                b["sum"] += got[1]
        if p.live:
            live_total += 1
            got = found.get(keys.get(p.key, ("", {}))[0])
            if eligible and got is not None and got[0] == "pending" and got[2] is not None:
                live_ok += 1
    for b in c3.values():
        ok3 = ok3 and b["source_n"] == b["n"] and b["source_sum"] == b["sum"]
    eligible_all = [
        p
        for p in items
        if p.slug in ctx.instances
        and p.user_id is not None
        and int(p.user_id) in ctx.users
        and p.amount_minor > 0
    ]
    present = sum(1 for p in eligible_all if keys.get(p.key, ("", {}))[0] in found)
    rep.part("C1", "payments", expected=len(eligible_all), present=present)
    rep.check("C3", ok3, per_instance=c3)
    rep.check("C4", live_ok == live_total, live=live_total, imported_pending=live_ok)
