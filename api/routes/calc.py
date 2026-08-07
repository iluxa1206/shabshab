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
