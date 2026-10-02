"""Logging setup and secret masking.

* :class:`SecretRegistry` — process-global set of secret values (≥ 6 chars) that must never appear in
  logs, error reports or "logs from bot". Settings/crypto register current secret values here. Bounded:
  at most :data:`MAX_SECRETS` values of at most :data:`MAX_SECRET_LEN` chars; when full, the least
  recently registered value is evicted (current secrets are re-registered on every settings load/apply).
* :func:`mask` — replaces registered secrets and well-known secret shapes (Telegram bot tokens,
  ``Bearer …``, passwords in URLs/DSNs, ``password=…``/``"token": "…"`` pairs) with ``***``.
* :class:`SecretMaskingFilter` — a :class:`logging.Filter` masking message, args, exception text and
  stack info of every record before any handler formats it.
* :class:`RingBuffer` — handler keeping the most recent formatted records in memory.
* :func:`setup_logging` — root configuration. Records go through a bounded queue to a background
  thread, so slow stderr (a stalled ``docker logs`` pipe) never blocks the event loop.
"""

from __future__ import annotations

import atexit
import copy
import json as _json
import logging
import logging.handlers
import queue
import re
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final, TextIO, TypeGuard
from urllib.parse import quote, quote_plus, unquote, urlsplit

__all__ = [
    "MASK",
    "MAX_SECRETS",
    "MAX_SECRET_LEN",
    "MIN_SECRET_LEN",
    "JsonFormatter",
    "RingBuffer",
    "RingRecord",
    "SecretMaskingFilter",
    "SecretRegistry",
    "TextFormatter",
    "flush_logging",
    "get_ring_buffer",
    "mask",
    "mask_record",
    "parse_level",
    "redact",
    "register_secret",
    "set_level",
    "setup_logging",
    "shutdown_logging",
]

MASK: Final = "***"
MIN_SECRET_LEN: Final = 6
# Longer values are not secrets we can mask usefully (a PEM private key fits); each value yields <= 8 forms.
MAX_SECRET_LEN: Final = 8 * 1024
# ``mask`` costs ~65 ns per form on a log line, so the registry is bounded: 256 values x <= 8 forms keeps
# the worst case at ~0.1-0.2 ms per line. Real deployments hold a few dozen values (current + rotated).
MAX_SECRETS: Final = 256
_EVICTION_WARN_EVERY: Final = 60.0  # s

_registry_log = logging.getLogger("svbg.log")

# --------------------------------------------------------------------------------------------------
# Secret registry
# --------------------------------------------------------------------------------------------------


def _variants(value: str) -> set[str]:
    """Forms in which a secret may show up in text: raw, URL-encoded, JSON/repr-escaped, DSN password."""
    out = {value, quote(value, safe=""), quote_plus(value, safe=""), repr(value)[1:-1]}
    out.add(_json.dumps(value)[1:-1])
    out.add(_json.dumps(value, ensure_ascii=False)[1:-1])
    if "://" in value:
        try:
            password = urlsplit(value).password
        except ValueError:
            password = None
        if password:
            out.update({password, unquote(password)})
    return {v for v in out if len(v) >= MIN_SECRET_LEN}


