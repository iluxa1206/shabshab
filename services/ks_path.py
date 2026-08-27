"""Путь базовой ставки (КС или RUONIA): исторический факт (живьём с ЦБ РФ) +
рыночная траектория ожиданий из bootstrap-кривой свопов.

Факт — из services.cbr (дневная история). Траектория ожиданий:
  КС    — короткий (~помесячный) форвард НАШЕЙ bootstrap-кривой IRS KEYRATE (та же,
          что дисконтирует SM/z — арбитраж-консистентно с прайсингом). Горизонт —
          последний узел кривой (10Y). Плюс DISPLAY-ONLY линии: прогноз ЦБ (ступени
          на заседаниях) и НРД Прил.3 (KsExpectationCurve: сплайн свопов + экспо-
          затухание к нейтрали за последним тенором). Реплика листа IRS файла
          502_504 (excel_ks_forward_segments) осталась в implied_curve для сверки с
          файлом, в путь НЕ идёт (её форвард не арбитражен, чарт расходился с ценой).
  RUONIA — короткий форвард нашей bootstrap-кривой OIS (Смита-Уилсона Прил.2 пока нет).
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

    КС: траектория = короткий (~помесячный) форвард bootstrap-кривой IRS KEYRATE
    на ks_quotes; горизонт — последний тенор (10Y). + DISPLAY-линии прогноз ЦБ и
    НРД Прил.3. RUONIA: форвард bootstrap-кривой OIS до её последнего узла.
    """
    hist = cbr.ks_history() if series == "ks" else cbr.ruonia_history()
    cutoff = date(calc_date.year - hist_years, calc_date.month, min(calc_date.day, 28))
    out: List[dict] = []
    for d, v in hist:
        if cutoff <= d <= calc_date:
            out.append({"date": d.isoformat(), "actual_pct": round(v, 4), "market_pct": None})

    if out:
        out[-1]["market_pct"] = out[-1]["actual_pct"]  # стыковка факт→прогноз

    # --- КС: рыночная траектория = короткий форвард bootstrap-кривой IRS KEYRATE
    # (помесячные сегменты, см. ниже) + прогноз ЦБ (ступени на заседаниях) ---
    if series == "ks" and ks_quotes and curve is not None:
        from services.implied_curve import KsExpectationCurve
        from services import cbr_forecast
        # «Рынок» = короткий форвард НАШЕЙ bootstrap-кривой. Раньше здесь была
        # excel_ks_forward_segments (реплика листа IRS): её форвард на ~1.7-2.5пп
        # ниже bootstrap и НЕ арбитражен (zero из неё < par на растущей кривой) →
        # чарт расходился с этой кривой. Реплика осталась в implied_curve для
        # сверки с файлом.
        #
        # ВАЖНО, ЧТОБЫ НЕ ВВОДИЛО В ЗАБЛУЖДЕНИЕ: «согласовано с прайсингом» здесь
        # НЕ значит «та же кривая». Купоны флоатеров прайсятся на
        # core.forwards.SheetForwardCurve — то есть на той самой методике листа, и
        # это осознанный выбор (решение 2026-08-26, см. её докстринг). График КС
        # намеренно строится на bootstrap, потому что тут нужна безарбитражность
        # пути ставки, а не воспроизведение листа. Две кривые расходятся на
        # 70-90 bps НА ДЛИННЫХ ТЕНОРАХ — это ожидаемо, а не баг.
        # НРД met_float Прил.3: ожидаемая КС = сплайн свопов + затухание к нейтрали
        # ЦБ за последним тенором (DISPLAY-ONLY, см. implied_curve).
        pril3 = KsExpectationCurve(ks_quotes)
        cur_ks = cbr.current_ks() or 0.0
        fc_path = cbr_forecast.meeting_step_path(calc_date, cur_ks)

        def fc_level(d: date) -> Optional[float]:
            if not fc_path:
                return None
            v = cur_ks
            for md, lv in fc_path:
                if md <= d:
                    v = lv
                else:
                    break
            return v

        # Горизонт market/forecast — последний узел bootstrap-кривой (10Y). Прил.3
        # продлеваем на +10 лет за него, чтобы экспо-реверсия к нейтрали была ВИДНА
        # (внутри 10Y Прил.3 ≈ свопам, весь смысл линии — хвост затухания).
        try:
            h_market = curve.nodes[-1][0]
        except Exception:
            h_market = calc_date + timedelta(days=3650)
        horizon = h_market + timedelta(days=3650)
        d = calc_date
        while d < horizon:
            in_market = d < h_market
            mv = fcv = None
            if in_market:
                f_start = max(d, curve.calc_date)
                nxt = _add_month(d)
                if f_start < nxt:
                    try:
                        mv = curve.forward(f_start, min(nxt, h_market)) * 100.0
                    except Exception:
                        mv = None
                fcv = fc_level(d)
            t_years = (d - calc_date).days / 365.0
            p3 = pril3.ks(t_years) * 100.0
            out.append({"date": d.isoformat(), "actual_pct": None,
                        "market_pct": round(mv, 3) if mv is not None else None,
                        "forecast_pct": round(fcv, 3) if fcv is not None else None,
                        "nrd_pril3_pct": round(p3, 3)})
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
