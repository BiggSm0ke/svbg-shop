"""IP Guard texts (Russian, Telegram HTML) and pure card renderers (05 §2.2.2).

Cards are built **from database rows** (never from ``callback.message``): a restart or a second admin sees the
same card. They are :class:`~svbg.tg.report.Report` objects (key-value lines, tables, folded top IPs): a rich
message where the admin chat takes them, Telegram HTML otherwise. The report escapes its values itself; HTML
written elsewhere (:func:`who`) goes in through :func:`~svbg.tg.report.from_html`.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from svbg.tg.report import Inline, Report, code, from_html

__all__ = [
    "CLOSE_REASONS",
    "KIND_TITLES",
    "MSK",
    "USER_KEYS",
    "T",
    "anomaly_card",
    "block_card",
    "digest_card",
    "esc",
    "fmt_dt",
    "fmt_duration",
    "ips_file",
    "unblock_user_text",
    "user_t",
    "warning_card",
    "who",
]

MSK: Final = ZoneInfo("Europe/Moscow")
DASH: Final = "—"
MAX_TOP_LINES: Final = 15

#: «🗑 Закрыть блок» zeroes the paid term: the admin picks one of these (``admin_audit.reason``). Buttons, not
#: free text: the card lives in the admin group, where a text form would catch other people's messages.
CLOSE_REASONS: Final[tuple[str, ...]] = (
    "Перепродажа доступа",
    "Повторное нарушение",
    "Ссылка раздаётся многим",
)

T: Final[Mapping[str, str]] = {
    # user
    "user_blocked": (
        "🚫 <b>Подписка заблокирована</b>\n\n"
        "Ссылкой пользуются с {n} разных IP. Похоже, её передали другим людям.\n"
        "Оставшиеся дни заморожены и вернутся после разблокировки.\n\n"
        "Напишите в поддержку, разберёмся."
    ),
    "user_unblocked": (
        "✅ <b>Доступ восстановлен</b>\nЗамороженное время вернулось: {left}.\nАктивна до {until}."
    ),
    "user_unblocked_revoked": "\n\nСсылка обновлена. Откройте «📱 Моя подписка» и подключитесь заново.",
    "user_unblocked_expired": (
        "✅ <b>Доступ восстановлен</b>\nСрок подписки закончился. Продлите её, чтобы подключиться."
    ),
    "btn_support": "💬 Поддержка",
    "btn_close": "Закрыть",
    "btn_my_sub": "📱 Моя подписка",
    "btn_renew": "💳 Продлить",
    "banner": "🚫 <b>Подписка заблокирована</b>\nДни заморожены, осталось {left}.\nНапишите в поддержку.",
    "status_line": "🚫 Подписка заблокирована. Напишите в поддержку.",
    # admin card pieces
    "reason_auto": "авто",
    "reason_manual": "вручную",
    "reason_anomaly": "из аномалии",
    "block_title": "Подписка заблокирована",
    "block_reason": "блок: {reason}",
    "k_who": "Кто",
    "k_sub": "Подписка",
    "k_ips": "IP за {window} мин",
    "k_subnets": "Подсетей",
    "k_live": "Живых IP",
    "k_frozen": "❄️ Заморожено",
    "thresholds": "Пороги: предупреждение {warn}, блок {block}, подсетей от {subnets}, живых от {live}",
    "actions": "🛡 Действия",
    "k_bot": "Бот",
    "k_panel": "Панель",
    "k_drop": "Соединения",
    "k_notify": "Пользователь",
    "act_bot": "✅ подписка заморожена",
    "act_panel_ok": "✅ отключена",
    "act_panel_wait": "⏳ отключаю (повторяю, пока не выйдет)",
    "act_drop_ok": "✅ разорваны на {n} нодах",
    "act_drop_wait": "⏳ разрываю",
    "act_drop_failed": "⚠️ не разорваны (нет прав connections:drop)",
    "act_drop_none": "нечего разрывать",
    "act_residual": "⚠️ Остались соединения: UDP/QUIC, нет CAP_NET_ADMIN у ноды или долгий mux",
    "notify_sent": "✅ уведомлён",
    "notify_unreachable": "⚠️ бот заблокирован или нет диалога",
    "notify_failed": "⚠️ не удалось отправить",
    "notify_off": "уведомления выключены",
    "notify_wait": "⏳",
    "unblocked": "🔓 Разблокирована",
    "unblocked_revoke": " + новая ссылка",
    "outcome_active": "возвращено {left}, активна до {until}",
    "outcome_expired": "срок закончился, пользователь продлит сам",
    "outcome_zeroed": "подписка была обнулена",
    "outcome_gone": "подписки больше нет",
    "closed": "🗑 Блок закрыт",
    "closed_what": "подписка обнулена, аккаунт отключён",
    "k_by": "Кто решил",
    "k_when": "Когда",
    "k_outcome": "Итог",
    "confirmed": "✅ Проверено",
    "bypass": (
        "🚨 Включили в обход кнопки, поэтому отключил снова. Снимайте блок кнопкой, иначе дни не вернутся."
    ),
    "warn_title": "Подозрение на слив",
    "nb_title": "Не блокирую",
    "failed_title": "Автоблок НЕ выполнен",
    "k_why": "Почему",
    "acked": "✅ Проверено",
    "top_title": "Самые активные IP",
    "top_more": "…и ещё {n}",
    "anomaly_title": "Автоблоки остановлены",
    "k_trigger": "Причина",
    "anomaly_until": "Карантин до {at} МСК. Пока он идёт, никого не блокирую.",
    "anomaly_over": "Карантин закончился.",
    "anomaly_members": "Участники: {n}",
    "col_who": "Кто",
    "col_ip": "IP",
    "col_state": "Статус",
    "col_sub": "Подписка",
    "col_why": "Почему",
    "member_pending": "ждёт решения",
    "member_blocked": "🚫 заблокирован",
    "member_dismissed": "✅ ложная тревога",
    "member_skipped": "пропущен",
    "digest_title": "Ещё {n} предупреждений за проход",
    # buttons
    "b_unblock": "🔓 Разблокировать",
    "b_unblock_plain": "Разблокировать",
    "b_unblock_revoke": "+ новая ссылка",
    "b_cancel": "Отмена",
    "b_confirmed": "✅ Верно — открепить",
    "b_close": "🗑 Закрыть блок",
    "b_close_yes": "Обнулить: {reason}",
    "close_pick": "Выберите причину. Срок подписки обнулится",
    "b_ack": "✅ Проверено — открепить",
    "b_block": "🚫 Заблокировать",
    "b_block_yes": "Да, заблокировать",
    "b_ips": "📄 Все IP",
    "b_anomaly_block": "🚫 Заблокировать перечисленных ({n})",
    "b_anomaly_block_yes": "Да, заблокировать {n}",
    "b_anomaly_dismiss": "✅ Ложная тревога",
    "b_digest_member": "⚠️ №{sid} · {w} IP",
    # file
    "file_full": "Все IP подписки №{sid} на {at} МСК (окно {window} мин).",
    "file_partial": (
        "Полный список недоступен (окно сброшено); показаны {n} самых активных IP на момент карточки."
    ),
}

#: The keys of :data:`T` the **user** sees (the rest is the admin card, Russian only).
USER_KEYS: Final[tuple[str, ...]] = (
    "user_blocked",
    "user_unblocked",
    "user_unblocked_revoked",
    "user_unblocked_expired",
    "btn_support",
    "btn_close",
    "btn_my_sub",
    "btn_renew",
    "banner",
    "status_line",
)


def user_t(key: str, _lang: str | None = None) -> str:
    """A user-facing string of :data:`USER_KEYS` (the second argument, an old language, is ignored)."""
    return T[key]


KIND_TITLES: Final[Mapping[str, str]] = {
    "pool": "мало подсетей — похоже на NAT оператора",
    "unconfirmed": "мало живых IP, блок через {left} проходов",
    "incomplete": "нода не ответила ({nodes})",
    "grace": "недавно разблокирован (до {until})",
    "whitelist": "подписка в белом списке",
    "dismissed": "отмечено «ложная тревога» (до {until})",
    "precondition": "{why}",
    "warn": "предупреждение",
    "block_failed": "автоблок не выполнен",
}

TRIGGERS: Final[Mapping[str, str]] = {
    "per_run": "{n} подписок одновременно на уровне блока (лимит {max_run})",
    "window": "{n} подтверждений за {window} мин (лимит {max_run})",
    "per_hour": "{hour} блоков за час + {n} кандидатов (лимит {max_hour})",
}


def esc(value: Any, limit: int = 64) -> str:
    if value is None or value == "":
        return DASH
    text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text, quote=False)


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return DASH
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def fmt_time(value: datetime | None) -> str:
    return DASH if value is None else value.astimezone(MSK).strftime("%H:%M")


def fmt_duration(seconds: float | None, _lang: str | None = None) -> str:
    """``N дн. M ч``; under an hour — ``M мин``."""
    d, h, m = "дн.", "ч", "мин"
    total = max(0, int(seconds or 0))
    if total < 3600:
        return f"{total // 60} {m}"
    days, rest = divmod(total, 86400)
    hours = rest // 3600
    parts = [f"{days} {d}"] if days else []
    if hours or not days:
        parts.append(f"{hours} {h}")
    return " ".join(parts)


def who(row: Mapping[str, Any]) -> str:
    """``Имя (@user, id 123)`` from a row with ``first_name``, ``username``, ``telegram_id``."""
    name = esc((row.get("first_name") or "").strip() or None)
    if name == DASH:
        name = "без имени"
    handle = f"@{esc(row.get('username'))}, " if row.get("username") else ""
    tg = row.get("telegram_id")
    ident = f"id <code>{int(tg)}</code>" if isinstance(tg, int) else "нет Telegram"
    return f"{name} ({handle}{ident})"


def _cut(value: Any, limit: int = 64) -> str:
    """Like :func:`esc` without the escaping (a report escapes its values itself)."""
    if value is None or value == "":
        return DASH
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _top_lines(evidence: Mapping[str, Any], names: Mapping[str, str]) -> list[Inline]:
    top = evidence.get("top") if isinstance(evidence, Mapping) else None
    if not isinstance(top, list) or not top:
        return []
    lines: list[Inline] = []
    for item in top[:MAX_TOP_LINES]:
        if not isinstance(item, Mapping):
            continue
        nodes = ", ".join(_cut(names.get(str(n), str(n)[:8]), 24) for n in item.get("nodes") or [])
        lines.append([code(_cut(item.get("key"), 64)), f" — {nodes or DASH}"])
    rest = int(evidence.get("total") or len(top)) - min(len(top), MAX_TOP_LINES)
    if rest > 0:
        lines.append(T["top_more"].format(n=rest))
    return lines


def _metrics(rep: Report, m: Mapping[str, Any], window: int) -> Report:
    return (
        rep.line(T["k_ips"].format(window=window), str(int(m.get("ip_count") or 0)))
        .line(T["k_subnets"], str(int(m.get("subnet_count") or 0)))
        .line(T["k_live"], str(int(m.get("live_ip_count") or 0)))
    )


def _by(name: str | None) -> str:
    return _cut(name) if name else "система"


def block_card(
    block: Mapping[str, Any],
    *,
    person: str,
    window: int,
    frozen_left: int,
    panel_disabled: bool,
    by_names: Mapping[str, str | None],
    node_names: Mapping[str, str] = {},
) -> Report:
    """The block card: header, numbers, the live «🛡 Действия» block, the outcome and the top IPs.

    ``person``: Telegram HTML (:func:`who`)."""
    reason = T.get(f"reason_{block['reason']}", str(block["reason"]))
    rep = Report("🚨", T["block_title"], subtitle=T["block_reason"].format(reason=reason))
    rep.line(T["k_who"], from_html(person)).line(T["k_sub"], f"№{int(block['subscription_id'])}")
    _metrics(rep, block, window)
    status = block["status"]
    events = [e for e in block.get("events") or [] if isinstance(e, Mapping)]
    kinds = [str(e.get("kind")) for e in events]
    if status == "active":
        rep.line(T["k_frozen"], fmt_duration(frozen_left))
        rep.section(T["actions"]).line(T["k_bot"], T["act_bot"])
        rep.line(T["k_panel"], T["act_panel_ok"] if panel_disabled else T["act_panel_wait"])
        drop = next(
            (e for e in reversed(events) if e.get("kind") in ("dropped", "drop_failed", "drop_none")), None
        )
        if drop is None:
            dropped = T["act_drop_wait"]
        elif drop.get("kind") == "dropped":
            dropped = T["act_drop_ok"].format(n=int(drop.get("nodes") or 0))
        elif drop.get("kind") == "drop_none":
            dropped = T["act_drop_none"]
        else:
            dropped = T["act_drop_failed"]
        rep.line(T["k_drop"], dropped)
        notify = next((e for e in reversed(events) if e.get("kind") == "notify"), None)
        state = T.get(f"notify_{notify.get('state')}", T["notify_wait"]) if notify else T["notify_wait"]
        rep.line(T["k_notify"], state)
        rep.bullets(
            [
                T["act_residual"] if "residual" in kinds else "",
                T["bypass"] if "bypass" in kinds else "",
            ]
        )
        if block.get("confirmed_at") is not None:
            rep.line(T["confirmed"], _by(by_names.get("confirmed")))
    elif status == "unblocked":
        outcome = str(block.get("outcome") or "active")
        if outcome == "active":
            text = T["outcome_active"].format(
                left=fmt_duration(int(block.get("frozen_seconds") or 0)),
                until=fmt_dt(block.get("new_paid_until")),
            )
        else:
            text = T.get(f"outcome_{outcome}", outcome)
        if block.get("unblock_mode") == "revoke":
            text += T["unblocked_revoke"]
        rep.section(T["unblocked"])
        rep.line(T["k_by"], _by(by_names.get("unblocked")))
        rep.line(T["k_when"], fmt_dt(block.get("unblocked_at")))
        rep.line(T["k_outcome"], text)
    else:
        rep.section(T["closed"])
        rep.line(T["k_by"], _by(by_names.get("closed"))).line(T["k_when"], fmt_dt(block.get("closed_at")))
        rep.line(T["k_outcome"], T["closed_what"])
    return rep.details(T["top_title"], _top_lines(block.get("evidence") or {}, node_names))


def _kind_why(alert: Mapping[str, Any]) -> str:
    reason = str(alert.get("reason") or "")
    m = alert.get("metrics") or {}
    template = KIND_TITLES.get(reason, reason or DASH)
    return template.format(
        left=int(m.get("sustained_left") or 1),
        nodes=", ".join(_cut(n, 24) for n in m.get("missing_nodes") or []) or DASH,
        until=fmt_time(_parse(m.get("until"))),
        why=_cut(m.get("precondition") or "автоблок выключен", 120),
    )


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def warning_card(
    alert: Mapping[str, Any],
    *,
    person: str,
    window: int,
    thresholds: Mapping[str, int],
    acked_by: str | None,
    node_names: Mapping[str, str] = {},
) -> Report:
    """A warning, a «не блокирую» note or a failed autoblock. ``person``: Telegram HTML or plain text."""
    kind = alert["kind"]
    m = alert.get("metrics") or {}
    if kind == "warn":
        rep = Report("⚠️", T["warn_title"])
    elif kind == "block_failed":
        rep = Report("🚨", T["failed_title"]).line(T["k_why"], _cut(m.get("fail_reason") or "ошибка", 160))
    else:
        rep = Report("ℹ️", T["nb_title"]).line(T["k_why"], _kind_why(alert))
    sub = alert.get("subscription_id")
    rep.line(T["k_who"], from_html(person))
    if sub is not None:
        rep.line(T["k_sub"], f"№{int(sub)}")
    _metrics(rep, m, window)
    rep.note(T["thresholds"].format(**thresholds))
    if alert.get("acked_at") is not None:
        rep.line(T["acked"], _by(acked_by))
    return rep.details(T["top_title"], _top_lines(alert.get("evidence") or {}, node_names))


def anomaly_card(
    alert: Mapping[str, Any],
    *,
    now: datetime,
    people: Mapping[str, str],
    params: Mapping[str, int],
) -> Report:
    """The quarantine card: why, until when, and the members as a table. ``people``: Telegram HTML."""
    m = alert.get("metrics") or {}
    trigger = str(m.get("trigger") or "per_run")
    why = TRIGGERS.get(trigger, trigger).format(
        n=int(m.get("trigger_count") or 0),
        max_run=params.get("max_run", 3),
        window=params.get("window", 10),
        hour=int(m.get("blocks_last_hour") or 0),
        max_hour=params.get("max_hour", 10),
    )
    until = alert.get("quarantine_until")
    rep = Report("🛑", T["anomaly_title"]).line(T["k_trigger"], why)
    rep.text(T["anomaly_until"].format(at=fmt_time(until)) if until and until > now else T["anomaly_over"])
    members = alert.get("members") or {}
    rows: list[list[Inline]] = []
    for pid, info in sorted(members.items(), key=lambda kv: -int((kv[1] or {}).get("ip") or 0)):
        state = T.get(f"member_{(info or {}).get('state')}", T["member_pending"])
        person = people.get(str(pid))
        rows.append(
            [
                from_html(person) if person else f"панель {_cut(pid)}",
                str(int((info or {}).get("ip") or 0)),
                state,
            ]
        )
    rep.section(T["anomaly_members"].format(n=len(members)))
    return rep.table([T["col_who"], T["col_ip"], T["col_state"]], rows, "lrl", max_rows=30)


def digest_card(alert: Mapping[str, Any]) -> Report:
    """Warnings of one pass that did not get a card of their own, as a table."""
    members = alert.get("members") or {}
    rows: list[list[Inline]] = []
    for _pid, raw in list(members.items()):
        info = raw or {}
        kind = KIND_TITLES.get(str(info.get("kind")), str(info.get("kind")))
        if "{" in kind:
            kind = str(info.get("kind"))
        rows.append([f"№{int(info.get('sub') or 0)}", str(int(info.get("ip") or 0)), _cut(kind, 80)])
    rep = Report("🧾", T["digest_title"].format(n=len(members)))
    return rep.table([T["col_sub"], T["col_ip"], T["col_why"]], rows, "lrl", max_rows=40, shrink=2)


def unblock_user_text(
    outcome: str, *, left: int, until: datetime | None, revoked: bool, lang: str | None = None
) -> str:
    if outcome != "active":
        return user_t("user_unblocked_expired")
    text = user_t("user_unblocked").format(left=fmt_duration(left), until=fmt_dt(until))
    return text + (user_t("user_unblocked_revoked") if revoked else "")


def ips_file(
    *,
    sid: int,
    at: datetime,
    window: int,
    full: Sequence[tuple[str, Sequence[str], Sequence[str], datetime]] | None,
    evidence: Mapping[str, Any],
    node_names: Mapping[str, str],
) -> str:
    """Plain-text file for «📄 Все IP»: the live window when it is there, else the stored top-50."""
    out: list[str] = []
    if full:
        out.append(T["file_full"].format(sid=sid, at=fmt_dt(at), window=window))
        out.append("")
        for key, raws, nodes, seen in full:
            names = ", ".join(node_names.get(n, n) for n in nodes)
            out.append(f"{key}\t{' '.join(raws)}\t{names}\t{fmt_dt(seen)}")
        return "\n".join(out) + "\n"
    top = evidence.get("top") if isinstance(evidence, Mapping) else None
    items = top if isinstance(top, list) else []
    out.append(T["file_partial"].format(n=len(items)))
    out.append("")
    for item in items:
        if isinstance(item, Mapping):
            names = ", ".join(node_names.get(str(n), str(n)) for n in item.get("nodes") or [])
            out.append(
                f"{item.get('key')}\t{' '.join(item.get('ips') or [])}\t{names}\t{item.get('seen') or ''}"
            )
    return "\n".join(out) + "\n"


def until_after(at: datetime, minutes: int) -> datetime:
    return at + timedelta(minutes=minutes)
