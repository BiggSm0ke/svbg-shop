"""Settings: typed registry, immutable snapshot, apply pipeline with probe/rollback, live ``.env`` mirror.

Typical wiring (see ``svbg/app.py``)::

    boot = load_bootstrap(env_path, os.environ)            # before the DB: token, key, DSN
    service = SettingsService(db, core_registry(), Crypto([boot.secret_key]), components,
                              environ=os.environ, env_path=env_path)
    await service.load()
    mirror = EnvMirror(service, service.registry, env_path, on_notice=notify_owner)
    await mirror.start()
    result = await service.apply([Change("TRIAL_DAYS", "5")], source="bot", actor_id=owner_id)

Submodules are imported lazily by users; this package re-exports the public names.
"""

from __future__ import annotations

from svbg.core.settings.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    default_env_path,
    load_bootstrap,
    read_bootstrap,
    wait_for_token,
)
from svbg.core.settings.mirror import EnvMirror, MirrorNotice, MirrorStatus
from svbg.core.settings.registry import SECTIONS, Apply, Registry, SettingDef, core_registry
from svbg.core.settings.service import (
    RESET,
    ApplyResult,
    Change,
    SettingsError,
    SettingsService,
    StaleSnapshotError,
)
from svbg.core.settings.snapshot import SettingsSnapshot
from svbg.core.settings.values import SECRET_PLACEHOLDER, SettingValueError

__all__ = [
    "RESET",
    "SECRET_PLACEHOLDER",
    "SECTIONS",
    "Apply",
    "ApplyResult",
    "BootstrapConfig",
    "BootstrapError",
    "Change",
    "EnvMirror",
    "MirrorNotice",
    "MirrorStatus",
    "Registry",
    "SettingDef",
    "SettingValueError",
    "SettingsError",
    "SettingsService",
    "SettingsSnapshot",
    "StaleSnapshotError",
    "core_registry",
    "default_env_path",
    "load_bootstrap",
    "read_bootstrap",
    "wait_for_token",
]
