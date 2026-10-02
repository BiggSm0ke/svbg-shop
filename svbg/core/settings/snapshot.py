"""Immutable settings snapshot (03 §5.1).

Readers take ``cfg = settings.current()`` once at the start of an operation and work with one consistent
version. A change creates a new snapshot and swaps one reference, so reads never lock and never hit the
database.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any, Final, Literal

from svbg.core.log import MASK

__all__ = ["SOURCES", "SettingsSnapshot", "Source", "effective_source"]

#: Where the effective value of a key comes from (as shown in the UI).
Source = Literal["default", "bot", "env_file", "environ", "locked", "cli", "import", "wizard", "system"]
SOURCES: Final = frozenset(
    {"default", "bot", "env_file", "environ", "locked", "cli", "import", "wizard", "system"}
)

# Stored row sources that read as another effective source.
_ROW_SOURCE_ALIASES: Final = {"env_seed": "environ"}


def effective_source(row_source: str) -> str:
    """Effective source for a stored row's ``source`` column (``env_seed`` reads as ``environ``)."""
    return _ROW_SOURCE_ALIASES.get(row_source, row_source)


class SettingsSnapshot(Mapping[str, Any]):
    """Read-only mapping ``key → typed value`` plus a version and the source of every value.

    Aliases (old key names) are accepted for reading. ``repr`` never shows secret values.
    """

    __slots__ = ("_aliases", "_secrets", "_sources", "_values", "version")

    def __init__(
        self,
        values: Mapping[str, Any],
        sources: Mapping[str, str],
        *,
        version: int = 1,
        aliases: Mapping[str, str] | None = None,
        secrets: frozenset[str] = frozenset(),
    ) -> None:
        missing = set(values) - set(sources)
        if missing:
            raise ValueError(f"no source for keys: {sorted(missing)}")
        self._values: Mapping[str, Any] = MappingProxyType(dict(values))
        self._sources: Mapping[str, str] = MappingProxyType({k: sources[k] for k in values})
        self._aliases: Mapping[str, str] = MappingProxyType(dict(aliases or {}))
        self._secrets = secrets
        self.version = version

    # ---- Mapping

    def _canonical(self, key: str) -> str:
        if key in self._values:
            return key
        return self._aliases.get(key, key)

    def __getitem__(self, key: str) -> Any:
        value = self._values[self._canonical(key)]
        # Lists are stored as tuples-free copies; hand out copies so callers cannot mutate the snapshot.
        return list(value) if isinstance(value, list) else value

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._canonical(key) in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    # ---- extras

    def source(self, key: str) -> str:
        """``default`` | ``bot`` | ``env_file`` | ``environ`` | ``locked`` (| ``cli`` | ``import`` …)."""
        return self._sources[self._canonical(key)]

    def sources(self) -> Mapping[str, str]:
        return self._sources

    def is_secret(self, key: str) -> bool:
        return self._canonical(key) in self._secrets

    def with_changes(self, values: Mapping[str, Any], sources: Mapping[str, str]) -> SettingsSnapshot:
        """A new snapshot (version + 1) with some values replaced."""
        unknown = set(values) - set(self._values)
        if unknown:
            raise KeyError(f"unknown settings: {sorted(unknown)}")
        merged = dict(self._values)
        merged.update(values)
        merged_sources = dict(self._sources)
        merged_sources.update({k: sources[k] for k in values})
        return SettingsSnapshot(
            merged,
            merged_sources,
            version=self.version + 1,
            aliases=self._aliases,
            secrets=self._secrets,
        )

    def as_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Plain dict copy; secrets replaced with ``***`` unless ``mask_secrets=False``."""
        return {
            k: (MASK if mask_secrets and k in self._secrets and v not in (None, "") else v)
            for k, v in self._values.items()
        }

    def __repr__(self) -> str:
        return f"SettingsSnapshot(version={self.version}, values={self.as_dict()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SettingsSnapshot):
            return NotImplemented
        return self.version == other.version and dict(self._values) == dict(other._values)

    def __hash__(self) -> int:
        return hash(self.version)
