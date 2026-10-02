from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from svbg.core import clock
from svbg.core.bus import ALL, Event, EventBus, Handler

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def __call__(self, event: Event) -> None:
        self.seen.append(event.name)


ErrorLog = list[tuple[str, Handler, BaseException]]


def make_bus(**kw: Any) -> tuple[EventBus, ErrorLog]:
    errors: ErrorLog = []

    def on_error(event: Event, handler: Handler, exc: BaseException) -> None:
        errors.append((event.name, handler, exc))

    return EventBus(on_error=on_error, **kw), errors


# -- Event ---------------------------------------------------------------------------------------


def test_event_defaults_and_immutability() -> None:
    fc = clock.FrozenClock(T0)
    clock.set_clock(fc)
    try:
        src = {"a": 1}
        e = Event("payment.succeeded", src)
    finally:
        clock.reset_clock()
    assert e.ts == T0
    src["a"] = 2
    assert e.payload["a"] == 1  # copied at creation
    with pytest.raises(TypeError):
        e.payload["b"] = 3  # type: ignore[index]
    with pytest.raises(AttributeError):
        e.name = "x"  # type: ignore[misc]
    assert Event("x").payload == {}


@pytest.mark.parametrize("bad", ["", "*", "Payment", "a b", "a.", ".a", "a..b", "a.*", "x" * 129])
def test_event_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError):
        Event(bad)


def test_event_rejects_naive_ts() -> None:
    with pytest.raises(ValueError, match="aware"):
        Event("x", ts=datetime(2026, 1, 1))


# -- subscriptions -------------------------------------------------------------------------------


async def test_exact_wildcard_and_prefix_matching() -> None:
    bus, errors = make_bus()
    exact, star, prefix = Recorder(), Recorder(), Recorder()
    bus.subscribe("payment.succeeded", exact)
    bus.subscribe(ALL, star)
    bus.subscribe("payment.*", prefix)

    for name in ("payment.succeeded", "payment.refund.done", "payments.x", "payment", "user.created"):
        await bus.publish(Event(name))

    assert exact.seen == ["payment.succeeded"]
    assert prefix.seen == ["payment.succeeded", "payment.refund.done"]
    assert star.seen == ["payment.succeeded", "payment.refund.done", "payments.x", "payment", "user.created"]
    assert errors == []


async def test_publish_without_subscribers() -> None:
    bus = EventBus()
    assert await bus.publish(Event("nobody.listens")) == 0


@pytest.mark.parametrize("bad", ["", "a b", "Pay.*", "*.x", "a.**", "a*"])
def test_subscribe_rejects_bad_patterns(bad: str) -> None:
    with pytest.raises(ValueError):
        EventBus().subscribe(bad, Recorder())


def test_subscribe_rejects_sync_function() -> None:
    def sync_handler(event: Event) -> None:
        pass

    with pytest.raises(TypeError, match="async"):
        EventBus().subscribe("x", sync_handler)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EventBus().subscribe("x", 42)  # type: ignore[arg-type]


async def test_duplicate_subscriptions_deliver_once() -> None:
    bus = EventBus()
    rec = Recorder()
    bus.subscribe("a.b", rec)
    bus.subscribe("a.b", rec)
    bus.subscribe("a.*", rec)
    bus.subscribe(ALL, rec)
    await bus.publish(Event("a.b"))
    assert rec.seen == ["a.b"]


async def test_unsubscribe() -> None:
    bus = EventBus()
    rec, star = Recorder(), Recorder()
    off = bus.subscribe("a", rec)
    off_star = bus.subscribe(ALL, star)
    bus.subscribe("b.*", rec)
    off()
    off()  # idempotent
    off_star()
    assert bus.unsubscribe("b.*", rec) is True
    assert bus.unsubscribe("b.*", rec) is False
    assert bus.unsubscribe(ALL, star) is False
    await bus.publish(Event("a"))
    await bus.publish(Event("b.c"))
    assert rec.seen == [] and star.seen == []
    assert bus.handlers_for("a") == ()


