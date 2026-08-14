"""Динамика спредов: историческая серия DM (флоатер) / g-спреда (фикс) по
дневным закрытиям MOEX. reprice каждой историч. цены под ТЕКУЩИЙ контекст
(accrued/curve/calc_date фиксированы) → НКД-пилы нет, серия = функция цены через
сегодняшнюю модель. Оценка динамики, не точный историч. спред (кривая/срок
менялись) — но для тренда достаточно. Без хранилища, on-demand."""
import asyncio
import math
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
            "y_idx_alt_bps": m.get("y_idx_alt_bps"), "horizon": m.get("horizon"),
            "alt_horizon": m.get("alt_horizon"),
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
        # спред ко второму горизонту — для свитчера «погашение ↔ оферта»
        "y_idx_alt_bps": r.get("y_idx_alt"), "horizon": r.get("horizon"),
        "alt_horizon": r.get("alt_horizon"),
        "src": "honest" if r.get("src") == "honest" else "exact",
    } for r in exact_rows]
    # поля, которых в строке нет (напр. y_idx у легаси-снапшота при упавшем
    # бэкфилле), добиваем candle-оценкой той же даты — график не пустеет
    est_by_date = {p["date"]: p for p in est}
    for p in exact:
        e = est_by_date.get(p["date"])
        if e:
            for k in ("dm_bps", "y_idx_bps", "g_spread_bps", "ytm", "price",
                      "y_idx_alt_bps", "horizon", "alt_horizon"):
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

    Цена — часовые свечи MOEX (value/volume/номинал). Спред: сегодня — живая
    модель, прошлые дни — честный as-of (кривая/НКД/номинал того дня); пересчёт
    прошлого идёт фоном, до его конца спред старых баров пуст (панель падает на
    дневную honest-серию). buy_vwap/sell_vwap — из тикового архива Alor
    (агрессор), глубина ~30 дней от первого запуска демона, дальше наш архив."""
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


async def _price_trades(isin: str, rows: list, kind: str,
                        days: int = 0, board: Optional[str] = None) -> int:
    """Проставляет строкам сделок y_idx_bps/dm_bps/ytm (у фикса — g_spread_bps)
    по ЦЕНЕ САМОЙ СДЕЛКИ. Возвращает число заполненных строк; при любой ошибке
    модели молча уходит ни с чем — маркеры сделок важнее спреда к ним.

    ПРОШЛЫЕ СЕССИИ — ЧЕСТНЫМ AS-OF (кривая/НКД/номинал того дня), тем же
    движком, что рисует линию спреда рядом. Раньше всё считалось сегодняшней
    моделью, и маркер расходился с линией на сотни bps там, где дюрация мала
    (замер: Магнит5Р03 03.08 — маркер −328 против линии −29). Сегодняшние
    сделки считаются живой моделью: as-of на текущий день не определён.
    Сборка as-of стоит доли секунды на тёплых кэшах; не собралась (бумага
    только размещена / нет истории) — молча остаёмся на живой модели."""
    from datetime import date as _date
    try:
        from services.orderbook_svc import build_metrics_fn
        metrics_fn, _cd, _face = await build_metrics_fn(isin, kind)
    except Exception as e:
        logger.info("trades pricing %s: модель недоступна (%s)", isin, e)
        return 0
    asof_fn = None
    if kind == "floater" and days > 1:
        try:
            from services.backdate import asof_bar_metrics
            asof_fn = await asof_bar_metrics(isin, days, board)
        except Exception as e:
            logger.info("trades pricing %s: as-of недоступен (%s) — прошлые дни "
                        "оценочно сегодняшней моделью", isin, e)
    today_iso = _date.today().isoformat()
    memo: dict = {}

    def _m(price, day=None):
        k = (day, round(float(price), 3))
        if k not in memo:
            try:
                memo[k] = ((asof_fn(day, k[1]) if day else metrics_fn(k[1])) or {})
            except Exception:
                memo[k] = {}
        return memo[k]

    n = 0
    for r in rows:
        if r.get("price") is None:
            continue
        day = str(r.get("ts") or "")[:10]
        past = bool(asof_fn) and day and day < today_iso
        m = await asyncio.to_thread(_m, r["price"], day if past else None)
        if not m and past:            # as-of споткнулся на дне — живая модель
            m = await asyncio.to_thread(_m, r["price"], None)
        if not m:
            continue
        for src, dst in (("y_idx_bps", "y_idx_bps"), ("dm_bps", "dm_bps"),
                         ("g_spread_bps", "g_spread_bps"), ("yield_pct", "ytm")):
            v = m.get(src)
            if v is not None:
                r[dst] = round(v, 2 if dst == "ytm" else 0)
        n += 1
    return n


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
    kind: str = Query("floater", pattern="^(floater|fixed)$"),
):
    """Сделки из тикового архива: цена, объём, рублёвый оборот, агрессор, спред.
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
    # ОБА АРХИВА, склейка по TRADENO (services/tape): тиковый начинается с
    # первого дрейна по бумаге, а ISS-лента ловит весь рынок — раньше слой не
    # видел крупных сделок за дни до старта дрейна (ОФЗ 29010: принты на 37 и
    # 49 млн ₽ 11.08). Адресные исключены: их рисует отдельный слой РПС.
    from services import tape as tape_svc
    rows, total = await asyncio.gather(
        asyncio.to_thread(tape_svc.read_isin_trades, isin, frm=frm,
                          min_value=min_value, side=side, limit=limit, order=order),
        # сколько сделок под фильтр вообще подходит: без этого клиент не отличает
        # «столько и было» от «лимит срезал остальное»
        asyncio.to_thread(tape_svc.count_isin_trades, isin, frm, None, min_value, side))
    # Спред КАЖДОЙ сделки — тем же reprice, что уровни стакана и бары: маркер
    # крупного принта без спреда заставлял считать в уме «дорого или дёшево он
    # взял». Модель выпуска строится один раз, дальше reprice по цене без I/O,
    # ответы мемоизируем — уникальных цен на сотню принтов десятки.
    if rows:
        await _price_trades(isin, rows, kind, days=days, board=board)

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
# Порог покрытия дня: медиана по 2 бумагам из 130 — не медиана бакета, а пик-артефакт.
# Опора — p95 числа бумаг в дне у самой серии (не медиана: покрытие бимодально,
# у ранних дат бэкфилл даёт 1-2 бумаги на сотню полных дней, медиана съезжает в 1).
# Порог относительный, поэтому мелкие серии живут: у эмитента с одной бумагой
# p95 = 1 → порог 1, линия остаётся целиком.
_AGG_MIN_N_FRAC = 0.5


