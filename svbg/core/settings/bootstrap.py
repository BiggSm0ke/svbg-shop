"""Bootstrap configuration: what is needed before the database and Telegram (03 §2.2, §3.2).

Priority for bootstrap keys: ``LOCKED_KEYS ∩ os.environ`` → ``data/.env`` → ``os.environ`` → default.
An empty value in the file (``DATABASE_URL=``, as in the first-start template) is "not set", not a value:
the environment still applies.
``SECRET_KEY`` is generated on first start and written to the file (atomically). Without ``BOT_TOKEN`` the
process does not crash-loop: :func:`wait_for_token` polls the file until the token appears.

Everything here is synchronous file work except :func:`wait_for_token`; nothing imports the database.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from svbg.boot.envfile import EnvDocument, EnvFileError, read_text, write_atomic
from svbg.core.clock import monotonic
from svbg.core.crypto import generate_key
from svbg.core.log import MASK, register_secret
from svbg.core.settings import values
from svbg.core.settings.envtext import comment_lines
from svbg.core.settings.registry import Registry, SettingDef, core_registry

__all__ = [
    "DEFAULT_DATA_DIR",
    "ENV_FILE_NAME",
    "BootstrapConfig",
    "BootstrapError",
    "default_env_path",
    "environ_value",
    "file_values",
    "load_bootstrap",
    "locked_keys",
    "read_bootstrap",
    "wait_for_token",
]

log = logging.getLogger("svbg.core.settings.bootstrap")

DEFAULT_DATA_DIR: Final = "./data"
ENV_FILE_NAME: Final = ".env"

_M = {
    "bad_key": "SECRET_KEY в {path} повреждён: {error}. Верните прежний ключ из резервной копии (.env.bak) — "
    "без него секреты в БД не расшифровать.",
    "cannot_save": "Не удалось сохранить сгенерированный SECRET_KEY в {path}: {error}. "
    "Проверьте права на каталог (например: sudo chown -R 1000:1000 ./data).",
    "file": "Файл {path} не читается: {error}",
    "waiting": "Жду BOT_TOKEN в {path}: впишите строку BOT_TOKEN=<токен от @BotFather>, перезапуск не нужен.",
}


class BootstrapError(Exception):
    """Bootstrap cannot continue. The message is owner-facing (Russian) and contains no secrets."""


@dataclass(frozen=True)
class BootstrapConfig:
    env_path: Path
    values: Mapping[str, Any]
    sources: Mapping[str, str]  # env_file | environ | default | locked
    locked: frozenset[str] = frozenset()
    problems: Mapping[str, str] = field(default_factory=dict)
    generated: tuple[str, ...] = ()
    file_exists: bool = False
    secret_keys: frozenset[str] = frozenset()

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def bot_token(self) -> str | None:
        return self.values.get("BOT_TOKEN")

    @property
    def has_token(self) -> bool:
        return bool(self.bot_token)

    @property
    def secret_key(self) -> str | None:
        return self.values.get("SECRET_KEY")

    @property
    def database_url(self) -> str | None:
        return self.values.get("DATABASE_URL")

    @property
    def owner_ids(self) -> list[int]:
        return list(self.values.get("OWNER_IDS") or [])

    @property
    def data_dir(self) -> Path:
        return Path(self.values.get("DATA_DIR") or DEFAULT_DATA_DIR)

    def __repr__(self) -> str:
        shown = {k: (MASK if v and k in self.secret_keys else v) for k, v in self.values.items()}
        return (
            f"BootstrapConfig(env_path={str(self.env_path)!r}, values={shown!r}, "
            f"problems={dict(self.problems)!r})"
        )


def default_env_path(environ: Mapping[str, str]) -> Path:
    """``$DATA_DIR/.env`` (``./data/.env`` by default)."""
    return Path(environ.get("DATA_DIR") or DEFAULT_DATA_DIR) / ENV_FILE_NAME


def environ_value(environ: Mapping[str, str], defn: SettingDef) -> str | None:
    """Value of a key (or one of its aliases) in the process environment."""
    for name in defn.names:
        if name in environ:
            return environ[name]
    return None


def file_values(doc: EnvDocument | None, registry: Registry) -> dict[str, str]:
    """Canonical key → raw value from a parsed ``.env``; the canonical name wins over aliases."""
    if doc is None:
        return {}
    raw = doc.as_dict()
    out: dict[str, str] = {}
    for defn in registry.all():
        for name in defn.names:
            if name in raw:
                out[defn.key] = raw[name]
                break
    return out


def locked_keys(names: list[str] | None, environ: Mapping[str, str], registry: Registry) -> frozenset[str]:
    """Canonical keys listed in ``LOCKED_KEYS`` that are actually present in the environment."""
    out: set[str] = set()
    for name in names or []:
        defn = registry.find(name)
        if defn is not None and environ_value(environ, defn) is not None:
            out.add(defn.key)
    return frozenset(out)


def read_bootstrap(
    env_path: Path, environ: Mapping[str, str], *, registry: Registry | None = None
) -> BootstrapConfig:
    """Resolve bootstrap keys without writing anything. Invalid values become ``problems`` (never raise for
    them); an unreadable file raises :class:`BootstrapError`."""
    reg = registry or core_registry()
    path = Path(env_path)
    try:
        text = read_text(path)
    except (EnvFileError, OSError) as exc:
        raise BootstrapError(_M["file"].format(path=path, error=_os_error(exc))) from None
    doc = EnvDocument.parse(text) if text is not None else None
    in_file = file_values(doc, reg)
    vals: dict[str, Any] = {}
    sources: dict[str, str] = {}
    problems: dict[str, str] = {}

    locked_def = reg.find("LOCKED_KEYS")
    locked_names: list[str] = []
    if locked_def is not None:
        raw_locked = in_file.get("LOCKED_KEYS")
        if raw_locked is None:
            raw_locked = environ_value(environ, locked_def)
        try:
            locked_names = values.parse_or_default(locked_def, raw_locked) if raw_locked else []
        except values.SettingValueError as exc:
            problems["LOCKED_KEYS"] = str(exc)
    locked = locked_keys(locked_names, environ, reg)

    for defn in reg.all():
        if not defn.bootstrap:
            continue
        candidates: list[tuple[str, str | None]] = []
        if defn.key in locked:
            candidates.append(("locked", environ_value(environ, defn)))
        else:
            if defn.in_file:
                candidates.append(("env_file", in_file.get(defn.key)))
            candidates.append(("environ", environ_value(environ, defn)))
        vals[defn.key], sources[defn.key] = defn.default, "default"
        for source, raw in candidates:
            if raw is None or (source == "env_file" and raw.strip() == ""):
                continue
            try:
                vals[defn.key], sources[defn.key] = values.parse_or_default(defn, raw), source
                break
            except values.SettingValueError as exc:
                problems.setdefault(defn.key, f"{_where(source)}: {exc}")
    for defn in reg.all():
        if defn.bootstrap and defn.is_secret and isinstance(vals.get(defn.key), str):
            register_secret(vals[defn.key])
    return BootstrapConfig(
        env_path=path,
        values=vals,
        sources=sources,
        locked=locked,
        problems=problems,
        file_exists=text is not None,
        secret_keys=frozenset(d.key for d in reg.all() if d.is_secret),
    )


def load_bootstrap(
    env_path: Path, environ: Mapping[str, str], *, registry: Registry | None = None
) -> BootstrapConfig:
    """:func:`read_bootstrap`, then generate ``SECRET_KEY`` if it is absent and persist it to the file.

    Raises :class:`BootstrapError` if the key in the file is broken (a new key would orphan every secret in
    the database) or if a generated key cannot be saved.
    """
    reg = registry or core_registry()
    cfg = read_bootstrap(env_path, environ, registry=reg)
    if "SECRET_KEY" in cfg.problems:
        raise BootstrapError(_M["bad_key"].format(path=cfg.env_path, error=cfg.problems["SECRET_KEY"]))
    if cfg.secret_key:
        return cfg
    defn = reg.get("SECRET_KEY")
    key = generate_key()
    register_secret(key)
    path = cfg.env_path
    try:
        text = read_text(path)
        doc = EnvDocument.parse(text or "")
        doc.set("SECRET_KEY", key, comment=comment_lines(defn), section=reg.section_title(defn.section))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, doc.render())
    except (EnvFileError, OSError) as exc:
        raise BootstrapError(_M["cannot_save"].format(path=path, error=_os_error(exc))) from None
    log.warning("Сгенерирован новый SECRET_KEY и записан в %s — сохраните копию файла", path)
    new_values = {**cfg.values, "SECRET_KEY": key}
    new_sources = {**cfg.sources, "SECRET_KEY": "env_file"}
    return BootstrapConfig(
        env_path=path,
        values=new_values,
        sources=new_sources,
        locked=cfg.locked,
        problems=cfg.problems,
        generated=("SECRET_KEY",),
        file_exists=True,
        secret_keys=cfg.secret_keys,
    )


async def wait_for_token(
    env_path: Path,
    environ: Mapping[str, str],
    *,
    registry: Registry | None = None,
    interval: float = 2.0,
    max_wait: float | None = None,
    on_waiting: Callable[[str], Awaitable[None] | None] | None = None,
) -> BootstrapConfig:
    """Poll the file until a valid ``BOT_TOKEN`` appears; returns the bootstrap config as
    :func:`load_bootstrap` does (``SECRET_KEY`` generated again if the owner rewrote the file meanwhile).

    Logs (and calls ``on_waiting`` with) an owner-facing hint once; after ``max_wait`` s → `TimeoutError`.
    Raises :class:`BootstrapError` like :func:`load_bootstrap`.
    """
    reg = registry or core_registry()
    deadline = None if max_wait is None else monotonic() + max_wait
    announced = False
    last_error: str | None = None
    while True:
        try:
            cfg = await asyncio.to_thread(read_bootstrap, env_path, environ, registry=reg)
        except BootstrapError as exc:
            cfg = None
            if str(exc) != last_error:
                log.warning("%s", exc)
                last_error = str(exc)
        if cfg is not None and cfg.has_token:
            if announced:
                log.info("BOT_TOKEN найден в %s, продолжаю запуск", env_path)
            return await asyncio.to_thread(load_bootstrap, env_path, environ, registry=reg)
        if not announced:
            message = _M["waiting"].format(path=env_path)
            log.warning("%s", message)
            if on_waiting is not None:
                res = on_waiting(message)
                if res is not None:
                    await res
            announced = True
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError(f"BOT_TOKEN did not appear in {env_path}")
        await asyncio.sleep(interval)


def _where(source: str) -> str:
    return {"env_file": "в .env", "environ": "в окружении", "locked": "в окружении (LOCKED_KEYS)"}.get(
        source, source
    )


def _os_error(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    if isinstance(exc, EnvFileError):
        return str(exc)
    return type(exc).__name__
