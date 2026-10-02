"""Stable error fingerprints.

``fingerprint = sha1(exception type + normalized own frames + place)`` where *own frames* are traceback
frames from files under the ``svbg`` package, normalized to ``<relative posix path>:<function>`` (no line
numbers). Messages are not part of the fingerprint, so ids/numbers in messages never split a group, and
editing code above a failing line does not change it. Consecutive duplicate frames (recursion) collapse.
"""

from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from types import TracebackType

import svbg

_ROOT = os.path.normcase(os.path.abspath(Path(svbg.__file__).parent))
_ROOT_PREFIX = _ROOT + os.sep
_MAX_FRAMES = 64


@functools.lru_cache(maxsize=4096)
def own_relpath(filename: str) -> str | None:
    """``svbg/<...>.py`` (posix) if ``filename`` lies inside the svbg package, else ``None``."""
    if not filename or filename.startswith("<"):
        return None
    norm = os.path.normcase(os.path.abspath(filename))
    if not norm.startswith(_ROOT_PREFIX):
        return None
    rel = norm[len(_ROOT_PREFIX) :].replace(os.sep, "/")
    return f"svbg/{rel}"


def own_frames(tb: TracebackType | None) -> list[str]:
    """Normalized ``path:function`` entries of own frames, outermost first, recursion collapsed."""
    frames: list[str] = []
    while tb is not None:
        code = tb.tb_frame.f_code
        rel = own_relpath(code.co_filename)
        if rel is not None:
            entry = f"{rel}:{code.co_qualname}"
            if not frames or frames[-1] != entry:
                frames.append(entry)
        tb = tb.tb_next
    if len(frames) > _MAX_FRAMES:
        # Keep both ends: where it entered our code and where it failed.
        half = _MAX_FRAMES // 2
        frames = [*frames[:half], "…", *frames[-half:]]
    return frames


def exc_type_name(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    if module in {"builtins", "__main__"}:
        return cls.__qualname__
    return f"{module}.{cls.__qualname__}"


def fingerprint(exc: BaseException, place: str) -> str:
    """40-hex sha1 identifying "the same error in the same place"."""
    parts = [exc_type_name(exc), *own_frames(exc.__traceback__), f"@{place}"]
    digest = hashlib.sha1("\n".join(parts).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()
