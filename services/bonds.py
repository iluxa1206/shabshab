from datetime import date
from typing import Dict, Any, Optional

from valuation import BondRefData
from services.cashflow import parse_base_and_spread
from forwards import add_months


# База купона НРД -> наш тип
_NRD_BASE = {"CBRATED": "KEYRATE", "KEYRATE": "KEYRATE", "RUONIARATED": "RUONIA", "RUONIA": "RUONIA"}
_BASE_LABEL = {"KEYRATE": "Ключевая ставка", "RUONIA": "RUONIA"}


def _to_date(s):
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def build_ref_external(isin: str, mo: dict, nrd: Optional[dict]) -> BondRefData:
    """Строит BondRefData для ПРОИЗВОЛЬНОЙ бумаги (нет в isins_cache).
    Справочник — MOEX ISS; база+спред флоатера — из НРД (base_coupon_index, nominal_margin)."""
    mo = mo or {}
    try:
        face = float(mo.get("face") or 1000)
    except (ValueError, TypeError):
        face = 1000.0
    try:
        cp = int(mo.get("coupon_period")) if mo.get("coupon_period") else None
    except (ValueError, TypeError):
        cp = None
    try:
        accrued = float(mo.get("accrued")) if mo.get("accrued") is not None else 0.0
    except (ValueError, TypeError):
        accrued = 0.0

    base, spread = "UNKNOWN", 0
    if nrd:
        base = _NRD_BASE.get((nrd.get("base_coupon_index") or "").upper(), "UNKNOWN")
        nm = nrd.get("nominal_margin_bps")
        if nm is not None:
            spread = int(nm)

    issue = _to_date(mo.get("issue"))
    maturity = _to_date(mo.get("maturity"))
    cpy = round(365 / cp) if cp else 4
    first_coupon = add_months(issue, 12 // cpy) if (issue and cpy) else None

    return BondRefData(
        isin=isin, base=base, spread_issue_bps=spread, face_value=face,
        accrued_rub=accrued, maturity_date=maturity, first_coupon_date=first_coupon,
        coupons_per_year=cpy, issue_date=issue, coupon_period_days=cp,
    )


def external_formula(ref: BondRefData) -> str:
    """Синтетическая формула купона для отображения (нет FORMULA из кэша)."""
    label = _BASE_LABEL.get(ref.base, ref.base)
    if ref.spread_issue_bps:
        return f"{label} + {ref.spread_issue_bps / 100:g}%"
    return label


def next_coupon_after(ref_obj: BondRefData, today: date) -> Optional[date]:
    """Ближайшая купонная дата >= today (шагаем от первого купона по периоду).
    Для списка — корректная замена first_coupon_date (тот часто в прошлом)."""
    d = ref_obj.first_coupon_date
    if not d:
        return ref_obj.maturity_date
    step = 12 // (ref_obj.coupons_per_year or 4)
    guard = 0
    while d < today and guard < 600:
        if ref_obj.maturity_date and d >= ref_obj.maturity_date:
            return ref_obj.maturity_date
        d = add_months(d, step)
        guard += 1
    if ref_obj.maturity_date and d > ref_obj.maturity_date:
        return ref_obj.maturity_date
    return d

def create_bond_ref_data(data: dict, isin: str) -> BondRefData:
    """Helper to convert cache dict to BondRefData."""
    try:
        fv = float(data.get("FACEVALUE", 1000))
    except (ValueError, TypeError):
        fv = 1000.0

    try:
        start_date = date.fromisoformat(data.get("STARTDATE", ""))
    except (ValueError, TypeError):
        start_date = None

    try:
        end_date = date.fromisoformat(data.get("ENDDATE", "") or data.get("MATDATE", ""))
    except (ValueError, TypeError):
        end_date = None

    try:
        coupon_period_days = int(data.get("COUPONPERIOD", ""))
    except (ValueError, TypeError):
        coupon_period_days = None

    try:
        if data.get("FREQUENCY"):
            frequency = int(data.get("FREQUENCY"))
        else:
            frequency = None
    except (ValueError, TypeError):
        frequency = None

    try:
        next_coupon_date = date.fromisoformat(data.get("NEXTCOUPON")) if data.get("NEXTCOUPON") else None
    except (ValueError, TypeError):
        next_coupon_date = None
        
    acc_str = data.get('ACCRUEDINT')
    accrued = float(acc_str) if acc_str is not None else 0.0

    formula = data.get("FORMULA", "")
    base_rate_str = data.get("BASE_RATE")
    base, spread_bps = parse_base_and_spread(formula, base_rate_str)
    
    # Exact fallback from CLI for first coupon mapping
    step_months = 12 // (frequency or 4)
    from forwards import add_months
    first_coupon = None
    if start_date:
        first_coupon = add_months(start_date, step_months)

    return BondRefData(
        isin=isin,
        base=base or "UNKNOWN",
        spread_issue_bps=spread_bps,
        face_value=fv,
        accrued_rub=accrued,
        maturity_date=end_date,
        first_coupon_date=first_coupon, 
        coupons_per_year=frequency or 4,
        issue_date=start_date,
        coupon_period_days=coupon_period_days
    )

def extract_bond_reference_dict(isin: str, data: dict, ref_obj: BondRefData) -> Dict[str, Any]:
    next_coupon_date = None
    try:
        if data.get("NEXTCOUPON"):
            next_coupon_date = date.fromisoformat(data.get("NEXTCOUPON"))
    except:
        pass
        
    return {
        "isin": isin,
        "short_name": data.get("SHORTNAME", ""),
        "face_value": ref_obj.face_value,
        "face_unit": data.get("FACEUNIT", "RUB"),
        "base_rate_type": ref_obj.base,
        "spread_bps": ref_obj.spread_issue_bps,
        "formula": data.get("FORMULA", ""),
        "start_date": ref_obj.issue_date,
        "maturity_date": ref_obj.maturity_date,
        "coupon_period_days": ref_obj.coupon_period_days,
        "coupons_per_year": ref_obj.coupons_per_year,
        "next_coupon_date": next_coupon_date,
        "accrued_interest": ref_obj.accrued_rub
    }
