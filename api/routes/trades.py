"""Вкладка СДЕЛКИ — общерыночная лента обезличенных сделок.

Источник — тот же тиковый архив (trade_tick), что и лента в карточке выпуска,
только без фильтра по одной бумаге. Архив наливает часовой демон по всему
юниверсу (hourly_bars_worker), поэтому лента отстаёт от биржи максимум на один
прогон — сюда мы в Alor не ходим: массовый онлайн-дрейн по 500+ бумагам на
запрос страницы упёрся бы в rate-limit брокера.

Глубина: за сырым окном (TICK_RAW_DAYS ≈ 35 дней) ретеншен оставляет только
принты от TICK_BIG_VALUE_RUB, так что дальний конец ленты — крупные сделки.
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


def _labels() -> dict:
    """isin → {name, emitter, base, rating} для подписей ленты.

    Реестр знает только флоатеры (и то, что через него прошло), а тики демон
    льёт и по фиксам — их имена берём из прогретого универса ФИКСОВ, иначе
    половина ленты подписана голым ISIN."""
    from services import instruments_registry as reg
    from services.market_data import market_cache

    out = dict(reg.labels_map())
    for u in (market_cache.get("fixed_universe") or []):
        cur = out.get(u["isin"]) or {}
        out[u["isin"]] = {"name": cur.get("name") or u.get("name") or u["isin"],
                          "emitter": cur.get("emitter") or u.get("issuer"),
                          "base": cur.get("base") or "FIXED",
                          "rating": cur.get("rating")}
    return out


@router.get("", tags=["Trades"])
async def tape(
    days: int = Query(1, ge=1, le=400, description="окно в календарных днях назад"),
    min_value: float = Query(0, ge=0, description="порог суммы сделки, ₽"),
    side: Optional[str] = Query(None, description="buy | sell (агрессор)"),
    issuer: Optional[list[str]] = Query(None, description="эмитенты (можно повторять параметр)"),
    isin: Optional[str] = Query(None, description="одна бумага"),
    limit: int = Query(500, ge=1, le=5000),
):
    """{trades, summary} — лента рынка, новые сверху, с именем бумаги и эмитентом."""
    if side is not None and side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side: buy | sell")
    isin = (isin or "").strip().upper() or None
    if isin and not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")

    from services import trades_archive as ta

    # SQLite тут синхронный, а агрегат ленты на 1.4 млн тиков меряется секундами
    # (замер на проде: до 2.2с) — в event loop это встало бы ВСЁ приложение,
    # включая WS-пуши и чужие запросы. Отсюда и ниже — только через to_thread.
    labels = await asyncio.to_thread(_labels)
    isins: Optional[list[str]] = None
    if isin:
        isins = [isin]
    elif issuer:
        want = {e for e in issuer if e}
        isins = [k for k, v in labels.items() if (v.get("emitter") or "") in want]
        if not isins:
            return {"from": None, "trades": [],
                    "summary": {"n": 0, "value": 0, "buy_value": 0, "sell_value": 0,
                                "issuers_top": [], "archive_till": None},
                    "warning": "по эмитенту нет бумаг в реестре"}

    # лимит переменных SQLite: столько бумаг = выбран почти весь рынок, фильтр
    # всё равно ничего не режет — идём без него, чем резать список молча
    if isins and len(isins) > 900:
        logger.info("tape: фильтр по %d бумагам — игнорирую", len(isins))
        isins = None

    frm = (date.today() - timedelta(days=days - 1)).isoformat()
    # строки и агрегат независимы — читаем параллельно (WAL допускает
    # конкурентных читателей), ответ приходит за время медленного из двух
    rows, summary = await asyncio.gather(
        asyncio.to_thread(ta.read_tape, frm=frm, min_value=min_value, side=side,
                          isins=isins, limit=limit),
        asyncio.to_thread(ta.tape_stats, frm=frm, min_value=min_value, side=side,
                          isins=isins))

    for r in rows:
        lb = labels.get(r["isin"]) or {}
        r["name"] = lb.get("name") or r["isin"]
        r["emitter"] = lb.get("emitter")
        r["base"] = lb.get("base")
        r["rating"] = lb.get("rating")
    for t in summary.get("issuers_top") or []:
        lb = labels.get(t["isin"]) or {}
        t["name"] = lb.get("name") or t["isin"]
        t["emitter"] = lb.get("emitter")

    return {"from": frm, "days": days, "min_value": min_value, "side": side,
            "truncated": len(rows) >= limit and summary["n"] > len(rows),
            "trades": rows, "summary": summary}


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
