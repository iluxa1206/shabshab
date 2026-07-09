from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Literal
from datetime import date, timedelta
from api.schemas import (CurveResponse, ForwardRateResponse, CurveNode, CurveSegment,
                         CurvePlotResponse, CurveQuote, CurveSample,
                         KsPathResponse, KsPathPoint)
from services.market_data import MarketDataService, market_cache
from forwards import get_maturity_date
from rates import tenor_to_days

router = APIRouter()


@router.get("/ks-path", response_model=KsPathResponse, tags=["Curves"])
async def get_ks_path():
    """Путь ключевой ставки: рыночный форвард (СПФИ, наш bootstrap) vs ручные
    сценарии ЦБ + факт по прошедшим заседаниям. Реплика листа «КС-прогноз»."""
    from services.ks_path import build_ks_path, current_ks_pct, SCENARIO_LABELS
    _ruonia, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    cd = calc_date or date.today()
    points = build_ks_path(keyrate_curve, cd)
    warnings = []
    if keyrate_curve is None:
        warnings.append("Кривая КС недоступна — рыночный путь не построен.")
    if rates_date and (date.today() - rates_date).days > 4:
        warnings.append(f"Котировки устарели на {(date.today() - rates_date).days} дн.")
    return KsPathResponse(
        calc_date=cd, current_ks_pct=current_ks_pct(cd),
        scenario_labels=SCENARIO_LABELS,
        points=[KsPathPoint(**p) for p in points], warnings=warnings,
    )


@router.get("/plot", response_model=CurvePlotResponse, tags=["Curves"])
async def get_curve_plot(
    type: Literal["ruonia", "keyrate"] = Query(..., description="ruonia | keyrate")
):
    """Котировки СПФИ (что запарсилось) + построенная кривая (spot/forward-сэмплы)
    для визуализации. spot_pct — средняя ставка индекса на срок (из DF, конвенция
    индекса); forward_pct — мгновенный форвард ~30д вперёд."""
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    curve = ruonia_curve if type == "ruonia" else keyrate_curve
    quotes = market_cache.get("ois_quotes" if type == "ruonia" else "irs_quotes") or []
    if not curve:
        raise HTTPException(status_code=404, detail=f"Curve '{type}' unavailable")

    start = curve.calc_date  # effective start (calc_date + 1)
    q_out = []
    for q in sorted(quotes, key=lambda x: tenor_to_days(x.tenor)):
        end = get_maturity_date(start, q.tenor)
        q_out.append(CurveQuote(tenor=q.tenor, days=(end - start).days,
                                value_pct=round(q.value, 4), name=q.name))

    # сэмплируем кривую до самого длинного тенора (плотнее на коротком конце)
    max_days = max((c.days for c in q_out), default=3650)
    grid = sorted(set(
        list(range(7, min(max_days, 370), 14)) +
        list(range(370, max_days + 1, 90)) + [max_days]
    ))
    samples = []
    for dd in grid:
        d = start + timedelta(days=dd)
        try:
            spot = curve.forward(start, d) * 100.0
            fwd = curve.forward(d, d + timedelta(days=30)) * 100.0
        except Exception:
            continue
        samples.append(CurveSample(days=dd, date=d, spot_pct=round(spot, 4),
                                   forward_pct=round(fwd, 4)))

    warnings = []
    if rates_date and (date.today() - rates_date).days > 4:
        warnings.append(f"Котировки устарели на {(date.today() - rates_date).days} дн.")

    return CurvePlotResponse(
        curve_type=type.upper(), calc_date=calc_date or date.today(),
        rates_date=rates_date, quotes=q_out, samples=samples, warnings=warnings,
    )

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
