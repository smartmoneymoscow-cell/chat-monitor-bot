#!/usr/bin/env python3
"""
Bot health monitor — checks if the bot is consuming Telegram updates.
If not, restarts the Render service and sends alert.
Run via cron every 15 minutes.
"""
import urllib.request
import json
import sys
import time

RENDER_KEY = "rnd_dHZaTklMCnKBx4eCSIQ39YJxwfFn"
SERVICE_ID = "srv-d95um6b4g1os73bkqe10"
ALERT_CHAT_ID = 820252069  # Vladimir

def api(url, method="GET", data=None, headers=None):
    h = headers or {}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        return {"error": str(e)}

def get_bot_token():
    req = urllib.request.Request(
        f"https://api.render.com/v1/services/{SERVICE_ID}/env-vars",
        headers={"Authorization": f"Bearer {RENDER_KEY}"}
    )
    envs = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return [e["envVar"]["value"] for e in envs if e["envVar"]["key"] == "BOT_TOKEN"][0]

def send_alert(bot_token, text):
    data = json.dumps({"chat_id": ALERT_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

def restart_service():
    req = urllib.request.Request(
        f"https://api.render.com/v1/services/{SERVICE_ID}/suspend",
        method="POST",
        headers={"Authorization": f"Bearer {RENDER_KEY}", "Content-Type": "application/json"},
        data=b"{}"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass
    time.sleep(5)
    req2 = urllib.request.Request(
        f"https://api.render.com/v1/services/{SERVICE_ID}/resume",
        method="POST",
        headers={"Authorization": f"Bearer {RENDER_KEY}", "Content-Type": "application/json"},
        data=b"{}"
    )
    try:
        urllib.request.urlopen(req2, timeout=15)
    except Exception:
        pass

def main():
    bot_token = get_bot_token()
    
    # 1. Clear old updates
    api(f"https://api.telegram.org/bot{bot_token}/getUpdates?offset=-1")
    time.sleep(2)
    
    # 2. Check health endpoint
    try:
        health = urllib.request.urlopen("https://nedvig-2.onrender.com/", timeout=30).read().decode()
        if health.strip() != "OK":
            send_alert(bot_token, f"⚠️ Healthcheck failed: {health}")
            restart_service()
            send_alert(bot_token, "🔄 Bot restarted (healthcheck failed)")
            return
    except Exception as e:
        send_alert(bot_token, f"⚠️ Healthcheck unreachable: {e}")
        restart_service()
        send_alert(bot_token, "🔄 Bot restarted (unreachable)")
        return
    
    # 3. Check if bot consumes updates
    # First check if there are old pending updates
    up = api(f"https://api.telegram.org/bot{bot_token}/getUpdates?limit=5")
    pending = up.get("result", [])
    
    if len(pending) > 2:
        # Bot has stale updates — it's stuck
        send_alert(bot_token, f"⚠️ Bot stuck: {len(pending)} pending updates not consumed")
        restart_service()
        time.sleep(20)
        send_alert(bot_token, "🔄 Bot restarted (stuck updates)")
        return
    
    # 4. All good
    print(f"OK — health=OK, pending={len(pending)}")

if __name__ == "__main__":
    main()
