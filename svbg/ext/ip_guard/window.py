"""IP Guard: normalization of node answers and the in-memory sliding window (05 §2.2.3).

Pure and synchronous: :meth:`Window.apply_pass` takes the node list and the parsed answers of one pass and
returns per-subscription numbers (:class:`UserStats`). Nothing here talks to the panel or the database; the
window is lost on restart by design (the decider then needs fresh confirmation passes).

Rules (owner's collector, kept 1:1):

* IPv6 without brackets; IPv4-mapped IPv6 → IPv4; only ``is_global``; never multicast/loopback/link-local;
* IPs of the nodes themselves (``ips``, literal ``address`` and resolved host names) and ``ignore_cidrs`` are
  dropped;
* key of the window: the IPv4 address or the IPv6 /prefix network; subnet for S: IPv4 /24, IPv6 the key;
* a **shared IP** seen at ≥ ``shared_ip_users`` users in one pass counts for nobody, is removed from the
  window retroactively and is never dropped;
* required nodes of a user: nodes where they were seen within 2 intervals or 2 actual passes (whichever is
  longer), minus offline nodes and nodes failing 3+ passes; a node answering "0 users" after ≥ 5 is treated
  as a failure for up to 3 passes (xray restart, lesson 4);
* caps: 5000 keys per user, 2000 live IPs per user and node, ``max_tracked_keys`` overall.

Speed: :func:`prepare_polls` normalizes every distinct raw address of a pass **once** (``ipaddress`` is the
expensive part) and touches no window state, so the service runs it in a worker thread; :meth:`Window.
apply_pass` then only does dictionary work on the event loop.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from svbg.ext.ip_guard.config import CollectorParams

__all__ = [
    "EMPTY_SUSPICIOUS_MAX_PASSES",
    "FAIL_STREAK_HEALTH",
    "FAIL_STREAK_NOT_REQUIRED",
    "MAX_KEYS_PER_USER",
    "NodeInfo",
    "NodePoll",
    "PassResult",
    "Prepared",
    "UserStats",
    "Window",
    "ip_key",
    "normalize_ip",
    "parse_ip",
    "parse_job_status",
    "prepare_polls",
    "strip_ip",
    "subnet_of_key",
]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
NodeStatus = Literal["ok", "failed", "offline"]
#: One counted address: ``(normalized IP, window key, raw address without brackets)``.
Entry = tuple[str, str, str]
#: node uuid → panel user → counted addresses of that user on that node (non-counted ones are left out).
NodeEntries = dict[int, list[Entry]]

MAX_RAW_PER_KEY: Final = 64
MAX_KEYS_PER_USER: Final = 5000
MAX_LIVE_PER_USER_NODE: Final = 2000
FAIL_STREAK_NOT_REQUIRED: Final = 3
FAIL_STREAK_HEALTH: Final = 10
EMPTY_SUSPICIOUS_MIN_USERS: Final = 5
EMPTY_SUSPICIOUS_MAX_PASSES: Final = 3
EVIDENCE_TOP: Final = 50


# ------------------------------------------------------------------------------------------ normalization


def strip_ip(raw: str) -> str:
    """Address without spaces and IPv6 brackets (a node may answer ``[2001:db8::1]``)."""
    return raw.strip().removeprefix("[").removesuffix("]")


def parse_ip(raw: Any) -> IPAddress | None:
    """Parse without filters; IPv4-mapped IPv6 becomes IPv4."""
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    try:
        ip = ipaddress.ip_address(strip_ip(raw))
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:  # type: ignore[union-attr]
        ip = ip.ipv4_mapped  # type: ignore[union-attr]
    return ip


def normalize_ip(
    raw: Any,
    *,
    infra_ips: frozenset[IPAddress] | set[IPAddress] = frozenset(),
    ignore_cidrs: Iterable[IPNetwork] = (),
) -> IPAddress | None:
    ip = parse_ip(raw)
    if ip is None:
        return None
    # ``is_global`` is True for some multicast ranges: cut them explicitly.
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or not ip.is_global:
        return None
    if ip in infra_ips or any(ip in net for net in ignore_cidrs):
        return None
    return ip


def ip_key(ip: IPAddress, ipv6_prefix: int = 64) -> str:
    """Window key: an IPv4 address or an IPv6 ``/prefix`` network."""
    if ip.version == 4:
        return str(ip)
    return str(ipaddress.IPv6Network((ip, ipv6_prefix), strict=False))


def subnet_of_key(key: str) -> str:
    """Subnet for S: IPv4 ``/24``, IPv6 — the key itself.

    Plain string work (called for every key of every user on each pass): an IPv4 key is always the canonical
    dotted form written by :func:`ip_key`; anything else falls back to ``ipaddress``."""
    if ":" in key:
        return key
    head, dot, last = key.rpartition(".")
    if dot and last.isdigit() and head.count(".") == 2:
        return f"{head}.0/24"
    return str(ipaddress.IPv4Network((key, 24), strict=False))


@dataclass(frozen=True, slots=True)
class Prepared:
    """:func:`prepare_polls` of one pass: counted addresses per node and the shared IPs among those nodes."""

    nodes: dict[str, NodeEntries]
    shared: dict[str, int]


def _shared_of(nodes: Iterable[NodeEntries], params: CollectorParams) -> dict[str, int]:
    users_by_ip: dict[str, set[int]] = {}
    for users in nodes:
        for pid, entries in users.items():
            for ip, _key, _raw in entries:
                users_by_ip.setdefault(ip, set()).add(pid)
    return {ip: len(u) for ip, u in users_by_ip.items() if len(u) >= params.shared_ip_users}


def prepare_polls(
    polls: Iterable[NodePoll], infra_ips: frozenset[IPAddress], params: CollectorParams
) -> Prepared:
    """Normalize the answers of the ``ok`` nodes of one pass (each distinct raw address is parsed once) and
    find the shared IPs among them.

    Pure (reads only its arguments): safe to run in a worker thread while the event loop goes on."""
    cache: dict[str, tuple[str, str] | None] = {}
    prefix = params.ipv6_prefix
    ignore = params.ignore_cidrs
    out: dict[str, NodeEntries] = {}
    for poll in polls:
        if poll.status != "ok":
            continue
        node: NodeEntries = {}
        for pid, entries in poll.users.items():
            counted: list[Entry] = []
            for raw, _last_seen in entries:
                hit = cache.get(raw, False)
                if hit is False:
                    ip = normalize_ip(raw, infra_ips=infra_ips, ignore_cidrs=ignore)
                    hit = cache[raw] = None if ip is None else (str(ip), ip_key(ip, prefix))
                if hit is not None:
                    counted.append((hit[0], hit[1], strip_ip(raw)))
            if counted:
                node[pid] = counted
        out[poll.uuid] = node
    return Prepared(out, _shared_of(out.values(), params))


# ------------------------------------------------------------------------------------------- node answers


@dataclass(frozen=True, slots=True)
class NodeInfo:
    uuid: str
    name: str = ""
    address: str = ""
    is_connected: bool = False
    is_disabled: bool = False
    ips: tuple[str, ...] = ()


@dataclass(slots=True)
class NodePoll:
    uuid: str
    status: NodeStatus
    reason: str | None = None
    http_status: int | None = None
    users: dict[int, list[tuple[str, datetime | None]]] = field(default_factory=dict)


def _parse_last_seen(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_job_status(node_uuid: str, payload: Mapping[str, Any] | None) -> NodePoll | None:
    """Status of a ``by-node`` job; ``None`` while it is still running.

    Only ``result.success`` and a matching ``nodeUuid`` count: on a node error the panel answers
    ``isCompleted`` with ``success=false`` instead of ``isFailed`` (05 §2.2.3).
    """
    if payload is None:
        return NodePoll(node_uuid, "failed", "job_missing")
    if payload.get("isFailed"):
        return NodePoll(node_uuid, "failed", "job_failed")
    if not payload.get("isCompleted"):
        return None
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return NodePoll(node_uuid, "failed", "result_null")
    if result.get("success") is not True:
        return NodePoll(node_uuid, "failed", "success_false")
    if str(result.get("nodeUuid") or "").lower() != node_uuid.lower():
        return NodePoll(node_uuid, "failed", "node_mismatch")
    users: dict[int, list[tuple[str, datetime | None]]] = {}
    for item in result.get("users") or []:
        if not isinstance(item, Mapping):
            continue
        raw_id = item.get("userId")
        if isinstance(raw_id, bool):
            continue
        try:
            user_id = int(raw_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        bucket = users.setdefault(user_id, [])
        for entry in item.get("ips") or []:
            if isinstance(entry, str):
                bucket.append((entry, None))
            elif isinstance(entry, Mapping) and isinstance(entry.get("ip"), str):
                bucket.append((entry["ip"], _parse_last_seen(entry.get("lastSeen"))))
    return NodePoll(node_uuid, "ok", users=users)


# ------------------------------------------------------------------------------------------------ window


@dataclass(slots=True)
class KeyEntry:
    raw: set[str]
    nodes: dict[str, datetime]
    seen_at: datetime
    raw_truncated: bool = False
    subnet: str = ""  # subnet_of_key(key), computed once when the key enters the window


@dataclass(frozen=True, slots=True)
class UserStats:
    panel_user_id: int
    ip_count: int  # W: distinct keys in the window
    live_ip_count: int  # L: keys seen in this pass
    subnet_count: int  # S
    complete: bool
    missing_nodes: tuple[str, ...] = ()
    keys_capped: bool = False


@dataclass(slots=True)
class PassResult:
    at: datetime
    polls: dict[str, NodePoll]
    stats: dict[int, UserStats]
    live: dict[int, dict[str, set[str]]]  # panel user → node → raw IPs seen now
    shared_ips: dict[str, int] = field(default_factory=dict)
    window_overflow: bool = False
    nodes_error: str | None = None
    long_failed_nodes: tuple[str, ...] = ()

    @property
    def any_ok(self) -> bool:
        return any(p.status == "ok" for p in self.polls.values())

    @property
    def forbidden(self) -> bool:
        return any(p.http_status in (401, 403) for p in self.polls.values())

    def node_reasons(self) -> dict[str, str]:
        reasons = {u: p.reason or p.status for u, p in self.polls.items() if p.status != "ok"}
        if self.nodes_error:
            reasons["nodes"] = self.nodes_error
        return reasons


class Window:
    """Per-process window of IP keys per panel user (bounded; see module docstring)."""

    def __init__(self) -> None:
        self.keys: dict[int, dict[str, KeyEntry]] = {}
        self.live: dict[int, dict[str, set[str]]] = {}
        self.last_nodes: dict[int, dict[str, datetime]] = {}
        self.node_fail_streak: dict[str, int] = {}
        self._empty_baseline: dict[str, int] = {}
        self._empty_streak: dict[str, int] = {}
        self._prev_pass_at: datetime | None = None
        self._pass_fresh: timedelta | None = None

    @property
    def total_keys(self) -> int:
        return sum(len(k) for k in self.keys.values())

    def forget_user(self, panel_user_id: int) -> None:
        """After an unblock: the user starts from an empty window."""
        self.keys.pop(panel_user_id, None)
        self.live.pop(panel_user_id, None)
        self.last_nodes.pop(panel_user_id, None)

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def apply_pass(
        self,
        nodes: Sequence[NodeInfo],
        polls: Mapping[str, NodePoll],
        infra_ips: frozenset[IPAddress],
        params: CollectorParams,
        *,
        now: datetime,
        excluded: frozenset[str] = frozenset(),
        nodes_error: str | None = None,
        min_w: int = 1,
        prepared: Prepared | None = None,
    ) -> PassResult:
        """One pass: node statuses, window update, per-user numbers. ``excluded``: CDN node uuids.

        ``prepared``: :func:`prepare_polls` of the same ``polls`` (computed off the loop by the service);
        when missing (or missing a node) it is computed here."""
        fresh_base = timedelta(seconds=2 * params.interval_s)
        self._pass_fresh = None
        if self._prev_pass_at is not None:
            period = min(now - self._prev_pass_at, timedelta(minutes=params.window_minutes))
            self._pass_fresh = max(fresh_base, 2 * period)
        candidates = {n.uuid: n for n in nodes if not n.is_disabled and n.uuid not in excluded}
        self._drop_nodes(excluded)

        statuses: dict[str, NodePoll] = {}
        if nodes_error is None:
            for uuid, node in candidates.items():
                if not node.is_connected:
                    statuses[uuid] = NodePoll(uuid, "offline", "offline")
                    continue
                poll = polls.get(uuid) or NodePoll(uuid, "failed", "not_polled")
                statuses[uuid] = self._check_empty(poll)
            for uuid, poll in statuses.items():
                if poll.status == "failed":
                    self.node_fail_streak[uuid] = self.node_fail_streak.get(uuid, 0) + 1
                elif poll.status == "ok":
                    self.node_fail_streak[uuid] = 0
            for uuid in [u for u in self.node_fail_streak if u not in candidates]:
                del self.node_fail_streak[uuid]

        ok_polls = [p for p in statuses.values() if p.status == "ok"]
        ok_ids = {p.uuid for p in ok_polls}
        if prepared is None or not ok_ids <= prepared.nodes.keys():
            prepared = prepare_polls(ok_polls, infra_ips, params)
        counted = [(p.uuid, prepared.nodes[p.uuid]) for p in ok_polls]
        # A node demoted by ``_check_empty`` has no users, so it never changes the shared set; only a node
        # that is no longer ``ok`` with users (not the service's case) needs a recount here.
        same = all(not users for u, users in prepared.nodes.items() if u not in ok_ids)
        shared = prepared.shared if same else _shared_of((users for _u, users in counted), params)
        overflow, live_keys = self._update(counted, shared, params, now)
        self._expire(params, now, fresh_base)

        stats: dict[int, UserStats] = {}
        for pid, keys in self.keys.items():
            if len(keys) < min_w:
                continue
            required = self.required_nodes(pid, statuses, now, fresh_base)
            missing = tuple(sorted(u for u in required if statuses[u].status != "ok"))
            stats[pid] = UserStats(
                panel_user_id=pid,
                ip_count=len(keys),
                live_ip_count=len(live_keys.get(pid, ())),
                subnet_count=len({e.subnet or subnet_of_key(k) for k, e in keys.items()}),
                complete=not missing,
                missing_nodes=missing,
                keys_capped=len(keys) >= MAX_KEYS_PER_USER,
            )
        long_failed = tuple(sorted(u for u, s in self.node_fail_streak.items() if s >= FAIL_STREAK_HEALTH))
        self._prev_pass_at = now
        return PassResult(
            at=now,
            polls={u: NodePoll(u, p.status, p.reason, p.http_status) for u, p in statuses.items()},
            stats=stats,
            live=self.live,
            shared_ips=shared,
            window_overflow=overflow,
            nodes_error=nodes_error,
            long_failed_nodes=long_failed,
        )

    # ------------------------------------------------------------------------------------------ helpers

    def _drop_nodes(self, excluded: frozenset[str]) -> None:
        """A node marked CDN on the fly removes its keys at once."""
        if not excluded:
            return
        for pid, keys in list(self.keys.items()):
            for key, entry in list(keys.items()):
                gone = [u for u in entry.nodes if u in excluded]
                if not gone:
                    continue
                for u in gone:
                    entry.nodes.pop(u, None)
                if entry.nodes:
                    entry.seen_at = max(entry.nodes.values())
                else:
                    del keys[key]
            seen = self.last_nodes.get(pid)
            if seen:
                for u in [u for u in seen if u in excluded]:
                    del seen[u]
            if not keys:
                self.forget_user(pid)

    def _check_empty(self, poll: NodePoll) -> NodePoll:
        if poll.status != "ok":
            return poll
        if poll.users:
            self._empty_baseline[poll.uuid] = len(poll.users)
            self._empty_streak[poll.uuid] = 0
            return poll
        if self._empty_baseline.get(poll.uuid, 0) < EMPTY_SUSPICIOUS_MIN_USERS:
            return poll
        streak = self._empty_streak.get(poll.uuid, 0) + 1
        if streak <= EMPTY_SUSPICIOUS_MAX_PASSES:
            self._empty_streak[poll.uuid] = streak
            return NodePoll(poll.uuid, "failed", "empty_suspicious")
        self._empty_baseline[poll.uuid] = 0  # the node really emptied: accept it
        self._empty_streak[poll.uuid] = 0
        return poll

    def _update(
        self,
        counted: Sequence[tuple[str, NodeEntries]],
        shared: Mapping[str, int],
        params: CollectorParams,
        now: datetime,
    ) -> tuple[bool, dict[int, set[str]]]:
        total = self.total_keys
        cap = params.max_tracked_keys
        overflow = False
        new_live: dict[int, dict[str, set[str]]] = {}
        live_keys: dict[int, set[str]] = {}
        # Per-user lookups are hoisted out of the per-address loop: this part runs on the event loop.
        for uuid, users in counted:
            for pid, entries in users.items():
                kept = [e for e in entries if e[0] not in shared] if shared else entries
                if not kept:
                    continue
                node_live = new_live.setdefault(pid, {}).setdefault(uuid, set())
                user_live = live_keys.setdefault(pid, set())
                self.last_nodes.setdefault(pid, {})[uuid] = now
                keys = self.keys.get(pid)
                for _ip, key, raw_clean in kept:
                    if len(node_live) < MAX_LIVE_PER_USER_NODE:
                        node_live.add(raw_clean)
                    user_live.add(key)
                    if keys is None:
                        if total >= cap:
                            overflow = True
                            continue
                        keys = self.keys[pid] = {}
                    entry = keys.get(key)
                    if entry is None:
                        if len(keys) >= MAX_KEYS_PER_USER or total >= cap:
                            overflow = overflow or total >= cap
                            continue
                        entry = keys[key] = KeyEntry(
                            raw=set(), nodes={}, seen_at=now, subnet=subnet_of_key(key)
                        )
                        total += 1
                    raw = entry.raw
                    if raw_clean not in raw:
                        if len(raw) < MAX_RAW_PER_KEY:
                            raw.add(raw_clean)
                        else:
                            entry.raw_truncated = True
                    entry.nodes[uuid] = now
                    entry.seen_at = now
        if shared:
            self._drop_shared(shared)
        for pid in [u for u in self.last_nodes if u not in self.keys]:
            del self.last_nodes[pid]
        self.live = new_live
        return overflow, live_keys

    def _drop_shared(self, shared: Mapping[str, int]) -> None:
        """A shared IP (relay, CDN) counts for nobody, even when it got into the window earlier."""
        for pid, keys in list(self.keys.items()):
            for key, entry in list(keys.items()):
                if key in shared:
                    del keys[key]
                    continue
                if ":" in key:
                    hit = {r for r in entry.raw if str(parse_ip(r)) in shared}
                    if hit:
                        entry.raw -= hit
                        if not entry.raw and not entry.raw_truncated:
                            del keys[key]
            if not keys:
                self.forget_user(pid)

    def _expire(self, params: CollectorParams, now: datetime, fresh_base: timedelta) -> None:
        horizon = now - timedelta(minutes=params.window_minutes)
        # Node marks live as long as the longest possible "fresh" span (2 × the pass period, the period being
        # capped by the window): a mark that may still count is never dropped, so ``required_nodes`` needs
        # only ``last_nodes`` and not every key's own node marks.
        keep = now - max(fresh_base, 2 * timedelta(minutes=params.window_minutes))
        for pid, keys in list(self.keys.items()):
            for key in [k for k, e in keys.items() if e.seen_at < horizon]:
                del keys[key]
            if not keys:
                del self.keys[pid]
                self.last_nodes.pop(pid, None)
                continue
            seen = self.last_nodes.get(pid)
            if seen:
                for u in [u for u, at in seen.items() if at < keep]:
                    del seen[u]

    def required_nodes(
        self, pid: int, statuses: Mapping[str, NodePoll], now: datetime, fresh_base: timedelta
    ) -> set[str]:
        """Fresh nodes of the user, without offline ones and ones failing for 3+ passes.

        Only ``last_nodes`` is read: every ``entry.nodes[node] = t`` is written together with
        ``last_nodes[pid][node] = t``, and :meth:`_expire` keeps a mark as long as it can still be fresh, so
        the per-key node marks add nothing (scanning them was the costliest step of a pass)."""
        fresh = now - (self._pass_fresh or fresh_base)
        required = {u for u, at in self.last_nodes.get(pid, {}).items() if at >= fresh}
        return {
            u
            for u in required
            if u in statuses
            and statuses[u].status != "offline"
            and self.node_fail_streak.get(u, 0) < FAIL_STREAK_NOT_REQUIRED
        }

    # ------------------------------------------------------------------------------------------ evidence

    def evidence(self, pid: int, limit: int = EVIDENCE_TOP) -> dict[str, Any]:
        """Top-``limit`` keys (freshest first) with their nodes: what a block or a card stores."""
        keys = self.keys.get(pid, {})
        ordered = sorted(keys.items(), key=lambda kv: (kv[1].seen_at, kv[0]), reverse=True)
        top = [
            {"key": k, "ips": sorted(e.raw)[:8], "nodes": sorted(e.nodes), "seen": e.seen_at.isoformat()}
            for k, e in ordered[:limit]
        ]
        return {"top": top, "total": len(keys)}

    def all_keys(self, pid: int) -> list[tuple[str, list[str], list[str], datetime]]:
        """Every key of the user right now (for «📄 Все IP»)."""
        keys = self.keys.get(pid, {})
        return [
            (k, sorted(e.raw), sorted(e.nodes), e.seen_at)
            for k, e in sorted(keys.items(), key=lambda kv: kv[1].seen_at, reverse=True)
        ]

    def drop_map(self, pid: int, *, limit: int = 1000) -> dict[str, list[str]]:
        """Node → raw IPs of the user's window keys seen on that node (what a block drops, per node)."""
        out: dict[str, set[str]] = {}
        for entry in self.keys.get(pid, {}).values():
            for node in entry.nodes:
                bucket = out.setdefault(node, set())
                if len(bucket) < limit:
                    bucket.update(entry.raw)
        return {node: sorted(ips)[:limit] for node, ips in out.items() if ips}

    def live_by_node(self, pid: int) -> dict[str, list[str]]:
        """Raw IPs per node seen in the last pass (what a block drops)."""
        return {node: sorted(ips) for node, ips in self.live.get(pid, {}).items() if ips}
