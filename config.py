"""Конфигурация — читает из Environment Variables (для Render)."""
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TELETHON_API_ID = int(os.environ.get("TELETHON_API_ID", "0"))
TELETHON_API_HASH = os.environ.get("TELETHON_API_HASH", "")
DATA_DIR = os.environ.get("DATA_DIR", "data")
