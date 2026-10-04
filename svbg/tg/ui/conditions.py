"""Visibility conditions: a small declarative JSON DSL compiled into Python closures (07 §2.4.1).

Grammar (a condition is a JSON object; ``None``/``{}`` means "always")::

    {"all": [cond, ...]}           every condition holds (empty list → true)
    {"any": [cond, ...]}           at least one holds (empty list → false)
    {"not": cond}
    {"atom1": v1, "atom2": v2}     several keys in one object are an implicit "all"

Atoms:

====================  =======================================================================
``role``              ``"admin"`` | ``["admin", "owner"]`` | ``{"gte": "admin"}`` (also lte/gt/lt/eq)
``sub``               none | trial | active | expired | frozen (or a list of them)
``days_left``         int (equality) or ``{"lt"|"lte"|"gt"|"gte"|"eq"|"ne": int, ...}``;
                      users without a subscription (``days_left is None``) never match
``balance_minor``     like ``days_left``
``ref_count``         like ``days_left``
``has_paid``          bool
``is_new``            bool
``channel_member``    bool (unknown membership counts as "not a member")
``source``            str | list[str] (ad tag)
``plan``              str | list[str] (plan code)
``flag:<name>``       bool — module flag present in ``UserCtx.flags`` (e.g. ``flag:lte.blocked``)
``segment:<tag>``     bool — tag present in ``UserCtx.segments``
====================  =======================================================================

The bot is Russian-only: an old ``lang`` atom saved in content still compiles, every user counts as ``ru``.

All validation happens in :func:`compile_condition` (unknown atom, wrong type, bad operator →
:class:`ConditionError` with a path such as ``all[1].days_left``). Evaluating a compiled condition only
reads attributes of an already loaded :class:`UserCtx` — no SQL, no I/O.

:func:`to_sql` compiles the same DSL into a SQL predicate over ``users`` (broadcast segments).
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa

from svbg.db.meta import UtcDateTime
from svbg.tg.ui.context import ROLE_RANK, SUB_STATES, UserCtx

__all__ = [
    "ALWAYS",
    "ATOMS",
    "MAX_DEPTH",
    "MAX_NODES",
    "SQL_UNSUPPORTED",
    "Condition",
    "ConditionError",
    "compile_condition",
    "to_sql",
    "validate_condition",
]

Condition = Callable[[UserCtx], bool]

MAX_DEPTH: Final = 12
MAX_NODES: Final = 200

_CMP: Final[Mapping[str, Callable[[Any, Any], bool]]] = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "eq": operator.eq,
    "ne": operator.ne,
}
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_STR_VALUE_MAX: Final = 128
_LIST_MAX: Final = 64

ATOMS: Final[tuple[str, ...]] = (
    "role",
    "sub",
    "days_left",
    "balance_minor",
    "ref_count",
    "has_paid",
    "is_new",
    "channel_member",
    "source",
    "plan",
    "flag:<name>",
    "segment:<tag>",
)


def _unknown_cmp(op_name: Any) -> str:
    return f"неизвестное сравнение '{op_name}' (есть lt/lte/gt/gte/eq/ne)"


class ConditionError(ValueError):
    """The condition is malformed. ``path`` points at the offending node (``all[1].days_left``)."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path or "<root>"
        self.message = message
        super().__init__(f"{self.path}: {message}")


def _always(_ctx: UserCtx) -> bool:
    return True


def _never(_ctx: UserCtx) -> bool:
    return False


ALWAYS: Final[Condition] = _always


class _Compiler:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0

    def compile(self, node: Any, path: str, depth: int) -> Condition:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ConditionError(path, f"условие слишком большое (больше {MAX_NODES} частей)")
        if depth > MAX_DEPTH:
            raise ConditionError(path, f"слишком глубокая вложенность (больше {MAX_DEPTH} уровней)")
        if not isinstance(node, Mapping):
            raise ConditionError(path, f"нужен объект, а пришло {type(node).__name__}")
        if not node:
            return _always
        parts: list[Condition] = []
        for key, value in node.items():
            if not isinstance(key, str):
                raise ConditionError(path, "ключи должны быть строками")
            sub_path = f"{path}.{key}" if path else key
            parts.append(self._compile_key(key, value, sub_path, depth))
        return parts[0] if len(parts) == 1 else _all_of(parts)

    def _compile_key(self, key: str, value: Any, path: str, depth: int) -> Condition:
        if key in ("all", "any"):
            if not isinstance(value, list):
                raise ConditionError(path, f"'{key}' ждёт список")
            if len(value) > _LIST_MAX:
                raise ConditionError(path, f"список '{key}' слишком длинный")
            subs = [self.compile(item, f"{path}[{i}]", depth + 1) for i, item in enumerate(value)]
            if key == "all":
                return _always if not subs else (subs[0] if len(subs) == 1 else _all_of(subs))
            return _never if not subs else (subs[0] if len(subs) == 1 else _any_of(subs))
        if key == "not":
            inner = self.compile(value, path, depth + 1)
            return lambda ctx: not inner(ctx)
        if key.startswith("flag:"):
            return _set_member("flags", _name(key[5:], path), _bool(value, path))
        if key.startswith("segment:"):
            return _set_member("segments", _name(key[8:], path), _bool(value, path))
        builder = _ATOM_BUILDERS.get(key)
        if builder is None:
            raise ConditionError(path, f"неизвестное условие '{key}'")
        return builder(value, path)


