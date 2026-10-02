"""Locations from the panel: add/update/missing/return, broken plans with alerts, the empty-panel guard."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from svbg.catalog.locations import BROKEN_PREFIX, attention_key, sync_locations
from svbg.catalog.service import CatalogService
from svbg.core.attention import AttentionService
from svbg.remnawave.api import RemnawaveApi
from svbg.remnawave.errors import ErrorKind, RemnawaveError
from svbg.remnawave.transport import Transport, TransportConfig
from tests.catalog.kit import SQ_DE, SQ_FI, SQ_NL, StubSource, add_location, add_plan, squad
from tests.dbkit import CountingDatabase
from tests.fakes.remnawave import FakeRemnawave


async def open_items(db: CountingDatabase) -> dict[str, dict[str, object]]:
    rows = await db.raw("select * from attention_items where resolved_at is null")
    return {r["dedup_key"]: dict(r) for r in rows}


async def test_sync_adds_updates_and_keeps_titles(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "old-name")
    await db.raw("""update locations set title = '{"ru": "Нидерланды"}'::jsonb, flag = '🇳🇱'""")
    source = StubSource([squad(SQ_NL, "NL-new", 0, 12), squad(SQ_DE, "DE", 1, 3)])
    result = await sync_locations(db, source)
    assert result.added == (SQ_DE,) and result.gone == () and result.total == 2 and result.changed
    snap = await catalog.reload()
    nl, de = snap.location(SQ_NL), snap.location(SQ_DE)
    assert nl is not None and nl.label() == "🇳🇱 Нидерланды" and nl.panel_name == "NL-new" and nl.members == 12
    assert de is not None and de.label() == "DE" and de.sort == 10 and de.present
    again = await sync_locations(db, source)
    assert not again.changed


async def test_gone_squad_breaks_plans_and_alerts_then_repairs(
    db: CountingDatabase, catalog: CatalogService
) -> None:
    attention = AttentionService(db)
    await add_location(db, SQ_NL, "NL")
    await add_location(db, SQ_DE, "DE")
    hit = await add_plan(db, "hit", squads=(SQ_NL, SQ_DE))
    safe = await add_plan(db, "safe", squads=(SQ_NL,))
    manual = await add_plan(db, "manual", squads=(SQ_DE,), broken_reason="ручная пометка")
    result = await sync_locations(db, StubSource([squad(SQ_NL, "NL")]), attention=attention)
    assert result.gone == (SQ_DE,) and result.broken == (hit,) and result.repaired == ()
    snap = await catalog.reload()
    assert snap.plan(hit).broken_reason == BROKEN_PREFIX + "DE"  # type: ignore[union-attr]
    assert snap.plan(safe).broken_reason is None  # type: ignore[union-attr]
    assert snap.plan(manual).broken_reason == "ручная пометка"  # type: ignore[union-attr]  # not ours
    assert not snap.location(SQ_DE).present  # type: ignore[union-attr]
    assert snap.purchasable(hit, _Paid(), currency="RUB") is None
    items = await open_items(db)
    assert set(items) == {attention_key(hit)}
    assert items[attention_key(hit)]["fix_action"] == f"screen:pl:{hit}"
    assert "Стандарт" in str(items[attention_key(hit)]["title"])
    # a second sync with the same state changes nothing and raises nothing new
    again = await sync_locations(db, StubSource([squad(SQ_NL, "NL")]), attention=attention)
    assert again.broken == () and again.gone == ()
    # the squad comes back
    back = await sync_locations(db, StubSource([squad(SQ_NL, "NL"), squad(SQ_DE, "DE")]), attention=attention)
    assert back.returned == (SQ_DE,) and back.repaired == (hit,)
    snap = await catalog.reload()
    assert snap.plan(hit).broken_reason is None and snap.location(SQ_DE).present  # type: ignore[union-attr]
    assert await open_items(db) == {}


async def test_unknown_squad_in_a_plan_counts_as_gone(db: CountingDatabase, catalog: CatalogService) -> None:
    pid = await add_plan(db, "std", squads=(SQ_FI,))
    result = await sync_locations(db, StubSource([squad(SQ_NL, "NL")]))
    assert result.broken == (pid,)
    assert BROKEN_PREFIX in ((await catalog.reload()).plan(pid).broken_reason or "")  # type: ignore[union-attr]


async def test_empty_panel_is_guarded(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "NL")
    pid = await add_plan(db, "std")
    result = await sync_locations(db, StubSource([]))
    assert result.guarded and result.gone == () and result.broken == ()
    snap = await catalog.reload()
    assert snap.location(SQ_NL).present and snap.plan(pid).broken_reason is None  # type: ignore[union-attr]


async def test_panel_errors_change_nothing(db: CountingDatabase, catalog: CatalogService) -> None:
    await add_location(db, SQ_NL, "NL")
    error = RemnawaveError(ErrorKind.TRANSIENT, None, "TIMEOUT", "нет ответа")
    with pytest.raises(RemnawaveError):
        await sync_locations(db, StubSource(error=error))

    class Slow(StubSource):
        async def internal_squads(self, *, lane: object = None) -> list:  # type: ignore[override]
            await asyncio.sleep(5)
            return []

    with pytest.raises(TimeoutError):
        await sync_locations(db, Slow(), timeout=0.05)
    assert (await catalog.reload()).location(SQ_NL).present  # type: ignore[union-attr]


class _Paid:
    has_paid = True


# ------------------------------------------------------------------------------------------- real client


@pytest.fixture
async def panel() -> AsyncIterator[FakeRemnawave]:
    async with FakeRemnawave() as fake:
        yield fake


async def test_sync_through_the_real_client(
    db: CountingDatabase, catalog: CatalogService, panel: FakeRemnawave
) -> None:
    nl = panel.add_internal_squad("NL")
    de = panel.add_internal_squad("DE")
    transport = Transport(TransportConfig(base_url=panel.url, token=panel.add_token()))
    try:
        result = await sync_locations(db, RemnawaveApi(transport))
    finally:
        await transport.aclose()
    assert set(result.added) == {nl, de}
    snap = await catalog.reload()
    assert [loc.panel_name for loc in snap.locations] == ["NL", "DE"]
    assert len(panel.calls("/internal-squads", "GET")) == 1
