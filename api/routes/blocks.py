"""Вкладка КРУПНЫЕ — лента крупных сделок по всем облигациям MOEX.

Отличие от /api/trades: там обезличенная лента из тик-архива Alor (только
безадресные борды и только юниверс реестра), здесь — сделки от порога по
ВСЕМУ рынку облигаций, включая адресные режимы (РПС, РПС с ЦК, размещения,
выкупы). Источник и ограничения — см. services/block_trades.
"""
import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

# Человеческие названия режимов: BOARDID сам по себе трейдеру ничего не говорит.
BOARD_TITLES = {
    "TQCB": "Безадресный: корп.", "TQOB": "Безадресный: ОФЗ",
    "TQRD": "Безадресный: Д-облигации", "TQOD": "Безадресный: USD",
    "TQOY": "Безадресный: CNY", "TQOE": "Безадресный: EUR",
    "TQIR": "Безадресный: ПИР", "TQUD": "Безадресный: Д USD",
    "PSOB": "РПС", "PTOB": "РПС с ЦК", "PSAU": "Размещение",
    "PSBB": "Выкуп", "PACT": "Аукцион (адресный)", "AUCT": "Аукцион размещения",
    "AUBB": "Аукцион выкупа", "PSDB": "РПС: Д-облигации",
    "PTDB": "РПС с ЦК: Д-облигации", "PSYO": "РПС: CNY", "PTOY": "РПС с ЦК: CNY",
    "PACY": "Размещение (CNY)", "PSEU": "РПС: USD", "PSUD": "РПС: Д USD",
    "PTUD": "РПС с ЦК: Д USD", "PTOD": "РПС с ЦК: USD", "PSEO": "РПС: EUR",
    "PTOE": "РПС с ЦК: EUR", "PSBU": "Выкуп (USD)", "PSBY": "Выкуп (CNY)",
    "PAUS": "Размещение (USD)",
}

# Короткая подпись режима для таблиц: в ленте колонка узкая, а трейдеру важна
# только суть режима (безадресный стакан / адресная сделка / с ЦК), валюта и
# тип бумаги видны по самой бумаге.
BOARD_SHORT = {
    "TQCB": "Т+", "TQOB": "Т+", "TQRD": "Т+", "TQOD": "Т+", "TQOY": "Т+",
    "TQOE": "Т+", "TQIR": "Т+", "TQUD": "Т+",
    "PSOB": "РПС", "PSDB": "РПС", "PSYO": "РПС", "PSEU": "РПС", "PSUD": "РПС",
    "PSEO": "РПС",
    "PTOB": "РПС с ЦК", "PTDB": "РПС с ЦК", "PTOY": "РПС с ЦК", "PTUD": "РПС с ЦК",
    "PTOD": "РПС с ЦК", "PTOE": "РПС с ЦК",
    "PSAU": "Размещ.", "PACY": "Размещ.", "PAUS": "Размещ.", "AUCT": "Размещ.",
    "PSBB": "Выкуп", "PSBU": "Выкуп", "PSBY": "Выкуп", "AUBB": "Выкуп",
    "PACT": "Аукцион",
}


def board_short(board: Optional[str]) -> str:
    """Короткая подпись; неизвестный борд показываем кодом — врать нечем."""
    b = board or ""
    return BOARD_SHORT.get(b, b)


def _labels() -> dict:
    """isin → {name, emitter, base, rating}. Реестр знает флоатеры, кэш ФИКСОВ —
    остальное; лента шире обоих (весь рынок облигаций), поэтому неизвестные
    бумаги остаются с именем из справочника MOEX (см. ниже) или голым ISIN."""
    from services import instruments_registry as reg
    from services.market_data import market_cache

    out = dict(reg.labels_map())
    for u in (market_cache.get("fixed_universe") or []):
        cur = out.get(u["isin"]) or {}
        out[u["isin"]] = {"name": cur.get("name") or u.get("name") or u["isin"],
                          "emitter": cur.get("emitter") or u.get("issuer"),
                          "base": cur.get("base") or "FIXED",
                          "rating": cur.get("rating"),
                          "maturity": cur.get("maturity") or u.get("maturity_date")}
    return out


# Охват ленты. Флоатер отличаем по базе купона (KEYRATE/RUONIA) — тем же
# признаком, что и вся остальная система; всё, что не флоатер и есть в наших
# справочниках, — фикс.
SCOPES = ("market", "universe", "float", "fixed")
_FLOAT_BASES = ("KEYRATE", "RUONIA")


def _scope_isins(scope: str, labels: dict) -> Optional[list[str]]:
    """Список бумаг под охват (None = весь рынок, фильтра нет)."""
    if scope == "universe":
        return list(labels.keys())
    if scope == "float":
        return [k for k, v in labels.items() if (v.get("base") or "") in _FLOAT_BASES]
    if scope == "fixed":
        return [k for k, v in labels.items() if (v.get("base") or "") not in _FLOAT_BASES]
    return None


