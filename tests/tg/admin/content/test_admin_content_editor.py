"""Constructor in the bot: режим «✏️», screen editor (text + entities, media), button wizard, undo, rights."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto

from svbg.tg.ui import codec
from svbg.tg.ui.edit_mode import ACTIONS, SCREEN_BUTTON, SCREEN_EDITOR, SCREEN_PREVIEW
from tests.tg.admin.content.kit import ADMIN, EMOJI, OWNER, USER, CEnv, msg
from tests.tg.ui.ui_harness import callback, text_message


def home_id(ce: CEnv) -> int:
    entry = ce.env.content.get_screen("home")
    assert entry is not None
    return entry.id


async def test_edit_mode_shows_service_row_only_to_the_editor(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user")
    assert await ce.screens.handle_edit_command(text_message(OWNER, "/edit"))
    sid = home_id(ce)
    await ce.click(OWNER, "home")
    texts = [t for t, _ in ce.buttons()]
    assert texts[-3:] == ["✏️ Экран", "➕ Кнопка", "👁 Как видит…"]
    datas = [d for _, d in ce.buttons() if d]
    # every content button opens its editor instead of acting
    assert all(d.startswith(f"v1:{SCREEN_BUTTON}:o:") for d in datas[:-3])
    assert datas[-3] == codec.encode(SCREEN_EDITOR, codec.ACTION_OPEN, str(sid))

    await ce.click(USER, "home")
    user_texts = [t for t, _ in ce.buttons()]
    assert "✏️ Экран" not in user_texts
    assert not any((d or "").startswith(f"v1:{SCREEN_BUTTON}") for _, d in ce.buttons())

    # /edit again turns it off
    assert await ce.screens.handle_edit_command(text_message(OWNER, "/edit"))
    await ce.click(OWNER, "home")
    assert "✏️ Экран" not in [t for t, _ in ce.buttons()]


async def test_edit_requires_content_edit_right(ce: CEnv) -> None:
    await ce.add(ADMIN, "admin", perms=["plans"])
    await ce.add(USER, "user")
    assert not await ce.screens.handle_edit_command(text_message(ADMIN, "/edit"))
    assert not await ce.screens.handle_edit_command(text_message(USER, "/edit"))
    sid = home_id(ce)
    await ce.click(ADMIN, SCREEN_EDITOR, arg=str(sid))
    assert ce.toasts[-1] == "Нет прав"
    # an admin with the right can edit
    await ce.add(ADMIN + 1, "admin", perms=["content.edit"])
    await ce.click(ADMIN + 1, SCREEN_EDITOR, arg=str(sid))
    assert "✏️ Экран «" in ce.last_text()


async def test_owner_changes_text_others_see_it_next_click_and_undo_restores(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user")
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    old = entry.text("ru").text
    await ce.click(OWNER, SCREEN_EDITOR, arg=str(sid))
    await ce.press(OWNER, "📝 Текст RU")
    assert "Пришлите новый текст экрана (ru)" in ce.last_text()
    entities = [
        {"type": "bold", "offset": 0, "length": 6},
        {"type": "spoiler", "offset": 7, "length": 5},
        {"type": "custom_emoji", "offset": 13, "length": 2, "custom_emoji_id": EMOJI},
    ]
    assert await ce.send(msg(OWNER, "Привет скоро 🔥", entities=entities))
    assert "⚡ Применено" in ce.last_text()
    assert any("↩️ Отменить" in t for t, _ in ce.buttons())

    await ce.click(USER, "home")
    shown = ce.last()
    assert isinstance(shown, EditMessageText | SendMessage)
    assert shown.text == "Привет скоро 🔥"
    assert [e.type for e in shown.entities or []] == ["bold", "spoiler", "custom_emoji"]

    await ce.click(OWNER, SCREEN_EDITOR, arg=str(sid))
    await ce.press(OWNER, "↩️ Отменить")
    assert ce.toasts[-1] == "↩️ Отменено"
    await ce.click(USER, "home")
    assert ce.last_text() == old.replace("{balance}", "0 ₽")


async def test_screen_picture_is_uploaded_stored_and_shown(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user")
    sid = home_id(ce)
    await ce.act(OWNER, "md", str(sid))
    assert "Пришлите фото, GIF или видео" in ce.last_text()
    assert await ce.send(msg(OWNER, "не то"))
    assert "Нужно фото, GIF или видео" in ce.last_text()  # re-asked, capture kept
    assert await ce.send(msg(OWNER, photo=True))
    assert ce.downloads == ["PHOTO-ID"]
    entry = ce.env.content.get_screen(sid)
    assert entry is not None and entry.screen.media_id is not None
    media = ce.env.content.get_media(entry.screen.media_id)
    assert media is not None and media.kind == "photo"
    assert (ce.library.root / str(media.path)).is_file()
    assert media.file_ids.get("42") == "PHOTO-ID"  # kept as is → the bot's file_id is reused
    await ce.click(USER, "home")
    sent = ce.last()
    assert isinstance(sent, SendPhoto) and sent.photo == "PHOTO-ID"


async def test_button_wizard_creates_button_with_condition(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(USER, "user", sub_state="expired")
    await ce.add(USER + 1, "user", sub_state="active")
    sid = home_id(ce)
    await ce.act(OWNER, "nb", str(sid))
    assert "шаг 1 из 2" in ce.last_text()
    assert await ce.send(msg(OWNER, "🔥 Вернуться со скидкой"))
    assert "шаг 2 из 2" in ce.last_text()
    await ce.press(OWNER, "🔗 Ссылка")
    assert await ce.send(msg(OWNER, "ftp://nope"))
    assert "Не подходит" in ce.last_text()
    assert await ce.send(msg(OWNER, "https://example.com/sale"))
    assert "кнопка добавлена" in ce.last_text()
    bid = int(ce.button_data("👁 Условие").rsplit(":", 1)[1])

    await ce.press(OWNER, "👁 Условие")
    await ce.press(OWNER, "Подписка истекла")
    assert "Видна: Подписка истекла" in ce.last_text()
    await ce.press(OWNER, "🎨 Цвет")
    await ce.press(OWNER, "зелёный")
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    b = next(x for x in entry.screen.buttons if x.id == bid)
    assert b.visible_if == {"sub": "expired"} and b.style == "success"

    await ce.click(USER, "home")
    assert "🔥 Вернуться со скидкой" in [t for t, _ in ce.buttons()]
    await ce.click(USER + 1, "home")
    assert "🔥 Вернуться со скидкой" not in [t for t, _ in ce.buttons()]


async def test_button_actions_screen_and_system_pickers(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    await ce.act(OWNER, "nb", str(sid))
    await ce.send(msg(OWNER, "Купить"))
    await ce.press(OWNER, "⚙️ Системное")
    assert "buy" in [t for t, _ in ce.buttons()]
    await ce.press(OWNER, "buy")
    assert "Действие: системное: buy" in ce.last_text()
    bid = int(ce.button_data("👁 Условие").rsplit(":", 1)[1])
    await ce.press(OWNER, "⚡ Действие")
    await ce.press(OWNER, "📄 Экран")
    await ce.press(OWNER, "· bal")
    bal = ce.env.content.get_screen("bal")
    assert bal is not None
    assert f"Действие: экран «{bal.screen.title['ru']}»" in ce.last_text()
    assert button(ce, bid).action.to_json() == {"type": "screen", "target": "bal"}

    # «➡️ Перейти» opens exactly the target screen
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "➡️ Перейти")
    followed_text, followed_buttons = ce.last_text(), ce.buttons()
    await ce.click(OWNER, "bal")
    assert (ce.last_text(), ce.buttons()) == (followed_text, followed_buttons)
    await ce.click(OWNER, "home")
    assert ce.last_text() != followed_text
    assert "Меню обновилось" not in [t for t in ce.toasts if t]

    # «↕️ Позиция»: the button really moves one row down
    row = button(ce, bid).row
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "↕️ Позиция")
    assert f"ряд {row}," in ce.last_text()
    await ce.press(OWNER, "⬇️")
    assert button(ce, bid).row == row + 1
    assert f"ряд {row + 1}," in ce.last_text()


def button(ce: CEnv, bid: int) -> Any:
    for entry in ce.env.content.snapshot.by_id.values():
        for b in entry.screen.buttons:
            if b.id == bid:
                return b
    raise AssertionError(f"button {bid} not found")


async def custom_button(ce: CEnv, action: str = "copy:x") -> tuple[int, int]:
    """A custom screen with one more button: (screen id, button id)."""
    res = await ce.editor.create_screen("Акция", code="promo_x", actor=None)
    assert res.screen_id is not None and res.version is not None
    added = await ce.editor.add_button(
        res.screen_id, label={"ru": "Кнопка"}, action=action, expected_version=res.version, actor=None
    )
    assert added.created_id is not None
    return res.screen_id, added.created_id


async def test_changing_the_action_never_overwrites_another_admin(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    _sid, bid = await custom_button(ce)
    # A opens «⚡ Действие» → «📄 Экран»; meanwhile B changes the action of the same button
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "⚡ Действие")
    action_data = ce.button_data("🔗 Ссылка")
    await ce.press(OWNER, "📄 Экран")
    ver = ce.env.content.snapshot.by_id[_sid].screen.version
    await ce.editor.update_button(bid, action="url:https://b.example", expected_version=ver, actor=None)
    await ce.press(OWNER, "· bal")
    assert "успел изменить другой админ" in (ce.toasts[-1] or "")
    assert button(ce, bid).action.to_json() == {"type": "url", "url": "https://b.example"}

    # the value branch (a link typed as a message) keeps the version of the card, not of the click
    await ce.router.dispatch_callback(callback(OWNER, action_data))
    assert await ce.send(msg(OWNER, "https://a.example"))
    assert "успел изменить другой админ" in ce.last_text()
    assert button(ce, bid).action.to_json() == {"type": "url", "url": "https://b.example"}

    # the system picker too
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "⚡ Действие")
    await ce.press(OWNER, "⚙️ Системное")
    ver = ce.env.content.snapshot.by_id[_sid].screen.version
    await ce.editor.update_button(bid, label={"ru": "Другое"}, expected_version=ver, actor=None)
    await ce.press(OWNER, "buy")
    assert "успел изменить другой админ" in (ce.toasts[-1] or "")
    assert button(ce, bid).action.to_json() == {"type": "url", "url": "https://b.example"}

    # a fresh card works; a wizard message of the old format (no version) only re-opens the card
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "⚡ Действие")
    await ce.press(OWNER, "⚙️ Системное")
    await ce.press(OWNER, "buy")
    assert button(ce, bid).action.to_json() == {"type": "system", "name": "buy"}
    await ce.act(OWNER, "b.at", f"{bid}.screen")
    assert ce.toasts[-1] == "Мастер устарел — начните заново"


async def test_callback_writes_with_a_stale_version_are_refused(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid, bid = await custom_button(ce)
    old = ce.env.content.snapshot.by_id[sid].screen.version
    await ce.editor.set_title(sid, {"ru": "Другое название"}, expected_version=old, actor=None)
    before = button(ce, bid)
    for name, arg in (
        ("b.en", f"{bid}.{old}"),
        ("b.del", f"{bid}.{old}"),
        ("b.st", f"{bid}.{old}.danger"),
        ("b.icx", f"{bid}.{old}"),
        ("b.cd", f"{bid}.{old}.1"),
        ("b.mv", f"{bid}.{old}.up"),
    ):
        await ce.act(OWNER, name, arg)
        assert "успел изменить другой админ" in (ce.toasts[-1] or ""), name
    assert button(ce, bid) == before


async def test_module_actions_in_the_button_wizard(ce: CEnv) -> None:
    from svbg.tg.ui.renderer import MODULE_SCREEN

    await ce.add(OWNER, "owner")

    @ce.router.action(MODULE_SCREEN, "promo.claim")
    async def _claim(_ctx: Any, _arg: Any) -> None:
        return None

    @ce.router.action(MODULE_SCREEN, "promo.admin", required_role="admin")
    async def _admin(_ctx: Any, _arg: Any) -> None:
        return None

    sid, bid = await custom_button(ce)
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(bid))
    await ce.press(OWNER, "⚡ Действие")
    await ce.press(OWNER, "🧩 Модуль")
    names = [t for t, _ in ce.buttons()]
    assert "promo.claim" in names and "promo.admin" not in names  # only actions users may run
    await ce.press(OWNER, "promo.claim")
    assert "Действие: модуль promo.claim" in ce.last_text()
    assert button(ce, bid).action.to_json() == {"type": "module", "ext": "promo", "action": "claim"}
    # a new button with a module action
    await ce.act(OWNER, "nb", str(sid))
    assert await ce.send(msg(OWNER, "Забрать бонус"))
    await ce.press(OWNER, "🧩 Модуль")
    await ce.press(OWNER, "promo.claim")
    assert "кнопка добавлена" in ce.last_text()


async def test_icon_from_premium_emoji_or_custom_sticker_only(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    bid = next(b.id for b in entry.screen.buttons if b.id is not None)
    await ce.act(OWNER, "b.ic", str(bid))
    assert "премиум-эмодзи" in ce.last_text()
    # a regular sticker → clear refusal, nothing saved
    assert await ce.send(msg(OWNER, sticker_type="regular"))
    assert "Это обычный стикер — пришлите премиум-эмодзи из набора эмодзи" in ce.last_text()
    # an id Telegram does not know
    assert await ce.send(msg(OWNER, sticker="999", sticker_type="custom_emoji"))
    assert "Telegram не знает такой эмодзи" in ce.last_text()
    # a premium emoji in a text message
    ents = [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": EMOJI}]
    assert await ce.send(msg(OWNER, "🔥", entities=ents))
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    b = next(x for x in entry.screen.buttons if x.id == bid)
    assert b.icon_custom_emoji_id == EMOJI
    assert any(type(c).__name__ == "GetCustomEmojiStickers" for c in ce.transport.calls)
    # the probe ran with this icon and its result is shown in the wizard
    assert ce.premium.state.status == "ok" and ce.premium.state.emoji_id == EMOJI
    assert "премиум-эмодзи работают" in ce.last_text()


async def test_icon_without_premium_warns_honestly(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    ce.transport.premium = False
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    bid = next(b.id for b in entry.screen.buttons if b.id is not None)
    await ce.act(OWNER, "b.ic", str(bid))
    assert await ce.send(msg(OWNER, sticker=EMOJI, sticker_type="custom_emoji"))
    assert ce.premium.state.status == "stripped"
    assert "не показываются" in ce.last_text()
    assert "нет Telegram Premium" in ce.last_text()


async def test_rights_rechecked_on_every_write(ce: CEnv) -> None:
    admin = await ce.add(ADMIN, "admin", perms=["content.edit"])
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    bid = next(b.id for b in entry.screen.buttons if b.id is not None)
    # the right is taken away in the database; the cached context still has it
    await ce.db.raw("update users set perms = '[]'::jsonb where id = $1", admin.user_id)
    await ce.act(ADMIN, "b.en", f"{bid}.{entry.screen.version}")
    assert ce.toasts[-1] == "Нет прав"
    entry2 = ce.env.content.get_screen(sid)
    assert entry2 is not None and entry2.screen.version == entry.screen.version


async def test_two_admins_editing_the_same_screen(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.add(ADMIN, "admin", perms=["content.edit"])
    sid = home_id(ce)
    await ce.act(OWNER, "tx", f"{sid}.ru")
    await ce.act(ADMIN, "tx", f"{sid}.ru")
    assert await ce.send(msg(OWNER, "Текст владельца"))
    assert await ce.send(msg(ADMIN, "Текст админа"))
    assert "успел изменить другой админ" in ce.last_text()
    entry = ce.env.content.get_screen(sid)
    assert entry is not None and entry.text("ru").text == "Текст владельца"


async def test_preview_as_states(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    await ce.editor.add_button(
        sid,
        label={"ru": "Только истёкшим"},
        action="system:buy",
        visible_if={"sub": "expired"},
        expected_version=entry.screen.version,
        actor=None,
    )
    await ce.click(OWNER, SCREEN_PREVIEW, arg=f"{sid}.expired")
    assert "Только истёкшим" in [t for t, _ in ce.buttons()]
    noop = codec.encode(ACTIONS, "noop")
    assert dict(ce.buttons())["Только истёкшим"] == noop  # preview buttons are inert
    assert ce.last_text().startswith("👁 Так видит: истекла\n\n")
    await ce.click(OWNER, SCREEN_PREVIEW, arg=f"{sid}.active")
    assert "Только истёкшим" not in [t for t, _ in ce.buttons()]
    await ce.router.dispatch_callback(callback(OWNER, noop))
    assert ce.toasts[-1] == "Это предпросмотр"


async def test_custom_screen_create_rename_delete_undo(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    await ce.act(OWNER, "ns")
    assert await ce.send(msg(OWNER, "may_sale Акция мая"))
    created = ce.env.content.get_screen("may_sale")
    assert created is not None and created.screen.kind == "custom"
    assert "экран создан" in ce.last_text()
    await ce.press(OWNER, "✏️ Название")
    assert await ce.send(msg(OWNER, "Майская акция"))
    assert "«Майская акция»" in ce.last_text()
    await ce.press(OWNER, "🗑 Удалить")
    await ce.press(OWNER, "Да, удалить")
    assert ce.env.content.get_screen("may_sale") is None
    await ce.press(OWNER, "↩️ Отменить удаление")
    assert ce.env.content.get_screen("may_sale") is not None


async def test_history_lists_changes_with_undo(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    await ce.act(OWNER, "tx", f"{sid}.ru")
    await ce.send(msg(OWNER, "Один"))
    await ce.press(OWNER, "🕘 История")
    assert "Текст (ru)" in ce.last_text()
    await ce.press(OWNER, "↩️ Текст (ru)")
    assert ce.toasts[-1] == "↩️ Отменено"


async def test_system_button_card_hides_action_and_delete(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    entry = ce.env.content.get_screen("home")
    assert entry is not None
    sys_btn = next(b for b in entry.screen.buttons if b.system_key)
    await ce.click(OWNER, SCREEN_BUTTON, arg=str(sys_btn.id))
    texts = [t for t, _ in ce.buttons()]
    assert "⚡ Действие" not in texts and "🗑 Удалить" not in texts
    assert "Системная кнопка" in ce.last_text()


async def test_capture_cancel_and_other_commands(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    await ce.act(OWNER, "tx", f"{sid}.ru")
    assert await ce.send(msg(OWNER, "/cancel"))
    assert not ce.screens.capturing(msg(OWNER, "x"))
    await ce.act(OWNER, "tx", f"{sid}.ru")
    assert not await ce.send(msg(OWNER, "/start"))  # another command abandons the capture
    assert not await ce.send(msg(OWNER, "просто текст"))  # nothing is captured any more
    # a text that merely starts with «/» is a text
    await ce.act(OWNER, "tx", f"{sid}.ru")
    assert await ce.send(msg(OWNER, "/start — открыть меню"))
    entry = ce.env.content.get_screen(sid)
    assert entry is not None and entry.text("ru").text == "/start — открыть меню"


async def test_an_album_sets_one_picture_and_is_not_taken_for_a_conflict(ce: CEnv) -> None:
    import asyncio

    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    await ce.act(OWNER, "md", str(sid))
    album = [msg(OWNER, photo=True, message_id=800 + i, media_group_id="album-1") for i in range(3)]
    results = await asyncio.gather(*(ce.send(m) for m in album))
    assert results == [True, True, True]  # the whole album is the editor's (not a payment receipt)
    assert ce.downloads == ["PHOTO-ID"]  # one download, one stored file
    rows = await ce.db.raw("select count(*) as n from media")
    assert rows[0]["n"] == 1
    assert "успел изменить" not in ce.last_text()
    assert "⚡ Применено" in ce.last_text()
    entry = ce.env.content.get_screen(sid)
    assert entry is not None and entry.screen.media_id is not None


async def test_screen_card_counts_text_like_telegram(ce: CEnv) -> None:
    await ce.add(OWNER, "owner")
    sid = home_id(ce)
    entry = ce.env.content.get_screen(sid)
    assert entry is not None
    await ce.editor.set_text(sid, "ru", "🔥" * 10, None, expected_version=entry.screen.version, actor=None)
    await ce.click(OWNER, SCREEN_EDITOR, arg=str(sid))
    assert "ru (20/4096)" in ce.last_text()  # UTF-16 units, as the limits are checked


async def test_buttons_beyond_the_telegram_limit(ce: CEnv) -> None:
    from svbg.content.editing import MAX_BUTTONS, EditError

    await ce.add(OWNER, "owner")
    sid, _bid = await custom_button(ce)
    # legacy data over the limit (written before it existed): 12 rows × 8 + the two buttons
    rows = [(sid, r, c, {"ru": f"B{r}.{c}"}) for r in range(20, 32) for c in range(8)]
    for row in rows:
        await ce.db.raw(
            "insert into screen_buttons (screen_id, row, sort, label, action, enabled)"
            ' values ($1, $2, $3, $4::jsonb, \'{"type": "copy", "text": "x"}\'::jsonb, false)',
            *row,
        )
    await ce.env.content.reload()
    entry = ce.env.content.get_screen(sid)
    assert entry is not None and len(entry.screen.buttons) == 98
    with pytest.raises(EditError, match=f"больше {MAX_BUTTONS}"):
        await ce.editor.add_button(
            sid, label={"ru": "Ещё"}, action="copy:x", expected_version=entry.screen.version, actor=None
        )
    await ce.act(OWNER, "nb", str(sid))
    assert "кнопок" in (ce.toasts[-1] or "")

    # the «✏️» keyboard of the screen stays within Telegram's 100 buttons, the service row included
    assert await ce.screens.handle_edit_command(text_message(OWNER, "/edit"))
    await ce.click(OWNER, "promo_x")
    shown = ce.buttons()
    assert len(shown) <= 100
    assert [t for t, _ in shown[-3:]] == ["✏️ Экран", "➕ Кнопка", "👁 Как видит…"]

    # «🔘 Кнопки» pages through all of them
    await ce.click(OWNER, "ce.btns", arg=str(sid))
    first = [d for _, d in ce.buttons() if (d or "").startswith(f"v1:{SCREEN_BUTTON}:")]
    assert len(ce.buttons()) <= 100 and "Страница 1 из 2" in ce.last_text()
    await ce.press(OWNER, "➡️")
    second = [d for _, d in ce.buttons() if (d or "").startswith(f"v1:{SCREEN_BUTTON}:")]
    assert len(set(first) | set(second)) == 98 and not set(first) & set(second)


async def test_in_place_flow_from_the_service_row(ce: CEnv) -> None:
    import time as _time

    await ce.add(OWNER, "owner")
    await ce.add(USER, "user")
    assert await ce.screens.handle_edit_command(text_message(OWNER, "/edit"))
    await ce.click(OWNER, "home")
    # a content button opens its editor in the edit mode
    first_button = next(t for t, d in ce.buttons() if (d or "").startswith(f"v1:{SCREEN_BUTTON}"))
    await ce.press(OWNER, first_button)
    assert "🔘 Кнопка «" in ce.last_text()
    await ce.click(OWNER, "home")
    await ce.press(OWNER, "✏️ Экран")
    await ce.press(OWNER, "📝 Текст RU")
    started = _time.perf_counter()
    assert await ce.send(msg(OWNER, "Новая главная"))
    await ce.click(USER, "home")
    assert ce.last_text() == "Новая главная"
    assert _time.perf_counter() - started < 1.0  # saved, reloaded and seen by another user within 1 s
    await ce.click(OWNER, "home")
    await ce.press(OWNER, "➕ Кнопка")
    assert "шаг 1 из 2" in ce.last_text()
    await ce.press(OWNER, "Отмена")
    assert ce.toasts[-1] == "Отменено"


async def test_setup_entry_point_wires_everything(ce: CEnv, tmp_path: Any) -> None:
    from types import SimpleNamespace

    from aiogram import Router

    import svbg.tg.admin.content as module
    from tests.tg.ui.ui_harness import make_env

    scheduled: list[tuple[str, float]] = []

    class Sched:
        def every(self, name: str, interval: float, fn: Any, **_kw: Any) -> None:
            scheduled.append((name, interval))

    async def owner_ids() -> frozenset[int]:
        return frozenset({OWNER})

    async with make_env(ce.db, media_root=tmp_path) as env:
        deps = SimpleNamespace(
            db=ce.db,
            content=env.content,
            holder=SimpleNamespace(get=lambda: None),
            users=SimpleNamespace(owner_ids=owner_ids),
            scheduler=Sched(),
            settings=None,
        )
        router = await module.setup(env.router, deps)
        assert isinstance(router, Router)
        assert scheduled == [(module.PROBE_TASK, module.PROBE_INTERVAL_S)]
        await env.add(OWNER, "owner")
        await env.router.dispatch_callback(callback(OWNER, "v1:ce.home:o"))
        sent = env.transport.calls[-1]
        assert "Конструктор экранов" in str(getattr(sent, "text", ""))
