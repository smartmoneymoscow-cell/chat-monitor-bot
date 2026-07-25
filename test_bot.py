"""
Тесты для chat-monitor-bot.
22 теста покрывают: кнопки, состояния, storage, find_keywords, format_alert.
"""

import sys
import os
import asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

import config
import storage
import bot


def make_mock_update(callback_data=None, text=None, user_id=12345, is_callback=False):
    update = MagicMock()
    update.effective_user.id = user_id
    if is_callback:
        update.callback_query = MagicMock()
        update.callback_query.data = callback_data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message = MagicMock()
        update.message = None
    else:
        update.callback_query = None
        update.message = MagicMock()
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.message.chat_id = user_id
    return update


def make_mock_context():
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


# ═══════ КНОПКИ И МЕНЮ ═══════

async def test_start_command():
    update = make_mock_update(text="/start")
    await bot.cmd_start(update, make_mock_context())
    assert update.message.reply_text.called, "❌ /start не ответил"
    text = update.message.reply_text.call_args[0][0]
    assert "Бот-мониторинг" in text
    assert bot.get_state(12345)["state"] == bot.STATE_MENU
    print("✅ 1: /start → меню")


async def test_add_account_button():
    bot.clear_state(12345)
    update = make_mock_update(callback_data="add_account", is_callback=True)
    await bot.handle_callback(update, make_mock_context())
    assert update.callback_query.edit_message_text.called
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "телефон" in text.lower()
    assert bot.get_state(12345)["state"] == bot.STATE_ADD_PHONE
    print("✅ 2: Кнопка 'Добавить аккаунт'")


async def test_back_button():
    bot.set_state(12345, bot.STATE_ADD_PHONE)
    update = make_mock_update(callback_data="back_menu", is_callback=True)
    await bot.handle_callback(update, make_mock_context())
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Бот-мониторинг" in text
    assert bot.get_state(12345)["state"] == bot.STATE_MENU
    print("✅ 3: Кнопка 'Назад'")


async def test_unknown_button():
    bot.clear_state(12345)
    update = make_mock_update(callback_data="garbage", is_callback=True)
    await bot.handle_callback(update, make_mock_context())
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "устарела" in text.lower() or "start" in text.lower()
    print("✅ 4: Неизвестная кнопка → ошибка")


async def test_my_settings():
    bot.clear_state(12345)
    update = make_mock_update(callback_data="my_settings", is_callback=True)
    await bot.handle_callback(update, make_mock_context())
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Настройки" in text or "настройки" in text.lower()
    print("✅ 5: Кнопка 'Мои настройки'")


async def test_all_menu_buttons():
    buttons = ["add_account", "add_chats", "add_keywords", "set_notify",
               "my_settings", "forward_history", "start_monitor", "stop_monitor"]
    for btn in buttons:
        bot.clear_state(12345)
        update = make_mock_update(callback_data=btn, is_callback=True)
        await bot.handle_callback(update, make_mock_context())
        assert update.callback_query.answer.called, f"❌ '{btn}'"
        assert update.callback_query.edit_message_text.called, f"❌ '{btn}'"
    print("✅ 6: Все кнопки меню")


# ═══════ STATE MACHINE ═══════

async def test_text_routing():
    bot.set_state(12345, bot.STATE_ADD_PHONE)
    update = make_mock_update(text="bad")
    await bot.handle_text_message(update, make_mock_context())
    assert update.message.reply_text.called
    text = update.message.reply_text.call_args[0][0]
    assert "неверный" in text.lower() or "формат" in text.lower()
    print("✅ 7: Текст маршрутизируется по состоянию")


async def test_state_consistency():
    bot.clear_state(99999)
    assert bot.get_state(99999)["state"] == bot.STATE_MENU
    bot.set_state(99999, bot.STATE_ADD_PHONE, phone="+7999")
    assert bot.get_state(99999)["state"] == bot.STATE_ADD_PHONE
    assert bot.get_state(99999).get("phone") == "+7999"
    bot.clear_state(99999)
    assert bot.get_state(99999)["state"] == bot.STATE_MENU
    assert "phone" not in bot.get_state(99999)
    print("✅ 8: State machine консистентен")


async def test_set_state_no_duplicate():
    bot.set_state(88888, bot.STATE_MENU)
    state = bot.get_state(88888)
    extra = {k: v for k, v in state.items() if k != "state"}
    bot.set_state(88888, bot.STATE_ADD_PHONE, **extra)
    s2 = bot.get_state(88888)
    assert s2["state"] == bot.STATE_ADD_PHONE
    assert list(s2.keys()).count("state") == 1
    print("✅ 9: set_state без дублей")


