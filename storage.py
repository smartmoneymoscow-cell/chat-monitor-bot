"""
Хранилище пользовательских настроек.

Поддерживает два бэкенда:
  - SQLite (по умолчанию) — персистентно, переживает рестарты Render
  - JSON (legacy) — если задан STORAGE_BACKEND=json

Telethon-сессии хранятся как строки (StringSession), а не файлы.
"""

import os
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import config

# ── StringSession для Telethon ──────────────────────────────
from telethon.sessions import StringSession

# ── Выбор бэкенда ──────────────────────────────────────────
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "sqlite").lower()
DB_PATH = os.environ.get("DB_PATH", os.path.join(config.DATA_DIR, "bot.db"))

_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════
#  SQLite БЭКЕНД
# ═══════════════════════════════════════════════════════════

_db_conn: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    """Получить соединение с SQLite (создаёт таблицы при первом вызове)."""
    global _db_conn
    if _db_conn is None:
        os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _init_db(_db_conn)
    return _db_conn


def _init_db(conn: sqlite3.Connection):
    """Создаёт таблицы, если их нет."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            session_string TEXT NOT NULL
        );
    """)
    conn.commit()


# ── SQLite: пользователи ───────────────────────────────────

def _sqlite_load_user(user_id: int) -> dict:
    db = _get_db()
    row = db.execute("SELECT data FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return json.loads(row["data"])
    return {
        "user_id": user_id,
        "accounts": [],
        "notify_chat_id": user_id,
    }


def _sqlite_save_user(user_id: int, data: dict):
    db = _get_db()
    with _lock:
        db.execute(
            "INSERT INTO users (user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = ?",
            (user_id, json.dumps(data, ensure_ascii=False), json.dumps(data, ensure_ascii=False)),
        )
        db.commit()


def _sqlite_get_all_active_monitors() -> list[dict]:
    db = _get_db()
    monitors = []
    for row in db.execute("SELECT data FROM users"):
        data = json.loads(row["data"])
        user_id = data["user_id"]
        notify = data.get("notify_chat_id", user_id)
        for acc in data.get("accounts", []):
            if acc.get("active") and acc.get("session_ok") and acc.get("chats") and acc.get("keywords"):
                monitors.append({
                    "user_id": user_id,
                    "phone": acc["phone"],
                    "label": acc.get("label", acc["phone"]),
                    "chats": acc["chats"],
                    "keywords": acc["keywords"],
                    "notify_chat_id": notify,
                })
    return monitors


# ── SQLite: Telethon-сессии ────────────────────────────────

def _sqlite_save_session(phone: str, session_string: str):
    db = _get_db()
    with _lock:
        db.execute(
            "INSERT INTO sessions (phone, session_string) VALUES (?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET session_string = ?",
            (phone, session_string, session_string),
        )
        db.commit()


def _sqlite_get_session(phone: str) -> str | None:
    db = _get_db()
    row = db.execute("SELECT session_string FROM sessions WHERE phone = ?", (phone,)).fetchone()
    return row["session_string"] if row else None


# ═══════════════════════════════════════════════════════════
#  JSON БЭКЕНД (legacy)
# ═══════════════════════════════════════════════════════════

def _user_path(user_id: int) -> Path:
    return Path(config.DATA_DIR) / f"user_{user_id}.json"


def _json_load_user(user_id: int) -> dict:
    path = _user_path(user_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "user_id": user_id,
        "accounts": [],
        "notify_chat_id": user_id,
    }


def _json_save_user(user_id: int, data: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = _user_path(user_id)
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _json_get_all_active_monitors() -> list[dict]:
    monitors = []
    data_dir = Path(config.DATA_DIR)
    if not data_dir.exists():
        return monitors
    for f in data_dir.glob("user_*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            user_id = data["user_id"]
            notify = data.get("notify_chat_id", user_id)
            for acc in data.get("accounts", []):
                if acc.get("active") and acc.get("session_ok") and acc.get("chats") and acc.get("keywords"):
                    monitors.append({
                        "user_id": user_id,
                        "phone": acc["phone"],
                        "label": acc.get("label", acc["phone"]),
                        "chats": acc["chats"],
                        "keywords": acc["keywords"],
                        "notify_chat_id": notify,
                    })
        except Exception:
            continue
    return monitors


def _json_save_session(phone: str, session_string: str):
    """Сохраняет строку сессии в файл."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = Path(config.DATA_DIR) / f"session_{phone.replace('+', 'plus')}.session_str"
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            f.write(session_string)


def _json_get_session(phone: str) -> str | None:
    """Читает строку сессии из файла."""
    path = Path(config.DATA_DIR) / f"session_{phone.replace('+', 'plus')}.session_str"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    # Fallback: проверяем старый файл .session (для миграции)
    old_path = Path(config.DATA_DIR) / f"session_{phone.replace('+', 'plus')}.session"
    if old_path.exists():
        return None  # Старый формат, нужна переавторизация
    return None


# ═══════════════════════════════════════════════════════════
#  ПУБЛИЧНЫЙ API (автоматически выбирает бэкенд)
# ═══════════════════════════════════════════════════════════

def load_user(user_id: int) -> dict:
    if STORAGE_BACKEND == "sqlite":
        return _sqlite_load_user(user_id)
    return _json_load_user(user_id)


def save_user(user_id: int, data: dict):
    if STORAGE_BACKEND == "sqlite":
        _sqlite_save_user(user_id, data)
    else:
        _json_save_user(user_id, data)


def get_account(user_id: int, phone: str) -> dict | None:
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            return acc
    return None


def add_account(user_id: int, phone: str, label: str = "") -> dict:
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            return acc
    acc = {
        "phone": phone,
        "label": label or phone,
        "chats": [],
        "keywords": [],
        "active": False,
        "session_ok": False,
    }
    data["accounts"].append(acc)
    save_user(user_id, data)
    return acc


def update_account(user_id: int, phone: str, updates: dict):
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc.update(updates)
            break
    save_user(user_id, data)


def remove_account(user_id: int, phone: str):
    data = load_user(user_id)
    data["accounts"] = [a for a in data["accounts"] if a["phone"] != phone]
    save_user(user_id, data)


def add_chat(user_id: int, phone: str, chat_id: str):
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            if chat_id not in acc["chats"]:
                acc["chats"].append(chat_id)
            break
    save_user(user_id, data)


def remove_chat(user_id: int, phone: str, chat_id: str):
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc["chats"] = [c for c in acc["chats"] if c != chat_id]
            break
    save_user(user_id, data)


def add_keyword(user_id: int, phone: str, keyword: str):
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            kw = keyword.strip().lower()
            if kw and kw not in [k.lower() for k in acc["keywords"]]:
                acc["keywords"].append(keyword.strip())
            break
    save_user(user_id, data)


def remove_keyword(user_id: int, phone: str, keyword: str):
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc["keywords"] = [k for k in acc["keywords"] if k.lower() != keyword.lower()]
            break
    save_user(user_id, data)


def set_notify(user_id: int, chat_id: int):
    data = load_user(user_id)
    data["notify_chat_id"] = chat_id
    save_user(user_id, data)


def get_all_active_monitors() -> list[dict]:
    if STORAGE_BACKEND == "sqlite":
        return _sqlite_get_all_active_monitors()
    return _json_get_all_active_monitors()


# ── Telethon-сессии (строки, не файлы) ─────────────────────

def save_session_string(phone: str, session_string: str):
    """Сохраняет строку Telethon-сессии."""
    if STORAGE_BACKEND == "sqlite":
        _sqlite_save_session(phone, session_string)
    else:
        _json_save_session(phone, session_string)


def get_session_string(phone: str) -> str | None:
    """Получает строку Telethon-сессии."""
    if STORAGE_BACKEND == "sqlite":
        return _sqlite_get_session(phone)
    return _json_get_session(phone)
