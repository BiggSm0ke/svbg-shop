"""The whole application with every stage 3–4 module wired (fake Telegram, fake panel, fake cash desks).

* every module registers without errors (screens, routers, jobs, periodic tasks, admin topics, settings);
* a module that is switched off does not disturb the start and is switched on by a setting without a restart;
* a module that fails at start is reported (``missing_modules`` / module state ``failed``), the bot works.

The schema comes from the Alembic migration; :func:`create_module_tables` only fills in what a migration of
another build might lack (``CREATE … IF NOT EXISTS``). Dropping a module's tables is the "module failed at
start" case.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
import sqlalchemy as sa
from aiohttp import ClientSession

from svbg.app import OPTIONAL_UI_MODULES, App
from svbg.core.settings import Change
from svbg.ext.api import ModuleState
from svbg.payments.providers import BUILTIN_PROVIDERS
from tests.e2e.conftest import OWNER_ID, AppEnv, StartApp, apply_sql
from tests.e2e.test_stage2_kit import open_shop, until, until_async

pytestmark = pytest.mark.pg

#: Modules whose tables the stage 3–4 code declares (``sa.Table`` objects at module level).
MODULE_TABLES = (
    "svbg.promo.tables",
    "svbg.pages.tables",
    "svbg.ads.tables",
    "svbg.deeplinks.tables",
    "svbg.broadcasts.tables",
    "svbg.referral.tables",
    "svbg.ext.lte.tables",
    "svbg.ext.lte.service",
    "svbg.ext.ip_guard.tables",
    "svbg.importers",
)
MARKETING_DDL = "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_marketing boolean NOT NULL DEFAULT true"

#: What the app must wire with all tables present.
STAGE3_PARTS = (
    "svbg.deeplinks",
    "svbg.tg.admin.promo",
    "svbg.tg.admin.pages",
    "svbg.ads.admin",
    "svbg.referral.screens",
    "svbg.ext.ui",
    "svbg.ext.ip_guard.cards",
)


async def create_module_tables(dsn: str) -> None:
    """``CREATE TABLE IF NOT EXISTS`` for every module table (dependency order), plus the stage-3 column."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from svbg.db.engine import normalize_dsn

    found: dict[str, sa.Table] = {}
    for name in MODULE_TABLES:
        module = importlib.import_module(name)
        for value in vars(module).values():
            if isinstance(value, sa.Table):
                found.setdefault(value.name, value)
    ordered = sa.schema.sort_tables(list(found.values()))
    engine = create_async_engine(normalize_dsn(dsn)[0])
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(MARKETING_DDL)
            for table in ordered:
                await conn.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
    finally:
        await engine.dispose()


@pytest.fixture
async def module_env(app_env: AppEnv) -> AppEnv:
    await create_module_tables(app_env.dsn)
    return app_env


def _labels(markup: Any) -> list[str]:
    rows = (markup or {}).get("inline_keyboard") or []
    return [str(b.get("text")) for row in rows for b in row]


# ---------------------------------------------------------------------------------------------- tests


