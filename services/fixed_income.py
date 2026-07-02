"""Метрики облигаций с фиксированным купоном: YTM, мод. дюрация, DV01, G-spread.

Кэшфлоу — из реального расписания MOEX bondization (fetch_bond_schedule_full):
купоны с известным value + амортизации. Будущие купоны без value (купон после
оферты не определён) → оценка к оферте (yield-to-put): поток до последнего
известного купона + выкуп остаточного номинала на его дату.

YTM — через xirr (эффективная годовая, ACT/365, как НРД). Дюрация — численная
(bump ±10бп по ставке дисконтирования). G-spread = YTM − КБД(τ=дюрация).
Все расчёты в валюте номинала; конверсия в рубли — на уровне портфеля.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from valuation import xirr, xnpv
from services.market_data import MarketDataService


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def build_fixed_cashflows(schedule: dict, calc_date: date) -> Tuple[List[tuple], float, Optional[date]]:
    """Будущие кэшфлоу (pay_date, amount) из bondization + остаточный номинал на calc_date.

    Будущие купоны без value (после оферты купон не определён) → поток обрезается
    на последнем ИЗВЕСТНОМ купоне, и на его дату добавляется выкуп остаточного
    номинала (put@100) — стандартный yield-to-worst/to-put для корпов с офертой.
    Возвращает (cfs, current_face, put_date): put_date=None если поток полный
    до погашения (редемпшн из амортизаций bondization).
    """
    coupons: List[tuple] = []      # (date, value) известных будущих купонов
    put_date: Optional[date] = None
    current_face = None

    for c in schedule.get("coupons", []):
        end = _d(c.get("end"))
        if end is None or end <= calc_date:
            continue
        # первый будущий купон несёт face текущего периода (остаточный номинал)
        if current_face is None and c.get("face") is not None:
            current_face = float(c["face"])
        if c.get("value") is None:
            # первый неизвестный купон → оценка к оферте: всё после отбрасываем
            put_date = coupons[-1][0] if coupons else None
            break
        coupons.append((end, float(c["value"])))

    face = current_face if current_face is not None else 1000.0
    cfs = list(coupons)

    if put_date is not None:
        cfs.append((put_date, face))  # выкуп по номиналу на дату оферты
    else:
        for a in schedule.get("amorts", []):
            d = _d(a.get("date"))
            if d is None or d <= calc_date:
                continue
            cfs.append((d, float(a["value"])))

    cfs.sort(key=lambda x: x[0])
    return cfs, face, put_date


def fixed_metrics_from_schedule(
    schedule: dict,
    price_pct: float,
    accrued: float,
    calc_date: date,
    g_curve=None,
) -> dict:
    """{'ytm_pct','mod_dur','dv01','g_spread_bps','dirty','face_current','complete'}.

    price_pct — чистая цена в % от остаточного номинала; accrued — НКД в валюте
    номинала на одну бумагу. dirty/dv01 — на одну бумагу в валюте номинала.
    """
    out = {"ytm_pct": None, "mod_dur": None, "dv01": None,
           "g_spread_bps": None, "dirty": None, "face_current": None, "put_date": None}
    cfs, face, put_date = build_fixed_cashflows(schedule, calc_date)
    out["face_current"] = face
    out["put_date"] = put_date.isoformat() if put_date else None
    if not cfs:
        return out

    dirty = face * price_pct / 100.0 + (accrued or 0.0)
    out["dirty"] = dirty
    if dirty <= 0:
        return out

    flows = [(calc_date, -dirty)] + cfs
    y = xirr(flows)
    if y is None:
        return out
    out["ytm_pct"] = round(y * 100.0, 2)

    # численная дюрация: PV при y±10бп (без цены в потоке)
    dy = 0.001
    try:
        pv_dn = xnpv(y - dy, cfs)
        pv_up = xnpv(y + dy, cfs)
    except ValueError:
        return out
    if pv_dn <= 0 or pv_up <= 0:
        return out
    mod_dur = (pv_dn - pv_up) / (2.0 * dirty * dy)
    out["mod_dur"] = round(mod_dur, 2)
    out["dv01"] = round(mod_dur * dirty * 1e-4, 4)  # ₽(валюта)/бумагу на 1бп

    if g_curve is not None and getattr(g_curve, "ok", lambda: False)():
        tau = max(mod_dur, 0.01)
        out["g_spread_bps"] = round((y - g_curve.r(tau)) * 10000.0)
    return out


async def fixed_metrics(isin: str, price_pct: float, accrued: float,
                        calc_date: date, g_curve=None) -> dict:
    """Обёртка: тянет bondization из кэша MarketDataService и считает метрики."""
    schedule = await MarketDataService.fetch_bond_schedule_full(isin)
    return fixed_metrics_from_schedule(schedule, price_pct, accrued, calc_date, g_curve)
