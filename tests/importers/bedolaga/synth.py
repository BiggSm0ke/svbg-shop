"""Synthetic Bedolaga data on the real schema: a generic row writer (NOT NULL columns without a default are
filled automatically, so the scenarios name only the columns they test) + the edge-case scenario of 06 §2 + a
volume generator (~2.5k users / 2.1k subscriptions / 1.35k transactions)."""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from svbg.remnawave.models import PanelUser, SquadRef

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SQ_NL = "11111111-1111-4111-8111-111111111111"
SQ_DE = "22222222-2222-4222-8222-222222222222"
SQ_TWIN = "33333333-3333-4333-8333-333333333333"  # LTE twin of SQ_NL


def _fill(data_type: str) -> Any:
    if data_type == "boolean":
        return False
    if data_type in ("integer", "bigint", "smallint", "double precision", "numeric", "real"):
        return 0
    if data_type.startswith("timestamp"):
        return T0
    if data_type in ("json", "jsonb"):
        return "{}"
    if data_type == "uuid":
        return "00000000-0000-4000-8000-000000000000"
    return "x"


class Src:
    """Writes rows into the synthetic source (a test-only connection; the importer only reads)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self._required: dict[str, dict[str, str]] = {}
        self._types: dict[str, dict[str, str]] = {}

    @classmethod
    async def connect(cls, dsn: str) -> Src:
        return cls(await asyncpg.connect(dsn))

    async def close(self) -> None:
        await self.conn.close()

    async def _meta(self, table: str) -> None:
        if table in self._types:
            return
        rows = await self.conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
        self._types[table] = {r["column_name"]: r["data_type"] for r in rows}
        self._required[table] = {
            r["column_name"]: r["data_type"]
            for r in rows
            if r["is_nullable"] == "NO" and r["column_default"] is None
        }

    async def many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        await self._meta(table)
        types = self._types[table]
        cols = sorted(set(self._required[table]) | set().union(*(r.keys() for r in rows)))
        unknown = [c for c in cols if c not in types]
        assert not unknown, f"{table}: no columns {unknown}"
        values = []
        for r in rows:
            row = []
            for c in cols:
                if c in r:
                    v = r[c]
                elif c in self._required[table]:
                    v = _fill(self._required[table][c])
                else:
                    v = None
                if types[c] in ("json", "jsonb") and v is not None and not isinstance(v, str):
                    v = json.dumps(v)
                row.append(v)
            values.append(row)
        placeholders = ", ".join(
            f"${i + 1}::{'jsonb' if types[c] == 'jsonb' else ('json' if types[c] == 'json' else types[c])}"
            if types[c] in ("json", "jsonb")
            else f"${i + 1}"
            for i, c in enumerate(cols)
        )
        quoted = ", ".join(f'"{c}"' for c in cols)
        await self.conn.executemany(
            f'INSERT INTO public."{table}" ({quoted}) VALUES ({placeholders})', values
        )

    async def add(self, table: str, **values: Any) -> None:
        await self.many(table, [values])


def panel_user(
    pid: int,
    *,
    short: str | None = None,
    tg: int | None = None,
    status: str = "ACTIVE",
    expire: datetime | None = None,
    squads: Sequence[str] = (SQ_NL,),
    devices: int | None = 5,
    traffic: int = 0,
    strategy: str = "MONTH",
    tag: str | None = "PAID",
    username: str | None = None,
) -> PanelUser:
    return PanelUser(
        id=pid,
        short_uuid=short or f"short{pid}",
        username=username or (f"user_{tg}" if tg else f"panel_{pid}"),
        status=status,
        traffic_limit_bytes=traffic,
        traffic_limit_strategy=strategy,
        expire_at=expire or T0 + timedelta(days=20),
        telegram_id=tg,
        tag=tag,
        hwid_device_limit=devices,
        subscription_url=f"https://sub.example.com/{short or f'short{pid}'}",
        active_internal_squads=[SquadRef(uuid=s) for s in squads],
    )


@dataclass
class Scenario:
    panel: list[PanelUser] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


ENV = {
    "AVAILABLE_SUBSCRIPTION_PERIODS": "30,90,180,360",
    "PRICE_14_DAYS": "9900",
    "PRICE_30_DAYS": "17900",
    "PRICE_90_DAYS": "49900",
    "PRICE_180_DAYS": "89900",
    "PRICE_360_DAYS": "169900",
    "DEFAULT_DEVICE_LIMIT": "5",
    "MAX_DEVICES_LIMIT": "15",
    "PRICE_PER_DEVICE": "1900",
    "DEFAULT_TRAFFIC_RESET_STRATEGY": "MONTH",
    "PAID_SUBSCRIPTION_USER_TAG": "PAID",
    "TRIAL_DURATION_DAYS": "3",
    "TRIAL_DEVICE_LIMIT": "5",
    "TELEGRAM_STARS_RATE_RUB": "1",
}


async def seed_edge_cases(src: Src) -> Scenario:
    """Every mapped column and edge case of 06 §2 (ids are small and fixed; see the test for expectations)."""
    sc = Scenario(env=dict(ENV))
    h = timedelta(hours=1)
    d = timedelta(days=1)
    await src.many(
        "server_squads",
        [
            {
                "id": 1,
                "squad_uuid": SQ_NL,
                "display_name": "🇳🇱 Нидерланды",
                "original_name": "NL",
                "country_code": "NL",
                "is_available": True,
                "is_trial_eligible": True,
                "price_kopeks": 0,
                "sort_order": 1,
            },
            {
                "id": 2,
                "squad_uuid": SQ_DE,
                "display_name": "Германия",
                "original_name": "DE",
                "country_code": "de",
                "is_available": True,
                "is_trial_eligible": False,
                "price_kopeks": 5000,
                "sort_order": 2,
            },
        ],
    )
    await src.add(
        "wlq_squads",
        id=1,
        kind="twin",
        base_squad_uuid=SQ_NL,
        panel_uuid=SQ_TWIN,
        name="NL-twin",
        desired_inbound_uuids="[]",
    )
    await src.add("system_settings", id=1, key="PRICE_30_DAYS", value="99999")  # .env wins
    await src.add("system_settings", id=2, key="RESET_DEVICES_ON_RENEWAL", value="false")
    base = {
        "auth_type": "telegram",
        "language": "ru",
        "status": "active",
        "created_at": T0 - 100 * d,
        "updated_at": T0 - 2 * d,
    }
    users = [
        # 1: paid, balance, referral code, linked by users.remnawave_id
        {
            **base,
            "id": 1,
            "telegram_id": 1001,
            "username": "alice",
            "first_name": "Alice",
            "last_name": "A",
            "balance_kopeks": 15000,
            "has_had_paid_subscription": True,
            "has_made_first_topup": True,
            "referral_code": "refAAAA1111",
            "remnawave_id": 501,
            "last_activity": T0 - h,
        },
        # 2: en, trial, invited by 1, linked by short uuid only
        {
            **base,
            "id": 2,
            "telegram_id": 1002,
            "language": "en",
            "balance_kopeks": 0,
            "referred_by_id": 1,
            "referral_code": "refBBBB2222",
        },
        # 3: blocked the bot, linked by telegram id; panel expireAt later than end_date
        {**base, "id": 3, "telegram_id": 1003, "status": "blocked", "balance_kopeks": 500, "language": "uk"},
        # 4: deleted, nothing → skipped
        {**base, "id": 4, "telegram_id": 1004, "status": "deleted", "balance_kopeks": 0},
        # 5: deleted with money → kept
        {**base, "id": 5, "telegram_id": 1005, "status": "deleted", "balance_kopeks": 300},
        # 6: email-only
        {**base, "id": 6, "telegram_id": None, "auth_type": "email", "balance_kopeks": 0},
        # 7: IP Guard block
        {**base, "id": 7, "telegram_id": 1007, "remnawave_id": 507, "balance_kopeks": 0},
        # 8: trial, left the channel
        {**base, "id": 8, "telegram_id": 1008, "remnawave_id": 508},
        # 9: disabled by the panel admin
        {**base, "id": 9, "telegram_id": 1009, "remnawave_id": 509},
        # 10: conflict: remnawave_id points at a panel user with another telegramId
        {**base, "id": 10, "telegram_id": 1010, "remnawave_id": 510},
        # 11: LTE block: twin in the panel
        {**base, "id": 11, "telegram_id": 1011, "remnawave_id": 511, "has_had_paid_subscription": True},
        # 12: personal discount + restrictions + pending campaign
        {
            **base,
            "id": 12,
            "telegram_id": 1012,
            "promo_offer_discount_percent": 15,
            "promo_offer_discount_source": "offer",
            "promo_offer_discount_expires_at": T0 + 5 * d,
            "restriction_topup": True,
            "restriction_reason": "abuse",
            "pending_campaign_slug": "promoX",
        },
    ]
    await src.many("users", users)
    sub = {
        "status": "active",
        "is_trial": False,
        "start_date": T0 - 30 * d,
        "traffic_limit_gb": 0,
        "device_limit": 5,
        "created_at": T0 - 30 * d,
        "is_daily_paused": False,
    }
    rows = [
        {
            **sub,
            "id": 101,
            "user_id": 1,
            "end_date": T0 + 20 * d,
            "connected_squads": [SQ_NL, SQ_DE],
            "device_limit": 7,
            "remnawave_short_uuid": "short501",
            "last_revoke_at": T0 - 3 * d,
            "autopay_enabled": True,
        },
        {
            **sub,
            "id": 102,
            "user_id": 2,
            "is_trial": True,
            "status": "trial",
            "end_date": T0 + 2 * d,
            "connected_squads": [SQ_NL],
            "subscription_url": "https://sub.example.com/short502",
        },
        {**sub, "id": 103, "user_id": 3, "end_date": T0 + 10 * d, "connected_squads": [SQ_NL]},
        {**sub, "id": 106, "user_id": 6, "end_date": T0 + 9 * d, "connected_squads": [SQ_NL]},
        {**sub, "id": 107, "user_id": 7, "end_date": T0 + 15 * d, "connected_squads": [SQ_NL]},
        {
            **sub,
            "id": 108,
            "user_id": 8,
            "is_trial": True,
            "end_date": T0 + 1 * d,
            "connected_squads": [SQ_NL],
        },
        {**sub, "id": 109, "user_id": 9, "end_date": T0 + 4 * d, "connected_squads": []},
        {**sub, "id": 110, "user_id": 10, "end_date": T0 + 4 * d, "connected_squads": [SQ_NL]},
        {**sub, "id": 111, "user_id": 11, "end_date": T0 + 8 * d, "connected_squads": [SQ_NL]},
        {
            **sub,
            "id": 112,
            "user_id": 12,
            "status": "expired",
            "end_date": T0 - 40 * d,
            "connected_squads": [SQ_NL],
        },
    ]
    await src.many("subscriptions", [{**r, "remnawave_short_id": f"sid{r['id']}"} for r in rows])
    await src.add("subscription_servers", id=1, subscription_id=101, server_squad_id=2)
    await src.many(
        "wlq_subjects",
        [
            {"id": 1, "panel_user_id": 501, "subscription_id": 101, "kind": "bot"},
            {"id": 2, "panel_user_id": 511, "subscription_id": 111, "kind": "bot"},
        ],
    )
    await src.add(
        "ip_guard_blocks",
        id=1,
        status="active",
        panel_user_id=507,
        subscription_id=107,
        owner_kind="bot_sub",
        ip_count=30,
        live_ip_count=12,
        subnet_count=11,
        ips="[]",
        blocked_at=T0 - 5 * h,
        end_date_at_block=T0 + 15 * d,
        panel_expire_at_block=T0 + 16 * d,
        credited_seconds=60,
    )
    await src.add("user_channel_subscriptions", id=1, telegram_id=1008, channel_id="-100", is_member=False)
    sc.panel = [
        panel_user(501, tg=1001, devices=7, squads=[SQ_NL, SQ_DE], expire=T0 + 20 * d),
        panel_user(502, tg=1002, expire=T0 + 2 * d, tag="TRIAL"),
        panel_user(503, tg=1003, expire=T0 + 12 * d),  # renewed past the bot (+2 days)
        panel_user(507, tg=1007, status="DISABLED", expire=T0 + 15 * d),
        panel_user(508, tg=1008, status="DISABLED", expire=T0 + 1 * d, tag="TRIAL"),
        panel_user(509, tg=1009, status="DISABLED", expire=T0 + 4 * d, squads=[SQ_DE]),
        panel_user(510, tg=9999, expire=T0 + 4 * d),  # another person's account
        panel_user(511, tg=1011, squads=[SQ_TWIN], expire=T0 + 8 * d),
        panel_user(512, tg=1012, expire=T0 - 40 * d, status="EXPIRED"),
        panel_user(600, tg=None, expire=T0 + 30 * d, squads=[SQ_TWIN]),  # unowned: panel-only
        panel_user(601, tg=None, expire=T0 + 30 * d),  # service account, skipped by the owner
    ]

    # Payments.
    rolly = {"currency": "RUB", "description": "Пополнение"}
    await src.many(
        "rollypay_payments",
        [
            {
                **rolly,
                "id": 1,
                "user_id": 1,
                "order_id": "rp1001_aaaaaa",
                "rollypay_payment_id": "RP-1",
                "amount_kopeks": 17900,
                "status": "paid",
                "is_paid": True,
                "paid_at": T0 - 10 * d,
                "created_at": T0 - 10 * d,
            },
            {
                **rolly,
                "id": 2,
                "user_id": 2,
                "order_id": "rp1002_bbbbbb",
                "rollypay_payment_id": "RP-2",
                "amount_kopeks": 20000,
                "status": "pending",
                "is_paid": False,
                "created_at": T0 - 2 * h,
            },
            {
                **rolly,
                "id": 3,
                "user_id": 3,
                "order_id": "rp1003_cccccc",
                "rollypay_payment_id": "RP-3",
                "amount_kopeks": 30000,
                "status": "expired",
                "is_paid": False,
                "created_at": T0 - 10 * h,
            },
            {
                **rolly,
                "id": 4,
                "user_id": 3,
                "order_id": "rp1003_dddddd",
                "rollypay_payment_id": "RP-4",
                "amount_kopeks": 30000,
                "status": "expired",
                "is_paid": False,
                "created_at": T0 - 5 * d,
            },
            {
                **rolly,
                "id": 5,
                "user_id": 1,
                "order_id": "rp1001_eeeeee",
                "rollypay_payment_id": "RP-5",
                "amount_kopeks": 17900,
                "status": "canceled",
                "is_paid": False,
                "created_at": T0 - 4 * d,
            },
            {
                **rolly,
                "id": 6,
                "user_id": None,
                "order_id": "rp1004_ffffff",
                "rollypay_payment_id": "RP-6",
                "amount_kopeks": 17900,
                "status": "paid",
                "is_paid": True,
                "paid_at": T0 - 50 * d,
                "created_at": T0 - 50 * d,
            },
        ],
    )
    await src.many(
        "transactions",
        [
            {
                "id": 1,
                "user_id": 1,
                "type": "deposit",
                "amount_kopeks": 50000,
                "description": "CryptoBot",
                "payment_method": "cryptobot",
                "is_completed": True,
                "created_at": T0 - 20 * d,
                "completed_at": T0 - 20 * d,
            },
            {
                "id": 2,
                "user_id": 1,
                "type": "deposit",
                "amount_kopeks": 10000,
                "description": "Telegram Stars",
                "payment_method": "telegram_stars",
                "external_id": "stars-charge-1",
                "is_completed": True,
                "created_at": T0 - 15 * d,
                "completed_at": T0 - 15 * d,
            },
            {
                "id": 3,
                "user_id": 1,
                "type": "deposit",
                "amount_kopeks": 17900,
                "description": "Оплата на сайте (RollyPay), заказ tc_ABC123",
                "payment_method": "rollypay",
                "is_completed": True,
                "created_at": T0 - 7 * d,
            },
            {
                "id": 4,
                "user_id": 1,
                "type": "subscription_payment",
                "amount_kopeks": -17900,
                "description": "Продление «30 дней» через сайт",
                "is_completed": True,
                "created_at": T0 - 7 * d + 4 * timedelta(minutes=1),
            },
            {
                "id": 5,
                "user_id": 1,
                "type": "subscription_payment",
                "amount_kopeks": -17900,
                "description": "Покупка подписки",
                "is_completed": True,
                "created_at": T0 - 30 * d,
            },
            {
                "id": 6,
                "user_id": 2,
                "type": "referral_reward",
                "amount_kopeks": 5000,
                "description": "Реферальная награда",
                "is_completed": True,
                "created_at": T0 - 60 * d,
            },
        ],
    )
    await src.many(
        "cryptobot_payments",
        [
            {
                "id": 1,
                "user_id": 1,
                "invoice_id": "CB-1",
                "amount": "5.2",
                "asset": "USDT",
                "status": "paid",
                "paid_at": T0 - 20 * d,
                "transaction_id": 1,
                "created_at": T0 - 20 * d,
            },
            {
                "id": 2,
                "user_id": 2,
                "invoice_id": "CB-2",
                "amount": "3.1",
                "asset": "USDT",  # issued in the asset (currency_type=crypto); the payload has the rubles
                "status": "active",
                "payload": "balance_2_30000",
                "created_at": T0 - 3 * h,
            },
            {
                "id": 3,
                "user_id": 2,
                "invoice_id": "CB-3",
                "amount": "3.1",
                "asset": "TON",
                "status": "active",
                "payload": "balance_2_30000",
                "created_at": T0 - 3 * d,
            },
        ],
    )
    await src.add(
        "platega_payments",
        id=1,
        user_id=1,
        correlation_id="PL-1",
        amount_kopeks=49900,
        currency="RUB",
        payment_method_code=2,
        status="CONFIRMED",
        is_paid=True,
        paid_at=T0 - 80 * d,
        created_at=T0 - 80 * d,
    )

    # Promo codes and uses.
    promo = {"is_active": True, "first_purchase_only": False, "current_uses": 1, "max_uses": 100}
    await src.many(
        "promocodes",
        [
            {**promo, "id": 1, "code": "Bonus100", "type": "balance", "balance_bonus_kopeks": 10000},
            {
                **promo,
                "id": 2,
                "code": "DAYS7",
                "type": "subscription_days",
                "subscription_days": 7,
                "max_uses": 0,
            },
            {
                **promo,
                "id": 3,
                "code": "Sale20",
                "type": "discount",
                "balance_bonus_kopeks": 20,
                "subscription_days": 48,
                "valid_until": T0 + 30 * d,
            },
            {
                **promo,
                "id": 4,
                "code": "COMBO",
                "type": "balance_and_days",
                "balance_bonus_kopeks": 5000,
                "subscription_days": 3,
                "first_purchase_only": True,
            },
            {**promo, "id": 5, "code": "GROUP", "type": "promo_group"},
        ],
    )
    await src.many(
        "promocode_uses",
        [
            {"id": 1, "promocode_id": 1, "user_id": 1, "used_at": T0 - 9 * d},
            {"id": 2, "promocode_id": 2, "user_id": 2, "used_at": T0 - 8 * d},
            {"id": 3, "promocode_id": 2, "user_id": 3, "used_at": T0 - 7 * d},
            {"id": 4, "promocode_id": 1, "user_id": 4, "used_at": T0 - 6 * d},
        ],  # user 4 is not imported
    )

    # Campaigns.
    await src.many(
        "advertising_campaigns",
        [
            {
                "id": 1,
                "name": "Telegram ads",
                "start_parameter": "tgads_oct",
                "bonus_type": "balance",
                "balance_bonus_kopeks": 5000,
                "is_active": True,
                "partner_user_id": 1,
                "created_at": T0 - 90 * d,
            },
            {
                "id": 2,
                "name": "Promo X",
                "start_parameter": "promoX",
                "bonus_type": "none",
                "is_active": False,
                "created_at": T0 - 90 * d,
            },
        ],
    )
    await src.many(
        "advertising_campaign_registrations",
        [
            {"id": 1, "campaign_id": 2, "user_id": 3, "bonus_type": "none", "created_at": T0 - 50 * d},
            {"id": 2, "campaign_id": 1, "user_id": 3, "bonus_type": "balance", "created_at": T0 - 60 * d},
            {"id": 3, "campaign_id": 1, "user_id": 2, "bonus_type": "balance", "created_at": T0 - 55 * d},
        ],
    )

    # Referral earnings: money (legacy) + a days marker (left to the referral-days importer).
    await src.many(
        "referral_earnings",
        [
            {
                "id": 1,
                "user_id": 1,
                "referral_id": 2,
                "amount_kopeks": 5000,
                "reason": "referral_first_topup",
                "created_at": T0 - 60 * d,
            },
            {
                "id": 2,
                "user_id": 1,
                "referral_id": 2,
                "amount_kopeks": 0,
                "reason": "referral_days_inviter",
                "created_at": T0 - 59 * d,
            },
        ],
    )

    # Reminders already sent.
    await src.many(
        "sent_notifications",
        [
            {
                "id": 1,
                "user_id": 2,
                "subscription_id": 102,
                "notification_type": "expiring",
                "days_before": 3,
                "created_at": T0 - 1 * d,
            },
            {
                "id": 2,
                "user_id": 12,
                "subscription_id": 112,
                "notification_type": "expired",
                "created_at": T0 - 39 * d,
            },
            {
                "id": 3,
                "user_id": 8,
                "subscription_id": 108,
                "notification_type": "trial_expiring",
                "created_at": T0 - 2 * h,
            },
        ],
    )
    return sc


async def seed_volume(
    src: Src, *, users: int = 2522, subs: int = 2109, txs: int = 1352, seed: int = 7
) -> list[PanelUser]:
    """Prod-like volumes; every subscription has a panel account (+22 panel-only)."""
    rnd = random.Random(seed)
    d = timedelta(days=1)
    urows, srows, prows, trows, panel = [], [], [], [], []
    for uid in range(1, users + 1):
        urows.append(
            {
                "id": uid,
                "telegram_id": 10_000 + uid,
                "auth_type": "telegram",
                "status": "active",
                "language": "ru",
                "balance_kopeks": rnd.choice([0, 0, 0, 100, 1790, 17900, 50000]),
                "created_at": T0 - rnd.randint(1, 300) * d,
                "referred_by_id": (uid - 1) if uid % 7 == 0 else None,
                "referral_code": f"ref{uid:08d}",
                "remnawave_id": 100_000 + uid if uid <= subs else None,
            }
        )
    for i in range(1, subs + 1):
        end = T0 + rnd.randint(-30, 60) * d
        srows.append(
            {
                "id": i,
                "user_id": i,
                "status": "active",
                "is_trial": i % 5 == 0,
                "start_date": T0 - 30 * d,
                "end_date": end,
                "device_limit": 5,
                "connected_squads": [SQ_NL],
                "created_at": T0 - 30 * d,
                "is_daily_paused": False,
                "traffic_limit_gb": 0,
                "remnawave_short_id": f"v{i}",
            }
        )
        panel.append(panel_user(100_000 + i, tg=10_000 + i, expire=end))
    for j in range(22):
        panel.append(panel_user(300_000 + j, tg=None))
    for t in range(1, txs + 1):
        uid = rnd.randint(1, users)
        trows.append(
            {
                "id": t,
                "user_id": uid,
                "type": "deposit",
                "amount_kopeks": 17900,
                "description": "Пополнение",
                "payment_method": "rollypay",
                "is_completed": True,
                "created_at": T0 - rnd.randint(1, 200) * d,
            }
        )
        if t <= 548:
            prows.append(
                {
                    "id": t,
                    "user_id": uid,
                    "order_id": f"rp{uid}_{t:06x}",
                    "rollypay_payment_id": f"RPV-{t}",
                    "amount_kopeks": 17900,
                    "currency": "RUB",
                    "status": "paid",
                    "is_paid": True,
                    "paid_at": T0 - d,
                    "created_at": T0 - 2 * d,
                }
            )
    await src.many("users", urows)
    await src.many("subscriptions", srows)
    await src.many("transactions", trows)
    await src.many("rollypay_payments", prows)
    return panel


SQ_LTE_GROUP = "55555555-5555-4555-8555-555555555555"  # the LTE group's own squad
LTE_NODE = "44444444-4444-4444-8444-444444444444"
GB = 10**9


async def _each(src: Src, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Row by row: a column one row leaves out keeps its database default (``many`` would send NULL)."""
    for row in rows:
        await src.add(table, **row)


