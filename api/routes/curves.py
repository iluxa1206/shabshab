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


@router.get("/floater-yield", tags=["Curves"])
async def floater_yield(isin: str = Query(..., description="ISIN KEYRATE-флоатера")):
    """YTM/купоны флоатера по методу 502_504 (Floater spread): проекция купона =
    среднее рыночного пути КС (форвард СПФИ) по окну рефиксинга + спред, XIRR.
    Пока только KEYRATE."""
    from services import nrd as nrd_service
    from services.bonds import build_ref_external
    from services.floater_model import make_ks_path, project_floater, floater_xirr_pct, actual_ks

    _ruonia, keyrate_curve, calc_date, _rd = await MarketDataService.get_curves()
    cd = calc_date or date.today()
    uni = await nrd_service.fetch_floater_universe()
    u = next((x for x in uni if x.get("isin") == isin), None)
    if u is None:
        raise HTTPException(status_code=404, detail=f"{isin} не найден в юниверсе флоатеров")
    base = "KEYRATE" if u.get("base_rate_type") == "KEYRATE" else u.get("base_rate_type")
    if base != "KEYRATE":
        raise HTTPException(status_code=400, detail="Фаза 1 — только KEYRATE-флоатеры")
    if keyrate_curve is None:
        raise HTTPException(status_code=503, detail="Кривая КС недоступна")

    secs = await MarketDataService.fetch_moex_securities([isin])
    full = await MarketDataService.fetch_bond_schedule_full(isin)
    snap = (await MarketDataService.fetch_moex_snapshot([isin])).get(isin, {})
    ref = build_ref_external(isin, secs.get(isin, {}),
                             {"base_coupon_index": "CBRATED",
                              "nominal_margin_bps": u.get("spread_issue_bps") or 0})
    spread_pct = (u.get("spread_issue_bps") or 0) / 10000.0

    # будущие периоды (start,end) из расписания MOEX
    periods = []
    for c in full.get("coupons", []):
        s, e = c.get("start"), c.get("end")
        if s and e:
            sd, ed = date.fromisoformat(s), date.fromisoformat(e)
            if ed > cd:
                periods.append((sd, ed))
    if not periods:
        raise HTTPException(status_code=422, detail="Нет будущих купонов")

    price = snap.get("prev") or u.get("nrd_price_pct") or 100.0
    accrued = snap.get("accrued") if snap.get("accrued") is not None else ref.accrued_rub
    dirty_pct = price + (accrued or 0.0) / ref.face_value * 100.0
    mat = ref.maturity_date

    path = make_ks_path(keyrate_curve, cd)
    cfs_mkt = project_floater(periods, spread_pct, mat, path, cd, lag_days=7)
    y_mkt = floater_xirr_pct(cfs_mkt, dirty_pct, cd)

    return {
        "isin": isin, "name": u.get("name") or isin, "base": base,
        "spread_bps": u.get("spread_issue_bps") or 0,
        "calc_date": cd.isoformat(), "price_flat_pct": round(price, 4),
        "current_ks_pct": round((actual_ks(cd) or 0) * 100, 2),
        "ytm_pct": y_mkt,
        "coupons_market": [{"date": d.isoformat(), "amount_pct": a} for d, a in cfs_mkt[:12]],
    }


@router.get("/ks-path", response_model=KsPathResponse, tags=["Curves"])
async def get_ks_path(
    series: Literal["ks", "ruonia"] = Query("ks", description="ks | ruonia")
):
    """Путь базовой ставки: факт живьём с ЦБ РФ (дневная история) + рыночный
    форвард из СПФИ (наш bootstrap). series=ks — ключевая, ruonia — RUONIA."""
    import asyncio
    from services.ks_path import build_path, current_rate_pct
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    cd = calc_date or date.today()
    curve = keyrate_curve if series == "ks" else ruonia_curve
    points = await asyncio.to_thread(build_path, curve, cd, series)
    cur = await asyncio.to_thread(current_rate_pct, series, cd)
    warnings = []
    if curve is None:
        warnings.append("Кривая недоступна — рыночный форвард не построен.")
    if rates_date and (date.today() - rates_date).days > 4:
        warnings.append(f"Котировки устарели на {(date.today() - rates_date).days} дн.")
    return KsPathResponse(
        calc_date=cd, current_ks_pct=cur,
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
