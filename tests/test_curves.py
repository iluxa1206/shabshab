"""Golden-тесты кривых (forwards.py): bootstrap, интерполяция DF, экстраполяция,
конвенции форварда. Проверяют математические тождества, а не захардкоженные
числа — устойчивы к смене входных квот, ломаются только при регрессе логики.
"""
import math
from datetime import date, timedelta

import pytest

from forwards import (
    CurveBootstrapper, DiscountCurve, BootstrappedForwardCurve,
    yf_act365, add_months, get_maturity_date,
)


def test_df_monotone_and_bounded(keyrate_curve):
    """DF строго убывает и в (0,1] — инвариант _validate_curve на живом bootstrap."""
    nodes = keyrate_curve.nodes
    assert nodes[0][1] == 1.0
    for i in range(1, len(nodes)):
        assert 0.0 < nodes[i][1] <= nodes[i - 1][1]


def test_flat_par_gives_flat_forward(keyrate_curve, calc_date):
    """Плоские par-квоты 15% → форвард сегментов ≈ 15% на всём диапазоне
    (KEYRATE simple). Допуск широкий: фикс-нога квартальная, микродрейф от
    day-count конвенции +1день ожидаем."""
    start = calc_date + timedelta(days=1)
    for yrs in (0.5, 1, 2, 3, 5, 8):
        a = start + timedelta(days=int(365 * yrs))
        f = keyrate_curve.forward(a, a + timedelta(days=90))
        assert 0.14 < f < 0.16, f"forward@{yrs}y = {f:.4f} вне [14%,16%]"


def test_keyrate_forward_convention_is_simple(keyrate_curve):
    """KEYRATE: 1 + f·days/365 == DF(t1)/DF(t2) точно (простая инверсия bootstrap)."""
    t1 = keyrate_curve.calc_date + timedelta(days=100)
    t2 = t1 + timedelta(days=95)
    f = keyrate_curve.forward(t1, t2)
    days = (t2 - t1).days
    factor = keyrate_curve.df(t1) / keyrate_curve.df(t2)
    assert math.isclose(1.0 + f * days / 365.0, factor, rel_tol=1e-9)


def test_ruonia_forward_convention_is_daily_comp(ruonia_curve):
    """RUONIA: (1+f/365)^days == DF(t1)/DF(t2) точно (daily-comp инверсия)."""
    t1 = ruonia_curve.calc_date + timedelta(days=100)
    t2 = t1 + timedelta(days=95)
    f = ruonia_curve.forward(t1, t2)
    days = (t2 - t1).days
    factor = ruonia_curve.df(t1) / ruonia_curve.df(t2)
    assert math.isclose((1.0 + f / 365.0) ** days, factor, rel_tol=1e-9)


def test_rate_convention_attributes(keyrate_curve, ruonia_curve):
    """C4: конвенция явная на кривой (не по типу), потребитель её проверяет."""
    assert keyrate_curve.rate_convention == "simple"
    assert ruonia_curve.rate_convention == "daily_comp"


def test_log_linear_df_interpolation():
    """DF между узлами — лог-линейная (постоянный непрерывный форвард на сегменте)."""
    cd = date(2026, 1, 12)
    n1, n2 = cd + timedelta(days=365), cd + timedelta(days=730)
    curve = DiscountCurve(cd, [(n1, 0.87), (n2, 0.75)])
    mid = cd + timedelta(days=547)  # ~середина второго сегмента
    w = yf_act365(n1, mid) / yf_act365(n1, n2)
    expected = math.exp(math.log(0.87) + w * (math.log(0.75) - math.log(0.87)))
    assert math.isclose(curve.df(mid), expected, rel_tol=1e-12)


def test_flat_forward_extrapolation_constant(keyrate_curve):
    """За последним узлом форвард ОСТАЁТСЯ константой (flat-forward по наклону
    ln DF последнего сегмента) — не затухает гиперболически."""
    last = keyrate_curve.nodes[-1][0]
    fwds = []
    for y in (1, 3, 6, 10):
        a = last + timedelta(days=365 * y)
        fwds.append(keyrate_curve.forward(a, a + timedelta(days=365)))
    for f in fwds[1:]:
        assert math.isclose(f, fwds[0], rel_tol=1e-6), f"экстраполяция дрейфует: {fwds}"


def test_equivalent_rate_zero_guards():
    """_equivalent_rate защита от вырожденных входов."""
    c = BootstrappedForwardCurve(date(2026, 1, 12),
                                 [(date(2027, 1, 12), 0.87)], "KEYRATE")
    assert c._equivalent_rate(1.0, 0) == 0.0
    assert c._equivalent_rate(0.0, 90) == 0.0


def test_forward_requires_ordered_dates(keyrate_curve):
    d = keyrate_curve.calc_date + timedelta(days=100)
    with pytest.raises(ValueError):
        keyrate_curve.forward(d, d)


def test_get_maturity_date_tenors():
    s = date(2026, 1, 12)
    assert get_maturity_date(s, "ON") == s + timedelta(days=1)
    assert get_maturity_date(s, "3M") == add_months(s, 3) + timedelta(days=1)
    assert get_maturity_date(s, "1Y") == add_months(s, 12) + timedelta(days=1)


def test_add_months_end_of_month_clamp():
    """31 янв + 1 мес → 28/29 фев (клэмп на последний день)."""
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # високосный
