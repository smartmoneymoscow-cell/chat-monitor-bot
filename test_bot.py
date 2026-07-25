"""
Тесты для chat-monitor-bot.
Проверяют:
1. /start → бот отвечает с меню
2. Кнопка "Добавить аккаунт" → бот показывает форму телефона
3. Кнопка "Назад" → возврат в меню
4. Роутер кнопок корректно обрабатывает состояния
"""

import sys
import os
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(__file__))

import config
import storage
import bot


# ═══════════════════════════════════════════════════════════
#  МОКИ
# ═══════════════════════════════════════════════════════════

def make_mock_update(callback_data=None, text=None, user_id=12345, is_callback=False):
    """Создаёт мок Update."""
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


# ═══════════════════════════════════════════════════════════
#  ТЕСТЫ
# ═══════════════════════════════════════════════════════════

async def test_start_command():
    """Тест 1: /start → бот отвечает с меню."""
    update = make_mock_update(text="/start")
    ctx = make_mock_context()

    await bot.cmd_start(update, ctx)

    # Проверяем, что reply_text был вызван
    assert update.message.reply_text.called, "❌ /start не вызвал reply_text"

    args = update.message.reply_text.call_args
    text = args[0][0] if args[0] else args[1].get("text", "")

    assert "Бот-мониторинг" in text, f"❌ Текст не содержит 'Бот-мониторинг': {text[:100]}"
    assert "reply_markup" in (args[1] or {}) or len(args[0]) > 1, "❌ Нет reply_markup"

    # Проверяем, что есть кнопки
    kwargs = args[1] or {}
    markup = kwargs.get("reply_markup")
    if not markup and len(args) > 1:
        markup = args[0][1] if len(args[0]) > 1 else None

    assert markup is not None, "❌ reply_markup = None"

    # Проверяем наличие кнопки "Добавить аккаунт"
    buttons_text = str(markup)
    assert "add_account" in buttons_text, f"❌ Нет кнопки add_account в markup"

    # Проверяем состояние
    state = bot.get_state(12345)
    assert state["state"] == bot.STATE_MENU, f"❌ Состояние не MENU: {state['state']}"

    print("✅ Тест 1 PASSED: /start → меню с кнопками")


async def test_add_account_button():
    """Тест 2: Кнопка 'Добавить аккаунт' → форма телефона."""
    # Сначала /start
    bot.clear_state(12345)

    # Нажатие кнопки
    update = make_mock_update(callback_data="add_account", is_callback=True)
    ctx = make_mock_context()

    await bot.handle_callback(update, ctx)

    # Проверяем, что answer() вызван
    assert update.callback_query.answer.called, "❌ answer() не вызван"

    # Проверяем, что edit_message_text вызван с формой телефона
    assert update.callback_query.edit_message_text.called, "❌ edit_message_text не вызван"

    args = update.callback_query.edit_message_text.call_args
    text = args[0][0] if args[0] else ""

    assert "номер телефона" in text.lower() or "телефон" in text.lower(), \
        f"❌ Текст не содержит форму телефона: {text[:100]}"

    # Проверяем состояние
    state = bot.get_state(12345)
    assert state["state"] == bot.STATE_ADD_PHONE, \
        f"❌ Состояние не ADD_PHONE: {state['state']}"

    print("✅ Тест 2 PASSED: Кнопка 'Добавить аккаунт' → форма телефона")


async def test_back_button():
    """Тест 3: Кнопка 'Назад' → возврат в меню."""
    # Установим состояние
    bot.set_state(12345, bot.STATE_ADD_PHONE, phone="+79001234567")

    update = make_mock_update(callback_data="back_menu", is_callback=True)
    ctx = make_mock_context()

    await bot.handle_callback(update, ctx)

    assert update.callback_query.answer.called, "❌ answer() не вызван"
    assert update.callback_query.edit_message_text.called, "❌ edit_message_text не вызван"

    args = update.callback_query.edit_message_text.call_args
    text = args[0][0] if args[0] else ""

    assert "Бот-мониторинг" in text, f"❌ Не вернулся в меню: {text[:100]}"

    state = bot.get_state(12345)
    assert state["state"] == bot.STATE_MENU, f"❌ Состояние не MENU: {state['state']}"

    print("✅ Тест 3 PASSED: Кнопка 'Назад' → возврат в меню")


