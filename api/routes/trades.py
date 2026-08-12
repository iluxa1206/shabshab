"""Вкладка СДЕЛКИ — единая лента сделок рынка.

Два архива под капотом, для пользователя — одна лента (склейка и её правила:
services/tape):
  • тиковый архив Alor — все безадресные сделки любого размера по юниверсу;
  • крупные сделки всего рынка из ISS — от 1 млн ₽, включая адресные режимы
    (РПС, РПС с ЦК, размещения, выкупы), которых в стакане нет вообще.

Сюда в Alor мы не ходим: массовый онлайн-дрейн по 500+ бумагам на запрос
страницы упёрся бы в rate-limit брокера. Обе таблицы наливают фоновые демоны
(hourly_bars_worker и block_trades_worker), поэтому лента отстаёт от биржи не
больше, чем на их такт.

Глубина: за сырым окном тик-архива (≈35 дней) ретеншен оставляет только принты
от 1 млн ₽, так что дальний конец ленты — крупные сделки.
"""
import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.routes.blocks import (BOARD_TITLES, SCOPES, _labels, _moex_names, _scope_isins,
                               board_short)

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def _ttm_isins(labels: dict, isins: Optional[list[str]],
               ttm_min: Optional[float], ttm_max: Optional[float]) -> Optional[list[str]]:
    """Сузить охват сроком до погашения (годы). Бумаги без даты погашения в
    справочниках под такой фильтр не попадают — срок у них неизвестен, а не
    «любой»; при scope=market это отсекает всё, чего нет в наших справочниках."""
    if ttm_min is None and ttm_max is None:
        return isins
    today = date.today()
    keep = []
    pool = isins if isins is not None else labels.keys()
    for i in pool:
        md = (labels.get(i) or {}).get("maturity")
        if not md:
            continue
        try:
            yrs = (date.fromisoformat(str(md)[:10]) - today).days / 365.25
        except ValueError:
            continue
        if ttm_min is not None and yrs < ttm_min:
            continue
        if ttm_max is not None and yrs > ttm_max:
            continue
        keep.append(i)
    return keep


