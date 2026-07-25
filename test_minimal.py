"""Minimal test: does polling work at all?"""
import asyncio
import logging
import urllib.request
import json
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("test")

# Get token from Render
RENDER_KEY = "rnd_dHZaTklMCnKBx4eCSIQ39YJxwfFn"
req = urllib.request.Request(
    "https://api.render.com/v1/services/srv-d95um6b4g1os73bkqe10/env-vars",
    headers={"Authorization": f"Bearer {RENDER_KEY}"}
)
envs = json.loads(urllib.request.urlopen(req).read())
BOT_TOKEN = [e["envVar"]["value"] for e in envs if e["envVar"]["key"] == "BOT_TOKEN"][0]
API_ID = [e["envVar"]["value"] for e in envs if e["envVar"]["key"] == "TELETHON_API_ID"][0]
API_HASH = [e["envVar"]["value"] for e in envs if e["envVar"]["key"] == "TELETHON_API_HASH"][0]

import os
os.environ["BOT_TOKEN"] = BOT_TOKEN
os.environ["TELETHON_API_ID"] = API_ID
os.environ["TELETHON_API_HASH"] = API_HASH

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info(f"[/start] from {update.effective_user.id}")
    await update.message.reply_text("✅ Бот работает! Меню загружается...")

async def post_init(app):
    log.info("post_init OK")

def main():
    log.info(f"Starting test bot with token {BOT_TOKEN[:8]}...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    log.info("Calling run_polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
