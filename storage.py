"""
Хранилище пользовательских настроек (PostgreSQL).

Таблицы:
  - users — настройки пользователя (notify_chat_id)
  - accounts — Telethon-аккаунты (phone, label, session_string, active, etc.)
  - chats — чаты для мониторинга
  - keywords — ключевые слова
"""

import os
import json
import logging
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

import config

log = logging.getLogger("storage")

# ── Подключение ─────────────────────────────────────────────

def _get_conn():
    """Подключение к PostgreSQL."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    # Render даёт postgres://, psycopg2 хочет postgresql://
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(dsn, cursor_factory=RealDictCursor)


def init_db():
    """Создаёт таблицы если не существуют."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    notify_chat_id BIGINT NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    phone TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    session_string TEXT DEFAULT '',
                    active BOOLEAN DEFAULT FALSE,
                    session_ok BOOLEAN DEFAULT FALSE,
                    UNIQUE(user_id, phone)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    chat_id TEXT NOT NULL,
                    UNIQUE(account_id, chat_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS keywords (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    keyword TEXT NOT NULL,
                    UNIQUE(account_id, keyword)
                );
            """)
            conn.commit()
            log.info("✅ Таблицы PostgreSQL созданы/проверены")
    finally:
        conn.close()


# ── Пользователи ────────────────────────────────────────────

def load_user(user_id: int) -> dict:
    """Загружает конфиг пользователя."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Создаём если нет
            cur.execute(
                "INSERT INTO users (user_id, notify_chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, user_id)
            )
            conn.commit()

            cur.execute("SELECT notify_chat_id FROM users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            notify = row["notify_chat_id"] if row else user_id

            # Загружаем аккаунты
            cur.execute("SELECT * FROM accounts WHERE user_id = %s", (user_id,))
            accounts = []
            for acc in cur.fetchall():
                # Загружаем чаты
                cur.execute("SELECT chat_id FROM chats WHERE account_id = %s", (acc["id"],))
                chats = [r["chat_id"] for r in cur.fetchall()]

                # Загружаем ключевые слова
                cur.execute("SELECT keyword FROM keywords WHERE account_id = %s", (acc["id"],))
                keywords = [r["keyword"] for r in cur.fetchall()]

                accounts.append({
                    "phone": acc["phone"],
                    "label": acc["label"],
                    "chats": chats,
                    "keywords": keywords,
                    "active": acc["active"],
                    "session_ok": acc["session_ok"],
                    "session_string": acc.get("session_string", ""),
                })

            return {
                "user_id": user_id,
                "accounts": accounts,
                "notify_chat_id": notify,
            }
    finally:
        conn.close()


def save_user(user_id: int, data: dict):
    """Сохраняет конфиг пользователя (не используется напрямую, см. отдельные функции)."""
    pass


def get_account(user_id: int, phone: str) -> dict | None:
    """Находит аккаунт по номеру телефона."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            acc = cur.fetchone()
            if not acc:
                return None

            cur.execute("SELECT chat_id FROM chats WHERE account_id = %s", (acc["id"],))
            chats = [r["chat_id"] for r in cur.fetchall()]

            cur.execute("SELECT keyword FROM keywords WHERE account_id = %s", (acc["id"],))
            keywords = [r["keyword"] for r in cur.fetchall()]

            return {
                "phone": acc["phone"],
                "label": acc["label"],
                "chats": chats,
                "keywords": keywords,
                "active": acc["active"],
                "session_ok": acc["session_ok"],
                "session_string": acc.get("session_string", ""),
            }
    finally:
        conn.close()


def add_account(user_id: int, phone: str, label: str = "") -> dict:
    """Добавляет новый аккаунт."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Гарантируем что user есть
            cur.execute(
                "INSERT INTO users (user_id, notify_chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, user_id)
            )
            cur.execute(
                """INSERT INTO accounts (user_id, phone, label)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, phone) DO UPDATE SET label = EXCLUDED.label
                   RETURNING id""",
                (user_id, phone, label or phone)
            )
            conn.commit()
            return {"phone": phone, "label": label or phone, "chats": [], "keywords": [], "active": False, "session_ok": False}
    finally:
        conn.close()


def update_account(user_id: int, phone: str, updates: dict):
    """Обновляет поля аккаунта."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Обновляем простые поля
            simple_fields = {}
            for k in ("label", "active", "session_ok", "session_string"):
                if k in updates:
                    simple_fields[k] = updates[k]

            if simple_fields:
                set_parts = [f"{k} = %s" for k in simple_fields]
                vals = list(simple_fields.values()) + [user_id, phone]
                cur.execute(
                    f"UPDATE accounts SET {', '.join(set_parts)} WHERE user_id = %s AND phone = %s",
                    vals
                )

            conn.commit()
    finally:
        conn.close()


def remove_account(user_id: int, phone: str):
    """Удаляет аккаунт."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            conn.commit()
    finally:
        conn.close()


def add_chat(user_id: int, phone: str, chat_id: str):
    """Добавляет чат к аккаунту."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            acc = cur.fetchone()
            if acc:
                cur.execute(
                    "INSERT INTO chats (account_id, chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (acc["id"], chat_id)
                )
                conn.commit()
    finally:
        conn.close()


def remove_chat(user_id: int, phone: str, chat_id: str):
    """Удаляет чат из аккаунта."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            acc = cur.fetchone()
            if acc:
                cur.execute(
                    "DELETE FROM chats WHERE account_id = %s AND chat_id = %s",
                    (acc["id"], chat_id)
                )
                conn.commit()
    finally:
        conn.close()


def add_keyword(user_id: int, phone: str, keyword: str):
    """Добавляет ключевое слово."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            acc = cur.fetchone()
            if acc:
                kw = keyword.strip()
                if kw:
                    cur.execute(
                        "INSERT INTO keywords (account_id, keyword) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (acc["id"], kw)
                    )
                    conn.commit()
    finally:
        conn.close()


def remove_keyword(user_id: int, phone: str, keyword: str):
    """Удаляет ключевое слово."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM accounts WHERE user_id = %s AND phone = %s",
                (user_id, phone)
            )
            acc = cur.fetchone()
            if acc:
                cur.execute(
                    "DELETE FROM keywords WHERE account_id = %s AND keyword = %s",
                    (acc["id"], keyword)
                )
                conn.commit()
    finally:
        conn.close()


def set_notify(user_id: int, chat_id: int):
    """Устанавливает куда слать уведомления."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, notify_chat_id) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET notify_chat_id = EXCLUDED.notify_chat_id",
                (user_id, chat_id)
            )
            conn.commit()
    finally:
        conn.close()


def get_all_active_monitors() -> list[dict]:
    """Возвращает все активные мониторинги."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.phone, a.label, a.session_string, u.user_id, u.notify_chat_id
                FROM accounts a
                JOIN users u ON u.user_id = a.user_id
                WHERE a.active = TRUE AND a.session_ok = TRUE
            """)
            monitors = []
            for acc in cur.fetchall():
                cur.execute("SELECT chat_id FROM chats WHERE account_id = (SELECT id FROM accounts WHERE user_id = %s AND phone = %s)", (acc["user_id"], acc["phone"]))
                chats = [r["chat_id"] for r in cur.fetchall()]

                cur.execute("SELECT keyword FROM keywords WHERE account_id = (SELECT id FROM accounts WHERE user_id = %s AND phone = %s)", (acc["user_id"], acc["phone"]))
                keywords = [r["keyword"] for r in cur.fetchall()]

                if chats and keywords:
                    monitors.append({
                        "user_id": acc["user_id"],
                        "phone": acc["phone"],
                        "label": acc["label"],
                        "chats": chats,
                        "keywords": keywords,
                        "notify_chat_id": acc["notify_chat_id"],
                        "session_string": acc.get("session_string", ""),
                    })
            return monitors
    finally:
        conn.close()
