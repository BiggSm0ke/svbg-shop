"""IP Guard texts (Russian, Telegram HTML; the messages to the user also exist in English) and pure card
renderers (05 §2.2.2).

Cards are built **from database rows** (never from ``callback.message``): a restart or a second admin sees the
same card. Every value that came from a user or the panel goes through :func:`esc`.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

__all__ = [
    "CLOSE_REASONS",
    "KIND_TITLES",
    "MSK",
    "USER_KEYS",
    "USER_T_EN",
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
MAIN_LIMIT: Final = 3900

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
    "block_title": "🚨 <b>Подписка заблокирована</b> · {reason}",
    "metrics": "IP за {window} мин: <b>{w}</b> · подсетей {s} · живых {l}",
    "thresholds": "Пороги: предупреждение {warn}, блок {block}, подсетей ≥{subnets}, живых ≥{live}",
    "frozen": "❄️ Заморожено: {left}",
    "actions": "🛡 <b>Действия</b>",
    "act_bot": "• Бот: ✅ подписка заморожена",
    "act_panel_ok": "• Панель: ✅ отключена",
    "act_panel_wait": "• Панель: ⏳ отключаю (повторяю, пока не выйдет)",
    "act_drop_ok": "• Соединения: разорваны на {n} нодах",
    "act_drop_wait": "• Соединения: ⏳ разрываю",
    "act_drop_failed": "• Соединения: ⚠️ не разорваны (нет прав connections:drop)",
    "act_drop_none": "• Соединения: нечего разрывать",
    "act_residual": "• ⚠️ Остались соединения: UDP/QUIC, нет CAP_NET_ADMIN у ноды или долгий mux",
    "act_notify": "• Пользователь: {state}",
    "notify_sent": "✅ уведомлён",
    "notify_unreachable": "⚠️ бот заблокирован или нет диалога",
    "notify_failed": "⚠️ не удалось отправить",
    "notify_off": "уведомления выключены",
    "notify_wait": "⏳",
    "unblocked": "🔓 <b>Разблокирована</b> ({by}, {at}): {outcome}",
    "unblocked_revoke": " + новая ссылка",
    "outcome_active": "возвращено {left}, активна до {until}",
    "outcome_expired": "срок закончился, пользователь продлит сам",
    "outcome_zeroed": "подписка была обнулена",
    "outcome_gone": "подписки больше нет",
    "closed": "🗑 <b>Блок закрыт</b> ({by}, {at}): подписка обнулена, аккаунт отключён",
    "confirmed": "✅ Проверено: {by}",
    "bypass": (
        "🚨 Включили в обход кнопки, поэтому отключил снова. Снимайте блок кнопкой, иначе дни не вернутся."
    ),
    "warn_title": "⚠️ <b>Подозрение на слив</b>",
    "nb_title": "ℹ️ <b>Не блокирую:</b> {why}",
    "failed_title": "🚨 <b>Автоблок НЕ выполнен:</b> {why}",
    "acked": "✅ Проверено ({by})",
    "top_title": "Самые активные IP:",
    "top_more": "…и ещё {n}",
    "anomaly_title": "🛑 <b>Автоблоки остановлены</b>",
    "anomaly_reason": "Причина: {why}",
    "anomaly_until": "Карантин до {at} МСК. Пока он идёт, никого не блокирую.",
    "anomaly_over": "Карантин закончился.",
    "anomaly_members": "Участники ({n}):",
    "anomaly_member": "• {who} — {w} IP · {state}",
    "member_pending": "ждёт решения",
    "member_blocked": "🚫 заблокирован",
    "member_dismissed": "✅ ложная тревога",
    "member_skipped": "пропущен",
    "digest_title": "🧾 <b>Ещё {n} предупреждений за проход</b>",
    "digest_line": "• №{sid} — {w} IP · {kind}",
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

#: English of :data:`USER_KEYS` (same placeholders and markup as the Russian ones).
USER_T_EN: Final[Mapping[str, str]] = {
    "user_blocked": (
        "🚫 <b>Subscription blocked</b>\n\n"
        "Your link is being used from {n} different IPs. Looks like it was shared with other people.\n"
        "Your remaining days are frozen and will come back once you are unblocked.\n\n"
        "Contact support and we will sort it out."
    ),
    "user_unblocked": ("✅ <b>Access restored</b>\nThe frozen time is back: {left}.\nActive until {until}."),
    "user_unblocked_revoked": "\n\nThe link was renewed. Open «📱 My subscription» and connect again.",
    "user_unblocked_expired": (
        "✅ <b>Access restored</b>\nYour subscription has ended. Renew it to connect."
    ),
    "btn_support": "💬 Support",
    "btn_close": "Close",
    "btn_my_sub": "📱 My subscription",
    "btn_renew": "💳 Renew",
    "banner": "🚫 <b>Subscription blocked</b>\nDays are frozen, {left} left.\nContact support.",
    "status_line": "🚫 Subscription blocked. Contact support.",
}


def user_t(key: str, lang: str | None = "ru") -> str:
    """A user-facing string of :data:`USER_KEYS` in ``lang`` (Russian fallback)."""
    if lang == "en" and key in USER_T_EN:
        return USER_T_EN[key]
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


def fmt_duration(seconds: float | None, lang: str | None = "ru") -> str:
    """``N дн. M ч``; under an hour — ``M мин`` (English: ``N d M h`` / ``M min``)."""
    d, h, m = ("d", "h", "min") if lang == "en" else ("дн.", "ч", "мин")
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


def _top_lines(evidence: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    top = evidence.get("top") if isinstance(evidence, Mapping) else None
    if not isinstance(top, list) or not top:
        return []
    lines = [T["top_title"]]
    for item in top[:MAX_TOP_LINES]:
        if not isinstance(item, Mapping):
            continue
        nodes = ", ".join(esc(names.get(str(n), str(n)[:8]), 24) for n in item.get("nodes") or [])
        lines.append(f"<code>{esc(item.get('key'), 64)}</code> — {nodes or DASH}")
    rest = int(evidence.get("total") or len(top)) - min(len(top), MAX_TOP_LINES)
    if rest > 0:
        lines.append(T["top_more"].format(n=rest))
    return lines


def _quote(lines: Sequence[str]) -> str:
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>" if lines else ""


def _clip(text: str) -> str:
    return text if len(text) <= MAIN_LIMIT else text[: MAIN_LIMIT - 1] + "…"


def _metrics(m: Mapping[str, Any], window: int) -> str:
    return T["metrics"].format(
        window=window,
        w=int(m.get("ip_count") or 0),
        s=int(m.get("subnet_count") or 0),
        l=int(m.get("live_ip_count") or 0),
    )


def _by(name: str | None) -> str:
    return esc(name) if name else "система"


def block_card(
    block: Mapping[str, Any],
    *,
    person: str,
    window: int,
    frozen_left: int,
    panel_disabled: bool,
    by_names: Mapping[str, str | None],
    node_names: Mapping[str, str] = {},
) -> str:
    """The block card: header, numbers, the live «🛡 Действия» block, the outcome and the top IPs."""
    reason = T.get(f"reason_{block['reason']}", str(block["reason"]))
    lines = [
        T["block_title"].format(reason=reason),
        f"👤 {person} · подписка №{int(block['subscription_id'])}",
        _metrics(block, window),
    ]
    status = block["status"]
    events = [e for e in block.get("events") or [] if isinstance(e, Mapping)]
    kinds = [str(e.get("kind")) for e in events]
    if status == "active":
        lines.append(T["frozen"].format(left=fmt_duration(frozen_left)))
        lines += ["", T["actions"], T["act_bot"]]
        lines.append(T["act_panel_ok"] if panel_disabled else T["act_panel_wait"])
        drop = next(
            (e for e in reversed(events) if e.get("kind") in ("dropped", "drop_failed", "drop_none")), None
        )
        if drop is None:
            lines.append(T["act_drop_wait"])
        elif drop.get("kind") == "dropped":
            lines.append(T["act_drop_ok"].format(n=int(drop.get("nodes") or 0)))
        elif drop.get("kind") == "drop_none":
            lines.append(T["act_drop_none"])
        else:
            lines.append(T["act_drop_failed"])
        if "residual" in kinds:
            lines.append(T["act_residual"])
        notify = next((e for e in reversed(events) if e.get("kind") == "notify"), None)
        state = T.get(f"notify_{notify.get('state')}", T["notify_wait"]) if notify else T["notify_wait"]
        lines.append(T["act_notify"].format(state=state))
        if "bypass" in kinds:
            lines.append(T["bypass"])
        if block.get("confirmed_at") is not None:
            lines.append(T["confirmed"].format(by=_by(by_names.get("confirmed"))))
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
        lines.append(
            T["unblocked"].format(
                by=_by(by_names.get("unblocked")), at=fmt_dt(block.get("unblocked_at")), outcome=text
            )
        )
    else:
        lines.append(T["closed"].format(by=_by(by_names.get("closed")), at=fmt_dt(block.get("closed_at"))))
    quote = _quote(_top_lines(block.get("evidence") or {}, node_names))
    return _clip("\n".join(lines) + ("\n" + quote if quote else ""))


def _kind_why(alert: Mapping[str, Any]) -> str:
    reason = str(alert.get("reason") or "")
    m = alert.get("metrics") or {}
    template = KIND_TITLES.get(reason, reason or DASH)
    return template.format(
        left=int(m.get("sustained_left") or 1),
        nodes=", ".join(esc(n, 24) for n in m.get("missing_nodes") or []) or DASH,
        until=fmt_time(_parse(m.get("until"))),
        why=esc(m.get("precondition") or "автоблок выключен", 120),
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
) -> str:
    kind = alert["kind"]
    m = alert.get("metrics") or {}
    if kind == "warn":
        title = T["warn_title"]
    elif kind == "block_failed":
        title = T["failed_title"].format(why=esc(m.get("fail_reason") or "ошибка", 160))
    else:
        title = T["nb_title"].format(why=_kind_why(alert))
    sub = alert.get("subscription_id")
    lines = [
        title,
        f"👤 {person}" + (f" · подписка №{int(sub)}" if sub is not None else ""),
        _metrics(m, window),
        T["thresholds"].format(**thresholds),
    ]
    if alert.get("acked_at") is not None:
        lines.append(T["acked"].format(by=_by(acked_by)))
    quote = _quote(_top_lines(alert.get("evidence") or {}, node_names))
    return _clip("\n".join(lines) + ("\n" + quote if quote else ""))


def anomaly_card(
    alert: Mapping[str, Any],
    *,
    now: datetime,
    people: Mapping[str, str],
    params: Mapping[str, int],
) -> str:
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
    lines = [T["anomaly_title"], T["anomaly_reason"].format(why=why)]
    lines.append(
        T["anomaly_until"].format(at=fmt_time(until)) if until and until > now else T["anomaly_over"]
    )
    members = alert.get("members") or {}
    lines += ["", T["anomaly_members"].format(n=len(members))]
    for pid, info in sorted(members.items(), key=lambda kv: -int((kv[1] or {}).get("ip") or 0))[:30]:
        state = T.get(f"member_{(info or {}).get('state')}", T["member_pending"])
        lines.append(
            T["anomaly_member"].format(
                who=people.get(str(pid), f"панель {esc(pid)}"),
                w=int((info or {}).get("ip") or 0),
                state=state,
            )
        )
    if len(members) > 30:
        lines.append(T["top_more"].format(n=len(members) - 30))
    return _clip("\n".join(lines))


def digest_card(alert: Mapping[str, Any]) -> str:
    members = alert.get("members") or {}
    lines = [T["digest_title"].format(n=len(members))]
    for _pid, raw in list(members.items())[:40]:
        info = raw or {}
        kind = KIND_TITLES.get(str(info.get("kind")), str(info.get("kind")))
        if "{" in kind:
            kind = str(info.get("kind"))
        lines.append(
            T["digest_line"].format(
                sid=int(info.get("sub") or 0), w=int(info.get("ip") or 0), kind=esc(kind, 80)
            )
        )
    return _clip("\n".join(lines))


def unblock_user_text(
    outcome: str, *, left: int, until: datetime | None, revoked: bool, lang: str | None = "ru"
) -> str:
    if outcome != "active":
        return user_t("user_unblocked_expired", lang)
    text = user_t("user_unblocked", lang).format(left=fmt_duration(left, lang), until=fmt_dt(until))
    return text + (user_t("user_unblocked_revoked", lang) if revoked else "")


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
