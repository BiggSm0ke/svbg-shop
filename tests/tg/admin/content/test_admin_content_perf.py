"""Acceptance (07 stage 3a): 30 buttons with visibility conditions add ≤ 5 ms to the p95 of a click."""

from __future__ import annotations

import statistics
import time

from tests.tg.admin.content.kit import OWNER, USER, CEnv
from tests.tg.ui.ui_harness import callback

CLICKS = 150

CONDITIONS = [
    {"sub": "expired"},
    {"sub": ["trial", "active"]},
    {"all": [{"sub": "active"}, {"days_left": {"lte": 3}}]},
    {"balance_minor": {"lt": 10_000}},
    {"role": {"gte": "admin"}},
    {"any": [{"lang": "en"}, {"has_paid": False}]},
]


def p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[18]


async def measure(ce: CEnv) -> float:
    for _ in range(10):  # warm up caches (ui_state, user, memoized buttons)
        await ce.router.dispatch_callback(callback(USER, "v1:home:o"))
    samples = []
    for _ in range(CLICKS):
        started = time.perf_counter()
        await ce.router.dispatch_callback(callback(USER, "v1:home:o"))
        samples.append((time.perf_counter() - started) * 1000)
    return p95(samples)


async def test_thirty_conditional_buttons_cost_at_most_5ms_p95(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user", sub_state="expired", days_left=0, balance_minor=500)
    ce.screens.edit_mode.enable(await ce.add(OWNER + 7, "owner"))  # an editor exists, the user is not one
    base = await measure(ce)

    entry = ce.env.content.get_screen("home")
    assert entry is not None
    ver = entry.screen.version
    for i in range(30):
        res = await ce.editor.add_button(
            entry.id,
            label={"ru": f"Кнопка {i} · {{balance}}" if i % 5 == 0 else f"Кнопка {i}"},
            action="system:buy",
            row=10 + i // 6,
            visible_if=CONDITIONS[i % len(CONDITIONS)],
            expected_version=ver,
            actor=None,
        )
        assert res.version is not None
        ver = res.version
    shown = await measure(ce)
    visible = [t for t, _ in ce.buttons() if t.startswith("Кнопка")]
    assert visible  # some of them are visible to this user, some are not
    assert len(visible) < 30
    assert shown - base <= 5.0, f"p95 grew from {base:.2f} ms to {shown:.2f} ms"


async def test_a_user_click_costs_no_sql_while_someone_edits(ce: CEnv) -> None:
    await ce.add(USER, "user", sub_state="active", days_left=10)
    editor = await ce.add(OWNER, "owner")
    assert ce.screens.edit_mode.enable(editor)  # the «✏️» mode is on for another user
    await ce.router.dispatch_callback(callback(OWNER, "v1:home:o"))
    assert ce.buttons()[-1][0] == "👁 Как видит…"
    for _ in range(3):  # warm up caches (ui_state, user, memoized buttons)
        await ce.router.dispatch_callback(callback(USER, "v1:home:o"))
    before = ce.db.queries
    for _ in range(10):
        await ce.router.dispatch_callback(callback(USER, "v1:home:o"))
    assert ce.db.queries == before
    assert "✏️ Экран" not in [t for t, _ in ce.buttons()]
