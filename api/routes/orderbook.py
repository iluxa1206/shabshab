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

@router.get("/{isin}", response_model=OrderbookResponse, tags=["Orderbook"])
async def get_orderbook(
    isin: str = Path(...),
    depth: int = Query(10, ge=1, le=50)
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

    # 4. Process and Calculate
    processed_bids = []
    processed_asks = []

    def process_level(level_list, limit, target_array):
        for entry in level_list[:limit]:
            price = entry.get("price")
            qty = entry.get("volume")
            ytm = entry.get("yield") # sometimes Alor natively sends YTM, let's keep it if we can

            sm_bps = None
            dm_bps = None
            calc_yield = ytm

            if price is not None:
                try:
                    metrics = reprice_at_price(ctx, price)
                    sm_bps = metrics.get("sm_bps")
                    dm_bps = metrics.get("dm_bps")
                    calc_yield = metrics.get("yield_xirr_pct")
                except Exception:
                    pass

            if price is not None and qty is not None:
                target_array.append(OrderbookLevel(
                    price_pct=price,
                    quantity=qty,
                    yield_pct=calc_yield,
                    sm_bps=sm_bps,
                    dm_bps=dm_bps
                ))

    process_level(snapshot.get("bids", []), depth, processed_bids)
    process_level(snapshot.get("asks", []), depth, processed_asks)
    
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
