import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from forwards import DiscountCurve, add_months, yf_act365

# Configure basic logging for the valuation module
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# 1) Входные структуры
# -------------------------------------------------------------------------

@dataclass
class BondRefData:
    """
    Reference data for a floater bond.
    base: "RUONIA" or "KEYRATE"
    """
    isin: str
    base: str  
    spread_issue_bps: int
    face_value: float
    accrued_rub: float
    maturity_date: date
    first_coupon_date: date
    coupons_per_year: int
    issue_date: Optional[date] = None


@dataclass
class MarketPrice:
    """
    Market quotes for a bond.
    clean_price_pct: Price in % of face value (e.g., 101.25)
    """
    clean_price_pct: float


@dataclass
class Cashflow:
    """
    Simulated cashflow modeled on forward rates.
    type: "COUPON" or "REDEMPTION"
    """
    pay_date: date
    amount_rub: float
    type: str  


# -------------------------------------------------------------------------
# 2) Основные функции расчёта
# -------------------------------------------------------------------------

def dirty_price_rub(face_value: float, clean_price_pct: float, accrued_rub: float) -> float:
    """
    Переход от 'чистой' цены к 'грязной' (в рублях).
    """
    return face_value * (clean_price_pct / 100.0) + accrued_rub


def generate_coupon_dates(first_coupon_date: date, maturity_date: date, coupons_per_year: int) -> List[date]:
    """
    Генерирует сетку дат купонов строго по шагу (без бизнес-сдвигов).
    Обрезает даты строго по maturity_date (включительно).
    Если maturity_date не ложится ровно на сетку, она НЕ добавляется как фиктивный купон.
    """
    step_months = 12 // coupons_per_year
    dates = []
    
    current = first_coupon_date
    while current <= maturity_date:
        dates.append(current)
        current = add_months(current, step_months)
        
    return dates


def build_cashflows_to_maturity(bond: BondRefData, curve: DiscountCurve, calc_date: date) -> List[Cashflow]:
    """
    Строит ожидаемые cashflows: купоны (forward + spread) и номинал.
    Участвуют только CF с pay_date > calc_date.
    """
    # 1. coupon_dates
    coupon_dates = generate_coupon_dates(bond.first_coupon_date, bond.maturity_date, bond.coupons_per_year)
    
    # Validation
    if any(d > bond.maturity_date for d in coupon_dates):
        raise ValueError(f"Found coupon pay_date after maturity_date for ISIN: {bond.isin}")

    # 2. rebuild periods
    step_months = 12 // bond.coupons_per_year
    start_1 = add_months(bond.first_coupon_date, -step_months)
    if bond.issue_date and start_1 < bond.issue_date:
        start_1 = bond.issue_date
        
    periods = []
    prev_end = start_1
    for d in coupon_dates:
        periods.append((prev_end, d))
        prev_end = d
        
    cfs = []
    
    # 3. generate coupons
    for start, end in periods:
        if end <= calc_date:
            continue
            
        days = (end - start).days
        alpha = days / 365.0
        
        # F = curve.forward(start, end)
        f_rate = curve.forward(start, end)
        s_rate = bond.spread_issue_bps / 10000.0
        r_rate = f_rate + s_rate
        
        # Compounding factors
        if bond.base == "RUONIA":
            factor = (1.0 + r_rate / 365.0)**days - 1.0
        elif bond.base == "KEYRATE":
            factor = (1.0 + r_rate / 4.0)**(4.0 * alpha) - 1.0
        else:
            raise ValueError(f"Unknown base rate type: {bond.base} for ISIN: {bond.isin}")
            
        coupon_amt = bond.face_value * factor
        cfs.append(Cashflow(pay_date=end, amount_rub=coupon_amt, type="COUPON"))
        
    # 4. redemption
    if bond.maturity_date > calc_date:
        cfs.append(Cashflow(pay_date=bond.maturity_date, amount_rub=bond.face_value, type="REDEMPTION"))
        
    # 5. Sort by pay_date
    cfs.sort(key=lambda cf: cf.pay_date)
    return cfs


