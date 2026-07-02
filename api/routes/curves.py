from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Literal
from datetime import date, timedelta
from api.schemas import CurveResponse, ForwardRateResponse, CurveNode, CurveSegment
from services.market_data import MarketDataService

router = APIRouter()

@router.get("", response_model=CurveResponse, tags=["Curves"])
async def get_curve(
    type: Literal["ruonia", "keyrate"] = Query(..., description="Type of the curve to fetch")
):
    ruonia_curve, keyrate_curve, calc_date, _ = await MarketDataService.get_curves()
    curve = ruonia_curve if type == "ruonia" else keyrate_curve
    
    if not curve:
        raise HTTPException(status_code=404, detail=f"Curve '{type}' not found or unavailable")
        
    nodes = []
    for dt, df in curve.nodes:
        # compute instant forward roughly
        fwd = curve.forward(dt, dt + timedelta(days=30)) * 100
        nodes.append(CurveNode(date=dt, discount_factor=df, forward_pct=round(fwd, 4)))
        
    segments = []
    if len(nodes) > 1:
        for i in range(len(nodes) - 1):
            d1, d2 = nodes[i].date, nodes[i+1].date
            fwd = curve.forward(d1, d2) * 100
            segments.append(CurveSegment(start_date=d1, end_date=d2, forward_pct=round(fwd, 4)))
            
    return CurveResponse(
        curve_type=type.upper(),
        calc_date=curve.calc_date,
        nodes=nodes,
        segments=segments
    )

@router.get("/forwards", response_model=ForwardRateResponse, tags=["Curves"])
async def get_forward_rate(
    type: Literal["ruonia", "keyrate"] = Query(..., description="Type of the curve to fetch"),
    start_date: date = Query(..., description="Start date of the forward period"),
    end_date: date = Query(..., description="End date of the forward period")
):
    ruonia_curve, keyrate_curve, calc_date, _ = await MarketDataService.get_curves()
    curve = ruonia_curve if type == "ruonia" else keyrate_curve
    
    if not curve:
        raise HTTPException(status_code=404, detail=f"Curve '{type}' not found or unavailable")
        
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be strictly before end_date")
        
    try:
        fwd_rate = curve.forward(start_date, end_date)
        return ForwardRateResponse(
            curve_type=type.upper(),
            calc_date=curve.calc_date,
            start_date=start_date,
            end_date=end_date,
            forward_pct=round(fwd_rate * 100, 4)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Calculation error: {e}")