def _acceptable(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and MIN_SECRET_LEN <= len(value) <= MAX_SECRET_LEN


class SecretRegistry:
    """Process-global registry of secret strings to cut out of any logged text.

    All methods are classmethods, so both ``SecretRegistry.register(v)`` and
    ``SecretRegistry().register(v)`` act on the same global set. Values shorter than
    :data:`MIN_SECRET_LEN` or longer than :data:`MAX_SECRET_LEN` are ignored (masking short ones would
    mangle ordinary text). At most :data:`MAX_SECRETS` values are kept, in registration order: registering
    a known value again refreshes it, and a new value beyond the limit evicts the least recently
    registered one (a warning is logged, without the values). The limits are enforced at registration, so
    :meth:`mask` stays a lock-free scan over a bounded tuple of plain substrings (much faster than a big
    regex alternation).
    """

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _values: ClassVar[dict[str, frozenset[str]]] = {}  # original -> textual forms; oldest first
    _forms: ClassVar[tuple[str, ...]] = ()  # all forms, longest first; swapped atomically
    _evicted: ClassVar[int] = 0  # values evicted since start / clear() (diagnostics)
    _warned_at: ClassVar[float] = float("-inf")  # monotonic time of the last eviction warning

    @classmethod
    def register(cls, value: str | None) -> bool:
        """Add (or refresh) a secret; returns ``False`` when ignored (``None``, too short, too long)."""
        if not _acceptable(value):
            return False
        with cls._lock:
            changed, evicted = cls._add_locked(value)
            if changed:
                cls._rebuild()
        cls._warn_evicted(evicted)
        return True

    @classmethod
    def register_many(cls, values: Iterable[str | None]) -> int:
        """Register several values with a single rebuild; returns how many were accepted."""
        accepted = [v for v in values if _acceptable(v)]
        changed = False
        evicted = 0
        with cls._lock:
            for v in accepted:
                added, out = cls._add_locked(v)
                changed |= added
                evicted += out
            if changed:
                cls._rebuild()
        cls._warn_evicted(evicted)
        return len(accepted)

    @classmethod
    def _add_locked(cls, value: str) -> tuple[bool, int]:
        """Insert or refresh ``value`` (caller holds the lock); returns (forms changed, values evicted)."""
        values = cls._values
        forms = values.pop(value, None)
        if forms is not None:  # known: move to the newest end, the forms are unchanged
            values[value] = forms
            return False, 0
        evicted = 0
        while len(values) >= MAX_SECRETS:
            del values[next(iter(values))]
            evicted += 1
        values[value] = frozenset(_variants(value))
        cls._evicted += evicted
        return True, evicted

    @classmethod
    def _warn_evicted(cls, evicted: int) -> None:
        # Outside the lock: handlers call ``mask``, which must never wait for a registration.
        # At most one warning per ``_EVICTION_WARN_EVERY`` seconds, so a flood cannot flood the log too.
        if not evicted:
            return
        now = time.monotonic()
        if now - cls._warned_at < _EVICTION_WARN_EVERY:
            return
        cls._warned_at = now
        _registry_log.warning(
            "Реестр секретов переполнен (лимит %d): вытеснены самые старые значения, всего %d. "
            "Часто меняются секретные настройки? Сами значения в лог не пишутся.",
            MAX_SECRETS,
            cls._evicted,
        )

    @classmethod
    def unregister(cls, value: str | None) -> None:
        if not isinstance(value, str):
            return
        with cls._lock:
            if cls._values.pop(value, None) is not None:
                cls._rebuild()

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._values = {}
            cls._forms = ()
            cls._evicted = 0
            cls._warned_at = float("-inf")

    @classmethod
    def contains(cls, value: str) -> bool:
        return value in cls._values

    @classmethod
    def count(cls) -> int:
        return len(cls._values)

    @classmethod
    def forms_count(cls) -> int:
        """Textual forms ``mask`` scans for (<= 8 per value)."""
        return len(cls._forms)

    @classmethod
    def evicted(cls) -> int:
        """Values evicted because the registry was full (since start or :meth:`clear`)."""
        return cls._evicted

    @classmethod
    def _rebuild(cls) -> None:
        # Caller holds the lock. Longest first so a secret containing another one is cut out whole.
        forms: set[str] = set()
        for variants in cls._values.values():
            forms |= variants
        cls._forms = tuple(sorted(forms, key=len, reverse=True))

    @classmethod
    def mask(cls, text: str) -> str:
        """Replace registered secrets only (no shape heuristics)."""
        forms = cls._forms  # single atomic read
        if not forms or not text:
            return text
        for form in forms:
            if form in text:
                text = text.replace(form, MASK)
        return text


def register_secret(value: str | None) -> bool:
    """Shortcut for :meth:`SecretRegistry.register`."""
    return SecretRegistry.register(value)


# --------------------------------------------------------------------------------------------------
# Shape-based masking
# --------------------------------------------------------------------------------------------------

# scheme://user:password@ → keep scheme and user, hide the password (empty user allowed: redis://:pw@).
_URL_CREDS_RE: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,30}://)([^:/?#@\s]{0,256}):([^@\s]{1,512}?)@")
# Telegram bot token: <bot id>:<35 chars>; also inside API URLs (".../bot123456:AA.../getMe").
_TG_TOKEN_RE: Final = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_\-]{30,}")
_BEARER_RE: Final = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*")
_KEYWORDS: Final = (
    r"passw(?:or)?d|secret|token|api[_\-]?key|apikey|private[_\-]?key|access[_\-]?key|credentials?"
)
# key=value / key: value / "key": "value" for secret-looking keys (BOT_TOKEN=…, ?api_key=…, 'password': '…').
# "=" must not be surrounded by spaces, so source lines like "token = load()" in tracebacks stay readable.
_KV_RE: Final = re.compile(
    r"(?i)(?<![\w.\-])(?P<key>[\w.\-]{0,40}?(?:" + _KEYWORDS + r")[\w.\-]{0,40})"
    r"(?P<kq>[\"']?)(?P<sep>=|:[ \t]*)(?P<vq>[\"']?)(?P<val>[^\s\"'&,;)}\]<>]+)"
)
# Header-like values that may contain spaces (Authorization: Basic xxx, Cookie: a=b; c=d).
_HEADER_RE: Final = re.compile(
    r"(?i)(?<![\w.\-])(?P<key>(?:proxy-)?authorization|(?:set-)?cookie|x-api-key)"
    r"(?P<kq>[\"']?)(?P<sep>=|:[ \t]*)(?P<vq>[\"']?)(?P<val>[^\r\n\"']+)"
)
# Substrings (lower-case) that must be present for _KV_RE to have a chance; skips the regex otherwise.
_KV_HINTS: Final = ("passw", "secret", "token", "key", "credential")


