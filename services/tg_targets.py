"""Адресаты доставки сигналов: группы и каналы, куда добавлен бот.

Зачем отдельно от tg_users: там идентичность человека (заявка, одобрение
админом, пауза доставки), здесь — просто адрес, принадлежащий аккаунту.
Фильтр ссылается сюда полем signal_filters.tg_target_id: «Р5» уходит в канал
«Р5», «Ф5» — в свой, а фильтр без ссылки идёт в личные чаты, как раньше.

КАК РЕГИСТРИРУЕТСЯ КАНАЛ: владелец добавляет бота в канал/группу (в канал —
администратором, иначе Bot API не даст писать) и пересылает оттуда любое
сообщение боту в личку. В пересланном апдейте Telegram отдаёт id и название
исходного чата — этого хватает; спрашивать id у пользователя не нужно.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

MAX_TARGETS_PER_USER = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_for_user(user_email: str) -> List[dict]:
    email = (user_email or "").strip().lower()
    if not email:
        return []
    with _connect() as c:
        rows = c.execute("SELECT * FROM tg_targets WHERE user_email=? ORDER BY id",
                         (email,)).fetchall()
    return [dict(r) for r in rows]


def get(target_id: int) -> Optional[dict]:
    with _connect() as c:
        r = c.execute("SELECT * FROM tg_targets WHERE id=?", (target_id,)).fetchone()
    return dict(r) if r else None


def chat_id_for(target_id: Optional[int], user_email: str) -> Optional[int]:
    """Куда слать сигналы фильтра. None — адресат не задан или уже не наш
    (владельца сменили, канал отвязали): доставка тогда идёт в личные чаты,
    то есть сигнал не теряется молча."""
    if not target_id:
        return None
    t = get(int(target_id))
    if not t or t["user_email"] != (user_email or "").strip().lower():
        return None
    return int(t["chat_id"])


def add(user_email: str, chat_id: int, title: str, kind: str) -> dict:
    """Регистрирует адрес за аккаунтом (повторная привязка обновляет название —
    канал могли переименовать)."""
    email = (user_email or "").strip().lower()
    if not email:
        raise ValueError("не указан аккаунт")
    title = (title or "").strip()[:120] or str(chat_id)
    with _lock, _connect() as c:
        n = c.execute("SELECT COUNT(*) FROM tg_targets WHERE user_email=?",
                      (email,)).fetchone()[0]
        exists = c.execute("SELECT id FROM tg_targets WHERE user_email=? AND chat_id=?",
                           (email, int(chat_id))).fetchone()
        if not exists and n >= MAX_TARGETS_PER_USER:
            raise ValueError(f"Больше {MAX_TARGETS_PER_USER} адресатов не поддерживается")
        c.execute(
            "INSERT INTO tg_targets(user_email,chat_id,title,kind,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_email,chat_id) DO UPDATE SET "
            "title=excluded.title, kind=excluded.kind",
            (email, int(chat_id), title, kind, _now()))
        r = c.execute("SELECT * FROM tg_targets WHERE user_email=? AND chat_id=?",
                      (email, int(chat_id))).fetchone()
    return dict(r)


def remove(user_email: str, target_id: int) -> bool:
    """Снимает адресата. Фильтры, которые на него ссылались, не трогаем —
    chat_id_for вернёт None, и они вернутся к доставке в личку."""
    email = (user_email or "").strip().lower()
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM tg_targets WHERE id=? AND user_email=?",
                        (int(target_id), email))
        return cur.rowcount > 0
