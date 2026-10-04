"""LTE quotas: the minimal Telegram admin (05 §2.1.6) — every action checks rights in the service, under a row
lock, writes ``admin_audit`` (``details.domain = 'lte'``) and answers with its result.

Rights (X13): ``lte.view`` — the quota block of the user card (Support, Admin); ``lte.users`` — +ГБ (above
``LTE_ADMIN_GB_MAX`` only the owner), «снять блок до сброса», «заблокировать до сброса», exemption;
``lte.config`` — the owner (or an explicit grant, never ``*``): limits with a preview, enforcement mode,
switching the module off with «Снять / Оставить», quarantine / incident confirmation, «⛔ Снять все блоки»
(with the code word).

Server-side refusals (Г12): an exempt subscription gets no manual block, no «+ГБ»; a manual block needs a
live period and ``LTE_ENFORCE=on``; a block is never placed without a healthy twin.

Switching the module off (07 §2.4.3 p.4) is never silent: «Снять» releases every block through the writer and
drops the module's twin map once no substitution uses it (so the core writes the base squads back even after
the module is off); «Оставить» keeps the substitutions — the core keeps applying them and freezes the
squads of those subscriptions until the module is back.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import sqlalchemy as sa

from svbg.core.clock import now
from svbg.ext.api import SlotButton, SlotResult
from svbg.ext.lte import packs
from svbg.ext.lte.decide import Release
from svbg.ext.lte.enforce import MODULE, manual_candidate
from svbg.ext.lte.model import ENFORCE_MODES
from svbg.ext.lte.notify import fmt_date, fmt_gb, group_name
from svbg.ext.lte.planner import Preview, preview
from svbg.ext.lte.service import (
    K_ENABLED,
    K_ENFORCE,
    audit,
    kv_get,
    kv_put,
    load_model,
    load_subjects,
)
from svbg.ext.lte.tables import (
    lte_blocks,
    lte_credits,
    lte_group_nodes,
    lte_groups,
    lte_overrides,
    lte_period_usage,
    lte_periods,
    lte_twins,
)
from svbg.subscriptions.tables import panel_squad_substitutions, panel_squad_twins, subscriptions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.ext.api import SlotCall
    from svbg.ext.lte.packs import PackFacts
    from svbg.ext.lte.service import LteService

__all__ = [
    "CODE_WORD",
    "PERM_CONFIG",
    "PERM_USERS",
    "PERM_VIEW",
    "SCREEN",
    "SCREEN_GROUP",
    "AdminCard",
    "LteAdmin",
    "Result",
    "can",
    "install",
    "load_card",
    "parse_limit",
    "render_card",
]

log = logging.getLogger("svbg.ext.lte.admin")

PERM_VIEW: Final = "lte.view"
PERM_USERS: Final = "lte.users"
PERM_CONFIG: Final = "lte.config"
SCREEN: Final = "lte"
SCREEN_GROUP: Final = "lte_grp"
FORM_GB: Final = "lte.gb"
FORM_UNBLOCK: Final = "lte.unblock"
REASON_PROMPT: Final = "Причина (для журнала админов):"
FORM_LIMIT: Final = "lte.limit"
FORM_RELEASE: Final = "lte.release_all"
CODE_WORD: Final = "СНЯТЬ"
_ID_RE: Final = re.compile(r"^\d{1,18}$")
_APPLY_RE: Final = re.compile(r"^(\d{1,12}):([dt]):(u|\d{1,6}):(\d{1,9})$")
_WHICH: Final[Mapping[str, str]] = {"d": "default", "t": "trial"}

T: Final[Mapping[str, str]] = {
    "denied": "Недостаточно прав.",
    "bad": "Неверные данные.",
    "no_sub": "Подписка не найдена.",
    "no_period": "Нет живого периода LTE — действие не к чему применить.",
    "exempt_no_block": "У подписки исключение — блок не ставится.",
    "exempt_no_gb": "У подписки исключение — гигабайты не нужны.",
    "gb_max": "Больше {max} ГБ за раз добавляет только владелец.",
    "gb_done": "✅ Добавлено {gb} ГБ до сброса {reset}.{released}",
    "released_suffix": " Блок снят.",
    "unblock_done": "✅ Блок снят до сброса {reset}. Новых блоков до сброса не будет.",
    "unblock_none": "✅ Блока нет. До сброса {reset} блоков не будет.",
    "block_done": "🚫 Заблокировано до сброса {reset}.",
    "block_exists": "Блок уже стоит.",
    "block_mode": "Применение сейчас «{mode}» — блок в панели не поставить.",
    "block_twin": "Блок неприменим: у сквада подписки нет годного двойника.",
    "exempt_on": "♾ Исключение включено, блоки сняты.",
    "exempt_off": "Исключение выключено.",
    "exempt_same": "Ничего не изменилось.",
    "limit_stale": "Группа изменилась, пока вы смотрели превью — откройте её заново.",
    "limit_done": "✅ Лимит сохранён. Решения применятся в ближайшем цикле учёта.",
    "mode_done": "✅ Применение: {mode}.",
    "no_settings": "Настройки сейчас недоступны.",
    "settings_failed": "Не сохранилось: {reason}",
    "off_release": "✅ Блоки сняты ({n}), модуль выключен.",
    "off_keep": "✅ Модуль выключен, блоки оставлены ({n}). Сквады этих подписок заморожены до включения.",
    "release_done": "✅ Сняты все блоки LTE: {n}.",
    "quarantine_done": "✅ Волна подтверждена: блоки поставятся в ближайшем цикле.",
    "incident_done": "✅ Инцидент снят: новые блоки снова разрешены.",
    "card": "🌐 <b>Квота LTE</b>: {used} из {limit} ГБ (база {base} + пакеты {credits} − запас {margin}) · "
    "сброс {reset} · якорь: {anchor}",
    "card_unlimited": "🌐 <b>Квота LTE</b>: без ограничения · израсходовано {used} ГБ",
    "card_zero": "🌐 <b>Квота LTE</b>: недоступно (лимит 0)",
    "card_blocked": "🚫 блок: {reason}",
    "card_exempt": "♾ исключение",
    "btn_gb": "➕ ГБ до сброса",
    "btn_unblock": "🔓 Снять блок до сброса",
    "btn_block": "🚫 Заблокировать до сброса",
    "btn_exempt_on": "♾ Исключение: включить",
    "btn_exempt_off": "♾ Исключение: выключить",
}
ANCHORS: Final[Mapping[str, str]] = {
    "paid": "оплата",
    "admin": "админ",
    "manual": "вручную",
    "trial": "пробный",
    "provisional": "предварительный",
    "import": "импорт",
}
MODES: Final[Mapping[str, str]] = {"on": "включено", "shadow": "тень", "off": "выключено"}


@dataclass(frozen=True, slots=True)
class Result:
    ok: bool
    text: str


def _deny() -> Result:
    return Result(False, T["denied"])


def can(user: Any, perm: str) -> bool:
    """``lte.config`` — the owner or an explicit grant (``*`` does not grant it); others — ``has_perm``;
    ``lte.view`` — also every Support+ (only the lines of the user card)."""
    role = getattr(user, "role", "user")
    perms = getattr(user, "perms", frozenset()) or frozenset()
    if role == "owner":
        return True
    if perm == PERM_CONFIG:
        return role == "admin" and PERM_CONFIG in perms
    if perm == PERM_VIEW and role in ("support", "admin"):
        return True
    check = getattr(user, "has_perm", None)
    try:
        return bool(check(perm)) if callable(check) else False
    except Exception:  # noqa: BLE001 - a broken user context gets nothing
        return False


def _actor(user: Any) -> int | None:
    value = getattr(user, "user_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _reason(reason: str | None, default: str) -> str:
    """The admin's reason for ``admin_audit`` (spaces collapsed, ≤ 300 chars); ``default`` when not given."""
    text = " ".join((reason or "").split())[:300]
    return text or default