def _creds_sub(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}:{MASK}@"


def _bearer_sub(m: re.Match[str]) -> str:
    return f"{m.group(1)} {MASK}"


def _kv_sub(m: re.Match[str]) -> str:
    return f"{m.group('key')}{m.group('kq')}{m.group('sep')}{m.group('vq')}{MASK}"


def mask(text: str) -> str:
    """Return ``text`` with secrets replaced by ``***``. Safe on any string; idempotent.

    Cheap substring checks gate every regex, so ordinary log lines cost only a few microseconds.
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    text = SecretRegistry.mask(text)
    if "://" in text:
        text = _URL_CREDS_RE.sub(_creds_sub, text)
    if ":" in text:
        text = _TG_TOKEN_RE.sub(MASK, text)
    low = text.lower()
    if "bearer" in low:
        text = _BEARER_RE.sub(_bearer_sub, text)
    if "authorization" in low or "cookie" in low or "x-api-key" in low:
        text = _HEADER_RE.sub(_kv_sub, text)
    if ("=" in text or ":" in text) and any(hint in low for hint in _KV_HINTS):
        text = _KV_RE.sub(_kv_sub, text)
    return text


def redact(value: str | None, *, keep_last: int = 4) -> str:
    """UI form of a secret: ``••••••••a1B9`` (last ``keep_last`` chars only if the value is long enough)."""
    dots = "\N{BULLET}" * 8
    if not value:
        return ""
    if keep_last <= 0 or len(value) < max(12, keep_last * 3):
        return dots
    return dots + value[-keep_last:]


# --------------------------------------------------------------------------------------------------
# Records, filter, formatters
# --------------------------------------------------------------------------------------------------

_MASKED_ATTR: Final = "_svbg_masked"


def mask_record(record: logging.LogRecord) -> logging.LogRecord:
    """Mask a record in place (message+args merged, exception and stack text). Idempotent."""
    if getattr(record, _MASKED_ATTR, False):
        return record
    try:
        message = record.getMessage()
    except (TypeError, ValueError, KeyError, IndexError):
        message = f"{record.msg!s} (bad log args: {record.args!r})"
    record.msg = mask(message)
    record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
    if record.exc_text:
        record.exc_text = mask(record.exc_text)
    if record.stack_info:
        record.stack_info = mask(record.stack_info)
    setattr(record, _MASKED_ATTR, True)
    return record


class SecretMaskingFilter(logging.Filter):
    """Masks every record passing through; never drops records."""

    def filter(self, record: logging.LogRecord) -> bool:
        mask_record(record)
        return True


class TextFormatter(logging.Formatter):
    """``2026-10-01T12:00:00.123Z INFO    svbg.x: message`` in UTC."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )


