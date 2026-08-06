"""Динамика спредов: историческая серия DM (флоатер) / g-спреда (фикс) по
дневным закрытиям MOEX. reprice каждой историч. цены под ТЕКУЩИЙ контекст
(accrued/curve/calc_date фиксированы) → НКД-пилы нет, серия = функция цены через
сегодняшнюю модель. Оценка динамики, не точный историч. спред (кривая/срок
менялись) — но для тренда достаточно. Без хранилища, on-demand."""
import asyncio
import re
import logging
from typing import Optional
from fastapi import APIRouter, Path, Query, HTTPException

from services.market_data import MarketDataService
from services.exceptions import NotFoundException

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_SECID_RE = re.compile(r"[A-Z0-9]{4,14}")
# Борды, где живут бумаги юниверса: корп TQCB, ОФЗ TQOB, риск-сектор TQRD.
_BOARDS = ("TQCB", "TQOB", "TQRD")


@router.get("/{isin}/spread", tags=["History"])
async def spread_history(
    isin: str = Path(...),
    kind: str = Query("floater", description="floater | fixed"),
    secid: str = Query(None, description="SECID (ОФЗ ≠ ISIN); пусто — резолв по ISIN"),
    board: str = Query(None, description="TQCB корп / TQOB ОФЗ / TQRD риск-сектор; пусто — авто"),
    days: int = Query(120, ge=10, le=400),
    from_date: Optional[str] = Query(
        None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Календарная граница окна (ISO). Задана — days игнорируется: "
                    "серия режется по дате, как график цены. Нужна, чтобы окна "
                    "цены и спреда в карточке совпадали (30 календарных дней ≠ "
                    "30 торговых)."),
):
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board is not None and board not in _BOARDS:
        raise HTTPException(status_code=400, detail="bad board")
    # ОФЗ: свечи/history живут на тикере SU29… и борде TQOB — по ISIN@TQCB пусто
    if not secid or not board:
        from services.backdate import resolve_market
        auto_sec, auto_board = await resolve_market(isin, board)
        secid, board = secid or auto_sec, board or auto_board
    sec = secid or isin
    if not _SECID_RE.fullmatch(sec):
        raise HTTPException(status_code=400, detail="bad secid")

    candles = await MarketDataService.fetch_candles(sec, "1d", board)
    if not candles:
        return {"isin": isin, "kind": kind, "points": [], "warning": "нет свечей MOEX"}
    if from_date:
        # календарное окно: число торговых точек в нём и есть эффективный days
        # (им дальше режется бэкфилл и чтение точной истории)
        candles = [c for c in candles if str(c.get("t", ""))[:10] >= from_date]
        if not candles:
            return {"isin": isin, "kind": kind, "points": [], "warning": "нет сделок за период"}
        days = max(10, min(400, len(candles)))
    else:
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

    # Флоатер: прошлое досчитывается ЧЕСТНЫМ движком (as-of кривая/НКД/номинал
    # каждого дня) и персистится в spread_daily (src='honest'); вечерние
    # снапшоты (src='snap') авторитетнее и не перезаписываются. Первый запрос
    # по бумаге считает ~120 точек (десятки секунд), дальше — мгновенно из базы.
    if kind == "floater":
        try:
            from services.backdate import ensure_honest_backfill
            await ensure_honest_backfill(isin, days, board)
        except Exception as e:
            logger.warning(f"honest backfill {isin}: {e} — серия останется candle-оценкой")

    from services.spread_history import read_history, keep_trade_days
    exact_rows = await asyncio.to_thread(read_history, isin, days=days)
    if from_date:
        # read_history режет по числу строк — календарную границу держим здесь,
        # иначе точная история могла бы уйти левее окна графика цены
        exact_rows = [r for r in exact_rows if str(r.get("date", "")) >= from_date]
    # ДНИ БЕЗ СДЕЛОК НЕ ПРАЙСИМ: точка спреда рисуется только там, где есть
    # свеча. Иначе снапшот/бэкфилл добавляли даты без торгов (цена — стейл
    # prev-close), график спреда шёл по календарю, а график цены — по сделкам.
    _trade_days = {p["date"] for p in est}
    exact_rows, skipped_no_trades = keep_trade_days(exact_rows, _trade_days)
    exact = [{
        "date": r["date"], "price": r.get("price_pct"),
        "dm_bps": r.get("dm_bps"), "y_idx_bps": r.get("y_idx"),
        "g_spread_bps": r.get("g_spread_bps"),
        "ytm": r.get("ytm"),
        "src": "honest" if r.get("src") == "honest" else "exact",
    } for r in exact_rows]
    # поля, которых в строке нет (напр. y_idx у легаси-снапшота при упавшем
    # бэкфилле), добиваем candle-оценкой той же даты — график не пустеет
    est_by_date = {p["date"]: p for p in est}
    for p in exact:
        e = est_by_date.get(p["date"])
        if e:
            for k in ("dm_bps", "y_idx_bps", "g_spread_bps", "ytm", "price"):
                if p.get(k) is None and e.get(k) is not None:
                    p[k] = e[k]
    # est — только хвосты вне точного окна: до первой точной даты (бэкфилл не
    # покрыл/сломался) и после последней (сегодняшний live до вечернего
    # снапшота, выходные сессии). Est-точки МЕЖДУ точными (выходные свечи)
    # не подмешиваем — две модели в одной линии и были причиной «расхождения».
    first_exact = exact[0]["date"] if exact else None
    last_exact = exact[-1]["date"] if exact else None
    pre = [p for p in est if first_exact is None or p["date"] < first_exact]
    post = [p for p in est if last_exact is not None and p["date"] > last_exact]
    points = sorted(pre + exact + post, key=lambda x: x["date"])

    return {"isin": isin, "kind": kind, "calc_date": str(calc_date),
            "exact_from": first_exact, "points": points,
            # сколько дней истории отброшено как неторговые (диагностика: у
            # неликвида это может быть половина календаря)
            "skipped_no_trades": skipped_no_trades}


