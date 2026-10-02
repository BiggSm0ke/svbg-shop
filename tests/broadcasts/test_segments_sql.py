"""``to_sql`` (DSL → SQL) agrees with ``compile_condition`` on the UserCtx the user path builds; presets."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from svbg.broadcasts.repo import MARKETING_RECHECK, Audience, AudienceConfig
from svbg.broadcasts.segments import PRESETS, SegmentError, recipients_where, validate_segment
from svbg.core.tables import users
from svbg.tg.ui.conditions import ConditionError, compile_condition, to_sql
from svbg.tg.ui.context import UserCtx
from svbg.tg.user.status import StatusReader
from tests.broadcasts.kit import NOW, mk_user
from tests.dbkit import CountingDatabase

CHANNEL = -1001234


async def population(db: CountingDatabase) -> dict[str, int]:
    u = {
        "nosub": await mk_user(db, 1),
        "active": await mk_user(db, 2, lang="en", wallet=500, sub={"days": 10, "plan": "month"}, paid=True),
        "trial": await mk_user(db, 3, sub={"days": 2, "trial": True, "plan": "trial"}),
        "expired5": await mk_user(db, 4, sub={"days": -5, "plan": "month"}, paid=True),
        "frozen": await mk_user(db, 5, sub={"days": 20, "hold": True}),
        "no_until": await mk_user(db, 6, sub={"none_paid_until": True, "state": "pending"}),
        "closed": await mk_user(db, 7, sub={"days": 30, "state": "closed"}),
        "admin_de": await mk_user(db, 8, role="admin", lang="de", wallet=1),
        "half_day": await mk_user(db, 9, role="owner", sub={"days": 0.5, "plan": "year"}),
        "expired40": await mk_user(db, 10, sub={"days": -40}),
        "support": await mk_user(db, 11, role="support", sub={"days": 3}, paid=True),
    }
    # two subscriptions: the live (linked) expired one wins over a newer panel_missing active one
    two = await mk_user(db, 12, sub={"days": -2})
    await db.raw(
        "insert into subscriptions (user_id, link_state, paid_until, panel_user_id) "
        "values ($1, 'panel_missing', $2, $3)",
        two,
        NOW.replace(year=2027),
        777_777,
    )
    u["two"] = two
    for tg, member in ((2, True), (3, False), (8, True)):
        await db.raw(
            "insert into channel_members (chat_id, telegram_id, status, is_member, seen_at) "
            "values ($1, $2, $3, $4, $5)",
            CHANNEL,
            tg,
            "member" if member else "left",
            member,
            NOW,
        )
    return u


async def contexts(db: CountingDatabase) -> dict[int, UserCtx]:
    reader = StatusReader(db)
    rows = await db.raw("select id, telegram_id, role, language from users")
    members = {
        int(r["telegram_id"]): bool(r["is_member"])
        for r in await db.raw(
            "select telegram_id, is_member from channel_members where chat_id = $1", CHANNEL
        )
    }
    out: dict[int, UserCtx] = {}
    for r in rows:
        base = UserCtx(
            int(r["id"]),
            telegram_id=r["telegram_id"],
            role=r["role"],
            lang=r["language"] if r["language"] in ("ru", "en") else "ru",
            channel_member=members.get(r["telegram_id"]),
        )
        status = await reader.load(int(r["id"]))
        assert status is not None
        out[base.user_id] = status.enrich(base, at=NOW)
    return out


async def sql_ids(db: CountingDatabase, where: Any) -> set[int]:
    async with db.read() as conn:
        return {int(x) for x in (await conn.execute(sa.select(users.c.id).where(where))).scalars()}


DSLS: list[dict[str, Any]] = [
    {},
    {"sub": "none"},
    {"sub": "active"},
    {"sub": "trial"},
    {"sub": "expired"},
    {"sub": "frozen"},
    {"sub": ["active", "trial"]},
    {"days_left": {"lte": 3}},
    {"days_left": 1},
    {"days_left": 0},
    {"days_left": {"gt": 5, "ne": 20}},
    {"balance_minor": {"gt": 0}},
    {"balance_minor": 0},
    {"has_paid": True},
    {"has_paid": False},
    {"plan": "month"},
    {"plan": ["month", "year"]},
    {"lang": "ru"},
    {"lang": ["en"]},
    {"role": "admin"},
    {"role": {"gte": "admin"}},
    {"role": {"lt": "admin", "gt": "user"}},
    {"role": ["owner", "support"]},
    {"channel_member": True},
    {"channel_member": False},
    {"not": {"sub": "active"}},
    {"any": []},
    {"all": []},
    {"any": [{"sub": "trial"}, {"has_paid": True}]},
    {"all": [{"sub": "active"}, {"days_left": {"lte": 30}}], "lang": "en"},
    {"not": {"any": [{"sub": "none"}, {"plan": "month"}]}},
]


async def test_sql_matches_python_conditions(db: CountingDatabase) -> None:
    await population(db)
    ctxs = await contexts(db)
    for dsl in DSLS:
        cond = compile_condition(dsl)
        expected = {uid for uid, ctx in ctxs.items() if cond(ctx)}
        got = await sql_ids(db, to_sql(dsl, at=NOW, channel_id=CHANNEL))
        assert got == expected, dsl


async def test_spot_checks(db: CountingDatabase) -> None:
    u = await population(db)
    assert await sql_ids(db, to_sql({"sub": "none"}, at=NOW)) == {u["nosub"], u["closed"], u["admin_de"]}
    assert await sql_ids(db, to_sql({"sub": "expired"}, at=NOW)) == {
        u["expired5"],
        u["no_until"],
        u["expired40"],
        u["two"],
    }
    assert await sql_ids(db, to_sql({"days_left": 1}, at=NOW)) == {u["half_day"]}
    assert await sql_ids(db, to_sql({"lang": "ru"}, at=NOW, default_lang="en")) == set()


@pytest.mark.parametrize(
    "dsl",
    [
        {"is_new": True},
        {"ref_count": {"gt": 1}},
        {"source": "vk"},
        {"flag:lte.blocked": True},
        {"segment:vip": True},
        {"any": [{"sub": "active"}, {"not": {"is_new": False}}]},
        {"channel_member": True},  # no channel configured
        {"bogus": 1},
        {"days_left": "3"},
    ],
)
def test_unsupported_or_invalid_atoms_are_rejected(dsl: dict[str, Any]) -> None:
    with pytest.raises(ConditionError):
        to_sql(dsl, at=NOW)


def test_validate_segment() -> None:
    assert validate_segment({"preset": "active"}) == {"preset": "active"}
    assert validate_segment({}) == {"preset": "all"}
    assert validate_segment({"preset": "all", "dsl": {"sub": "trial"}}) == {
        "preset": "all",
        "dsl": {"sub": "trial"},
    }
    for bad in ({"preset": "nope"}, {"dsl": {"is_new": True}}, {"dsl": [1]}, "all", None):
        with pytest.raises(SegmentError):
            validate_segment(bad)


async def test_presets_and_base_filter(db: CountingDatabase) -> None:
    u = await population(db)
    blocked = await mk_user(db, 20, blocked=True)
    banned = await mk_user(db, 21, banned=True)
    optout = await mk_user(db, 22, marketing=False)
    no_tg = await mk_user(db, None)
    expired_today = await mk_user(db, 23, sub={"days": -0.5})
    everyone = set(u.values()) | {expired_today}
    got = await sql_ids(db, recipients_where({"preset": "all"}, at=NOW))
    assert got == everyone
    assert not {blocked, banned, optout, no_tg} & got
    assert await sql_ids(db, recipients_where({"preset": "expired30"}, at=NOW)) == {u["expired5"], u["two"]}
    assert await sql_ids(db, recipients_where({"preset": "trial"}, at=NOW)) == {u["trial"]}
    assert await sql_ids(db, recipients_where({"preset": "balance"}, at=NOW)) == {u["active"], u["admin_de"]}
    assert await sql_ids(db, recipients_where({"preset": "lang_en"}, at=NOW)) == {u["active"]}
    assert await sql_ids(db, recipients_where({"preset": "active"}, at=NOW)) == {
        u["active"],
        u["half_day"],
        u["support"],
    }
    combined = recipients_where({"preset": "active", "dsl": {"has_paid": True}}, at=NOW)
    assert await sql_ids(db, combined) == {u["active"], u["support"]}
    assert set(PRESETS) >= {"all", "active", "trial", "expired30", "never_paid", "balance", "lang_ru"}


async def test_audience_without_the_marketing_column(db: CountingDatabase) -> None:
    await mk_user(db, 1)
    await mk_user(db, 2, marketing=False)
    audience = Audience(AudienceConfig)
    async with db.read() as conn:
        assert await audience.count(conn, {"preset": "all"}) == 1
    await db.raw("alter table users drop column notify_marketing")
    now = [0.0]
    fresh = Audience(monotonic=lambda: now[0])
    async with db.read() as conn:
        assert await fresh.marketing(conn) is False
        assert await fresh.count(conn, {"preset": "all"}) == 2  # nobody can have opted out yet
    # the migration adds the column: the opt-out applies after the re-check, without a restart
    await db.raw("alter table users add column notify_marketing boolean not null default true")
    await db.raw("update users set notify_marketing = false where telegram_id = 2")
    async with db.read() as conn:
        assert await fresh.count(conn, {"preset": "all"}) == 2  # still within the re-check interval
        now[0] = MARKETING_RECHECK + 1
        assert await fresh.marketing(conn) is True
        assert await fresh.count(conn, {"preset": "all"}) == 1
        now[0] = 0.0  # a positive answer is kept
        assert await fresh.marketing(conn) is True
