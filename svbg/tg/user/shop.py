"""Buying with the wallet (07 §4.5): plan → period → checkout → «Оплатить» → (not enough) «Пополнить на X».

Screens and actions (all re-validate their callback arguments: callback data can be forged):

* ``buy`` — plans for sale (the current plan first for renewal); a single plan skips straight to its periods.
  One SQL (status), the catalog is in memory.
* ``buy_plan`` — periods with the benefit of the longer ones («3 мес. — 499 ₽ · 166 ₽/мес, −7%»). One SQL.
* ``buy:pick`` — a draft with the frozen price (billing: 2 SQL) and the checkout summary. With enough money
  it offers «Оплатить»; when the balance is short it offers the payment methods right away («📱 СБП — 179 ₽»,
  one button per method kind, provider names hidden) and «Другая сумма» — the first purchase is three taps
  from the menu: «Купить» → period → method.
* ``pay:go`` — «Оплатить»: billing debits the wallet in one transaction (then «⏳ Оформляю…» turns into
  «✅ … + 🔗 Подключиться» by itself) or answers «не хватает X» → the shortfall screen (the same method
  buttons). A repeated tap on an order that is already paid shows «Подключиться».
* ``pay:inv`` — for a purchase first «Оплатить» (billing: the order waits for this top-up, or is paid outright
  when the balance grew meanwhile), then the message turns into «⏳ Создаю счёт…» without buttons, the top-up
  and its invoice are created (the only provider call, with a timeout) and the pay button is shown **in the
  same message**; after the money arrives billing completes the purchase and edits this message. A repeated
  tap on the same message within :data:`INVOICE_REUSE` shows the invoice already created instead of a new one.
  ``pay:paid`` — «Я оплатил» (an immediate status check, budget-capped by the poller).
* ``bal`` / ``topup`` — the balance with ``WALLET_TOPUP_PRESETS`` and «Другая сумма» (a form).

Hooks set by the app after construction (both optional, isolated: a failing hook never breaks the purchase):

* :attr:`ShopScreens.promo` — the promo service: ``checkout_discounts(user_id, plan_id)`` priced into the
  draft (the discount line is shown on the checkout screen) and
  ``claim(conn, order_id=…, user_id=…, snapshot=…)`` right before «Оплатить» (only for a user with a waiting
  discount: no SQL otherwise);
* :attr:`ShopScreens.link_code` — ``user_id → plan code`` of a link-only plan opened through its deep link
  (``catalog.for_sale(..., link_code=…)``, in memory).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import CreateInvoiceLink, EditMessageText
from aiogram.types import InlineKeyboardButton, LabeledPrice

from svbg.billing.checkout import BillingError
from svbg.billing.ports import UiRef
from svbg.billing.tables import orders
from svbg.core.clock import now
from svbg.core.money import exponent, parse_money
from svbg.payments.tables import payments as payments_t
from svbg.subscriptions.hold import localize_spend
from svbg.tg.ui import codec
from svbg.tg.ui.forms import Field, Form, ValidationError
from svbg.tg.ui.renderer import nav_button
from svbg.tg.ui.view import Redirect, Toast, View
from svbg.tg.user import seeds
from svbg.tg.user.base import Base, parse_ids
from svbg.tg.user.deps import cfg_int_list
from svbg.tg.user.render import screen_view
from svbg.tg.user.status import wallet_users
from svbg.tg.user.texts import fmt_date, method_label, money, plural_days, t

if TYPE_CHECKING:
    from svbg.billing.checkout import Draft, PayResult, TopupResult
    from svbg.billing.service import TopupOption
    from svbg.catalog.model import Plan
    from svbg.tg.ui.context import UserCtx
    from svbg.tg.ui.router import HandlerResult, ScreenCtx, ScreenRouter
    from svbg.tg.user.status import UserStatus

__all__ = ["FORM_AMOUNT", "INVOICE_REUSE", "PAY", "ShopScreens"]

log = logging.getLogger("svbg.tg.user.shop")

PAY: Final = "pay"  # callback namespace of the payment actions
FORM_AMOUNT: Final = "user.topup_amount"
I_PAID_TIMEOUT_S: Final = 8.0
DEFAULT_PRESETS: Final = (100, 300, 500)
_PAYMENT_ID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_WAITING: Final = ("draft", "awaiting_funds")
#: A second tap on the same method button within this window shows the invoice already created.
INVOICE_REUSE: Final = timedelta(minutes=10)
#: An invoice that expires sooner than this is not offered again.
_REUSE_MIN_LEFT: Final = timedelta(minutes=2)


class ShopScreens(Base):
    #: Promo service (``checkout_discounts``, ``claim``, ``pending``); set by the app.
    promo: Any = None
    #: ``user_id → link-only plan code`` (deep links); set by the app.
    link_code: Callable[[int], str | None] | None = None

    # ------------------------------------------------------------------ registration

    def register(self, router: ScreenRouter) -> None:
        router.screen(seeds.BUY)(self.buy)
        router.screen(seeds.BUY_PLAN)(self.plan_screen)
        router.screen(seeds.CHECKOUT)(self.checkout_screen)
        router.screen(seeds.SHORTFALL)(self.shortfall_screen)
        router.screen(seeds.BALANCE)(self.balance)
        router.screen(seeds.TOPUP)(self.topup_screen)
        router.action(seeds.BUY, "pick")(self.pick)
        router.action(seeds.BUY, "reorder")(self.reorder)
        router.action(PAY, "go")(self.pay)
        router.action(PAY, "inv")(self.invoice)
        router.action(PAY, "paid")(self.i_paid)
        router.action(PAY, "amt")(self.other_amount)
        router.action(PAY, "cancel")(self.cancel)
        router.form(
            Form(
                FORM_AMOUNT,
                (
                    Field(
                        "amount",
                        {"ru": t("ru", "amount_prompt"), "en": t("en", "amount_prompt")},
                        self._amount,
                    ),
                ),
                on_done=self._amount_done,
            )
        )

    # ------------------------------------------------------------------ helpers

    @property
    def scale(self) -> int:
        try:
            return 10 ** exponent(self.currency)
        except ValueError:
            return 100

    def money(self, amount_minor: int, lang: str, currency: str | None = None) -> str:
        return money(amount_minor, currency or self.currency, lang)

    def _snapshot(self) -> Any:
        catalog = self.deps.catalog
        return None if catalog is None else catalog.snapshot

    def _link_code(self, user_id: int) -> str | None:
        fn = self.link_code
        if fn is None:
            return None
        try:
            code = fn(user_id)
        except Exception:  # noqa: BLE001 - a deep-link grant is a convenience
            log.warning("link plan lookup failed for user %s", user_id)
            return None
        return code if isinstance(code, str) and code else None

    async def _discounts(self, user_id: int, plan_id: int, lang: str = "ru") -> list[Any]:
        """Promo discounts for a draft of ``plan_id`` (``[]`` without the promo module or on its failure)."""
        promo = self.promo
        if promo is None:
            return []
        try:
            return list(await promo.checkout_discounts(user_id, plan_id, lang))
        except Exception:
            log.exception("promo discounts failed for user %s", user_id)
            return []

    async def _claim_promo(self, order_id: int, user_id: int, lang: str = "ru") -> str | None:
        """Bind the user's reserved promo use to the order before «Оплатить» (``None``: fine)."""
        promo = self.promo
        if promo is None or promo.pending(user_id) is None:
            return None
        try:
            async with self.deps.db.tx() as conn:
                snapshot = await conn.scalar(
                    sa.select(orders.c.snapshot).where(
                        orders.c.id == order_id,
                        orders.c.user_id == user_id,
                        orders.c.status.in_(_WAITING),
                    )
                )
                if not isinstance(snapshot, Mapping):
                    return None
                return await promo.claim(
                    conn, order_id=order_id, user_id=user_id, snapshot=snapshot, lang=lang
                )
        except Exception:
            log.exception("promo claim failed for order %s", order_id)
            return None

    def _sale_plans(self, user: UserCtx, status: UserStatus | None) -> list[Plan]:
        snap = self._snapshot()
        if snap is None:
            return []
        cur = self.currency
        plans: list[Plan] = list(snap.for_sale(user, currency=cur, link_code=self._link_code(user.user_id)))
        sub = status.sub if status is not None else None
        if sub is not None and not sub.is_trial and sub.plan_id is not None:
            current = snap.renewable(sub.plan_id, currency=cur)
            if current is not None:
                plans = [current, *(p for p in plans if p.id != current.id)]
        return plans

    def _user_with(self, ctx: ScreenCtx, balance_minor: int) -> None:
        ctx.user = replace(ctx.user, balance_minor=balance_minor, _placeholders=None)

    @staticmethod
    def _ref(ctx: ScreenCtx) -> UiRef | None:
        return UiRef(ctx.chat_id, ctx.message_id, now()) if ctx.message_id is not None else None

    async def _order(self, order_id: int, user_id: int) -> Mapping[str, Any] | None:
        """The order with the buyer's balance (1 SQL)."""
        stmt = (
            sa.select(
                orders.c.id,
                orders.c.status,
                orders.c.kind,
                orders.c.total_minor,
                orders.c.currency,
                orders.c.plan_id,
                orders.c.snapshot,
                wallet_users.c.wallet_minor,
            )
            .select_from(orders.join(wallet_users, wallet_users.c.id == orders.c.user_id))
            .where(orders.c.id == order_id, orders.c.user_id == user_id, orders.c.kind != "topup")
        )
        async with self.deps.db.read() as conn:
            return (await conn.execute(stmt)).mappings().first()

    # ------------------------------------------------------------------ plans and periods

    async def buy(self, ctx: ScreenCtx, _arg: Any) -> HandlerResult:
        status = await self.enrich(ctx)
        lang = ctx.lang
        plans = self._sale_plans(ctx.user, status)
        if not plans:
            return View(text=t(lang, "no_plans"), keyboard=[*self.support_row(lang), self.menu(lang)])
        if len(plans) == 1:
            return self._plan_view(ctx, status, plans[0], single=True)
        rows = []
        for plan in plans:
            low = plan.min_price(self.currency)
            label = t(
                lang,
                "btn_plan",
                plan=plan.title(lang),
                price=self.money(low.amount_minor, lang) if low else "—",
            )
            rows.append([nav_button(label, seeds.BUY_PLAN, codec.ACTION_OPEN, str(plan.id))])
        return screen_view(ctx, seeds.BUY, top=rows)

    async def plan_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        if ids is None:
            return Redirect(seeds.BUY)
        status = await self.enrich(ctx)
        plans = self._sale_plans(ctx.user, status)
        plan = next((p for p in plans if p.id == ids[0]), None)
        if plan is None:
            return Redirect(seeds.BUY, toast=t(ctx.lang, "plan_gone"))
        return self._plan_view(ctx, status, plan, single=len(plans) == 1)

    def _plan_view(self, ctx: ScreenCtx, status: UserStatus | None, plan: Plan, *, single: bool) -> View:
        lang = ctx.lang
        prices = sorted(plan.prices_in(self.currency), key=lambda p: p.days)
        base_ts = 0
        sub = status.sub if status is not None else None
        if sub is not None and sub.paid_until is not None and sub.paid_until > now():
            base_ts = int(sub.paid_until.timestamp())
        rows: list[list[InlineKeyboardButton]] = []
        ref = prices[0].amount_minor / prices[0].days if prices else 0.0
        for p in prices:
            period = plural_days(p.days, lang)
            price = self.money(p.amount_minor, lang)
            per_day = p.amount_minor / p.days
            save = round((1 - per_day / ref) * 100) if ref else 0
            if p.days > prices[0].days and save >= 1:
                monthly = self.money(round(per_day * 30 / self.scale) * self.scale, lang)  # whole units
                label = t(lang, "btn_period_save", period=period, price=price, monthly=monthly, save=save)
            else:
                label = t(lang, "btn_period", period=period, price=price)
            arg = f"{plan.id}:{p.days}:{base_ts}"
            rows.append([nav_button(label, seeds.BUY, "pick", arg, style="success" if p.highlight else None)])
        limit = plan.device_limit
        values = {
            "plan": plan.title(lang),
            "devices": t(lang, "devices_unlimited") if not limit else str(limit),
            "traffic": t(lang, "unlimited")
            if plan.unlimited_traffic
            else f"{plan.traffic_bytes / 1024**3:g} GB",
        }
        back = self.back(lang, seeds.HOME if single else seeds.BUY)
        return screen_view(ctx, seeds.BUY_PLAN, values, top=rows, bottom=[back])

    # ------------------------------------------------------------------ checkout

    def _until(self, kind: str, days: int, base_ts: int) -> str:
        at = now()
        base = at
        if base_ts > 0 and kind in ("renew", "change"):
            base = max(at, datetime.fromtimestamp(base_ts, tz=at.tzinfo))
        text = fmt_date(base + timedelta(days=days), self.tz)
        return f"≈ {text}" if kind == "change" else text

    def _checkout_view(
        self,
        ctx: ScreenCtx,
        *,
        order_id: int,
        title: str,
        days: int,
        total: int,
        balance: int,
        until: str,
        plan_id: int | None,
        discounts: Sequence[Any] = (),
    ) -> View:
        lang = ctx.lang
        self._user_with(ctx, balance)
        price = self.money(total, lang)
        labels = [
            str((d.get("label") if isinstance(d, Mapping) else getattr(d, "label", "")) or "")
            for d in discounts
        ]
        # «💳 Цена: 399 ₽ (Промокод AUTUMN −20 %)»
        price_line = f"{price} ({', '.join(x for x in labels if x)})" if any(labels) else price
        pay_button = [nav_button(t(lang, "btn_pay", price=price), PAY, "go", str(order_id), style="success")]
        top = [pay_button]
        if total == 0:
            pay_line = t(lang, "pay_line_free")
        elif balance >= total:
            pay_line = t(lang, "pay_line_enough", price=price, left=self.money(balance - total, lang))
        else:
            missing = total - balance
            options = self._options(missing)
            if options:
                # Not enough money: the methods right away (one tap fewer than «Оплатить» → «Не хватает»).
                pay_line = t(lang, "pay_line_methods", missing=self.money(missing, lang))
                pay_line += self._surplus_note(lang, options)
                top = self._method_rows(lang, options, order_id)
                top.append([nav_button(t(lang, "btn_other_amount"), PAY, "amt", str(order_id))])
            else:  # «Оплатить» leads to the shortfall screen that explains it and links support
                pay_line = t(lang, "pay_line_short", missing=self.money(missing, lang))
        values = {
            "plan": title,
            "period": plural_days(days, lang),
            "until": until,
            "price": price_line,
            "pay_line": pay_line,
        }
        bottom = [self.back(lang, seeds.BUY_PLAN, str(plan_id))] if plan_id else []
        bottom.append(self.menu(lang))
        return screen_view(ctx, seeds.CHECKOUT, values, top=top, bottom=bottom)

    def _draft_view(self, ctx: ScreenCtx, draft: Draft, base_ts: int) -> View:
        q = draft.quote
        return self._checkout_view(
            ctx,
            order_id=draft.order_id,
            title=q.title,
            days=q.days,
            total=q.total_minor,
            balance=draft.balance_minor,
            until=self._until(q.kind, q.days, base_ts),
            plan_id=q.plan_id,
            discounts=getattr(q, "discounts", ()) or (),
        )

    async def pick(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 3)
        billing = self.deps.billing
        if ids is None or billing is None:
            return Redirect(seeds.BUY)
        plan_id, days, base_ts = ids
        uid = ctx.user.user_id
        try:
            draft = await billing.checkout.draft_plan(
                uid,
                plan_id,
                days,
                lang=ctx.lang,
                link_code=self._link_code(uid),
                discounts=await self._discounts(uid, plan_id, ctx.lang),
            )
        except BillingError as e:
            return Toast(e.localized(ctx.lang), alert=True)
        return self._draft_view(ctx, draft, base_ts)

    async def reorder(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        billing = self.deps.billing
        if ids is None or billing is None:
            return Redirect(seeds.BUY)
        try:
            draft = await billing.checkout.reorder(ids[0], ctx.user.user_id, lang=ctx.lang)
        except BillingError as e:
            return Toast(e.localized(ctx.lang), alert=True)
        return self._draft_view(ctx, draft, 0)

    async def checkout_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        if ids is None:
            return Redirect(seeds.BUY)
        order = await self._order(ids[0], ctx.user.user_id)
        if order is None or order["status"] not in _WAITING:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "order_gone"))
        snap = order["snapshot"] or {}
        days = int(snap.get("days") or 0)
        return self._checkout_view(
            ctx,
            order_id=int(order["id"]),
            title=str(snap.get("title") or ""),
            days=days,
            total=int(order["total_minor"]),
            balance=int(order["wallet_minor"] or 0),
            until=self._until(str(order["kind"]), days, 0) if days else "—",
            plan_id=order["plan_id"],
            discounts=snap.get("discounts") or (),
        )

    async def cancel(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        billing = self.deps.billing
        if ids is not None and billing is not None:
            await billing.checkout.cancel(ids[0], ctx.user.user_id)
        return Redirect(seeds.HOME, toast=t(ctx.lang, "order_canceled"))

    # ------------------------------------------------------------------ pay

    async def pay(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        billing = self.deps.billing
        if ids is None or billing is None:
            return Redirect(seeds.BUY)
        refused = await self._claim_promo(ids[0], ctx.user.user_id, ctx.lang)
        if refused:
            return Toast(refused, alert=True)
        try:
            res = await billing.checkout.pay(ids[0], ctx.user.user_id, ui_ref=self._ref(ctx))
        except BillingError as e:
            return Toast(e.localized(ctx.lang), alert=True)
        shown = await self._pay_outcome(ctx, res)
        if shown is not None:
            return shown
        return self._shortfall_view(ctx, res.order_id, res.price_minor, res.balance_minor)

    async def _pay_outcome(self, ctx: ScreenCtx, res: PayResult) -> HandlerResult | None:
        """The screen after «Оплатить»; ``None`` when the order now waits for a top-up."""
        lang = ctx.lang
        self._user_with(ctx, res.balance_minor)
        if res.outcome == "already":
            # A repeated tap. Still being fulfilled: billing will draw the result, «Оформляю…» is right.
            # Fulfilled: the result may have been drawn long ago and nothing would replace «Оформляю…».
            order = await self._order(res.order_id, ctx.user.user_id)
            if order is not None and order["status"] == "fulfilled":
                return Redirect(seeds.CONNECT, toast=t(lang, "paid_already"))
        if res.outcome in ("paid", "already"):
            # billing edits this message into «✅ Оплачено + 🔗 Подключиться» (after this screen is drawn)
            return screen_view(ctx, seeds.PAY_WAIT, bottom=[self.menu(lang)])
        if res.outcome == "denied":
            text = localize_spend(res.text, lang) or t(lang, "error_generic")
            return View(text=text, keyboard=[*self.support_row(lang), self.menu(lang)])
        return None

    async def shortfall_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1)
        if ids is None:
            return Redirect(seeds.HOME)
        order = await self._order(ids[0], ctx.user.user_id)
        if order is None or order["status"] not in _WAITING:
            return Redirect(seeds.HOME, toast=t(ctx.lang, "order_gone"))
        return self._shortfall_view(
            ctx, int(order["id"]), int(order["total_minor"]), int(order["wallet_minor"] or 0)
        )

    def _options(self, amount_minor: int) -> list[TopupOption]:
        """One option per method kind (the cheapest for the user), in the registry's display order."""
        billing, payments = self.deps.billing, self.deps.payments
        if billing is None or payments is None or amount_minor <= 0:
            return []
        picked: dict[str, TopupOption] = {}
        for opt in billing.topup_options(amount_minor, payments.instances.all()):
            key = opt.method_kind or f"#{opt.instance_id}"
            best = picked.get(key)
            if best is None or opt.credit_minor < best.credit_minor:
                picked[key] = opt
        return list(picked.values())

    def _method_rows(
        self, lang: str, options: Sequence[TopupOption], parent: int | None
    ) -> list[list[InlineKeyboardButton]]:
        rows = []
        for opt in options:
            icon, name = method_label(opt.method_kind, lang)
            amount = money(opt.pay_amount_minor, opt.pay_currency, lang)
            label = t(lang, "btn_topup_method", icon=icon, method=name, amount=amount)
            arg = f"{parent if parent is not None else '-'}:{opt.instance_id}:{opt.credit_minor}"
            rows.append([nav_button(label, PAY, "inv", arg, style="primary")])
        return rows

    def _surplus_note(self, lang: str, options: Sequence[TopupOption]) -> str:
        with_surplus = [o for o in options if o.surplus_minor > 0]
        if not with_surplus:
            return ""
        if len(options) == 1:
            opt = options[0]
            return t(
                lang,
                "surplus_exact",
                method=method_label(opt.method_kind, lang)[1],
                amount=self.money(opt.credit_minor, lang),
                surplus=self.money(opt.surplus_minor, lang),
            )
        return t(lang, "surplus")

    def _shortfall_view(self, ctx: ScreenCtx, order_id: int, price: int, balance: int) -> View:
        lang = ctx.lang
        self._user_with(ctx, balance)
        missing = max(0, price - balance)
        options = self._options(missing)
        rows = self._method_rows(lang, options, order_id)
        note = self._surplus_note(lang, options)
        if not options:
            note = "\n\n" + t(lang, "no_methods")
        rows.append([nav_button(t(lang, "btn_other_amount"), PAY, "amt", str(order_id))])
        values = {
            "price": self.money(price, lang),
            "missing": self.money(missing, lang),
            "surplus_note": note,
        }
        bottom = [
            self.back(lang, seeds.CHECKOUT, str(order_id)),
            [nav_button(t(lang, "btn_cancel_order"), PAY, "cancel", str(order_id))],
            *([] if options else self.support_row(lang)),
        ]
        return screen_view(ctx, seeds.SHORTFALL, values, top=rows, bottom=bottom)

    # ------------------------------------------------------------------ invoices

    @staticmethod
    def _invoice_arg(arg: Any) -> tuple[int | None, int, int] | None:
        if not isinstance(arg, str):
            return None
        parts = arg.split(":")
        if len(parts) != 3:
            return None
        head, rest = parts[0], parts[1:]
        ids = parse_ids(":".join(rest), 2)
        if ids is None:
            return None
        if head == "-":
            parent = None
        else:
            one = parse_ids(head, 1)
            if one is None:
                return None
            parent = one[0]
        return parent, ids[0], ids[1]

    async def invoice(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        from svbg.payments.core import CheckoutError

        parsed = self._invoice_arg(arg)
        billing, payments = self.deps.billing, self.deps.payments
        if parsed is None or billing is None or payments is None:
            return Redirect(seeds.BALANCE)
        parent, instance_id, amount = parsed
        lang = ctx.lang
        if parent is not None:
            # «Оплатить» first: the purchase waits for exactly this top-up (or is paid when the balance grew).
            refused = await self._claim_promo(parent, ctx.user.user_id, lang)
            if refused:
                return Toast(refused, alert=True)
            try:
                res = await billing.checkout.pay(parent, ctx.user.user_id, ui_ref=self._ref(ctx))
            except BillingError as e:
                return Toast(e.localized(lang), alert=True)
            shown = await self._pay_outcome(ctx, res)
            if shown is not None:
                return shown
            if amount < res.missing_minor:  # the balance changed since the buttons were drawn
                return self._shortfall_view(ctx, res.order_id, res.price_minor, res.balance_minor)
        inst = payments.instances.get(instance_id)
        kinds = tuple(getattr(inst, "method_kinds", ()) or ())
        await ctx.answer()
        await self._progress(ctx, t(lang, "creating_invoice"))
        result = await self._recent_invoice(ctx, parent, instance_id, amount)
        if result is None:
            try:
                result = await billing.checkout.start_topup(
                    ctx.user.user_id,
                    instance_id=instance_id,
                    amount_minor=amount,
                    parent_order_id=parent,
                    ui_ref=self._ref(ctx),
                    method_kind=kinds[0] if kinds else None,
                    lang=lang,
                )
            except BillingError as e:
                return self._invoice_error(ctx, e.localized(lang), parent, amount)
            except CheckoutError as e:  # includes SpendDeniedError (frozen / banned)
                return self._invoice_error(ctx, localize_spend(e.localized(lang), lang), parent, amount)
        return await self._invoice_view(ctx, result, parent, amount)

    @staticmethod
    async def _progress(ctx: ScreenCtx, text: str) -> None:
        """Turn the clicked message into ``text`` without buttons while the provider is called: the progress
        is visible and the method buttons cannot be tapped again. Best effort (the result is drawn anyway)."""
        if ctx.message_id is None or ctx.shape is None or ctx.shape.is_media:
            return
        method = EditMessageText(chat_id=ctx.chat_id, message_id=ctx.message_id, text=text, parse_mode=None)
        try:
            await ctx.router.transport.call(method, chat_id=ctx.chat_id)
        except TelegramAPIError as e:
            log.debug("progress edit skipped: %s", type(e).__name__)

    async def _recent_invoice(
        self, ctx: ScreenCtx, parent: int | None, instance_id: int, amount: int
    ) -> TopupResult | None:
        """The invoice this very message created a moment ago for the same method and amount (a double tap
        queued behind the first one), so no second invoice is opened at the provider. One SQL."""
        from svbg.billing.checkout import TopupResult
        from svbg.sdk.payments import Checkout

        if ctx.message_id is None:
            return None
        at = now()
        stmt = (
            sa.select(
                payments_t.c.id,
                payments_t.c.amount_minor,
                payments_t.c.currency,
                payments_t.c.checkout,
                orders.c.id.label("order_id"),
                orders.c.total_minor,
            )
            .select_from(payments_t.join(orders, orders.c.id == payments_t.c.order_id))
            .where(
                payments_t.c.user_id == ctx.user.user_id,
                payments_t.c.instance_id == instance_id,
                payments_t.c.status == "pending",
                payments_t.c.created_at > at - INVOICE_REUSE,
                payments_t.c.checkout["kind"].astext.is_not(None),
                sa.or_(payments_t.c.expires_at.is_(None), payments_t.c.expires_at > at + _REUSE_MIN_LEFT),
                orders.c.user_id == ctx.user.user_id,
                orders.c.kind == "topup",
                orders.c.status == "awaiting_payment",
                orders.c.total_minor == amount,
                orders.c.parent_order_id.is_not_distinct_from(parent),
                orders.c.ui_ref["chat_id"].astext == str(ctx.chat_id),
                orders.c.ui_ref["message_id"].astext == str(ctx.message_id),
            )
            .order_by(payments_t.c.created_at.desc())
            .limit(1)
        )
        async with self.deps.db.read() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        if row is None or not isinstance(row["checkout"], Mapping):
            return None
        co = row["checkout"]
        try:
            checkout = Checkout(
                kind=co["kind"],
                pay_url=co.get("pay_url"),
                invoice=co.get("invoice"),
                details=co.get("details"),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return TopupResult(
            int(row["order_id"]),
            str(row["id"]),
            int(row["total_minor"]),
            int(row["amount_minor"]),
            str(row["currency"]),
            checkout,
        )

    def _other_method_row(self, lang: str, parent: int | None, amount: int) -> list[InlineKeyboardButton]:
        if parent is not None:
            return [nav_button(t(lang, "btn_other_method"), seeds.SHORTFALL, codec.ACTION_OPEN, str(parent))]
        return [nav_button(t(lang, "btn_other_method"), seeds.TOPUP, codec.ACTION_OPEN, str(amount))]

    def _invoice_error(self, ctx: ScreenCtx, text: str, parent: int | None, amount: int) -> View:
        lang = ctx.lang
        keyboard = [self._other_method_row(lang, parent, amount), *self.support_row(lang), self.menu(lang)]
        return View(text=f"⚠️ {text}", keyboard=keyboard)

    async def _invoice_view(
        self, ctx: ScreenCtx, result: TopupResult, parent: int | None, amount: int
    ) -> View:
        lang = ctx.lang
        co = result.checkout
        values = {
            "amount": self.money(result.credit_minor, lang),
            "pay_amount": money(result.pay_amount_minor, result.pay_currency, lang),
            "after": t(lang, "after_purchase" if parent is not None else "after_topup"),
            "details": str(getattr(co, "details", "") or ""),
        }
        bottom = [self._other_method_row(lang, parent, amount), self.menu(lang)]
        kind = getattr(co, "kind", None)
        if kind == "details":
            return screen_view(ctx, seeds.PAY_DETAILS, values, bottom=bottom)
        top: list[list[InlineKeyboardButton]] = []
        if kind == "url" and co.pay_url:
            label = t(lang, "btn_pay_url", amount=values["pay_amount"])
            top.append([InlineKeyboardButton(text=label, url=co.pay_url, style="success")])
            top.append([nav_button(t(lang, "btn_i_paid"), PAY, "paid", result.payment_id)])
        elif kind == "invoice" and co.invoice:
            link = await self._invoice_link(ctx, co.invoice)
            if link is None:
                return self._invoice_error(ctx, t(lang, "error_generic"), parent, amount)
            label = t(lang, "btn_pay_stars", amount=values["pay_amount"])
            top.append([InlineKeyboardButton(text=label, url=link, style="success")])
        else:
            return self._invoice_error(ctx, t(lang, "error_generic"), parent, amount)
        return screen_view(ctx, seeds.PAY_INVOICE, values, top=top, bottom=bottom)

    @staticmethod
    async def _invoice_link(ctx: ScreenCtx, invoice: Mapping[str, Any]) -> str | None:
        """``createInvoiceLink`` from the plugin's invoice parameters (the bot owns the token)."""
        currency = str(invoice.get("currency") or "XTR")
        default_title = "Balance top-up" if ctx.lang == "en" else "Пополнение баланса"
        title = str(invoice.get("title") or default_title)[:32]
        raw_prices = invoice.get("prices")
        prices: list[LabeledPrice] = []
        if isinstance(raw_prices, list):
            for p in raw_prices:
                if isinstance(p, Mapping) and isinstance(p.get("amount"), int):
                    prices.append(
                        LabeledPrice(label=str(p.get("label") or title)[:32], amount=int(p["amount"]))
                    )
        elif isinstance(invoice.get("amount"), int):
            prices.append(LabeledPrice(label=title, amount=int(invoice["amount"])))
        payload = invoice.get("payload")
        if not prices or not isinstance(payload, str):
            return None
        method = CreateInvoiceLink(
            title=title,
            description=str(invoice.get("description") or title)[:255],
            payload=payload,
            currency=currency,
            prices=prices,
            provider_token="" if currency == "XTR" else invoice.get("provider_token"),
        )
        try:
            link = await ctx.router.transport.call(method, chat_id=ctx.chat_id)
        except TelegramAPIError as e:
            log.warning("createInvoiceLink failed: %s", type(e).__name__)
            return None
        return link if isinstance(link, str) and link.startswith("https://") else None

    async def i_paid(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        lang = ctx.lang
        check = self.deps.i_paid
        if not isinstance(arg, str) or not _PAYMENT_ID_RE.match(arg):
            return Toast(t(lang, "error_generic"))
        if check is None:
            return Toast(t(lang, "not_paid_yet"), alert=True)
        try:
            async with asyncio.timeout(I_PAID_TIMEOUT_S):
                record = await check(arg, ctx.user.user_id)
        except TimeoutError:
            return Toast(t(lang, "not_paid_yet"), alert=True)
        if record is None:
            return Toast(t(lang, "error_generic"))
        if record.status == "paid":
            return Toast(t(lang, "paid_already"))
        return Toast(t(lang, "not_paid_yet"), alert=True)

    # ------------------------------------------------------------------ balance and top-up

    def _presets(self) -> list[int]:
        default: Sequence[int] = DEFAULT_PRESETS
        snap = self._snapshot()
        if snap is not None:
            for plan in snap.plans:
                prices = sorted(plan.prices_in(self.currency), key=lambda p: p.days)[:3]
                if plan.sellable_shape and prices and all(p.amount_minor % self.scale == 0 for p in prices):
                    default = [p.amount_minor // self.scale for p in prices]
                    break
        return cfg_int_list(self.deps.config, "WALLET_TOPUP_PRESETS", default)

    def _limits(self) -> tuple[int, int]:
        billing = self.deps.billing
        if billing is None:
            return 1, 10**12
        cfg = billing.config
        return cfg.topup_min_minor, cfg.topup_max_minor

    async def balance(self, ctx: ScreenCtx, _arg: Any) -> View:
        await self.enrich(ctx)
        lang = ctx.lang
        lo, hi = self._limits()
        amounts = [p * self.scale for p in self._presets() if lo <= p * self.scale <= hi]
        buttons = [nav_button(self.money(a, lang), seeds.TOPUP, codec.ACTION_OPEN, str(a)) for a in amounts]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        rows.append([nav_button(t(lang, "btn_other_amount"), PAY, "amt", "-")])
        return screen_view(ctx, seeds.BALANCE, top=rows)

    async def topup_screen(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        ids = parse_ids(arg, 1) or parse_ids(arg, 2)
        if ids is None:
            return Redirect(seeds.BALANCE)
        amount, parent = ids[0], (ids[1] if len(ids) == 2 else None)
        lo, hi = self._limits()
        if not lo <= amount <= hi:
            return Redirect(seeds.BALANCE)
        return self._topup_view(ctx, amount, parent)

    def _topup_view(self, ctx: ScreenCtx, amount: int, parent: int | None) -> View:
        lang = ctx.lang
        options = self._options(amount)
        rows = self._method_rows(lang, options, parent)
        note = self._surplus_note(lang, options) if options else "\n\n" + t(lang, "no_methods")
        back = (
            self.back(lang, seeds.SHORTFALL, str(parent))
            if parent is not None
            else self.back(lang, seeds.BALANCE)
        )
        bottom = [back, *([] if options else self.support_row(lang)), self.menu(lang)]
        values = {"amount": self.money(amount, lang), "surplus_note": note}
        return screen_view(ctx, seeds.TOPUP, values, top=rows, bottom=bottom)

    async def other_amount(self, ctx: ScreenCtx, arg: Any) -> HandlerResult:
        parent: int | None = None
        if arg != "-":
            ids = parse_ids(arg, 1)
            if ids is None:
                return Redirect(seeds.BALANCE)
            parent = ids[0]
        return await ctx.start_form(FORM_AMOUNT, {"o": parent})

    def _amount(self, value: str) -> int:
        """Form validator: an amount in the shop currency within ``WALLET_TOPUP_MIN..MAX``."""
        try:
            amount = parse_money(value, self.currency)
        except ValueError:
            raise ValidationError(t("ru", "amount_bad"), t("en", "amount_bad")) from None
        lo, hi = self._limits()
        if not lo <= amount <= hi:
            raise ValidationError(
                t("ru", "amount_range", min=self.money(lo, "ru"), max=self.money(hi, "ru")),
                t("en", "amount_range", min=self.money(lo, "en"), max=self.money(hi, "en")),
            )
        return amount

    async def _amount_done(self, ctx: ScreenCtx, data: dict[str, Any]) -> HandlerResult:
        amount = data.get("amount")
        parent = data.get("o")
        if not isinstance(amount, int) or isinstance(amount, bool):
            return Redirect(seeds.BALANCE)
        if isinstance(parent, int) and not isinstance(parent, bool):
            order = await self._order(parent, ctx.user.user_id)
            if order is None or order["status"] not in _WAITING:
                return Redirect(seeds.HOME, toast=t(ctx.lang, "order_gone"))
            missing = max(0, int(order["total_minor"]) - int(order["wallet_minor"] or 0))
            if amount < missing:
                view = await ctx.start_form(FORM_AMOUNT, {"o": parent})
                hint = t(ctx.lang, "amount_min", min=self.money(missing, ctx.lang))
                view.text = f"⚠️ {hint}\n\n{view.text}"
                return view
            return self._topup_view(ctx, amount, parent)
        return self._topup_view(ctx, amount, None)