_bar_locks: dict = {}        # per-ISIN, чтобы параллельные запросы не дублировали налив


def _bar_lock(isin: str):
    import asyncio
    lock = _bar_locks.get(isin)
    if lock is None:
        lock = _bar_locks[isin] = asyncio.Lock()
    return lock


@router.get("/{isin}/bars", tags=["History"])
async def hourly_bars(
    isin: str = Path(...),
    kind: str = Query("floater", description="floater | fixed"),
    days: int = Query(30, ge=1, le=730),
    hours: int = Query(1, ge=1, le=24, description="склейка часов в N-часовой бар"),
    board: str = Query(None, description="TQCB / TQOB / TQRD; пусто — авто"),
    refresh: bool = Query(True, description="дотянуть свежие свечи/сделки перед ответом"),
    with_ticks: bool = Query(True, description="стороны сделок (buy/sell VWAP) из тиков"),
):
    """Часовые бары: средневзвешенная цена часа (VWAP) и спред по ней.

    Цена — часовые свечи MOEX (value/volume/номинал), спред — reprice VWAP через
    текущую модель бумаги: уровень серии оценочный, форма движения честная.
    buy_vwap/sell_vwap — из тикового архива Alor (агрессор), глубина ~30 дней
    назад от первого запуска демона, дальше копится нашим архивом."""
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board is not None and board not in _BOARDS:
        raise HTTPException(status_code=400, detail="bad board")
    if kind not in ("floater", "fixed"):
        raise HTTPException(status_code=400, detail="kind: floater | fixed")

    from datetime import date as _date, timedelta as _td
    from services import bars as bars_svc
    frm = (_date.today() - _td(days=days)).isoformat()

    if refresh:
        async with _bar_lock(isin):
            try:
                await bars_svc.ensure_bars(isin, days=days, kind=kind, board=board)
            except NotFoundException:
                raise HTTPException(status_code=404, detail="Bond not found")
            except Exception as e:
                logger.warning("bars refresh %s: %s", isin, e)
            if with_ticks:
                try:
                    from services import trades_archive as ta
                    await ta.drain(isin, days=min(days, ta.ALOR_HISTORY_DAYS), board=board)
                    # GROUP BY по тикам бумаги — синхронный SQLite: в event loop
                    # он держал бы весь сервер на время агрегации
                    await asyncio.to_thread(ta.enrich_bars_with_ticks, isin, frm=frm)
                except Exception as e:
                    logger.warning("ticks %s: %s", isin, e)

    rows = await asyncio.to_thread(bars_svc.read_bars, isin, frm=frm)
    if hours > 1:
        rows = bars_svc.resample(rows, hours)
    return {"isin": isin, "kind": kind, "hours": hours, "from": frm,
            "bars": rows, "n": len(rows)}