async def test_ignore_text_in_menu():
    bot.clear_state(12345)
    update = make_mock_update(text="随便")
    await bot.handle_text_message(update, make_mock_context())
    assert not update.message.reply_text.called, "❌ Ответил на текст в MENU"
    print("✅ 10: Текст в MENU игнорируется")


# ═══════ ПОЛЬЗОВАТЕЛЬСКИЕ СЦЕНАРИИ ═══════

async def test_add_phone_no_crash():
    bot.clear_state(12345)
    await bot.handle_callback(make_mock_update(callback_data="add_account", is_callback=True), make_mock_context())
    assert bot.get_state(12345)["state"] == bot.STATE_ADD_PHONE

    # Невалидный номер
    await bot.handle_text_message(make_mock_update(text="abc"), make_mock_context())
    assert bot.get_state(12345)["state"] == bot.STATE_ADD_PHONE

    # Валидный номер — не крашится на set_state
    try:
        await bot.handle_text_message(make_mock_update(text="+79164732405"), make_mock_context())
    except TypeError as e:
        if "got multiple values" in str(e):
            assert False, f"РЕГРЕССИЯ: {e}"
        raise
    assert bot.get_state(12345)["state"] in (bot.STATE_ADD_CODE, bot.STATE_MENU)
    print("✅ 11: Ввод номера (regression)")


async def test_add_chats_flow():
    storage.add_account(12345, "+79001234567", label="T")
    bot.set_state(12345, bot.STATE_ADD_CHATS, edit_phone="+79001234567")
    await bot.handle_text_message(make_mock_update(text="-1001234567890\n@my_chat"), make_mock_context())
    text = make_mock_update.__class__  # just check no crash
    acc = storage.get_account(12345, "+79001234567")
    assert "-1001234567890" in acc["chats"], f"❌ {acc['chats']}"
    assert "@my_chat" in acc["chats"]
    assert bot.get_state(12345)["state"] == bot.STATE_MENU
    print("✅ 12: Добавление чатов")


async def test_add_keywords_flow():
    storage.add_account(12345, "+79009999999", label="T2")
    bot.set_state(12345, bot.STATE_ADD_KEYWORDS, edit_phone="+79009999999")
    await bot.handle_text_message(make_mock_update(text="дизайнер\nпрораб"), make_mock_context())
    acc = storage.get_account(12345, "+79009999999")
    assert "дизайнер" in acc["keywords"]
    assert "прораб" in acc["keywords"]
    print("✅ 13: Добавление ключевых слов")


async def test_set_notify_me():
    bot.set_state(12345, bot.STATE_SET_NOTIFY)
    await bot.handle_callback(make_mock_update(callback_data="notify_me", is_callback=True), make_mock_context())
    data = storage.load_user(12345)
    assert data["notify_chat_id"] == 12345
    assert bot.get_state(12345)["state"] == bot.STATE_MENU
    print("✅ 14: Уведомления 'себе'")


async def test_set_notify_group():
    bot.set_state(12345, bot.STATE_SET_NOTIFY)
    await bot.handle_callback(make_mock_update(callback_data="notify_group", is_callback=True), make_mock_context())
    assert bot.get_state(12345)["state"] == bot.STATE_SET_NOTIFY
    await bot.handle_text_message(make_mock_update(text="-1009876543210"), make_mock_context())
    data = storage.load_user(12345)
    assert data["notify_chat_id"] == -1009876543210
    print("✅ 15: Уведомления в группу")


async def test_chats_no_accounts():
    bot.clear_state(99997)
    storage.save_user(99997, {"user_id": 99997, "accounts": [], "notify_chat_id": 99997})
    await bot.handle_callback(make_mock_update(callback_data="add_chats", is_callback=True, user_id=99997), make_mock_context())
    text = make_mock_update(callback_data="x", is_callback=True, user_id=99997).callback_query.edit_message_text.call_args
    print("✅ 16: Чаты без аккаунта")


async def test_invite_link_rejected():
    """Тест: invite-ссылка не крашит, даёт понятный ответ."""
    storage.add_account(12345, "+79001234567", label="T")
    bot.set_state(12345, bot.STATE_ADD_CHATS, edit_phone="+79001234567")
    update = make_mock_update(text="https://t.me/+1Lomw30tNkxIMTFi")
    await bot.handle_text_message(update, make_mock_context())
    args = update.message.reply_text.call_args
    text = args[0][0] if args[0] else ""
    assert "Invite" in text or "invite" in text.lower() or "не подходят" in text.lower(), \
        f"❌ Не объяснило про invite-ссылки: {text[:150]}"
    # Чат не должен добавиться
    acc = storage.get_account(12345, "+79001234567")
    has_invite = any("+" in c for c in acc["chats"])
    assert not has_invite, f"❌ Invite-ссылка попала в чаты: {acc['chats']}"
    print("✅ 18b: Invite-ссылка отклонена")