async def test_every_module_registers_with_fakes(start_app: StartApp, module_env: AppEnv) -> None:
    async with open_shop(start_app, module_env) as shop:
        app = shop.app
        problems = {k: v for k, v in app.missing_modules.items()}
        for name in OPTIONAL_UI_MODULES:
            assert name in app.wired_modules, (name, problems)
        for name in STAGE3_PARTS:
            assert name in app.wired_parts, (name, problems)
        assert not problems, problems

        # services and deps
        for attr in ("media", "public_media", "content_transfer", "promo", "pages", "ads", "referral"):
            assert getattr(app, attr) is not None, attr
        assert app.deeplinks is not None and app.ext is not None and app.shadow is not None
        deps = app.deps
        assert deps is not None
        assert deps.deeplinks is app.deeplinks and deps.extensions is app.ext and deps.media is app.media
        assert deps.content_export is not None and deps.register_job is not None
        # joints: /m/<token> links of the preview mode, resume after onboarding, the LTE order kind
        assert app.screens is not None and app.screens.media_url is not None
        assert app.user_path is not None and app.user_path.home.after_onboarding is not None
        assert app.billing is not None and "addon_lte" in app.billing.fulfiller._kinds

        # all 27 payment plugins are known to the catalog, one component per instance key set
        assert app.pay_instances is not None
        assert len(BUILTIN_PROVIDERS) == 27
        assert {cls.manifest.slug for cls in BUILTIN_PROVIDERS} <= set(app.payment_slugs())

        # jobs of every module are registered with the worker
        for kind in ("broadcast.run", "broadcast.cleanup", "lte.term", "ip_guard.card", "ip_guard.drop_ips"):
            assert kind in app.job_handlers, kind
        assert any(k.startswith("referral.") for k in app.job_handlers)
        assert "panel.hwid_reset" in app.job_handlers and "panel.hwid_delete" in app.job_handlers

        # periodic tasks of the modules
        assert app.scheduler is not None
        tasks = set(app.scheduler.tasks())
        for name in ("promo.sweep", "deeplinks.daily", "importers.shadow", "lte.cycle", "ip_guard.collect"):
            assert name in tasks, (name, sorted(tasks))

        # module settings are in the registry (and so in .env and in the bot's settings)
        for key in (
            "BACKUP_ENABLED",
            "REFERRAL_ENABLED",
            "LTE_ENABLED",
            "IP_GUARD_ENABLED",
            "IMPORT_SOURCE_DSN",
        ):
            assert key in app.registry, key

        # admin chat topics of the modules
        assert app.admin_chat is not None
        kinds = {d.kind for d in app.admin_chat.topic_defs()}
        assert {"partners", "lte", "antiabuse", "backups"} <= kinds, kinds

        # owner modules are off by default and do not run
        assert app.ext.state("lte") is ModuleState.DISABLED
        assert app.ext.state("ip_guard") is ModuleState.DISABLED

        # /m/<token>: the public media route answers (404 for an unknown token, no listing)
        assert app.web is not None
        async with ClientSession() as session, session.get(f"{app.web.url}/m/{'x' * 24}") as resp:
            assert resp.status == 404

        # the home screen: module buttons for the user, «🛠 Админка» for the owner → the admin hub
        user = shop.person(5005)
        await user.start()
        labels = [str(b.get("text")) for b in user.buttons()]
        assert any("Промокод" in x for x in labels), labels
        assert not any("Пригласить" in x for x in labels), "referral is off by default"
        assert not any("Админка" in x for x in labels)
        owner = shop.person(OWNER_ID)
        await owner.start()
        await owner.press("Админка", expect="Выберите раздел")
        hub_labels = [str(b.get("text")) for b in owner.buttons()]
        for label in ("Сводка", "Пользователи", "Промокоды", "Рассылки", "Конструктор", "Бэкапы"):
            assert any(label in x for x in hub_labels), (label, hub_labels)
        await owner.press("Промокоды")
        await owner.start()
        await owner.press("Админка", expect="Выберите раздел")
        await owner.press("Сводка")

        # promo by the user's button: the code form opens
        await user.start()
        await user.press("Промокод")


async def test_referral_switched_on_without_restart(start_app: StartApp, module_env: AppEnv) -> None:
    async with open_shop(start_app, module_env) as shop:
        app = shop.app
        assert app.settings is not None
        user = shop.person(5101)
        await user.start()
        assert not any("Пригласить" in str(b.get("text")) for b in user.buttons())
        await app.settings.apply([Change("REFERRAL_ENABLED", "true")], source="bot", actor_id=None)
        await user.start()
        await user.press("Пригласить", expect="Пригласите друзей")
        assert "start=r_" in user.text()


