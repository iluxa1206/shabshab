"""Архив своп-котировок (OIS RUONIA / IRS KEYRATE) по датам.

Cbonds отдаёт только последний срез, rates_cache.json перезаписывается — история
кривой нигде не копилась. Этот модуль пишет каждую свежую пачку котировок в
portfolio.db (переживает редеплой) и отдаёт срез «на дату ≤ D» для честного
bootstrap прошлой кривой (services.backdate, mode="market").

История копится с момента деплоя модуля; даты ДО первой записи закрываются
гибридной кривой (реализованный факт индекса + текущая кривая, mode="realized").
"""
from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional, Tuple

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS swap_quotes_daily(
  date  TEXT NOT NULL,          -- дата котировки (rates_date Cbonds, не дата фетча)
  base  TEXT NOT NULL,          -- RUONIA | KEYRATE
  tenor TEXT NOT NULL,          -- ON/1W/3M/1Y/...
  value REAL NOT NULL,          -- par-ставка, %
  PRIMARY KEY(date, base, tenor)
);
CREATE INDEX IF NOT EXISTS ix_swapq_base_date ON swap_quotes_daily(base, date);
"""

_schema_done = False


def _ensure_schema() -> None:
    global _schema_done
    if _schema_done:
        return
    with _lock, _connect() as c:
        c.executescript(_SCHEMA)
    _schema_done = True


def save_snapshot(ois_quotes: list, irs_quotes: list) -> int:
    """Пишет пачку котировок под ИХ датой (q.date). Идемпотентно (OR REPLACE).
    Зовётся из market_data после успешного bootstrap — best-effort."""
    rows: List[Tuple[str, str, str, float]] = []
    for base, quotes in (("RUONIA", ois_quotes or []), ("KEYRATE", irs_quotes or [])):
        for q in quotes:
            if q.tenor and q.value is not None and q.date:
                rows.append((q.date.isoformat(), base, q.tenor, float(q.value)))
    if not rows:
        return 0
    _ensure_schema()
    with _lock, _connect() as c:
        c.executemany(
            "INSERT OR REPLACE INTO swap_quotes_daily(date,base,tenor,value) "
            "VALUES(?,?,?,?)", rows)
    return len(rows)


def quotes_first(base: str) -> Optional[tuple]:
    """Самая РАННЯЯ дата архива котировок базы + котировки этого дня.
    Якорь гибридной кривой для дат ДО начала архива: сшивать реализованный факт
    индекса с первой архивной кривой честнее, чем с сегодняшней — иначе серия
    рвётся скачком на границе архива (см. backdate.curve_asof).
    → (date, list[core.rates.Quote]) или None (архив пуст)."""
    _ensure_schema()
    with _connect() as c:
        row = c.execute("SELECT MIN(date) AS d FROM swap_quotes_daily WHERE base=?",
                        (base,)).fetchone()
        qd = row["d"] if row else None
        if not qd:
            return None
        rows = c.execute(
            "SELECT tenor, value FROM swap_quotes_daily WHERE base=? AND date=?",
            (base, qd)).fetchall()
    from core.rates import Quote
    qdate = date.fromisoformat(qd)
    return qdate, [Quote(f"{base} {r['tenor']}", r["tenor"], r["value"], qdate) for r in rows]


def quotes_asof(base: str, d: date, max_lag_days: int = 7) -> Optional[list]:
    """Котировки на ближайшую дату ≤ d (не старше max_lag_days).
    → list[core.rates.Quote] или None (архив ещё не покрывает дату)."""
    _ensure_schema()
    with _connect() as c:
        row = c.execute(
            "SELECT MAX(date) AS d FROM swap_quotes_daily WHERE base=? AND date<=?",
            (base, d.isoformat())).fetchone()
        qd = row["d"] if row else None
        if not qd:
            return None
        if (d - date.fromisoformat(qd)).days > max_lag_days:
            return None
        rows = c.execute(
            "SELECT tenor, value FROM swap_quotes_daily WHERE base=? AND date=?",
            (base, qd)).fetchall()
    from core.rates import Quote
    qdate = date.fromisoformat(qd)
    return [Quote(f"{base} {r['tenor']}", r["tenor"], r["value"], qdate) for r in rows]
