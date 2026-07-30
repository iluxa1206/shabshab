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
    est = []
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
        est.append({
            "date": t[:10], "price": round(px, 3),
            "dm_bps": m.get("dm_bps"), "y_idx_bps": m.get("y_idx_bps"),
            "g_spread_bps": m.get("g_spread_bps"),
            "ytm": m.get("yield_pct"), "src": "est",
        })

    # Точная история (дневные снапшоты) приоритетна; candle-оценкой добиваем
    # прошлое ДО первого точного снапшота (иначе график пуст, пока копится).
    from services.spread_history import read_history
    exact_rows = read_history(isin, days=days)
    exact = [{
        "date": r["date"], "price": r.get("price_pct"),
        "dm_bps": r.get("dm_bps"), "y_idx_bps": r.get("y_idx"),
        "g_spread_bps": r.get("g_spread_bps"),
        "ytm": r.get("ytm"), "src": "exact",
    } for r in exact_rows]
    # поля, которых снапшот ещё не писал (y_idx появился 2026-07-30), добиваем
    # candle-оценкой той же даты — иначе Y-IDX-график пуст на старой точной истории
    est_by_date = {p["date"]: p for p in est}
    for p in exact:
        e = est_by_date.get(p["date"])
        if e:
            for k in ("dm_bps", "y_idx_bps", "g_spread_bps", "ytm", "price"):
                if p.get(k) is None and e.get(k) is not None:
                    p[k] = e[k]
    first_exact = exact[0]["date"] if exact else None
    pre = [p for p in est if first_exact is None or p["date"] < first_exact]
    points = sorted(pre + exact, key=lambda x: x["date"])

    return {"isin": isin, "kind": kind, "calc_date": str(calc_date),
            "exact_from": first_exact, "points": points}


_YIDX_BAND = (-1500, 3000)   # тот же бэнд, что у DM в аналитике: мусор стейл/тонких цен
_AGG_TOP_ISSUERS = 8         # линий в режиме «эмитент» (медиана рынка — отдельно)


@router.get("/aggregate/yidx", tags=["History"])
async def yidx_aggregate(
    days: int = Query(91, ge=7, le=400),
    by: str = Query("rating", description="rating | issuer"),
):
    """Динамика медианного Y-IDX по рейтинг-бакетам или топ-эмитентам из точных
    дневных снапшотов spread_daily (флоатеры). Рейтинг/эмитент — текущие из
    реестра (историю атрибутов не храним). История копится вперёд с первого
    снапшота — глубина ограничена exact_from."""
    if by not in ("rating", "issuer"):
        raise HTTPException(status_code=400, detail="by: rating | issuer")
    from datetime import date as _date, timedelta
    from statistics import median as _median
    from services.portfolio_db import _connect
    from services import instruments_registry

    cutoff = (_date.today() - timedelta(days=days)).isoformat()
    with _connect() as c:
        rows = c.execute(
            "SELECT isin, date, y_idx FROM spread_daily "
            "WHERE kind='floater' AND y_idx IS NOT NULL AND date >= ? "
            "ORDER BY date", (cutoff,)).fetchall()
    lo, hi = _YIDX_BAND
    rows = [r for r in rows if lo < r["y_idx"] < hi]
    if not rows:
        return {"by": by, "days": days, "dates": [], "series": [], "exact_from": None}

    uni = {u["isin"]: u for u in instruments_registry.universe_rows()}
    buckets = {"AAA", "AA", "A", "BBB", "BB", "B"}

    def key_of(isin: str):
        u = uni.get(isin)
        if u is None:
            return None
        if by == "rating":
            r = u.get("rating")
            return r if r in buckets else "NR"
        return u.get("emitter_name") or None

    # {key: {date: [y_idx…]}}
    acc: dict = {}
    for r in rows:
        k = key_of(r["isin"])
        if k is None:
            continue
        acc.setdefault(k, {}).setdefault(r["date"], []).append(r["y_idx"])

    if by == "issuer":
        # топ-N эмитентов по числу бумаг с данными (стабильные ликвидные линии)
        def npapers(k):
            return max(len(v) for v in acc[k].values())
        keys = sorted(acc, key=lambda k: (-npapers(k), k))[:_AGG_TOP_ISSUERS]
        # медиана всего рынка — базовая линия сравнения
        mkt: dict = {}
        for k in acc:
            for d, vs in acc[k].items():
                mkt.setdefault(d, []).extend(vs)
        acc = {k: acc[k] for k in keys}
        acc["РЫНОК"] = mkt
    else:
        order = ["AAA", "AA", "A", "BBB", "BB", "B", "NR"]
        acc = {k: acc[k] for k in order if k in acc}

    dates = sorted({d for v in acc.values() for d in v})
    series = [{
        "key": k,
        "points": [{"date": d, "med": round(_median(v[d]), 1), "n": len(v[d])}
                   for d in dates if d in v],
    } for k, v in acc.items()]
    return {"by": by, "days": days, "dates": dates, "series": series,
            "exact_from": dates[0] if dates else None}


@router.get("/{isin}/reprice", tags=["History"])
async def reprice_past(
    isin: str = Path(...),
    d: str = Query(..., alias="date", description="Дата в прошлом, YYYY-MM-DD"),
    price: float = Query(None, ge=1, le=500,
                         description="Чистая цена, % (пусто — close той даты)"),
    board: str = Query("TQCB", description="TQCB корп / TQOB ОФЗ"),
):
    """Калькулятор прошлых периодов: (дата, цена) → SM/DM/y-idx/YTM как-на-дату.
    НКД/номинал — факт MOEX history той даты; кривая — архив котировок (market)
    либо гибрид реализованный-факт+текущая (realized)."""
    from datetime import date as _date
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board not in ("TQCB", "TQOB"):
        raise HTTPException(status_code=400, detail="bad board")
    try:
        dd = _date.fromisoformat(d)
    except ValueError:
        raise HTTPException(status_code=400, detail="Дата: YYYY-MM-DD")

    from services.backdate import load_backdate_ctx, reprice_asof
    from services.exceptions import NotFoundException, CalculationException
    try:
        ctx = await load_backdate_ctx(isin, dd, board)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")
    except CalculationException as e:
        raise HTTPException(status_code=422, detail=str(e))

    px = price if price is not None else ctx["close"]
    if px is None:
        raise HTTPException(status_code=422,
                            detail=f"Нет цены: торгов ≤ {d} не найдено, задайте price")
    m = reprice_asof(ctx, px)
    return {
        "isin": isin, "date": d, "trade_date": ctx["trade_date"],
        "price": px, "close": ctx["close"], "legalclose": ctx["legalclose"],
        "accint": ctx["accrued"], "face_value": ctx["ref_obj"].face_value,
        "base": ctx["ref_obj"].base, "curve_mode": ctx["curve_mode"],
        "metrics": m,
    }


@router.get("/{isin}/spread_honest", tags=["History"])
async def spread_honest(
    isin: str = Path(...),
    days: int = Query(180, ge=10, le=400),
    board: str = Query("TQCB", description="TQCB корп / TQOB ОФЗ"),
):
    """Честная динамика спредов: каждый день пересчитан своим calc_date, своей
    as-of кривой и фактическими НКД/номиналом/close (MOEX history)."""
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board not in ("TQCB", "TQOB"):
        raise HTTPException(status_code=400, detail="bad board")
    from services.backdate import honest_spread_series
    from services.exceptions import NotFoundException, CalculationException
    try:
        return await honest_spread_series(isin, days, board)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")
    except CalculationException as e:
        raise HTTPException(status_code=422, detail=str(e))
