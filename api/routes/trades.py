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


def _rating_isins(labels: dict, isins: Optional[list[str]],
                  rating: Optional[list[str]]) -> Optional[list[str]]:
    """Сузить охват рейтингами (в реестре они грубые: AAA/AA/A/BBB/BB/B).
    Бумаги без рейтинга под фильтр не попадают — рейтинг неизвестен, а не любой."""
    want = {r.strip().upper() for r in (rating or []) if r and r.strip()}
    if not want:
        return isins
    nr = "NR" in want          # NR — «без рейтинга», отдельный бакет, как в СПИСКЕ
    pool = isins if isins is not None else labels.keys()
    out = []
    for i in pool:
        rt = ((labels.get(i) or {}).get("rating") or "").strip().upper()
        if (rt in want) or (nr and not rt):
            out.append(i)
    return out


def _ttm_isins(labels: dict, isins: Optional[list[str]],
               ttm_min: Optional[float], ttm_max: Optional[float],
               uni: Optional[dict] = None) -> Optional[list[str]]:
    """Сузить охват сроком до ГОРИЗОНТА ПРАЙСИНГА (годы) — той даты, к которой
    посчитаны метрики бумаги: оферта/колл по правилу цены, иначе погашение.
    uni — метрики юниверса (offer_date/horizon); без них считаем к погашению.

    Бумаги без даты под такой фильтр не попадают — срок у них неизвестен, а не
    «любой»; при scope=market это отсекает всё, чего нет в наших справочниках."""
    if ttm_min is None and ttm_max is None:
        return isins
    today = date.today()
    keep = []
    pool = isins if isins is not None else labels.keys()
    for i in pool:
        mx = (uni or {}).get(i) or {}
        md = (mx.get("offer_date") if mx.get("horizon") in ("put", "call") else None) \
            or (labels.get(i) or {}).get("maturity")
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


# ОФЗ: суверен Минфина. Тот же критерий, что в /api/bonds (_is_ofz) — иначе
# «ОФЗ» в ленте и в СПИСКЕ означали бы разное.
_OFZ_NAME_RE = re.compile(r"^ОФЗ\s", re.I)


