from __future__ import annotations

from pathlib import Path

import svbg
from svbg.core.errors.fingerprint import fingerprint, own_frames, own_relpath

ROOT = Path(svbg.__file__).parent
# A virtual file inside the svbg package: frames compiled with this filename count as "own code".
FAKE_FILE = str(ROOT / "core" / "errors" / "_virtual_fp_module.py")

SOURCE = """
def inner(x):
    raise {exc}(f"order {{x}} failed for user {{x * 7}}")


def outer(x):
    inner(x)


def recurse(n, x):
    if n == 0:
        inner(x)
    recurse(n - 1, x)
"""


def _raise(*, shift: int = 0, x: int = 1, exc: str = "ValueError", fn: str = "outer", depth: int = 0):
    ns: dict[str, object] = {}
    code = compile("\n" * shift + SOURCE.format(exc=exc), FAKE_FILE, "exec")
    exec(code, ns)
    try:
        if fn == "recurse":
            ns["recurse"](depth, x)  # type: ignore[operator]
        else:
            ns[fn](x)  # type: ignore[operator]
    except Exception as e:
        return e
    raise AssertionError("did not raise")


def test_stable_across_line_shifts() -> None:
    a = fingerprint(_raise(shift=0), "screen:home")
    b = fingerprint(_raise(shift=17), "screen:home")
    assert a == b
    assert len(a) == 40
    assert all(c in "0123456789abcdef" for c in a)


def test_stable_across_messages_with_numbers() -> None:
    a = fingerprint(_raise(x=1), "screen:home")
    b = fingerprint(_raise(x=987654321), "screen:home")
    assert a == b
    assert str(_raise(x=1)) != str(_raise(x=2))


def test_differs_by_type_place_and_frames() -> None:
    base = fingerprint(_raise(), "screen:home")
    assert fingerprint(_raise(exc="KeyError"), "screen:home") != base
    assert fingerprint(_raise(), "screen:buy") != base
    assert fingerprint(_raise(fn="inner"), "screen:home") != base


def test_recursion_depth_collapses() -> None:
    a = fingerprint(_raise(fn="recurse", depth=2), "job:x")
    b = fingerprint(_raise(fn="recurse", depth=9), "job:x")
    assert a == b


def test_only_own_frames_are_used() -> None:
    exc = _raise()
    frames = own_frames(exc.__traceback__)
    assert frames == [
        "svbg/core/errors/_virtual_fp_module.py:outer",
        "svbg/core/errors/_virtual_fp_module.py:inner",
    ]
    # this test file is outside svbg/
    assert own_relpath(__file__) is None
    assert own_relpath("<string>") is None
    assert own_relpath("") is None


def test_exception_without_traceback() -> None:
    a = fingerprint(RuntimeError("x"), "p")
    b = fingerprint(RuntimeError("y 123"), "p")
    assert a == b
    assert fingerprint(RuntimeError("x"), "q") != a
