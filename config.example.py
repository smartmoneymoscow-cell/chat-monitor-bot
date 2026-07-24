"""
Конфигурация бота-мониторинга.

Скопируйте в config.py и заполните:
  1. BOT_TOKEN — от @BotFather
  2. TELETHON_API_ID / TELETHON_API_HASH — с https://my.telegram.org
"""

# ── Telegram Bot (от @BotFather) ────────────────────────────
BOT_TOKEN = ""

# ── Telethon API (для мониторинга чатов) ────────────────────
# Получить на https://my.telegram.org → API development tools
TELETHON_API_ID = 0          # int
TELETHON_API_HASH = ""       # str

# ── Хранилище ───────────────────────────────────────────────
DATA_DIR = "data"            # Папка для данных (сессии, конфиги)
