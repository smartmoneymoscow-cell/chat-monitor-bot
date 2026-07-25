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
    MessageHandler, ConversationHandler, ContextTypes, filters,
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

# ── Состояния ConversationHandler ────────────────────────────
(
    STATE_MENU,
    STATE_ADD_PHONE,
    STATE_ADD_CODE,
    STATE_ADD_2FA,
    STATE_ADD_LABEL,
    STATE_ADD_CHATS,
    STATE_ADD_KEYWORDS,
    STATE_SET_NOTIFY,
) = range(8)

# ── Глобальные объекты ──────────────────────────────────────
_telethon_clients: dict[str, TelegramClient] = {}   # phone → client
_telethon_loop: asyncio.AbstractEventLoop | None = None
_telethon_thread: threading.Thread | None = None
_bot_app: Application | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None


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
    """Кнопки с аккаунтами пользователя."""
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
    """Безопасный edit_message_text — игнорирует 'Message is not modified'."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приветствие + главное меню."""
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
    return STATE_MENU


async def cb_back_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню."""
    ctx.user_data.clear()
    await cmd_start(update, ctx)
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  1. ДОБАВИТЬ АККАУНТ (Telethon-авторизация)
# ═══════════════════════════════════════════════════════════

async def cb_add_account_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: запрос номера телефона."""
    q = update.callback_query
    await q.answer()
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
    return STATE_ADD_PHONE


async def msg_add_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: отправка кода через Telethon."""
    phone = update.message.text.strip()
    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text(
            "❌ Неверный формат. Введите номер в формате <code>+79001234567</code>",
            parse_mode="HTML",
        )
        return STATE_ADD_PHONE

    ctx.user_data["phone"] = phone

    # Проверяем, есть ли уже сессия в БД
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
                # Обновляем строку сессии
                new_session = tc.session.save()
                storage.save_session_string(phone, new_session)
                await tc.disconnect()
                storage.add_account(update.effective_user.id, phone, label=me.first_name or phone)
                storage.update_account(update.effective_user.id, phone, {"session_ok": True})
                await update.message.reply_text(
                    f"✅ Аккаунт <b>{me.first_name}</b> ({phone}) уже авторизован!\n"
                    "Настройте чаты и ключевые слова.",
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
                return STATE_MENU
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
        ctx.user_data["phone_code_hash"] = sent.phone_code_hash
        ctx.user_data["tc_session_string"] = tc.session.save()
        await tc.disconnect()

        await update.message.reply_text(
            "📩 Код подтверждения отправлен в Telegram.\n"
            "Введите код из сообщения:",
            reply_markup=back_keyboard(),
        )
        return STATE_ADD_CODE

    except PhoneNumberInvalidError:
        await update.message.reply_text(
            "❌ Неверный номер телефона. Попробуйте ещё раз:",
            reply_markup=back_keyboard(),
        )
        return STATE_ADD_PHONE
    except Exception as e:
        log.error(f"Ошибка отправки кода: {e}")
        await update.message.reply_text(
            f"❌ Ошибка: {e}\nПопробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        return STATE_MENU


async def msg_add_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: ввод кода подтверждения."""
    code = update.message.text.strip().replace(" ", "")
    phone = ctx.user_data["phone"]
    session_string = ctx.user_data["tc_session_string"]
    phone_code_hash = ctx.user_data["phone_code_hash"]

    try:
        tc = TelegramClient(
            storage.StringSession(session_string),
            config.TELETHON_API_ID, config.TELETHON_API_HASH,
        )
        await tc.connect()
        await tc.sign_in(phone, code, phone_code_hash=phone_code_hash)
        me = await tc.get_me()

        final_session = tc.session.save()
        storage.save_session_string(phone, final_session)
        await tc.disconnect()

        storage.add_account(update.effective_user.id, phone, label=me.first_name or phone)
        storage.update_account(update.effective_user.id, phone, {"session_ok": True})

        await update.message.reply_text(
            f"✅ Авторизация успешна! Аккаунт: <b>{me.first_name}</b> ({phone})\n\n"
            "Теперь добавьте чаты для мониторинга и ключевые слова.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        ctx.user_data.clear()
        return STATE_MENU

    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 У вас включена двухфакторная аутентификация.\n"
            "Введите пароль 2FA:",
            reply_markup=back_keyboard(),
        )
        return STATE_ADD_2FA

    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text(
            "❌ Неверный или истёкший код. Введите код ещё раз:",
            reply_markup=back_keyboard(),
        )
        return STATE_ADD_CODE

    except Exception as e:
        log.error(f"Ошибка входа: {e}")
        await update.message.reply_text(
            f"❌ Ошибка: {e}",
            reply_markup=main_menu_keyboard(),
        )
        ctx.user_data.clear()
        return STATE_MENU