from pydantic import BaseModel, Field


def _one_horizon(rows, lo: float, hi: float) -> list[dict]:
    """ОДИН ГОРИЗОНТ НА ВСЮ ЛИНИЮ. Горизонт бумаги меняется во времени (появилась
    дата колла, цена перешла порог выкупа), а строка архива хранит спред к тому
    горизонту, что действовал в её день: СибурХ1Р06 12.08.2026 переключился с
    погашения (5,6 г) на колл (0,3 г) и линия обвалилась на 220 б.п. без движения
    цены. Берём ветку, совпадающую с ПОСЛЕДНИМ известным горизонтом бумаги
    (сегодняшняя таблица считает по нему же), при несовпадении — второй горизонт
    из той же строки. Нет ни того, ни другого (легаси-строки до появления колонок —
    лечатся scripts/backfill_horizon.py) — точку отбрасываем, линия рвётся вместо
    обвала. rows должны идти по возрастанию даты."""
    cur_hz: dict = {}
    for r in rows:
        if r["horizon"]:
            cur_hz[r["isin"]] = r["horizon"]
    out = []
    for r in rows:
        hz = cur_hz.get(r["isin"])
        v = r["y_idx"]
        if hz is not None:
            if r["horizon"] == hz:
                v = r["y_idx"]
            elif r["alt_horizon"] == hz and r["y_idx_alt"] is not None:
                v = r["y_idx_alt"]
            else:
                continue
        if v is None or not (lo < v < hi):
            continue
        out.append({"isin": r["isin"], "date": r["date"], "y_idx": v,
                    "price": r["price_pct"] if "price_pct" in r.keys() else None})
    return out


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
                "SELECT isin, date, y_idx, y_idx_alt, horizon, alt_horizon FROM spread_daily "
                "WHERE kind='floater' AND y_idx IS NOT NULL AND date >= ? "
                "ORDER BY date", (cutoff,)).fetchall()

    rows = await asyncio.to_thread(_read_daily)
    lo, hi = _YIDX_BAND
    want = {i.strip().upper() for i in body.isins if _ISIN_RE.fullmatch(i.strip().upper())} \
        if body.isins else None
    rows = [r for r in rows if want is None or r["isin"] in want]
    rows = _one_horizon(rows, lo, hi)
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

    def _p95(ns: list[int]) -> int:
        s = sorted(ns)
        return s[min(len(s) - 1, math.ceil(len(s) * 0.95) - 1)]

    # тонкие дни вон: порог = доля от полного (p95) покрытия самой серии
    series = []
    for k, v in acc.items():
        n_min = max(1, math.ceil(_p95([len(x) for x in v.values()]) * _AGG_MIN_N_FRAC))
        pts = [{"date": d, "med": round(_median(vs), 1), "n": len(vs)}
               for d, vs in sorted(v.items()) if len(vs) >= n_min]
        if pts:
            series.append({"key": k, "points": pts, "n_min": n_min})

    dates = sorted({p["date"] for s in series for p in s["points"]})
    return {"by": by, "days": days, "dates": dates, "series": series,
            "exact_from": dates[0] if dates else None}


