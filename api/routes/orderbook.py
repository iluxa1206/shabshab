from fastapi import APIRouter, Query, HTTPException, Path
from typing import Optional
from datetime import datetime, date, timezone
import asyncio
import aiohttp
from api.schemas import OrderbookResponse, OrderbookSnapshot, OrderbookLevel
from services.market_data import MarketDataService
from services.bonds import create_bond_ref_data
from services.valuation import calculate_valuation_metrics
from auth import get_access_token, REFRESH_TOKEN, BASE_API
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

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
    cache = MarketDataService.get_local_bond_cache("isins_cache.json")
    data = cache.get(isin)
    
    if not data:
        raise HTTPException(status_code=404, detail="Bond not found in cache")
        
    ref_obj = create_bond_ref_data(data, isin)
    
    # 2. Get Curves
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    if not calc_date:
        calc_date = rates_date or date.today()
        
    curve = ruonia_curve if ref_obj.base == "RUONIA" else keyrate_curve
    
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
            
            dm_bps = None
            calc_yield = ytm
            
            if price is not None and curve:
                try:
                    metrics = calculate_valuation_metrics(ref_obj, price, curve, calc_date)
                    dm_bps = metrics.get("dm_bps")
                    calc_yield = metrics.get("yield_xirr_pct")
                except Exception:
                    pass
                    
            if price is not None and qty is not None:
                target_array.append(OrderbookLevel(
                    price_pct=price,
                    quantity=qty,
                    yield_pct=calc_yield,
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