async def msg_add_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 4: ввод пароля 2FA."""
    password = update.message.text.strip()
    phone = ctx.user_data["phone"]
    session_string = ctx.user_data["tc_session_string"]

    try:
        tc = TelegramClient(
            storage.StringSession(session_string),
            config.TELETHON_API_ID, config.TELETHON_API_HASH,
        )
        await tc.connect()
        await tc.sign_in(password=password)
        me = await tc.get_me()

        final_session = tc.session.save()
        storage.save_session_string(phone, final_session)
        await tc.disconnect()

        storage.add_account(update.effective_user.id, phone, label=me.first_name or phone)
        storage.update_account(update.effective_user.id, phone, {"session_ok": True})

        await update.message.reply_text(
            f"✅ Авторизация успешна! Аккаунт: <b>{me.first_name}</b> ({phone})",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        ctx.user_data.clear()
        return STATE_MENU

    except Exception as e:
        await update.message.reply_text(
            f"❌ Неверный пароль или ошибка: {e}",
            reply_markup=back_keyboard(),
        )
        return STATE_ADD_2FA


# ═══════════════════════════════════════════════════════════
#  2. ДОБАВИТЬ ЧАТЫ
# ═══════════════════════════════════════════════════════════

async def cb_add_chats_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта → ввод чатов."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    if not data["accounts"]:
        await safe_edit(
            q,
            "❌ Сначала добавьте хотя бы один аккаунт.",
            reply_markup=main_menu_keyboard(),
        )
        return STATE_MENU

    await safe_edit(
        q,
        "💬 <b>Добавление чатов</b>\n\n"
        "Выберите аккаунт, для которого добавляете чаты:",
        parse_mode="HTML",
        reply_markup=accounts_keyboard(user_id, "select_acc_chats"),
    )
    return STATE_MENU


async def cb_select_acc_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Аккаунт выбран → запрос чатов."""
    q = update.callback_query
    await q.answer()
    phone = q.data.split(":")[1]
    ctx.user_data["edit_phone"] = phone

    acc = storage.get_account(update.effective_user.id, phone)
    current = ", ".join(acc["chats"]) if acc and acc["chats"] else "пусто"

    await safe_edit(
        q,
        f"💬 <b>Добавление чатов</b>\n"
        f"Аккаунт: <b>{phone}</b>\n"
        f"Текущие чаты: <code>{current}</code>\n\n"
        "Отправьте ID или @username чата.\n"
        "Можно несколько — каждый с новой строки.\n\n"
        "Примеры:\n"
        "<code>-1001234567890</code>\n"
        "<code>@chat_username</code>\n"
        "<code>https://t.me/chatname</code>\n\n"
        "Чтобы узнать ID чата — перешлите из него сообщение боту "
        "<a href='https://t.me/userinfobot'>@userinfobot</a>",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )
    return STATE_ADD_CHATS


