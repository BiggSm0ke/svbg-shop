"""Panel writer: single writer, absolute targets, coalescing, adoption, squads, hwid, forever (02 §4)."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from svbg.remnawave.api import MUTATING_METHODS
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.models import FOREVER
from svbg.remnawave.writer import (
    K_RENEW,
    K_REVOKE,
    enqueue_action,
    enqueue_renew,
    enqueue_update,
    next_username,
)
from tests.subscriptions.kit import sync_env

pytestmark = pytest.mark.timeout(90)

REPO = Path(__file__).resolve().parents[2]
WRITER = REPO / "svbg" / "remnawave" / "writer.py"


class LossyApi:
    """Wraps the real API: ``method`` reaches the panel, then the response is "lost" (a timeout)."""

    def __init__(self, inner: Any, method: str, times: int = 1) -> None:
        self.inner = inner
        self.method = method
        self.times = times
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name != self.method:
            return attr

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            result = await attr(*args, **kwargs)
            if self.times > 0:
                self.times -= 1
                raise RemnawaveError(ErrorKind.TRANSIENT, None, "TIMEOUT", "ответ потерян (тест)")
            return result

        return wrapped


# ------------------------------------------------------------------------------------------ single writer


#: Names too generic to flag on any object (``topic.enable()``): flagged only on a panel-looking receiver.
GENERIC: frozenset[str] = frozenset({"enable", "disable"})
_PANEL_HINTS = ("api", "client", "panel", "remnawave", "rw")


def _mutating_calls(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_METHODS:
            receiver = ast.unparse(func.value).lower()
            if func.attr not in GENERIC or any(h in receiver for h in _PANEL_HINTS):
                found.append((node.lineno, func.attr))
        # getattr(api, "create_user")(…) is a call too
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in MUTATING_METHODS
        ):
            found.append((node.lineno, str(node.args[1].value)))
    return found


def test_writer_is_the_only_caller_of_mutating_panel_methods() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "svbg").rglob("*.py")):
        if path == WRITER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, name in _mutating_calls(tree):
            offenders.append(f"{path.relative_to(REPO)}:{line} {name}()")
    assert not offenders, "мутирующие методы панели вызываются вне writer.py:\n" + "\n".join(offenders)
    # The rule is not vacuous: the writer itself does call them.
    used = {name for _, name in _mutating_calls(ast.parse(WRITER.read_text(encoding="utf-8")))}
    assert {
        "create_user",
        "update_user",
        "enable",
        "disable",
        "delete_user",
        "revoke",
        "reset_traffic",
    } <= used


def test_ast_rule_catches_a_bypass() -> None:
    bad = ast.parse(
        "async def f(api, comp, topic):\n"
        "    await api.update_user(1, tag='X')\n"
        "    getattr(api, 'disable')(1)\n"
        "    await comp.client.enable(5)\n"
        "    await topic.enable()\n"  # not the panel: allowed
    )
    assert sorted(n for _, n in _mutating_calls(bad)) == ["disable", "enable", "update_user"]


def test_next_username_keeps_suffix_within_36() -> None:
    assert next_username("sv_123", 2) == "sv_123_2"
    long = "a" * 36
    assert next_username(long, 12) == "a" * 33 + "_12"


# ------------------------------------------------------------------------------------------------ create


async def test_create_links_subscription_and_sends_contract_body(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.new_sub(555, traffic_bytes=10 * 2**30)
        jobs = await env.jobs()
        assert [(j["kind"], j["ordering_key"], j["queue"]) for j in jobs] == [
            ("panel.create", f"sub:{sid}", "panel")
        ]
        outcomes = await env.drain()
        assert [o for _, o in outcomes] == ["done"]
        row = await env.sub(sid)
        assert row["link_state"] == "linked"
        user = env.panel.users[row["panel_user_id"]]
        assert row["panel_short_uuid"] == user["shortUuid"]
        assert row["subscription_url"] == f"https://sub.example.com/{user['shortUuid']}"
        body = env.panel.calls("/users", "POST")[0].body
        assert body["username"] == "sv_555" and body["telegramId"] == 555
        assert body["activeInternalSquads"] == [env.squad]
        assert body["trafficLimitBytes"] == 10 * 2**30
        # hwidDeviceLimit NULL (= panel fallback) cannot be sent on create: the field is omitted.
        assert "hwidDeviceLimit" not in body and "externalSquadUuid" not in body and "status" not in body
        assert body["description"].startswith("sv:")
        # A repeated create job (e.g. a duplicate) is a no-op: still one panel user.
        async with env.db.tx() as conn:
            from svbg.remnawave.writer import enqueue_create

            await enqueue_create(conn, sid)
        await env.drain()
        assert len(env.panel.users) == 1


async def test_hwid_triple_semantics_and_forever(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(1, device_limit=0, expire_at=FOREVER)
        body = env.panel.calls("/users", "POST")[0].body
        assert body["hwidDeviceLimit"] == 0  # 0 = no limit, sent as is
        assert body["expireAt"] == "2099-12-31T00:00:00.000Z"
        row = await env.sub(sid)
        assert row["panel_expire_at"] == FOREVER and row["desired_expire_at"] == FOREVER

        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_device_limit=None)
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body == {
            "id": row["panel_user_id"],
            "hwidDeviceLimit": None,
        }
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_device_limit=5)
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body["hwidDeviceLimit"] == 5
        assert env.panel_user(row["panel_user_id"])["hwidDeviceLimit"] == 5

        async with env.db.tx() as conn:
            await env.service.set_forever(conn, sid)
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body["expireAt"] == "2099-12-31T00:00:00.000Z"


async def test_adoption_after_lost_create_response(pg_dsn: str) -> None:
    """The panel created the user but the answer was lost; the retry gets A019 and adopts it."""
    async with sync_env(pg_dsn) as env:
        lossy = LossyApi(env.api, "create_user")
        env.api = lossy  # type: ignore[assignment]
        sid = await env.new_sub(777)
        assert [o for _, o in await env.drain()] == ["failed"]
        assert len(env.panel.users) == 1 and (await env.sub(sid))["link_state"] == "pending"
        assert [o for _, o in await env.drain(make_due=True)] == ["done"]
        assert len(env.panel.users) == 1, "второй пользователь панели не создан"
        row = await env.sub(sid)
        assert row["link_state"] == "linked" and row["panel_user_id"] == next(iter(env.panel.users))
        kinds = [
            r["kind"]
            for r in await env.db.raw("select kind from subscription_events where subscription_id=$1", sid)
        ]
        assert kinds == ["panel_adopted"]
        assert len(env.panel.calls("/users/by-username/*", "GET")) == 1


async def test_a019_foreign_user_takes_a_suffix(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        env.panel.add_user(username="sv_900", telegramId=12345)  # someone else
        env.panel.add_user(
            username="sv_900_2", telegramId=900, createdAt=datetime.now(UTC) - timedelta(days=90)
        )
        sid = await env.new_sub(900)
        await env.drain()
        row = await env.sub(sid)
        assert row["link_state"] == "linked"
        assert row["panel_username"] == "sv_900_3"  # foreign one, then an OLD account of the same person
        assert len(env.panel.users) == 3


async def test_vanished_external_squad_is_dropped_with_alert(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.new_sub(5, ext_squad="00000000-0000-4000-8000-000000000000")
        await env.drain()
        row = await env.sub(sid)
        assert row["link_state"] == "linked" and row["desired_ext_squad"] is None
        assert "externalSquadUuid" not in env.panel.calls("/users", "POST")[-1].body
        assert any(k.startswith("rw:ext_squad_gone:") for k in await env.attention_keys())


async def test_vanished_internal_squad_is_dead_with_alert(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.new_sub(6, squads=["11111111-1111-4111-8111-111111111111"])
        assert [o for _, o in await env.drain()] == ["dead"]
        assert (await env.sub(sid))["link_state"] == "pending"
        assert f"rw:squad_gone:{sid}" in await env.attention_keys()
        assert not env.panel.users


# ------------------------------------------------------------------------------------- absolute targets


async def test_renew_retry_after_timeout_does_not_extend_twice(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(42, days=10)
        pid = (await env.sub(sid))["panel_user_id"]
        before = env.panel_user(pid)["expireAt"]
        env.api = LossyApi(env.api, "update_user")  # type: ignore[assignment]
        async with env.db.tx() as conn:
            await env.service.renew(conn, sid, 30)
        assert [o for _, o in await env.drain()] == ["failed"]
        job = (await env.jobs(K_RENEW))[0]
        target = datetime.fromisoformat(job["payload"]["target_expire_at"])
        assert abs(target - (before + timedelta(days=30))) < timedelta(milliseconds=2)
        assert [o for _, o in await env.drain(make_due=True)] == ["done"]
        after = env.panel_user(pid)["expireAt"]
        assert abs(after - (before + timedelta(days=30))) < timedelta(milliseconds=2), "продлено дважды"
        patches = env.panel.calls("/users", "PATCH")
        assert len(patches) == 2 and patches[0].body == patches[1].body, "повтор шлёт ту же абсолютную дату"
        assert len(env.panel.calls(f"/users/{pid}", "GET")) == 1, "цель считается один раз"
        assert not env.panel.calls("/users/*/actions/extend", "POST")
        row = await env.sub(sid)
        assert row["paid_until"] == row["desired_expire_at"] == target
        events = await env.db.raw("select kind, delta_seconds from subscription_events where kind='renewed'")
        assert [(e["kind"], e["delta_seconds"]) for e in events] == [("renewed", 30 * 86400)]


async def test_renew_of_expired_user_counts_from_now(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(43)
        pid = (await env.sub(sid))["panel_user_id"]
        env.panel_user(pid).update(expireAt=datetime.now(UTC) - timedelta(days=5), status="EXPIRED")
        async with env.db.tx() as conn:
            await enqueue_renew(conn, sid, 7)
        await env.drain()
        user = env.panel_user(pid)
        assert user["status"] == "ACTIVE"  # PATCH expireAt without status re-activates (02 §4.3)
        assert abs(user["expireAt"] - (datetime.now(UTC) + timedelta(days=7))) < timedelta(seconds=30)
        assert "status" not in env.panel.calls("/users", "PATCH")[-1].body


# ------------------------------------------------------------------------------------------- coalescing


async def test_consecutive_updates_are_coalesced_into_one_patch(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(50)
        pid = (await env.sub(sid))["panel_user_id"]
        for kwargs in (
            {"desired_traffic_bytes": 1000},
            {"desired_device_limit": 3},
            {"desired_traffic_bytes": 2000},
        ):
            async with env.db.tx() as conn:
                await env.service.change(conn, sid, **kwargs)
        await env.drain()
        patches = env.panel.calls("/users", "PATCH")
        assert len(patches) == 1
        assert patches[0].body == {"id": pid, "trafficLimitBytes": 2000, "hwidDeviceLimit": 3}
        statuses = [j["status"] for j in await env.jobs("panel.update")]
        assert statuses == ["done", "done", "done"]


async def test_coalescing_stops_at_a_different_operation(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(51)
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=1)
        async with env.db.tx() as conn:
            await env.service.disable(conn, sid, reason="BOT_BAN")
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=2)
        await env.drain()
        assert len(env.panel.calls("/users", "PATCH")) == 2
        order = [r.path for r in env.panel.requests if r.method in ("PATCH", "POST") and r.path != "/users"]
        assert order == [f"/users/{(await env.sub(sid))['panel_user_id']}/actions/disable"]


# ------------------------------------------------------------------------------------------------ squads


async def test_never_sends_empty_active_internal_squads(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        with pytest.raises(ValueError, match="сквад"):
            await env.new_sub(60, squads=[])
        sid = await env.linked_sub(61)
        await env.db.raw("update subscriptions set desired_squads = '[]'::jsonb where id = $1", sid)
        async with env.db.tx() as conn:
            await enqueue_update(conn, sid, ["squads", "traffic"])
        await env.drain()
        assert all("activeInternalSquads" not in r.body for r in env.panel.calls("/users", "PATCH"))
        assert env.panel_user((await env.sub(sid))["panel_user_id"])["activeInternalSquads"] == [env.squad]


async def test_core_applies_substitutions_forward(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        twin = env.panel.add_internal_squad("NL-LTE-blocked")
        env.register_module("lte", "ok")
        sid = await env.linked_sub(70)
        await env.substitute(sid, env.squad, twin)
        async with env.db.tx() as conn:
            await enqueue_update(conn, sid, ["squads"])
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body["activeInternalSquads"] == [twin]
        # A create also goes through the core's tables.
        sid2 = await env.new_sub(71)
        await env.substitute(sid2, env.squad, twin)
        await env.drain()
        assert env.panel.calls("/users", "POST")[-1].body["activeInternalSquads"] == [twin]


@pytest.mark.parametrize("state", ["degraded", "unloaded", "raises", False])
async def test_chaos_degraded_module_freezes_squads(pg_dsn: str, state: Any) -> None:
    """07 §2.4.3 п.3/п.5: module down + live substitution → expireAt written, squads untouched, attention."""
    async with sync_env(pg_dsn) as env:
        twin = env.panel.add_internal_squad("NL-LTE-blocked")
        env.register_module("lte", "ok")
        sid = await env.linked_sub(80)
        await env.substitute(sid, env.squad, twin)
        async with env.db.tx() as conn:
            await enqueue_update(conn, sid, ["squads"])
        await env.drain()
        pid = (await env.sub(sid))["panel_user_id"]
        assert env.panel_user(pid)["activeInternalSquads"] == [twin]  # blocked by the module

        if state == "unloaded":
            env.contributors._status.pop("lte")
        elif state == "raises":
            env.module_states["lte"] = RuntimeError("LTE упал")
        else:
            env.module_states["lte"] = state
        new_expire = datetime.now(UTC) + timedelta(days=60)
        async with env.db.tx() as conn:
            # a plan change: new term + new squads list (the module's base squad stays in it)
            await env.service.change(conn, sid, desired_expire_at=new_expire, desired_squads=[env.squad])
        async with env.db.tx() as conn:
            await env.service.renew(conn, sid, 30)
        await env.drain()
        patches = [r.body for r in env.panel.calls("/users", "PATCH")[1:]]
        assert patches and all("activeInternalSquads" not in b for b in patches), patches
        assert any("expireAt" in b for b in patches)
        assert env.panel_user(pid)["activeInternalSquads"] == [twin], "двойник снят молча"
        assert "rw:squads_frozen:lte" in await env.attention_keys()
        item = await env.attention.get("rw:squads_frozen:lte")
        assert item is not None and "1 подписок" in item.title

        # The module recovers: the periodic check resolves the item, and the squads are writable again.
        env.register_module("lte", "ok")
        assert await env.contributors.refresh_attention(env.db) == []
        assert "rw:squads_frozen:lte" not in await env.attention_keys()


# --------------------------------------------------------------------------------------- overrides, errors


async def test_writer_respects_overrides(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(90, traffic_bytes=100)
        await env.db.raw(
            """update subscriptions set overrides = '{"traffic_bytes": 555}'::jsonb where id = $1""", sid
        )
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=200, desired_device_limit=2)
        await env.drain()
        body = env.panel.calls("/users", "PATCH")[-1].body
        assert "trafficLimitBytes" not in body and body["hwidDeviceLimit"] == 2
        # An explicit admin change clears the override first (clear_overrides).
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=300, clear_overrides=["traffic_bytes"])
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body["trafficLimitBytes"] == 300
        assert "traffic_bytes" not in (await env.sub(sid))["overrides"]


async def test_validation_error_goes_dead_with_owner_alert(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(91)
        await env.db.raw("update subscriptions set desired_tag = 'bad tag' where id = $1", sid)
        async with env.db.tx() as conn:
            await enqueue_update(conn, sid, ["tag"])
        assert [o for _, o in await env.drain()] == ["dead"]
        assert f"rw:op_dead:{sid}:panel.update" in await env.attention_keys()


async def test_transient_failures_alert_after_five_attempts(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(92)
        env.panel.inject("503", path="/users", method="PATCH", times=None)
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=7)
        for _ in range(5):
            # 503 → retried with backoff; after 5 in a row the panel breaker opens → retry after its cooldown
            assert [o for _, o in await env.drain(make_due=True)] in (["failed"], ["retry"])
        assert "rw:op_stuck" in await env.attention_keys()
        assert (await env.jobs("panel.update"))[0]["status"] == "ready", (
            "транзиентная ошибка не убивает задачу"
        )
        env.panel.clear_faults()
        env.api.transport.breaker.reset()
        assert [o for _, o in await env.drain(make_due=True)] == ["done"]


async def test_user_deleted_in_panel_becomes_panel_missing(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(93)
        del env.panel.users[(await env.sub(sid))["panel_user_id"]]
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=1)
        assert [o for _, o in await env.drain()] == ["done"]
        row = await env.sub(sid)
        assert row["link_state"] == "panel_missing" and row["paid_until"] is not None
        assert f"rw:panel_missing:{sid}" in await env.attention_keys()
        assert not env.panel.calls("/users", "POST")[1:], "не пересоздаём автоматически"


# ----------------------------------------------------------------------------------------------- actions


async def test_ban_unban_and_bounce_protection(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(94)
        pid = (await env.sub(sid))["panel_user_id"]
        async with env.db.tx() as conn:
            await env.service.disable(conn, sid, reason="BOT_BAN")
        await env.drain()
        assert env.panel_user(pid)["status"] == "DISABLED"
        assert (await env.sub(sid))["disabled_reason"] == "BOT_BAN"
        # unban lifts only BOT_BAN; the panel user is expired → flags cleared, no enable (it would bounce)
        env.panel_user(pid)["expireAt"] = datetime.now(UTC) - timedelta(hours=1)
        async with env.db.tx() as conn:
            await env.service.enable(conn, sid, only_reason="BOT_BAN")
        await env.drain()
        assert not env.panel.calls(f"/users/{pid}/actions/enable", "POST")
        assert (await env.sub(sid))["disabled_reason"] is None
        env.panel_user(pid)["expireAt"] = datetime.now(UTC) + timedelta(days=3)
        await env.db.raw("update subscriptions set disabled_reason='BOT_BAN' where id=$1", sid)
        async with env.db.tx() as conn:
            await env.service.enable(conn, sid, only_reason="BOT_BAN")
        await env.drain()
        assert env.panel_user(pid)["status"] == "ACTIVE"


async def test_revoke_is_not_repeated_after_lost_response(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(95)
        pid = (await env.sub(sid))["panel_user_id"]
        env.api = LossyApi(env.api, "revoke")  # type: ignore[assignment]
        async with env.db.tx() as conn:
            await enqueue_action(conn, sid, K_REVOKE)
        await env.drain()
        await env.drain(make_due=True)
        assert len(env.panel.calls(f"/users/{pid}/actions/revoke", "POST")) == 1
        row = await env.sub(sid)
        assert row["panel_short_uuid"] == env.panel_user(pid)["shortUuid"]


async def test_delete_closes_and_404_is_success(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(96)
        async with env.db.tx() as conn:
            await env.service.close(conn, sid, delete_in_panel=True)
        async with env.db.tx() as conn:
            await env.service.close(conn, sid, delete_in_panel=True)
        assert [o for _, o in await env.drain()] == ["done", "done"]
        assert not env.panel.users
        assert (await env.sub(sid))["link_state"] == "closed"


async def test_panel_not_configured_waits(pg_dsn: str) -> None:
    from svbg.remnawave.errors import PanelNotConfiguredError

    async with sync_env(pg_dsn) as env:

        def missing() -> Any:
            raise PanelNotConfiguredError()

        env.current_api = missing  # type: ignore[method-assign]
        env.writer._api = missing
        await env.new_sub(97)
        await env.db.raw("update jobs set max_attempts = 2")
        for _ in range(4):  # a panel still being set up never sends the job to dead
            assert [o for _, o in await env.drain(make_due=True)] == ["retry"]
        job = (await env.jobs())[0]
        assert job["status"] == "ready" and job["attempts"] == 4


# ------------------------------------------------------------------------------------- review fixes


async def test_coalesced_fields_survive_a_failed_patch(pg_dsn: str) -> None:
    """Folded updates are persisted into the surviving job: a retry after 503 still sends every field."""
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(110)
        pid = (await env.sub(sid))["panel_user_id"]
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=4096)
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_device_limit=4)
        env.panel.inject("503", path="/users", method="PATCH", times=None)
        assert [o for _, o in await env.drain()] in (["failed"], ["retry"])
        jobs = await env.jobs("panel.update")
        assert [j["status"] for j in jobs] == ["ready", "done"]
        assert jobs[0]["payload"]["fields"] == ["device_limit", "traffic"]
        env.panel.clear_faults()
        env.api.transport.breaker.reset()
        assert [o for _, o in await env.drain(make_due=True)] == ["done"]
        body = env.panel.calls("/users", "PATCH")[-1].body
        assert body == {"id": pid, "trafficLimitBytes": 4096, "hwidDeviceLimit": 4}
        assert env.panel_user(pid)["hwidDeviceLimit"] == 4


async def test_coalescing_is_rolled_back_when_the_lease_is_lost(pg_dsn: str) -> None:
    from svbg.jobs.queue import JobQueue
    from svbg.jobs.worker import JobContext, RetryJob

    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(111)
        for value in (1, 2):
            async with env.db.tx() as conn:
                await env.service.change(conn, sid, desired_traffic_bytes=value)
        queue = JobQueue(env.db)
        job = (await queue.claim("interactive", "w1", 1))[0]
        stranger = JobContext(db=env.db, queue=queue, worker_id="another-worker")
        with pytest.raises(RetryJob):
            await env.writer.handlers()["panel.update"](job, stranger)
        assert [j["status"] for j in await env.jobs("panel.update")] == ["running", "ready"]
        assert not env.panel.calls("/users", "PATCH")


async def test_closed_during_lost_create_response_is_not_an_orphan(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        env.api = LossyApi(env.api, "create_user")  # type: ignore[assignment]
        sid = await env.new_sub(120)
        assert [o for _, o in await env.drain()] == ["failed"]  # created in the panel, answer lost
        async with env.db.tx() as conn:
            await env.service.close(conn, sid)  # pending → closed before the retry
        await env.drain(make_due=True)
        row = await env.sub(sid)
        assert row["link_state"] == "closed" and row["panel_user_id"] is not None
        assert len(env.panel.users) == 1
        assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED", "сирота с доступом не осталась"


async def test_closed_while_create_is_in_flight_is_not_an_orphan(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        inner = env.api
        sids: list[int] = []

        class ClosingApi:
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            async def create_user(self, **kwargs: Any) -> Any:
                user = await inner.create_user(**kwargs)
                async with env.db.tx() as conn:  # the owner closes it while the POST is on its way back
                    await env.service.close(conn, sids[0])
                return user

        env.api = ClosingApi()  # type: ignore[assignment]
        sids.append(await env.new_sub(121))
        await env.drain()
        row = await env.sub(sids[0])
        assert row["link_state"] == "closed" and row["panel_user_id"] is not None
        assert env.panel_user(row["panel_user_id"])["status"] == "DISABLED"


async def test_update_drops_a_deleted_external_squad_and_keeps_the_rest(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(130)
        pid = (await env.sub(sid))["panel_user_id"]
        gone = "00000000-0000-4000-8000-0000000000aa"
        new_expire = datetime.now(UTC) + timedelta(days=90)
        async with env.db.tx() as conn:
            await env.service.change(
                conn, sid, desired_ext_squad=gone, desired_traffic_bytes=777, desired_expire_at=new_expire
            )
        assert [o for _, o in await env.drain()] == ["done"]
        user = env.panel_user(pid)
        assert user["trafficLimitBytes"] == 777 and user["externalSquadUuid"] is None
        assert abs(user["expireAt"] - new_expire) < timedelta(seconds=1), "оплаченная смена применена"
        assert (await env.sub(sid))["desired_ext_squad"] is None
        assert f"rw:ext_squad_gone:{gone}" in await env.attention_keys()


async def test_update_with_a_deleted_internal_squad_is_dead_with_alert(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(131)
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_squads=["11111111-1111-4111-8111-111111111111"])
        assert [o for _, o in await env.drain()] == ["dead"]
        assert f"rw:squad_gone:{sid}" in await env.attention_keys()
        item = await env.attention.get(f"rw:squad_gone:{sid}")
        assert item is not None and "изменение не применено" in item.body


async def test_close_in_disable_mode_closes_the_link(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(140)
        pid = (await env.sub(sid))["panel_user_id"]
        async with env.db.tx() as conn:
            await env.service.close(conn, sid)
        await env.drain()
        row = await env.sub(sid)
        assert row["link_state"] == "closed" and row["disabled_reason"] == "closed"
        assert env.panel_user(pid)["status"] == "DISABLED"
        kinds = [
            r["kind"]
            for r in await env.db.raw(
                "select kind from subscription_events where subscription_id=$1 order by id", sid
            )
        ]
        assert "closed" in kinds
        before = len(env.panel.requests)
        async with env.db.tx() as conn:
            await env.service.renew(conn, sid, 30)
            await env.service.enable(conn, sid)
            await env.service.change(conn, sid, desired_traffic_bytes=1)
        await env.drain()
        writes = [r for r in env.panel.requests[before:] if r.method != "GET"]
        assert writes == [], "закрытую подписку писатель не трогает (кроме disable/delete)"
        assert env.panel_user(pid)["status"] == "DISABLED"


async def test_explicit_tag_removal_sends_null(pg_dsn: str) -> None:
    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(150, tag="TRIAL")
        pid = (await env.sub(sid))["panel_user_id"]
        assert env.panel_user(pid)["tag"] == "TRIAL"
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_traffic_bytes=5)  # tag not asked for: untouched
        await env.drain()
        assert "tag" not in env.panel.calls("/users", "PATCH")[-1].body
        async with env.db.tx() as conn:
            await env.service.change(conn, sid, desired_tag=None)
        await env.drain()
        assert env.panel.calls("/users", "PATCH")[-1].body == {"id": pid, "tag": None}
        assert env.panel_user(pid)["tag"] is None


async def test_panel_outage_does_not_use_up_attempts(pg_dsn: str) -> None:
    """02 §6.5: the outbox outlives any outage — breaker-open retries never send a paid job to dead."""
    from svbg.remnawave.errors import PanelUnavailableError

    async with sync_env(pg_dsn) as env:
        sid = await env.linked_sub(160)
        inner = env.api

        class DownApi:
            def __getattr__(self, name: str) -> Any:
                if name in ("get_user", "update_user"):

                    async def unavailable(*args: Any, **kwargs: Any) -> Any:
                        raise PanelUnavailableError(method="GET", path="/users", retry_in=120)

                    return unavailable
                return getattr(inner, name)

        env.api = DownApi()  # type: ignore[assignment]
        async with env.db.tx() as conn:
            await env.service.renew(conn, sid, 30)
        await env.db.raw("update jobs set max_attempts = 3 where kind = 'panel.renew'")
        for _ in range(6):
            assert [o for _, o in await env.drain(make_due=True)] == ["retry"]
        job = (await env.jobs(K_RENEW))[0]
        assert job["status"] == "ready" and job["attempts"] == 6 and job["max_attempts"] == 9
        env.api = inner
        assert [o for _, o in await env.drain(make_due=True)] == ["done"]