def _flag_isins(labels: dict, isins: Optional[list[str]], bases: Optional[list[str]],
                hide_subord: bool, hide_amort: bool,
                cls: Optional[list[str]]) -> Optional[list[str]]:
    """Сузить охват признаками выпуска: база купона, суборд, амортизация, класс.

    Суборд определяется по имени (как в скринере), класс — по эмитенту/имени.
    Амортизация приходит из метрик юниверса: у бумаг вне юниверса признака нет
    вообще, и они считаются НЕамортизируемыми — иначе фильтр молча выкосил бы
    весь рынок за пределами реестра."""
    want_base = {b.strip().upper() for b in (bases or []) if b and b.strip()}
    want_cls = {c.strip().upper() for c in (cls or []) if c and c.strip()}
    if not (want_base or want_cls or hide_subord or hide_amort):
        return isins
    from services.screener_core import is_subord
    from services.market_data import MarketDataService
    um = MarketDataService.universe_metrics() or {} if hide_amort else {}
    pool = isins if isins is not None else labels.keys()
    keep = []
    for i in pool:
        lb = labels.get(i) or {}
        name = (lb.get("name") or "").strip()
        if want_base and (lb.get("base") or "").upper() not in want_base:
            continue
        if hide_subord and is_subord({"name": name}):
            continue
        if hide_amort and (um.get(i) or {}).get("has_amort"):
            continue
        if want_cls:
            ofz = (lb.get("emitter") or "").strip() == "Минфин России" \
                or bool(_OFZ_NAME_RE.match(name))
            if ("OFZ" if ofz else "CORP") not in want_cls:
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
    isins: Optional[list[str]] = Query(None, description="набор бумаг (избранное), можно повторять"),
    max_value: Optional[float] = Query(None, ge=0, description="порог суммы сделки до, ₽"),
    spread_min: Optional[float] = Query(None, description="R-spread сделки от, бп"),
    spread_max: Optional[float] = Query(None, description="R-spread сделки до, бп"),
    ttm_min: Optional[float] = Query(None, ge=0, description="срок до горизонта прайсинга от, лет"),
    ttm_max: Optional[float] = Query(None, ge=0, description="срок до горизонта прайсинга до, лет"),
    rating: Optional[list[str]] = Query(None, description="рейтинги (можно повторять)"),
    base: Optional[list[str]] = Query(None, description="база купона: KEYRATE | RUONIA"),
    cls: Optional[list[str]] = Query(None, description="класс: OFZ | CORP"),
    hide_subord: bool = Query(False, description="убрать суборды и перпы"),
    hide_amort: bool = Query(False, description="убрать амортизируемые выпуски"),
    before_ts: Optional[str] = Query(None, description="курсор пагинации: ts последней показанной сделки"),
    before_id: Optional[int] = Query(None, description="курсор пагинации: её trade_id"),
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
    watch = [i.strip().upper() for i in (isins or []) if i and i.strip()]
    isins: Optional[list[str]] = None
    if isin:
        isins = [isin]
    elif watch:
        # избранное: набор ISIN приходит с фронта (watchlist живёт в браузере)
        isins = watch
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

    # Метрики юниверса нужны и фильтру срока (горизонт прайсинга), и разметке
    # строк ниже — берём один раз тут: вызов синхронный, читает готовый снапшот.
    from services.market_data import MarketDataService
    um = MarketDataService.universe_metrics() or {}

    if not isin:
        isins = _flag_isins(
            labels,
            _rating_isins(labels, _ttm_isins(labels, isins, ttm_min, ttm_max, um), rating),
            base, hide_subord, hide_amort, cls)
        if isins is not None and not isins:
            return {"from": None, "trades": [], "scope": scope,
                    "summary": {"n": 0, "value": 0, "buy_value": 0, "sell_value": 0,
                                "by_market": {}, "top": [], "archive_till": None},
                    "warning": "под фильтры срока/рейтинга бумаг нет"}

    frm = (date.today() - timedelta(days=days - 1)).isoformat()
    # строки и агрегат независимы — читаем параллельно (WAL допускает
    # конкурентных читателей), ответ приходит за время медленного из двух
    rows, summary = await asyncio.gather(
        asyncio.to_thread(tape_svc.read_tape, frm=frm, min_value=min_value, side=side,
                          market=market, boards=board, isins=isins, limit=limit,
                          y_min=spread_min, y_max=spread_max, max_value=max_value,
                          before_ts=before_ts, before_id=before_id),
        # Итоги — по ВСЕМУ окну, поэтому считаются один раз, на первой странице:
        # догрузка следующей порции их не меняет, а стоит агрегат секунд.
        asyncio.to_thread(tape_svc.tape_stats, frm=frm, min_value=min_value, side=side,
                          market=market, boards=board, isins=isins,
                          y_min=spread_min, y_max=spread_max, max_value=max_value)
        if not before_ts else asyncio.sleep(0, result={}))
    moex = await asyncio.to_thread(_moex_names)
    # Оферты: дата и вид ближайшей — из метрик юниверса um (там же, откуда их
    # берёт МОНИТОР), call-опцион — из реестра. Маркеры p/c рядом с датой оферты.
    # Y-IDX приезжает готовым из архива (считает демон при приходе сделки, см.
    # block_trades.price_new_trades): цена в % номинала между выпусками
    # несравнима, спред к индексу — сравним. Считать здесь нельзя: прогрев
    # контекстов по сотне выпусков занимает минуту на первом запросе.
    priced = sum(1 for r in rows if r.get("y_idx_bps") is not None)
    # база спреда за предыдущие 7 дней: строка ленты показывает, на сколько
    # спред сделки отклонился от того уровня, по которому бумага торговалась
    # неделю (services.bars.spread_avg_map — кэш в памяти, запрос раз в 15 мин)
    from services import bars as bars_svc
    try:
        avg7 = await asyncio.to_thread(bars_svc.spread_avg_map, 7)
    except Exception as e:
        logger.warning("spread_avg_map failed: %s", e)
        avg7 = {}

    for r in rows:
        lb = labels.get(r["isin"]) or {}
        r["name"] = lb.get("name") or moex.get(r["isin"]) or r["isin"]
        r["emitter"] = lb.get("emitter")
        r["base"] = lb.get("base")
        r["rating"] = lb.get("rating")
        r["board_title"] = BOARD_TITLES.get(r.get("board") or "", r.get("board"))
        r["board_short"] = board_short(r.get("board"))
        r["maturity"] = lb.get("maturity")
        # формула купона рисуется тем же компонентом, что в СПИСКЕ
        r["margin_bps"] = lb.get("margin_bps")
        r["coupons_per_year"] = lb.get("coupons_per_year")
        r["coupon_text"] = lb.get("coupon_text")
        mx = um.get(r["isin"]) or {}
        r["offer_date"] = mx.get("offer_date")
        r["offer_kind"] = mx.get("offer_kind")
        r["preferred_horizon"] = mx.get("horizon")
        r["has_call"] = lb.get("has_call")
        r["y_idx_avg7_bps"] = avg7.get(r["isin"])
    for t in summary.get("top") or []:
        lb = labels.get(t["isin"]) or {}
        t["name"] = lb.get("name") or moex.get(t["isin"]) or t["isin"]
        t["emitter"] = lb.get("emitter")

    return {"from": frm, "days": days, "min_value": min_value, "side": side,
            "market": market, "board": board, "scope": scope,
            # has_more — есть ли следующая страница: на страницах пагинации
            # итогов нет (их считает только первый запрос), поэтому полный
            # лимит строк сам по себе означает «дальше ещё есть»
            "truncated": len(rows) >= limit and (not summary or summary.get("n", 0) > len(rows)),
            "has_more": len(rows) >= limit,
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


@router.get("/ratings", tags=["Trades"])
async def ratings():
    """Рейтинги для фильтра — из справочников (в реестре шкала грубая:
    AAA/AA/A/BBB/BB/B), с числом бумаг на грейд. Порядок — от старшего."""
    counts: dict[str, int] = {}
    for v in (await asyncio.to_thread(_labels)).values():
        rt = (v.get("rating") or "").strip().upper()
        # NR — отдельный бакет «рейтинга нет», как в МОНИТОРЕ: без него такие
        # бумаги нельзя ни выбрать, ни понять, сколько их
        counts[rt or "NR"] = counts.get(rt or "NR", 0) + 1
    order = {r: i for i, r in enumerate(
        ("AAA", "AA", "A", "BBB", "BB", "B", "CCC", "CC", "C", "D", "NR"))}
    items = [{"name": k, "count": v} for k, v in counts.items()]
    items.sort(key=lambda x: (order.get(x["name"], 99), x["name"]))
    return {"ratings": items}


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
