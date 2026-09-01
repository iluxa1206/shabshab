from fastapi import APIRouter, Query, HTTPException, Path
from typing import Optional
from datetime import datetime, date, timezone
import os
import re
import asyncio
import aiohttp
from api.schemas import OrderbookResponse, OrderbookSnapshot, OrderbookLevel
from services.market_data import MarketDataService
from services.exceptions import NotFoundException
from services.paths import cache_path as _cache_path
from auth import alor_token, REFRESH_TOKEN, BASE_API
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

async def fetch_alor_orderbook_snapshot(isin: str, depth: int) -> Optional[dict]:
    access_token = await alor_token()
    if not access_token:
        return None
        
    url = f"{BASE_API}/md/v2/orderbooks/MOEX/{isin}"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"depth": depth}
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.warning(f"Error fetching orderbook snapshot: {e}")
        
    return None

_MAX_LADDER = 60   # потолок синтетических уровней (bounds reprice-компьют)


def _level(got: dict, price, qty):
    """Один уровень стакана → OrderbookLevel. Метрики берутся из ГОТОВОГО батча
    (services.orderbook_svc.build_levels_fn): набор полей зависит от типа бумаги
    (флоатер: Y-IDX+DM+YTM; фикс: YTM+g-спред)."""
    m = got.get(round(float(price), 4)) or {}
    return OrderbookLevel(price_pct=price, quantity=qty, yield_pct=m.get("yield_pct"),
                          dm_bps=m.get("dm_bps"), y_idx_bps=m.get("y_idx_bps"),
                          g_spread_bps=m.get("g_spread_bps"))


@router.get("/depth/all", tags=["Orderbook"])
async def get_depth_all():
    """Лестницы стаканов по всему юниверсу флоатеров одним ответом — сырьё для
    фильтра по объёму в таблице (VWAP на тикет считает фронт: объём задаёт
    пользователь, а деньги уровня = qty × (номинал × цена% + НКД) он берёт из
    face_value/accrued_rub строки /api/bonds).

    Наполняет фоновый depth_poller (батч-снимок Alor WS раз в ~2 мин в торговые
    часы). Пустой items — снимка ещё нет или он протух: фронт в этом случае
    молча не применяет фильтр, а не показывает пустую таблицу."""
    from services import depth as depth_svc
    items = depth_svc.get_depth()
    return {"ts": depth_svc.depth_ts(), "count": len(items), "items": items}


@router.get("/{isin}", response_model=OrderbookResponse, tags=["Orderbook"])
async def get_orderbook(
    isin: str = Path(...),
    depth: int = Query(10, ge=1, le=50),
    full: bool = Query(False, description="Все уровни лестницы (не только с заявками)"),
    kind: str = Query("floater", description="floater | fixed — набор метрик уровня"),
    horizon: str = Query("auto", description="auto | maturity | put | call — горизонт "
                                             "прайсинга уровней (auto = правило цены)"),
):
    # 1. Get Bond Data
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")

    # 2. Тёплый контекст пересчёта на выпуск (один раз) → levels_fn(цены) считает
    # ВСЕ уровни одним проходом, без I/O. Тот же слой, что у WS-потока и ленты:
    # флоатер — Y-IDX+DM+YTM к горизонту уровня, фикс — YTM+g-спред.
    from services.orderbook_svc import build_levels_fn
    try:
        levels_fn, calc_date, _face = await build_levels_fn(isin, kind, horizon)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")

    # 3. Fetch Snapshot
    snapshot = await fetch_alor_orderbook_snapshot(isin, depth)

    pricing_status = "SUCCESS"
    warnings = []

    if not snapshot:
        return OrderbookResponse(
            isin=isin,
            market_timestamp=datetime.now(timezone.utc),
            pricing_status="NO_MARKET_DATA",
            calc_date=calc_date,
            orderbook=OrderbookSnapshot(bids=[], asks=[]),
            warnings=["Could not fetch orderbook from Alor"]
        )

    # 4. Уровни с заявками (обрезаем до depth)
    raw_bids = [(e["price"], e.get("volume")) for e in snapshot.get("bids", [])[:depth]
                if e.get("price") is not None]
    raw_asks = [(e["price"], e.get("volume")) for e in snapshot.get("asks", [])[:depth]
                if e.get("price") is not None]

    # 5. Уровни к расчёту: в режиме «все уровни» — вся лестница (метрики и на
    # пустых ценах, для анализа «при какой цене спред станет X»), иначе только
    # цены с заявками. Сетку лестницы строит та же функция, что у WS-потока.
    plan = None
    if full:
        from services.orderbook_svc import ladder_plan
        plan = ladder_plan(raw_bids, raw_asks)
    prices = ([p for p, _q in plan["levels"]] if plan
              else [p for p, _q in raw_bids] + [p for p, _q in raw_asks])

    # СЧЁТ — В ПОТОКЕ ИСПОЛНИТЕЛЯ, НЕ В EVENT LOOP. Лестница на 60 уровней это
    # XIRR и солвер DM на каждый уровень: даже батчем (один поток на бумагу
    # вместо одного на цену, ×65 по замеру 28.08.2026) счёт остаётся
    # процессорным, а карточка поллит ручку раз в 15 с при живом WS и раз в 3 с
    # без него — в цикле это лаг всего сервера.
    from services.heavy import run_heavy
    got = await run_heavy(levels_fn, prices) or {}

    if plan is not None:
        processed_bids, processed_asks = [], []
        for p, q in plan["levels"]:
            (processed_asks if p > plan["mid"] else processed_bids).append(_level(got, p, q))
    else:
        processed_bids = [_level(got, p, q) for p, q in raw_bids]
        processed_asks = [_level(got, p, q) for p, q in raw_asks]

    return OrderbookResponse(
        isin=isin,
        market_timestamp=datetime.now(timezone.utc),
        pricing_status=pricing_status,
        calc_date=calc_date,
        orderbook=OrderbookSnapshot(
            bids=sorted(processed_bids, key=lambda x: x.price_pct, reverse=True),
            asks=sorted(processed_asks, key=lambda x: x.price_pct)
        ),
        warnings=warnings
    )