async def test_bound_methods_dedupe_and_unsubscribe() -> None:
    class Service:
        def __init__(self) -> None:
            self.n = 0

        async def on_event(self, event: Event) -> None:
            self.n += 1

    svc = Service()
    bus = EventBus()
    bus.subscribe("x", svc.on_event)
    bus.subscribe("x", svc.on_event)  # a new bound-method object, equal to the first
    bus.subscribe(ALL, svc.on_event)
    await bus.publish(Event("x"))
    assert svc.n == 1
    assert bus.unsubscribe("x", svc.on_event) is True
    assert bus.unsubscribe(ALL, svc.on_event) is True
    await bus.publish(Event("x"))
    assert svc.n == 1


async def test_unsubscribe_during_publish_is_safe() -> None:
    bus = EventBus()
    calls: list[str] = []
    off_holder: list[Any] = []

    async def first(event: Event) -> None:
        calls.append("first")
        off_holder[0]()

    async def second(event: Event) -> None:
        calls.append("second")

    bus.subscribe("x", first)
    off_holder.append(bus.subscribe("x", second))
    await bus.publish(Event("x"))  # snapshot taken before delivery: second still runs
    await bus.publish(Event("x"))
    assert calls == ["first", "second", "first"]


# -- isolation -----------------------------------------------------------------------------------


async def test_failing_handler_isolated() -> None:
    bus, errors = make_bus()
    ok1, ok2 = Recorder(), Recorder()

    async def boom(event: Event) -> None:
        raise RuntimeError("handler broke")

    bus.subscribe("x", ok1)
    bus.subscribe("x", boom)
    bus.subscribe(ALL, ok2)
    failed = await bus.publish(Event("x", {"k": 1}))
    assert failed == 1
    assert ok1.seen == ["x"] and ok2.seen == ["x"]
    assert len(errors) == 1
    name, handler, exc = errors[0]
    assert name == "x" and handler is boom and isinstance(exc, RuntimeError)


async def test_single_failing_handler_does_not_raise() -> None:
    bus, errors = make_bus()

    async def boom(event: Event) -> None:
        raise KeyError("x")

    bus.subscribe("x", boom)
    assert await bus.publish(Event("x")) == 1
    assert isinstance(errors[0][2], KeyError)


async def test_failure_without_hook_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()

    async def boom(event: Event) -> None:
        raise RuntimeError("bad")

    bus.subscribe("x", boom)
    with caplog.at_level("ERROR", logger="svbg.core.bus"):
        assert await bus.publish(Event("x", {"password": "hunter2-secret"})) == 1
    assert "boom" in caplog.text
    assert "hunter2-secret" not in caplog.text  # payload is never logged


async def test_on_error_hook_failure_is_swallowed() -> None:
    async def bad_hook(event: Event, handler: Handler, exc: BaseException) -> None:
        raise ValueError("hook broke")

    bus = EventBus(on_error=bad_hook)
    rec = Recorder()

    async def boom(event: Event) -> None:
        raise RuntimeError("x")

    bus.subscribe("x", boom)
    bus.subscribe("x", rec)
    assert await bus.publish(Event("x")) == 1
    assert rec.seen == ["x"]


async def test_handler_timeout() -> None:
    bus, errors = make_bus(handler_timeout=0.05)
    rec = Recorder()

    async def slow(event: Event) -> None:
        await asyncio.sleep(5)

    bus.subscribe("x", slow)
    bus.subscribe("x", rec)
    started = time.perf_counter()
    assert await bus.publish(Event("x")) == 1
    assert time.perf_counter() - started < 1
    assert rec.seen == ["x"]
    assert isinstance(errors[0][2], TimeoutError)


def test_bad_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        EventBus(handler_timeout=0)


