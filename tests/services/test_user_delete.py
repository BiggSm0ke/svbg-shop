"""«🗑 Удалить полностью»: every row tied to the user goes, the panel first, a fresh /start is a new user."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiogram.types import User as TgUser

from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.services.user_delete import AUDIT_ACTION, FK_POLICY, UserDeleter, user_fk_columns
from svbg.subscriptions.trial import TrialService
from svbg.tg.user.directory import UserDirectory
from tests.dbkit import CountingDatabase, add_user, open_db

OWNER = 1001
ADMIN = 2002  # has «users.delete»
ADMIN_NO = 3003  # admin without «users.delete»
SUPPORT = 4004
TG = 5005  # the user to delete
INVITEE = 6006
INVITER = 8008
CONF_OWNER = 7007  # owner by OWNER_IDS only, stored as a plain user
PANEL_IDS = (777, 778)


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[CountingDatabase]:
    async with open_db(pg_dsn) as database:
        yield database


class FakeApi:
    def __init__(self, fail: Exception | None = None, *, fail_on: int | None = None) -> None:
        self.fail = fail
        self.fail_on = fail_on
        self.deleted: list[int] = []

    async def delete_user(self, id: int, *, lane: Any = None) -> bool:
        if self.fail is not None and (self.fail_on is None or self.fail_on == id):
            raise self.fail
        self.deleted.append(id)
        return True


class FakeChat:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def post(self, kind: str, text: str, *, html: bool = False, **_kw: Any) -> None:
        self.posts.append((kind, text))


class Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def current(self) -> dict[str, Any]:
        return self.values


async def owners() -> frozenset[int]:
    return frozenset({CONF_OWNER})


def make(db: CountingDatabase, api: FakeApi | None = None) -> tuple[UserDeleter, FakeChat, list[Any]]:
    chat = FakeChat()
    forgotten: list[Any] = []
    deleter = UserDeleter(
        db,
        owner_ids=owners,
        panel=(lambda: api) if api is not None else None,
        forget=lambda uid, tg: forgotten.append((uid, tg)),
        admin_chat=chat,
    )
    return deleter, chat, forgotten


async def one(db: CountingDatabase, sql: str, *args: Any) -> Any:
    return (await db.raw(sql, *args))[0][0]


async def staff(db: CountingDatabase) -> None:
    await add_user(db, OWNER, "owner")
    await add_user(db, ADMIN, "admin")
    await db.raw("update users set perms = '[\"users.delete\"]'::jsonb where telegram_id = $1", ADMIN)
    await add_user(db, ADMIN_NO, "admin")
    await db.raw(
        'update users set perms = \'["stats", "users.ban"]\'::jsonb where telegram_id = $1', ADMIN_NO
    )
    await add_user(db, SUPPORT, "support")


async def seed(db: CountingDatabase) -> dict[str, Any]:
    """A user with a row in (almost) every table that can point at them."""
    await staff(db)
    owner = await one(db, "select id from users where telegram_id = $1", OWNER)
    uid = await add_user(db, TG)
    await db.raw(
        "update users set first_name = 'Иван', username = 'ivan', wallet_minor = 150, "
        "captcha_passed_at = now() where id = $1",
        uid,
    )
    inviter = await add_user(db, INVITER)
    invitee = await add_user(db, INVITEE)
    squads = json.dumps(["11111111-1111-4111-8111-111111111111"])
    sid = await one(
        db,
        "insert into subscriptions (user_id, link_state, panel_user_id, paid_until, desired_expire_at, "
        "desired_squads, panel_telegram_id) values ($1, 'linked', $2, now() + interval '10 days', "
        "now() + interval '10 days', $3::jsonb, $4) returning id",
        uid,
        PANEL_IDS[0],
        squads,
        TG,
    )
    old_sid = await one(
        db,
        "insert into subscriptions (user_id, link_state, panel_user_id, paid_until, desired_squads, "
        "is_trial) values ($1, 'closed', $2, now() - interval '30 days', $3::jsonb, true) returning id",
        uid,
        PANEL_IDS[1],
        squads,
    )
    await db.raw(
        "insert into subscription_events (subscription_id, kind, source) values ($1, 'extended', 'bot')", sid
    )
    await db.raw(
        "insert into ip_guard_blocks (subscription_id, reason, user_id) values ($1, 'manual', $2)", sid, uid
    )
    inst = await one(
        db,
        "insert into payment_instances (provider, slug, title, config, webhook_token) "
        "values ('rollypay', 'rollypay', 'RollyPay', 'enc:v1:x', 'enc:v1:y') returning id",
    )
    pid = await one(
        db,
        "insert into payments (instance_id, user_id, status, amount_minor, currency, paid_amount_minor, "
        "paid_at) values ($1, $2, 'paid', 15000, 'RUB', 15000, now()) returning id",
        inst,
        uid,
    )
    await db.raw(
        "insert into payment_events (instance_id, payment_id, body_sha256, outcome, accepted) "
        "values ($1, $2, $3, 'applied', true)",
        inst,
        pid,
        "a" * 64,
    )
    rid = await one(
        db,
        "insert into manual_receipts (payment_id, user_id, amount_minor, currency) "
        "values ($1, $2, 15000, 'RUB') returning id",
        pid,
        uid,
    )
    oid = await one(
        db,
        "insert into orders (user_id, kind, status, currency, total_minor, subscription_id, paid_at, "
        "fulfilled_at) values ($1, 'new', 'fulfilled', 'RUB', 100, $2, now(), now()) returning id",
        uid,
        sid,
    )
    await db.raw(
        "insert into order_items (order_id, position, type, amount_minor) values ($1, 1, 'plan', 100)", oid
    )
    await db.raw(
        "insert into wallet_ledger (user_id, amount_minor, currency, balance_after, reason, ref_type, "
        "ref_id) values ($1, 150, 'RUB', 150, 'topup', 'payment', $2)",
        uid,
        pid,
    )
    await db.raw(
        "insert into trial_grants (user_id, telegram_id, subscription_id) values ($1, $2, $3)",
        uid,
        TG,
        old_sid,
    )
    await db.raw("insert into referrals (referred_user_id, referrer_id) values ($1, $2)", uid, inviter)
    await db.raw("insert into referrals (referred_user_id, referrer_id) values ($1, $2)", invitee, uid)
    await db.raw("insert into referral_codes (user_id, code) values ($1, 'ivan5005')", uid)
    await db.raw(
        "insert into referral_rewards (referred_user_id, user_id, side, kind, status, days, granted_at) "
        "values ($1, $2, 'inviter', 'days', 'granted', 3, now())",
        invitee,
        uid,
    )
    tid = await one(db, "insert into tickets (user_id) values ($1) returning id", uid)
    await db.raw(
        "insert into ticket_messages (ticket_id, dir, user_msg_id, group_msg_id) values ($1, 'in', 1, 2)", tid
    )
    other_ticket = await one(
        db,
        "insert into tickets (user_id, status, closed_at, closed_by) "
        "values ($1, 'closed', now(), $2) returning id",
        invitee,
        uid,
    )
    promo = await one(
        db, "insert into promocodes (code, kind, days, uses) values ('P1', 'days', 3, 1) returning id"
    )
    await db.raw("insert into promo_uses (promo_id, user_id, order_id) values ($1, $2, $3)", promo, uid, oid)
    await db.raw(
        "insert into promo_pending (user_id, promo_id, until) values ($1, $2, now() + interval '1 day')",
        uid,
        promo,
    )
    ad = await one(db, "insert into ad_links (code, title, clicks) values ('ad1', 'Реклама', 5) returning id")
    await db.raw("insert into ad_link_users (user_id, ad_link_id) values ($1, $2)", uid, ad)
    link = await one(
        db,
        "insert into deeplinks (code, title, intent, uses, created_by) "
        "values ('dl1', 'Ссылка', '{}'::jsonb, 1, $1) returning id",
        uid,
    )
    await db.raw(
        "insert into deeplink_hits (link_id, payload, kind, user_id, is_new) "
        "values ($1, 'dl1', 'link', $2, true)",
        link,
        uid,
    )
    await db.raw(
        "insert into deeplink_daily (day, link_key, link_id, hits, users, new_users) "
        "values (current_date, 'dl1', $1, 1, 1, 1)",
        link,
    )
    page = await one(db, "insert into pages (code, kind) values ('rules', 'rules') returning id")
    await db.raw("insert into page_consents (user_id, page_id, version) values ($1, $2, 1)", uid, page)
    await db.raw(
        "insert into ui_state (user_id, chat_id, main_msg_id, pending_intent) "
        "values ($1, $2, 10, '{\"x\": 1}')",
        uid,
        TG,
    )
    await db.raw("insert into user_identities (user_id, provider, subject) values ($1, 'site', 's1')", uid)
    notice = await one(
        db,
        "insert into notification_log (target, kind, anchor, user_id, subscription_id) "
        "values ('user', 'expiring', 'a1', $1, $2) returning id",
        uid,
        sid,
    )
    jobs = [
        ("notify.user", {"id": notice}, None),
        ("panel.update", {"sub_id": sid, "fields": ["expire"]}, f"sub:{sid}"),
        ("referral.notify", {"user_id": uid, "key": "x"}, None),
        ("billing.fulfill", {"order_id": oid}, None),
        ("payments.verify", {"payment_id": pid}, None),
        ("referral.pair", {"referred_user_id": invitee, "referrer_id": uid}, None),
        ("referral.notify", {"user_id": invitee, "key": "y"}, None),  # about somebody else: stays
    ]
    for kind, payload, key in jobs:
        await db.raw(
            "insert into jobs (kind, payload, ordering_key) values ($1, $2::jsonb, $3)",
            kind,
            json.dumps(payload),
            key,
        )
    for entity, old, new in (
        ("user", "42", uid),
        ("subscription", "43", sid),
        ("panel_user", str(PANEL_IDS[0]), sid),
        ("payment", "p1", pid),
        ("order", "o1", oid),
        ("user", "44", invitee),  # stays
    ):
        await db.raw(
            "insert into legacy_id_map (source, entity, old_id, new_id) values ('bedolaga', $1, $2, $3)",
            entity,
            old,
            str(new),
        )
    await db.raw(
        "insert into legacy_transactions (source, legacy_id, user_id, type, amount_minor, currency) "
        "values ('bedolaga', 'lt1', $1, 'deposit', 100, 'RUB')",
        uid,
    )
    await db.raw(
        "insert into channel_members (chat_id, telegram_id, status, is_member, seen_at) "
        "values (-100123, $1, 'member', true, now())",
        TG,
    )
    await db.raw(
        "insert into error_groups (fingerprint, place, title, first_seen, last_seen, episode_started_at) "
        "values ('fp1', 'screen:home', 'Ошибка', now(), now(), now())"
    )
    await db.raw("insert into error_events (fingerprint, ts, user_id) values ('fp1', now(), $1)", uid)
    bid = await one(
        db, "insert into broadcasts (content, created_by) values ('{}'::jsonb, $1) returning id", owner
    )
    await db.raw(
        "insert into broadcast_msgs (broadcast_id, user_id, chat_id, msg_id, delete_at) "
        "values ($1, $2, $3, 5, now())",
        bid,
        uid,
        TG,
    )
    await db.raw(
        "insert into rw_inbox (hash, ts, scope, event, panel_user_id) "
        "values ($1, now(), 'user', 'user.modified', $2)",
        "b" * 64,
        PANEL_IDS[0],
    )
    await db.raw(
        "insert into lte_counters (node_uuid, usage_date, panel_user_id) values ('n1', current_date, $1)",
        PANEL_IDS[0],
    )
    await db.raw("insert into admin_cards (kind, ref) values ('payments', $1)", f"receipt:{rid}")
    await db.raw(
        "insert into admin_audit (actor_id, role, action, target) values ($1, 'owner', 'user.ban', $2)",
        owner,
        f"user:{uid}",
    )
    return {
        "uid": uid,
        "sid": sid,
        "old_sid": old_sid,
        "pid": pid,
        "oid": oid,
        "invitee": invitee,
        "inviter": inviter,
        "other_ticket": other_ticket,
        "ad": ad,
        "link": link,
        "promo": promo,
        "bid": bid,
    }


async def leftovers(db: CountingDatabase, ids: dict[str, Any]) -> dict[str, int]:
    """Rows still pointing at the deleted user (foreign keys and the known references without one)."""
    uid = ids["uid"]
    found: dict[str, int] = {}
    for table, column, _ in user_fk_columns():
        n = await one(db, f'select count(*) from "{table}" where "{column}" = $1', uid)
        if n:
            found[f"{table}.{column}"] = n
    sids = [ids["sid"], ids["old_sid"]]
    checks = {
        "users": ("select count(*) from users where id = $1 or telegram_id = $2", uid, TG),
        "subscriptions": ("select count(*) from subscriptions where id = any($1::bigint[])", sids),
        "subscription_events": (
            "select count(*) from subscription_events where subscription_id = any($1)",
            sids,
        ),
        "order_items": ("select count(*) from order_items where order_id = $1", ids["oid"]),
        "payment_events": ("select count(*) from payment_events where payment_id = $1", ids["pid"]),
        "trial_grants": ("select count(*) from trial_grants where telegram_id = $1", TG),
        "channel_members": ("select count(*) from channel_members where telegram_id = $1", TG),
        "jobs": (
            "select count(*) from jobs where payload->>'user_id' = $1 or payload->>'referrer_id' = $1 "
            "or kind in ('notify.user', 'panel.update', 'billing.fulfill', 'payments.verify')",
            str(uid),
        ),
        "legacy_id_map": ("select count(*) from legacy_id_map where old_id <> '44'",),
        "legacy_transactions": ("select count(*) from legacy_transactions where user_id = $1", uid),
        "error_events": ("select count(*) from error_events where user_id = $1", uid),
        "broadcast_msgs": ("select count(*) from broadcast_msgs where user_id = $1", uid),
        "rw_inbox": (
            "select count(*) from rw_inbox where panel_user_id = any($1::bigint[])",
            list(PANEL_IDS),
        ),
        "lte_counters": (
            "select count(*) from lte_counters where panel_user_id = any($1::bigint[])",
            list(PANEL_IDS),
        ),
        "admin_cards": ("select count(*) from admin_cards",),
        "ticket_messages": ("select count(*) from ticket_messages",),
    }
    for name, (sql, *args) in checks.items():
        n = await one(db, sql, *args)
        if n:
            found[name] = n
    return found


# ------------------------------------------------------------------------------------------------- graph


def test_every_reference_to_users_is_handled() -> None:
    """A new table with a foreign key to ``users`` must be listed in ``FK_POLICY`` (delete or clear)."""
    found = {(t, c) for t, c, _ in user_fk_columns()}
    assert found, "the metadata has no foreign keys to users?"
    missing = found - set(FK_POLICY)
    assert not missing, f"add these to svbg.services.user_delete.FK_POLICY: {sorted(missing)}"
    assert not set(FK_POLICY) - found, "FK_POLICY lists columns that no longer reference users"


# ------------------------------------------------------------------------------------------------- delete


async def test_delete_leaves_nothing_behind(db: CountingDatabase) -> None:
    ids = await seed(db)
    uid = ids["uid"]
    assert await leftovers(db, ids)  # the seed really points at the user
    api = FakeApi()
    deleter, chat, forgotten = make(db, api)
    async with db.read() as conn:
        pv = await deleter.preview(conn, uid)
    assert pv is not None
    assert (pv.subscriptions, pv.panel_users, pv.payments, pv.orders) == (2, 2, 1, 1)
    assert (pv.invited, pv.invited_by, pv.tickets, pv.promo_uses, pv.wallet_minor) == (1, True, 1, 1, 150)

    result = await deleter.delete(OWNER, uid)
    assert result.ok, result.text
    assert sorted(api.deleted) == sorted(PANEL_IDS) and result.panel_deleted == 2
    assert await leftovers(db, ids) == {}
    # aggregate counters and other people stay
    assert await one(db, "select clicks from ad_links where id = $1", ids["ad"]) == 5
    assert await one(db, "select count(*) from deeplink_daily") == 1
    assert await one(db, "select uses from promocodes where id = $1", ids["promo"]) == 1
    assert await one(db, "select created_by from deeplinks where id = $1", ids["link"]) is None
    assert await one(db, "select count(*) from broadcasts where id = $1", ids["bid"]) == 1
    assert (
        await one(
            db, "select count(*) from users where id = any($1::bigint[])", [ids["invitee"], ids["inviter"]]
        )
        == 2
    )
    assert await one(db, "select count(*) from referrals") == 0  # the invitee lost the inviter
    assert await one(db, "select closed_by from tickets where id = $1", ids["other_ticket"]) is None
    assert await one(db, "select count(*) from jobs") == 1  # only the job about somebody else
    assert await one(db, "select count(*) from legacy_id_map") == 1
    # one journal entry: who deleted whom; the earlier staff journal stays
    audit = await db.raw("select * from admin_audit where action = $1", AUDIT_ACTION)
    assert len(audit) == 1 and audit[0]["target"] == f"user:{uid}"
    assert audit[0]["details"]["telegram_id"] == TG and audit[0]["details"]["name"] == "Иван @ivan"
    assert audit[0]["details"]["panel"] == "deleted"
    assert await one(db, "select count(*) from admin_audit where action = 'user.ban'") == 1
    assert forgotten == [(uid, TG)]
    assert len(chat.posts) == 1 and chat.posts[0][0] == "new_users"
    assert "Иван @ivan" in chat.posts[0][1] and "5005" in chat.posts[0][1]
    # twice: nothing to delete any more
    again = await deleter.delete(OWNER, uid)
    assert not again.ok and again.code == "not_found"


async def test_after_deletion_start_is_a_brand_new_user(db: CountingDatabase) -> None:
    ids = await seed(db)
    trials = TrialService(db, None, config=lambda: {"TRIAL_DAYS": 3})  # type: ignore[arg-type]
    assert (await trials.check(ids["uid"])).reason == "used"
    deleter, _, _ = make(db, FakeApi())
    assert (await deleter.delete(OWNER, ids["uid"])).ok
    directory = UserDirectory(db, Settings({"OWNER_IDS": [CONF_OWNER], "DEFAULT_LANGUAGE": "ru"}))
    ctx = await directory.load(TgUser(id=TG, is_bot=False, first_name="Иван", username="ivan"))
    assert ctx is not None
    assert ctx.is_new and not ctx.captcha_passed and ctx.user_id != ids["uid"] and ctx.role == "user"
    assert (await trials.check(ctx.user_id)).ok
    assert await one(db, "select wallet_minor from users where id = $1", ctx.user_id) == 0


# ------------------------------------------------------------------------------------------------- panel


async def test_panel_failure_stops_everything(db: CountingDatabase) -> None:
    ids = await seed(db)
    api = FakeApi(RemnawaveError(ErrorKind.TRANSIENT, 503))
    deleter, chat, forgotten = make(db, api)
    result = await deleter.delete(OWNER, ids["uid"])
    assert not result.ok and result.code == "panel"
    assert result.text == "Панель не дала удалить пользователя: панель не отвечает (503)."
    assert await one(db, "select count(*) from users where id = $1", ids["uid"]) == 1
    assert await one(db, "select count(*) from subscriptions where user_id = $1", ids["uid"]) == 2
    assert await one(db, "select count(*) from admin_audit where action = $1", AUDIT_ACTION) == 0
    assert not chat.posts and not forgotten
    # «Удалить только в боте»: the panel is not touched, its users stay there
    only_bot = await deleter.delete(OWNER, ids["uid"], panel=False)
    assert only_bot.ok and only_bot.panel_kept == 2 and only_bot.panel_deleted == 0
    assert api.deleted == []
    assert await leftovers(db, ids) == {}
    audit = await db.raw("select details from admin_audit where action = $1", AUDIT_ACTION)
    assert audit[0]["details"]["panel"] == "kept"


async def test_panel_partial_failure_and_no_panel(db: CountingDatabase) -> None:
    ids = await seed(db)
    api = FakeApi(RemnawaveError(ErrorKind.SERVER, 500, "A018"), fail_on=PANEL_IDS[1])
    deleter, _, _ = make(db, api)
    result = await deleter.delete(OWNER, ids["uid"])
    assert not result.ok and result.panel_deleted == 1
    assert result.text.startswith("Панель удалила 1 из 2, дальше ошибка: внутренняя ошибка панели")
    no_panel, _, _ = make(db, None)
    result = await no_panel.delete(OWNER, ids["uid"])
    assert result.code == "panel" and "панель не подключена" in result.text
    assert await one(db, "select count(*) from users where id = $1", ids["uid"]) == 1


async def test_user_without_panel_needs_no_panel(db: CountingDatabase) -> None:
    await staff(db)
    uid = await add_user(db, TG)
    deleter, chat, _ = make(db, None)  # the panel is not connected: nothing to delete there anyway
    result = await deleter.delete(OWNER, uid)
    assert result.ok and result.panel_deleted == 0 and result.panel_kept == 0
    assert "Панель: не было" in chat.posts[0][1]


# ------------------------------------------------------------------------------------------------- rights


async def test_who_may_delete_whom(db: CountingDatabase) -> None:
    await staff(db)
    uid = await add_user(db, TG)
    conf_owner = await add_user(db, CONF_OWNER)
    admin = await one(db, "select id from users where telegram_id = $1", ADMIN)
    owner = await one(db, "select id from users where telegram_id = $1", OWNER)
    deleter, _, _ = make(db, FakeApi())
    for tg in (SUPPORT, ADMIN_NO, TG, 999):
        result = await deleter.delete(tg, uid)
        assert result.code == "denied" and result.denied, tg
        assert not await deleter.allowed(tg, uid)
    assert (await deleter.delete(ADMIN, admin)).code == "self"
    assert (await deleter.delete(OWNER, owner)).code == "self"
    assert (await deleter.delete(ADMIN, owner)).code == "owner"
    assert (await deleter.delete(ADMIN, conf_owner)).code == "owner"  # OWNER_IDS, stored as a plain user
    support = await one(db, "select id from users where telegram_id = $1", SUPPORT)
    staff_refusal = await deleter.delete(OWNER, support)
    assert (
        staff_refusal.code == "staff" and staff_refusal.text == "Это сотрудник. Сначала снимите с него роль."
    )
    assert (await deleter.delete(OWNER, 99_999)).code == "not_found"
    assert await one(db, "select count(*) from users") == 6
    assert await deleter.allowed(ADMIN, uid)
    assert (await deleter.delete(ADMIN, uid)).ok  # an admin with «users.delete»
    star = await add_user(db, 9009, "admin")
    await db.raw("update users set perms = '[\"*\"]'::jsonb where id = $1", star)
    other = await add_user(db, 9010)
    assert (await deleter.delete(9009, other)).ok  # «*» = the whole Admin column, «users.delete» included
