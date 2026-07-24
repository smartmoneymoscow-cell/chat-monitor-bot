"""
Конфигурация бота-мониторинга.

Скопируйте в config.py и заполните для локального запуска.
Для деплоя на Render — используйте Environment Variables.
"""

import os

# ── Telegram Bot (от @BotFather) ────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")

# ── Telethon API (для мониторинга чатов) ────────────────────
# Получить на https://my.telegram.org → API development tools
TELETHON_API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH", "")

# ── Хранилище ───────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "data")
