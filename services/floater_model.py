"""Оценка флоатера «по методу 502_504» (лист Floater spread) — Фаза 1.

Отличие от прод-проекции (services.cashflow / valuation): купон фиксируется не
одним форвардом на период, а СРЕДНИМ дневного пути базовой ставки по окну
рефиксинга [купон − лаг − интервал, купон − лаг] (поля «лаг»/«интервал» СПФИ-
конвенции). База берётся со СКЛЕЕННОГО пути: факт (прошлое) + прогноз (будущее).

Прогноз пути — два режима (переключатель T1 в файле):
  mode="market"   — рыночный форвард нашей bootstrap-кривой КС (что в свопах);
  mode="scenario" — ручной сценарий ЦБ (flat/base/fast из ks_path).

Купон[i] = (base_avg + spread) · days_i/365 · 100  (+100 в погашение).
Доходность = XIRR(потоки, даты) c dirty-ценой как оттоком в t0.

Фаза 1 — только KEYRATE-флоатеры (путь КС ступенчатый, строится из заседаний +
форварда). RUONIA-флоатеры требуют дневную историю RUONIA — отдельная фаза.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple

from services.ks_path import _MEETINGS
from valuation import xirr

# Факт КС по датам заседаний (ступенька) — из встроенной таблицы ks_path.
_ACTUAL_KS = [(date.fromisoformat(d), a) for d, a, *_ in _MEETINGS]
_SCEN_IDX = {"flat": 2, "base": 3, "fast": 4}


def actual_ks(d: date) -> Optional[float]:
    """Факт КС на дату (decimal) — последнее заседание ≤ d."""
    val = None
    for md, a in _ACTUAL_KS:
        if md <= d:
            val = a
        else:
            break
    return val


def scenario_ks(d: date, scenario: str) -> float:
    """Сценарный КС на дату (decimal) — последнее заседание ≤ d, колонка сценария."""
    idx = _SCEN_IDX.get(scenario, 3)
    val = _MEETINGS[0][idx]
    for row in _MEETINGS:
        if date.fromisoformat(row[0]) <= d:
            val = row[idx]
        else:
            break
    return val


def make_ks_path(curve, calc_date: date, mode: str = "market",
                 scenario: str = "base") -> Callable[[date], float]:
    """Возвращает функцию date→КС(decimal): факт до calc_date, прогноз после.

    mode="market"  — прогноз = форвард кривой КС на ~30д от даты (в конвенции КС);
    mode="scenario"— прогноз = ступенька выбранного сценария ЦБ.
    """
    def path(d: date) -> float:
        a = actual_ks(d)
        if d <= calc_date and a is not None:
            return a
        if mode == "scenario":
            return scenario_ks(d, scenario)
        # market: форвард кривой (конвенция КС ≈ уровень ставки)
        if curve is not None:
            try:
                end = d + timedelta(days=30)
                if d >= curve.calc_date and d < end:
                    return curve.forward(max(d, curve.calc_date), end)
            except Exception:
                pass
        return a if a is not None else 0.0
    return path


def _avg_over_window(path: Callable[[date], float], end: date,
                     lag_days: int, interval_days: int) -> float:
    """Среднее пути базы по окну рефиксинга [end−lag−interval, end−lag]."""
    w_end = end - timedelta(days=lag_days)
    w_start = w_end - timedelta(days=interval_days)
    days = (w_end - w_start).days or 1
    total = 0.0
    for k in range(days):
        total += path(w_start + timedelta(days=k))
    return total / days


def project_floater(
    coupon_dates: List[Tuple[date, date]],   # [(start, end)] будущих периодов
    spread_pct: float,                        # маржа выпуска, % (0.0125 → 1.25%)
    maturity: date,
    path: Callable[[date], float],
    calc_date: date,
    lag_days: int = 7,
    interval_days: Optional[int] = None,      # None → длина купонного периода
) -> List[Tuple[date, float]]:
    """Потоки флоатера (дата, сумма % от номинала) по методу файла.

    Купон = (avg_base_по_окну + spread)·days/365·100; +100 в дату погашения.
    Только будущие купоны (end > calc_date)."""
    cfs: List[Tuple[date, float]] = []
    for start, end in coupon_dates:
        if end <= calc_date:
            continue
        days = (end - start).days or 1
        iv = interval_days if interval_days is not None else days
        base = _avg_over_window(path, end, lag_days, iv)
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
