#!/bin/bash
# Start health check server first
python3 -c "
from healthcheck import start_health_server
start_health_server()
import time
time.sleep(999999)
" &

# Wait for health check to be ready
sleep 2

# Start the bot
exec python3 -u bot.py