def parse_limit(text: str) -> int | None:
    """``"50"`` → 50 GB, ``"0"`` → unavailable, ``"∞"`` / ``"без лимита"`` → ``None`` (unlimited)."""
    from svbg.tg.ui.forms import ValidationError

    raw = text.strip().lower().replace(" ", "")
    if raw in ("∞", "inf", "безлимита", "безлимит", "unlimited", "-"):
        return None
    if not raw.isdigit() or len(raw) > 6:
        raise ValidationError("Целое число ГБ (0 — недоступно) или ∞")
    return int(raw)


# ------------------------------------------------------------------------------------------ the service


class LteAdmin:
    """Admin operations over :class:`~svbg.ext.lte.service.LteService` (rights are checked here too)."""

    def __init__(self, service: LteService) -> None:
        self.svc = service

    # ------------------------------------------------------------------------------------- helpers

    async def _lock(self, conn: AsyncConnection, sid: int) -> Mapping[str, Any] | None:
        return (
            (
                await conn.execute(
                    sa.select(
                        subscriptions.c.id, subscriptions.c.panel_user_id, subscriptions.c.desired_squads
                    )
                    .where(subscriptions.c.id == sid)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    async def _facts(conn: AsyncConnection, sid: int, at: datetime) -> PackFacts | None:
        found = await packs.load_facts(conn, sid=sid, at=at)
        return next((f for f in found if f.rights), found[0] if found else None)

    async def _locked(
        self, conn: AsyncConnection, sid: int, at: datetime
    ) -> tuple[Mapping[str, Any] | None, PackFacts | None]:
        row = await self._lock(conn, sid)
        if row is None:
            return None, None
        return row, await self._facts(conn, sid, at)

    # ------------------------------------------------------------------------------- user actions

    async def add_gb(self, sid: int, gb: int, *, actor: Any, reason: str | None = None) -> Result:
        if not can(actor, PERM_USERS):
            return _deny()
        cfg = self.svc.cfg()
        if isinstance(gb, bool) or not isinstance(gb, int) or gb < 1 or gb > 100_000:
            return Result(False, T["bad"])
        if gb > cfg.admin_gb_max and getattr(actor, "role", "") != "owner":
            return Result(False, T["gb_max"].format(max=cfg.admin_gb_max))
        at = now()
        async with self.svc.db.tx() as conn:
            row, f = await self._locked(conn, sid, at)
            if row is None:
                return Result(False, T["no_sub"])
            if f is None or f.period_id is None or f.period_state not in ("open", "deferred"):
                return Result(False, T["no_period"])
            if f.exempt:
                return Result(False, T["exempt_no_gb"])
            await conn.execute(
                sa.insert(lte_credits).values(
                    subscription_id=sid,
                    group_id=f.group_id,
                    period_id=f.period_id,
                    bytes=gb * cfg.gb_bytes,
                    source="admin",
                    status="active",
                    amount_minor=0,
                )
            )
            await audit(
                conn,
                _actor(actor),
                "lte.add_gb",
                f"sub:{sid}",
                reason=_reason(reason, "+ГБ до сброса"),
                details={"gb": gb, "group_id": f.group_id, "period_id": f.period_id},
                role=getattr(actor, "role", None),
            )
            applied = await self.svc.decide_for(conn, [sid], at=at)
        await self.svc._after(applied)
        suffix = T["released_suffix"] if applied.released else ""
        return Result(True, T["gb_done"].format(gb=gb, reset=fmt_date(f.planned_end_at), released=suffix))

    async def unblock(self, sid: int, *, actor: Any, reason: str | None = None) -> Result:
        """«Снять блок до сброса»: release + ``no_block`` for the rest of the period (admin's ``reason``)."""
        if not can(actor, PERM_USERS):
            return _deny()
        why = _reason(reason, "снять блок до сброса")
        at = now()
        async with self.svc.db.tx() as conn:
            row, f = await self._locked(conn, sid, at)
            if row is None:
                return Result(False, T["no_sub"])
            if f is None or f.period_id is None:
                return Result(False, T["no_period"])
            await conn.execute(
                sa.insert(lte_overrides).values(
                    subscription_id=sid,
                    group_id=f.group_id,
                    kind="no_block",
                    period_id=f.period_id,
                    reason=why,
                    actor_id=_actor(actor),
                )
            )
            b = lte_blocks.c
            live = (
                await conn.execute(
                    sa.select(b.id, b.mode).where(
                        b.subscription_id == sid, b.group_id == f.group_id, b.status == "active"
                    )
                )
            ).first()
            released = False
            if live is not None:
                rel = Release(
                    int(live.id),
                    sid,
                    int(row["panel_user_id"] or 0),
                    f.group_id,
                    "admin",
                    live.mode == "shadow",
                )
                released = await self.svc.enforcer.release(conn, rel, at=at)
            await audit(
                conn,
                _actor(actor),
                "lte.unblock",
                f"sub:{sid}",
                reason=why,
                details={"released": released, "period_id": f.period_id},
                role=getattr(actor, "role", None),
            )
        reset = fmt_date(f.planned_end_at)
        return Result(True, (T["unblock_done"] if released else T["unblock_none"]).format(reset=reset))

    async def block(self, sid: int, *, actor: Any) -> Result:
        """«Заблокировать до сброса»: a manual block (released only by an admin or at the reset)."""
        if not can(actor, PERM_USERS):
            return _deny()
        cfg = self.svc.cfg()
        if cfg.mode != "on":
            return Result(False, T["block_mode"].format(mode=MODES.get(cfg.mode, cfg.mode)))
        at = now()
        async with self.svc.db.tx() as conn:
            row, f = await self._locked(conn, sid, at)
            if row is None:
                return Result(False, T["no_sub"])
            if f is None or f.period_id is None or f.period_state not in ("open", "deferred"):
                return Result(False, T["no_period"])
            if f.exempt:
                return Result(False, T["exempt_no_block"])
            if f.block_status in ("active", "releasing"):
                return Result(False, T["block_exists"])
            o = lte_overrides.c
            await conn.execute(
                sa.update(lte_overrides)
                .where(
                    o.subscription_id == sid,
                    o.kind == "no_block",
                    o.revoked_at.is_(None),
                    sa.or_(o.group_id.is_(None), o.group_id == f.group_id),
                )
                .values(revoked_at=at, revoke_reason="manual_block")
            )
            model = await load_model(conn, at=at)
            topo = self.svc.topology
            cand = manual_candidate(
                subscription_id=sid,
                panel_user_id=int(row["panel_user_id"] or 0),
                group_id=f.group_id,
                period_id=int(f.period_id),
                used=f.used_bytes,
                limit=int(f.limit.limit or 0),
                mode="enforce",
            )
            placed = await self.svc.enforcer.block(
                conn,
                cand,
                desired=[str(x) for x in (row["desired_squads"] or ())],
                twins=model.twins,
                squad_inbounds=topo.squad_inbounds if topo is not None else None,
                group_tags=topo.group_tags(model.group_nodes) if topo is not None else None,
                at=at,
            )
            if placed is None:
                return Result(False, T["block_twin"])
            await audit(
                conn,
                _actor(actor),
                "lte.block",
                f"sub:{sid}",
                reason="заблокировать до сброса",
                details={"block_id": placed.block_id, "period_id": f.period_id},
                role=getattr(actor, "role", None),
            )
        return Result(True, T["block_done"].format(reset=fmt_date(f.planned_end_at)))

    async def set_exempt(self, sid: int, on: bool, *, actor: Any) -> Result:
        if not can(actor, PERM_USERS):
            return _deny()
        at = now()
        o = lte_overrides.c
        async with self.svc.db.tx() as conn:
            row = await self._lock(conn, sid)
            if row is None:
                return Result(False, T["no_sub"])
            live = (
                await conn.execute(
                    sa.select(o.id).where(
                        o.subscription_id == sid, o.kind == "exempt", o.revoked_at.is_(None)
                    )
                )
            ).first()
            if on == (live is not None):
                return Result(True, T["exempt_same"])
            if on:
                await conn.execute(
                    sa.insert(lte_overrides).values(
                        subscription_id=sid,
                        kind="exempt",
                        exempt_kind="manual",
                        reason="исключение из карточки",
                        actor_id=_actor(actor),
                    )
                )
            else:
                await conn.execute(
                    sa.update(lte_overrides)
                    .where(o.subscription_id == sid, o.kind == "exempt", o.revoked_at.is_(None))
                    .values(revoked_at=at, revoke_reason="admin")
                )
            await audit(
                conn,
                _actor(actor),
                "lte.exempt_on" if on else "lte.exempt_off",
                f"sub:{sid}",
                reason="исключение из карточки",
                role=getattr(actor, "role", None),
            )
            applied = await self.svc.decide_for(conn, [sid], at=at)
        await self.svc._after(applied)
        return Result(True, T["exempt_on"] if on else T["exempt_off"])

    # ------------------------------------------------------------------------------ configuration

    async def preview_limit(self, group_id: int, which: str, value_gb: int | None) -> Preview | None:
        """What a new limit would do — the same planner as the cycle (05 §2.1.6)."""
        if which not in ("default", "trial"):
            return None
        cfg = self.svc.cfg()
        at = now()
        async with self.svc.db.read() as conn:
            model = await load_model(conn, at=at)
            group = model.groups.get(group_id)
            if group is None:
                return None
            subjects = await load_subjects(conn, model, at=at, topology=self.svc.topology)
        rows = dict(group.limit_rows)
        rows[which] = None if value_gb is None else value_gb * cfg.gb_bytes
        changed = replace(group, limit_rows=rows)
        groups = [changed.input() if g.id == group_id else g.input() for g in model.groups.values()]
        return preview(
            groups=groups,
            subjects=[row.subject for row in subjects.values()],
            settings=replace(cfg.enforce, mode="on") if cfg.mode == "shadow" else cfg.enforce,
            now=at,
            params=cfg.planner,
        )

    async def apply_limit(
        self, group_id: int, which: str, value_gb: int | None, version: int, *, actor: Any
    ) -> Result:
        if not can(actor, PERM_CONFIG):
            return _deny()
        if which not in ("default", "trial"):
            return Result(False, T["bad"])
        value = None if value_gb is None else value_gb * self.svc.cfg().gb_bytes
        g = lte_groups.c
        values: dict[str, Any] = (
            {"has_default": True, "limit_default_bytes": value}
            if which == "default"
            else {"has_trial": True, "limit_trial_bytes": value}
        )
        async with self.svc.db.tx() as conn:
            done = (
                await conn.execute(
                    sa.update(lte_groups)
                    .where(g.id == group_id, g.version == version)
                    .values(**values, version=g.version + 1, updated_at=now())
                    .returning(g.id)
                )
            ).first()
            if done is None:
                return Result(False, T["limit_stale"])
            await audit(
                conn,
                _actor(actor),
                "lte.limit",
                f"group:{group_id}",
                reason=f"лимит {which}",
                details={"which": which, "bytes": value},
                role=getattr(actor, "role", None),
            )
        _wake()
        return Result(True, T["limit_done"])

    async def set_mode(self, mode: str, *, actor: Any) -> Result:
        if not can(actor, PERM_CONFIG):
            return _deny()
        if mode not in ENFORCE_MODES:
            return Result(False, T["bad"])
        res = await self._apply_settings({K_ENFORCE: mode}, actor)
        if res is not None:
            return res
        _wake()
        return Result(True, T["mode_done"].format(mode=MODES[mode]))

    async def _apply_settings(self, changes: Mapping[str, Any], actor: Any) -> Result | None:
        settings = self.svc.settings
        if settings is None:
            return Result(False, T["no_settings"])
        from svbg.core.settings.service import Change

        result = await settings.apply(
            [Change(k, v) for k, v in changes.items()], source="bot", actor_id=_actor(actor)
        )
        if not result.ok:
            return Result(False, T["settings_failed"].format(reason="; ".join(result.rejected.values())))
        return None

    async def switch_off(self, action: Literal["release", "keep"], *, actor: Any) -> Result:
        """Turn the module off with the owner's explicit choice (07 §2.4.3 p.4)."""
        if not can(actor, PERM_CONFIG):
            return _deny()
        if action not in ("release", "keep"):
            return Result(False, T["bad"])
        n = 0
        if action == "release":
            n = await self.svc.release_all("feature_off", actor_id=_actor(actor))
            async with self.svc.db.tx() as conn:
                await drop_unused_twin_map(conn)
        else:
            async with self.svc.db.read() as conn:
                n = int(
                    await conn.scalar(
                        sa.select(
                            sa.func.count(sa.distinct(panel_squad_substitutions.c.subscription_id))
                        ).where(panel_squad_substitutions.c.owner_module == MODULE)
                    )
                    or 0
                )
        async with self.svc.db.tx() as conn:
            await kv_put(conn, "off_choice", {"action": action, "at": now().isoformat(), "n": n})
            await audit(
                conn,
                _actor(actor),
                "lte.switch_off",
                "lte",
                reason="снять" if action == "release" else "оставить",
                details={"action": action, "n": n},
                role=getattr(actor, "role", None),
            )
        res = await self._apply_settings({K_ENABLED: False}, actor)
        if res is not None:
            return res
        return Result(True, (T["off_release"] if action == "release" else T["off_keep"]).format(n=n))

    async def release_all(self, *, actor: Any) -> Result:
        """«⛔ Снять все блоки LTE» (after the code word)."""
        if not can(actor, PERM_CONFIG):
            return _deny()
        n = await self.svc.release_all("emergency", actor_id=_actor(actor))
        return Result(True, T["release_done"].format(n=n))

    async def confirm_quarantine(self, *, actor: Any) -> Result:
        if not can(actor, PERM_CONFIG):
            return _deny()
        async with self.svc.db.tx() as conn:
            state = await kv_get(conn, "fuses")
            state["cleared"] = sorted({int(g) for g in state.get("quarantine", [])})
            await kv_put(conn, "fuses", state)
            await audit(conn, _actor(actor), "lte.quarantine_confirm", "lte", reason="подтверждение волны")
        await self._resolve("lte:quarantine")
        _wake()
        return Result(True, T["quarantine_done"])

    async def clear_incidents(self, *, actor: Any) -> Result:
        if not can(actor, PERM_CONFIG):
            return _deny()
        async with self.svc.db.tx() as conn:
            state = await kv_get(conn, "fuses")
            state["incidents"] = {}
            await kv_put(conn, "fuses", state)
            await audit(conn, _actor(actor), "lte.incident_clear", "lte", reason="история панели проверена")
        await self._resolve("lte:incident")
        return Result(True, T["incident_done"])

    async def _resolve(self, key: str) -> None:
        attention = self.svc.attention
        if attention is None:
            return
        try:
            await attention.auto_resolve(key, keep=())
        except Exception:
            log.exception("lte: could not resolve %s", key)

    # ------------------------------------------------------------------------------------- overview

    async def overview(self) -> Overview:
        cfg = self.svc.cfg()
        b, g = lte_blocks.c, lte_groups.c
        pu, p = lte_period_usage.c, lte_periods.c
        o = lte_overrides.c
        async with self.svc.db.read() as conn:
            cycle = await kv_get(conn, "cycle")
            fuses = await kv_get(conn, "fuses")
            counts = (
                await conn.execute(
                    sa.select(
                        sa.select(sa.func.count())
                        .where(b.status == "active", b.mode == "enforce")
                        .scalar_subquery()
                        .label("blocked"),
                        sa.select(sa.func.count(sa.distinct(o.subscription_id)))
                        .where(o.kind == "exempt", o.revoked_at.is_(None))
                        .scalar_subquery()
                        .label("exempt"),
                        sa.select(sa.func.count())
                        .select_from(
                            lte_period_usage.join(lte_periods, p.id == pu.period_id).join(
                                lte_groups, g.id == pu.group_id
                            )
                        )
                        .where(
                            p.state != "closed",
                            g.limit_default_bytes > 0,
                            pu.used_bytes * 100 >= g.limit_default_bytes * cfg.warn_percent,
                        )
                        .scalar_subquery()
                        .label("warn"),
                    )
                )
            ).one()
            groups = (await conn.execute(sa.select(lte_groups).order_by(g.sort, g.id))).mappings().all()
        return Overview(
            cycle=cycle,
            quarantine=tuple(int(x) for x in fuses.get("quarantine", [])),
            incidents=tuple(str(k) for k in (fuses.get("incidents") or {})),
            blocked=int(counts.blocked or 0),
            exempt=int(counts.exempt or 0),
            warn=int(counts.warn or 0),
            groups=tuple(dict(r) for r in groups),
        )


@dataclass(frozen=True, slots=True)
class Overview:
    cycle: Mapping[str, Any]
    quarantine: tuple[int, ...]
    incidents: tuple[str, ...]
    blocked: int
    exempt: int
    warn: int
    groups: tuple[Mapping[str, Any], ...]


async def drop_unused_twin_map(conn: AsyncConnection) -> int:
    """Remove the module's rows of the core twin map that no live substitution uses (after «Снять»)."""
    t = panel_squad_twins
    used = sa.select(panel_squad_substitutions.c.substitute_squad_uuid).where(
        panel_squad_substitutions.c.owner_module == MODULE
    )
    result = await conn.execute(
        sa.delete(t).where(t.c.owner_module == MODULE, t.c.substitute_squad_uuid.not_in(used))
    )
    return int(result.rowcount or 0)


def _wake() -> None:
    """Run the cycle soon after a configuration change (best effort)."""
    from svbg.ext.lte.service import RUNTIME

    ctx = RUNTIME.ctx
    if ctx is None:
        return
    try:
        ctx.wake("cycle")
    except Exception:
        log.debug("lte: wake failed", exc_info=True)


# -------------------------------------------------------------------------------------------- user card


@dataclass(frozen=True, slots=True)
class AdminCard:
    facts: tuple[PackFacts, ...]
    subscription_id: int | None = None


async def load_card(conn: AsyncConnection, user: Any, view: Mapping[str, Any]) -> AdminCard | None:
    """``admin.user_card`` read model: the viewed user's subscription × LTE groups (one query)."""
    del user
    target = view.get("user_id") if isinstance(view, Mapping) else None
    if target is None and isinstance(view, Mapping):
        target = view.get("target_user_id")
    if isinstance(target, bool) or not isinstance(target, int):
        return None
    facts = await packs.load_facts(conn, user_id=target)
    if not facts:
        return None
    return AdminCard(tuple(facts), facts[0].subscription_id)


def card_lines(f: PackFacts, gb: int) -> list[str]:
    if f.exempt or f.limit.unlimited:
        lines = [T["card_unlimited"].format(used=fmt_gb(f.used_bytes, gb))]
    elif f.limit.zero:
        lines = [T["card_zero"]]
    else:
        lines = [
            T["card"].format(
                used=fmt_gb(f.used_bytes, gb),
                limit=fmt_gb(f.limit.limit, gb),
                base=fmt_gb(f.limit.base, gb),
                credits=fmt_gb(f.limit.credits, gb),
                margin=fmt_gb(f.limit.margin, gb),
                reset=fmt_date(f.planned_end_at),
                anchor=ANCHORS.get(str(f.anchor_kind), "—"),
            )
        ]
    if f.block_status in ("active", "releasing"):
        from svbg.ext.lte.notify import REASONS

        lines.append(T["card_blocked"].format(reason=REASONS.get(str(f.block_reason), str(f.block_reason))))
    if f.exempt:
        lines.append(T["card_exempt"])
    return lines


def render_card(call: SlotCall) -> SlotResult | None:
    """``admin.user_card.sections`` (perm ``lte.view``); the buttons need ``lte.users``."""
    model = call.model
    if not isinstance(model, AdminCard):
        return None
    from svbg.ext.lte.service import LteConfig

    try:
        cfg = LteConfig.from_snapshot(call.module.config())
    except Exception:  # noqa: BLE001
        cfg = LteConfig()
    f = next((x for x in model.facts if x.rights and x.period_id is not None), None)
    if f is None:
        return None
    lines = card_lines(f, cfg.gb_bytes)
    sid = str(f.subscription_id)
    buttons: list[SlotButton] = []
    if not f.exempt:
        buttons.append(SlotButton(T["btn_gb"], action="lte.gb", arg=sid, perm=PERM_USERS))
        if f.blocked:
            buttons.append(SlotButton(T["btn_unblock"], action="lte.unblock", arg=sid, perm=PERM_USERS))
        else:
            buttons.append(
                SlotButton(T["btn_block"], action="lte.block", arg=sid, perm=PERM_USERS, style="danger")
            )
        buttons.append(SlotButton(T["btn_exempt_on"], action="lte.exon", arg=sid, perm=PERM_USERS))
    else:
        buttons.append(SlotButton(T["btn_exempt_off"], action="lte.exoff", arg=sid, perm=PERM_USERS))
    return SlotResult(lines=tuple(lines), buttons=tuple(buttons))


def render_home_entry(call: SlotCall) -> SlotResult | None:
    """``admin.home.entries``: «🌐 Трафик LTE» for Admin and Owner."""
    if not getattr(call.user, "at_least", lambda _r: False)("admin"):
        return None
    return SlotResult(buttons=(SlotButton("🌐 Трафик LTE", action="lte.open"),))


# ----------------------------------------------------------------------------------------------- screens


def overview_text(ov: Overview, cfg: Any, *, at: datetime, title: str = "🌐 <b>Трафик LTE</b>") -> str:
    lines = [title, ""]
    mode = MODES.get(cfg.mode, cfg.mode)
    off = "снять блоки" if cfg.off_action == "release" else "оставить блоки"
    lines.append(f"Применение: {mode} · при выключении: {off}")
    raw = ov.cycle.get("at")
    if raw:
        try:
            age = int((at - datetime.fromisoformat(str(raw))).total_seconds() // 60)
            state = "ok" if ov.cycle.get("ok") else "неполный"
            lines.append(f"Цикл учёта: {age} мин назад · {state}")
        except ValueError:
            lines.append("Цикл учёта: —")
    else:
        lines.append("Цикл учёта: ещё не было")
    for g in ov.groups:
        limit = "∞" if g["limit_default_bytes"] is None else fmt_gb(g["limit_default_bytes"], cfg.gb_bytes)
        lines.append(f"Группа «{group_name(g['name'])}»: {limit} ГБ · {g['state']}")
    lines.append(f"Заблокировано: {ov.blocked} · ≥{cfg.warn_percent}%: ≈{ov.warn} · исключений: {ov.exempt}")
    if ov.quarantine:
        lines.append("⚠️ Карантин новых блоков — нужна проверка и подтверждение.")
    if ov.incidents:
        lines.append("⚠️ История панели пропала или откатилась — новые блоки остановлены.")
    return "\n".join(lines)


def _sid(arg: Any) -> int | None:
    raw = str(arg or "")
    return int(raw) if _ID_RE.match(raw) else None


def install(router: Any, service: Callable[[], LteService]) -> None:
    """Admin screens ``lte`` / ``lte_grp``, forms and the ``mod`` actions of the user card section."""
    from svbg.tg.admin import nav
    from svbg.tg.ui.forms import Field, Form, ValidationError, integer
    from svbg.tg.ui.forms import text as text_field
    from svbg.tg.ui.renderer import MODULE_SCREEN, nav_button
    from svbg.tg.ui.view import Redirect, Toast, View

    def admin() -> LteAdmin:
        return LteAdmin(service())

    def back(screen: str = SCREEN, arg: str | None = None) -> list[Any]:
        return [nav_button("◀️ Назад", screen, arg=arg), nav_button("🛠 Админка", nav.ROOT)]

    def result_view(res: Result, screen: str = SCREEN, arg: str | None = None) -> Any:
        return View(text=res.text, parse_mode="HTML", keyboard=[back(screen, arg)])

    @router.screen(SCREEN, required_role="admin")
    async def lte_screen(ctx: Any, arg: Any) -> Any:
        del arg
        svc = service()
        cfg = svc.cfg()
        ov = await admin().overview()
        rows: list[list[Any]] = []
        if can(ctx.user, PERM_CONFIG):
            rows.append(
                [
                    nav_button(("• " if cfg.mode == m else "") + MODES[m], SCREEN, "mode", m)
                    for m in ("on", "shadow", "off")
                ]
            )
        for g in ov.groups:
            rows.append([nav_button(f"Группа «{group_name(g['name'])}»", SCREEN_GROUP, arg=str(g["id"]))])
        if can(ctx.user, PERM_CONFIG):
            if ov.quarantine:
                rows.append([nav_button("✅ Подтвердить волну блоков", SCREEN, "qok")])
            if ov.incidents:
                rows.append([nav_button("🧹 История проверена — снять инцидент", SCREEN, "inc")])
            rows.append([nav_button("⛔ Снять все блоки LTE", SCREEN, "rall", style="danger")])
            rows.append([nav_button("⏻ Выключить модуль…", SCREEN, "off")])
        if ctx.user.role == "owner" and nav.has_screen(router, "set.v"):
            rows.append([nav_button("⚙️ Настройки", "set.v", arg="mod.lte")])
        rows.append(nav.back_row(SCREEN))
        text = overview_text(ov, cfg, at=now(), title=nav.header(SCREEN))
        return View(text=text, parse_mode="HTML", keyboard=rows)

    @router.action(SCREEN, "mode", required_role="admin")
    async def set_mode(ctx: Any, arg: Any) -> Any:
        res = await admin().set_mode(str(arg or ""), actor=ctx.user)
        return Redirect(SCREEN, toast=res.text) if res.ok else Toast(res.text, alert=True)

    @router.action(SCREEN, "qok", required_role="admin")
    async def quarantine_ok(ctx: Any, arg: Any) -> Any:
        del arg
        res = await admin().confirm_quarantine(actor=ctx.user)
        return Redirect(SCREEN, toast=res.text) if res.ok else Toast(res.text, alert=True)

    @router.action(SCREEN, "inc", required_role="admin")
    async def incident_ok(ctx: Any, arg: Any) -> Any:
        del arg
        res = await admin().clear_incidents(actor=ctx.user)
        return Redirect(SCREEN, toast=res.text) if res.ok else Toast(res.text, alert=True)

    @router.action(SCREEN, "off", required_role="admin")
    async def off_menu(ctx: Any, arg: Any) -> Any:
        if not can(ctx.user, PERM_CONFIG):
            return Toast(T["denied"], alert=True)
        choice = str(arg or "")
        if choice in ("release", "keep"):
            res = await admin().switch_off(choice, actor=ctx.user)  # type: ignore[arg-type]
            return result_view(res)
        text = (
            "⏻ <b>Выключить учёт LTE?</b>\n"
            "«Снять» — все блоки снимаются через панель, пользователи сразу получают LTE.\n"
            "«Оставить» — блоки остаются как есть, сквады этих подписок заморожены до включения."
        )
        rows = [
            [nav_button("🔓 Снять блоки и выключить", SCREEN, "off", "release", style="danger")],
            [nav_button("🔒 Оставить блоки и выключить", SCREEN, "off", "keep")],
            back(),
        ]
        return View(text=text, parse_mode="HTML", keyboard=rows)

    async def release_done(ctx: Any, data: dict[str, Any]) -> Any:
        del data
        return result_view(await admin().release_all(actor=ctx.user))

    def code_word(value: str) -> str:
        if value.strip().upper() != CODE_WORD:
            raise ValidationError(f"Введите {CODE_WORD} — или «Отмена»")
        return CODE_WORD

    router.form(
        Form(
            FORM_RELEASE,
            (Field("code", f"⛔ Снять все блоки LTE. Чтобы подтвердить, введите {CODE_WORD}.", code_word),),
            release_done,
            required_role="owner",
        )
    )

    @router.action(SCREEN, "rall", required_role="admin")
    async def release_all(ctx: Any, arg: Any) -> Any:
        del arg
        if not can(ctx.user, PERM_CONFIG):
            return Toast(T["denied"], alert=True)
        return await ctx.start_form(FORM_RELEASE)

    # -------------------------------------------------------------------------------- group card

    @router.screen(SCREEN_GROUP, required_role="admin")
    async def group_screen(ctx: Any, arg: Any) -> Any:
        gid = _sid(arg)
        if gid is None:
            return Redirect(SCREEN)
        svc = service()
        cfg = svc.cfg()
        async with svc.db.read() as conn:
            g = (await conn.execute(sa.select(lte_groups).where(lte_groups.c.id == gid))).mappings().first()
            if g is None:
                return Redirect(SCREEN, toast="Группа не найдена")
            n = lte_group_nodes.c
            nodes = (
                (
                    await conn.execute(
                        sa.select(n.node_uuid)
                        .where(n.group_id == gid, n.counted_to.is_(None))
                        .order_by(n.node_uuid)
                    )
                )
                .scalars()
                .all()
            )
            twins = (
                (await conn.execute(sa.select(lte_twins).where(lte_twins.c.group_id == gid))).mappings().all()
            )
        names = svc.topology.nodes if svc.topology is not None else {}
        squads = svc.topology.squad_names if svc.topology is not None else {}

        def limit_text(has: bool, value: int | None) -> str:
            if not has:
                return "не задан"
            return "∞" if value is None else f"{fmt_gb(value, cfg.gb_bytes)} ГБ"

        lines = [
            f"🌐 <b>Группа «{group_name(g['name'])}»</b> · {g['state']}",
            "Пример строки: └ 12,4 из "
            + limit_text(g["has_default"], g["limit_default_bytes"])
            + " · сброс 07.10",
            f"Лимит: {limit_text(g['has_default'], g['limit_default_bytes'])} · "
            f"пробный: {limit_text(g['has_trial'], g['limit_trial_bytes'])}",
            f"Запас: {fmt_gb(g['margin_bytes'], cfg.gb_bytes)} ГБ + {g['margin_pct']}%",
            "Ноды: " + (", ".join((names[x].name if x in names else x[:8]) for x in nodes) or "нет"),
        ]
        for t in twins:
            problem = f" ⚠️ {t['problem']}" if t["problem"] else ""
            base = squads.get(t["base_squad_uuid"], t["base_squad_uuid"][:8])
            twin = squads.get(t["twin_squad_uuid"], t["twin_squad_uuid"][:8])
            lines.append(f"Двойник: {base} → {twin}{problem}")
        rows: list[list[Any]] = []
        if can(ctx.user, PERM_CONFIG):
            ver = str(g["version"])
            rows.append(
                [
                    nav_button("✏️ Лимит", SCREEN_GROUP, "lim", f"{gid}:d:{ver}"),
                    nav_button("✏️ Пробный", SCREEN_GROUP, "lim", f"{gid}:t:{ver}"),
                ]
            )
        rows.append(back())
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    async def limit_done(ctx: Any, data: dict[str, Any]) -> Any:
        gid, which, ver = int(data["gid"]), str(data["which"]), int(data["ver"])
        value = data.get("value")
        pv = await admin().preview_limit(gid, _WHICH[which], value)
        if pv is None:
            return Redirect(SCREEN)
        hist = ", ".join(f"{k}%: {v}" for k, v in pv.histogram.items() if v)
        lines = [
            f"Превью: лимит {'∞' if value is None else f'{value} ГБ'}",
            f"Сейчас заблокировано {pv.blocked_now} → после: +{pv.new_blocks} блоков, −{pv.releases} разблок"
            + (f", ждут цикла: {pv.throttled}" if pv.throttled else ""),
            f"Распределение: {hist or 'нет данных'}",
        ]
        if pv.quarantine:
            lines.append("⚠️ Сработает карантин: блоки будут ждать подтверждения.")
        for r in pv.sample:
            act = "🚫" if r.action == "block" else "🔓"
            lines.append(f"{act} №{r.subscription_id}: {fmt_gb(r.used_bytes)} из {fmt_gb(r.limit_bytes)} ГБ")
        v = "u" if value is None else str(value)
        rows = [
            [nav_button("✅ Применить", SCREEN_GROUP, "apply", f"{gid}:{which}:{v}:{ver}", style="success")],
            back(SCREEN_GROUP, str(gid)),
        ]
        return View(text="\n".join(lines), parse_mode="HTML", keyboard=rows)

    def limit_value(value: str) -> Any:
        return parse_limit(value)

    router.form(
        Form(
            FORM_LIMIT,
            (Field("value", "Новый лимит в ГБ (0 — недоступно, ∞ — без лимита):", limit_value),),
            limit_done,
            required_role="admin",
        )
    )

    @router.action(SCREEN_GROUP, "lim", required_role="admin")
    async def limit_start(ctx: Any, arg: Any) -> Any:
        if not can(ctx.user, PERM_CONFIG):
            return Toast(T["denied"], alert=True)
        parts = str(arg or "").split(":")
        if (
            len(parts) != 3
            or not _ID_RE.match(parts[0])
            or parts[1] not in _WHICH
            or not _ID_RE.match(parts[2])
        ):
            return Redirect(SCREEN)
        return await ctx.start_form(
            FORM_LIMIT, {"gid": int(parts[0]), "which": parts[1], "ver": int(parts[2])}
        )

    @router.action(SCREEN_GROUP, "apply", required_role="admin")
    async def limit_apply(ctx: Any, arg: Any) -> Any:
        m = _APPLY_RE.match(str(arg or ""))
        if m is None:
            return Redirect(SCREEN)
        gid, which, raw, ver = int(m[1]), _WHICH[m[2]], m[3], int(m[4])
        res = await admin().apply_limit(gid, which, None if raw == "u" else int(raw), ver, actor=ctx.user)
        return result_view(res, SCREEN_GROUP, str(gid))

    # ------------------------------------------------------------------------- user card actions

    async def gb_done(ctx: Any, data: dict[str, Any]) -> Any:
        res = await admin().add_gb(
            int(data["sid"]), int(data["gb"]), actor=ctx.user, reason=data.get("reason")
        )
        return View(text=res.text, keyboard=[nav.back_to(nav.ROOT)])

    reason_field = Field("reason", REASON_PROMPT, text_field(min_len=3, max_len=300))
    router.form(
        Form(
            FORM_GB,
            (
                Field("gb", "Сколько ГБ добавить до сброса?", integer(min_value=1, max_value=100_000)),
                reason_field,
            ),
            gb_done,
            required_role="support",
        )
    )

    async def unblock_done(ctx: Any, data: dict[str, Any]) -> Any:
        res = await admin().unblock(int(data["sid"]), actor=ctx.user, reason=data.get("reason"))
        return View(text=res.text, keyboard=[nav.back_to(nav.ROOT)])

    router.form(Form(FORM_UNBLOCK, (reason_field,), unblock_done, required_role="support"))

    async def unblock_start(ctx: Any, sid: int) -> Any:
        if not can(ctx.user, PERM_USERS):
            return Toast(T["denied"], alert=True)
        return await ctx.start_form(FORM_UNBLOCK, {"sid": sid})

    async def gb_start(ctx: Any, sid: int) -> Any:
        if not can(ctx.user, PERM_USERS):
            return Toast(T["denied"], alert=True)
        return await ctx.start_form(FORM_GB, {"sid": sid})

    def mod(name: str, fn: Callable[[Any, int], Any]) -> None:
        async def handler(ctx: Any, arg: Any) -> Any:
            sid = _sid(arg)
            if sid is None:
                return Toast(T["bad"], alert=True)
            if not can(ctx.user, PERM_USERS):
                return Toast(T["denied"], alert=True)
            res = await fn(ctx, sid)
            return res if not isinstance(res, Result) else Toast(res.text, alert=True)

        router.action(MODULE_SCREEN, f"lte.{name}", required_role="support")(handler)

    @router.action(MODULE_SCREEN, "lte.open", required_role="admin")
    async def open_screen(ctx: Any, arg: Any) -> Any:
        del ctx, arg
        return Redirect(SCREEN)

    mod("gb", gb_start)
    mod("unblock", unblock_start)
    mod("block", lambda ctx, sid: admin().block(sid, actor=ctx.user))
    mod("exon", lambda ctx, sid: admin().set_exempt(sid, True, actor=ctx.user))
    mod("exoff", lambda ctx, sid: admin().set_exempt(sid, False, actor=ctx.user))


def sections() -> Sequence[tuple[str, str]]:
    return (("lte", "🌐 Трафик LTE"),)
