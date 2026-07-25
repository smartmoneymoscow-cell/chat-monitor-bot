"""
Telegram-бот с inline-меню для настройки мониторинга чатов.

Архитектура:
  - python-telegram-bot — интерфейс (меню, кнопки) — основной event loop
  - Telethon — мониторинг чатов — ОТДЕЛЬНЫЙ поток с отдельным event loop
  - SQLite — хранение настроек и Telethon-сессий (персистентно на Render)

Запуск:
  1. Задайте переменные окружения BOT_TOKEN, TELETHON_API_ID, TELETHON_API_HASH
  2. pip install telethon python-telegram-bot pysocks
  3. python3 bot.py
"""

import os
import re
import sys
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telethon import TelegramClient, events
from telethon.errors import (
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    SessionPasswordNeededError, PhoneNumberInvalidError,
)

from telegram.error import BadRequest

import config
from healthcheck import start_health_server
import storage

# ── Логирование ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ── Состояния пользователей (в памяти) ──────────────────────
# user_id → {"state": str, ...data}
user_states: dict[int, dict] = {}

STATE_MENU = "menu"
STATE_ADD_PHONE = "add_phone"
STATE_ADD_CODE = "add_code"
STATE_ADD_2FA = "add_2fa"
STATE_ADD_CHATS = "add_chats"
STATE_ADD_KEYWORDS = "add_keywords"
STATE_SET_NOTIFY = "set_notify"

# ── Глобальные объекты ──────────────────────────────────────
_telethon_clients: dict[str, TelegramClient] = {}
_telethon_loop: asyncio.AbstractEventLoop | None = None
_telethon_thread: threading.Thread | None = None
_bot_app: Application | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None


def get_state(user_id: int) -> dict:
    """Получить состояние пользователя."""
    if user_id not in user_states:
        user_states[user_id] = {"state": STATE_MENU}
    return user_states[user_id]


def set_state(user_id: int, state: str, **kwargs):
    """Установить состояние пользователя."""
    s = get_state(user_id)
    s["state"] = state
    s.update(kwargs)


def clear_state(user_id: int):
    """Сбросить состояние в меню."""
    user_states[user_id] = {"state": STATE_MENU}


# ═══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton("💬 Добавить чаты", callback_data="add_chats")],
        [InlineKeyboardButton("🔑 Добавить ключевые слова", callback_data="add_keywords")],
        [InlineKeyboardButton("🔔 Куда слать уведомления", callback_data="set_notify")],
        [InlineKeyboardButton("📋 Мои настройки", callback_data="my_settings")],
        [InlineKeyboardButton("📜 История за месяц", callback_data="forward_history")],
        [InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="start_monitor")],
        [InlineKeyboardButton("⏹ Остановить мониторинг", callback_data="stop_monitor")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="back_menu")],
    ])


def accounts_keyboard(user_id: int, action: str) -> InlineKeyboardMarkup:
    data = storage.load_user(user_id)
    buttons = []
    for acc in data["accounts"]:
        label = acc.get("label", acc["phone"])
        status = "✅" if acc.get("session_ok") else "❌"
        buttons.append([
            InlineKeyboardButton(
                f"{status} {label} ({acc['phone']})",
                callback_data=f"{action}:{acc['phone']}",
            )
        ])
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data=f"confirm:{action}"),
            InlineKeyboardButton("❌ Нет", callback_data="back_menu"),
        ],
    ])


# ═══════════════════════════════════════════════════════════
#  /start  И  ГЛАВНОЕ МЕНЮ
# ═══════════════════════════════════════════════════════════

