"""Путь базовой ставки (КС или RUONIA): исторический факт (живьём с ЦБ РФ) +
рыночная траектория ожиданий из СПФИ.

Факт — из services.cbr (дневная история). Траектория ожиданий:
  КС    — по методике НРД met_float Прил.3: натуральный кубический сплайн через
          ставки СПФИ IRS KeyRate (проходит точно через свопы, C²), за последним
          свопом — экспо-затухание к долгосрочной нейтральной ставке ЦБ
          (services.implied_curve.KsExpectationCurve). Это и есть «ожидаемая КС(t)».
  RUONIA — форвард нашей bootstrap-кривы (Смита-Уилсона Прил.2 пока не реализован).
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


def build_path(curve, calc_date: date, series: str = "ks", hist_years: int = 3,
               ks_quotes: list = None) -> List[dict]:
    """Точки пути: факт (≤ calc_date, дневной с ЦБ, обрезан на hist_years назад) +
    рыночная траектория (помесячно вперёд).

    КС: траектория = KsExpectationCurve (сплайн свопов + затухание к нейтрали, НРД
    Прил.3) на ks_quotes; горизонт ~15 лет (видно реверс к нейтрали). RUONIA:
    форвард bootstrap-кривой до её последнего узла.
    """
    hist = cbr.ks_history() if series == "ks" else cbr.ruonia_history()
    cutoff = date(calc_date.year - hist_years, calc_date.month, min(calc_date.day, 28))
    out: List[dict] = []
    for d, v in hist:
        if cutoff <= d <= calc_date:
            out.append({"date": d.isoformat(), "actual_pct": round(v, 4), "market_pct": None})

    if out:
        out[-1]["market_pct"] = out[-1]["actual_pct"]  # стыковка факт→прогноз

    # --- КС: методика НРД Прил.3 (сплайн + затухание к нейтрали) ---
    if series == "ks" and ks_quotes:
        from services.implied_curve import KsExpectationCurve
        ksc = KsExpectationCurve(ks_quotes)
        horizon = date(calc_date.year + 15, calc_date.month, min(calc_date.day, 28))
        d = calc_date
        while d < horizon:
            t = (d - calc_date).days / 365.0
            out.append({"date": d.isoformat(), "actual_pct": None,
                        "market_pct": round(ksc.ks(t) * 100.0, 3)})
            d = _add_month(d)
        return out

    # --- RUONIA (или нет квот КС): форвард bootstrap-кривой ---
    if curve is not None:
        try:
            horizon = curve.nodes[-1][0]
        except Exception:
            horizon = calc_date + timedelta(days=3650)
        d = max(calc_date, curve.calc_date)
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