def pv_cashflows_with_dm(
    bond: BondRefData, 
    curve: DiscountCurve, 
    cashflows: List[Cashflow], 
    calc_date: date, 
    dm_bps: int
) -> float:
    """
    Считает PV всех cashflows рекурсивным дисконтированием: F_i + DM на сетке платежей.
    """
    dm = dm_bps / 10000.0
    
    # Уникальные даты платежей > calc_date
    pay_dates_set = {cf.pay_date for cf in cashflows if cf.pay_date > calc_date}
    grid = sorted(list(pay_dates_set))
    
    df_dm = 1.0
    prev = calc_date
    pv = 0.0
    
    for d in grid:
        days = (d - prev).days
        alpha = days / 365.0
        
        f_rate = curve.forward(prev, d)
        r_rate = f_rate + dm
        
        if bond.base == "RUONIA":
            base_factor = 1.0 + r_rate / 365.0
            if base_factor <= 0.0:
                raise ValueError("Rate too negative")
            df_dm /= base_factor**days
        elif bond.base == "KEYRATE":
            base_factor = 1.0 + r_rate / 4.0
            if base_factor <= 0.0:
                raise ValueError("Rate too negative")
            df_dm /= base_factor**(4.0 * alpha)
        else:
            raise ValueError(f"Unknown base: {bond.base}")
            
        # Сумма всех CF в эту дату (может быть и купон, и погашение)
        amount_on_d = sum(cf.amount_rub for cf in cashflows if cf.pay_date == d)
        pv += amount_on_d * df_dm
        
        prev = d
        
    return pv


def solve_dm_bps(
    bond: BondRefData, 
    curve: DiscountCurve, 
    cashflows: List[Cashflow], 
    calc_date: date, 
    dirty_target_rub: float, 
    low_bps: int = -50000, 
    high_bps: int = 50000, 
    tol_bps: int = 1
) -> Optional[int]:
    """
    Бисекция для поиска DM (Discount Margin) в bps, которая приравнивает PV к dirty_target_rub.
    """
    def f(dm_bps_val: int) -> float:
        try:
            return pv_cashflows_with_dm(bond, curve, cashflows, calc_date, dm_bps_val) - dirty_target_rub
        except ValueError:
            return float('inf') if dm_bps_val < 0 else -float('inf')
        
    f_low = f(low_bps)
    f_high = f(high_bps)
    
    if f_low * f_high > 0:
        logger.warning(
            f"Невозможно забрекетировать DM для {bond.isin}: f({low_bps}bps)={f_low:,.2f}, f({high_bps}bps)={f_high:,.2f}"
        )
        return None
        
    low = low_bps
    high = high_bps
    
    while (high - low) > tol_bps:
        mid = (low + high) // 2
        f_mid = f(mid)
        
        if abs(f_mid) < 1e-8:
            return mid
            
        if f_low * f_mid < 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
            
    return (low + high) // 2


def implied_yield_pct(
    bond: BondRefData, 
    curve: DiscountCurve, 
    cashflows: List[Cashflow], 
    calc_date: date, 
    dm_bps: int
) -> float:
    """
    Вычисляет эквивалентную доходность к погашению из DF_DM на дату maturity (implied yield).
    Возвращает значение в процентах годовых (e.g. 15.25 -> 15.25%).
    """
    dm = dm_bps / 10000.0
    pay_dates_set = {cf.pay_date for cf in cashflows if cf.pay_date > calc_date}
    grid = sorted(list(pay_dates_set))
    
    df_dm = 1.0
    prev = calc_date
    df_dm_at_maturity = 1.0
    
    found_maturity = False
    
    for d in grid:
        days = (d - prev).days
        alpha = days / 365.0
        f_rate = curve.forward(prev, d)
        r_rate = f_rate + dm
        
        if bond.base == "RUONIA":
            df_dm /= (1.0 + r_rate / 365.0)**days
        elif bond.base == "KEYRATE":
            df_dm /= (1.0 + r_rate / 4.0)**(4.0 * alpha)
            
        if d == bond.maturity_date:
            df_dm_at_maturity = df_dm
            found_maturity = True
            break
            
        prev = d
        
    if not found_maturity or df_dm_at_maturity <= 0.0 or df_dm_at_maturity >= 1.0:
        return 0.0
        
    tau_days = (bond.maturity_date - calc_date).days
    if tau_days <= 0:
        return 0.0
        
    tau_years = tau_days / 365.0
    
    # Эффективная годовая доходность (Effective Annual Yield), 
    # которая учитывает капитализацию / реинвестирование промежуточных выплат
    y_effective = math.pow(df_dm_at_maturity, -1.0 / tau_years) - 1.0
    return y_effective * 100.0


