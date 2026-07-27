"""Точная история спредов: дневные снапшоты spread-метрик из тёплых кэшей
поллера (universe_metrics флоатеры + fixed_metrics фиксы). В отличие от
candle-оценки (историч. цена × текущая модель) — точные значения на дату, с
реальной кривой/НКД/сроком того дня. Копится вперёд, идемпотентно per (isin,date)."""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from services.portfolio_db import _connect, _lock

logger = logging.getLogger(__name__)

_MSK_OFFSET = 3  # часы


def _msk_date() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=_MSK_OFFSET)).date().isoformat()


def write_snapshot() -> int:
    """Снимок спред-метрик всего юниверса на сегодня (МСК) из market_cache.
    Возвращает число записанных строк. Идемпотентно (INSERT OR REPLACE)."""
    from services.market_data import market_cache, MarketDataService
    d = _msk_date()
    rows = []

    um = MarketDataService.universe_metrics() or {}
    for isin, m in um.items():
        if not isin or not isinstance(m, dict):
            continue
        rows.append((isin, d, "floater", m.get("last"), m.get("disc_dm"),
                     None, m.get("z_model"), m.get("ytm")))

    fxm = market_cache.get("fixed_metrics") or {}
    for isin, m in fxm.items():
        if not isin or not isinstance(m, dict):
            continue
        rows.append((isin, d, "fixed", m.get("last"), None,
                     m.get("g_spread_bps"), m.get("z_spread_bps"), m.get("ytm")))

    # пишем только строки с хоть каким-то спредом (иначе шум пустых)
    rows = [r for r in rows if r[4] is not None or r[5] is not None or r[6] is not None]
    if not rows:
        return 0
    with _lock, _connect() as c:
        c.executemany(
            "INSERT OR REPLACE INTO spread_daily(isin,date,kind,price_pct,dm_bps,"
            "g_spread_bps,z_bps,ytm) VALUES(?,?,?,?,?,?,?,?)", rows)
    logger.info("spread snapshot %s: %d строк", d, len(rows))
    return len(rows)


def read_history(isin: str, days: int = 400) -> List[dict]:
    """Точная история по бумаге, по возрастанию даты."""
    with _connect() as c:
        r = c.execute(
            "SELECT date, kind, price_pct, dm_bps, g_spread_bps, z_bps, ytm "
            "FROM spread_daily WHERE isin=? ORDER BY date DESC LIMIT ?",
            (isin, days)).fetchall()
    return [dict(x) for x in reversed(r)]