def _all_of(parts: Sequence[Condition]) -> Condition:
    items = tuple(parts)
    return lambda ctx: all(p(ctx) for p in items)


def _any_of(parts: Sequence[Condition]) -> Condition:
    items = tuple(parts)
    return lambda ctx: any(p(ctx) for p in items)


def _name(raw: str, path: str) -> str:
    if not _NAME_RE.match(raw):
        raise ConditionError(path, "неверное имя (можно латиницу, цифры, '_', '.', '-'; до 64)")
    return raw


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConditionError(path, "нужно true или false")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _STR_VALUE_MAX:
        raise ConditionError(path, f"нужна непустая строка (до {_STR_VALUE_MAX} символов)")
    return value


def _str_set(value: Any, path: str, allowed: Sequence[str] | None = None) -> frozenset[str]:
    if isinstance(value, list):
        if not value or len(value) > _LIST_MAX:
            raise ConditionError(path, f"нужно от 1 до {_LIST_MAX} значений")
        values = [_str(v, f"{path}[{i}]") for i, v in enumerate(value)]
    else:
        values = [_str(value, path)]
    if allowed is not None:
        for v in values:
            if v not in allowed:
                raise ConditionError(path, f"'{v}' не из списка: {', '.join(allowed)}")
    return frozenset(values)


def _int(value: Any, path: str) -> int:
    # bool is an int subclass in Python; "true" is never a meaningful number here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConditionError(path, "нужно целое число")
    return value


def _numeric(attr: str) -> Callable[[Any, str], Condition]:
    def build(value: Any, path: str) -> Condition:
        checks: list[tuple[Callable[[Any, Any], bool], int]] = []
        if isinstance(value, Mapping):
            if not value:
                raise ConditionError(path, "нужно хотя бы одно сравнение")
            for op_name, bound in value.items():
                fn = _CMP.get(op_name) if isinstance(op_name, str) else None
                if fn is None:
                    raise ConditionError(path, _unknown_cmp(op_name))
                checks.append((fn, _int(bound, f"{path}.{op_name}")))
        else:
            checks.append((operator.eq, _int(value, path)))
        frozen = tuple(checks)

        def check(ctx: UserCtx) -> bool:
            current = getattr(ctx, attr)
            if current is None:
                return False
            return all(fn(current, bound) for fn, bound in frozen)

        return check

    return build


def _in_set(attr: str, allowed: Sequence[str] | None = None) -> Callable[[Any, str], Condition]:
    def build(value: Any, path: str) -> Condition:
        values = _str_set(value, path, allowed)
        if len(values) == 1:
            (single,) = values
            return lambda ctx: getattr(ctx, attr) == single
        return lambda ctx: getattr(ctx, attr) in values

    return build


def _flag_attr(attr: str) -> Callable[[Any, str], Condition]:
    def build(value: Any, path: str) -> Condition:
        expected = _bool(value, path)
        return lambda ctx: bool(getattr(ctx, attr)) is expected

    return build


def _set_member(attr: str, name: str, expected: bool) -> Condition:
    return lambda ctx: (name in getattr(ctx, attr)) is expected


def _role(value: Any, path: str) -> Condition:
    roles = tuple(ROLE_RANK)
    if isinstance(value, Mapping):
        if not value:
            raise ConditionError(path, "нужно хотя бы одно сравнение")
        checks: list[tuple[Callable[[Any, Any], bool], int]] = []
        for op_name, bound in value.items():
            fn = _CMP.get(op_name) if isinstance(op_name, str) else None
            if fn is None:
                raise ConditionError(path, _unknown_cmp(op_name))
            role = _str(bound, f"{path}.{op_name}")
            if role not in ROLE_RANK:
                raise ConditionError(f"{path}.{op_name}", f"'{role}' не из списка: {', '.join(roles)}")
            checks.append((fn, ROLE_RANK[role]))
        frozen = tuple(checks)
        return lambda ctx: all(fn(ROLE_RANK.get(ctx.role, -1), bound) for fn, bound in frozen)
    return _in_set("role", roles)(value, path)


