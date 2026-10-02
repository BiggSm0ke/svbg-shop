"""How the app plugs the referral core in (kept here so the integration step is a few lines in ``app.py``).

* :data:`PARTNERS_TOPIC` — the «🤝 Партнёры» topic of the admin chat (X11), register it with
  ``admin_chat.register_topic``;
* :func:`build` — the service with the app's settings, bus, admin chat and notifier; then
  ``job_handlers.update(svc.handlers())``, ``svc.install(bus)``, ``svc.schedule(scheduler)``, and the
  deep-link service gets ``referral=svc`` (``attach_referrer(user_id, code)``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from svbg.referral.service import K_PARTNERS, Poster, ReferralService, UserSender
from svbg.services.admin_chat import TopicDef
from svbg.tg.notifier import Priority

if TYPE_CHECKING:
    from svbg.core.bus import EventBus
    from svbg.db.engine import Database
    from svbg.jobs.scheduler import Scheduler
    from svbg.jobs.worker import Handler
    from svbg.referral.texts import Overrides

__all__ = ["PARTNERS_TOPIC", "build"]

PARTNERS_TOPIC: Final = TopicDef(
    K_PARTNERS,
    "Партнёры",
    "🤝",
    Priority.NORMAL,
    ("🤝", "👥", "🎁"),
    9367192,  # green, like the other money-ish topics
    ("награда", "награды", "наград"),
)


def build(
    db: Database,
    *,
    config: Callable[[], Mapping[str, Any]],
    bus: EventBus | None,
    scheduler: Scheduler | None,
    job_handlers: dict[str, Handler],
    poster: Poster | None = None,
    sender: UserSender | None = None,
    overrides: Overrides | None = None,
    bot_username: Callable[[], str | None] = lambda: None,
    timezone: Callable[[], str] = lambda: "Europe/Moscow",
) -> ReferralService:
    """Create the service and register its jobs, bus subscriptions and the hourly sweep."""
    svc = ReferralService(
        db,
        config=config,
        bus=bus,
        poster=poster,
        sender=sender,
        overrides=overrides,
        bot_username=bot_username,
        timezone=timezone,
    )
    job_handlers.update(svc.handlers())
    if bus is not None:
        svc.install(bus)
    if scheduler is not None:
        svc.schedule(scheduler)
    return svc