async def test_deep_links_on_start(start_app: StartApp, module_env: AppEnv) -> None:
    """An ad code that looks like a top-up (``t_tiktok``) reaches the deep-link service; /start works."""
    async with open_shop(start_app, module_env) as shop:
        app = shop.app
        assert app.ads is not None and app.db is not None
        async with app.db.tx() as conn:
            await conn.execute(
                sa.text(
                    "insert into ad_links (code, title, bonus, enabled, clicks, source)"
                    " values ('t_tiktok', 'TikTok', '{}'::jsonb, true, 0, 'bot')"
                )
            )
        await app.ads.load()
        user = shop.person(5202)
        await user.start("t_tiktok")
        assert "Привет" in user.text()
        rows = await shop.rows(
            "select l.code from ad_link_users u join ad_links l on l.id = u.ad_link_id"
            " join users x on x.id = u.user_id where x.telegram_id = 5202"
        )
        assert [r["code"] for r in rows] == ["t_tiktok"]
        hits = await shop.rows("select count(*) as n from deeplink_hits")
        assert hits[0]["n"] >= 1


async def test_owner_module_switched_on_by_setting(start_app: StartApp, module_env: AppEnv) -> None:
    async with open_shop(start_app, module_env) as shop:
        app = shop.app
        assert app.ext is not None and app.settings is not None
        await app.settings.apply([Change("IP_GUARD_ENABLED", "true")], source="bot", actor_id=None)
        await app.ext.drain()
        assert app.ext.state("ip_guard") in (ModuleState.ACTIVE, ModuleState.DEGRADED)
        health = await app.components.health("module:ip_guard")
        assert health is not None
        await app.settings.apply([Change("IP_GUARD_ENABLED", "false")], source="bot", actor_id=None)
        await app.ext.drain()
        assert app.ext.state("ip_guard") is ModuleState.DISABLED
        user = shop.person(5303)
        await user.start()
        assert "Привет" in user.text()


async def test_failing_module_is_reported_and_bot_works(
    start_app: StartApp, module_env: AppEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from svbg.ext.lte import service as lte_service
    from svbg.promo.service import PromoService

    async def broken_load(self: Any) -> int:
        raise RuntimeError("promo is broken")

    async def broken_setup(ctx: Any) -> None:
        raise RuntimeError("lte is broken")

    monkeypatch.setattr(PromoService, "load", broken_load)
    monkeypatch.setattr(lte_service, "setup", broken_setup)
    async with open_shop(start_app, module_env, extra_env={"LTE_ENABLED": "true"}) as shop:
        app = shop.app
        assert "svbg.promo" in app.missing_modules and app.promo is None
        assert app.ext is not None and app.ext.state("lte") is ModuleState.FAILED
        report = await app.components.health("module:lte")
        assert report is not None and report.status.value in ("down", "degraded")
        user = shop.person(5404)
        await user.start()
        assert "Привет" in user.text()
        labels = [str(b.get("text")) for b in user.buttons()]
        assert not any("Промокод" in x for x in labels), labels
        await user.press("Баланс")


async def test_start_without_module_tables_degrades(start_app: StartApp, app_env: AppEnv) -> None:
    """Without the tables of promo and pages: both are reported, the bot and sales work."""
    await apply_sql(
        app_env.dsn, "DROP TABLE IF EXISTS promo_pending, page_consents, page_versions, pages CASCADE"
    )
    async with open_shop(start_app, app_env) as shop:
        app: App = shop.app
        assert app.payments is not None and app.catalog is not None
        # promo (its tables are missing) is reported, the rest of the bot is up
        assert "svbg.promo" in app.missing_modules, app.missing_modules
        assert "svbg.pages" in app.missing_modules, app.missing_modules
        assert app.promo is None and app.pages is None
        assert app.ads is not None and app.deeplinks is not None  # the other modules are wired
        user = shop.person(5505)
        await user.start()
        assert "Привет" in user.text()
        await until(lambda: app.runner is not None, what="runner")
        await until_async(lambda: _health_ok(app), what="bot health")


async def _health_ok(app: App) -> bool:
    report = await app.components.health("bot")
    return report is not None and report.status.value == "ok"
