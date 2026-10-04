"""The whole admin tree as the owner sees it (every screen a button opens), checked against the layout rules.

With ``SVBG_ADMIN_DUMP=1`` the walk is also written to ``admin-tree-dump.md`` (or ``SVBG_ADMIN_DUMP_PATH``)
for a human review.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from svbg.tg.admin import nav
from tests.e2e.admin_tree import crawl, problems, render_markdown
from tests.e2e.conftest import OWNER_ID, REPO, AppEnv, StartApp
from tests.e2e.test_stage2_kit import open_shop
from tests.e2e.test_stage3_kit import chat, tg  # noqa: F401

pytestmark = [pytest.mark.pg, pytest.mark.timeout(900)]

DUMP = Path(os.environ.get("SVBG_ADMIN_DUMP_PATH") or REPO / "admin-tree-dump.md")


async def test_admin_tree(start_app: StartApp, app_env: AppEnv) -> None:
    extra = {"LTE_ENABLED": "true", "IP_GUARD_ENABLED": "true", "PUBLIC_URL": "https://bot.example.com"}
    async with open_shop(start_app, app_env, extra_env=extra) as shop:
        client = chat(shop, 5_401)
        await client.start()
        owner = chat(shop, OWNER_ID)
        await owner.start()
        screens = await crawl(owner, shop.app.screens)
        if os.environ.get("SVBG_ADMIN_DUMP"):
            DUMP.write_text(render_markdown(screens, who="владелец"), encoding="utf-8", newline="\n")
        assert len(screens) > 30
        broken = problems(
            screens,
            sections=set(nav.TITLES) - {nav.ROOT, nav.ROOT_ALIAS},
            skip={"setup.wiz"},  # the first-run wizard (svbg.tg.setup) has its own way through
            foreign={"lte", "ipguard"},  # drawn by the modules themselves (svbg.ext)
            long_ok={"status.panel"},  # the commands for the panel's .env
        )
        if broken:
            pytest.fail("\n".join(broken), pytrace=False)