async def test_empty_input():
    bot.set_state(12345, bot.STATE_ADD_CHATS, edit_phone="+79001234567")
    await bot.handle_text_message(make_mock_update(text="  \n  "), make_mock_context())
    print("✅ 17: Пустой ввод не крашит")


async def test_tme_normalization():
    storage.add_account(77777, "+79001111111", label="T")
    bot.set_state(77777, bot.STATE_ADD_CHATS, edit_phone="+79001111111")
    await bot.handle_text_message(make_mock_update(text="https://t.me/my_chat", user_id=77777), make_mock_context())
    acc = storage.get_account(77777, "+79001111111")
    assert "@my_chat" in acc["chats"]
    storage.remove_account(77777, "+79001111111")
    print("✅ 18: t.me ссылки")


# ═══════ УТИЛИТЫ ═══════

async def test_find_keywords():
    assert "дизайнер" in bot.find_keywords("Нужен ДИЗАЙНЕР", ["дизайнер"])
    assert bot.find_keywords("", ["x"]) == []
    assert bot.find_keywords("Привет", []) == []
    assert bot.find_keywords("Привет", ["дизайнер"]) == []
    r = bot.find_keywords("дизайнер и прораб", ["дизайнер", "прораб", "маляр"])
    assert len(r) == 2
    print("✅ 19: find_keywords")


async def test_format_alert():
    alert = bot.format_alert("Chat", "chat", "Иван", "ivan", 123, "text", "https://t.me/c/1", ["кw"], datetime(2026, 7, 25, 18, 0, 0, tzinfo=timezone.utc))
    for s in ["Chat", "@chat", "Иван", "@ivan", "123", "text", "https://t.me/c/1", "кw", "21:00"]:
        assert s in alert, f"❌ {s}"
    long = bot.format_alert("C", None, "A", None, None, "A"*1000, None, ["x"], datetime.now(timezone.utc))
    assert "…" in long
    print("✅ 20: format_alert")


# ═══════ STORAGE ═══════

async def test_storage_crud():
    uid = 55555
    data = storage.load_user(uid)
    assert data["accounts"] == []
    acc = storage.add_account(uid, "+79005555555", label="T")
    assert acc["phone"] == "+79005555555"
    storage.update_account(uid, "+79005555555", {"session_ok": True})
    assert storage.get_account(uid, "+79005555555")["session_ok"] is True
    storage.add_chat(uid, "+79005555555", "-100111")
    storage.add_chat(uid, "+79005555555", "@c2")
    assert len(storage.get_account(uid, "+79005555555")["chats"]) == 2
    storage.add_keyword(uid, "+79005555555", "Привет")
    storage.add_keyword(uid, "+79005555555", "Привет")
    assert len(storage.get_account(uid, "+79005555555")["keywords"]) == 1
    storage.remove_chat(uid, "+79005555555", "@c2")
    assert len(storage.get_account(uid, "+79005555555")["chats"]) == 1
    storage.remove_keyword(uid, "+79005555555", "Привет")
    assert len(storage.get_account(uid, "+79005555555")["keywords"]) == 0
    storage.set_notify(uid, -100999)
    assert storage.load_user(uid)["notify_chat_id"] == -100999
    storage.remove_account(uid, "+79005555555")
    assert len(storage.load_user(uid)["accounts"]) == 0
    print("✅ 21: SQLite CRUD")


async def test_session_strings():
    phone = "+79007777777"
    fake = "1B...fake...AA=="
    storage.save_session_string(phone, fake)
    assert storage.get_session_string(phone) == fake
    new = "2C...new...BB=="
    storage.save_session_string(phone, new)
    assert storage.get_session_string(phone) == new
    print("✅ 22: Session strings")


# ═══════ ЗАПУСК ═══════

async def run_all():
    tests = [
        test_start_command, test_add_account_button, test_back_button,
        test_unknown_button, test_my_settings, test_all_menu_buttons,
        test_text_routing, test_state_consistency, test_set_state_no_duplicate,
        test_ignore_text_in_menu, test_add_phone_no_crash,
        test_add_chats_flow, test_add_keywords_flow,
        test_invite_link_rejected,
        test_set_notify_me, test_set_notify_group,
        test_chats_no_accounts, test_empty_input, test_tme_normalization,
        test_find_keywords, test_format_alert,
        test_storage_crud, test_session_strings,
    ]
    passed = failed = 0
    for t in tests:
        try:
            await t()
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"Результат: {passed} passed, {failed} failed из {len(tests)}")
    print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!" if failed == 0 else "⚠️ ЕСТЬ ПАДЕНИЯ!")
    return failed == 0

if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
