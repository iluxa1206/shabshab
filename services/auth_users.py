"""Хранилище пользователей дашборда: bcrypt-хеши в data/users.json.

Доступ к сайту закрыт — аккаунты создаёт только админ (CLI scripts/useradd.py).
Формат файла: {"users": {"<email>": {"hash": "<bcrypt>", "role": "user|admin",
"created": "<iso>"}}}. Запись атомарная (tmp + os.replace) под threading.Lock,
чтение кэшируется в память — как в auth.py для токена Alor.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt

# data/ монтируется как том в проде (docker-compose.prod.yml) — переживает редеплой.
_ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = Path(os.getenv("AUTH_USERS_FILE", _ROOT / "data" / "users.json"))

_lock = threading.Lock()
_cache: Optional[dict] = None
_cache_mtime: float = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    """Читает файл юзеров с ленивым кэшем по mtime (перечитывает при изменении)."""
    global _cache, _cache_mtime
    try:
        mtime = USERS_FILE.stat().st_mtime
    except OSError:
        _cache = {"users": {}}
        _cache_mtime = 0.0
        return _cache
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "users" not in data:
            data = {"users": {}}
    except (json.JSONDecodeError, OSError):
        data = {"users": {}}
    _cache = data
    _cache_mtime = mtime
    return data


def _save(data: dict) -> None:
    global _cache, _cache_mtime
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)  # атомарно
    _cache = data
    try:
        _cache_mtime = USERS_FILE.stat().st_mtime
    except OSError:
        _cache_mtime = 0.0


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_credentials(email: str, password: str) -> Optional[dict]:
    """Возвращает {"email", "role"} при верном пароле, иначе None."""
    email = (email or "").strip().lower()
    rec = _load()["users"].get(email)
    if not rec:
        return None
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), rec["hash"].encode("ascii"))
    except (ValueError, KeyError):
        return None
    if not ok:
        return None
    return {"email": email, "role": rec.get("role", "user")}


def get_user(email: str) -> Optional[dict]:
    """Пользователь по email (без проверки пароля) — для валидации сессии."""
    email = (email or "").strip().lower()
    rec = _load()["users"].get(email)
    if not rec:
        return None
    return {"email": email, "role": rec.get("role", "user")}


def add_user(email: str, password: str, role: str = "user", overwrite: bool = False) -> None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("email должен быть валидным адресом")
    if len(password) < 8:
        raise ValueError("пароль минимум 8 символов")
    with _lock:
        data = _load()
        # копия под локом, чтобы не мутировать кэш до успешной записи
        data = {"users": dict(data["users"])}
        if email in data["users"] and not overwrite:
            raise ValueError(f"пользователь {email} уже существует (--force для перезаписи)")
        data["users"][email] = {
            "hash": hash_password(password),
            "role": role,
            "created": data["users"].get(email, {}).get("created", _now_iso()),
        }
        _save(data)


def remove_user(email: str) -> bool:
    email = (email or "").strip().lower()
    with _lock:
        data = _load()
        if email not in data["users"]:
            return False
        data = {"users": dict(data["users"])}
        del data["users"][email]
        _save(data)
        return True


def list_users() -> list[dict]:
    return [
        {"email": e, "role": r.get("role", "user"), "created": r.get("created")}
        for e, r in sorted(_load()["users"].items())
    ]


def count_admins() -> int:
    return sum(1 for r in _load()["users"].values() if r.get("role") == "admin")


def set_password(email: str, password: str) -> None:
    email = (email or "").strip().lower()
    if len(password) < 8:
        raise ValueError("пароль минимум 8 символов")
    with _lock:
        data = _load()
        if email not in data["users"]:
            raise ValueError(f"нет пользователя {email}")
        data = {"users": dict(data["users"])}
        rec = dict(data["users"][email])
        rec["hash"] = hash_password(password)
        data["users"][email] = rec
        _save(data)


def set_role(email: str, role: str) -> None:
    email = (email or "").strip().lower()
    if role not in ("user", "admin"):
        raise ValueError("роль должна быть user или admin")
    with _lock:
        data = _load()
        if email not in data["users"]:
            raise ValueError(f"нет пользователя {email}")
        data = {"users": dict(data["users"])}
        rec = dict(data["users"][email])
        rec["role"] = role
        data["users"][email] = rec
        _save(data)
