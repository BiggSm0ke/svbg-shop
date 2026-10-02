"""Pages (06 §2.10), owner to-do lists, owner-module stage hooks, the shadow port, money edge cases."""

from __future__ import annotations

import sys
import types
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from svbg.content.model import parse_text_blocks
from svbg.core.crypto import Crypto, generate_key
from svbg.importers.bedolaga import BedolagaImporter, ImportConfig, StaticPanelReader
from svbg.importers.bedolaga.misc import html_to_entities
from svbg.importers.bedolaga.plan import importer_port
from svbg.importers.bedolaga.source import open_source
from svbg.pages.service import PageService
from tests.dbkit import CountingDatabase
from tests.importers.bedolaga.synth import T0, Scenario, Src, seed_edge_cases

pytestmark = pytest.mark.timeout(180)


def _importer(db: CountingDatabase, src_dsn: str, sc: Scenario) -> BedolagaImporter:
    cfg = ImportConfig(
        env=sc.env, t0=T0, crypto=Crypto([generate_key()]), overrides={"skip_panel_user_ids": [601]}
    )
    return BedolagaImporter(db, src_dsn, panel=StaticPanelReader(sc.panel), config=cfg)


@pytest.fixture
async def scenario(src_dsn: str) -> Scenario:
    src = await Src.connect(src_dsn)
    try:
        sc = await seed_edge_cases(src)
        await seed_pages_and_lists(src)
        return sc
    finally:
        await src.close()


async def seed_pages_and_lists(src: Src) -> None:
    d = timedelta(days=1)
    await src.many(
        "faq_pages",
        [
            {
                "id": 1,
                "language": "ru",
                "title": "Как подключиться?",
                "content": "Нажмите <b>Подключиться</b> и <a href='https://example.com/app'>скачайте</a>.",
                "display_order": 2,
                "is_active": True,
            },
            {
                "id": 2,
                "language": "ru",
                "title": "Сколько устройств?",
                "content": "До <i>5</i> устройств 🚀<br>Можно докупить.",
                "display_order": 1,
                "is_active": True,
            },
            {
                "id": 3,
                "language": "ru",
                "title": "Старое",
                "content": "x",
                "display_order": 0,
                "is_active": False,
            },
            {
                "id": 4,
                "language": "en",
                "title": "How?",
                "content": "Tap <b>Connect</b>",
                "display_order": 1,
                "is_active": True,
            },
        ],
    )
    await src.add("faq_settings", id=1, language="ru", is_enabled=True)
    await src.many(
        "service_rules",
        [
            {
                "id": 1,
                "order": 1,
                "title": "Правила",
                "content": "Не делитесь <u>ссылкой</u>.",
                "is_active": True,
                "language": "ru",
            },
        ],
    )
    await src.add("public_offers", id=1, language="ru", content="<p>Оферта</p>" + "x" * 5000, is_enabled=True)
    await src.add(
        "privacy_policies", id=1, language="ru", content="  <b>Политика</b> &amp; данные  ", is_enabled=True
    )
    await src.many(
        "guest_purchases",
        [
            {
                "id": 1,
                "token": "t1",
                "contact_type": "telegram",
                "contact_value": "@x",
                "period_days": 30,
                "amount_kopeks": 17900,
                "status": "paid",
                "paid_at": T0 - d,
            },
            {
                "id": 2,
                "token": "t2",
                "contact_type": "telegram",
                "contact_value": "@y",
                "period_days": 30,
                "amount_kopeks": 17900,
                "status": "delivered",
                "paid_at": T0 - d,
                "delivered_at": T0 - d,
            },
        ],
    )
    await src.many(
        "discount_offers",
        [
            {
                "id": 1,
                "user_id": 1,
                "notification_type": "expired",
                "discount_percent": 20,
                "bonus_amount_kopeks": 0,
                "expires_at": T0 + d,
                "is_active": True,
                "effect_type": "percent_discount",
            },
            {
                "id": 2,
                "user_id": 2,
                "notification_type": "expired",
                "discount_percent": 20,
                "bonus_amount_kopeks": 0,
                "expires_at": T0 - d,
                "is_active": True,
                "effect_type": "percent_discount",
            },
        ],
    )
    await src.add("admin_roles", id=1, name="Support", level=1)
    await src.many(
        "user_roles",
        [
            {"id": 1, "user_id": 1, "role_id": 1, "is_active": True},
            {"id": 2, "user_id": 2, "role_id": 1, "is_active": False},
        ],
    )


# ----------------------------------------------------------------------------------------- HTML → entities


def test_html_to_entities_offsets_are_utf16_and_nested() -> None:
    text, ents = html_to_entities("🚀 <b>Жирный <i>курсив</i></b> &amp; <a href='https://x.io'>ссылка</a>")
    assert text == "🚀 Жирный курсив & ссылка"
    by = {e["type"]: e for e in ents}
    assert by["bold"] == {"type": "bold", "offset": 3, "length": 13}  # 🚀 is 2 UTF-16 units
    assert by["italic"] == {"type": "italic", "offset": 10, "length": 6}
    assert by["text_link"] == {"type": "text_link", "url": "https://x.io", "offset": 19, "length": 6}
    parse_text_blocks({"ru": {"text": text, "entities": ents}})