async def msg_add_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохранение чатов."""
    phone = ctx.user_data.get("edit_phone")
    if not phone:
        await update.message.reply_text("❌ Ошибка. Начните заново /start")
        return STATE_MENU

    lines = update.message.text.strip().split("\n")
    added = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        chat_id = line
        if "t.me/" in line:
            chat_id = "@" + line.split("t.me/")[-1].strip("/")
        storage.add_chat(update.effective_user.id, phone, chat_id)
        added.append(chat_id)

    if added:
        await update.message.reply_text(
            f"✅ Добавлено чатов: <b>{len(added)}</b>\n"
            + "\n".join(f"  • <code>{c}</code>" for c in added),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ Не указано ни одного чата.",
            reply_markup=main_menu_keyboard(),
        )
    ctx.user_data.pop("edit_phone", None)
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  3. ДОБАВИТЬ КЛЮЧЕВЫЕ СЛОВА
# ═══════════════════════════════════════════════════════════

async def cb_add_keywords_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта → ввод слов."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    if not data["accounts"]:
        await safe_edit(
            q,
            "❌ Сначала добавьте хотя бы один аккаунт.",
            reply_markup=main_menu_keyboard(),
        )
        return STATE_MENU

    await safe_edit(
        q,
        "🔑 <b>Добавление ключевых слов</b>\n\n"
        "Выберите аккаунт:",
        parse_mode="HTML",
        reply_markup=accounts_keyboard(user_id, "select_acc_kw"),
    )
    return STATE_MENU


async def cb_select_acc_kw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Аккаунт выбран → запрос слов."""
    q = update.callback_query
    await q.answer()
    phone = q.data.split(":")[1]
    ctx.user_data["edit_phone"] = phone

    acc = storage.get_account(update.effective_user.id, phone)
    current = ", ".join(acc["keywords"]) if acc and acc["keywords"] else "пусто"

    await safe_edit(
        q,
        f"🔑 <b>Ключевые слова</b>\n"
        f"Аккаунт: <b>{phone}</b>\n"
        f"Текущие: <code>{current}</code>\n\n"
        "Отправьте ключевые слова — каждое с новой строки.\n"
        "Поиск регистронезависимый (по подстроке).\n\n"
        "Пример:\n"
        "<code>дизайнер\n"
        "дизайн интерьера\n"
        "ремонт квартиры\n"
        "прораб</code>",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )
    return STATE_ADD_KEYWORDS


async def msg_add_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохранение ключевых слов."""
    phone = ctx.user_data.get("edit_phone")
    if not phone:
        await update.message.reply_text("❌ Ошибка. Начните заново /start")
        return STATE_MENU

    lines = update.message.text.strip().split("\n")
    added = []
    for line in lines:
        kw = line.strip()
        if kw:
            storage.add_keyword(update.effective_user.id, phone, kw)
            added.append(kw)

    if added:
        await update.message.reply_text(
            f"✅ Добавлено слов: <b>{len(added)}</b>\n"
            + "\n".join(f"  • {k}" for k in added),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ Не указано ни одного слова.",
            reply_markup=main_menu_keyboard(),
        )
    ctx.user_data.pop("edit_phone", None)
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  4. КУДА СЛАТЬ УВЕДОМЛЕНИЯ
# ═══════════════════════════════════════════════════════════

def notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Мне в ЛС", callback_data="notify_me")],
        [InlineKeyboardButton("👥 В группу / канал", callback_data="notify_group")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_menu")],
    ])


async def cb_set_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор способа уведомлений."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    current = data.get("notify_chat_id", user_id)

    await safe_edit(
        q,
        f"🔔 <b>Куда слать уведомления</b>\n\n"
        f"Текущий получатель: <code>{current}</code>\n\n"
        "<b>📩 Мне в ЛС</b> — уведомления придут вам лично в диалог с ботом. "
        "Удобно, если мониторите для себя.\n\n"
        "<b>👥 В группу / канал</b> — уведомления придут в указанный чат. "
        "Нужен числовой ID группы или канала (узнать можно, переслав сообщение в @userinfobot).",
        parse_mode="HTML",
        reply_markup=notify_keyboard(),
    )
    return STATE_SET_NOTIFY


async def cb_notify_me(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Установить уведомления себе в ЛС."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id

    storage.set_notify(user_id, user_id)
    await safe_edit(
        q,
        f"✅ Уведомления будут приходить вам в ЛС (ID: <code>{user_id}</code>)",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    return STATE_MENU


async def cb_notify_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Запрос ID группы/канала."""
    q = update.callback_query
    await q.answer()
    await safe_edit(
        q,
        "👥 <b>Уведомления в группу / канал</b>\n\n"
        "Отправьте числовой ID группы или канала.\n\n"
        "<b>Как узнать ID:</b>\n"
        "1. Перешлите любое сообщение из чата боту @userinfobot\n"
        "2. Он ответит с числовой ID этого чата\n\n"
        "Пример: <code>-1001234567890</code>",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )
    return STATE_SET_NOTIFY


