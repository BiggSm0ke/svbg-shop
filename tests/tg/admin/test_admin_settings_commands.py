"""Search and the commands ``/set`` and ``/settings``."""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, SendMessage

from svbg.core.log import SecretRegistry
from svbg.tg.admin.settings import ACTIONS, SCREEN_DONE, SCREEN_FIND
from tests.tg.admin.settings_harness import ADMIN, OWNER, SUPPORT, USER, SEnv, add_staff
from tests.tg.ui.ui_harness import text_message

BOT_TOKEN = "123456789:AAH" + "x" * 32


async def run_set(env: SEnv, tg: int, text: str, message_id: int = 900) -> bool:
    return await env.screens.handle_set(text_message(tg, text, message_id=message_id))


# ---------------------------------------------------------------- search


async def test_search_form_finds_keys(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, f"v1:{ACTIONS}:find")
    assert "Что найти" in senv.text
    assert await senv.type(OWNER, "пробный")
    assert "Поиск" in senv.text
    assert any(label.startswith("Дней пробного периода") for label in senv.labels())
    await senv.press(OWNER, "Дней пробного периода")
    assert "<code>TRIAL_DAYS</code>" in senv.text


async def test_search_is_filtered_by_rights(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(ADMIN, f"v1:{ACTIONS}:find")
    assert await senv.type(ADMIN, "токен")
    assert not any("Токен" in label or "токен" in label for label in senv.labels())
    assert "Ничего не найдено" in senv.text
    await senv.click(OWNER, f"v1:{ACTIONS}:find")
    assert await senv.type(OWNER, "токен")
    assert any("Токен бота" in label for label in senv.labels())


async def test_search_too_short_and_cancel(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, f"v1:{ACTIONS}:find")
    assert await senv.type(OWNER, "x")
    assert "Слишком коротко" in senv.text
    await senv.press(OWNER, "Отмена")
    assert "Все настройки" in senv.text


async def test_find_screen_masks_tokens_in_the_query(senv: SEnv) -> None:
    await add_staff(senv)
    await senv.click(OWNER, f"v1:{SCREEN_FIND}:o:{BOT_TOKEN}")
    assert BOT_TOKEN not in senv.text
    assert "***" in senv.text


# ---------------------------------------------------------------- /set


async def test_set_key_value_applies_and_shows_undo(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, "/set TRIAL_DAYS 10")
    assert senv.service.current()["TRIAL_DAYS"] == 10
    assert isinstance(senv.last(), SendMessage)
    assert "⚡ <b>Применено</b>" in senv.text
    undo = senv.button("Отменить")
    await senv.click(OWNER, undo)
    assert senv.service.current()["TRIAL_DAYS"] == 3
    assert not senv.transport.of(DeleteMessage) or all(
        c.message_id != 900 for c in senv.transport.of(DeleteMessage)
    )  # a plain value is not deleted


async def test_set_value_with_spaces_and_alias_case(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, "/set timezone   Europe/Moscow")
    assert "Применено" in senv.text  # explicitly set (stored), even though it equals the default
    assert await run_set(senv, OWNER, "/set TIMEZONE Europe/Moscow", message_id=903)
    assert "Значение не изменилось" in senv.text
    assert "Отменить" not in " ".join(senv.labels())
    assert await run_set(senv, OWNER, "/set@svbg_bot TRIAL_AUDIENCE channel_members", message_id=901)
    assert senv.service.current()["TRIAL_AUDIENCE"] == "channel_members"


async def test_set_rejects_with_reason(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, "/set TRIAL_DAYS много")
    assert "Не применено" in senv.text
    assert "ожидалось целое число" in senv.text
    assert await run_set(senv, OWNER, "/set SECRET_KEY abc", message_id=901)
    assert "ротации ключа" in senv.text


async def test_set_secret_deletes_the_message_first(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, f"/set BOT_TOKEN {BOT_TOKEN}", message_id=950)
    first = senv.transport.calls[0]
    assert isinstance(first, DeleteMessage) and first.message_id == 950
    assert senv.service.current()["BOT_TOKEN"] == BOT_TOKEN
    assert BOT_TOKEN not in senv.all_text()
    assert "Применено" in senv.text
    assert "удалите его вручную" not in senv.text


async def test_set_secret_warns_when_deletion_fails(senv: SEnv) -> None:
    await add_staff(senv)
    senv.transport.fail_next(
        DeleteMessage, TelegramBadRequest(method=DeleteMessage(chat_id=1, message_id=1), message="too old")
    )
    assert await run_set(senv, OWNER, f"/set REMNAWAVE_TOKEN {'s' * 40}", message_id=951)
    assert "Не получилось удалить сообщение с секретом" in senv.text
    assert "s" * 40 not in senv.all_text()


async def test_set_unknown_key_with_token_value_is_deleted_and_not_echoed(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, f"/set TOKEN {BOT_TOKEN}", message_id=952)
    assert any(c.message_id == 952 for c in senv.transport.of(DeleteMessage))
    assert BOT_TOKEN not in senv.all_text()
    assert "Поиск" in senv.text and "«TOKEN»" in senv.text


async def test_set_without_value_and_search(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, "/set")
    assert "Все настройки" in senv.text
    assert await run_set(senv, OWNER, "/set TRIAL_DAYS", message_id=901)
    assert "<code>TRIAL_DAYS</code>" in senv.text
    assert await run_set(senv, OWNER, "/set пробный период", message_id=902)
    assert "«пробный период»" in senv.text
    assert any(label.startswith("Дней пробного периода") for label in senv.labels())


async def test_set_is_for_the_owner_only(senv: SEnv) -> None:
    staff = await add_staff(senv)
    assert await run_set(senv, ADMIN, "/set TRIAL_DAYS 10")
    assert senv.text == "Нет прав"
    assert senv.denied.calls == [(staff["admin"].user_id, "command:/set")]
    assert await run_set(senv, SUPPORT, "/set TRIAL_DAYS 10")
    assert not await run_set(senv, USER, "/set TRIAL_DAYS 10")  # ordinary users: not our command
    assert not await run_set(senv, 999_999, "/set TRIAL_DAYS 10")  # unknown user
    assert senv.service.current()["TRIAL_DAYS"] == 3
    # an ordinary user's message with a token is still removed from the chat
    assert await run_set(senv, USER, f"/set BOT_TOKEN {BOT_TOKEN}", message_id=990)
    assert any(c.message_id == 990 for c in senv.transport.of(DeleteMessage))
    assert senv.service.current()["BOT_TOKEN"] is None


async def test_set_ignores_groups(senv: SEnv) -> None:
    await add_staff(senv)
    assert not await senv.screens.handle_set(text_message(OWNER, "/set TRIAL_DAYS 10", chat_type="group"))
    assert senv.service.current()["TRIAL_DAYS"] == 3


async def test_set_from_non_owners_does_not_register_secrets(senv: SEnv) -> None:
    await add_staff(senv)
    junk = ["polling-" + str(i) for i in range(3)] + ["s" * 4000]
    for i, value in enumerate(junk):
        for tg in (USER, ADMIN, 999_999):
            await run_set(senv, tg, f"/set BOT_TOKEN {value}", message_id=1000 + i)
    for value in junk:
        assert not SecretRegistry.contains(value)  # no registry growth, no forged "***" in logs
    assert senv.service.current()["BOT_TOKEN"] is None
    # the owner's value is registered (it is going to be applied)
    owner_value = "owner-secret-" + "q" * 20
    assert await run_set(senv, OWNER, f"/set REMNAWAVE_TOKEN {owner_value}", message_id=1100)
    assert SecretRegistry.contains(owner_value)


async def test_secret_posted_in_a_group_is_deleted_and_not_applied(senv: SEnv) -> None:
    await add_staff(senv)
    secret = "g" * 40
    msg = text_message(OWNER, f"/set REMNAWAVE_TOKEN {secret}", message_id=1200, chat_type="supergroup")
    assert await senv.screens.handle_set(msg)
    assert any(c.message_id == 1200 for c in senv.transport.of(DeleteMessage))
    assert not senv.transport.of(SendMessage)  # deleted: nothing to warn about
    assert senv.service.current()["REMNAWAVE_TOKEN"] != secret
    assert not SecretRegistry.contains(secret)
    # a token-looking value under an unknown key is removed too, for any member
    assert await senv.screens.handle_set(
        text_message(USER, f"/set X {BOT_TOKEN}", message_id=1201, chat_type="group")
    )
    assert any(c.message_id == 1201 for c in senv.transport.of(DeleteMessage))


async def test_secret_in_a_group_warns_once_when_it_cannot_be_deleted(senv: SEnv) -> None:
    await add_staff(senv)
    for i in range(3):
        senv.transport.fail_next(
            DeleteMessage,
            TelegramBadRequest(method=DeleteMessage(chat_id=1, message_id=1), message="no rights"),
        )
        msg = text_message(OWNER, f"/set BOT_TOKEN {BOT_TOKEN}", message_id=1300 + i, chat_type="group")
        assert await senv.screens.handle_set(msg)
    warnings = senv.transport.of(SendMessage)
    assert len(warnings) == 1  # rate-limited per group
    assert "смените этот секрет" in warnings[0].text
    assert BOT_TOKEN not in senv.all_text()
    assert senv.service.current()["BOT_TOKEN"] is None


async def test_done_screen_belongs_to_its_user(senv: SEnv) -> None:
    await add_staff(senv)
    assert await run_set(senv, OWNER, "/set TRIAL_DAYS 11")
    batch = next(iter(senv.screens._results))
    await senv.click(ADMIN, f"v1:{SCREEN_DONE}:o:{batch}")
    assert "Применено" not in senv.text
    assert "Все настройки" in senv.text


async def test_user_loader_failure_is_contained(senv: SEnv) -> None:
    await add_staff(senv)
    senv.users.fail = RuntimeError("db down")
    assert not await run_set(senv, OWNER, "/set TRIAL_DAYS 10")
    assert senv.service.current()["TRIAL_DAYS"] == 3


# ---------------------------------------------------------------- /settings


async def test_settings_command(senv: SEnv) -> None:
    await add_staff(senv)
    assert await senv.screens.handle_settings(text_message(ADMIN, "/settings"))
    assert "Все настройки" in senv.text
    assert isinstance(senv.last(), SendMessage)
    assert not await senv.screens.handle_settings(text_message(USER, "/settings"))
    assert not await senv.screens.handle_settings(text_message(SUPPORT, "/settings"))


async def test_aiogram_router_is_built(senv: SEnv) -> None:
    router: Any = senv.screens.aiogram_router()
    assert router.name == "svbg-settings"
    assert len(router.message.handlers) == 2


async def test_set_abandons_a_half_filled_form(senv: SEnv) -> None:
    staff = await add_staff(senv)
    await senv.click(OWNER, "v1:set.key:o:TRIAL_DAYS")
    await senv.press(OWNER, "Изменить")
    assert (await senv.ui_state.get(staff["owner"].user_id)).awaiting is not None
    assert await run_set(senv, OWNER, "/set TRIAL_DAYS 6")
    assert (await senv.ui_state.get(staff["owner"].user_id)).awaiting is None
    assert senv.service.current()["TRIAL_DAYS"] == 6
    assert not await senv.type(OWNER, "8")  # free text no longer goes into the old form


async def test_module_setup_entry_point(make_senv: Any) -> None:
    from types import SimpleNamespace

    from svbg.tg.admin.settings import setup

    env = await make_senv()
    other = env.router.__class__(
        transport=env.transport, user_loader=env.users, ui_state=env.ui_state, content=None, codec=None
    )
    router: Any = setup(other, SimpleNamespace(settings=env.service))
    assert router.name == "svbg-settings"
    assert "settings_root" in other._screens