# -------------------------------------------------------------------------
# 3) Выходной API Pipeline
# -------------------------------------------------------------------------

def calculate_floater_metrics(
    bond: BondRefData, 
    price: float, # чистая цена (pct) %
    curve: DiscountCurve, 
    calc_date: date
) -> dict:
    """
    Полный пайплайн для расчёта аналитики для одной бумаги (для Last/Bid/Ask или уровня стакана).
    """
    dirty_rub = dirty_price_rub(bond.face_value, price, bond.accrued_rub)
    
    cfs = build_cashflows_to_maturity(bond, curve, calc_date)
    
    dm_bps_val = solve_dm_bps(bond, curve, cfs, calc_date, dirty_rub)
    
    impl_yield = 0.0
    if dm_bps_val is not None:
        impl_yield = implied_yield_pct(bond, curve, cfs, calc_date, dm_bps_val)
        
    return {
        "clean_price_pct": price,
        "accrued_rub": bond.accrued_rub,
        "dirty_rub": dirty_rub,
        "dm_bps": dm_bps_val,
        "implied_yield_pct": impl_yield,
        "spread_issue_bps": bond.spread_issue_bps,
        "yield_base_type": bond.base 
    }


# ========================================================================
# ПРИМЕР ИСПОЛЬЗОВАНИЯ (Smoke tests / Local checks)
# ========================================================================
if __name__ == "__main__":
    from datetime import timedelta

    calc_d = date(2024, 7, 1)
    
    # Мок-кривая 10% Flat Forward
    class FlatCurveMock(DiscountCurve):
        def forward(self, t1: date, t2: date) -> float:
            return 0.10
            
    mock_curve = FlatCurveMock(calc_date=calc_d, nodes=[(calc_d, 1.0)])
    
    # 1. Тест дат генерации:
    # 1-й купон - 2024-09-01, Quarterly. maturity = 2025-06-01
    c_dates = generate_quarterly_schedule = generate_coupon_dates(
        first_coupon_date=date(2024, 9, 1),
        maturity_date=date(2025, 6, 1), 
        coupons_per_year=4
    )
    print(f"Coupon Dates: {c_dates}")
    assert c_dates == [date(2024, 9, 1), date(2024, 12, 1), date(2025, 3, 1), date(2025, 6, 1)]

    # 2. Мок RUONIA бумаги (spread = 150 bps)
    mock_bond = BondRefData(
        isin="RU000A10X0M4",
        base="RUONIA",
        spread_issue_bps=150,
        face_value=1000.0,
        accrued_rub=8.5,
        maturity_date=date(2025, 6, 1),
        first_coupon_date=date(2024, 9, 1),
        coupons_per_year=4,
        issue_date=date(2024, 6, 1)
    )

    # Cashflows Builder Test
    cfs = build_cashflows_to_maturity(mock_bond, mock_curve, calc_d)
    print(f"\nCashflows built: {len(cfs)}")
    for cf in cfs:
        print(f" - {cf.pay_date} | {cf.type} | {cf.amount_rub:,.2f} RUB")
        
    # Dirty Price
    # Если мы торгуемся по номиналу (clean=100), значит DM (спред к кривой) должен быть равен spread_issue (150 bps).
    dirty_p = dirty_price_rub(1000.0, 100.0, mock_bond.accrued_rub)
    
    # Solver test
    dm_calculated = solve_dm_bps(mock_bond, mock_curve, cfs, calc_d, dirty_p)
    print(f"\nSolved DM for Par Price: {dm_calculated} bps (Expected ~150 bps)")
    
    if dm_calculated is not None:
        y_pct = implied_yield_pct(mock_bond, mock_curve, cfs, calc_d, dm_calculated)
        print(f"Implied Yield out: {y_pct:.4f} %")
