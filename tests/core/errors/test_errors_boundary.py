from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from svbg.core.errors import guard, timeout_guard, was_captured


class FakeHub:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def capture(
        self,
        exc: BaseException,
        place: str,
        *,
        module: str | None = None,
        user_id: int | None = None,
        context: Mapping[str, Any] | None = None,
        handled: str = "",
    ) -> str | None:
        self.calls.append(
            {
                "exc": exc,
                "place": place,
                "module": module,
                "user_id": user_id,
                "context": context,
                "handled": handled,
            }
        )
        if self.fail:
            raise RuntimeError("hub broken")
        return "fp"


async def test_guard_captures_and_suppresses() -> None:
    hub = FakeHub()
    seen: list[BaseException] = []
    async with guard(
        "screen:home",
        hub=hub,
        module="core",
        user_id=7,
        on_error=seen.append,
        handled="показан аварийный экран",
    ):
        raise ValueError("boom")
    assert len(hub.calls) == 1
    call = hub.calls[0]
    assert call["place"] == "screen:home"
    assert call["module"] == "core"
    assert call["user_id"] == 7
    assert call["handled"] == "показан аварийный экран"
    assert isinstance(seen[0], ValueError)
    assert was_captured(seen[0])


async def test_guard_success_path() -> None:
    hub = FakeHub()
    async with guard("p", hub=hub):
        pass
    assert hub.calls == []


async def test_async_on_error() -> None:
    hub = FakeHub()
    shown: list[str] = []

    async def fallback(exc: BaseException) -> None:
        await asyncio.sleep(0)
        shown.append(type(exc).__name__)

    async with guard("p", hub=hub, on_error=fallback):
        raise KeyError("k")
    assert shown == ["KeyError"]


async def test_reraise_and_nested_capture_once() -> None:
    hub = FakeHub()
    with pytest.raises(ValueError):
        async with guard("outer", hub=hub, reraise=True), guard("inner", hub=hub, reraise=True):
            raise ValueError("x")
    assert [c["place"] for c in hub.calls] == ["inner"]


async def test_cancelled_error_is_not_captured() -> None:
    hub = FakeHub()
    on_error_calls: list[BaseException] = []
    with pytest.raises(asyncio.CancelledError):
        async with guard("p", hub=hub, on_error=on_error_calls.append):
            raise asyncio.CancelledError
    assert hub.calls == []
    assert on_error_calls == []


async def test_real_task_cancellation_propagates() -> None:
    hub = FakeHub()
    started = asyncio.Event()

    async def worker() -> None:
        async with guard("p", hub=hub):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.calls == []


async def test_base_exceptions_pass_through() -> None:
    hub = FakeHub()
    with pytest.raises(SystemExit):
        async with guard("p", hub=hub):
            raise SystemExit(1)
    with pytest.raises(KeyboardInterrupt):
        async with guard("p", hub=hub):
            raise KeyboardInterrupt
    assert hub.calls == []


async def test_on_error_failure_is_captured_not_propagated() -> None:
    hub = FakeHub()

    def bad_fallback(exc: BaseException) -> None:
        raise RuntimeError("fallback failed")

    async with guard("screen:x", hub=hub, on_error=bad_fallback):
        raise ValueError("x")
    assert [c["place"] for c in hub.calls] == ["screen:x", "screen:x:on_error"]


async def test_broken_hub_does_not_break_boundary() -> None:
    hub = FakeHub(fail=True)
    async with guard("p", hub=hub):
        raise ValueError("x")
    assert len(hub.calls) == 1


async def test_no_hub_only_logs() -> None:
    async with guard("p", hub=None):
        raise ValueError("x")


async def test_decorator() -> None:
    hub = FakeHub()

    @guard("job:sum", hub=hub)
    async def add(a: int, b: int) -> int:
        if a < 0:
            raise ValueError("negative")
        return a + b

    assert await add(1, 2) == 3
    assert await add(-1, 2) is None
    # shared decorator instance is safe for concurrent calls
    results = await asyncio.gather(*(add(i - 5, 1) for i in range(10)))
    assert results == [None] * 5 + [1, 2, 3, 4, 5]
    assert len(hub.calls) == 6
    assert add.__name__ == "add"


def test_decorator_rejects_sync() -> None:
    with pytest.raises(TypeError):
        guard("p", hub=None)(lambda: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        timeout_guard("p", 1, hub=None)(lambda: None)  # type: ignore[arg-type]


async def test_timeout_guard_captures_timeout() -> None:
    hub = FakeHub()
    calls: list[BaseException] = []
    async with timeout_guard("update", 0.05, hub=hub, on_error=calls.append, context={"u": 1}):
        await asyncio.sleep(5)
    assert len(hub.calls) == 1
    assert isinstance(hub.calls[0]["exc"], TimeoutError)
    assert hub.calls[0]["context"] == {"u": 1, "timeout_s": 0.05}
    assert isinstance(calls[0], TimeoutError)


async def test_timeout_guard_fast_path_and_errors() -> None:
    hub = FakeHub()
    async with timeout_guard("update", 1, hub=hub):
        await asyncio.sleep(0)
    assert hub.calls == []
    async with timeout_guard("update", 1, hub=hub):
        raise ValueError("inside")
    assert isinstance(hub.calls[0]["exc"], ValueError)


async def test_timeout_guard_external_cancel_propagates() -> None:
    hub = FakeHub()
    started = asyncio.Event()

    async def worker() -> None:
        async with timeout_guard("update", 30, hub=hub):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.calls == []


async def test_timeout_guard_decorator_and_validation() -> None:
    hub = FakeHub()

    @timeout_guard("job:slow", 0.05, hub=hub)
    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    @timeout_guard("job:fast", 1, hub=hub)
    async def fast() -> int:
        return 2

    assert await asyncio.gather(slow(), slow(), fast()) == [None, None, 2]
    assert [c["place"] for c in hub.calls] == ["job:slow", "job:slow"]
    with pytest.raises(ValueError):
        timeout_guard("p", 0, hub=hub)


async def test_timeout_guard_instance_not_reentrant() -> None:
    tg = timeout_guard("p", 1, hub=None)
    async with tg:
        with pytest.raises(RuntimeError):
            await tg.__aenter__()
