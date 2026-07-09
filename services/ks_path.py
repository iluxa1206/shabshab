"""Путь базовой ставки (КС или RUONIA): исторический факт (живьём с ЦБ РФ) +
рыночный форвард из СПФИ (наш bootstrap).

Факт и текущее значение — из services.cbr (cbr.ru, дневная история). Рыночный
форвард — помесячная выборка forward() нашей кривой (КС в конвенции КС, RUONIA в
daily-comp). Ручные сценарии ЦБ убраны — траектория объективно из рынка.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import List, Optional

from services import cbr


def current_rate_pct(series: str, calc_date: date) -> Optional[float]:
    """Действующая базовая ставка, % — живьём с ЦБ (КС или RUONIA)."""
    return cbr.current_ks() if series == "ks" else cbr.current_ruonia()


def _add_month(d: date) -> date:
    m = d.month % 12 + 1
    y = d.year + (1 if d.month == 12 else 0)
    day = min(d.day, 28)
    return date(y, m, day)


def build_path(curve, calc_date: date, series: str = "ks",
               hist_years: int = 3) -> List[dict]:
    """Точки пути: факт (≤ calc_date, дневной с ЦБ, обрезан на hist_years назад) +
    рыночный форвард (помесячно от calc_date до горизонта кривой).

    series="ks"|"ruonia". actual_pct — факт; market_pct — форвард кривой.
    """
    hist = cbr.ks_history() if series == "ks" else cbr.ruonia_history()
    cutoff = date(calc_date.year - hist_years, calc_date.month, min(calc_date.day, 28))
    out: List[dict] = []
    for d, v in hist:
        if cutoff <= d <= calc_date:
            out.append({"date": d.isoformat(), "actual_pct": round(v, 4), "market_pct": None})

    # рыночный форвард помесячно до последнего узла кривой
    if curve is not None:
        try:
            horizon = curve.nodes[-1][0]
        except Exception:
            horizon = calc_date + timedelta(days=3650)
        d = max(calc_date, curve.calc_date)
        # состыковать начало форварда с последним фактом (без разрыва)
        if out:
            out[-1]["market_pct"] = out[-1]["actual_pct"]
        while d < horizon:
            nxt = _add_month(d)
            try:
                start = max(d, curve.calc_date)
                if start < nxt:
                    mv = curve.forward(start, min(nxt, horizon)) * 100.0
                    out.append({"date": d.isoformat(), "actual_pct": None, "market_pct": round(mv, 3)})
            except Exception:
                pass
            d = nxt
    return out