def test_html_to_entities_pre_code_spoiler_quote_and_junk() -> None:
    html = (
        "\n  <pre><code class='language-bash'>ls -la</code></pre> <code>x</code>"
        "<tg-spoiler>s</tg-spoiler><span class='tg-spoiler'>t</span>"
        "<blockquote expandable>q</blockquote><a href='javascript:alert(1)'>bad</a><font>f</font>"
        "<b>unclosed"
    )
    text, ents = html_to_entities(html)
    assert text == "ls -la xstqbadfunclosed"
    kinds = [(e["type"], e["offset"], e["length"]) for e in ents]
    assert ("pre", 0, 6) in kinds and ents[0].get("language") == "bash"
    assert ("code", 7, 1) in kinds and ("spoiler", 8, 1) in kinds and ("spoiler", 9, 1) in kinds
    assert ("expandable_blockquote", 10, 1) in kinds and ("bold", 15, 8) in kinds
    assert not [e for e in ents if e["type"] == "text_link"], "a javascript: link keeps only its text"
    parse_text_blocks({"ru": {"text": text, "entities": ents}})


# --------------------------------------------------------------------------------------------- pages


async def test_pages_imported_and_owner_edits_respected(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await PageService(target).load()  # the bot seeded its placeholders before the import
    report = await _importer(target, src_dsn, scenario).run("apply")
    pages = {r["code"]: dict(r) for r in await target.raw("select * from pages")}
    faq = pages["faq"]
    assert faq["enabled"] and faq["version"] == 2
    ru = faq["body"]["ru"]
    assert ru["text"].startswith(
        "Сколько устройств?\nДо 5 устройств 🚀\nМожно докупить.\n\nКак подключиться?"
    )
    assert "Старое" not in ru["text"], "inactive questions are not carried over"
    assert {e["type"] for e in ru["entities"]} == {"bold", "italic", "text_link"}
    assert faq["body"]["en"]["text"] == "How?\nTap Connect"
    assert pages["rules"]["body"]["ru"]["text"] == "Правила\nНе делитесь ссылкой."
    assert (
        pages["privacy"]["kind"] == "custom" and pages["privacy"]["body"]["ru"]["text"] == "Политика & данные"
    )
    assert pages["offer"]["version"] == 1, "a text longer than one message is reported, not cut"
    assert report.issue_totals["page_too_long"] == 1
    svc = PageService(target)
    assert await svc.load() >= 5  # every imported page loads (entities valid)
    assert svc.get("faq").block_for("ru").entities  # type: ignore[union-attr]

    # The owner edits the FAQ in the bot; the source changes too — the import leaves the owner's text alone.
    await svc.save_text("faq", "ru", "Мой текст", None, (None, None))
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute("update service_rules set content = 'Новые правила' where id = 1")
        await src.conn.execute("update faq_pages set content = 'Изменено' where id = 1")
    finally:
        await src.close()
    again = await _importer(target, src_dsn, scenario).run("apply")
    pages = {r["code"]: dict(r) for r in await target.raw("select * from pages")}
    assert pages["faq"]["body"]["ru"]["text"] == "Мой текст"
    assert again.issue_totals["page_changed_locally"] == 1
    assert pages["rules"]["body"]["ru"]["text"] == "Правила\nНовые правила" and pages["rules"]["version"] == 3
    versions = await target.raw(
        "select v.version from page_versions v join pages p on p.id = v.page_id where p.code = 'rules'"
    )
    assert sorted(r["version"] for r in versions) == [1, 2, 3]


async def test_owner_lists_are_reported(target: CountingDatabase, src_dsn: str, scenario: Scenario) -> None:
    report = await _importer(target, src_dsn, scenario).run("dry_run")
    assert report.issue_totals["guest_purchase_undelivered"] == 1
    assert report.issues["guest_purchase_undelivered"][0]["guest_purchase_id"] == 1
    assert "guest_purchase_undelivered" in report.blocking, "settled by hand before T0 (06 §4.1 п.9)"
    assert [i["offer_id"] for i in report.issues["discount_offer_open"]] == [1]
    assert report.issues["role_to_assign"] == [{"user_id": 1, "role": "Support"}]
    assert report.skipped == {"panel_users": [601], "promocodes": [5], "promocode_uses": [4]}
    assert report.issue_totals.get("traffic_differs") is None


# ------------------------------------------------------------------------------------------ module hooks


async def test_owner_module_stages_run_in_place(
    target: CountingDatabase, src_dsn: str, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []

    def fake(name: str, *, with_check: bool) -> types.ModuleType:
        mod = types.ModuleType(f"svbg.importers.bedolaga.{name}")

        async def run(ctx: Any) -> None:
            ledger = await ctx.conn.scalar(sa.text("select count(*) from wallet_ledger"))
            calls.append((name, (len(ctx.subs), int(ledger))))
            ctx.report.inc(name, "ran")

        mod.run = run  # type: ignore[attr-defined]
        if with_check:

            async def check(ctx: Any) -> None:
                calls.append((f"{name}.check", None))

            mod.check = check  # type: ignore[attr-defined]
        return mod

    modules = "svbg.importers.bedolaga."
    monkeypatch.setitem(sys.modules, modules + "lte", fake("lte", with_check=True))
    monkeypatch.setitem(sys.modules, modules + "referral_days", fake("referral_days", with_check=False))
    monkeypatch.setitem(sys.modules, modules + "ip_guard", None)  # «not installed»
    report = await _importer(target, src_dsn, scenario).run("dry_run")
    names = [n for n, _ in calls]
    assert names == ["lte", "referral_days", "lte.check"]
    lte_info = dict(calls)["lte"]
    assert lte_info[0] == 10 and lte_info[1] == 0, "after subscriptions, before the wallet stage"
    assert report.counts["modules"] == {"lte": 1, "ip_guard": 0, "referral_days": 1}
    assert report.counts["lte"] == {"ran": 1}


# -------------------------------------------------------------------------------------------- shadow port


async def test_shadow_port_returns_the_report(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    port = importer_port(
        target,
        panel=lambda: StaticPanelReader(scenario.panel),
        config=lambda: ImportConfig(
            env=scenario.env,
            t0=T0,
            crypto=Crypto([generate_key()]),
            overrides={"skip_subscription_ids": [112]},
        ),
    )
    result = await port(mode="shadow", source_dsn=src_dsn)
    assert result["mode"] == "shadow" and result["finished"] is True
    assert result["skipped"]["subscriptions"] == [112]
    run = await target.raw("select mode, status from import_runs")
    assert [(r["mode"], r["status"]) for r in run] == [("shadow", "done")]


# ------------------------------------------------------------------------------------------- money edges


async def test_invoice_paid_by_both_bots_blocks_the_cutover(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    await _importer(target, src_dsn, scenario).run("shadow")
    # The stand's reconciler credited the live invoice RP-2 and Bedolaga marked it paid as well.
    await target.raw(
        "update payments set status = 'paid', paid_amount_minor = amount_minor, paid_currency = currency, "
        "paid_at = now() where external_id = 'RP-2'"
    )
    src = await Src.connect(src_dsn)
    try:
        await src.conn.execute("update rollypay_payments set status = 'paid', is_paid = true where id = 2")
    finally:
        await src.close()
    report = await _importer(target, src_dsn, scenario).run("shadow")
    assert report.issue_totals["payment_paid_twice"] == 1
    assert "payment_paid_twice" in report.blocking and not report.green


async def test_stars_rule_matches_the_reconciliation(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    src = await Src.connect(src_dsn)
    try:
        await src.many(
            "transactions",
            [
                {
                    "id": 50,
                    "user_id": 2,
                    "type": "subscription_payment",
                    "amount_kopeks": 9900,
                    "payment_method": "TELEGRAM_STARS",
                    "external_id": "stars-2",
                    "is_completed": True,
                },
                {
                    "id": 51,
                    "user_id": 2,
                    "type": "deposit",
                    "amount_kopeks": 9900,
                    "payment_method": "telegram_stars",
                    "external_id": "stars-3",
                    "is_completed": False,
                },
            ],
        )
    finally:
        await src.close()
    report = await _importer(target, src_dsn, scenario).run("apply")
    stars = {
        r["external_id"]: r
        for r in await target.raw(
            "select p.* from payments p join payment_instances i on i.id = p.instance_id "
            "where i.slug = 'stars'"
        )
    }
    assert set(stars) == {"stars-charge-1", "stars-2"}
    assert stars["stars-2"]["amount_minor"] == 99 and stars["stars-2"]["currency"] == "XTR"
    assert report.checks["C1"]["payments"]["expected"] == report.checks["C1"]["payments"]["present"]


async def test_every_requested_column_exists_in_the_owner_schema(
    target: CountingDatabase, src_dsn: str, scenario: Scenario
) -> None:
    """A typo in a column list would silently read NULL (tolerance to other schema versions): against the
    owner's real schema no requested column may be missing."""
    seen: list[str] = []

    def spy() -> Any:
        cm = open_source(src_dsn)

        class Wrap:
            async def __aenter__(self) -> Any:
                self.src = await cm.__aenter__()
                return self.src

            async def __aexit__(self, *exc: Any) -> Any:
                seen.extend(self.src.statements)
                return await cm.__aexit__(*exc)

        return Wrap()

    cfg = ImportConfig(env=scenario.env, t0=T0, crypto=Crypto([generate_key()]))
    await BedolagaImporter(target, spy, panel=StaticPanelReader(scenario.panel), config=cfg).run("dry_run")
    assert seen
    assert not [sql for sql in seen if "NULL AS" in sql]