async def seed_owner_modules(src: Src) -> None:
    """State of the owner's modules (LTE / IP Guard / referral days) on top of :func:`seed_edge_cases`.

    LTE: one active group on one node; live enforce blocks of panel 511 (sub 111) and of the panel-only
    account 600 — both already in the twin; a shadow-mode block (501, not carried over); exemptions
    (``owner_personal`` of 501, a ``launch_trial`` of the trial 102, a ``launch_trial`` of 103 already paid
    for); a limit, a ``no_block`` period override, an active top-up, sent / pending notices, counters
    of D−1/D.
    IP Guard (besides the active block of 107): a closed block of 501, an unblocked-but-not-restored one of
    503, warnings, and the white list ``501, 999;abc`` in ``system_settings``.
    Referral days: granted / skipped markers (deferred and expired), a money pair with a days marker, a pair
    without markers (the live tail).
    """
    d = timedelta(days=1)
    h = timedelta(hours=1)
    # ---- LTE
    await src.add(
        "wlq_groups",
        id=1,
        slug="lte",
        name="LTE",
        name_en="LTE",
        state="active",
        enforce_enabled=True,
        margin_bytes=0,
        margin_percent=5,
        created_at=T0 - 90 * d,
    )
    await _each(
        src,
        "wlq_group_limits",
        [
            {"group_id": 1, "scope": "default", "limit_bytes": 10 * GB},
            {"group_id": 1, "scope": "trial", "limit_bytes": 1 * GB},
        ],
    )
    await src.add(
        "wlq_group_nodes",
        id=1,
        group_id=1,
        node_uuid=LTE_NODE,
        node_name="lte-1",
        state="active",
        counted_from=T0 - 90 * d,
        created_at=T0 - 90 * d,
    )
    await src.conn.execute("UPDATE wlq_squads SET group_id = 1 WHERE id = 1")
    await src.add(
        "wlq_squads",
        id=2,
        kind="group",
        group_id=1,
        panel_uuid=SQ_LTE_GROUP,
        name="LTE-group",
        desired_inbound_uuids="[]",
    )
    await _each(
        src,
        "wlq_subjects",
        [
            {"id": 3, "panel_user_id": 600, "subscription_id": None, "kind": "panel"},
            {"id": 4, "panel_user_id": 502, "subscription_id": 102, "kind": "bot"},
            {"id": 5, "panel_user_id": 503, "subscription_id": 103, "kind": "bot"},
        ],
    )
    anchor = {"anchor_kind": "paid", "anchor_source": "payment", "anchor_day": 1, "series_state": "open"}
    await _each(
        src,
        "wlq_anchors",
        [
            {**anchor, "subject_id": 1, "anchor_at": T0 - 40 * d, "streak_started_at": T0 - 40 * d},
            {
                **anchor,
                "subject_id": 2,
                "anchor_at": T0 - 20 * d,
                "streak_started_at": T0 - 80 * d,
                "coverage_end": T0 + 8 * d,
            },
            {
                **anchor,
                "subject_id": 3,
                "anchor_kind": "manual",
                "anchor_at": T0 - 10 * d,
                "streak_started_at": T0 - 10 * d,
                "review_state": "pending",
                "review_reason": "panel only",
            },
            {
                **anchor,
                "subject_id": 4,
                "anchor_kind": "trial",
                "anchor_at": T0 - 1 * d,
                "streak_started_at": T0 - 1 * d,
                "is_trial": True,
            },
        ],
    )
    per = {"period_index": 0, "state": "open"}
    await _each(
        src,
        "wlq_periods",
        [
            {
                **per,
                "id": 10,
                "subject_id": 2,
                "anchor_at": T0 - 50 * d,
                "starts_at": T0 - 50 * d,
                "planned_end_at": T0 - 20 * d,
                "ended_at": T0 - 20 * d,
                "state": "closed",
            },
            {
                **per,
                "id": 11,
                "subject_id": 2,
                "anchor_at": T0 - 20 * d,
                "period_index": 1,
                "starts_at": T0 - 20 * d,
                "planned_end_at": T0 + 10 * d,
            },
            {
                **per,
                "id": 12,
                "subject_id": 3,
                "anchor_at": T0 - 10 * d,
                "starts_at": T0 - 10 * d,
                "planned_end_at": T0 + 20 * d,
                "start_estimated": True,
            },
            {
                **per,
                "id": 13,
                "subject_id": 1,
                "anchor_at": T0 - 40 * d,
                "period_index": 1,
                "starts_at": T0 - 10 * d,
                "planned_end_at": T0 + 20 * d,
                "state": "deferred",
            },
        ],
    )
    await _each(
        src,
        "wlq_period_usage",
        [
            {"period_id": 10, "group_id": 1, "used_bytes": 9 * GB},
            {
                "period_id": 11,
                "group_id": 1,
                "used_bytes": 10_200_000_000,
                "after_block_bytes": 100_000_000,
                "estimated_bytes": 5_000_000,
                "gap_estimated_bytes": 1_000_000,
                "last_delta_at": T0 - h,
            },
            {"period_id": 12, "group_id": 1, "used_bytes": 12 * GB},
            {"period_id": 13, "group_id": 1, "used_bytes": 3 * GB},
        ],
    )
    blk = {"group_id": 1, "mode": "enforce", "reason": "quota", "limit_bytes_at_block": 10 * GB}
    await _each(
        src,
        "wlq_blocks",
        [
            {
                **blk,
                "id": 1,
                "subject_id": 2,
                "panel_user_id": 511,
                "period_id": 11,
                "status": "active",
                "used_bytes_at_block": 10_000_000_001,
                "created_at": T0 - 2 * d,
                "applied_at": T0 - 2 * d,
            },
            {
                **blk,
                "id": 2,
                "subject_id": 3,
                "panel_user_id": 600,
                "period_id": 12,
                "status": "pending_apply",
                "used_bytes_at_block": 10 * GB,
                "created_at": T0 - h,
            },
            {
                **blk,
                "id": 3,
                "subject_id": 1,
                "panel_user_id": 501,
                "period_id": 13,
                "status": "active",
                "mode": "shadow",
                "used_bytes_at_block": 3 * GB,
            },
            {
                **blk,
                "id": 4,
                "subject_id": 2,
                "panel_user_id": 511,
                "period_id": 10,
                "status": "released",
                "release_reason": "reset",
                "used_bytes_at_block": 10 * GB,
                "released_at": T0 - 20 * d,
            },
        ],
    )
    await _each(
        src,
        "wlq_exemptions",
        [
            {
                "id": 1,
                "subject_id": 1,
                "kind": "owner_personal",
                "reason": "владелец",
                "created_at": T0 - 90 * d,
            },
            {"id": 2, "subject_id": 4, "kind": "launch_trial", "reason": "триал на запуске"},
            {"id": 3, "subject_id": 5, "kind": "launch_trial", "reason": "триал на запуске"},
            {"id": 4, "subject_id": 1, "kind": "manual", "reason": "снято", "revoked_at": T0 - 5 * d},
        ],
    )
    # A payment Bedolaga's cycle has not processed yet: the import still converts the launch_trial of 103.
    await src.add(
        "wlq_billing_events",
        id=1,
        occurred_at=T0 - 3 * h,
        panel_user_id=503,
        subscription_id=103,
        kind="paid",
        classified_by="rule",
    )
    await _each(
        src,
        "wlq_user_limits",
        [
            {"id": 1, "subject_id": 1, "group_id": 1, "limit_bytes": 20 * GB, "reason": "VIP"},
            {
                "id": 2,
                "subject_id": 2,
                "group_id": 1,
                "limit_bytes": 50 * GB,
                "reason": "old",
                "valid_until": T0 - d,
            },
        ],
    )
    await src.add("wlq_period_overrides", period_id=13, group_id=1, no_block=True, reason="поездка")
    await src.add(
        "wlq_topups",
        id=1,
        subject_id=2,
        group_id=1,
        period_id=11,
        bytes=5 * GB,
        price_kopeks=9900,
        source="purchase",
        status="active",
        idempotency_key="k1",
        created_at=T0 - d,
    )
    note = {"subject_id": 2, "group_id": 1, "state": "sent"}
    await _each(
        src,
        "wlq_notifications",
        [
            {**note, "id": 1, "period_id": 11, "kind": "warn", "threshold": 80, "sent_at": T0 - 3 * d},
            {**note, "id": 2, "period_id": 11, "kind": "exhausted", "sent_at": T0 - 2 * d},
            {**note, "id": 3, "subject_id": 1, "period_id": 13, "kind": "warn", "state": "pending"},
            {**note, "id": 4, "period_id": 10, "kind": "warn", "sent_at": T0 - 30 * d},
        ],
    )
    cnt = {
        "node_uuid": LTE_NODE,
        "panel_user_id": 511,
        "first_seen_at": T0 - 3 * d,
        "last_seen_at": T0 - h,
        "last_cycle_id": 1,
    }
    await _each(
        src,
        "wlq_counters",
        [
            {
                **cnt,
                "usage_date": (T0 - d).date(),
                "total_bytes": 500,
                "baseline_bytes": 100,
                "accounted_bytes": 400,
            },
            # broken invariant (baseline + accounted ≠ total + carry): reported, not imported
            {**cnt, "usage_date": T0.date(), "total_bytes": 300, "accounted_bytes": 250},
            {**cnt, "usage_date": (T0 - 5 * d).date(), "total_bytes": 9, "accounted_bytes": 9},
        ],
    )
    await src.add(
        "wlq_node_status",
        node_uuid=LTE_NODE,
        name="lte-1",
        is_connected=True,
        is_disabled=False,
        first_read_at=T0 - 90 * d,
        last_ok_read_at=T0 - h,
        last_ok_read_date=T0.date(),
        gap_tail_cycles=2,
        xray_uptime_s=3600,
    )

    # ---- IP Guard
    ipg = {"ip_count": 3, "live_ip_count": 1, "subnet_count": 2, "owner_kind": "bot_sub"}
    ips = [
        {
            "key": f"10.0.0.{i}",
            "raw": [f"10.0.0.{i}"],
            "nodes": {LTE_NODE: T0.isoformat()},
            "seen_at": (T0 - i * h).isoformat(),
        }
        for i in range(1, 61)
    ]
    await _each(
        src,
        "ip_guard_blocks",
        [
            {
                **ipg,
                "id": 2,
                "status": "closed",
                "panel_user_id": 501,
                "subscription_id": 101,
                "ips": ips[:3],
                "blocked_at": T0 - 60 * d,
                "closed_at": T0 - 59 * d,
                "closed_by": 1001,
            },
            {
                **ipg,
                "id": 3,
                "status": "unblocked",
                "panel_user_id": 503,
                "subscription_id": 103,
                "ips": ips,
                "blocked_at": T0 - 3 * d,
                "unblocked_at": T0 - 2 * h,
                "unblock_mode": "plain",
                "unblock_outcome": "restored",
                "new_end_date": T0 + 12 * d,
                "panel_restored": False,
            },
        ],
    )
    await src.conn.execute(
        "UPDATE ip_guard_blocks SET ips = $1::jsonb, confirmed_by = 1001, ip_count = 60 WHERE id = 1",
        json.dumps(ips),
    )
    warn = {"kind": "warn", "ip_count": 4, "live_ip_count": 2, "subnet_count": 3, "ips": "[]"}
    await _each(
        src,
        "ip_guard_warnings",
        [
            {**warn, "id": 1, "panel_user_id": 501, "created_at": T0 - 10 * d},
            {**warn, "id": 2, "panel_user_id": 501, "created_at": T0 - 4 * d},
            {**warn, "id": 3, "panel_user_id": 999, "created_at": T0 - 4 * d},
        ],
    )
    await src.add("system_settings", id=3, key="IP_GUARD_WHITELIST_PANEL_USER_IDS", value="501, 999;abc")

    # ---- referral days (1 → 2 already has money + an inviter marker 59 days ago)
    for uid, ref in ((3, 1), (5, 3), (7, 1), (8, 11), (9, 11)):
        await src.conn.execute("UPDATE users SET referred_by_id = $2 WHERE id = $1", uid, ref)
    mk = {"amount_kopeks": 0}
    await _each(
        src,
        "referral_earnings",
        [
            {**mk, "id": 10, "user_id": 1, "referral_id": 3, "reason": "referral_days_inviter"},
            {**mk, "id": 11, "user_id": 1, "referral_id": 3, "reason": "referral_days_invitee"},
            {**mk, "id": 12, "user_id": 1, "referral_id": 7, "reason": "referral_days_inviter_skipped"},
            {**mk, "id": 13, "user_id": 11, "referral_id": 8, "reason": "referral_days_invitee_skipped"},
            {**mk, "id": 14, "user_id": 3, "referral_id": 5, "reason": "referral_days_inviter"},
            {**mk, "id": 16, "user_id": 1, "referral_id": 2, "reason": "referral_days_inviter_skipped"},
        ],
    )
    for rid, ago in ((10, 5 * d), (11, 5 * d), (12, 2 * d), (13, 10 * d), (14, d), (16, 60 * d)):
        await src.conn.execute("UPDATE referral_earnings SET created_at = $2 WHERE id = $1", rid, T0 - ago)