_ATOM_BUILDERS: Final[Mapping[str, Callable[[Any, str], Condition]]] = {
    "role": _role,
    "lang": _in_set("lang"),
    "sub": _in_set("sub_state", SUB_STATES),
    "days_left": _numeric("days_left"),
    "balance_minor": _numeric("balance_minor"),
    "ref_count": _numeric("ref_count"),
    "has_paid": _flag_attr("has_paid"),
    "is_new": _flag_attr("is_new"),
    "channel_member": _flag_attr("channel_member"),
    "source": _in_set("source"),
    "plan": _in_set("plan_code"),
}


def compile_condition(dsl: Mapping[str, Any] | None) -> Condition:
    """Validate ``dsl`` and compile it into a fast predicate over :class:`UserCtx`.

    Raises :class:`ConditionError` on any problem; never returns a partially valid predicate.
    """
    if dsl is None:
        return _always
    return _Compiler().compile(dsl, "", 0)


def validate_condition(dsl: Mapping[str, Any] | None) -> None:
    """Raise :class:`ConditionError` if ``dsl`` is invalid (used when content is saved or imported)."""
    compile_condition(dsl)


def to_sql(
    dsl: Mapping[str, Any] | None,
    *,
    at: datetime | None = None,
    default_lang: str = "ru",
    langs: Sequence[str] = ("ru",),
    channel_id: int | None = None,
) -> sa.ColumnElement[bool]:
    """Compile the DSL into a SQL predicate over ``users`` (broadcast segments, 07 §2.4.5).

    The predicate is correlated to the ``users`` table (``svbg.core.tables.users``): use it in a ``SELECT``
    whose ``FROM`` has ``users`` itself (not an alias). It reproduces what :func:`compile_condition`
    decides on the ``UserCtx`` the user path builds (``svbg.tg.user.status``): the *current* subscription
    is the newest not-closed one, live (``pending``/``linked``) first;
    ``days_left`` = ⌈seconds left / 86400⌉ ≥ 0;
    ``has_paid`` = a paid or fulfilled order. Differences, by design: ``role`` is the stored role
    (``OWNER_IDS`` owners without ``role='owner'`` are not matched); an old ``lang`` atom matches everyone
    when it lists ``ru`` (``default_lang``/``langs`` are accepted for old callers and ignored);
    ``channel_member`` needs ``channel_id``.

    Atoms without a meaning in SQL (``is_new``, ``ref_count``, ``source``, ``flag:*``, ``segment:*``, and
    ``channel_member`` without a channel) raise :class:`ConditionError`, as does any invalid condition.
    ``at`` is "now" for ``sub``/``days_left`` (defaults to :func:`svbg.core.clock.now`).
    """
    validate_condition(dsl)
    from svbg.core import clock

    when = at if at is not None else clock.now()
    del default_lang, langs
    return _SqlCompiler(when, channel_id).compile(dsl or {}, "")


#: Atoms :func:`to_sql` refuses (no column, or no meaning outside a live update).
SQL_UNSUPPORTED: Final[Mapping[str, str]] = {
    "is_new": "'is_new' значит «пришёл только что», в сегментах его нельзя",
    "ref_count": "'ref_count' пока не хранится, в сегментах его нельзя",
    "source": "'source' (рекламная метка) пока не хранится, в сегментах его нельзя",
}


