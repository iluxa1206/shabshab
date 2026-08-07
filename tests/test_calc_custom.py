"""Калькулятор кастомной облигации: синтетика графика купонов и НКД."""
from datetime import date

from api.routes.calc import build_custom_schedule, _accrued, _shift_months
from core.valuation import settle_date
from services.fixed_income import fixed_metrics_from_schedule


def test_shift_months_month_end_clamp():
    assert _shift_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert _shift_months(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert _shift_months(date(2026, 1, 15), 12) == date(2025, 1, 15)


def test_schedule_quarterly():
    cd = date(2026, 8, 6)
    s = build_custom_schedule(date(2029, 8, 15), 14.5, 4, 1000.0, cd)
    ends = [c["end"] for c in s["coupons"]]
    # один «прошедший» купон как якорь начала периода + будущие с шагом 3 мес
    assert ends[0] == "2026-05-15"
    assert ends[1] == "2026-08-15"
    assert ends[-1] == "2029-08-15"
    assert all(abs(c["value"] - 1000 * 14.5 / 100 / 4) < 1e-9 for c in s["coupons"])
    assert s["amorts"] == [{"date": "2029-08-15", "value": 1000.0}]


def test_accrued_linear_in_period():
    cd = date(2026, 8, 6)
    s = build_custom_schedule(date(2029, 8, 15), 14.5, 4, 1000.0, cd)
    settle = settle_date(cd)  # 2026-08-07
    a = _accrued(s, settle, cd)
    # период 2026-05-15 → 2026-08-15 (92 дня), прошло 84
    assert abs(a - 36.25 * 84 / 92) < 0.01


def test_metrics_par_bond_ytm_above_coupon():
    """По номиналу эффективная YTM > номинальной ставки (капитализация)."""
    cd = date(2026, 8, 6)
    s = build_custom_schedule(date(2029, 8, 15), 14.5, 4, 1000.0, cd)
    settle = settle_date(cd)
    a = _accrued(s, settle, cd)
    m = fixed_metrics_from_schedule(s, 100.0, a, cd, None)
    assert m["ytm_pct"] is not None and 14.5 < m["ytm_pct"] < 16.0
    assert m["mod_dur"] is not None and 1.5 < m["mod_dur"] < 3.0
    # дисконт к цене поднимает доходность
    m95 = fixed_metrics_from_schedule(s, 95.0, a, cd, None)
    assert m95["ytm_pct"] > m["ytm_pct"]


def test_annual_freq_schedule():
    cd = date(2026, 8, 6)
    s = build_custom_schedule(date(2028, 1, 10), 10.0, 1, 500.0, cd)
    ends = [c["end"] for c in s["coupons"]]
    assert ends == ["2026-01-10", "2027-01-10", "2028-01-10"]
    assert s["coupons"][0]["value"] == 50.0
