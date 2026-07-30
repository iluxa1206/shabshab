import asyncio
import logging
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Literal
from datetime import date, timedelta

logger = logging.getLogger(__name__)
from api.schemas import (CurveResponse, ForwardRateResponse, CurveNode, CurveSegment,
                         CurvePlotResponse, CurveQuote, CurveSample,
                         KsPathResponse, KsPathPoint)
from services.market_data import MarketDataService, market_cache
from core.forwards import get_maturity_date
from core.rates import tenor_to_days

router = APIRouter()


@router.get("/floater-yield", tags=["Curves"])
async def floater_yield(isin: str = Query(..., description="ISIN KEYRATE-флоатера")):
    """YTM/купоны флоатера по методу 502_504 (Floater spread): проекция купона =
    среднее рыночного пути КС (форвард bootstrap-кривой IRS KEYRATE) по окну
    рефиксинга + спред, XIRR. Пока только KEYRATE."""
    from services import instruments_registry
    from services.bonds import build_ref_external
    from services.floater_model import make_ks_path, project_floater, floater_xirr_pct, actual_ks

    _ruonia, keyrate_curve, calc_date, _rd = await MarketDataService.get_curves()
    cd = calc_date or date.today()
    uni = await instruments_registry.fetch_floater_universe()
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
                             base="KEYRATE", spread_bps=u.get("spread_issue_bps") or 0)
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

    price = snap.get("prev") or 100.0
    accrued = snap.get("accrued") if snap.get("accrued") is not None else ref.accrued_rub
    dirty_pct = price + (accrued or 0.0) / ref.face_value * 100.0
    mat = ref.maturity_date

    path = make_ks_path(keyrate_curve, cd)
    cfs_mkt = project_floater(periods, spread_pct, mat, path, cd, lag_days=7)
    y_mkt = floater_xirr_pct(cfs_mkt, dirty_pct, cd)

    # bond-vs-index: по каждому будущему периоду — ставка индекса (среднее пути КС
    # по окну рефиксинга) и ставка купона бумаги (= индекс + спред). Зазор = carry.
    from services.floater_model import _avg_over_window
    rate_series = []
    for s, e in periods:
        days = (e - s).days or 1
        base = _avg_over_window(path, e, 7, days)
        rate_series.append({
            "date": e.isoformat(),
            "base_pct": round(base * 100.0, 4),
            "coupon_pct": round((base + spread_pct) * 100.0, 4),
        })

    # actual_ks → cbr.ks_history: на холодном/протухшем кэше это 2 requests.get
    # (таймауты 20+15с) — в потоке, не блокируем event loop (как в /ks-path)
    cur_ks = await asyncio.to_thread(actual_ks, cd)

    return {
        "isin": isin, "name": u.get("name") or isin, "base": base,
        "spread_bps": u.get("spread_issue_bps") or 0,
        "calc_date": cd.isoformat(), "price_flat_pct": round(price, 4),
        "current_ks_pct": round((cur_ks or 0) * 100, 2),
        "ytm_pct": y_mkt,
        "coupons_market": [{"date": d.isoformat(), "amount_pct": a} for d, a in cfs_mkt[:12]],
        "rate_series": rate_series,
    }


@router.get("/ks-path", response_model=KsPathResponse, tags=["Curves"])
async def get_ks_path(
    series: Literal["ks", "ruonia"] = Query("ks", description="ks | ruonia")
):
    """Путь базовой ставки: факт живьём с ЦБ РФ (дневная история) + рыночный
    форвард нашей bootstrap-кривой (IRS KEYRATE / OIS RUONIA свопы). series=ks —
    ключевая, ruonia — RUONIA."""
    from services.ks_path import build_path, current_rate_pct
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    cd = calc_date or date.today()
    curve = keyrate_curve if series == "ks" else ruonia_curve
    ks_quotes = market_cache.get("irs_quotes") if series == "ks" else None
    points = await asyncio.to_thread(build_path, curve, cd, series, 3, ks_quotes)
    cur = await asyncio.to_thread(current_rate_pct, series, cd)
    warnings = []
    if curve is None:
        warnings.append("Кривая недоступна — рыночный форвард не построен.")
    if rates_date and (date.today() - rates_date).days > 4:
        warnings.append(f"Котировки устарели на {(date.today() - rates_date).days} дн.")
    # принятое, но ещё не вступившее решение ЦБ по КС (только для series=ks)
    dec = None
    if series == "ks":
        from services import cbr_forecast
        dec = cbr_forecast.key_rate_decision(cd)
    return KsPathResponse(
        calc_date=cd, current_ks_pct=cur,
        decided_rate_pct=dec["decided_pct"] if dec else None,
        decided_effective=dec["effective_date"] if dec else None,
        decided_decision=dec["decision_date"] if dec else None,
        points=[KsPathPoint(**p) for p in points], warnings=warnings,
    )