class _SqlCompiler:
    """Mirror of :class:`_Compiler` producing SQLAlchemy predicates (the input is already validated)."""

    __slots__ = ("at", "channel_id", "t")

    def __init__(self, at: datetime, channel_id: int | None) -> None:
        from svbg.billing.tables import orders
        from svbg.core.tables import users
        from svbg.subscriptions.tables import channel_members, subscriptions

        self.at = at
        self.channel_id = channel_id
        self.t: dict[str, sa.Table] = {
            "users": users,
            "subs": subscriptions,
            "orders": orders,
            "members": channel_members,
        }

    # ---- structure

    def compile(self, node: Mapping[str, Any], path: str) -> sa.ColumnElement[bool]:
        if not node:
            return sa.true()
        parts = [self._key(k, v, f"{path}.{k}" if path else k) for k, v in node.items()]
        return parts[0] if len(parts) == 1 else sa.and_(*parts)

    def _key(self, key: str, value: Any, path: str) -> sa.ColumnElement[bool]:
        if key in ("all", "any"):
            subs = [self.compile(item, f"{path}[{i}]") for i, item in enumerate(value)]
            if key == "all":
                return sa.and_(sa.true(), *subs)
            return sa.or_(sa.false(), *subs)
        if key == "not":
            return sa.not_(self.compile(value, path))
        if key.startswith(("flag:", "segment:")):
            raise ConditionError(path, f"'{key}' зависит от текущего действия, в сегментах его нельзя")
        if key in SQL_UNSUPPORTED:
            raise ConditionError(path, SQL_UNSUPPORTED[key])
        users = self.t["users"]
        if key == "role":
            return self._role(value)
        if key == "lang":  # Russian-only bot: everyone is "ru"
            return sa.true() if "ru" in _str_set(value, path) else sa.false()
        if key == "sub":
            return sa.or_(*(self._sub_state(s) for s in sorted(_str_set(value, path, SUB_STATES))))
        if key == "days_left":
            return self._current_sub(self._numeric(self._days_left_expr(), value, path), paid=True)
        if key == "balance_minor":
            return self._numeric(users.c.wallet_minor, value, path)
        if key == "has_paid":
            orders = self.t["orders"]
            paid = (
                sa.select(sa.literal(1))
                .where(orders.c.user_id == users.c.id, orders.c.status.in_(("paid", "fulfilled")))
                .exists()
            )
            return paid if _bool(value, path) else sa.not_(paid)
        if key == "channel_member":
            if self.channel_id is None:
                raise ConditionError(path, "обязательный канал не настроен")
            members = self.t["members"]
            member = (
                sa.select(sa.literal(1))
                .where(
                    members.c.chat_id == self.channel_id,
                    members.c.telegram_id == users.c.telegram_id,
                    members.c.is_member.is_(True),
                )
                .exists()
            )
            return member if _bool(value, path) else sa.not_(member)
        if key == "plan":
            code = self.t["subs"].c.plan_snapshot["code"].astext
            return self._current_sub(code.in_(sorted(_str_set(value, path))))
        raise ConditionError(path, f"неизвестное условие '{key}'")  # pragma: no cover - validated before

    # ---- atoms

    def _role(self, value: Any) -> sa.ColumnElement[bool]:
        role = self.t["users"].c.role
        if isinstance(value, Mapping):
            rank = sa.case(*((role == name, n) for name, n in ROLE_RANK.items()), else_=-1)
            return sa.and_(*(_CMP[op](rank, ROLE_RANK[bound]) for op, bound in value.items()))
        values = [value] if isinstance(value, str) else list(value)
        return role.in_(sorted(set(values)))

    @staticmethod
    def _numeric(expr: Any, value: Any, path: str) -> sa.ColumnElement[bool]:
        if isinstance(value, Mapping):
            return sa.and_(*(_CMP[op](expr, _int(bound, f"{path}.{op}")) for op, bound in value.items()))
        return expr == _int(value, path)  # type: ignore[no-any-return]

    def _now(self) -> Any:
        return sa.literal(self.at, UtcDateTime)

    def _days_left_expr(self) -> Any:
        subs = self.t["subs"]
        seconds = sa.extract("epoch", subs.c.paid_until - self._now())
        return sa.func.greatest(0, sa.func.ceil(seconds / 86_400))

    def _current_id(self) -> Any:
        cur = self.t["subs"].alias("cur")
        live_first = sa.case((cur.c.link_state.in_(("pending", "linked")), 0), else_=1)
        return (
            sa.select(cur.c.id)
            .where(cur.c.user_id == self.t["users"].c.id, cur.c.link_state != "closed")
            .order_by(live_first, cur.c.id.desc())
            .limit(1)
            .correlate(self.t["users"])
            .scalar_subquery()
        )

    def _current_sub(self, cond: sa.ColumnElement[bool], *, paid: bool = False) -> sa.ColumnElement[bool]:
        subs = self.t["subs"]
        where = [subs.c.id == self._current_id(), cond]
        if paid:
            where.append(subs.c.paid_until.is_not(None))
        return sa.select(sa.literal(1)).select_from(subs).where(*where).exists()

    def _sub_state(self, state: str) -> sa.ColumnElement[bool]:
        subs = self.t["subs"]
        if state == "none":
            return self._current_id().is_(None)  # type: ignore[no-any-return]
        if state == "frozen":
            return self._current_sub(subs.c.hold_kind.is_not(None))
        left = sa.and_(subs.c.paid_until.is_not(None), subs.c.paid_until > self._now())
        if state == "expired":
            return self._current_sub(sa.and_(subs.c.hold_kind.is_(None), sa.not_(left)))
        trial = subs.c.is_trial.is_(True) if state == "trial" else subs.c.is_trial.is_(False)
        return self._current_sub(sa.and_(subs.c.hold_kind.is_(None), left, trial))