@router.get("/{isin}/trades", tags=["History"])
async def trades(
    isin: str = Path(...),
    days: int = Query(7, ge=1, le=400),
    min_value: float = Query(0, ge=0, description="порог в рублях — фильтр крупных"),
    side: str = Query(None, description="buy | sell (агрессор)"),
    limit: int = Query(500, ge=1, le=5000),
    order: str = Query("ts", pattern="^(ts|value)$",
                       description="ts — последние по времени, value — самые крупные за окно"),
    refresh: bool = Query(True),
    board: str = Query(None),
):
    """Сделки из тикового архива: цена, объём, рублёвый оборот, агрессор.
    min_value отсекает мелочь — остаются крупные принты."""
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if side is not None and side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side: buy | sell")
    from datetime import date as _date, timedelta as _td
    from services import trades_archive as ta
    frm = (_date.today() - _td(days=days)).isoformat()
    if refresh:
        async with _bar_lock(isin):
            try:
                await ta.drain(isin, days=min(days, ta.ALOR_HISTORY_DAYS), board=board)
            except Exception as e:
                logger.warning("trades drain %s: %s", isin, e)
    rows = await asyncio.to_thread(ta.read_trades, isin, frm=frm, min_value=min_value,
                                   side=side, limit=limit, order=order)
    # сколько сделок под фильтр вообще подходит: без этого клиент не отличает
    # «столько и было» от «лимит срезал остальное»
    total = await asyncio.to_thread(ta.count_trades, isin, frm, min_value, side)

    def _vwap(rs):
        q = sum(r.get("qty") or 0 for r in rs)
        return round(sum((r["price"] or 0) * (r["qty"] or 0) for r in rs) / q, 4) if q else None

    buys = [r for r in rows if r.get("side") == "buy"]
    sells = [r for r in rows if r.get("side") == "sell"]
    vb, vs = _vwap(buys), _vwap(sells)
    return {"isin": isin, "from": frm, "min_value": min_value, "n": len(rows),
            "total": total, "truncated": total > len(rows), "order": order,
            "vwap_pct": _vwap(rows), "buy_vwap": vb, "sell_vwap": vs,
            # эффективный спред по агрессору, б.п. цены (100 б.п. = 1 п.п. цены)
            "eff_spread_bps": round((vb - vs) * 100, 1) if vb is not None and vs is not None else None,
            "volume": sum(r.get("qty") or 0 for r in rows),
            "value": sum(r.get("value") or 0 for r in rows),
            "archive": await asyncio.to_thread(ta.stats, isin), "trades": rows}


_YIDX_BAND = (-1500, 3000)   # тот же бэнд, что у DM в аналитике: мусор стейл/тонких цен
_AGG_TOP_ISSUERS = 8         # линий в режиме «эмитент» (медиана рынка — отдельно)


from pydantic import BaseModel, Field


class YidxAggBody(BaseModel):
    days: int = Field(91, ge=7, le=400)
    by: str = "rating"
    # ISIN-ы отфильтрованного набора таблицы (★/база/рейтинг/эмитент/поиск):
    # агрегируем только по ним — график согласован с остальной аналитикой.
    # None/пусто — весь юниверс.
    isins: Optional[list[str]] = None


