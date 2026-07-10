from datetime import date
from typing import Dict, Any, Optional

from forwards import DiscountCurve
from valuation import (
    BondRefData,
    dirty_price_rub,
    build_cashflows_with_spread,
    xirr_yield_pct,
    solve_dm_bps,
    solve_discount_margin_bps,
    current_index_pct,
    FlatForwardCurve,
    implied_yield_pct,
)

def calculate_valuation_metrics(
    bond: BondRefData,
    price: float,
    curve: DiscountCurve,
    calc_date: date,
    accrued_override: float = None,
    periods=None,
    amorts=None,
    offers=None,
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
    # Бумага гасится не позже даты расчётов T+1: покупателю не достаётся ни одного
    # платежа (весь поток ex) — метрики бессмысленны, а стейл prev-цена давала
    # мусорные отрицательные SM (Магнит4P06 за 2 дня до погашения: SM −330).
    from valuation import settle_date as _sd
    if bond.maturity_date is not None and bond.maturity_date <= _sd(calc_date):
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "base_yield_pct": None, "spread_to_base_bps": None,
            "pricing_status": "MATURED", "warnings": ["Погашение ≤ T+1 — потоки покупателю не достаются"],
        }

    # Перпы/суборды без даты погашения: поток не терминируется — флоатер-метрики
    # (SM/DM к погашению) не определены, выходим без крэша.
    if bond.maturity_date is None:
        return {
            "clean_price_pct": price, "dirty_price_rub": None,
            "dm_bps": None, "sm_bps": None, "disc_margin_bps": None, "dm_label": None,
            "yield_xirr_pct": None, "base_yield_pct": None, "spread_to_base_bps": None,
            "pricing_status": "NO_MATURITY", "warnings": ["Нет даты погашения (перп/суборд)"],
        }

    accrued = accrued_override if accrued_override is not None else bond.accrued_rub
    # T+1: амортизация в окне (calc, settle] — продавцу; цена котируется от остатка
    from valuation import face_for_pricing
    _pricing_face = face_for_pricing(bond.face_value, amorts, calc_date)
    dirty_rub = dirty_price_rub(_pricing_face, price, accrued)

    # DM считается по cfs с реальным спредом: value зафикс. купонов сохраняем
    # (факт MOEX), амортизации учитываем. base_cfs (spread=0) — контрфактуал только
    # для spread_to_base_bps: там купон проектируем (value не применим к spread=0),
    # поэтому передаём стрипнутые пары без value.
    base_periods = [(p[0], p[1]) for p in periods] if periods else None

    cfs = build_cashflows_with_spread(bond, curve, calc_date, bond.spread_issue_bps,
                                      explicit_periods=periods, amorts=amorts, offers=offers)
    base_cfs = build_cashflows_with_spread(bond, curve, calc_date, 0,
                                           explicit_periods=base_periods, amorts=amorts,
                                           offers=offers)

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
        
    # SIMPLE MARGIN (наш sm_bps): дисконт по форвард-кривей+спред. Воспроизводит
    # НРД simple_margin (сверка: ликвид near-par med 0-2bps). Поле dm_bps сохранено
    # для обратной совместимости = то же значение (это простая маржа, не discount).
    sm_bps = None
    try:
        if curve and len(cfs) > 0:
            sm_bps = solve_dm_bps(bond, curve, cfs, calc_date, dirty_rub)
    except Exception as e:
        print(f"SM calculation error for {bond.isin}: {e}")

    # DISCOUNT MARGIN (наш disc_margin_bps): настоящий FRN DM — индекс плоский на
    # ТЕКУЩЕМ уровне (из зафикс. купона), money-market дисконт (L+DM). Воспроизводит
    # НРД discount_margin (med −20, m|Δ|≈47bps; остаток — их проприетарная машина).
    disc_margin_bps = None
    try:
        L = current_index_pct(periods, calc_date, bond.spread_issue_bps, bond.face_value)
        if L is not None:
            flat = FlatForwardCurve(calc_date, L)
            flat_cfs = build_cashflows_with_spread(bond, flat, calc_date, bond.spread_issue_bps,
                                                   explicit_periods=periods, amorts=amorts,
                                                   offers=offers)
            disc_margin_bps = solve_discount_margin_bps(flat_cfs, calc_date, dirty_rub, L)
    except Exception as e:
        print(f"Discount margin error for {bond.isin}: {e}")

    return {
        "clean_price_pct": price,
        "dirty_price_rub": dirty_rub,
        "dm_bps": sm_bps,                      # backward-compat (= simple margin)
        "sm_bps": sm_bps,                      # simple margin (наш) ≈ НРД simple_margin
        "disc_margin_bps": disc_margin_bps,    # discount margin (наш) ≈ НРД discount_margin
        "dm_label": "simple_margin" if sm_bps is not None else None,
        "yield_xirr_pct": round(impl_yield, 4) if impl_yield is not None else None,
        "base_yield_pct": round(base_yield, 4) if base_yield is not None else None,
        "spread_to_base_bps": spread_to_base_bps,
        "pricing_status": "SUCCESS" if sm_bps is not None else "DM_FAILED",
        "warnings": []
    }
