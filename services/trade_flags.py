"""Отмеченные сделки ленты («красный флажок»), per-user.

Хранилище — portfolio.db, таблица trade_flag (см. portfolio_db). Пишем СНИМОК
строки, а не ссылку на trade_tick: тиковый архив подчищается ретеншеном
(мелочь старше TICK_RAW_DAYS удаляется), и отмеченная сделка через месяц
пропала бы из списка вместе с исходной строкой.
"""
from datetime import datetime, timezone
from typing import Optional

from services.portfolio_db import _connect, _lock

# Поля снимка: ровно то, что рисует лента (см. api/routes/trades.tape).
_SNAP = ("isin", "ts", "price", "qty", "value", "side", "board", "market",
         "cur", "y_idx_bps", "yld")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add(user_email: str, trade: dict, note: Optional[str] = None) -> dict:
    """Ставит флаг (идемпотентно: повторный вызов обновляет снимок и заметку)."""
    tid = trade.get("trade_id")
    isin = (trade.get("isin") or "").strip().upper()
    ts = (trade.get("ts") or "").strip()
    if tid is None or not isin or not ts:
        raise ValueError("нужны trade_id, isin и ts")
    vals = [user_email, int(tid), isin, ts,
            *(trade.get(k) for k in _SNAP[2:]), note, _now()]
    cols = ("user_email", "trade_id", *_SNAP, "note", "created_at")
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{k}=excluded.{k}" for k in (*_SNAP[2:], "note"))
    with _lock, _connect() as c:
        c.execute(f"INSERT INTO trade_flag({','.join(cols)}) VALUES({ph}) "
                  f"ON CONFLICT(user_email,trade_id) DO UPDATE SET {upd}", vals)
    return {"trade_id": int(tid), "isin": isin, "ts": ts, "flagged": True}


def remove(user_email: str, trade_id: int) -> bool:
    with _lock, _connect() as c:
        n = c.execute("DELETE FROM trade_flag WHERE user_email=? AND trade_id=?",
                      (user_email, int(trade_id))).rowcount
    return bool(n)


def ids(user_email: str) -> set:
    """Множество trade_id пользователя — разметка строк ленты одним запросом."""
    with _connect() as c:
        return {r[0] for r in c.execute(
            "SELECT trade_id FROM trade_flag WHERE user_email=?", (user_email,))}


def listing(user_email: str, limit: int = 1000) -> list[dict]:
    """Снимки отмеченных сделок, новые сверху."""
    with _connect() as c:
        rows = c.execute(
            "SELECT trade_id, isin, ts, price, qty, value, side, board, market, "
            "cur, y_idx_bps, yld, note, created_at FROM trade_flag "
            "WHERE user_email=? ORDER BY ts DESC, trade_id DESC LIMIT ?",
            (user_email, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["negotiated"] = d.get("market") == "ndm"
        d["flagged"] = True
        out.append(d)
    return out