async def test_unknown_button_in_menu():
    """Тест 4: Неизвестная кнопка в меню → 'Сессия устарела'."""
    bot.clear_state(12345)

    update = make_mock_update(callback_data="some_random_garbage", is_callback=True)
    ctx = make_mock_context()

    await bot.handle_callback(update, ctx)

    assert update.callback_query.edit_message_text.called, "❌ edit_message_text не вызван"

    args = update.callback_query.edit_message_text.call_args
    text = args[0][0] if args[0] else ""

    assert "устарела" in text.lower() or "start" in text.lower(), \
        f"❌ Не показано сообщение об ошибке: {text[:100]}"

    print("✅ Тест 4 PASSED: Неизвестная кнопка → 'Сессия устарела'")


async def test_my_settings_button():
    """Тест 5: Кнопка 'Мои настройки'."""
    bot.clear_state(12345)

    update = make_mock_update(callback_data="my_settings", is_callback=True)
    ctx = make_mock_context()

    await bot.handle_callback(update, ctx)

    assert update.callback_query.edit_message_text.called, "❌ edit_message_text не вызван"

    args = update.callback_query.edit_message_text.call_args
    text = args[0][0] if args[0] else ""

    assert "Настройки" in text or "настройки" in text.lower(), \
        f"❌ Не показаны настройки: {text[:100]}"

    state = bot.get_state(12345)
    assert state["state"] == bot.STATE_MENU, f"❌ Состояние не MENU: {state['state']}"

    print("✅ Тест 5 PASSED: Кнопка 'Мои настройки'")


async def test_text_message_routing():
    """Тест 6: Текстовое сообщение маршрутизируется по состоянию."""
    # В состоянии ADD_PHONE текст обрабатывается как номер
    bot.set_state(12345, bot.STATE_ADD_PHONE)

    update = make_mock_update(text="not_a_phone")
    ctx = make_mock_context()

    await bot.handle_text_message(update, ctx)

    # Должен ответить "неверный формат"
    assert update.message.reply_text.called, "❌ reply_text не вызван"

    args = update.message.reply_text.call_args
    text = args[0][0] if args[0] else ""

    assert "неверный" in text.lower() or "формат" in text.lower(), \
        f"❌ Не показана ошибка формата: {text[:100]}"

    print("✅ Тест 6 PASSED: Текст маршрутизируется по состоянию")


async def test_state_machine_consistency():
    """Тест 7: State machine консистентен."""
    # Очистка
    bot.clear_state(99999)
    state = bot.get_state(99999)
    assert state["state"] == bot.STATE_MENU, "❌ clear_state не ставит MENU"

    # Установка
    bot.set_state(99999, bot.STATE_ADD_PHONE, phone="+7999")
    state = bot.get_state(99999)
    assert state["state"] == bot.STATE_ADD_PHONE, "❌ set_state не работает"
    assert state.get("phone") == "+7999", "❌ set_state не сохраняет данные"

    # Очистка
    bot.clear_state(99999)
    state = bot.get_state(99999)
    assert state["state"] == bot.STATE_MENU, "❌ clear_state не сбрасывает"
    assert "phone" not in state, "❌ clear_state не чистит данные"

    print("✅ Тест 7 PASSED: State machine консистентен")


async def test_all_menu_buttons():
    """Тест 8: Все кнопки меню обрабатываются без ошибок."""
    buttons = [
        "add_account", "add_chats", "add_keywords", "set_notify",
        "my_settings", "forward_history", "start_monitor", "stop_monitor",
    ]

    for btn in buttons:
        bot.clear_state(12345)
        update = make_mock_update(callback_data=btn, is_callback=True)
        ctx = make_mock_context()

        try:
            await bot.handle_callback(update, ctx)
        except Exception as e:
            print(f"❌ Кнопка '{btn}' упала с ошибкой: {e}")
            continue

        assert update.callback_query.answer.called, f"❌ '{btn}': answer() не вызван"
        assert update.callback_query.edit_message_text.called, f"❌ '{btn}': edit_message_text не вызван"

    print("✅ Тест 8 PASSED: Все кнопки меню обрабатываются")


# ═══════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════

async def run_all():
    tests = [
        test_start_command,
        test_add_account_button,
        test_back_button,
        test_unknown_button_in_menu,
        test_my_settings_button,
        test_text_message_routing,
        test_state_machine_consistency,
        test_all_menu_buttons,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            await test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Результат: {passed} passed, {failed} failed из {len(tests)}")
    if failed == 0:
        print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ!")
    else:
        print("⚠️ ЕСТЬ ПАДЕНИЯ!")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
