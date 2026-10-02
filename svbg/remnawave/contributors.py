"""Squad contributors (X1) applied by the core, fail-closed (07 §2.4.3).

Owner modules (LTE and the like) never compute ``activeInternalSquads`` themselves at write time. They store
their *decisions* as rows of the core tables:

* ``panel_squad_substitutions(subscription_id, base → substitute, owner_module)`` — "for this subscription use
  ``substitute`` instead of ``base``" (written in the same transaction as the module's own state change);
* ``panel_squad_twins(substitute → base, owner_module)`` — the global map used to translate the panel's
  squads back to plan squads before comparing (``reverse``).

The core applies them without any module code: the writer runs :meth:`SquadContributors.plan` (forward) before
PATCH, projection and reconciliation run :func:`reverse`. When the module that owns a live substitution is
``degraded`` or not loaded, the writer **does not touch** ``activeInternalSquads`` at all (other fields are
written) and «Требует внимания» gets «Модуль «X» недоступен: сквады N подписок заморожены». There is no path
that silently drops a substitution.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlalchemy as sa

from svbg.core.component import fix_screen
from svbg.subscriptions.tables import panel_squad_substitutions, panel_squad_twins

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from svbg.core.attention import AttentionService
    from svbg.db.engine import Database

__all__ = [
    "ATTENTION_PREFIX",
    "ModuleState",
    "SquadContributors",
    "SquadPlan",
    "Substitution",
    "forward",
    "reverse",
    "same_squads",
]

log = logging.getLogger("svbg.remnawave.contributors")

ATTENTION_PREFIX = "rw:squads_frozen:"

_TXT_TITLE = "Модуль «{module}» недоступен: сквады {n} подписок заморожены"
_TXT_BODY = (
    "Модуль «{module}» {state}, а у {n} подписок есть его замены сквадов. Бот продолжает продлевать и менять "
    "лимиты, но не трогает сквады этих подписок в панели, чтобы не снять блоки модуля. Смена сквадов "
    "применится сама, когда модуль снова заработает. Откройте «Состояние», чтобы проверить модуль."
)
_TXT_STATE = {"degraded": "работает с ошибками", "unloaded": "не загружен или выключен"}


class ModuleState(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNLOADED = "unloaded"


#: A module's liveness probe: ``ModuleState``/its string value, or ``bool`` (``True`` = ok).
StatusFn = Callable[[], "ModuleState | str | bool"]


@dataclass(frozen=True, slots=True)
class Substitution:
    base: str
    substitute: str
    owner_module: str


@dataclass(frozen=True, slots=True)
class SquadPlan:
    """What the writer may send as ``activeInternalSquads``.

    ``squads is None`` means "do not send the field" (fail-closed freeze, or nothing to send).
    """

    squads: list[str] | None
    frozen_modules: tuple[str, ...] = ()
    substitutions: tuple[Substitution, ...] = ()

    @property
    def frozen(self) -> bool:
        return bool(self.frozen_modules)


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def forward(base: Sequence[str], subs: Iterable[Substitution]) -> list[str]:
    """Plan squads → squads to send: every ``base`` with a substitution is replaced by its ``substitute``."""
    mapping = {s.base: s.substitute for s in subs}
    return _unique(mapping.get(squad, squad) for squad in base)


def reverse(squads: Sequence[str], twins: Mapping[str, str]) -> list[str]:
    """Panel squads → plan squads: every known twin is translated back to its base squad."""
    return _unique(twins.get(squad, squad) for squad in squads)


def same_squads(a: Sequence[str] | None, b: Sequence[str] | None) -> bool:
    """Order-insensitive comparison (the panel does not promise an order)."""
    return sorted(set(a or ())) == sorted(set(b or ()))


class SquadContributors:
    """Registry of module liveness + the core-side forward/reverse over the substitution tables."""

    def __init__(self, attention: AttentionService | None = None) -> None:
        self._attention = attention
        self._status: dict[str, StatusFn] = {}

    # ------------------------------------------------------------------------------------------ modules

    def register(self, module: str, status: StatusFn) -> Callable[[], None]:
        """A loaded module announces itself with a liveness probe. Returns ``unregister`` (= unloaded)."""
        if not module:
            raise ValueError("module name must not be empty")
        self._status[module] = status

        def unregister() -> None:
            if self._status.get(module) is status:
                del self._status[module]

        return unregister

    def state(self, module: str) -> ModuleState:
        """Liveness of ``module``. Not registered → ``UNLOADED``; a failing probe → ``DEGRADED``."""
        fn = self._status.get(module)
        if fn is None:
            return ModuleState.UNLOADED
        try:
            value = fn()
        except Exception:  # a broken probe is itself a sign of a broken module
            log.exception("module %s: liveness probe failed", module)
            return ModuleState.DEGRADED
        if isinstance(value, bool):
            return ModuleState.OK if value else ModuleState.DEGRADED
        try:
            return ModuleState(value)
        except ValueError:
            return ModuleState.DEGRADED

    # --------------------------------------------------------------------------------------------- data

    @staticmethod
    async def load(conn: AsyncConnection, subscription_id: int) -> list[Substitution]:
        t = panel_squad_substitutions
        rows = (
            await conn.execute(
                sa.select(t.c.base_squad_uuid, t.c.substitute_squad_uuid, t.c.owner_module)
                .where(t.c.subscription_id == subscription_id)
                .order_by(t.c.id)
            )
        ).all()
        return [Substitution(r[0], r[1], r[2]) for r in rows]

    @staticmethod
    async def load_many(
        conn: AsyncConnection, subscription_ids: Sequence[int]
    ) -> dict[int, list[Substitution]]:
        if not subscription_ids:
            return {}
        t = panel_squad_substitutions
        rows = (
            await conn.execute(
                sa.select(
                    t.c.subscription_id, t.c.base_squad_uuid, t.c.substitute_squad_uuid, t.c.owner_module
                )
                .where(t.c.subscription_id.in_(list(subscription_ids)))
                .order_by(t.c.id)
            )
        ).all()
        out: dict[int, list[Substitution]] = {}
        for sid, base, sub, owner in rows:
            out.setdefault(int(sid), []).append(Substitution(base, sub, owner))
        return out

    @staticmethod
    async def twins(conn: AsyncConnection) -> dict[str, str]:
        """``substitute → base`` (small: a handful of twin squads per module)."""
        t = panel_squad_twins
        rows = (await conn.execute(sa.select(t.c.substitute_squad_uuid, t.c.base_squad_uuid))).all()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    async def twin_owners(conn: AsyncConnection) -> dict[str, str]:
        """``substitute → owner_module``."""
        t = panel_squad_twins
        rows = (await conn.execute(sa.select(t.c.substitute_squad_uuid, t.c.owner_module))).all()
        return {r[0]: r[1] for r in rows}

    # ---------------------------------------------------------------------------------------- planning

    def plan(
        self,
        desired: Sequence[str],
        subs: Sequence[Substitution],
        *,
        panel_squads: Sequence[str] | None = None,
        twin_owners: Mapping[str, str] | None = None,
    ) -> SquadPlan:
        """Squads to send for ``desired`` plan squads, fail-closed on a degraded/unloaded owner module."""
        frozen = self.frozen_modules(subs, panel_squads=panel_squads, twin_owners=twin_owners)
        if frozen:
            return SquadPlan(None, frozen, tuple(subs))
        squads = forward(desired, subs)
        return SquadPlan(squads or None, (), tuple(subs))

    def frozen_modules(
        self,
        subs: Sequence[Substitution],
        *,
        panel_squads: Sequence[str] | None = None,
        twin_owners: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Modules that block squad writes for one subscription: owners of its live substitutions, and owners
        of twin squads the panel still shows (an unfinished decision of the module), when not ``OK``."""
        owners = {s.owner_module for s in subs}
        if panel_squads and twin_owners:
            owners.update(twin_owners[s] for s in panel_squads if s in twin_owners)
        return tuple(sorted(m for m in owners if self.state(m) is not ModuleState.OK))

    # --------------------------------------------------------------------------------------- attention

    async def raise_frozen(self, db: Database, modules: Iterable[str]) -> None:
        """Raise (or refresh) «сквады N подписок заморожены» for each module; never raises itself."""
        if self._attention is None:
            return
        for module in sorted(set(modules)):
            try:
                n = await self.count_subscriptions(db, module)
                state = self.state(module)
                await self._attention.raise_item(
                    ATTENTION_PREFIX + module,
                    "warn",
                    _TXT_TITLE.format(module=module, n=n),
                    _TXT_BODY.format(module=module, n=n, state=_TXT_STATE.get(state.value, state.value)),
                    fix_action=fix_screen("status"),
                )
            except Exception:  # attention is advisory; the write itself already happened
                log.exception("could not raise the frozen-squads item for module %s", module)

    async def refresh_attention(self, db: Database) -> list[str]:
        """Periodic check: raise for modules still frozen, resolve the rest. Returns frozen modules."""
        if self._attention is None:
            return []
        t = panel_squad_substitutions
        async with db.read() as conn:
            modules = [r[0] for r in (await conn.execute(sa.select(t.c.owner_module).distinct())).all()]
        frozen = [m for m in modules if self.state(m) is not ModuleState.OK]
        await self.raise_frozen(db, frozen)
        try:
            await self._attention.auto_resolve(ATTENTION_PREFIX, keep=[ATTENTION_PREFIX + m for m in frozen])
        except Exception:
            log.exception("could not resolve frozen-squads items")
        return frozen

    @staticmethod
    async def count_subscriptions(db: Database, module: str) -> int:
        t = panel_squad_substitutions
        async with db.read() as conn:
            value = await conn.scalar(
                sa.select(sa.func.count(sa.distinct(t.c.subscription_id))).where(t.c.owner_module == module)
            )
        return int(value or 0)
