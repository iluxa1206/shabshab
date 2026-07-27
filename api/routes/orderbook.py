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
from api.routes.bonds import get_base_dir
from auth import get_access_token, REFRESH_TOKEN, BASE_API
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

async def fetch_alor_orderbook_snapshot(isin: str, depth: int) -> Optional[dict]:
    access_token = await asyncio.to_thread(get_access_token, REFRESH_TOKEN)
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


def _level_metrics(ctx, reprice_at_price, price, qty, alor_yield=None):
    """Один уровень стакана → OrderbookLevel c SM/DM/YTM под цену уровня.
    DM = disc_margin_bps (настоящая дисконт-маржа); dm_bps в метриках — легаси-
    алиас sm_bps, поэтому SM и DM совпадали. SM = sm_bps (fallback dm_bps),
    как в калькуляторе карточки."""
    sm_bps = dm_bps = None
    calc_yield = alor_yield
    try:
        m = reprice_at_price(ctx, price)
        sm_bps = m.get("sm_bps") if m.get("sm_bps") is not None else m.get("dm_bps")
        dm_bps = m.get("disc_margin_bps")
        calc_yield = m.get("yield_xirr_pct")
    except Exception:
        pass
    return OrderbookLevel(price_pct=price, quantity=qty, yield_pct=calc_yield,
                          sm_bps=sm_bps, dm_bps=dm_bps)


@router.get("/{isin}", response_model=OrderbookResponse, tags=["Orderbook"])
async def get_orderbook(
    isin: str = Path(...),
    depth: int = Query(10, ge=1, le=50),
    full: bool = Query(False, description="Все уровни лестницы (не только с заявками)"),
):
    # 1. Get Bond Data
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise HTTPException(status_code=400, detail="Некорректный ISIN")
    cache = MarketDataService.get_local_bond_cache(os.path.join(get_base_dir(), "isins_cache.json"))

    # 2. Тёплый контекст пересчёта (ref_obj/кривая/calc_date + amorts/offers/
    # periods/НКД) — один раз на выпуск, далее reprice_at_price по уровням без I/O.
    # Полные amorts/offers → per-level SM/DM совпадают с калькулятором карточки
    # (раньше orderbook звал calculate_valuation_metrics без них → расхождение).
    from services.bond_details import load_reprice_ctx, reprice_at_price
    try:
        ctx = await load_reprice_ctx(isin, cache)
    except NotFoundException:
        raise HTTPException(status_code=404, detail="Bond not found")
    calc_date = ctx["calc_date"]

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

    if full and (raw_bids or raw_asks):
        # Режим «все уровни»: непрерывная лестница цен с рыночным шагом между
        # минимальной bid и максимальной ask, с SM/DM на КАЖДОМ уровне (даже без
        # заявки) — для анализа «при какой цене DM = X». Шаг = мин. разрыв между
        # соседними реальными ценами; при слишком широком диапазоне — огрубляем,
        # чтобы влезть в _MAX_LADDER уровней.
        prices = sorted({p for p, _ in raw_bids + raw_asks})
        qty_at = {round(p, 4): q for p, q in raw_bids + raw_asks}
        lo, hi = prices[0], prices[-1]
        diffs = [round(b - a, 6) for a, b in zip(prices, prices[1:]) if b - a > 1e-9]
        step = min(diffs) if diffs else 0.01
        nsteps = int(round((hi - lo) / step)) if step > 0 else 0
        if nsteps > _MAX_LADDER:
            step = (hi - lo) / _MAX_LADDER
            nsteps = _MAX_LADDER
        best_bid = max((p for p, _ in raw_bids), default=None)
        best_ask = min((p for p, _ in raw_asks), default=None)
        mid = ((best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None
               else (best_bid if best_bid is not None else best_ask))
        processed_bids, processed_asks = [], []
        for i in range(nsteps + 1):
            price = round(lo + i * step, 4)
            qty = qty_at.get(price)
            lvl = _level_metrics(ctx, reprice_at_price, price, qty)
            (processed_asks if price > mid else processed_bids).append(lvl)
    else:
        processed_bids = [_level_metrics(ctx, reprice_at_price, p, q) for p, q in raw_bids]
        processed_asks = [_level_metrics(ctx, reprice_at_price, p, q) for p, q in raw_asks]

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
