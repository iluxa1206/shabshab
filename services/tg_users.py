"""Привязка телеграм-чатов к веб-аккаунтам дашборда.

Своей настройки у бота нет: он дублирует в чат алерты и сигналы того веб-юзера,
к которому привязан. /start заводит заявку (status='pending'), привязку делает
админ на сайте (Настройки доступа → Телеграм): выбирает email и одобряет.
Дашборд приватный — бот тоже: неодобренному чату отвечаем отказом.

Хранилище — portfolio.db (таблица tg_users). Один email → сколько угодно чатов.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

# Легаси-идентичность автономного бота (алерты под 'tg:<id>'). Новых записей не
# появляется; при одобрении заявки старые алерты переезжают на email владельца.
LEGACY_PREFIX = "tg:"

STATUSES = ("pending", "approved", "rejected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def legacy_email(tg_user_id: int) -> str:
    return f"{LEGACY_PREFIX}{tg_user_id}"


def get(tg_user_id: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM tg_users WHERE tg_user_id=?",
                      (tg_user_id,)).fetchone()
        return dict(r) if r else None


def email_for(tg_user_id: int) -> Optional[str]:
    """Веб-аккаунт чата. None — заявка не одобрена."""
    row = get(tg_user_id)
    if not row or row.get("status") != "approved":
        return None
    return row.get("email") or None


def is_allowed(tg_user_id: int) -> bool:
    row = get(tg_user_id)
    return bool(row and row.get("status") == "approved" and row.get("email"))


def request_access(tg_user_id: int, chat_id: int,
                   username: Optional[str] = None) -> dict:
    """/start: заводит заявку либо освежает chat_id/username уже известного
    чата. Статус одобренного не сбрасываем — повторный /start не разлогинивает."""
    with _lock, _connect() as c:
        c.execute(
            "INSERT INTO tg_users(tg_user_id, chat_id, username, created_at, status) "
            "VALUES(?,?,?,?, 'pending') ON CONFLICT(tg_user_id) DO UPDATE SET "
            "chat_id=excluded.chat_id, username=excluded.username",
            (tg_user_id, chat_id, username, _now()))
    return get(tg_user_id)


def list_all() -> List[dict]:
    """Все чаты для админки: сначала необработанные заявки."""
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM tg_users ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
            "created_at DESC").fetchall()
    return [dict(r) for r in rows]


def pending_count() -> int:
    with _connect() as c:
        return c.execute("SELECT COUNT(*) FROM tg_users "
                         "WHERE status='pending'").fetchone()[0]


def approve(tg_user_id: int, email: str, by: str) -> Optional[dict]:
    """Привязка чата к веб-аккаунту. Заодно переносит алерты легаси-идентичности
    'tg:<id>' на email владельца — иначе они молча осиротеют."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("не указан email аккаунта")
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE tg_users SET email=?, status='approved', approved_at=?, "
            "approved_by=? WHERE tg_user_id=?",
            (email, _now(), by, tg_user_id))
        if not cur.rowcount:
            return None
        moved = c.execute("UPDATE alerts SET user_email=? WHERE user_email=?",
                          (email, legacy_email(tg_user_id))).rowcount
    if moved:
        logger.info("tg approve %s → %s: перенесено алертов %d",
                    tg_user_id, email, moved)
    return get(tg_user_id)


def revoke(tg_user_id: int) -> Optional[dict]:
    """Отвязка: доставка прекращается, чат может подать заявку заново."""
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE tg_users SET email=NULL, status='rejected', approved_at=NULL, "
            "approved_by=NULL WHERE tg_user_id=?", (tg_user_id,))
        if not cur.rowcount:
            return None
    return get(tg_user_id)


def delete(tg_user_id: int) -> bool:
    with _lock, _connect() as c:
        return bool(c.execute("DELETE FROM tg_users WHERE tg_user_id=?",
                              (tg_user_id,)).rowcount)


def chats_for_email(user_email: str) -> List[dict]:
    """Привязанные чаты аккаунта, готовые к доставке (без mute)."""
    email = (user_email or "").strip().lower()
    if not email:
        return []
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM tg_users WHERE email=? AND status='approved' "
            "AND muted=0", (email,)).fetchall()
    return [dict(r) for r in rows]


def has_chats(user_email: str) -> bool:
    """Есть ли смысл вообще класть событие в очередь доставки."""
    email = (user_email or "").strip().lower()
    if not email:
        return False
    with _connect() as c:
        return c.execute(
            "SELECT 1 FROM tg_users WHERE email=? AND status='approved' "
            "AND muted=0 LIMIT 1", (email,)).fetchone() is not None


def set_muted(tg_user_id: int, muted: bool) -> None:
    with _lock, _connect() as c:
        c.execute("UPDATE tg_users SET muted=? WHERE tg_user_id=?",
                  (1 if muted else 0, tg_user_id))
