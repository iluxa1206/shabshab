from datetime import date
from typing import Dict, Any, Optional

from forwards import DiscountCurve
from valuation import (
    BondRefData,
    dirty_price_rub,
    build_cashflows_with_spread,
    xirr_yield_pct,
    solve_dm_bps,
    implied_yield_pct
)

def calculate_valuation_metrics(
    bond: BondRefData,
    price: float,
    curve: DiscountCurve,
    calc_date: date,
    accrued_override: float = None,
    periods=None,
    amorts=None,
) -> Dict[str, Any]:
    """
    Computes all valuation metrics for a given bond and price.
    accrued_override — НКД на calc_date из MOEX (приоритет над стейл-кэшем).
    periods — реальное расписание купонов [(start,end,value),...] из MOEX;
              value (зафикс. рублёвая сумма купона) прокидывается в DM-cashflow,
              чтобы текущий/прошлый купон брался фактом, а не перепрогнозом.
    amorts — график амортизаций MOEX [{date, value},...] для DM амортизируемых бумаг.
    Returns a dictionary suitable for formatting by Pydantic.
    """
    accrued = accrued_override if accrued_override is not None else bond.accrued_rub
    dirty_rub = dirty_price_rub(bond.face_value, price, accrued)

    # DM считается по cfs с реальным спредом: value зафикс. купонов сохраняем
    # (факт MOEX), амортизации учитываем. base_cfs (spread=0) — контрфактуал только
    # для spread_to_base_bps: там купон проектируем (value не применим к spread=0),
    # поэтому передаём стрипнутые пары без value.
    base_periods = [(p[0], p[1]) for p in periods] if periods else None

    cfs = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps,
                                      explicit_periods=periods, amorts=amorts)
    base_cfs = build_cashflows_with_spread(bond, curve, calc_date, 0,
                                           explicit_periods=base_periods, amorts=amorts)

    try:
        impl_yield = xirr_yield_pct(dirty_rub, cfs, calc_date)
    except Exception as e:
        print(f"XIRR error for {bond.isin}: {e}")
        impl_yield = None
        
    try:
        base_yield = xirr_yield_pct(dirty_rub, base_cfs, calc_date)
    except Exception as e:
        base_yield = None

    spread_to_base_bps = None
    if impl_yield is not None and base_yield is not None:
        spread_to_base_bps = round((impl_yield - base_yield) * 100.0)
        
    # DM Calculation
    dm_bps = None
    try:
        if curve and len(cfs) > 0:
            dm_bps = solve_dm_bps(bond, curve, cfs, calc_date, dirty_rub)
    except Exception as e:
        print(f"DM calculation error for {bond.isin}: {e}")
        
    return {
        "clean_price_pct": price,
        "dirty_price_rub": dirty_rub,
        "dm_bps": dm_bps,
        "dm_label": "to_maturity" if dm_bps is not None else None,
        "yield_xirr_pct": round(impl_yield, 4) if impl_yield is not None else None,
        "base_yield_pct": round(base_yield, 4) if base_yield is not None else None,
        "spread_to_base_bps": spread_to_base_bps,
        "pricing_status": "SUCCESS" if dm_bps is not None else "DM_FAILED",
        "warnings": []
    }