@router.get("", tags=["Trades"])
async def tape(
    days: int = Query(1, ge=1, le=400, description="окно в календарных днях назад"),
    min_value: float = Query(0, ge=0, description="порог суммы сделки, ₽"),
    side: Optional[str] = Query(None, description="buy | sell (агрессор)"),
    market: Optional[str] = Query(None, description="bonds (безадресные) | ndm (адресные)"),
    board: Optional[list[str]] = Query(None, description="борды MOEX (можно повторять)"),
    scope: str = Query("market", description="market | universe | float | fixed"),
    issuer: Optional[list[str]] = Query(None, description="эмитенты (можно повторять параметр)"),
    isin: Optional[str] = Query(None, description="одна бумага"),
    spread_min: Optional[float] = Query(None, description="R-spread сделки от, бп"),
    spread_max: Optional[float] = Query(None, description="R-spread сделки до, бп"),
    ttm_min: Optional[float] = Query(None, ge=0, description="срок до погашения от, лет"),
    ttm_max: Optional[float] = Query(None, ge=0, description="срок до погашения до, лет"),
    limit: int = Query(500, ge=1, le=20000),
):
    """{trades, summary} — лента рынка, новые сверху, с именем бумаги и эмитентом."""
    if side is not None and side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side: buy | sell")
    if market is not None and market not in ("bonds", "ndm"):
        raise HTTPException(status_code=400, detail="market: bonds | ndm")
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope: {' | '.join(SCOPES)}")
    isin = (isin or "").strip().upper() or None
    if isin and not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")

    from services import tape as tape_svc

    # SQLite тут синхронный, а агрегат ленты на миллионах тиков меряется
    # секундами (замер на проде: до 2.2с) — в event loop это встало бы ВСЁ
    # приложение, включая WS-пуши. Отсюда и ниже — только через to_thread.
    labels = await asyncio.to_thread(_labels)
    isins: Optional[list[str]] = None
    if isin:
        isins = [isin]
    elif issuer:
        want = {e for e in issuer if e}
        isins = [k for k, v in labels.items() if (v.get("emitter") or "") in want]
        if not isins:
            return {"from": None, "trades": [], "scope": scope,
                    "summary": {"n": 0, "value": 0, "buy_value": 0, "sell_value": 0,
                                "by_market": {}, "top": [], "archive_till": None},
                    "warning": "по эмитенту нет бумаг в реестре"}
    else:
        # список любой длины: большой уезжает во временную таблицу внутри
        # services/tape — плейсхолдерами 1300+ флоатеров не поместились бы
        isins = _scope_isins(scope, labels)
        if isins is not None and not isins:
            return {"from": None, "trades": [], "scope": scope,
                    "summary": {"n": 0, "value": 0, "buy_value": 0, "sell_value": 0,
                                "by_market": {}, "top": [], "archive_till": None},
                    "warning": "справочники ещё не прогреты — охват пуст"}

    if not isin:
        isins = _ttm_isins(labels, isins, ttm_min, ttm_max)
        if isins is not None and not isins:
            return {"from": None, "trades": [], "scope": scope,
                    "summary": {"n": 0, "value": 0, "buy_value": 0, "sell_value": 0,
                                "by_market": {}, "top": [], "archive_till": None},
                    "warning": "под фильтр срока до погашения бумаг нет"}

    frm = (date.today() - timedelta(days=days - 1)).isoformat()
    # строки и агрегат независимы — читаем параллельно (WAL допускает
    # конкурентных читателей), ответ приходит за время медленного из двух
    rows, summary = await asyncio.gather(
        asyncio.to_thread(tape_svc.read_tape, frm=frm, min_value=min_value, side=side,
                          market=market, boards=board, isins=isins, limit=limit,
                          y_min=spread_min, y_max=spread_max),
        asyncio.to_thread(tape_svc.tape_stats, frm=frm, min_value=min_value, side=side,
                          market=market, boards=board, isins=isins,
                          y_min=spread_min, y_max=spread_max))
    moex = await asyncio.to_thread(_moex_names)
    # Y-IDX приезжает готовым из архива (считает демон при приходе сделки, см.
    # block_trades.price_new_trades): цена в % номинала между выпусками
    # несравнима, спред к индексу — сравним. Считать здесь нельзя: прогрев
    # контекстов по сотне выпусков занимает минуту на первом запросе.
    priced = sum(1 for r in rows if r.get("y_idx_bps") is not None)

    for r in rows:
        lb = labels.get(r["isin"]) or {}
        r["name"] = lb.get("name") or moex.get(r["isin"]) or r["isin"]
        r["emitter"] = lb.get("emitter")
        r["base"] = lb.get("base")
        r["rating"] = lb.get("rating")
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
        r["board_short"] = board_short(r.get("board"))
        r["maturity"] = lb.get("maturity")
    for t in summary.get("top") or []:
        lb = labels.get(t["isin"]) or {}
        t["name"] = lb.get("name") or moex.get(t["isin"]) or t["isin"]
        t["emitter"] = lb.get("emitter")

    return {"from": frm, "days": days, "min_value": min_value, "side": side,
            "market": market, "board": board, "scope": scope,
            "truncated": len(rows) >= limit and summary["n"] > len(rows),
            "y_idx_rows": priced,      # по скольким строкам спред посчитан
            "trades": rows, "summary": summary}


@router.get("/boards", tags=["Trades"])
async def boards(days: int = Query(30, ge=1, le=400)):
    """Режимы торгов, реально встречавшиеся в окне — источник опций фильтра.
    Считаем по слою крупных сделок: только он знает адресные борды."""
    from services import block_trades as bt
    rows = await asyncio.to_thread(bt.boards_seen, days)
    for r in rows:
        r["title"] = BOARD_TITLES.get(r["board"] or "", r["board"])
    return {"days": days, "boards": rows}


@router.get("/issuers", tags=["Trades"])
async def issuers():
    """Эмитенты для фильтра ленты — из справочников, а не из выборки сделок
    (иначе список прыгал бы при каждой смене окна)."""
    counts: dict[str, int] = {}
    for v in (await asyncio.to_thread(_labels)).values():
        em = v.get("emitter")
        if em:
            counts[em] = counts.get(em, 0) + 1
    items = [{"name": k, "count": v} for k, v in counts.items()]
    items.sort(key=lambda x: (-x["count"], x["name"]))
    return {"issuers": items}
