"""Settings screens: navigation, cards, access rules on every callback (real PG, recording transport)."""

from __future__ import annotations

from svbg.core.component import HealthReport
from svbg.core.settings.service import Change
from svbg.tg.admin.settings import (
    ACTIONS,
    SCREEN_KEY,
    SCREEN_ROOT,
    SCREEN_SECTION,
    can_view,
    form_name,
    is_business,
)
from svbg.tg.ui.context import UserCtx
from tests.tg.admin.settings_harness import (
    ADMIN,
    ADMIN_NOPERM,
    LONG_KEY,
    OWNER,
    SUPPORT,
    USER,
    EnvFactory,
    SEnv,
    add_staff,
    extended_registry,
)

ROOT = f"v1:{SCREEN_ROOT}:o"


def key_cb(key: str) -> str:
    return f"v1:{SCREEN_KEY}:o:{key}"


# ---------------------------------------------------------------- access rules (pure)


def test_business_rules(senv: SEnv) -> None:
    reg = senv.service.registry
    owner = UserCtx(1, role="owner")
    admin = UserCtx(2, role="admin", perms=frozenset({"settings.business"}))
    star_admin = UserCtx(3, role="admin", perms=frozenset({"*"}))
    plain_admin = UserCtx(4, role="admin")
    support = UserCtx(5, role="support", perms=frozenset({"settings.business"}))
    assert is_business(reg.get("TRIAL_DAYS"))
    for key in ("BOT_TOKEN", "REMNAWAVE_TOKEN", "OWNER_IDS", "DATABASE_URL", "ENV_SECRETS", "BOT_MODE"):
        defn = reg.get(key)
        assert not is_business(defn), key
        assert can_view(owner, defn)
        assert not can_view(admin, defn)
    trial = reg.get("TRIAL_DAYS")
    assert can_view(admin, trial)
    assert can_view(star_admin, trial)
    assert not can_view(plain_admin, trial)
    assert not can_view(support, trial)  # support never edits settings, even with the perm


def test_form_names_fit_the_form_engine() -> None:
    assert form_name("TRIAL_DAYS") == "set.e.trial_days"
    long_name = form_name(LONG_KEY)
    assert len(long_name) <= 48
    assert long_name.startswith("set.e.h")
    assert long_name == form_name(LONG_KEY)  # stable across restarts


# ---------------------------------------------------------------- root


async def test_owner_root_lists_sections_with_component_status(senv: SEnv) -> None:
    await add_staff(senv)
    senv.component("remnawave").health_report = HealthReport.down("401")
    await senv.click(OWNER, ROOT)
    assert "Все настройки" in senv.text
    assert senv.last().parse_mode == "HTML"
    labels = senv.labels()
    assert any("Запуск" in label for label in labels)
    assert any("Remnawave" in label and "❌" in label for label in labels)
    assert any("Telegram" in label and "✅" in label for label in labels)
    assert any("Продажи и триал" in label for label in labels)
    assert any("Платёжки" in label for label in labels)  # stage 2: PAY_<SLUG>_* of the built-in desks
    assert not any("Рефералка" in label for label in labels)  # empty sections are hidden
    assert "🔎 Поиск" in labels
    assert senv.button("Поиск") == f"v1:{ACTIONS}:find"


