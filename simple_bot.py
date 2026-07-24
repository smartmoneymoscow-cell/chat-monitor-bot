"""Simplified bot to test Telegram connection."""
import os
import sys
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

# Health check
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

port = int(os.environ.get("PORT", 10000))
server = HTTPServer(("0.0.0.0", port), HealthHandler)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
log.info(f"Health check on port {port}")

# Import and run bot
import config
log.info(f"BOT_TOKEN: {config.BOT_TOKEN[:10]}...")
log.info(f"TELETHON_API_ID: {config.TELETHON_API_ID}")

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is working!")

async def post_init(application: Application):
    log.info("Bot initialized successfully")

app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
app.add_handler(CommandHandler("start", start))

log.info("Starting bot...")
try:
    app.run_polling(drop_pending_updates=True)
except Exception as e:
    log.error(f"Bot error: {e}", exc_info=True)
    sys.exit(1)
