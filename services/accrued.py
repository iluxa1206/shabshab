"""НКД: одна лестница источников для всех расчётов.

Раньше оценка НКД жила тремя независимыми реализациями — своя в живом расчёте
(экстраполяция прошлого купона), своя в as-of движке (спот-индекс плюс маржа),
своя в фолбэке по параметрам выпуска. На одной и той же бумаге они расходились
вдвое (ВЭБ2Р-50 25.08: 23 против 42 ₽ при биржевом факте 22,7), а значит
расходились и спреды, посчитанные разными путями.

Порядок такой, потому что каждый следующий шаг слабее предыдущего:
  1) КУПОН ОПУБЛИКОВАН — точный НКД из его суммы, спорить не о чем;
  2) СПЕКА ФИКСИНГА — та же ставка, по которой строится сам поток (окно, лаг,
     капитализация). Сверка с биржей 25.08: 32,93 против факта 32,97;
  3) ПРОШЛЫЙ КУПОН — пропорция по дням от последней известной ставки: не нужен
     ни индекс, ни спека, ошибка в проценты (23,06 против 22,70);
  4) ИНДЕКС + МАРЖА на дату — последняя соломинка, когда нет ни графика ставок,
     ни спеки. Систематически завышает при падающей ставке, поэтому последний.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


def accrued_for(periods, d: date, *, face: float,
                base: Optional[str] = None, margin_bps: Optional[int] = None,
                isin: Optional[str] = None, idx=None,
                index_pct: Optional[float] = None,
                calc_date: Optional[date] = None) -> tuple:
    """НКД на дату d → (значение, чем посчитали) либо (None, None).

    periods — [(start, end, value)]; value=None у необъявленного купона.
    idx — инжектированная история индекса (coupon_calib._index), чтобы не
    ходить в сеть на каждой бумаге; index_pct — факт индекса на d для шага 4.
    """
    from core.valuation import accrued_at, accrued_estimate, period_at

    exact = accrued_at(periods, d)
    if exact is not None:
        return exact, "купон опубликован"

    p = period_at(periods, d)
    if not p:
        return None, None
    s, e, _ = p
    days = max(0, (d - s).days)
    if days == 0:
        return 0.0, "период начался сегодня"

    if isin and base and face:
        rate = _rate_by_spec(isin, base, s, e, calc_date or d, idx)
        if rate is not None:
            full = rate + (margin_bps or 0) / 100.0
            return face * full / 100.0 * days / 365.0, "спека фиксинга"

    guess = accrued_estimate(periods, d)
    if guess is not None:
        return guess, "прошлый купон"

    if index_pct is not None and face:
        full = index_pct + (margin_bps or 0) / 100.0
        return face * full / 100.0 * days / 365.0, "индекс + маржа"
    return None, None


def _rate_by_spec(isin: str, base: str, start: date, end: date,
                  calc_date: date, idx) -> Optional[float]:
    """Ставка индекса за период по спеке фиксинга бумаги, % годовых."""
    try:
        from services.coupon_calib import _index, projected_ks_pct
        from services.ref_data import coupon_formula
        sp = coupon_formula(isin) or {}
        spec = {"mode": sp.get("coupon_mode") or "average",
                "lag": sp.get("fixing_lag") or 0,
                "lag_unit": sp.get("fixing_lag_unit") or "cal",
                "base": base,
                "avg_window_days": sp.get("avg_window_days"),
                "compounded": sp.get("compounded")}
        return projected_ks_pct(spec, start, end, calc_date,
                                fwd_pct=lambda _d: None, idx=idx or _index(base))
    except Exception as e:
        logger.debug("accrued by spec %s: %s", isin, e)
        return None
