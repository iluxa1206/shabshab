"""Стоп-лист бумаги в телеграм-чате (команда /stop).

Бумага, которую весь день разбирают по кусочку, забивает чат — а выключать
ради неё фильтр значит ослепнуть по остальному рынку. Стоп-лист глушит ровно
ДОСТАВКУ в этот чат: событие пишется в ленту и уходит в браузер как обычно,
поэтому история цела и /daystat видит всё — иначе анализ шума лечился бы
удалением самого шума.

Область — ЧАТ, а не аккаунт: в личке тихо, в канале команды сигналы идут
дальше. Срок — до конца дня МСК: торговый день кончился, история бумаги
началась заново, и молчание переносить на завтра незачем.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def day_end() -> datetime:
    """Полночь МСК, следующая за текущим моментом — конец действия стоп-листа."""
    msk = _now().astimezone(_MSK)
    return ((msk + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                              microsecond=0)
            .astimezone(timezone.utc))


def add(chat_id: int, isin: str) -> dict:
    """Заглушает бумагу в чате до конца дня МСК. Повтор продлевает срок —
    команда идемпотентна, и «уже молчит» не ошибка."""
    isin = (isin or "").strip().upper()
    until = day_end()
    now = _now().isoformat()
    with _lock, _connect() as c:
        c.execute("INSERT INTO tg_mute_isin(chat_id,isin,until,created_at) "
                  "VALUES(?,?,?,?) ON CONFLICT(chat_id,isin) DO UPDATE SET "
                  "until=excluded.until", (int(chat_id), isin, until.isoformat(), now))
    return {"isin": isin, "until": until}


def remove(chat_id: int, isin: str) -> bool:
    isin = (isin or "").strip().upper()
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM tg_mute_isin WHERE chat_id=? AND isin=?",
                        (int(chat_id), isin))
        return cur.rowcount > 0


def clear(chat_id: int) -> int:
    with _lock, _connect() as c:
        cur = c.execute("DELETE FROM tg_mute_isin WHERE chat_id=?", (int(chat_id),))
        return cur.rowcount


def list_for_chat(chat_id: int) -> List[dict]:
    """Живые записи чата. Просроченные подчищаем здесь же: отдельный воркер
    ради пары строк в день не нужен, а список читают редко."""
    now = _now().isoformat()
    with _lock, _connect() as c:
        c.execute("DELETE FROM tg_mute_isin WHERE until <= ?", (now,))
        rows = c.execute("SELECT isin, until FROM tg_mute_isin WHERE chat_id=? "
                         "ORDER BY isin", (int(chat_id),)).fetchall()
    return [dict(r) for r in rows]


def muted_set(chat_id: int) -> set:
    """ISIN, по которым этот чат сегодня молчит. Зовётся на каждой отправке,
    поэтому один короткий SELECT без чисток."""
    now = _now().isoformat()
    try:
        with _connect() as c:
            return {r["isin"] for r in c.execute(
                "SELECT isin FROM tg_mute_isin WHERE chat_id=? AND until > ?",
                (int(chat_id), now)).fetchall()}
    except Exception as e:                      # доставку ронять нельзя
        logger.warning("tg_mute muted_set error (chat %s): %s", chat_id, e)
        return set()


def is_muted(chat_id: int, isin: Optional[str]) -> bool:
    return bool(isin) and (isin or "").strip().upper() in muted_set(chat_id)
