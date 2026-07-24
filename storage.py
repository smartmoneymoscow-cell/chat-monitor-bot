"""
Хранилище пользовательских настроек (JSON-based).

Каждый пользователь хранит свой конфиг:
  - аккаунты Telethon (сессии для мониторинга)
  - чаты для каждого аккаунта
  - ключевые слова для каждого аккаунта
  - куда слать уведомления
"""

import os
import json
import threading
from pathlib import Path
from typing import Any

import config

_lock = threading.Lock()


def _user_path(user_id: int) -> Path:
    """Путь к JSON-файлу пользователя."""
    return Path(config.DATA_DIR) / f"user_{user_id}.json"


def load_user(user_id: int) -> dict:
    """Загружает конфиг пользователя (или пустой шаблон)."""
    path = _user_path(user_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "user_id": user_id,
        "accounts": [],      # Список Telethon-аккаунтов
        "notify_chat_id": user_id,  # Куда слать уведомления (по умолчанию — себе)
    }


def save_user(user_id: int, data: dict):
    """Сохраняет конфиг пользователя."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = _user_path(user_id)
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_account(user_id: int, phone: str) -> dict | None:
    """Находит аккаунт по номеру телефона."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            return acc
    return None


def add_account(user_id: int, phone: str, label: str = "") -> dict:
    """Добавляет новый аккаунт."""
    data = load_user(user_id)
    # Проверяем дубликат
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            return acc
    acc = {
        "phone": phone,
        "label": label or phone,
        "chats": [],         # Список чатов (id или username)
        "keywords": [],      # Ключевые слова
        "active": False,     # Активен ли мониторинг
        "session_ok": False, # Авторизован ли Telethon
    }
    data["accounts"].append(acc)
    save_user(user_id, data)
    return acc


def update_account(user_id: int, phone: str, updates: dict):
    """Обновляет поля аккаунта."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc.update(updates)
            break
    save_user(user_id, data)


def remove_account(user_id: int, phone: str):
    """Удаляет аккаунт."""
    data = load_user(user_id)
    data["accounts"] = [a for a in data["accounts"] if a["phone"] != phone]
    save_user(user_id, data)


def add_chat(user_id: int, phone: str, chat_id: str):
    """Добавляет чат к аккаунту."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            if chat_id not in acc["chats"]:
                acc["chats"].append(chat_id)
            break
    save_user(user_id, data)


def remove_chat(user_id: int, phone: str, chat_id: str):
    """Удаляет чат из аккаунта."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc["chats"] = [c for c in acc["chats"] if c != chat_id]
            break
    save_user(user_id, data)


def add_keyword(user_id: int, phone: str, keyword: str):
    """Добавляет ключевое слово."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            kw = keyword.strip().lower()
            if kw and kw not in [k.lower() for k in acc["keywords"]]:
                acc["keywords"].append(keyword.strip())
            break
    save_user(user_id, data)


def remove_keyword(user_id: int, phone: str, keyword: str):
    """Удаляет ключевое слово."""
    data = load_user(user_id)
    for acc in data["accounts"]:
        if acc["phone"] == phone:
            acc["keywords"] = [k for k in acc["keywords"] if k.lower() != keyword.lower()]
            break
    save_user(user_id, data)


def set_notify(user_id: int, chat_id: int):
    """Устанавливает куда слать уведомления."""
    data = load_user(user_id)
    data["notify_chat_id"] = chat_id
    save_user(user_id, data)


def get_all_active_monitors() -> list[dict]:
    """Возвращает все активные мониторинги (для Telethon-воркера)."""
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
