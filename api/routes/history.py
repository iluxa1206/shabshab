"""Динамика спредов: историческая серия DM (флоатер) / g-спреда (фикс) по
дневным закрытиям MOEX. reprice каждой историч. цены под ТЕКУЩИЙ контекст
(accrued/curve/calc_date фиксированы) → НКД-пилы нет, серия = функция цены через
сегодняшнюю модель. Оценка динамики, не точный историч. спред (кривая/срок
менялись) — но для тренда достаточно. Без хранилища, on-demand."""
import re
import logging
from fastapi import APIRouter, Path, Query, HTTPException

from services.market_data import MarketDataService
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_SECID_RE = re.compile(r"[A-Z0-9]{4,14}")


@router.get("/{isin}/spread", tags=["History"])
async def spread_history(
    isin: str = Path(...),
    kind: str = Query("floater", description="floater | fixed"),
    secid: str = Query(None, description="SECID (ОФЗ ≠ ISIN)"),
    board: str = Query("TQCB", description="TQCB корп / TQOB ОФЗ"),
    days: int = Query(120, ge=10, le=400),
):
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    sec = secid or isin
    if not _SECID_RE.fullmatch(sec) or board not in ("TQCB", "TQOB"):
        raise HTTPException(status_code=400, detail="bad secid/board")

    candles = await MarketDataService.fetch_candles(sec, "1d", board)
    if not candles:
        return {"isin": isin, "kind": kind, "points": [], "warning": "нет свечей MOEX"}
    candles = candles[-days:]

    from services.orderbook_svc import build_metrics_fn
    try:
        metrics_fn, calc_date, _face = await build_metrics_fn(isin, kind)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")

    memo: dict = {}
    points = []
    for cndl in candles:
        px = cndl.get("c")
        t = cndl.get("t")
        if px is None or not t:
            continue
        m = memo.get(px)
        if m is None:
            try:
                m = metrics_fn(px) or {}
            except Exception:
                m = {}
            memo[px] = m
        points.append({
            "date": t[:10],
            "price": round(px, 3),
            "dm_bps": m.get("dm_bps"),
            "g_spread_bps": m.get("g_spread_bps"),
            "ytm": m.get("yield_pct"),
        })

    return {"isin": isin, "kind": kind, "calc_date": str(calc_date), "points": points}
