"""Golden-тесты z-спреда (services/zspread.py) и инварианта консолидации C2:
project_cfs после слияния — тонкая обёртка над build_cashflows_to_maturity,
поэтому потоки обоих пайплайнов обязаны совпадать на одном входе.
"""
import math
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods, CALC_DATE, _flat_quotes
from core.valuation import build_cashflows_with_spread
from services.zspread import (
    ExpCurve, GCurve, project_cfs, solve_z_bps, solve_z_discrete,
    solve_flat_y, current_period_len, compute_z_bps,
)


@pytest.fixture
def exp_keyrate(calc_date):
    return ExpCurve(calc_date, _flat_quotes(15.0, calc_date), base="KEYRATE")


@pytest.fixture
def gcurve():
    """Плоская КБД ОФЗ 12% на всех сроках."""
    return GCurve([(0.25, 12.0), (1, 12.0), (3, 12.0), (5, 12.0), (10, 12.0)])


def test_gcurve_flat_interpolation(gcurve):
    assert gcurve.r(0.1) == pytest.approx(0.12)   # клэмп короткого конца
    assert gcurve.r(2.0) == pytest.approx(0.12)   # интерполяция
    assert gcurve.r(20.0) == pytest.approx(0.12)  # клэмп длинного конца


def test_gcurve_needs_two_points():
    assert not GCurve([(1, 12.0)]).ok()
    assert GCurve([(1, 12.0), (2, 13.0)]).ok()


def test_project_cfs_matches_build_cashflows(exp_keyrate, calc_date, flat_index_15):
    """ИНВАРИАНТ C2: суммы потоков project_cfs == build_cashflows_to_maturity на
    идентичном входе (после слияния это один алгоритм). Сверяем по датам платежей.

    ОДНА кривая в оба пайплайна: с 8fda88b ExpCurve внутри — SheetForwardCurve
    (методика вкладки КРИВЫЕ), а bootstrap-фикстура даёт ДРУГИЕ форварды на
    flat-квотах (лист: 1Y par → 14.24% квартального номинала, бутстрап → 15.0) —
    сравнение через разные кривые проверяло бы методики, а не алгоритм."""
    bond = make_bond(base="KEYRATE", margin_bps=150)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    coupons = [{"start": s, "end": e, "value": v} for s, e, v in periods]
    fn, _ = flat_index_15
    sheet_curve = exp_keyrate._curve   # та же кривая, что возьмёт project_cfs

    # build_cashflows (valuation)
    val_cfs = build_cashflows_with_spread(bond, sheet_curve, calc_date, 150,
                                          explicit_periods=periods, index_pct_fn=fn)
    val_by_date = {}
    for cf in val_cfs:
        val_by_date[cf.pay_date] = val_by_date.get(cf.pay_date, 0.0) + cf.amount_rub

    # project_cfs (zspread) — та же кривая ожиданий поверх того же bootstrap
    z_cfs = project_cfs(bond, exp_keyrate, calc_date, coupons, index_pct_fn=fn)
    z_by_date = {}
    for d, a in z_cfs:
        z_by_date[d] = z_by_date.get(d, 0.0) + a

    assert set(val_by_date) == set(z_by_date), "наборы дат платежей расходятся"
    for d in val_by_date:
        assert val_by_date[d] == pytest.approx(z_by_date[d], rel=1e-6), \
            f"поток на {d}: valuation={val_by_date[d]:.4f} vs zspread={z_by_date[d]:.4f}"


def test_solve_flat_y_recovers_rate():
    """Плоская доходность: поток одного платежа 115 через год против 100 → y≈ln(1.15)."""
    cd = date(2026, 1, 12)
    cfs = [(date(2027, 1, 12), 115.0)]
    y = solve_flat_y(cfs, cd, 100.0)
    assert y == pytest.approx(math.log(1.15), abs=1e-3)


def test_solve_z_discrete_par_gives_zero_spread(gcurve):
    """Поток, дисконтированный ровно по КБД (z=0), стоит dirty → солвер вернёт ~0."""
    cd = date(2026, 1, 12)
    # один платёж 100 через 2 года, дисконт по 12% дискретно
    cfs = [(cd + timedelta(days=730), 100.0)]
    tau = 730 / 365.0
    dirty = 100.0 / (1.0 + 0.12) ** tau
    z = solve_z_discrete(gcurve, cfs, cd, dirty)
    assert z == pytest.approx(0, abs=5)


def test_current_period_len():
    cd = date(2026, 1, 12)
    coupons = [{"start": "2025-12-01", "end": "2026-03-01", "value": 20.0},
               {"start": "2026-03-01", "end": "2026-06-01", "value": None}]
    plen = current_period_len(coupons, cd)
    assert plen == pytest.approx((date(2026, 3, 1) - date(2025, 12, 1)).days / 365.0)


def test_compute_z_perp_guard_returns_none(exp_keyrate, gcurve, calc_date):
    """C3: перп (maturity=None) без оферты → z=None, не мусор (нет принципала)."""
    bond = make_bond(base="KEYRATE", maturity=None)
    coupons = [{"start": "2025-10-12", "end": "2026-01-12", "value": 20.0},
               {"start": "2026-01-12", "end": "2026-04-12", "value": None}]
    z = compute_z_bps(bond, exp_keyrate, gcurve, calc_date, 100.0, 0.0, coupons)
    assert z is None


def test_compute_z_finite_for_normal_bond(exp_keyrate, gcurve, calc_date, flat_index_15, monkeypatch):
    """Обычный флоатер по номиналу → z конечен и в разумном коридоре.
    compute_z_bps сам фетчит index_history (I/O-граница) — мокаем на плоские 15%,
    чтобы тест был детерминирован и без сети."""
    _, idx = flat_index_15
    monkeypatch.setattr("services.coupon_calib.index_history", lambda base: idx)
    bond = make_bond(base="KEYRATE", margin_bps=150)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    coupons = [{"start": s, "end": e, "value": v} for s, e, v in periods]
    z = compute_z_bps(bond, exp_keyrate, gcurve, calc_date, 100.0, 0.0, coupons)
    assert z is not None
    assert -1000 < z < 2000, f"z={z} вне разумного коридора"