_STD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName", _MASKED_ATTR}
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, [exc], [stack], plus ``extra=`` fields (masked)."""

    def format(self, record: logging.LogRecord) -> str:
        mask_record(record)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        for key, value in vars(record).items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            if isinstance(value, bool | int | float) or value is None:
                payload[key] = value
            else:
                payload[key] = mask(value if isinstance(value, str) else repr(value))
        return _json.dumps(payload, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------------------------------
# Ring buffer
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RingRecord:
    ts: datetime
    level: int
    logger: str
    text: str


class RingBuffer(logging.Handler):
    """Keeps the last ``capacity`` formatted (and masked) records for "logs from bot"."""

    def __init__(self, capacity: int = 2000, level: int = logging.NOTSET) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        super().__init__(level)
        self._buf: deque[RingRecord] = deque(maxlen=capacity)
        self.addFilter(SecretMaskingFilter())
        self.setFormatter(TextFormatter())

    @property
    def capacity(self) -> int:
        return self._buf.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)  # record already masked by our filter
            self._buf.append(
                RingRecord(
                    ts=datetime.fromtimestamp(record.created, UTC),
                    level=record.levelno,
                    logger=record.name,
                    text=text,
                )
            )
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001 - logging contract: report via handleError, never raise
            self.handleError(record)

    def records(self, *, limit: int | None = None, min_level: int = logging.NOTSET) -> list[RingRecord]:
        """Oldest → newest, optionally only ``levelno >= min_level`` and only the last ``limit``."""
        with self.lock:  # type: ignore[union-attr]  # created by Handler.createLock
            items = [r for r in self._buf if r.level >= min_level]
        if limit is not None:
            items = items[-limit:] if limit > 0 else []
        return items

    def tail(self, n: int = 100, *, min_level: int = logging.NOTSET) -> list[str]:
        return [r.text for r in self.records(limit=n, min_level=min_level)]

    def clear(self) -> None:
        with self.lock:  # type: ignore[union-attr]
            self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------------------

_QUEUE_SIZE: Final = 10_000
_NOISY_LOGGERS: Final = ("aiogram.event",)


class _QueueHandler(logging.handlers.QueueHandler):
    """Masks, keeps exception text as a separate field, drops (and counts) records when the queue is full."""

    def __init__(self, q: queue.Queue[logging.LogRecord | None]) -> None:
        super().__init__(q)
        self.dropped = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        mask_record(record)
        prepared = copy.copy(record)
        prepared.exc_info = None  # drop frame references; exc_text already holds the masked traceback
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


@dataclass
class _State:
    handlers: list[logging.Handler]
    listener: logging.handlers.QueueListener | None
    queue: queue.Queue[logging.LogRecord | None] | None


_setup_lock = threading.Lock()
_state: _State | None = None
_ring: RingBuffer | None = None
_atexit_registered = False


def parse_level(level: str | int) -> int:
    """``"info"``/``"WARN"``/``"20"``/``20`` → numeric level; ``ValueError`` for anything else."""
    if isinstance(level, int) and not isinstance(level, bool):
        return level
    name = str(level).strip().upper()
    if name.isdigit():
        return int(name)
    aliases = {"WARN": "WARNING", "FATAL": "CRITICAL"}
    mapping = logging.getLevelNamesMapping()
    resolved = mapping.get(aliases.get(name, name))
    if resolved is None:
        raise ValueError(f"invalid log level: {level!r}")
    return resolved


def _tune_noisy(level: int) -> None:
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)


def set_level(level: str | int) -> None:
    """Change the root level at runtime (``LOG_LEVEL`` is a HOT setting)."""
    lvl = parse_level(level)
    logging.getLogger().setLevel(lvl)
    _tune_noisy(lvl)


def get_ring_buffer() -> RingBuffer:
    """The process ring buffer (created on first use, survives :func:`setup_logging` re-runs)."""
    global _ring  # noqa: PLW0603
    if _ring is None:
        _ring = RingBuffer()
    return _ring


def _teardown_locked() -> None:
    global _state  # noqa: PLW0603
    if _state is None:
        return
    root = logging.getLogger()
    for h in _state.handlers:
        root.removeHandler(h)
    if _state.listener is not None:
        _state.listener.stop()  # drains the queue, then joins the thread
    for h in _state.handlers:
        h.close()
    logging.captureWarnings(False)
    _state = None


def setup_logging(
    level: str | int,
    json: bool = False,
    *,
    stream: TextIO | None = None,
    ring_capacity: int = 2000,
    use_queue: bool = True,
) -> RingBuffer:
    """Configure the root logger (idempotent: re-running replaces only what we installed).

    Returns the ring buffer. ``use_queue=False`` writes synchronously (handy for CLI commands).
    """
    global _state, _ring, _atexit_registered  # noqa: PLW0603
    lvl = parse_level(level)
    formatter: logging.Formatter = JsonFormatter() if json else TextFormatter()
    with _setup_lock:
        _teardown_locked()

        if _ring is None or _ring.capacity != ring_capacity:
            old = _ring.records() if _ring is not None else []
            _ring = RingBuffer(ring_capacity)
            _ring._buf.extend(old[-ring_capacity:])
        _ring.setFormatter(formatter)

        out = logging.StreamHandler(stream if stream is not None else sys.stderr)
        out.setFormatter(formatter)
        out.addFilter(SecretMaskingFilter())
        sinks: list[logging.Handler] = [out, _ring]

        root = logging.getLogger()
        if use_queue:
            q: queue.Queue[logging.LogRecord | None] = queue.Queue(_QUEUE_SIZE)
            qh = _QueueHandler(q)
            qh.addFilter(SecretMaskingFilter())
            listener = logging.handlers.QueueListener(q, *sinks, respect_handler_level=True)
            listener.start()
            _state = _State(handlers=[qh], listener=listener, queue=q)
        else:
            _state = _State(handlers=sinks, listener=None, queue=None)
        for h in _state.handlers:
            root.addHandler(h)
        root.setLevel(lvl)
        _tune_noisy(lvl)
        logging.captureWarnings(True)
        if not _atexit_registered:
            atexit.register(shutdown_logging)
            _atexit_registered = True
        return _ring


def flush_logging() -> None:
    """Block until queued records reach the sinks (tests, shutdown). Never call from hot paths."""
    state = _state
    if state is not None and state.queue is not None:
        state.queue.join()
    for h in state.handlers if state is not None else []:
        h.flush()


def shutdown_logging() -> None:
    """Flush and remove the handlers installed by :func:`setup_logging`."""
    with _setup_lock:
        _teardown_locked()


def dropped_records() -> int:
    """Records dropped because the log queue was full (a sign of a log storm or stuck stderr)."""
    state = _state
    if state is None:
        return 0
    return sum(getattr(h, "dropped", 0) for h in state.handlers)
