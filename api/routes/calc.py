"""Вкладка КАЛЬКУЛЯТОР — расчёт кастомной (не обращающейся/новой) облигации с
фиксированным купоном по введённым параметрам. График купонов синтезируется от
даты погашения назад с шагом купонного периода, дальше метрики считает тот же
путь, что у фиксов таблицы (fixed_metrics_from_schedule + z-спред), поэтому
цифры сравнимы с колонками вкладки ФИКСЫ один-в-один. Сравнение с рынком фронт
строит сам по /api/fixed."""
import logging
from datetime import date, timedelta
from fastapi import APIRouter, Query, HTTPException

from services.market_data import MarketDataService

logger = logging.getLogger(__name__)
router = APIRouter()

_FREQS = (1, 2, 4, 12)  # выплат в год


def _shift_months(d: date, months: int) -> date:
    """Дата минус months месяцев, день зажат в конец месяца."""
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def build_custom_schedule(maturity: date, coupon_pct: float, freq: int,
                          face: float, calc_date: date) -> dict:
    """Синтетический bondization-словарь: купонные даты от погашения назад с
    шагом 12/freq месяцев (включая один период ДО calc_date — от него считается
    НКД), величина купона = face * ставка / freq. Погашение — одной амортизацией
    на дату maturity (без лесенки)."""
    step = 12 // freq
    ends = []
    d = maturity
    # один «прошедший» купон оставляем как якорь начала текущего периода
    while d > calc_date:
        ends.append(d)
        d = _shift_months(d, step)
    ends.append(d)
    ends.reverse()
    value = face * coupon_pct / 100.0 / freq
    coupons = [{"end": e.isoformat(), "value": value, "valueprc": coupon_pct}
               for e in ends]
    return {"coupons": coupons, "amorts": [{"date": maturity.isoformat(), "value": face}]}


