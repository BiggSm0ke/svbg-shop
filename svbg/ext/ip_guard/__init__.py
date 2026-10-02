"""IP Guard — the owner's anti-sharing module (05 §2.2), manifest only (heavy code is imported lazily).

Once a minute the collector reads which IPs each panel user is connected from (``connections/by-node`` of
every non-CDN node), keeps a 10-minute window in memory and the decider (owner thresholds) warns admins in
the topic «🛡 Антиабуз» or blocks: the subscription is frozen (core ``hold_kind='ip_guard'``), disabled in the
panel and its connections are dropped by IP on the nodes where they were seen. Only an admin's button
unblocks; the frozen time comes back. The automatic block is **off by default** (``IP_GUARD_AUTO_BLOCK``)
until a week of calibration: warnings, manual blocks and unblocks work right away.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Any

from svbg.ext.api import JobDef, ModuleSpec, Periodic, Perm, Slot, Topic, ViewLoader, lazy
from svbg.ext.ip_guard.config import K_ENABLED, SECTION, SETTINGS

if TYPE_CHECKING:
    from svbg.ext.api import ModuleContext

__all__ = ["SPEC", "ui"]


async def ui(router: Any, ctx: ModuleContext) -> None:
    """Admin screen and module actions on the screen router (called by the host at start)."""
    from svbg.ext.ip_guard.runtime import RUNTIME
    from svbg.ext.ip_guard.tg import install_screens

    RUNTIME.bind(ctx)
    if hasattr(router, "screen") and hasattr(router, "action"):
        install_screens(router, RUNTIME.service, ctx.dep("db"))


def _slot(name: str) -> Any:
    def call(c: Any) -> Any:
        from svbg.ext.ip_guard import runtime

        return getattr(runtime, name)(c)

    call.__qualname__ = f"ip_guard.{name}"
    return call


SPEC = ModuleSpec(
    name="ip_guard",
    title="IP Guard",
    enabled_key=K_ENABLED,
    settings=SETTINGS,
    section=SECTION,
    topics=(Topic("antiabuse", "Антиабуз", "🛡", priority="high", noun=("карточка", "карточки", "карточек")),),
    perms=(
        Perm("ip_guard.view", "IP Guard: просмотр", "Карточки и экран IP Guard"),
        Perm("ip_guard.block", "IP Guard: блокировать", "Блок вручную и решения по аномалии"),
        Perm("ip_guard.unblock", "IP Guard: снять блок", "Разблокировка и закрытие блока"),
        Perm("ip_guard.config", "IP Guard: настройка", "Белый список и CDN-ноды"),
    ),
    tasks=(
        Periodic("collect", lazy("svbg.ext.ip_guard.runtime:collect"), every_s=60, jitter_s=2, timeout_s=55),
        Periodic("purge", lazy("svbg.ext.ip_guard.runtime:purge"), daily_at=time(4, 20), optional=False),
    ),
    jobs=(
        JobDef(
            "ip_guard.card", lazy("svbg.ext.ip_guard.runtime:card_job"), when_disabled="run", timeout_s=60
        ),
        JobDef(
            "ip_guard.notify", lazy("svbg.ext.ip_guard.runtime:notify_job"), when_disabled="run", timeout_s=30
        ),
        JobDef(
            "ip_guard.drop_ips", lazy("svbg.ext.ip_guard.runtime:drop_job"), when_disabled="run", timeout_s=60
        ),
    ),
    slots=(
        Slot("subscription", "banner", _slot("render_banner"), order=10),
        Slot("home", "status_lines", _slot("render_status_line"), order=10),
        Slot("admin.user_card", "sections", _slot("render_admin_section")),
    ),
    views=(
        ViewLoader("subscription", lazy("svbg.ext.ip_guard.runtime:load_subscription")),
        ViewLoader("home", lazy("svbg.ext.ip_guard.runtime:load_subscription")),
        ViewLoader("admin.user_card", lazy("svbg.ext.ip_guard.runtime:load_admin_card")),
    ),
    setup=lazy("svbg.ext.ip_guard.runtime:setup"),
    teardown=lazy("svbg.ext.ip_guard.runtime:teardown"),
    health=lazy("svbg.ext.ip_guard.runtime:health"),
    status=lazy("svbg.ext.ip_guard.runtime:status"),
    report=lazy("svbg.ext.ip_guard.runtime:report"),
    ui=ui,
)
