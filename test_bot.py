"""Simple test bot to verify Render deployment works."""
import os
import sys
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

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

# Keep running
import time
while True:
    time.sleep(60)
    log.info("Bot is alive")