# Потолок линий вкладки СРАВНЕНИЕ. Цветом различаются первые десять, остальные
# идут серым фоном — предел здесь про размер ответа и читаемость графика, не про
# палитру (фронт держит тот же CMP_MAX).
_CMP_MAX_ISINS = 20


class MultiSpreadBody(BaseModel):
    days: int = Field(91, ge=7, le=400)
    isins: list[str] = Field(default_factory=list)
    # база дня: 'close' — вечерний снапшот/бэкфилл из spread_daily (глубокая
    # история), 'vwap' — средневзвешенная цена дня из часовых баров и спред по
    # ней (честнее для неликвида, но глубина = архиву баров, он копится с
    # августа 2026).
    base: str = "close"


@router.post("/multi/spread", tags=["History"])
async def multi_spread(body: MultiSpreadBody):
    """Сырые дневные ряды Y-IDX и цены по нескольким выпускам — одна линия на
    бумагу (вкладка СРАВНЕНИЕ). Только то, что уже лежит в базе: ни
    candle-оценки, ни honest-бэкфилла по запросу — восемь параллельных
    бэкфиллов вешают воркер на десятки секунд. Дыры в покрытии видны как более
    короткая линия."""
    if body.base not in ("close", "vwap"):
        raise HTTPException(status_code=400, detail="base: close | vwap")
    isins, seen = [], set()
    for raw in body.isins:
        i = (raw or "").strip().upper()
        if _ISIN_RE.fullmatch(i) and i not in seen:
            seen.add(i)
            isins.append(i)
    isins = isins[:_CMP_MAX_ISINS]
    if not isins:
        return {"days": body.days, "base": body.base, "series": [], "dates": [],
                "exact_from": None}

    from datetime import date as _date, timedelta
    from services.portfolio_db import _connect

    cutoff = (_date.today() - timedelta(days=body.days)).isoformat()
    ph = ",".join("?" * len(isins))
    lo, hi = _YIDX_BAND
    acc: dict = {i: [] for i in isins}

    if body.base == "close":
        def _read():
            with _connect() as c:
                return c.execute(
                    "SELECT isin, date, price_pct, y_idx, y_idx_alt, horizon, alt_horizon "
                    f"FROM spread_daily WHERE kind='floater' AND date >= ? AND isin IN ({ph}) "
                    "ORDER BY date", (cutoff, *isins)).fetchall()

        rows = await asyncio.to_thread(_read)
        for r in _one_horizon(rows, lo, hi):
            acc[r["isin"]].append({"date": r["date"], "y_idx": round(r["y_idx"], 1),
                                   "price": r["price"]})
    else:
        # день из часовых баров: цена — VWAP дня (взвешивание по обороту в
        # рублях, как внутри часа), спред — тем же весом по y_idx часа. Часы без
        # оборота (бар из свечи без сделок) в вес не идут.
        def _read_bars():
            with _connect() as c:
                return c.execute(
                    "SELECT isin, substr(ts,1,10) AS date, vwap_pct, y_idx_bps, value "
                    f"FROM bar_hourly WHERE kind='floater' AND substr(ts,1,10) >= ? "
                    f"AND isin IN ({ph}) ORDER BY ts", (cutoff, *isins)).fetchall()

        rows = await asyncio.to_thread(_read_bars)
        agg: dict = {}
        for r in rows:
            w = r["value"] or 0
            if w <= 0 or r["vwap_pct"] is None:
                continue
            a = agg.setdefault((r["isin"], r["date"]), [0.0, 0.0, 0.0, 0.0])
            a[0] += r["vwap_pct"] * w
            a[1] += w
            if r["y_idx_bps"] is not None:
                a[2] += r["y_idx_bps"] * w
                a[3] += w
        for (isin, date), (pw, w, yw, yws) in sorted(agg.items(), key=lambda kv: kv[0][1]):
            y = yw / yws if yws > 0 else None
            if y is not None and not (lo < y < hi):
                y = None
            acc[isin].append({"date": date, "y_idx": round(y, 1) if y is not None else None,
                              "price": round(pw / w, 4)})

    # порядок серий — как прислал клиент: цвет линии закреплён за позицией выбора
    series = [{"isin": i, "points": acc[i]} for i in isins if acc[i]]
    dates = sorted({p["date"] for s in series for p in s["points"]})
    return {"days": body.days, "base": body.base, "series": series, "dates": dates,
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
