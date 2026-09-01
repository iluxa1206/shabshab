"""Прайсинг КАСТОМНОЙ бумаги по введённым параметрам — без ISIN и реестра.

Единственный расчётный путь для «бумаги, которой нет в универсе»: вкладка
КАЛЬКУЛЯТОР (api/routes/calc) и анонсы первички (services/primary_pricing)
зовут ОДНИ И ТЕ ЖЕ функции. Раньше эта склейка жила прямо в теле роута; вторая
витрина превратила бы её во второй экземпляр прайсинга — ровно тот дубль,
который в проекте выкорчёвывали дважды (см. horizon-consistency, _dur_block).

Внутри — ничего своего: поток строит канонический build_cashflows_with_spread,
метрики считает calculate_valuation_metrics (флоатеры) и
fixed_income.fixed_metrics_from_schedule (фиксы), то есть те же функции, что
дают цифры строкам таблиц. Цифры калькулятора сравнимы с рынком один-в-один
именно поэтому.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from services.exceptions import APIException
from services.market_data import MarketDataService

logger = logging.getLogger(__name__)

FREQS = (1, 2, 4, 12)  # выплат в год


class CustomBondError(APIException):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__("CUSTOM_BOND", message, status_code)


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


def _check(freq: int, maturity, calc_date: date) -> date:
    """Общая валидация параметров обеих витрин."""
    if freq not in FREQS:
        raise CustomBondError("freq must be 1, 2, 4 or 12")
    mat = maturity if isinstance(maturity, date) else None
    if mat is None:
        try:
            mat = date.fromisoformat(str(maturity))
        except ValueError:
            raise CustomBondError("bad maturity date")
    # без хотя бы недели до погашения дисконтировать нечего
    if mat <= calc_date + timedelta(days=7):
        raise CustomBondError("maturity too close: nothing to discount")
    return mat


async def price_floater(base: str, spread_bps: float, freq: int, maturity,
                        price: float, face: float = 1000.0) -> dict:
    """Метрики кастомного ФЛОАТЕРА (база + спред): Y-IDX/SM/DM/YTM тем же
    прод-путём, что строки таблицы флоатеров (calculate_valuation_metrics).
    Сетка купонов якорится на погашение (first_coupon_date=None → шаг назад),
    будущие купоны — по форвардной кривой базы; НКД — линейно из первого
    купонного потока текущего периода."""
    base = (base or "").strip().upper()
    if base not in ("KEYRATE", "RUONIA"):
        raise CustomBondError("base must be KEYRATE or RUONIA")

    from core.valuation import (BondRefData, build_cashflows_with_spread,
                                settle_date)
    from services.valuation import calculate_valuation_metrics
    ruonia_curve, keyrate_curve, cd, rd = await MarketDataService.get_curves()
    calc_date = cd or rd or date.today()
    mat = _check(freq, maturity, calc_date)
    curve = ruonia_curve if base == "RUONIA" else keyrate_curve
    if curve is None:
        raise CustomBondError(f"{base} curve unavailable", status_code=503)

    ref = BondRefData(
        isin="CUSTOM", base=base, spread_issue_bps=int(round(spread_bps)),
        face_value=face, accrued_rub=0.0, maturity_date=mat,
        first_coupon_date=None, coupons_per_year=freq,
    )
    try:
        cfs = build_cashflows_with_spread(ref, curve, calc_date, ref.spread_issue_bps)
    except ValueError as e:
        raise CustomBondError(str(e))

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
            # спред-дюрация того же потока (единственный расчёт —
            # services.valuation._dur_block): ею калькулятор ставит точку
            # «вашей бумаги» на ту же ось, что и рынок
            "spread_dur_yrs": ((m.get("horizons") or {}).get("maturity") or {}).get("dur_yrs"),
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


async def price_fixed(coupon_pct: float, freq: int, maturity, price: float,
                      face: float = 1000.0) -> dict:
    """Метрики кастомного ФИКСА: YTM/тек.дох/G-спред/Z-спред/дюрации/convexity/
    DV01/НКД/dirty + будущий поток платежей. Кривая и calc_date — те же, что у
    вкладки ФИКСЫ."""
    from core.valuation import settle_date
    from services import fixed_income as fi
    _r, _k, cd, rd = await MarketDataService.get_curves()
    _ek, _eu, g = await MarketDataService.get_zspread_ctx()
    calc_date = cd or rd or date.today()
    mat = _check(freq, maturity, calc_date)

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
