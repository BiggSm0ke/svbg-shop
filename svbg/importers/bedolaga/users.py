"""Users (06 §2.1, §2.3): **Bedolaga ids are kept** (explicit insert; the counter is moved above ``MAX(id)``
after the run) — the referral cut-off, ``referred_by_id``, legacy Stars payloads ``balance_<users.id>_…`` and
the module tables all point at them.

* ``deleted`` users are skipped unless they have a subscription, a balance or a payment row (money first);
* ``blocked`` / kept ``deleted`` → ``bot_blocked_at``; language ``ru``/``en`` (anything else → ``ru``);
* every imported user counts as having passed the entry captcha (``captcha_passed_at``): they already use
  the bot;
* ``trial_grants(source='import')`` for **everyone with a row in ``subscriptions``** (С12, R10: no second
  trial);
* fields without a column yet (first payment / top-up time, personal discount, restrictions, notification
  flags) are kept in ``legacy_id_map.data`` until the integration adds the columns. Meanwhile a live
  restriction (``restriction_topup`` / ``restriction_subscription``, an anti-abuse measure) **blocks the
  cut-over** (``user_restricted``) until the owner applies it by hand and acknowledges it in
  ``import_overrides.ack_restricted_user_ids``; a live personal discount is reported
  (``personal_discount_not_applied``) — the bot would not honour it after T0;
* a target row with the same id or Telegram id that this importer did not write is a **conflict** (the user
and
  everything that hangs on it are skipped and reported — the cut-over gate stays red).

Secrets (``vless_uuid``, passwords, tokens) are never selected (source.FORBIDDEN_COLUMNS).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from svbg.core.tables import users
from svbg.importers.bedolaga.plan import chunks
from svbg.subscriptions.tables import trial_grants

if TYPE_CHECKING:
    from svbg.importers.bedolaga.plan import Ctx

__all__ = ["COLUMNS", "check", "run"]

COLUMNS: Final = (
    "id",
    "telegram_id",
    "auth_type",
    "username",
    "first_name",
    "last_name",
    "status",
    "language",
    "balance_kopeks",
    "has_had_paid_subscription",
    "referred_by_id",
    "referral_code",
    "created_at",
    "updated_at",
    "last_activity",
    "remnawave_id",
    "promo_offer_discount_percent",
    "promo_offer_discount_source",
    "promo_offer_discount_expires_at",
    "has_made_first_topup",
    "notification_settings",
    "restriction_topup",
    "restriction_subscription",
    "restriction_reason",
    "pending_campaign_slug",
)
_PAYMENT_TABLES: Final = ("rollypay_payments", "cryptobot_payments", "platega_payments")
_PROFILE: Final = ("username", "first_name", "language", "last_seen_at", "bot_blocked_at")


def _lang(value: Any) -> str:
    return "en" if str(value or "").lower().startswith("en") else "ru"


def _name(row: dict[str, Any]) -> str | None:
    full = " ".join(str(p).strip() for p in (row["first_name"], row["last_name"]) if p and str(p).strip())
    return full[:255] or None


async def _firsts(ctx: Ctx) -> tuple[dict[int, datetime], dict[int, datetime]]:
    """First completed top-up and first subscription payment per user (``transactions``)."""
    if not await ctx.src.has("transactions"):
        return {}, {}
    rows = await ctx.src.fetch(
        "SELECT user_id, type, MIN(COALESCE(completed_at, created_at)) AS at FROM public.transactions "
        "WHERE COALESCE(is_completed, true) GROUP BY user_id, type"
    )
    topup: dict[int, datetime] = {}
    paid: dict[int, datetime] = {}
    for r in rows:
        if r["at"] is None:
            continue
        kind = str(r["type"] or "").lower()
        if kind == "deposit":
            topup[int(r["user_id"])] = r["at"]
        elif kind == "subscription_payment":
            paid[int(r["user_id"])] = r["at"]
    return paid, topup


async def run(ctx: Ctx) -> None:
    rep, t0 = ctx.report, ctx.t0
    rows = await ctx.src.rows("users", COLUMNS)
    subs = await ctx.src.rows(
        "subscriptions", ["id", "user_id", "created_at", "start_date", "end_date", "is_trial"]
    )
    for s in subs:
        ctx.source_subs[int(s["id"])] = s
    ctx.source_max["users"] = max((int(r["id"]) for r in rows), default=0)
    ctx.source_max["subscriptions"] = max(ctx.source_subs, default=0)
    first_sub: dict[int, tuple[datetime | None, int]] = {}
    for s in sorted(subs, key=lambda s: (s["created_at"] or s["start_date"] or t0, s["id"])):
        first_sub.setdefault(int(s["user_id"]), (s["created_at"] or s["start_date"], int(s["id"])))
    payers: set[int] = set()
    for table in _PAYMENT_TABLES:
        if await ctx.src.has(table):
            payers.update(
                int(r[0])
                for r in await ctx.src.fetch(
                    f"SELECT DISTINCT user_id FROM public.{table} WHERE user_id IS NOT NULL"
                )
            )
    paid_at, topup_at = await _firsts(ctx)

    ids = [int(r["id"]) for r in rows]
    tgs = [int(r["telegram_id"]) for r in rows if r["telegram_id"] is not None]
    existing_ids: set[int] = set()
    by_tg: dict[int, int] = {}
    for chunk in chunks(ids, 5000):
        existing_ids.update(
            int(x)
            for x in (await ctx.conn.execute(sa.select(users.c.id).where(users.c.id.in_(chunk)))).scalars()
        )
    for chunk in chunks(tgs, 5000):
        for uid, tg in (
            await ctx.conn.execute(
                sa.select(users.c.id, users.c.telegram_id).where(users.c.telegram_id.in_(chunk))
            )
        ).all():
            by_tg[int(tg)] = int(uid)
    mapped = await ctx.mapped("user")

    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    remember: list[tuple[int, int, dict[str, Any]]] = []
    for r in rows:
        ctx.source_users[int(r["id"])] = r
    for r in rows:
        uid = int(r["id"])
        status = str(r["status"] or "active").lower()
        balance = int(r["balance_kopeks"] or 0)
        rep.inc("users", "source")
        if status == "deleted" and uid not in first_sub and balance == 0 and uid not in payers:
            rep.inc("users", "skipped_deleted")
            continue
        tg = int(r["telegram_id"]) if r["telegram_id"] is not None else None
        ours = str(uid) in mapped
        if uid in existing_ids and not ours:
            rep.issue("user_id_taken", user_id=uid, telegram_id=tg)
            continue
        if tg is not None and tg in by_tg and by_tg[tg] != uid:
            rep.issue("user_telegram_taken", user_id=uid, telegram_id=tg, target_user_id=by_tg[tg])
            continue
        blocked = status in ("blocked", "deleted")
        values: dict[str, Any] = {
            "username": (str(r["username"])[:255] if r["username"] else None),
            "first_name": _name(r),
            "language": _lang(r["language"]),
            "last_seen_at": r["last_activity"],
            "bot_blocked_at": (r["updated_at"] or r["created_at"] or t0) if blocked else None,
        }
        if ours or uid in existing_ids:
            updates.append({"b_id": uid, **values})
            rep.inc("users", "updated")
        else:
            inserts.append(
                {
                    "id": uid,
                    "telegram_id": tg,
                    "created_at": r["created_at"] or t0,
                    "captcha_passed_at": t0,
                    **values,
                }
            )
            rep.inc("users", "created")
        if tg is None:
            rep.inc("users", "without_telegram")
        if blocked:
            rep.inc("users", "blocked")
        remember.append((uid, uid, _extras(ctx, r, paid_at, topup_at)))
        ctx.users[uid] = r
    for chunk in chunks(inserts, 1000):
        await ctx.conn.execute(sa.insert(users).values(chunk))
    if updates:
        await ctx.conn.execute(
            sa.update(users)
            .where(users.c.id == sa.bindparam("b_id"))
            .values(
                {
                    **{k: sa.bindparam(k) for k in _PROFILE},
                    "captcha_passed_at": sa.func.coalesce(users.c.captcha_passed_at, sa.func.now()),
                }
            ),
            updates,
        )
    await ctx.remember("user", remember)

    grants = []
    for uid, (at, sid) in first_sub.items():
        row = ctx.users.get(uid)
        if row is None:
            continue
        grants.append(
            {
                "user_id": uid,
                "telegram_id": row["telegram_id"],
                "source": "import",
                "granted_at": at or row["created_at"] or t0,
                "_sid": sid,
            }
        )
    for chunk in chunks(grants, 1000):
        res = await ctx.conn.execute(
            pg_insert(trial_grants)
            .values([{k: v for k, v in g.items() if k != "_sid"} for g in chunk])
            .on_conflict_do_nothing()
            .returning(trial_grants.c.id)
        )
        rep.inc("users", "trial_grants", len(res.all()))


def _extras(
    ctx: Ctx, r: dict[str, Any], paid_at: dict[int, datetime], topup_at: dict[int, datetime]
) -> dict[str, Any]:
    uid = int(r["id"])
    out: dict[str, Any] = {"status": str(r["status"] or "active"), "auth_type": r["auth_type"]}
    created = r["created_at"] or ctx.t0
    if r["has_had_paid_subscription"]:
        at = paid_at.get(uid)
        out["first_paid_at"] = (at or created).isoformat()
        out["first_paid_estimated"] = at is None
    if r["has_made_first_topup"]:
        at = topup_at.get(uid)
        out["first_topup_at"] = (at or created).isoformat()
        out["first_topup_estimated"] = at is None
    pct, until = int(r["promo_offer_discount_percent"] or 0), r["promo_offer_discount_expires_at"]
    if pct > 0 and until is not None and until > ctx.t0:
        out["personal_discount"] = {
            "pct": pct,
            "source": r["promo_offer_discount_source"],
            "until": until.isoformat(),
        }
        ctx.report.inc("users", "personal_discounts")
        ctx.report.issue("personal_discount_not_applied", user_id=uid, pct=pct, until=until)
    if r["restriction_topup"] or r["restriction_subscription"]:
        out["restrictions"] = {
            "topup": bool(r["restriction_topup"]),
            "subscription": bool(r["restriction_subscription"]),
            "reason": r["restriction_reason"],
        }
        acked = uid in ctx.overrides.ack_restricted_user_ids
        ctx.report.issue(
            "user_restricted_acknowledged" if acked else "user_restricted",
            user_id=uid,
            topup=bool(r["restriction_topup"]),
            subscription=bool(r["restriction_subscription"]),
            reason=r["restriction_reason"],
        )
    if r["notification_settings"] is not None:
        raw = r["notification_settings"]
        try:
            out["notification_settings"] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            out["notification_settings"] = raw
    if r["pending_campaign_slug"]:
        out["pending_campaign_slug"] = r["pending_campaign_slug"]
    if r["referral_code"]:
        out["referral_code"] = r["referral_code"]
    return out


async def check(ctx: Ctx) -> None:
    """С12: every imported user that has a Bedolaga subscription has ``trial_grants``."""
    with_sub = sorted({int(s["user_id"]) for s in ctx.source_subs.values()} & set(ctx.users))
    have: set[int] = set()
    for chunk in chunks(with_sub, 5000):
        have.update(
            int(x)
            for x in (
                await ctx.conn.execute(
                    sa.select(trial_grants.c.user_id).where(trial_grants.c.user_id.in_(chunk))
                )
            ).scalars()
        )
    missing = [u for u in with_sub if u not in have]
    ctx.report.check("C12", not missing, expected=len(with_sub), present=len(have), missing=missing[:50])
    present = 0
    for chunk in chunks(list(ctx.users), 5000):
        present += int(await ctx.conn.scalar(sa.select(sa.func.count()).where(users.c.id.in_(chunk))) or 0)
    rep = ctx.report
    rep.part(
        "C1",
        "users",
        expected=rep.get("users", "source") - rep.get("users", "skipped_deleted"),
        present=present,
    )