async def msg_set_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохранение получателя уведомлений."""
    text = update.message.text.strip().lower()

    if text == "me" or text == "мне":
        notify_id = update.effective_user.id
    else:
        try:
            notify_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Введите числовой ID или <code>me</code>",
                parse_mode="HTML",
            )
            return STATE_SET_NOTIFY

    storage.set_notify(update.effective_user.id, notify_id)
    await update.message.reply_text(
        f"✅ Уведомления будут приходить в: <code>{notify_id}</code>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  5. МОИ НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════

async def cb_my_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает текущие настройки."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
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
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📱 <b>{acc.get('label', '')}</b> ({acc['phone']})",
                f"   Статус: {status}",
                f"   {active}",
                f"   Чаты: <code>{chats}</code>",
                f"   Слова: <code>{kws}</code>",
            ])

    await safe_edit(
        q, "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  6. ЗАПУСТИТЬ / ОСТАНОВИТЬ МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════

async def cb_start_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Запуск мониторинга для выбранных аккаунтов."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    ready = []
    not_ready = []
    for acc in data["accounts"]:
        if acc.get("session_ok") and acc.get("chats") and acc.get("keywords"):
            ready.append(acc)
        else:
            not_ready.append(acc)

    if not ready:
        await safe_edit(
            q,
            "❌ Нет аккаунтов с полной настройкой.\n\n"
            "Нужно: авторизованный аккаунт + чаты + ключевые слова.",
            reply_markup=main_menu_keyboard(),
        )
        return STATE_MENU

    # Активируем
    for acc in data["accounts"]:
        if acc in ready:
            acc["active"] = True
    storage.save_user(user_id, data)

    # Перезапускаем Telethon-мониторинг
    restart_telethon_monitor()

    text = "✅ <b>Мониторинг запущен!</b>\n\n"
    for acc in ready:
        text += f"  🟢 {acc.get('label', acc['phone'])} — {len(acc['chats'])} чатов, {len(acc['keywords'])} слов\n"
    if not_ready:
        text += "\n⚠️ Не запущены (не настроены):\n"
        for acc in not_ready:
            text += f"  ⚪ {acc.get('label', acc['phone'])}\n"

    await safe_edit(q, text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    return STATE_MENU


async def cb_stop_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Остановка мониторинга."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    for acc in data["accounts"]:
        acc["active"] = False
    storage.save_user(user_id, data)

    restart_telethon_monitor()

    await safe_edit(
        q,
        "⏹ Мониторинг остановлен для всех аккаунтов.",
        reply_markup=main_menu_keyboard(),
    )
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  7. ПЕРЕСЫЛКА ИСТОРИИ ЗА МЕСЯЦ
# ═══════════════════════════════════════════════════════════

async def cb_forward_history_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для пересылки истории."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = storage.load_user(user_id)

    ready = [a for a in data["accounts"] if a.get("session_ok") and a.get("chats") and a.get("keywords")]
    if not ready:
        await safe_edit(
            q,
            "❌ Нет аккаунтов с полной настройкой.\n\n"
            "Нужно: авторизованный аккаунт + чаты + ключевые слова.",
            reply_markup=main_menu_keyboard(),
        )
        return STATE_MENU

    await safe_edit(
        q,
        "📜 <b>Пересылка истории за месяц</b>\n\n"
        "Выберите аккаунт. Бот найдёт все сообщения с ключевыми словами "
        "в отслеживаемых чатах за последние 30 дней и перешлёт уведомления.",
        parse_mode="HTML",
        reply_markup=accounts_keyboard(user_id, "select_acc_history"),
    )
    return STATE_MENU


async def cb_select_acc_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Запуск поиска истории для выбранного аккаунта."""
    q = update.callback_query
    await q.answer("⏳ Ищу сообщения...", show_alert=True)
    phone = q.data.split(":")[1]
    user_id = update.effective_user.id
    acc = storage.get_account(user_id, phone)

    if not acc or not acc.get("session_ok"):
        await safe_edit(q, "❌ Аккаунт не авторизован.", reply_markup=main_menu_keyboard())
        return STATE_MENU

    data = storage.load_user(user_id)
    notify_id = data.get("notify_chat_id", user_id)

    session_string = storage.get_session_string(phone)
    if not session_string:
        await safe_edit(q, "❌ Сессия не найдена.", reply_markup=main_menu_keyboard())
        return STATE_MENU

    await safe_edit(q, "⏳ <b>Ищу сообщения за последний месяц...</b>\nЭто может занять несколько минут.", parse_mode="HTML")

    tc = TelegramClient(
        storage.StringSession(session_string),
        config.TELETHON_API_ID, config.TELETHON_API_HASH,
    )
    found_total = 0
    errors = []

    try:
        await tc.start()
        me = await tc.get_me()
        log.info(f"📜 [{phone}] Поиск истории запущен ({me.first_name})")

        keywords = acc["keywords"]
        since = datetime.now(timezone.utc) - timedelta(days=30)

        for chat_id in acc["chats"]:
            try:
                try:
                    entity = int(chat_id)
                except ValueError:
                    entity = chat_id

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

                    chat_title = getattr(chat_entity, "title", "Личные сообщения")
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

                    await forward_alert(
                        notify_chat_id=notify_id,
                        chat_title=chat_title,
                        chat_username=chat_username,
                        author_name=author_name,
                        author_username=author_username,
                        author_id=author_id,
                        msg_text=msg.text or "",
                        msg_link=msg_link,
                        matched_keywords=matched,
                        msg_date=msg.date,
                    )

                found_total += chat_found
                log.info(f"📜 [{phone}] {chat_id}: {chat_found} совпадений")

            except Exception as e:
                errors.append(f"{chat_id}: {e}")
                log.error(f"📜 Ошибка в чате {chat_id}: {e}")

        await tc.disconnect()

    except Exception as e:
        errors.append(str(e))
        log.error(f"📜 Ошибка Telethon: {e}")
    finally:
        try:
            await tc.disconnect()
        except Exception:
            pass

    result = f"✅ <b>Поиск завершён!</b>\nНайдено: <b>{found_total}</b> сообщений за 30 дней."
    if errors:
        result += "\n\n⚠️ Ошибки:\n" + "\n".join(f"  • {e}" for e in errors[:5])
    await safe_edit(q, result, parse_mode="HTML", reply_markup=main_menu_keyboard())
    return STATE_MENU