async def test_handler_internal_cancellation_is_isolated() -> None:
    bus, errors = make_bus(handler_timeout=None)
    rec = Recorder()

    async def cancels_itself(event: Event) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        fut.cancel()
        await fut  # raises CancelledError although nobody cancelled the publisher

    bus.subscribe("x", cancels_itself)
    assert await bus.publish(Event("x")) == 1  # single-handler fast path
    bus.subscribe("x", rec)
    assert await bus.publish(Event("x")) == 1  # concurrent path
    assert rec.seen == ["x"]
    assert all(isinstance(e[2], asyncio.CancelledError) for e in errors)


async def test_publisher_cancellation_propagates_to_handlers() -> None:
    bus, errors = make_bus()
    cancelled: list[str] = []

    def make(tag: str) -> Handler:
        async def h(event: Event) -> None:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(tag)
                raise

        return h

    bus.subscribe("x", make("a"))
    bus.subscribe("x", make("b"))
    task = asyncio.create_task(bus.publish(Event("x")))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(cancelled) == ["a", "b"]
    assert errors == []


# -- concurrency ---------------------------------------------------------------------------------


async def test_handlers_run_concurrently() -> None:
    bus = EventBus()

    async def slow(event: Event) -> None:
        await asyncio.sleep(0.2)

    for _ in range(5):
        bus.subscribe("x", Recorder())  # cheap ones
    handlers = []
    for _ in range(5):

        async def h(event: Event) -> None:
            await slow(event)

        handlers.append(h)
        bus.subscribe("x", h)
    started = time.perf_counter()
    assert await bus.publish(Event("x")) == 0
    assert time.perf_counter() - started < 0.6  # sequential would be >= 1.0 s


async def test_handlers_can_rendezvous() -> None:
    """Two handlers that wait for each other would deadlock if run one after another."""
    bus, errors = make_bus(handler_timeout=2)
    a_ready, b_ready = asyncio.Event(), asyncio.Event()

    async def a(event: Event) -> None:
        a_ready.set()
        await b_ready.wait()

    async def b(event: Event) -> None:
        b_ready.set()
        await a_ready.wait()

    bus.subscribe("x", a)
    bus.subscribe("x", b)
    assert await bus.publish(Event("x")) == 0
    assert errors == []


async def test_concurrent_publishers() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def h(event: Event) -> None:
        await asyncio.sleep(0)
        seen.append(event.payload["i"])

    bus.subscribe("x.*", h)
    await asyncio.gather(*(bus.publish(Event("x.y", {"i": i})) for i in range(200)))
    assert sorted(seen) == list(range(200))


async def test_publish_rejects_non_event() -> None:
    with pytest.raises(TypeError):
        await EventBus().publish("x")  # type: ignore[arg-type]


# -- background delivery -------------------------------------------------------------------------


async def test_publish_nowait_and_drain() -> None:
    bus = EventBus()
    rec = Recorder()
    assert bus.publish_nowait(Event("nobody")) is None
    bus.subscribe("x", rec)
    task = bus.publish_nowait(Event("x"))
    assert task is not None
    assert rec.seen == []  # not delivered synchronously
    await bus.drain()
    assert rec.seen == ["x"]
    assert task.result() == 0


async def test_drain_timeout_cancels_leftovers() -> None:
    bus = EventBus(handler_timeout=None)
    cancelled = asyncio.Event()

    async def slow(event: Event) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    bus.subscribe("x", slow)
    bus.publish_nowait(Event("x"))
    await asyncio.sleep(0)
    await bus.drain(grace_s=0.05)
    assert cancelled.is_set()


async def test_aclose_rejects_new_background_work() -> None:
    bus = EventBus()
    bus.subscribe("x", Recorder())
    await bus.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        bus.publish_nowait(Event("x"))
    assert await bus.publish(Event("x")) == 0  # direct publish still works
    with pytest.raises(TypeError):
        EventBus().publish_nowait("x")  # type: ignore[arg-type]