@router.get("/plot", response_model=CurvePlotResponse, tags=["Curves"])
async def get_curve_plot(
    type: Literal["ruonia", "keyrate"] = Query(..., description="ruonia | keyrate")
):
    """Par-котировки свопов (что запарсилось: IRS KEYRATE / OIS RUONIA) +
    implied avg / forward по методике трейдерского листа (Книга1-3.xlsx, IRS):
    avg считается ИЗ PAR НАПРЯМУЮ (без бутстрапа) с учётом расписания выплат
    свопа, forward — арифметическое телескопирование уровней avg.

    Расписание выплат: ≤1Y — одна выплата в погашение; >1Y — раз в 365 дней
    (годовой купон = par, интерес-на-интерес через годы НЕ компаундится).
    Базис avg: RUONIA — daily-comp (×365), KEYRATE — quarterly nominal (×4):
      RUONIA  ≤1Y: 365·((1+par·d/365)^(1/d)−1);   >1Y: 365·((1+par)^(1/365)−1)
      KEYRATE ≤1Y: 4·((1+par·q/4)^(1/q)−1), q=кварталы; >1Y: 4·((1+par)^(1/4)−1)
    Forward сегмента [d₁,d₂]: (avg₂·d₂ − avg₁·d₁)/(d₂−d₁) — от старта
    (в листе база окна съезжает из-за протяжки формулы, тут чистая)."""
    ruonia_curve, keyrate_curve, calc_date, rates_date = await MarketDataService.get_curves()
    curve = ruonia_curve if type == "ruonia" else keyrate_curve
    quotes = market_cache.get("ois_quotes" if type == "ruonia" else "irs_quotes") or []
    if not curve:
        raise HTTPException(status_code=404, detail=f"Curve '{type}' unavailable")

    start = curve.calc_date  # effective start (calc_date + 1)

    def _avg_pct(par_pct: float, dd: int) -> float:
        """Implied avg из par-котировки (методика листа, см. docstring)."""
        s = par_pct / 100.0
        if type == "ruonia":
            if dd <= 366:  # одна выплата в погашение
                return 365.0 * ((1.0 + s * dd / 365.0) ** (1.0 / dd) - 1.0) * 100.0
            return 365.0 * ((1.0 + s) ** (1.0 / 365.0) - 1.0) * 100.0  # годовые купоны
        # KEYRATE: квартальный базис
        if dd <= 366:
            q = max(1, round(dd * 4.0 / 365.0))
            return 4.0 * ((1.0 + s * q / 4.0) ** (1.0 / q) - 1.0) * 100.0
        return 4.0 * ((1.0 + s) ** 0.25 - 1.0) * 100.0

    # подпись форварда в конвенции листа «{старт}{длина}»: 3m3m = [3m,6m],
    # 9m3m = [9m,1Y], 2m1m = [2m,3m], 1Y1Y = [1Y,2Y]. Длина окна → тенор-строка.
    def _nrm(t: str) -> str:
        return t.replace("M", "m").replace("W", "w")

    def _gap_label(dd_gap: int) -> str:
        if dd_gap <= 10:
            return "1w"
        if dd_gap <= 18:
            return "2w"
        mo = round(dd_gap / 30.4)
        if mo < 12:
            return f"{mo}m"
        return f"{round(dd_gap / 365.0)}Y"

    q_out = []
    dds, avgs = [], []  # дни/avg предыдущих теноров (для базы K-формулы листа)
    prev_tenor = None
    for q in sorted(quotes, key=lambda x: tenor_to_days(x.tenor)):
        # −1 день: get_maturity_date даёт +1 к add_months (эмпирика бутстрапа под
        # сетку 502_504); здесь дни чистые как в листе — 1W=7, 3M=92, 1Y=365 (G=H−F).
        end = get_maturity_date(start, q.tenor) - timedelta(days=1)
        dd = (end - start).days
        if dd <= 0:
            continue
        imp = _avg_pct(q.value, dd)
        # forward = телескопирование от старта: (avgₙ·dₙ − avgₙ₋₁·dₙ₋₁)/Δd;
        # первый тенор — спот-окно, fwd = avg. Тождество Σfwd·Δd = avg·d.
        if not dds:
            fwd = imp
        else:
            prev_dd, prev_avg = dds[-1], avgs[-1]
            fwd = (imp * dd - prev_avg * prev_dd) / (dd - prev_dd)
        span = "спот" if prev_tenor is None else f"{_nrm(prev_tenor)}{_gap_label(dd - dds[-1])}"
        q_out.append(CurveQuote(tenor=q.tenor, days=dd,
                                value_pct=round(q.value, 4), name=q.name,
                                implied_avg_pct=round(imp, 4), forward_pct=round(fwd, 4),
                                fwd_span=span))
        dds.append(dd); avgs.append(imp); prev_tenor = q.tenor

    # сэмплируем до самого длинного тенора (плотнее на коротком конце).
    # Линии строятся из тех же avg/fwd на тенорах: fwd — ступень по сегменту,
    # avg(d) внутри сегмента — из телескопирования: (avgᵢ₋₁·dᵢ₋₁ + fwdᵢ·(d−dᵢ₋₁))/d.
    max_days = max((c.days for c in q_out), default=3650)
    grid = sorted(set(
        list(range(7, min(max_days, 370), 14)) +
        list(range(370, max_days + 1, 90)) + [max_days]
    ))
    samples = []
    if q_out:
        for dd in grid:
            i = next((k for k, c in enumerate(q_out) if dd <= c.days), len(q_out) - 1)
            c = q_out[i]
            if i == 0:
                spot = fwd = c.forward_pct  # спот-окно до первого тенора
            else:
                p = q_out[i - 1]
                fwd = c.forward_pct
                spot = (p.implied_avg_pct * p.days + fwd * (dd - p.days)) / dd
            d = start + timedelta(days=dd)
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
        logger.warning(f"forward-rate calc error ({type}, {start_date}..{end_date}): {e}")
        raise HTTPException(status_code=500, detail="Ошибка расчёта форвардной ставки")