# ═══════════════════════════════════════════════════════════
#  TELETHON-МОНИТОРИНГ (отдельный поток с отдельным event loop)
# ═══════════════════════════════════════════════════════════

def find_keywords(text: str, keywords: list[str]) -> list[str]:
    """Ищет ключевые слова в тексте (регистронезависимо)."""
    if not text:
        return []
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


def send_alert_sync(bot_token: str, notify_chat_id: int, text: str):
    """Синхронная отправка уведомления через requests (из Telethon-потока)."""
    import urllib.request
    import json as _json

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = _json.dumps({
        "chat_id": notify_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.error(f"Ошибка отправки уведомления: HTTP {resp.status}")
    except Exception as e:
        log.error(f"Ошибка отправки уведомления → {notify_chat_id}: {e}")


def format_alert(
    chat_title: str,
    chat_username: str | None,
    author_name: str,
    author_username: str | None,
    author_id: int | None,
    msg_text: str,
    msg_link: str | None,
    matched_keywords: list[str],
    msg_date: datetime,
) -> str:
    """Формирует текст уведомления."""
    moscow_tz = timezone(timedelta(hours=3))
    time_str = msg_date.astimezone(moscow_tz).strftime("%d.%m.%Y %H:%M MSK")

    text_preview = msg_text[:500] + "…" if len(msg_text) > 500 else msg_text

    lines = [
        "🔔 <b>Найдено ключевое слово!</b>",
        "",
        f"💬 <b>Чат:</b> {chat_title}",
    ]
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


async def telethon_client_task(phone: str, monitor_cfg: dict, loop: asyncio.AbstractEventLoop):
    """Запускает Telethon-клиент для одного аккаунта (в отдельном потоке)."""
    session_string = storage.get_session_string(phone)
    if not session_string:
        log.warning(f"Нет сессии для {phone}, пропускаем")
        return

    tc = TelegramClient(
        storage.StringSession(session_string),
        config.TELETHON_API_ID, config.TELETHON_API_HASH,
    )

    try:
        await tc.start()
        me = await tc.get_me()
        log.info(f"✅ Telethon {phone} ({me.first_name}) подключён")

        # Сохраняем обновлённую строку сессии
        storage.save_session_string(phone, tc.session.save())

        chats = monitor_cfg["chats"]
        keywords = monitor_cfg["keywords"]
        notify_id = monitor_cfg["notify_chat_id"]
        bot_token = config.BOT_TOKEN

        # Нормализуем ID чатов
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

                chat_title = getattr(chat_entity, "title", "Личные сообщения")
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

                alert_text = format_alert(
                    chat_title=chat_title,
                    chat_username=chat_username,
                    author_name=author_name,
                    author_username=author_username,
                    author_id=author_id,
                    msg_text=text,
                    msg_link=msg_link,
                    matched_keywords=found,
                    msg_date=event.message.date,
                )

                # Отправляем через requests (синхронно, из Telethon-потока)
                send_alert_sync(bot_token, notify_id, alert_text)
                log.info(f"✅ [{phone}] {chat_title}: {found}")

            except Exception as e:
                log.error(f"Ошибка обработки сообщения: {e}", exc_info=True)

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
    """Фоновый поток: запускает все Telethon-клиенты."""
    global _telethon_loop
    _telethon_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_telethon_loop)

    async def run_all():
        monitors = storage.get_all_active_monitors()
        if not monitors:
            log.info("Нет активных мониторингов")
            return

        tasks = []
        for m in monitors:
            log.info(f"Запуск мониторинга: {m['phone']} → {m['chats']}")
            tasks.append(telethon_client_task(m["phone"], m, _telethon_loop))

        await asyncio.gather(*tasks, return_exceptions=True)

    _telethon_loop.run_until_complete(run_all())


def restart_telethon_monitor():
    """Перезапускает Telethon-мониторинг (синхронно, из bot-потока)."""
    global _telethon_thread, _telethon_loop

    # Останавливаем старых клиентов
    for phone, tc in list(_telethon_clients.items()):
        try:
            if _telethon_loop and _telethon_loop.is_running():
                asyncio.run_coroutine_threadsafe(tc.disconnect(), _telethon_loop)
        except Exception:
            pass
    _telethon_clients.clear()

    # Запускаем новый поток
    _telethon_thread = threading.Thread(
        target=telethon_worker, daemon=True,
    )
    _telethon_thread.start()
    log.info("🔄 Telethon-мониторинг перезапущен")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    global _bot_app, _bot_loop

    if not config.BOT_TOKEN:
        print("❌ Задайте переменную окружения BOT_TOKEN!")
        sys.exit(1)
    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("❌ Задайте переменные окружения TELETHON_API_ID и TELETHON_API_HASH!")
        sys.exit(1)

    # ── Health check для Render Free ──
    start_health_server()
    log.info("Health check started on PORT=%s", os.environ.get("PORT", 10000))

    # ── post_init: запуск Telethon в отдельном потоке ──
    async def post_init(application: Application):
        global _bot_app, _bot_loop
        _bot_app = application
        _bot_loop = asyncio.get_event_loop()
        # Запускаем Telethon в отдельном потоке
        t = threading.Thread(target=telethon_worker, daemon=True)
        t.start()
        log.info("🔄 Telethon-мониторинг запущен в фоне")

    # ── Обработчик для неизвестных callback_query ──
    async def cb_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.callback_query:
            await update.callback_query.answer()
            await safe_edit(
                update.callback_query,
                "⚠️ Сессия устарела. Нажмите /start для перезапуска.",
            )

    # ── Строим приложение ──
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── ConversationHandler ──
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
        ],
        states={
            STATE_MENU: [
                CallbackQueryHandler(cb_add_account_start, pattern="^add_account$"),
                CallbackQueryHandler(cb_add_chats_start, pattern="^add_chats$"),
                CallbackQueryHandler(cb_add_keywords_start, pattern="^add_keywords$"),
                CallbackQueryHandler(cb_set_notify, pattern="^set_notify$"),
                CallbackQueryHandler(cb_my_settings, pattern="^my_settings$"),
                CallbackQueryHandler(cb_forward_history_start, pattern="^forward_history$"),
                CallbackQueryHandler(cb_select_acc_history, pattern=r"^select_acc_history:"),
                CallbackQueryHandler(cb_start_monitor, pattern="^start_monitor$"),
                CallbackQueryHandler(cb_stop_monitor, pattern="^stop_monitor$"),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
                CallbackQueryHandler(cb_select_acc_chats, pattern=r"^select_acc_chats:"),
                CallbackQueryHandler(cb_select_acc_kw, pattern=r"^select_acc_kw:"),
            ],
            STATE_ADD_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_add_phone),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
            ],
            STATE_ADD_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_add_code),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
            ],
            STATE_ADD_2FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_add_2fa),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
            ],
            STATE_ADD_CHATS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_add_chats),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
            ],
            STATE_ADD_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_add_keywords),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
            ],
            STATE_SET_NOTIFY: [
                CallbackQueryHandler(cb_notify_me, pattern="^notify_me$"),
                CallbackQueryHandler(cb_notify_group, pattern="^notify_group$"),
                CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_set_notify),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(cb_back_menu, pattern="^back_menu$"),
        ],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_unknown), group=1)

    # ── Запуск бота ──
    log.info("🚀 Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
