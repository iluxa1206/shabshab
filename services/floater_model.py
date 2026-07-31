"""Оценка флоатера «по методу 502_504» (лист Floater spread) — Фаза 1.

Отличие от прод-проекции (services.cashflow / valuation): купон фиксируется не
одним форвардом на период, а СРЕДНИМ дневного пути базовой ставки по окну
рефиксинга [купон − лаг − интервал, купон − лаг] (поля «лаг»/«интервал» —
конвенция рефиксинга выпуска). База берётся со СКЛЕЕННОГО пути: факт (прошлое) +
прогноз (будущее).

Прогноз пути = рыночный форвард нашей bootstrap-кривой IRS KEYRATE (та же, что
дисконтирует прайсинг). Ручные сценарии ЦБ убраны — путь объективно из рынка.

Купон[i] = (base_avg + spread) · days_i/365 · 100  (+100 в погашение).
Доходность = XIRR(потоки, даты) c dirty-ценой как оттоком в t0.

Фаза 1 — только KEYRATE-флоатеры (путь КС ступенчатый, строится из заседаний +
форварда). RUONIA-флоатеры требуют дневную историю RUONIA — отдельная фаза.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple

from services import cbr
from core.valuation import xirr


def actual_ks(d: date) -> Optional[float]:
    """Факт КС на дату (decimal) — последняя дневная КС ЦБ ≤ d."""
    val = None
    for md, r in cbr.ks_history():
        if md <= d:
            val = r / 100.0
        else:
            break
    return val


def make_ks_path(curve, calc_date: date) -> Callable[[date], float]:
    """Возвращает функцию date→КС(decimal): факт до calc_date, после — рыночный
    форвард bootstrap-кривой IRS KEYRATE СТУПЕНЬЮ сегмента (daily_forward):
    между тенорами ставка флэт, скачок на узле — как реальная КС между
    заседаниями. Раньше — окно +30д, интерполировавшее ступень."""
    def path(d: date) -> float:
        a = actual_ks(d)
        if d <= calc_date and a is not None:
            return a
        if curve is not None:
            try:
                if d >= curve.calc_date:
                    return curve.daily_forward(d)
            except Exception:
                pass
        return a if a is not None else 0.0
    return path


def _avg_days(path: Callable[[date], float], w_start: date, w_end: date) -> float:
    """Среднее пути базы по дням окна [w_start, w_end)."""
    days = (w_end - w_start).days or 1
    total = 0.0
    for k in range(days):
        total += path(w_start + timedelta(days=k))
    return total / days


def period_base_avg(path: Callable[[date], float], start: date, end: date,
                    spec: Optional[dict] = None) -> float:
    """База периода (decimal) ПО СПЕКЕ ФИКСИНГА выпуска (ref_data.coupon_formula:
    coupon_mode/fixing_lag/avg_window_days). Раньше метод хардкодил лаг 7 и окно
    «период с якорем на end» для всех бумаг — расходился с прайсингом на
    point/avg_prev/больших лагах. None-спека → дефолт: среднее периода без лага."""
    spec = spec or {}
    lag = int(spec.get("fixing_lag") or 0)
    mode = spec.get("coupon_mode")
    w = spec.get("avg_window_days")
    period = (end - start).days or 1
    if w:                                     # окно [start−lag−w, start−lag)
        hi = start - timedelta(days=lag)
        return path(hi - timedelta(days=1)) if int(w) <= 1 else _avg_days(path, hi - timedelta(days=int(w)), hi)
    if mode == "point":
        return path(start - timedelta(days=lag))
    if mode == "month_start":
        return path(start.replace(day=1))
    if mode == "avg_prev":                    # окно [start−lag−period, start−lag)
        hi = start - timedelta(days=lag)
        return _avg_days(path, hi - timedelta(days=period), hi)
    # average / дефолт: дни дохода (start, end] со сдвигом lag
    return _avg_days(path, start + timedelta(days=1 - lag), end + timedelta(days=1 - lag))


def project_floater(
    coupon_dates: List[Tuple[date, date]],   # [(start, end)] будущих периодов
    spread_pct: float,                        # маржа выпуска, % (0.0125 → 1.25%)
    maturity: date,
    path: Callable[[date], float],
    calc_date: date,
    spec: Optional[dict] = None,              # спека фиксинга (ref_data.coupon_formula)
) -> List[Tuple[date, float]]:
    """Потоки флоатера (дата, сумма % от номинала) по методу файла.

    Купон = (base_по_спеке_фиксинга + spread)·days/365·100; +100 в погашение.
    Только будущие купоны (end > calc_date)."""
    cfs: List[Tuple[date, float]] = []
    for start, end in coupon_dates:
        if end <= calc_date:
            continue
        days = (end - start).days or 1
        base = period_base_avg(path, start, end, spec)
        coupon = (base + spread_pct) * days / 365.0 * 100.0
        if end >= maturity:
            coupon += 100.0
        cfs.append((end, round(coupon, 5)))
    return cfs


def floater_xirr_pct(cfs: List[Tuple[date, float]], dirty_price_pct: float,
                     calc_date: date) -> Optional[float]:
    """XIRR-доходность, % годовых. dirty_price_pct — грязная цена (flat+НКД), %."""
    flows = [(calc_date, -dirty_price_pct)] + [(d, a) for d, a in cfs if d > calc_date]
    r = xirr(flows)
    return round(r * 100.0, 4) if r is not None else None