def _accrued(schedule: dict, settle: date, calc_date: date) -> float:
    """НКД на дату поставки: линейно по дням внутри текущего купонного периода."""
    dates = [date.fromisoformat(c["end"]) for c in schedule["coupons"]]
    prev = None
    for d in dates:
        if d <= settle:
            prev = d
        else:
            period_end = d
            break
    else:
        return 0.0
    if prev is None:
        # синтетика всегда кладёт один прошедший купон, но перестрахуемся
        prev = _shift_months(period_end, 12 // max(1, len(dates)))
    value = schedule["coupons"][0]["value"]
    days = (period_end - prev).days or 1
    return value * (settle - prev).days / days


@router.get("/custom_floater", tags=["Calc"])
async def calc_custom_floater(
    base: str = Query(..., description="База: KEYRATE | RUONIA"),
    spread_bps: float = Query(..., ge=-1000, le=10000, description="Спред к базе, bps"),
    freq: int = Query(..., description="Выплат в год: 1/2/4/12"),
    maturity: str = Query(..., description="Дата погашения, ISO"),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
    face: float = Query(1000.0, gt=0, description="Номинал"),
):
    """Метрики кастомного ФЛОАТЕРА (база + спред): Y-IDX/SM/DM/YTM тем же
    прод-путём, что строки таблицы флоатеров (calculate_valuation_metrics).
    Сетка купонов якорится на погашение (first_coupon_date=None → шаг назад),
    будущие купоны — по форвардной кривой базы; НКД — линейно из первого
    купонного потока текущего периода."""
    base = base.strip().upper()
    if base not in ("KEYRATE", "RUONIA"):
        raise HTTPException(status_code=400, detail="base must be KEYRATE or RUONIA")
    if freq not in _FREQS:
        raise HTTPException(status_code=400, detail="freq must be 1, 2, 4 or 12")
    try:
        mat = date.fromisoformat(maturity)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad maturity date")

    from core.valuation import (BondRefData, build_cashflows_with_spread,
                                settle_date)
    from services.valuation import calculate_valuation_metrics
    ruonia_curve, keyrate_curve, cd, rd = await MarketDataService.get_curves()
    calc_date = cd or rd or date.today()
    if mat <= calc_date + timedelta(days=7):
        raise HTTPException(status_code=400, detail="maturity too close: nothing to discount")
    curve = ruonia_curve if base == "RUONIA" else keyrate_curve
    if curve is None:
        raise HTTPException(status_code=503, detail=f"{base} curve unavailable")

    ref = BondRefData(
        isin="CUSTOM", base=base, spread_issue_bps=int(round(spread_bps)),
        face_value=face, accrued_rub=0.0, maturity_date=mat,
        first_coupon_date=None, coupons_per_year=freq,
    )
    try:
        cfs = build_cashflows_with_spread(ref, curve, calc_date, ref.spread_issue_bps)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # НКД: линейно внутри текущего купонного периода из того же потока,
    # которым бумага прайсится (базис settle — доначислять уже не нужно)
    settle = settle_date(calc_date)
    accrued = 0.0
    for cf in cfs:
        if (cf.type == "COUPON" and cf.period_start and cf.period_end
                and cf.period_start <= settle < cf.period_end):
            days = (cf.period_end - cf.period_start).days or 1
            accrued = cf.amount_rub * (settle - cf.period_start).days / days
            break
    ref.accrued_rub = accrued

    m = calculate_valuation_metrics(ref, price, curve, calc_date,
                                    ruonia_curve=ruonia_curve)

    cashflow = [{"date": cf.pay_date.isoformat(),
                 "type": "COUPON" if cf.type == "COUPON" else "MATURITY",
                 "amount": round(cf.amount_rub, 2),
                 "rate_pct": round(cf.coupon_rate_pct, 2) if cf.coupon_rate_pct is not None else None}
                for cf in cfs if cf.pay_date > settle]
    cashflow.sort(key=lambda x: (x["date"], x["type"] == "COUPON"))

    return {
        "params": {"base": base, "spread_bps": ref.spread_issue_bps, "freq": freq,
                   "maturity": mat.isoformat(), "price_pct": price, "face": face},
        "metrics": {
            "y_idx_bps": m.get("yield_over_index_bps"),
            "sm_bps": m.get("sm_bps"), "dm_bps": m.get("disc_margin_bps"),
            "yield_xirr_pct": m.get("yield_xirr_pct"),
            "index_yield_pct": m.get("index_yield_pct"),
            "accrued_rub": round(accrued, 2),
            "dirty_rub": round(m["dirty_price_rub"], 2) if m.get("dirty_price_rub") else None,
            "pricing_status": m.get("pricing_status"),
        },
        "warnings": m.get("warnings") or [],
        "cashflow": cashflow,
        "calc_date": calc_date.isoformat(),
    }


@router.get("/custom", tags=["Calc"])
async def calc_custom(
    coupon_pct: float = Query(..., ge=0, le=100, description="Ставка купона, % годовых"),
    freq: int = Query(..., description="Выплат в год: 1/2/4/12"),
    maturity: str = Query(..., description="Дата погашения, ISO"),
    price: float = Query(..., gt=0, le=1000, description="Чистая цена, % от номинала"),
    face: float = Query(1000.0, gt=0, description="Номинал"),
):
    """Метрики кастомного фикса: YTM/тек.дох/G-спред/Z-спред/дюрации/convexity/
    DV01/НКД/dirty + будущий поток платежей. Кривая и calc_date — те же, что у
    вкладки ФИКСЫ."""
    if freq not in _FREQS:
        raise HTTPException(status_code=400, detail="freq must be 1, 2, 4 or 12")
    try:
        mat = date.fromisoformat(maturity)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad maturity date")

    from core.valuation import settle_date
    from services import fixed_income as fi
    _r, _k, cd, rd = await MarketDataService.get_curves()
    _ek, _eu, g = await MarketDataService.get_zspread_ctx()
    calc_date = cd or rd or date.today()
    if mat <= calc_date + timedelta(days=7):
        raise HTTPException(status_code=400, detail="maturity too close: nothing to discount")

    schedule = build_custom_schedule(mat, coupon_pct, freq, face, calc_date)
    settle = settle_date(calc_date)
    accrued = _accrued(schedule, settle, calc_date)
    m = fi.fixed_metrics_from_schedule(schedule, price, accrued, calc_date, g)

    # z-спред над КБД — тем же дискретным методом, что строки таблицы фиксов
    z = None
    if g is not None and getattr(g, "ok", lambda: False)() and m.get("dirty"):
        try:
            from services.zspread import solve_z_discrete
            cfs, _face, _put = fi.build_fixed_cashflows(schedule, calc_date)
            if cfs:
                z = solve_z_discrete(g, cfs, calc_date, m["dirty"])
        except Exception as e:
            logger.warning(f"custom z-spread error: {e}")

    cashflow = [{"date": c["end"], "type": "COUPON",
                 "amount": round(c["value"], 2), "rate_pct": coupon_pct}
                for c in schedule["coupons"] if date.fromisoformat(c["end"]) > settle]
    cashflow.append({"date": mat.isoformat(), "type": "MATURITY",
                     "amount": round(face, 2), "rate_pct": None})
    cashflow.sort(key=lambda x: (x["date"], x["type"] == "COUPON"))

    return {
        "params": {"coupon_pct": coupon_pct, "freq": freq, "maturity": mat.isoformat(),
                   "price_pct": price, "face": face},
        "metrics": {
            "ytm_pct": m.get("ytm_pct"),
            "cur_yield_pct": round(coupon_pct / (price / 100.0), 4) if price else None,
            "g_spread_bps": m.get("g_spread_bps"), "z_spread_bps": z,
            "mod_dur": m.get("mod_dur"), "mac_dur": m.get("mac_dur"),
            "convexity": m.get("convexity"), "dv01": m.get("dv01"),
            "accrued_rub": round(accrued, 2), "dirty_rub": round(m["dirty"], 2) if m.get("dirty") else None,
        },
        "cashflow": cashflow,
        "calc_date": calc_date.isoformat(),
    }