async def test_admin_root_shows_business_sections_only(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(ADMIN, ROOT)
    labels = " | ".join(senv.labels())
    assert "Продажи и триал" in labels
    assert "Система" in labels  # DEFAULT_LANGUAGE / TIMEZONE / CURRENCY are business keys
    # «Админ-чат и уведомления» is visible since stage 2: user notification switches are business keys
    for hidden in ("Запуск", "База данных", "Telegram", "Remnawave", "Платёжки"):
        assert hidden not in labels
    assert "✅" not in labels  # component status is for the owner only


async def test_root_denied_for_users_without_rights(senv: SEnv) -> None:
    staff = await add_staff(senv)
    for tg in (ADMIN_NOPERM, SUPPORT, USER):
        before = len(senv.rendered())
        await senv.click(tg, ROOT)
        assert senv.toasts[-1] == "Нет прав"
        assert len(senv.rendered()) == before  # nothing rendered
    assert [uid for uid, _ in senv.denied.calls] == [
        staff["admin_noperm"].user_id,
        staff["support"].user_id,
        staff["user"].user_id,
    ]


async def test_root_reports_restart_and_problems(senv: SEnv) -> None:
    await add_staff(senv)
    senv.service.restart_pending.add("DATABASE_URL")
    senv.service.problems["REMNAWAVE_TOKEN"] = "секрет не расшифровывается <b>"
    await senv.click(OWNER, ROOT)
    assert "Ждут перезапуска: DATABASE_URL" in senv.text
    assert "секрет не расшифровывается &lt;b&gt;" in senv.text  # escaped
    await senv.click(ADMIN, ROOT)
    assert "DATABASE_URL" not in senv.text
    assert "REMNAWAVE_TOKEN" not in senv.text


# ---------------------------------------------------------------- sections


async def test_section_lists_keys_with_values_and_hides_advanced(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, f"v1:{SCREEN_SECTION}:o:system")
    labels = senv.labels()
    assert any(label.startswith("Часовой пояс: Europe/Moscow") for label in labels)
    assert not any("Вид файла .env" in label for label in labels)  # ENV_LAYOUT is advanced
    await senv.press(OWNER, "Расширенные")
    labels = senv.labels()
    assert any("Вид файла .env: Все ключи" in label for label in labels)  # the label, not «full»
    assert any("Скрыть расширенные" in label for label in labels)
    await senv.press(OWNER, "Скрыть расширенные")
    assert not any("Вид файла .env" in label for label in senv.labels())


async def test_section_with_only_advanced_keys_shows_them(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, f"v1:{SCREEN_SECTION}:o:logs")
    assert "только расширенные" in senv.text
    assert any("Уровень логов: INFO" in label for label in senv.labels())


async def test_section_pagination(make_senv: EnvFactory) -> None:
    env = await make_senv(screens_kw={"page_size": 2})
    await add_staff(env)
    await env.click(OWNER, f"v1:{SCREEN_SECTION}:o:sales")
    # nine everyday keys of «Продажи и триал» (stage 2, the entry captcha, the «Подписка» button colours)
    assert "1/5" in env.labels()
    first = [label for label in env.labels() if ":" in label]
    await env.press(OWNER, "▶️")
    assert "2/5" in env.labels()
    second = [label for label in env.labels() if ":" in label]
    assert first != second
    for _ in range(3):
        await env.press(OWNER, "▶️")
    assert "5/5" in env.labels()
    assert not any(label == "▶️" for label in env.labels())
    await env.press(OWNER, "◀️")
    assert "4/5" in env.labels()


async def test_admin_cannot_open_owner_section_by_forged_callback(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(ADMIN, f"v1:{SCREEN_SECTION}:o:remnawave")
    assert "Нет прав" in senv.text
    assert senv.denied.calls == [(staff["admin"].user_id, "section:remnawave")]


async def test_garbage_section_arg_goes_back_to_root(senv: SEnv) -> None:
    await add_staff(senv)
    for arg in ("nope", "sales:x:1", "sales:1:7", "sales:1"):
        await senv.click(OWNER, f"v1:{SCREEN_SECTION}:o:{arg}")
        assert "Все настройки" in senv.text, arg


# ---------------------------------------------------------------- cards


async def test_card_shows_everything_about_a_key(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_DAYS"))
    text = senv.text
    assert "<b>Дней пробного периода</b>" in text
    assert "<code>TRIAL_DAYS</code>" in text
    assert "Длительность пробного периода" in text
    assert "Сейчас: <b>3</b>" in text
    assert "По умолчанию: 3" in text
    assert "· по умолчанию" in text
    assert "⚡ применяется мгновенно" in text
    assert "Диапазон: 0–365" in text
    labels = senv.labels()
    assert "✏️ Изменить" in labels
    assert "↩️ По умолчанию" not in labels  # already the default
    assert "🕘 История" in labels
    assert text.rstrip().endswith("Ключ в .env: <code>TRIAL_DAYS</code>")  # the .env name comes last
    assert labels[:3] == ["1", "✅ 3", "7"]  # ready values, the current one marked
    assert senv.button("✅ 3") == f"v1:{ACTIONS}:pick:TRIAL_DAYS:3"
    # back to where the key lives in the admin: «📦 Тарифы → 🎁 Пробный период»
    assert senv.button("Пробный период") == "v1:set.v:o:p.trial"
    assert senv.button("🛠 Админка") == "v1:adm:o"


async def test_card_of_reload_and_restart_keys(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("REMNAWAVE_URL"))
    assert "🔄 переподключит компонент «remnawave»" in senv.text
    await senv.click(OWNER, key_cb("DATABASE_URL"))
    assert "♻️ нужен перезапуск" in senv.text


async def test_secret_card_is_masked_with_fingerprint(senv: SEnv) -> None:
    await add_staff(senv)
    token = "rw-token-" + "Z" * 30 + "a1B9"
    result = await senv.service.apply(
        [Change("REMNAWAVE_TOKEN", token)],
        source="cli",
        actor_id=None,
    )
    assert result.ok
    await senv.click(OWNER, key_cb("REMNAWAVE_TOKEN"))
    text = senv.text
    assert "••••••••a1B9" in text
    fp = senv.service.crypto.value_fingerprint(token)
    assert f"отпечаток <code>{fp}</code>" in text
    assert "⌨️ из командной строки" in text
    assert token not in senv.all_text()
    assert "По умолчанию" not in text  # secrets have no shown default


async def test_enum_card_has_choice_buttons(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("TRIAL_AUDIENCE"))
    labels = senv.labels()
    assert "✅ Всем" in labels  # values by their labels, not «all» / «channel_members»
    assert "Только подписчикам канала" in labels
    assert "✏️ Изменить" not in labels
    assert senv.button("Только подписчикам") == f"v1:{ACTIONS}:pick:TRIAL_AUDIENCE:channel_members"
    assert "channel_members" not in senv.text


async def test_card_accepts_aliases_and_case(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("trial_days"))
    assert "<code>TRIAL_DAYS</code>" in senv.text


async def test_unknown_key_is_stale(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("NO_SUCH_KEY"))
    assert "Все настройки" in senv.text
    assert "NO_SUCH_KEY" not in senv.text


async def test_admin_cannot_open_secret_or_owner_cards(senv: SEnv) -> None:
    staff = await add_staff(senv)
    for key in ("REMNAWAVE_TOKEN", "BOT_TOKEN", "OWNER_IDS", "ENV_SECRETS"):
        await senv.click(ADMIN, key_cb(key))
        assert "Нет прав" in senv.text, key
    assert [p for _, p in senv.denied.calls] == [
        "card:REMNAWAVE_TOKEN",
        "card:BOT_TOKEN",
        "card:OWNER_IDS",
        "card:ENV_SECRETS",
    ]
    assert all(uid == staff["admin"].user_id for uid, _ in senv.denied.calls)
    await senv.click(ADMIN, key_cb("TRIAL_DAYS"))
    assert "<code>TRIAL_DAYS</code>" in senv.text


async def test_rights_are_rechecked_on_every_callback(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(ADMIN, key_cb("TRIAL_DAYS"))
    edit = senv.button("Изменить")
    # the admin loses the permission while the card is open
    senv.users.by_tg[ADMIN] = UserCtx(staff["admin"].user_id, telegram_id=ADMIN, role="admin")
    await senv.click(ADMIN, edit)
    assert senv.toasts[-1] == "Нет прав"
    assert (await senv.ui_state.get(staff["admin"].user_id)).awaiting is None


async def test_locked_key_has_no_edit_controls(make_senv: EnvFactory) -> None:
    env = await make_senv(environ={"LOCKED_KEYS": "TRIAL_DAYS", "TRIAL_DAYS": "5"})
    await add_staff(env)
    await env.click(OWNER, key_cb("TRIAL_DAYS"))
    assert "Сейчас: <b>5</b>" in env.text
    assert "🔒 задано окружением (LOCKED_KEYS)" in env.text
    assert "Меняется только в окружении" in env.text
    assert "✏️ Изменить" not in env.labels()
    # a forged edit callback is refused with the reason
    await env.click(OWNER, f"v1:{ACTIONS}:edit:TRIAL_DAYS")
    assert "окружении" in (env.toasts[-1] or "")
    assert env.service.current()["TRIAL_DAYS"] == 5


async def test_readonly_and_file_only_keys(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, key_cb("SECRET_KEY"))
    assert "ротации ключа" in senv.text
    assert "✏️ Изменить" not in senv.labels()
    await senv.click(OWNER, key_cb("LOCKED_KEYS"))
    assert "только в файле .env" in senv.text
    assert "✏️ Изменить" not in senv.labels()
    await senv.click(OWNER, f"v1:{ACTIONS}:edit:LOCKED_KEYS")
    assert "только в файле .env" in (senv.toasts[-1] or "")


async def test_long_key_uses_short_tokens(make_senv: EnvFactory) -> None:
    env = await make_senv(registry=extended_registry())
    await add_staff(env)
    await env.click(OWNER, f"v1:{SCREEN_SECTION}:o:modules")
    data = env.button("Длинный ключ")
    assert len(data.encode()) <= 64
    assert "~" in data
    await env.click(OWNER, data)
    assert f"<code>{LONG_KEY}</code>" in env.text
    edit = env.button("Изменить")
    assert len(edit.encode()) <= 64
    await env.click(OWNER, edit)
    assert "Длинный ключ" in env.text
    assert await env.type(OWNER, "7")
    assert "Применено" in env.text
    assert env.service.current()[LONG_KEY] == 7


async def test_html_in_values_is_escaped(make_senv: EnvFactory) -> None:
    env = await make_senv(registry=extended_registry())
    await add_staff(env)
    await env.click(OWNER, key_cb("NOTE_TEXT"))
    await env.press(OWNER, "Изменить")
    assert await env.type(OWNER, "<b>x</b> & <i>")
    assert "&lt;b&gt;x&lt;/b&gt; &amp; &lt;i&gt;" in env.text
    await env.click(OWNER, key_cb("NOTE_TEXT"))
    assert "<b>x</b>" not in env.text.replace("<b>&lt;", "")
    assert "&lt;b&gt;x&lt;/b&gt;" in env.text


async def test_payment_instances_are_subsections_not_root_buttons(senv: SEnv) -> None:
    from svbg.core.settings.registry import PAYMENTS_SECTION

    await add_staff(senv)
    await senv.click(OWNER, ROOT)
    labels = senv.labels()
    assert any("Платёжки" in label for label in labels)
    assert not any("Платёжка «" in label for label in labels)  # 27 instances live inside «Платёжки»
    await senv.click(OWNER, f"v1:{SCREEN_SECTION}:o:{PAYMENTS_SECTION}")
    labels = senv.labels()
    assert any("Платёжка «Telegram Stars»" in label for label in labels)
    await senv.press(OWNER, "Платёжка «Telegram Stars»")
    assert "Telegram Stars" in senv.text
    assert any(":" in label for label in senv.labels())  # its keys
    await senv.press(OWNER, "⬅️ Платёжки")
    assert any("Платёжка «Telegram Stars»" in label for label in senv.labels())  # back to «Платёжки»