@router.post("/aggregate/yidx", tags=["History"])
async def yidx_aggregate(body: YidxAggBody):
    """Динамика медианного Y-IDX по рейтинг-бакетам или топ-эмитентам из дневных
    строк spread_daily (точные снапшоты + candle-бэкфилл). Рейтинг/эмитент —
    текущие из реестра (историю атрибутов не храним)."""
    days, by = body.days, body.by
    if by not in ("rating", "issuer"):
        raise HTTPException(status_code=400, detail="by: rating | issuer")
    from datetime import date as _date, timedelta
    from statistics import median as _median
    from services.portfolio_db import _connect
    from services import instruments_registry

    cutoff = (_date.today() - timedelta(days=days)).isoformat()

    def _read_daily():
        with _connect() as c:
            return c.execute(
                "SELECT isin, date, y_idx FROM spread_daily "
                "WHERE kind='floater' AND y_idx IS NOT NULL AND date >= ? "
                "ORDER BY date", (cutoff,)).fetchall()

    rows = await asyncio.to_thread(_read_daily)
    lo, hi = _YIDX_BAND
    want = {i.strip().upper() for i in body.isins if _ISIN_RE.fullmatch(i.strip().upper())} \
        if body.isins else None
    rows = [r for r in rows if lo < r["y_idx"] < hi
            and (want is None or r["isin"] in want)]
    if not rows:
        return {"by": by, "days": days, "dates": [], "series": [], "exact_from": None}

    uni = {u["isin"]: u for u in await asyncio.to_thread(instruments_registry.universe_rows)}
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
    board: str = Query(None, description="TQCB / TQOB / TQRD; пусто — авто по ISIN"),
):
    """Калькулятор прошлых периодов: (дата, цена) → SM/DM/y-idx/YTM как-на-дату.
    НКД/номинал — факт MOEX history той даты; кривая — архив котировок (market)
    либо гибрид реализованный-факт+текущая (realized)."""
    from datetime import date as _date
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board is not None and board not in _BOARDS:
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

    # цена по умолчанию — close даты; сделок не было → официальный legalclose
    px = price if price is not None else (ctx["close"] if ctx["close"] is not None
                                          else ctx["legalclose"])
    if px is None:
        raise HTTPException(status_code=422,
                            detail=f"Нет цены: торгов ≤ {d} не найдено, задайте price")
    m = reprice_asof(ctx, px)
    price_src = ("user" if price is not None
                 else ("close" if ctx["close"] is not None else "legalclose"))
    return {
        "isin": isin, "date": d, "trade_date": ctx["trade_date"],
        "price": px, "price_src": price_src,
        "close": ctx["close"], "legalclose": ctx["legalclose"],
        "stale_days": ctx["stale_days"], "secid": ctx["secid"], "board": ctx["board"],
        "accint": ctx["accrued"], "face_value": ctx["ref_obj"].face_value,
        "base": ctx["ref_obj"].base, "curve_mode": ctx["curve_mode"],
        "metrics": m,
    }


@router.get("/{isin}/spread_honest", tags=["History"])
async def spread_honest(
    isin: str = Path(...),
    days: int = Query(180, ge=10, le=400),
    board: str = Query(None, description="TQCB / TQOB / TQRD; пусто — авто по ISIN"),
):
    """Честная динамика спредов: каждый день пересчитан своим calc_date, своей
    as-of кривой и фактическими НКД/номиналом/close (MOEX history)."""
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if board is not None and board not in _BOARDS:
        raise HTTPException(status_code=400, detail="bad board")
    from services.backdate import honest_spread_series
    from services.exceptions import NotFoundException, CalculationException
    try:
        return await honest_spread_series(isin, days, board)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")
    except CalculationException as e:
        raise HTTPException(status_code=422, detail=str(e))
