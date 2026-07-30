"""Бэкфилл истории Y-IDX/DM в spread_daily по дневным свечам MOEX.

Наполняет график «Y-IDX ДИНАМИКА» (аналитика) прошлым: по каждой бумаге юниверса
берём дневные close (TQCB/TQOB/TQRD) и repricing'ом через ТЕКУЩУЮ модель бумаги
(load_reprice_ctx → reprice_at_price) получаем y_idx/DM на каждую прошлую дату.

Это ОЦЕНКА (candle-est): кривая/НКД/срок — сегодняшние, честный as-of на глубину
года невозможен (архив своп-котировок копится только с 2026-07-30). Для тренда
медиан по бакетам/эмитентам достаточно; точные дневные снапшоты пишутся вперёд
поллером и имеют приоритет: INSERT OR IGNORE не затирает существующие строки.

Запуск (в контейнере прода или локально из корня репо):
    python scripts/backfill_yidx_history.py [--days 365] [--limit N]
Идемпотентен: повторный прогон дозаполняет только отсутствующие (isin, date).
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_yidx")
log.setLevel(logging.INFO)

_BOARDS = ("TQCB", "TQOB", "TQRD")


async def _secid_board_map() -> dict:
    """{isin: (secid, board)} по листингам бордов (ОФЗ: SECID=SU… ≠ ISIN)."""
    import httpx
    out: dict = {}
    async with httpx.AsyncClient() as client:
        for board in _BOARDS:
            r = await client.get(
                f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/{board}/securities.json",
                params={"iss.only": "securities", "securities.columns": "SECID,ISIN"},
                timeout=30)
            r.raise_for_status()
            sec = r.json().get("securities", {})
            cols, rows = sec.get("columns", []), sec.get("data", [])
            si, ii = cols.index("SECID"), cols.index("ISIN")
            for row in rows:
                isin = row[ii]
                if isin and isin not in out:
                    out[isin] = (row[si], board)
    return out


async def backfill(days: int, limit: int | None) -> None:
    from services import instruments_registry
    from services.market_data import MarketDataService
    from services.bond_details import load_reprice_ctx, reprice_at_price
    from services.paths import cache_path
    from services.portfolio_db import init_db, _connect, _lock

    init_db()
    uni = await instruments_registry.fetch_floater_universe()
    if limit:
        uni = uni[:limit]
    sb = await _secid_board_map()
    cache = MarketDataService.get_local_bond_cache(cache_path("isins_cache.json"))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    today = date.today().isoformat()

    total_rows = skipped = 0
    for i, u in enumerate(uni, 1):
        isin = u["isin"]
        secid, board = sb.get(isin) or (isin, "TQCB")
        try:
            candles = await MarketDataService.fetch_candles(secid, "1d", board)
            if not candles:
                skipped += 1
                continue
            ctx = await load_reprice_ctx(isin, cache)
            memo: dict = {}
            rows = []
            for c in candles:
                px, t = c.get("c"), c.get("t")
                if px is None or not t:
                    continue
                d = t[:10]
                # прошлое в пределах окна; сегодня пишет точный снапшот поллера
                if d < cutoff or d >= today:
                    continue
                m = memo.get(px)
                if m is None:
                    try:
                        m = reprice_at_price(ctx, px) or {}
                    except Exception:
                        m = {}
                    memo[px] = m
                y = m.get("yield_over_index_bps")
                dm = m.get("disc_margin_bps")
                if y is None and dm is None:
                    continue
                rows.append((isin, d, "floater", round(px, 4), dm, None, None,
                             m.get("yield_xirr_pct"), y))
            if rows:
                with _lock, _connect() as conn:
                    conn.executemany(
                        "INSERT OR IGNORE INTO spread_daily(isin,date,kind,price_pct,"
                        "dm_bps,g_spread_bps,z_bps,ytm,y_idx) VALUES(?,?,?,?,?,?,?,?,?)",
                        rows)
                total_rows += len(rows)
            await asyncio.sleep(0.1)   # щадим MOEX ISS
        except Exception as e:
            skipped += 1
            log.warning("%s: %s", isin, e)
        if i % 25 == 0 or i == len(uni):
            log.info("%d/%d бумаг · строк записано %d · пропущено %d",
                     i, len(uni), total_rows, skipped)
    log.info("готово: %d строк, %d бумаг пропущено", total_rows, skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Бэкфилл y-idx истории в spread_daily")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--limit", type=int, default=None, help="первые N бумаг (отладка)")
    a = ap.parse_args()
    asyncio.run(backfill(a.days, a.limit))
