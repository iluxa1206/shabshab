"""Реконструкция формулы КС-купона из реализованных выплат (публичного источника
формулы нет — эмиссионные доки не машиночитаемы, MOEX/НРД формулу не отдают).

Ставка прошлого купона = value·365/(days·face). Наблюдённая КС = ставка − маржа.
Подбираем формулу выпуска по прошлым купонам. Два типа КС-флоатеров:
  • point   — КС на дату (начало периода − lag);
  • average — среднее КС за период по дням, каждый день с лагом lag (RUONIA-стиль).
Возвращаем спеку с лучшим фитом (ошибка < порога), иначе None (формула сложнее:
кэп/флор/иной индекс, или маржа неверна) → фолбэк на форвард-проекцию.

Спека применяется к текущему/будущим незафиксированным купонам для точной
проекции: прошлые дни периода — факт КС ЦБ, будущие — форвард-прогноз."""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional, Callable, List, Tuple

from services import cbr

_MAX_LAG = 12
_ERR_TOL_PP = 0.10     # порог средней ошибки, п.п.
_cache: dict = {}


import bisect

# Индекс истории для O(log n) поиска: (dates[], rates[]) по base, кэш на день.
_idx_cache: dict = {}


def _index(base: str):
    hist = cbr.ruonia_history() if base == "RUONIA" else cbr.ks_history()
    key = (base, len(hist), hist[-1][0] if hist else None)
    cached = _idx_cache.get(base)
    if cached and cached[0] == key:
        return cached[1], cached[2]
    dates = [d for d, _ in hist]
    rates = [r for _, r in hist]
    _idx_cache[base] = (key, dates, rates)
    return dates, rates


def _rate_at(idx, d: date) -> Optional[float]:
    """idx = (dates, rates), отсортированы по дате. Последняя ставка ≤ d (bisect)."""
    dates, rates = idx
    i = bisect.bisect_right(dates, d) - 1
    return rates[i] if i >= 0 else None


def _rate_avg(idx, s: date, e: date, lag: int) -> Optional[float]:
    tot, n, cur = 0.0, 0, s
    while cur < e:
        k = _rate_at(idx, cur - timedelta(days=lag))
        if k is not None:
            tot += k
            n += 1
        cur += timedelta(days=1)
    return tot / n if n else None


def _past_rows(coupons: list, margin_pct: float, face: float, calc_date: date):
    rows = []
    for c in coupons or []:
        s, e, v = c.get("start"), c.get("end"), c.get("value")
        if not s or not e or v is None:
            continue
        try:
            s = date.fromisoformat(s) if isinstance(s, str) else s
            e = date.fromisoformat(e) if isinstance(e, str) else e
        except (ValueError, TypeError):
            continue
        if s >= calc_date:
            continue
        days = (e - s).days or 1
        rows.append((s, e, float(v) / face * 365.0 / days * 100.0 - margin_pct))
    return rows[-8:]


def calibrate(isin: str, coupons: list, margin_pct: float, face: float,
              calc_date: date, base: str = "KEYRATE") -> Optional[dict]:
    """Спека формулы {'mode':'point'|'average','lag':int} по прошлым купонам.
    base='KEYRATE' — факт КС ЦБ; 'RUONIA' — дневной RUONIA (обычно average)."""
    ck = (isin, base)
    if ck in _cache:
        return _cache[ck]
    idx = _index(base)
    rows = _past_rows(coupons, margin_pct, face, calc_date)
    spec = None
    if len(rows) >= 2 and idx[0]:
        best = None  # (err, mode, lag)
        for lag in range(0, _MAX_LAG + 1):
            e_pt, e_av, n = 0.0, 0.0, 0
            for s, e, obs in rows:
                kp = _rate_at(idx, s - timedelta(days=lag))
                ka = _rate_avg(idx, s, e, lag)
                if kp is None or ka is None:
                    continue
                e_pt += abs(kp - obs)
                e_av += abs(ka - obs)
                n += 1
            if not n:
                continue
            for mode, err in (("point", e_pt / n), ("average", e_av / n)):
                if best is None or err < best[0]:
                    best = (err, mode, lag)
        if best and best[0] < _ERR_TOL_PP:
            spec = {"mode": best[1], "lag": best[2], "err_pp": round(best[0], 4), "base": base}
    _cache[ck] = spec
    return spec


def projected_ks_pct(spec: dict, start: date, end: date, calc_date: date,
                     fwd_pct: Callable[[date], float]) -> float:
    """Компонента ставки купона (%) по спеке: прошлые дни — факт ЦБ (КС/RUONIA),
    будущие — fwd_pct(date). point → одна дата; average → среднее по дням."""
    idx = _index(spec.get("base", "KEYRATE"))
    lag = spec.get("lag", 0)
    if spec.get("mode") == "point":
        fix = start - timedelta(days=lag)
        return (_rate_at(idx, fix) if fix <= calc_date else fwd_pct(fix)) or 0.0
    tot, n, cur = 0.0, 0, start
    while cur < end:
        obs = cur - timedelta(days=lag)
        k = _rate_at(idx, obs) if obs <= calc_date else fwd_pct(obs)
        if k is not None:
            tot += k
            n += 1
        cur += timedelta(days=1)
    return (tot / n) if n else 0.0