def _moex_names() -> dict:
    """isin → SHORTNAME из суточного справочника ISS: подписи для бумаг вне
    юниверса (их в ленте большинство — рынок шире нашего реестра)."""
    from services import block_trades as bt
    return {v["isin"]: v.get("name") for v in bt._secmap["map"].values() if v.get("isin")}


def _decorate(rows: list[dict], labels: dict, moex: dict) -> None:
    for r in rows:
        lb = labels.get(r["isin"]) or {}
        r["name"] = lb.get("name") or moex.get(r["isin"]) or r["isin"]
        r["emitter"] = lb.get("emitter")
        r["base"] = lb.get("base")
        r["rating"] = lb.get("rating")
        r["in_universe"] = bool(lb)
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
        r["board_short"] = board_short(r.get("board"))
        r["negotiated"] = r.get("market") == "ndm"


@router.get("", tags=["Blocks"])
async def blocks(
    days: int = Query(1, ge=1, le=400, description="окно в календарных днях назад"),
    min_value: float = Query(0, ge=0, description="порог суммы сделки, ₽ (сверх порога записи)"),
    market: Optional[str] = Query(None, description="bonds (безадресные) | ndm (адресные)"),
    board: Optional[list[str]] = Query(None, description="борды MOEX (можно повторять)"),
    issuer: Optional[list[str]] = Query(None, description="эмитенты (по реестру)"),
    isin: Optional[str] = Query(None, description="одна бумага"),
    side: Optional[str] = Query(None, description="buy | sell (только безадресные)"),
    scope: str = Query("market", description="market | universe | float | fixed"),
    universe_only: bool = Query(False, description="ЛЕГАСИ: то же, что scope=universe"),
    limit: int = Query(500, ge=1, le=5000),
):
    """{blocks, summary} — крупные сделки, новые сверху."""
    if market is not None and market not in ("bonds", "ndm"):
        raise HTTPException(status_code=400, detail="market: bonds | ndm")
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope: {' | '.join(SCOPES)}")
    if universe_only and scope == "market":
        scope = "universe"
    if side is not None and side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side: buy | sell")
    isin = (isin or "").strip().upper() or None
    if isin and not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")

    from services import block_trades as bt

    labels = await asyncio.to_thread(_labels)
    isins: Optional[list[str]] = None
    if isin:
        isins = [isin]
    elif issuer:
        want = {e for e in issuer if e}
        isins = [k for k, v in labels.items() if (v.get("emitter") or "") in want]
        if not isins:
            return {"from": None, "blocks": [],
                    "summary": {"n": 0, "value": 0, "by_market": {}, "top": [],
                                "archive_till": None},
                    "warning": "по эмитенту нет бумаг в реестре"}
    else:
        # список любой длины: большой уезжает во временную таблицу внутри
        # block_trades, плейсхолдерами он бы не поместился (флоатеров 1300+)
        isins = _scope_isins(scope, labels)
        if isins is not None and not isins:
            return {"from": None, "blocks": [], "scope": scope,
                    "summary": {"n": 0, "value": 0, "by_market": {}, "top": [],
                                "archive_till": None},
                    "warning": "справочники ещё не прогреты — охват пуст"}

    frm = (date.today() - timedelta(days=days - 1)).isoformat()
    rows, summary = await asyncio.gather(
        asyncio.to_thread(bt.read_blocks, frm=frm, min_value=min_value, market=market,
                          boards=board, isins=isins, side=side, limit=limit),
        asyncio.to_thread(bt.blocks_stats, frm=frm, min_value=min_value, market=market,
                          boards=board, isins=isins, side=side))
    moex = await asyncio.to_thread(_moex_names)
    _decorate(rows, labels, moex)
    for t in summary.get("top") or []:
        lb = labels.get(t["isin"]) or {}
        t["name"] = lb.get("name") or moex.get(t["isin"]) or t["isin"]
        t["emitter"] = lb.get("emitter")

    return {"from": frm, "days": days, "min_value": min_value, "market": market,
            "board": board, "side": side, "scope": scope,
            "truncated": len(rows) >= limit and summary["n"] > len(rows),
            "blocks": rows, "summary": summary}


@router.get("/boards", tags=["Blocks"])
async def boards(days: int = Query(30, ge=1, le=400)):
    """Режимы, реально встречавшиеся в окне — источник опций фильтра."""
    from services import block_trades as bt
    rows = await asyncio.to_thread(bt.boards_seen, days)
    for r in rows:
        r["title"] = BOARD_TITLES.get(r["board"] or "", r["board"])
    return {"days": days, "boards": rows}


