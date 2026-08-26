"""Привязка телеграм-чатов к веб-аккаунтам дашборда.

Своей настройки у бота нет: он дублирует в чат алерты и сигналы того веб-юзера,
к которому привязан. /start заводит заявку (status='pending'), привязку делает
админ на сайте (Настройки доступа → Телеграм): выбирает email и одобряет.
Дашборд приватный — бот тоже: неодобренному чату отвечаем отказом.

Хранилище — portfolio.db (таблица tg_users). Один email → сколько угодно чатов.
"""
import json
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
    """Привязка чата к веб-аккаунту."""
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


def email_exists_approved(user_email: str) -> bool:
    """Есть ли у аккаунта ЖИВАЯ привязка — БЕЗ учёта mute.

    Отличается от has_chats() ровно этим. Право доставки в КАНАЛ определяется
    существованием привязки владельца, а не тем, поставил ли он паузу личке:
    /mute — это «не пиши мне в личку», а не «отключи мои каналы». А revoke и
    удаление аккаунта канал гасить обязаны."""
    email = (user_email or "").strip().lower()
    if not email:
        return False
    with _connect() as c:
        return c.execute(
            "SELECT 1 FROM tg_users WHERE email=? AND status='approved' LIMIT 1",
            (email,)).fetchone() is not None


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


# ── свои маркеры строк (команда бота /custom) ──────────────────────────────
#
# Маркер — первый символ строки сигнала, и читают сообщение именно по нему.
# Дефолты подобраны под торговую конвенцию (оффер красный, бид зелёный), но
# «красный = плохо» у каждого своё, поэтому набор переопределяется на чат:
# один и тот же аккаунт может смотреть алерты с телефона и с рабочей машины.
ICON_SLOTS = {
    "ask": ("оффер", "🔴"),
    "bid": ("бид", "🟢"),
    "buy": ("сделка · покупка", "👍"),
    "sell": ("сделка · продажа", "👎"),
    "ndm": ("адресная сделка", "🤝"),
}
# Русские имена слотов — команда должна понимать то, что видит пользователь.
SLOT_ALIASES = {
    "оффер": "ask", "аск": "ask", "офер": "ask",
    "бид": "bid", "покупка": "buy", "покупки": "buy", "buy": "buy",
    "продажа": "sell", "продажи": "sell", "sell": "sell",
    "адресная": "ndm", "рпс": "ndm", "ndm": "ndm",
}


def slot_key(name: str) -> Optional[str]:
    """'оффер' / 'ASK' → 'ask'. None — слота с таким именем нет."""
    k = (name or "").strip().lower()
    if k in ICON_SLOTS:
        return k
    return SLOT_ALIASES.get(k)


def icons(row: Optional[dict]) -> dict:
    """Полный набор маркеров чата: свои поверх дефолтных.

    На вход — строка tg_users (её уже держат chats_for_email/get), чтобы
    доставка не ходила в базу второй раз на каждое сообщение."""
    out = {k: v[1] for k, v in ICON_SLOTS.items()}
    raw = (row or {}).get("emoji")
    if not raw:
        return out
    try:
        custom = json.loads(raw)
    except (TypeError, ValueError):
        return out
    if isinstance(custom, dict):
        for k, v in custom.items():
            if k in out and isinstance(v, str) and v:
                out[k] = v
    return out


def set_icon(tg_user_id: int, slot: str, emoji: Optional[str]) -> dict:
    """Ставит (или снимает при emoji=None) один маркер. → новый набор чата."""
    row = get(tg_user_id) or {}
    try:
        cur = json.loads(row.get("emoji") or "{}")
    except (TypeError, ValueError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    if emoji:
        cur[slot] = emoji
    else:
        cur.pop(slot, None)
    payload = json.dumps(cur, ensure_ascii=False) if cur else None
    with _lock, _connect() as c:
        c.execute("UPDATE tg_users SET emoji=? WHERE tg_user_id=?",
                  (payload, tg_user_id))
    return icons({"emoji": payload})


def reset_icons(tg_user_id: int) -> dict:
    with _lock, _connect() as c:
        c.execute("UPDATE tg_users SET emoji=NULL WHERE tg_user_id=?", (tg_user_id,))
    return icons(None)
