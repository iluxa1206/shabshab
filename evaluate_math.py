from datetime import date
from forwards import DiscountCurve, yf_act365
from valuation import (
    BondRefData,
    build_cashflows_to_maturity,
    pv_cashflows_with_dm,
    solve_dm_bps,
    implied_yield_pct,
    dirty_price_rub
)
import math

class FlatCurveMock(DiscountCurve):
    def forward(self, t1: date, t2: date) -> float:
        return 0.10

calc_d = date(2024, 7, 1)
mock_curve = FlatCurveMock(calc_d)

bond1 = BondRefData(
    isin="RUONIA_PAR",
    base="RUONIA",
    spread_issue_bps=150,
    face_value=1000.0,
    accrued_rub=0.0,  
    maturity_date=date(2025, 7, 1),
    first_coupon_date=date(2024, 10, 1),
    coupons_per_year=4,
    issue_date=date(2024, 7, 1)
)

print("--- TEST 1: RUONIA Par Case ---")
cfs1 = build_cashflows_to_maturity(bond1, mock_curve, calc_d)
dirty1 = dirty_price_rub(1000.0, 100.0, 0.0)
dm1 = solve_dm_bps(bond1, mock_curve, cfs1, calc_d, dirty1)
y1 = implied_yield_pct(bond1, mock_curve, cfs1, calc_d, dm1)
print(f"Spread Issue: {bond1.spread_issue_bps} bps | Solved DM: {dm1} bps")
print(f"Implied Yield out: {y1:.5f} %")

bond2 = BondRefData(
    isin="KEYRATE_PAR",
    base="KEYRATE",
    spread_issue_bps=200,
    face_value=1000.0,
    accrued_rub=0.0,
    maturity_date=date(2025, 7, 1),
    first_coupon_date=date(2024, 10, 1),
    coupons_per_year=4,
    issue_date=date(2024, 7, 1)
)

print("\n--- TEST 2: KEYRATE Par Case ---")
cfs2 = build_cashflows_to_maturity(bond2, mock_curve, calc_d)
dirty2 = dirty_price_rub(1000.0, 100.0, 0.0)
dm2 = solve_dm_bps(bond2, mock_curve, cfs2, calc_d, dirty2)
y2 = implied_yield_pct(bond2, mock_curve, cfs2, calc_d, dm2)
print(f"Spread Issue: {bond2.spread_issue_bps} bps | Solved DM: {dm2} bps")
print(f"Implied Yield out: {y2:.5f} %")

bond3 = BondRefData(
    isin="KEYRATE_DISCOUNT",
    base="KEYRATE",
    spread_issue_bps=200,
    face_value=1000.0,
    accrued_rub=0.0,
    maturity_date=date(2025, 7, 1),
    first_coupon_date=date(2024, 10, 1),
    coupons_per_year=4,
    issue_date=date(2024, 7, 1)
)

print("\n--- TEST 3: KEYRATE Discount Case (Price=95) ---")
cfs3 = build_cashflows_to_maturity(bond3, mock_curve, calc_d)
dirty3 = dirty_price_rub(1000.0, 95.0, 0.0)
dm3 = solve_dm_bps(bond3, mock_curve, cfs3, calc_d, dirty3)
y3 = implied_yield_pct(bond3, mock_curve, cfs3, calc_d, dm3)
print(f"Spread Issue: {bond3.spread_issue_bps} bps | Solved DM: {dm3} bps")
print(f"Implied Yield out: {y3:.5f} %")

print("\n--- TEST 4: RUONIA Premium Case (Price=102) ---")
cfs4 = build_cashflows_to_maturity(bond1, mock_curve, calc_d)
dirty4 = dirty_price_rub(1000.0, 102.0, 0.0)
dm4 = solve_dm_bps(bond1, mock_curve, cfs4, calc_d, dirty4)
y4 = implied_yield_pct(bond1, mock_curve, cfs4, calc_d, dm4)
print(f"Spread Issue: {bond1.spread_issue_bps} bps | Solved DM: {dm4} bps")
print(f"Implied Yield out: {y4:.5f} %")