@router.get("/days", tags=["Blocks"])
async def days_agg(
    isin: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=400),
    min_value: float = Query(0, ge=0),
    scope: str = Query("market", description="market | universe | float | fixed"),
    issuer: Optional[list[str]] = Query(None, description="эмитенты (по реестру)"),
    ttm_min: Optional[float] = Query(None, ge=0, description="срок до горизонта прайсинга от, лет"),
    ttm_max: Optional[float] = Query(None, ge=0, description="срок до горизонта прайсинга до, лет"),
    rating: Optional[list[str]] = Query(None, description="рейтинги (можно повторять)"),
    limit: int = Query(1000, ge=1, le=20000),
):
    """Дневные РПС-обороты (ISS history market=ndm).

    Это ЕДИНСТВЕННОЕ, что доступно за дни до запуска поштучного сбора: ISS не
    отдаёт адресные сделки за прошлые сессии, только агрегат бумага/борд/день.
    """
    isin = (isin or "").strip().upper() or None
    if isin and not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope: {' | '.join(SCOPES)}")
    from api.routes.trades import _rating_isins, _ttm_isins
    from services import block_trades as bt
    labels = await asyncio.to_thread(_labels)
    isins: Optional[list[str]] = None
    if not isin:
        if issuer:
            want = {e for e in issuer if e}
            isins = [k for k, v in labels.items() if (v.get("emitter") or "") in want]
        else:
            isins = _scope_isins(scope, labels)
        # срок считается до горизонта прайсинга — как в ленте и в МОНИТОРЕ
        from services.market_data import MarketDataService
        isins = _rating_isins(
            labels,
            _ttm_isins(labels, isins, ttm_min, ttm_max,
                       MarketDataService.universe_metrics() or {}),
            rating)
        if isins is not None and not isins:
            return {"from": None, "days": days, "rows": []}
    frm = (date.today() - timedelta(days=days)).isoformat()
    rows = await asyncio.to_thread(bt.read_days, isin=isin, frm=frm,
                                   min_value=min_value, limit=limit, isins=isins)
    moex = await asyncio.to_thread(_moex_names)
    for r in rows:
        lb = labels.get(r["isin"]) or {}
        r["name"] = lb.get("name") or moex.get(r["isin"]) or r["isin"]
        r["emitter"] = lb.get("emitter")
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
        r["board_short"] = board_short(r.get("board"))
    return {"from": frm, "days": days, "rows": rows}


@router.get("/{isin}", tags=["Blocks"])
async def by_isin(isin: str, days: int = Query(90, ge=1, le=400),
                  min_value: float = Query(0, ge=0),
                  limit: int = Query(500, ge=1, le=5000),
                  order: str = Query("ts", pattern="^(ts|value)$",
                                     description="ts — последние по времени, "
                                                 "value — самые крупные за окно")):
    """Крупные сделки одной бумаги + её дневные РПС-обороты (для карточки)."""
    isin = isin.strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    from services import block_trades as bt
    frm = (date.today() - timedelta(days=days - 1)).isoformat()
    rows, days_rows, summary, total = await asyncio.gather(
        asyncio.to_thread(bt.read_blocks, frm=frm, min_value=min_value,
                          isins=[isin], limit=limit, order=order),
        asyncio.to_thread(bt.read_days, isin=isin, frm=frm),
        asyncio.to_thread(bt.blocks_stats, frm=frm, min_value=min_value, isins=[isin]),
        asyncio.to_thread(bt.count_blocks, frm=frm, min_value=min_value, isins=[isin]))
    for r in rows:
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
        r["board_short"] = board_short(r.get("board"))
        r["negotiated"] = r.get("market") == "ndm"
    # Спред в базе посчитан ЖИВОЙ моделью в момент прихода сделки: для сегодняшних
    # это верно, для прошлых сессий — оценка, расходящаяся с линией спреда на
    # графике (у бумаг с малой дюрацией — на сотни bps). Пересчитываем прошлые
    # дни честным as-of, тем же движком, что рисует линию.
    # Только флоатеры: у фикса аналог — g-спред по своей кривой, архива которой
    # нет, и пересчитывать его сегодняшней моделью смысла нет (в базе то же).
    base = ((await asyncio.to_thread(_labels)).get(isin) or {}).get("base")
    if rows and base in ("KEYRATE", "RUONIA"):
        try:
            from api.routes.history import _price_trades
            past = [r for r in rows if str(r.get("ts") or "")[:10] < date.today().isoformat()]
            if past:
                await _price_trades(isin, past, "floater", days=days)
        except Exception as e:
            logger.info("blocks pricing %s: as-of недоступен (%s) — спред из базы", isin, e)
    for r in days_rows:
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
    return {"isin": isin, "from": frm, "days": days, "blocks": rows,
            # сколько подходит под фильтр всего и срезал ли лимит: без этого
            # график молча показывал часть точек как «все»
            "total": total, "truncated": total > len(rows),
            "day_totals": days_rows, "summary": summary}
