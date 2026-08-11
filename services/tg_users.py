"""Пользователи Telegram-бота: регистрация на /start, allowlist, mute.
Хранилище — portfolio.db (таблица tg_users). Идентичность бота автономна:
алерты живут в общей таблице alerts под user_email = 'tg:<tg_user_id>'.

Доступ: env TG_ALLOWED_IDS (id через запятую) — жёсткий allowlist. Если пуст,
бота «захватывает» первый /start: дальше пускаем только уже зарегистрированных.
Дашборд приватный — бот тоже, чужой /start получает отказ."""
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

EMAIL_PREFIX = "tg:"


def email_for(tg_user_id: int) -> str:
    """user_email для общей таблицы alerts."""
    return f"{EMAIL_PREFIX}{tg_user_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allowed_ids() -> List[int]:
    raw = os.getenv("TG_ALLOWED_IDS") or ""
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def is_allowed(tg_user_id: int) -> bool:
    ids = _allowed_ids()
    if ids:
        return tg_user_id in ids
    # allowlist пуст: пускаем зарегистрированных, а если база пуста — первого
    with _connect() as c:
        if c.execute("SELECT 1 FROM tg_users WHERE tg_user_id=?",
                     (tg_user_id,)).fetchone():
            return True
        return c.execute("SELECT COUNT(*) FROM tg_users").fetchone()[0] == 0


def upsert(tg_user_id: int, chat_id: int, username: Optional[str] = None) -> dict:
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO tg_users(tg_user_id, chat_id, username, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET "
            "chat_id=excluded.chat_id, username=excluded.username",
            (tg_user_id, chat_id, username, _now()))
    return get(tg_user_id)


def get(tg_user_id: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM tg_users WHERE tg_user_id=?",
                      (tg_user_id,)).fetchone()
        return dict(r) if r else None


def chat_for_email(user_email: str) -> Optional[dict]:
    """Строка tg_users по user_email вида 'tg:<id>'. None — не наш алерт."""
    if not user_email.startswith(EMAIL_PREFIX):
        return None
    suffix = user_email[len(EMAIL_PREFIX):]
    if not suffix.isdigit():
        return None
    return get(int(suffix))


def set_muted(tg_user_id: int, muted: bool) -> None:
    with _lock, _connect() as c:
        c.execute("UPDATE tg_users SET muted=? WHERE tg_user_id=?",
                  (1 if muted else 0, tg_user_id))