async def safe_edit(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_state(user_id)

    text = (
        "🤖 <b>Бот-мониторинг Telegram-чатов</b>\n\n"
        "Отслеживаю сообщения в выбранных чатах и пересылаю вам "
        "уведомления при совпадении с ключевыми словами.\n\n"
        "📌 <b>Как использовать:</b>\n"
        "1. Добавьте Telegram-аккаунт (через который состоите в чатах)\n"
        "2. Укажите чаты для мониторинга\n"
        "3. Настройте ключевые слова\n"
        "4. Укажите, куда слать уведомления\n"
        "5. Запустите мониторинг\n\n"
        "Выберите действие:"
    )
    if update.callback_query:
        await safe_edit(
            update.callback_query, text,
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )


# ═══════════════════════════════════════════════════════════
#  CALLBACK ROUTER (единый обработчик всех кнопок)
# ═══════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик всех callback_query."""
    q = update.callback_query
    if not q:
        return

    user_id = update.effective_user.id
    data = q.data
    state = get_state(user_id)

    log.info(f"Callback: user={user_id} data={data} state={state['state']}")

    # ── Глобальные кнопки (работают из любого состояния) ──
    if data == "back_menu":
        clear_state(user_id)
        await q.answer()
        await show_menu(q)
        return

    # ── Кнопки главного меню ──
    if state["state"] == STATE_MENU:
        if data == "add_account":
            await q.answer()
            await cb_add_account_start(q, user_id)
            return
        elif data == "add_chats":
            await q.answer()
            await cb_add_chats_start(q, user_id)
            return
        elif data == "add_keywords":
            await q.answer()
            await cb_add_keywords_start(q, user_id)
            return
        elif data == "set_notify":
            await q.answer()
            await cb_set_notify(q, user_id)
            return
        elif data == "my_settings":
            await q.answer()
            await cb_my_settings(q, user_id)
            return
        elif data == "forward_history":
            await q.answer()
            await cb_forward_history_start(q, user_id)
            return
        elif data == "start_monitor":
            await q.answer()
            await cb_start_monitor(q, user_id)
            return
        elif data == "stop_monitor":
            await q.answer()
            await cb_stop_monitor(q, user_id)
            return
        elif data.startswith("select_acc_chats:"):
            await q.answer()
            phone = data.split(":")[1]
            await cb_select_acc_chats(q, user_id, phone)
            return
        elif data.startswith("select_acc_kw:"):
            await q.answer()
            phone = data.split(":")[1]
            await cb_select_acc_kw(q, user_id, phone)
            return
        elif data.startswith("select_acc_history:"):
            await q.answer("⏳ Ищу сообщения...", show_alert=True)
            phone = data.split(":")[1]
            await cb_select_acc_history(q, user_id, phone)
            return

    # ── Кнопки состояния уведомлений ──
    if state["state"] == STATE_SET_NOTIFY:
        if data == "notify_me":
            await q.answer()
            storage.set_notify(user_id, user_id)
            clear_state(user_id)
            await safe_edit(
                q,
                f"✅ Уведомления будут приходить вам в ЛС (ID: <code>{user_id}</code>)",
                parse_mode="HTML", reply_markup=main_menu_keyboard(),
            )
            return
        elif data == "notify_group":
            await q.answer()
            await safe_edit(
                q,
                "👥 <b>Уведомления в группу / канал</b>\n\n"
                "Отправьте числовой ID группы или канала.\n\n"
                "Пример: <code>-1001234567890</code>",
                parse_mode="HTML", reply_markup=back_keyboard(),
            )
            return

    # ── Неизвестная кнопка ──
    await q.answer()
    await safe_edit(
        q,
        "⚠️ Сессия устарела. Нажмите /start для перезапуска.",
    )


async def show_menu(q):
    """Показать главное меню."""
    text = (
        "🤖 <b>Бот-мониторинг Telegram-чатов</b>\n\n"
        "Выберите действие:"
    )
    await safe_edit(q, text, parse_mode="HTML", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  1. ДОБАВИТЬ АККАУНТ
# ═══════════════════════════════════════════════════════════

async def cb_add_account_start(q, user_id: int):
    set_state(user_id, STATE_ADD_PHONE)
    await safe_edit(
        q,
        "📱 <b>Добавление аккаунта</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "<code>+79001234567</code>\n\n"
        "Это тот аккаунт, от имени которого бот будет мониторить чаты.\n"
        "На номер придёт код подтверждения.",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def msg_add_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_ADD_PHONE:
        return

    phone = update.message.text.strip()
    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text(
            "❌ Неверный формат. Введите номер в формате <code>+79001234567</code>",
            parse_mode="HTML",
        )
        return

    state["phone"] = phone

    # Проверяем сессию
    session_string = storage.get_session_string(phone)
    if session_string:
        try:
            tc = TelegramClient(
                storage.StringSession(session_string),
                config.TELETHON_API_ID, config.TELETHON_API_HASH,
            )
            await tc.connect()
            if await tc.is_user_authorized():
                me = await tc.get_me()
                new_session = tc.session.save()
                storage.save_session_string(phone, new_session)
                await tc.disconnect()
                storage.add_account(user_id, phone, label=me.first_name or phone)
                storage.update_account(user_id, phone, {"session_ok": True})
                clear_state(user_id)
                await update.message.reply_text(
                    f"✅ Аккаунт <b>{me.first_name}</b> ({phone}) уже авторизован!",
                    parse_mode="HTML", reply_markup=main_menu_keyboard(),
                )
                return
            await tc.disconnect()
        except Exception:
            pass

    # Запрашиваем код
    try:
        tc = TelegramClient(
            storage.StringSession(),
            config.TELETHON_API_ID, config.TELETHON_API_HASH,
        )
        await tc.connect()
        sent = await tc.send_code_request(phone)
        state["phone_code_hash"] = sent.phone_code_hash
        state["tc_session_string"] = tc.session.save()
        await tc.disconnect()

        set_state(user_id, STATE_ADD_CODE, **state)
        await update.message.reply_text(
            "📩 Код подтверждения отправлен в Telegram.\nВведите код:",
            reply_markup=back_keyboard(),
        )

    except PhoneNumberInvalidError:
        await update.message.reply_text(
            "❌ Неверный номер телефона.", reply_markup=back_keyboard(),
        )
    except Exception as e:
        log.error(f"Ошибка отправки кода: {e}")
        clear_state(user_id)
        await update.message.reply_text(
            f"❌ Ошибка: {e}", reply_markup=main_menu_keyboard(),
        )


async def msg_add_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_ADD_CODE:
        return

    code = update.message.text.strip().replace(" ", "")
    phone = state.get("phone")
    session_string = state.get("tc_session_string")
    phone_code_hash = state.get("phone_code_hash")

    try:
        tc = TelegramClient(
            storage.StringSession(session_string),
            config.TELETHON_API_ID, config.TELETHON_API_HASH,
        )
        await tc.connect()
        await tc.sign_in(phone, code, phone_code_hash=phone_code_hash)
        me = await tc.get_me()
        storage.save_session_string(phone, tc.session.save())
        await tc.disconnect()

        storage.add_account(user_id, phone, label=me.first_name or phone)
        storage.update_account(user_id, phone, {"session_ok": True})
        clear_state(user_id)

        await update.message.reply_text(
            f"✅ Авторизация успешна! Аккаунт: <b>{me.first_name}</b> ({phone})",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )

    except SessionPasswordNeededError:
        set_state(user_id, STATE_ADD_2FA, **state)
        await update.message.reply_text(
            "🔒 Введите пароль 2FA:", reply_markup=back_keyboard(),
        )
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text(
            "❌ Неверный код. Введите ещё раз:", reply_markup=back_keyboard(),
        )
    except Exception as e:
        log.error(f"Ошибка входа: {e}")
        clear_state(user_id)
        await update.message.reply_text(
            f"❌ Ошибка: {e}", reply_markup=main_menu_keyboard(),
        )


async def msg_add_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_ADD_2FA:
        return

    password = update.message.text.strip()
    phone = state.get("phone")
    session_string = state.get("tc_session_string")

    try:
        tc = TelegramClient(
            storage.StringSession(session_string),
            config.TELETHON_API_ID, config.TELETHON_API_HASH,
        )
        await tc.connect()
        await tc.sign_in(password=password)
        me = await tc.get_me()
        storage.save_session_string(phone, tc.session.save())
        await tc.disconnect()

        storage.add_account(user_id, phone, label=me.first_name or phone)
        storage.update_account(user_id, phone, {"session_ok": True})
        clear_state(user_id)

        await update.message.reply_text(
            f"✅ Авторизация успешна! Аккаунт: <b>{me.first_name}</b> ({phone})",
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: {e}", reply_markup=back_keyboard(),
        )


# ═══════════════════════════════════════════════════════════
#  2. ДОБАВИТЬ ЧАТЫ
# ═══════════════════════════════════════════════════════════

async def cb_add_chats_start(q, user_id: int):
    data = storage.load_user(user_id)
    if not data["accounts"]:
        await safe_edit(q, "❌ Сначала добавьте аккаунт.", reply_markup=main_menu_keyboard())
        return
    await safe_edit(
        q,
        "💬 <b>Добавление чатов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=accounts_keyboard(user_id, "select_acc_chats"),
    )


async def cb_select_acc_chats(q, user_id: int, phone: str):
    acc = storage.get_account(user_id, phone)
    current = ", ".join(acc["chats"]) if acc and acc["chats"] else "пусто"
    set_state(user_id, STATE_ADD_CHATS, edit_phone=phone)

    await safe_edit(
        q,
        f"💬 <b>Добавление чатов</b>\n"
        f"Аккаунт: <b>{phone}</b>\n"
        f"Текущие: <code>{current}</code>\n\n"
        "Отправьте ID или @username чата (каждый с новой строки).\n\n"
        "Примеры:\n<code>-1001234567890</code>\n<code>@chat_username</code>",
        parse_mode="HTML", reply_markup=back_keyboard(),
    )


async def msg_add_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_ADD_CHATS:
        return

    phone = state.get("edit_phone")
    if not phone:
        clear_state(user_id)
        await update.message.reply_text("❌ Ошибка. /start", reply_markup=main_menu_keyboard())
        return

    lines = update.message.text.strip().split("\n")
    added = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        chat_id = line
        if "t.me/" in line:
            chat_id = "@" + line.split("t.me/")[-1].strip("/")
        storage.add_chat(user_id, phone, chat_id)
        added.append(chat_id)

    clear_state(user_id)
    if added:
        await update.message.reply_text(
            f"✅ Добавлено чатов: <b>{len(added)}</b>\n" +
            "\n".join(f"  • <code>{c}</code>" for c in added),
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text("❌ Не указано ни одного чата.", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  3. ДОБАВИТЬ КЛЮЧЕВЫЕ СЛОВА
# ═══════════════════════════════════════════════════════════

async def cb_add_keywords_start(q, user_id: int):
    data = storage.load_user(user_id)
    if not data["accounts"]:
        await safe_edit(q, "❌ Сначала добавьте аккаунт.", reply_markup=main_menu_keyboard())
        return
    await safe_edit(
        q,
        "🔑 <b>Добавление ключевых слов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=accounts_keyboard(user_id, "select_acc_kw"),
    )


async def cb_select_acc_kw(q, user_id: int, phone: str):
    acc = storage.get_account(user_id, phone)
    current = ", ".join(acc["keywords"]) if acc and acc["keywords"] else "пусто"
    set_state(user_id, STATE_ADD_KEYWORDS, edit_phone=phone)

    await safe_edit(
        q,
        f"🔑 <b>Ключевые слова</b>\n"
        f"Аккаунт: <b>{phone}</b>\n"
        f"Текущие: <code>{current}</code>\n\n"
        "Отправьте ключевые слова (каждое с новой строки).\n"
        "Поиск регистронезависимый.",
        parse_mode="HTML", reply_markup=back_keyboard(),
    )


async def msg_add_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_ADD_KEYWORDS:
        return

    phone = state.get("edit_phone")
    if not phone:
        clear_state(user_id)
        await update.message.reply_text("❌ Ошибка. /start", reply_markup=main_menu_keyboard())
        return

    lines = update.message.text.strip().split("\n")
    added = []
    for line in lines:
        kw = line.strip()
        if kw:
            storage.add_keyword(user_id, phone, kw)
            added.append(kw)

    clear_state(user_id)
    if added:
        await update.message.reply_text(
            f"✅ Добавлено слов: <b>{len(added)}</b>\n" +
            "\n".join(f"  • {k}" for k in added),
            parse_mode="HTML", reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text("❌ Не указано ни одного слова.", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  4. КУДА СЛАТЬ УВЕДОМЛЕНИЯ
# ═══════════════════════════════════════════════════════════

def notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Мне в ЛС", callback_data="notify_me")],
        [InlineKeyboardButton("👥 В группу / канал", callback_data="notify_group")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_menu")],
    ])


async def cb_set_notify(q, user_id: int):
    data = storage.load_user(user_id)
    current = data.get("notify_chat_id", user_id)
    set_state(user_id, STATE_SET_NOTIFY)

    await safe_edit(
        q,
        f"🔔 <b>Куда слать уведомления</b>\n\n"
        f"Текущий: <code>{current}</code>\n\n"
        "📩 <b>Мне в ЛС</b> — уведомления в диалог с ботом.\n"
        "👥 <b>В группу / канал</b> — по числовому ID.",
        parse_mode="HTML", reply_markup=notify_keyboard(),
    )


async def msg_set_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_state(user_id)
    if state["state"] != STATE_SET_NOTIFY:
        return

    text = update.message.text.strip().lower()
    if text in ("me", "мне"):
        notify_id = user_id
    else:
        try:
            notify_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ Введите числовой ID или <code>me</code>", parse_mode="HTML")
            return

    storage.set_notify(user_id, notify_id)
    clear_state(user_id)
    await update.message.reply_text(
        f"✅ Уведомления → <code>{notify_id}</code>",
        parse_mode="HTML", reply_markup=main_menu_keyboard(),
    )


# ═══════════════════════════════════════════════════════════
#  5. МОИ НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════

async def cb_my_settings(q, user_id: int):
    data = storage.load_user(user_id)
    lines = [f"📋 <b>Настройки</b>", f"Уведомления → <code>{data.get('notify_chat_id', user_id)}</code>", ""]

    if not data["accounts"]:
        lines.append("Аккаунты: <i>не добавлены</i>")
    else:
        for acc in data["accounts"]:
            status = "✅ авторизован" if acc.get("session_ok") else "❌ не авторизован"
            active = "🟢 мониторинг вкл" if acc.get("active") else "⚪ мониторинг выкл"
            chats = ", ".join(acc["chats"]) if acc["chats"] else "—"
            kws = ", ".join(acc["keywords"]) if acc["keywords"] else "—"
            lines.extend([
                "━━━━━━━━━━━━━━━━━━━━",
                f"📱 <b>{acc.get('label', '')}</b> ({acc['phone']})",
                f"   Статус: {status}", f"   {active}",
                f"   Чаты: <code>{chats}</code>",
                f"   Слова: <code>{kws}</code>",
            ])

    await safe_edit(q, "\n".join(lines), parse_mode="HTML", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  6. ЗАПУСТИТЬ / ОСТАНОВИТЬ МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════

async def cb_start_monitor(q, user_id: int):
    data = storage.load_user(user_id)
    ready = [a for a in data["accounts"] if a.get("session_ok") and a.get("chats") and a.get("keywords")]
    not_ready = [a for a in data["accounts"] if a not in ready]

    if not ready:
        await safe_edit(q, "❌ Нет аккаунтов с полной настройкой.", reply_markup=main_menu_keyboard())
        return

    for acc in data["accounts"]:
        if acc in ready:
            acc["active"] = True
    storage.save_user(user_id, data)
    restart_telethon_monitor()

    text = "✅ <b>Мониторинг запущен!</b>\n\n"
    for acc in ready:
        text += f"  🟢 {acc.get('label', acc['phone'])} — {len(acc['chats'])} чатов, {len(acc['keywords'])} слов\n"
    if not_ready:
        text += "\n⚠️ Не запущены:\n"
        for acc in not_ready:
            text += f"  ⚪ {acc.get('label', acc['phone'])}\n"

    await safe_edit(q, text, parse_mode="HTML", reply_markup=main_menu_keyboard())


async def cb_stop_monitor(q, user_id: int):
    data = storage.load_user(user_id)
    for acc in data["accounts"]:
        acc["active"] = False
    storage.save_user(user_id, data)
    restart_telethon_monitor()
    await safe_edit(q, "⏹ Мониторинг остановлен.", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  7. ПЕРЕСЫЛКА ИСТОРИИ
# ═══════════════════════════════════════════════════════════

async def cb_forward_history_start(q, user_id: int):
    data = storage.load_user(user_id)
    ready = [a for a in data["accounts"] if a.get("session_ok") and a.get("chats") and a.get("keywords")]
    if not ready:
        await safe_edit(q, "❌ Нет аккаунтов с полной настройкой.", reply_markup=main_menu_keyboard())
        return

    await safe_edit(
        q,
        "📜 <b>Пересылка истории за месяц</b>\n\nВыберите аккаунт:",
        parse_mode="HTML", reply_markup=accounts_keyboard(user_id, "select_acc_history"),
    )


async def cb_select_acc_history(q, user_id: int, phone: str):
    acc = storage.get_account(user_id, phone)
    if not acc or not acc.get("session_ok"):
        await safe_edit(q, "❌ Аккаунт не авторизован.", reply_markup=main_menu_keyboard())
        return

    data = storage.load_user(user_id)
    notify_id = data.get("notify_chat_id", user_id)
    session_string = storage.get_session_string(phone)

    if not session_string:
        await safe_edit(q, "❌ Сессия не найдена.", reply_markup=main_menu_keyboard())
        return

    await safe_edit(q, "⏳ <b>Ищу сообщения за месяц...</b>", parse_mode="HTML")

    tc = TelegramClient(
        storage.StringSession(session_string),
        config.TELETHON_API_ID, config.TELETHON_API_HASH,
    )
    found_total = 0
    errors = []

    try:
        await tc.start()
        me = await tc.get_me()
        keywords = acc["keywords"]
        since = datetime.now(timezone.utc) - timedelta(days=30)

        for chat_id in acc["chats"]:
            try:
                entity = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
                chat_found = 0
                async for msg in tc.iter_messages(entity, offset_date=datetime.now(timezone.utc), reverse=False):
                    if msg.date < since:
                        break
                    if not msg.text:
                        continue
                    matched = find_keywords(msg.text, keywords)
                    if not matched:
                        continue
                    chat_found += 1
                    chat_entity = await msg.get_chat()
                    user_entity = await msg.get_sender()
                    chat_title = getattr(chat_entity, "title", "ЛС")
                    chat_username = getattr(chat_entity, "username", None)
                    author_name = "Неизвестный"
                    author_username = None
                    author_id = None
                    if user_entity:
                        parts = []
                        if getattr(user_entity, "first_name", None):
                            parts.append(user_entity.first_name)
                        if getattr(user_entity, "last_name", None):
                            parts.append(user_entity.last_name)
                        author_name = " ".join(parts) if parts else "Неизвестный"
                        author_username = getattr(user_entity, "username", None)
                        author_id = getattr(user_entity, "id", None)
                    msg_link = None
                    if chat_username:
                        msg_link = f"https://t.me/{chat_username}/{msg.id}"
                    elif msg.chat_id:
                        raw = str(msg.chat_id)
                        if raw.startswith("-100"):
                            raw = raw[4:]
                        msg_link = f"https://t.me/c/{raw}/{msg.id}"
                    alert = format_alert(chat_title, chat_username, author_name, author_username, author_id, msg.text, msg_link, matched, msg.date)
                    send_alert_sync(config.BOT_TOKEN, notify_id, alert)
                found_total += chat_found
            except Exception as e:
                errors.append(f"{chat_id}: {e}")
        await tc.disconnect()
    except Exception as e:
        errors.append(str(e))
    finally:
        try:
            await tc.disconnect()
        except Exception:
            pass

    result = f"✅ Найдено: <b>{found_total}</b> сообщений за 30 дней."
    if errors:
        result += "\n\n⚠️ Ошибки:\n" + "\n".join(f"  • {e}" for e in errors[:5])
    await safe_edit(q, result, parse_mode="HTML", reply_markup=main_menu_keyboard())


# ═══════════════════════════════════════════════════════════
#  TELETHON-МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════

def find_keywords(text: str, keywords: list[str]) -> list[str]:
    if not text:
        return []
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def send_alert_sync(bot_token: str, notify_chat_id: int, text: str):
    import urllib.request
    import json as _json
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = _json.dumps({
        "chat_id": notify_chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.error(f"Ошибка отправки: HTTP {resp.status}")
    except Exception as e:
        log.error(f"Ошибка отправки → {notify_chat_id}: {e}")


def format_alert(chat_title, chat_username, author_name, author_username, author_id, msg_text, msg_link, matched_keywords, msg_date):
    moscow_tz = timezone(timedelta(hours=3))
    time_str = msg_date.astimezone(moscow_tz).strftime("%d.%m.%Y %H:%M MSK")
    text_preview = msg_text[:500] + "…" if len(msg_text) > 500 else msg_text
    lines = ["🔔 <b>Найдено ключевое слово!</b>", "", f"💬 <b>Чат:</b> {chat_title}"]
    if chat_username:
        lines.append(f"🔗 <b>@{chat_username}</b>")
    lines.append(f"👤 <b>Автор:</b> {author_name}")
    if author_username:
        lines.append(f"🔗 @{author_username}")
    if author_id:
        lines.append(f"🆔 <code>{author_id}</code>")
    lines.append(f"🕐 {time_str}")
    lines.append(f"🎯 <b>Совпадение:</b> {', '.join(matched_keywords)}")
    if msg_link:
        lines.append(f"🔗 <a href=\"{msg_link}\">Открыть сообщение</a>")
    lines.extend(["", "📝 <b>Текст:</b>", f"<blockquote>{text_preview}</blockquote>"])
    return "\n".join(lines)


async def telethon_client_task(phone, monitor_cfg, loop):
    session_string = storage.get_session_string(phone)
    if not session_string:
        log.warning(f"Нет сессии для {phone}")
        return
    tc = TelegramClient(storage.StringSession(session_string), config.TELETHON_API_ID, config.TELETHON_API_HASH)
    try:
        await tc.start()
        me = await tc.get_me()
        log.info(f"✅ Telethon {phone} ({me.first_name})")
        storage.save_session_string(phone, tc.session.save())
        chats = monitor_cfg["chats"]
        keywords = monitor_cfg["keywords"]
        notify_id = monitor_cfg["notify_chat_id"]
        bot_token = config.BOT_TOKEN
        resolved_chats = set()
        for c in chats:
            try:
                resolved_chats.add(int(c))
            except ValueError:
                resolved_chats.add(c)

        @tc.on(events.NewMessage(chats=list(resolved_chats) if resolved_chats else None))
        async def on_new_message(event):
            try:
                text = event.message.text or ""
                found = find_keywords(text, keywords)
                if not found:
                    return
                chat_entity = await event.get_chat()
                user_entity = await event.get_sender()
                chat_title = getattr(chat_entity, "title", "ЛС")
                chat_username = getattr(chat_entity, "username", None)
                author_name = "Неизвестный"
                author_username = None
                author_id = None
                if user_entity:
                    parts = []
                    if hasattr(user_entity, "first_name") and user_entity.first_name:
                        parts.append(user_entity.first_name)
                    if hasattr(user_entity, "last_name") and user_entity.last_name:
                        parts.append(user_entity.last_name)
                    author_name = " ".join(parts) if parts else "Неизвестный"
                    author_username = getattr(user_entity, "username", None)
                    author_id = getattr(user_entity, "id", None)
                msg_link = None
                if chat_username:
                    msg_link = f"https://t.me/{chat_username}/{event.message.id}"
                elif event.chat_id:
                    raw = str(event.chat_id)
                    if raw.startswith("-100"):
                        raw = raw[4:]
                    msg_link = f"https://t.me/c/{raw}/{event.message.id}"
                alert = format_alert(chat_title, chat_username, author_name, author_username, author_id, text, msg_link, found, event.message.date)
                send_alert_sync(bot_token, notify_id, alert)
                log.info(f"✅ [{phone}] {chat_title}: {found}")
            except Exception as e:
                log.error(f"Ошибка: {e}", exc_info=True)

        _telethon_clients[phone] = tc
        await tc.run_until_disconnected()
    except Exception as e:
        log.error(f"Ошибка Telethon {phone}: {e}")
    finally:
        try:
            await tc.disconnect()
        except Exception:
            pass
        _telethon_clients.pop(phone, None)


def telethon_worker():
    global _telethon_loop
    _telethon_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_telethon_loop)
    async def run_all():
        monitors = storage.get_all_active_monitors()
        if not monitors:
            log.info("Нет активных мониторингов")
            return
        tasks = [telethon_client_task(m["phone"], m, _telethon_loop) for m in monitors]
        await asyncio.gather(*tasks, return_exceptions=True)
    _telethon_loop.run_until_complete(run_all())


def restart_telethon_monitor():
    global _telethon_thread, _telethon_loop
    for phone, tc in list(_telethon_clients.items()):
        try:
            if _telethon_loop and _telethon_loop.is_running():
                asyncio.run_coroutine_threadsafe(tc.disconnect(), _telethon_loop)
        except Exception:
            pass
    _telethon_clients.clear()
    _telethon_thread = threading.Thread(target=telethon_worker, daemon=True)
    _telethon_thread.start()
    log.info("🔄 Telethon перезапущен")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    global _bot_app, _bot_loop

    if not config.BOT_TOKEN:
        print("❌ Задайте BOT_TOKEN!")
        sys.exit(1)
    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("❌ Задайте TELETHON_API_ID и TELETHON_API_HASH!")
        sys.exit(1)

    start_health_server()
    log.info("Health check on PORT=%s", os.environ.get("PORT", 10000))

    async def post_init(application):
        global _bot_app, _bot_loop
        _bot_app = application
        _bot_loop = asyncio.get_event_loop()
        t = threading.Thread(target=telethon_worker, daemon=True)
        t.start()
        log.info("🔄 Telethon запущен")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Хэндлеры ──
    app.add_handler(CommandHandler("start", cmd_start))

    # Текстовые сообщения (номер телефона, код, 2FA, чаты, слова, уведомления)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Все callback query — единый обработчик
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("🚀 Бот запущен!")
    app.run_polling(drop_pending_updates=True)


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Маршрутизатор текстовых сообщений по состоянию."""
    user_id = update.effective_user.id
    state = get_state(user_id)

    if state["state"] == STATE_ADD_PHONE:
        await msg_add_phone(update, ctx)
    elif state["state"] == STATE_ADD_CODE:
        await msg_add_code(update, ctx)
    elif state["state"] == STATE_ADD_2FA:
        await msg_add_2fa(update, ctx)
    elif state["state"] == STATE_ADD_CHATS:
        await msg_add_chats(update, ctx)
    elif state["state"] == STATE_ADD_KEYWORDS:
        await msg_add_keywords(update, ctx)
    elif state["state"] == STATE_SET_NOTIFY:
        await msg_set_notify(update, ctx)
    # В состоянии MENU текстовые сообщения игнорируются


if __name__ == "__main__":
    main()
