"""Golden-тесты солверов: XIRR, SM (solve_dm_bps), discount margin, z-спред.
Сильнейший инвариант — par-тождество: бумага по номиналу → SM == марже выпуска
(телескопирование потока с DF кривой). Ломается при любом рассогласовании
конвенций начисления/дисконта.
"""
import math
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods
from valuation import (
    build_cashflows_with_spread, solve_dm_bps, solve_discount_margin_bps,
    current_index_pct, FlatForwardCurve, xirr, xnpv, dirty_price_rub,
)


def test_xnpv_zero_rate_is_sum():
    cfs = [(date(2026, 1, 12), -100.0), (date(2027, 1, 12), 50.0), (date(2028, 1, 12), 60.0)]
    assert xnpv(0.0, cfs) == pytest.approx(10.0)


def test_xirr_recovers_known_rate():
    """XIRR восстанавливает ставку: вложил 100, через год получил 115 → 15%."""
    cfs = [(date(2026, 1, 12), -100.0), (date(2027, 1, 12), 115.0)]
    r = xirr(cfs)
    assert r == pytest.approx(0.15, abs=1e-4)


def test_xirr_none_without_sign_change():
    assert xirr([(date(2026, 1, 12), 100.0), (date(2027, 1, 12), 50.0)]) is None


def test_par_identity_keyrate_sm_equals_margin(keyrate_curve, calc_date, flat_index_15):
    """PAR-ТОЖДЕСТВО (KEYRATE): бумага строго по номиналу (clean=100, НКД=0 на
    границе периода) → SM == марже выпуска. Это математическая инверсия bootstrap:
    при dm=марже DF-цепочка pv_cashflows == DF кривой, PV == номинал. Допуск 2bps
    (day-count дрейф +1день в get_maturity_date)."""
    margin = 150
    bond = make_bond(margin_bps=margin, accrued=0.0)
    # периоды выровнены так, что calc_date = начало периода (НКД=0)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, margin,
                                      explicit_periods=periods, index_pct_fn=fn)
    dirty = dirty_price_rub(bond.face_value, 100.0, 0.0)
    sm = solve_dm_bps(bond, keyrate_curve, cfs, calc_date, dirty)
    assert sm == pytest.approx(margin, abs=2), f"SM={sm} != margin={margin}"


def test_par_identity_ruonia_sm_equals_margin(ruonia_curve, calc_date, flat_index_15):
    """PAR-ТОЖДЕСТВО (RUONIA daily-comp): та же инверсия для дневного компаундинга."""
    margin = 200
    bond = make_bond(base="RUONIA", margin_bps=margin, accrued=0.0)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, ruonia_curve, calc_date, margin,
                                      explicit_periods=periods, index_pct_fn=fn)
    dirty = dirty_price_rub(bond.face_value, 100.0, 0.0)
    sm = solve_dm_bps(bond, ruonia_curve, cfs, calc_date, dirty)
    assert sm == pytest.approx(margin, abs=3), f"SM={sm} != margin={margin}"


def test_sm_rises_below_par(keyrate_curve, calc_date, flat_index_15):
    """Цена ниже номинала → SM выше марже (инвестор требует больше спреда)."""
    margin = 150
    bond = make_bond(margin_bps=margin)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, margin,
                                      explicit_periods=periods, index_pct_fn=fn)
    dirty_below = dirty_price_rub(bond.face_value, 98.0, 0.0)
    sm = solve_dm_bps(bond, keyrate_curve, cfs, calc_date, dirty_below)
    assert sm > margin


def test_solve_dm_none_when_unbracketable(keyrate_curve, calc_date, flat_index_15):
    """Недостижимая цель PV → None (не забрекетить), не крэш. PV потока строго
    положителен при любой dm ∈ ±50000bps, поэтому отрицательный target не брекетится."""
    bond = make_bond()
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, 150,
                                      explicit_periods=periods, index_pct_fn=fn)
    assert solve_dm_bps(bond, keyrate_curve, cfs, calc_date, -100.0) is None


def test_discount_margin_par_on_flat_index(calc_date, flat_index_15):
    """DM на плоском индексе: бумага по номиналу → disc_margin ≈ марже (плоская
    money-market конвенция L+DM с обеих сторон почти симметрична)."""
    margin = 150
    bond = make_bond(margin_bps=margin)
    L = 15.0
    flat = FlatForwardCurve(calc_date, L)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    flat_cfs = build_cashflows_with_spread(bond, flat, calc_date, margin,
                                           explicit_periods=periods, index_pct_fn=fn)
    dirty = dirty_price_rub(bond.face_value, 100.0, 0.0)
    dm = solve_discount_margin_bps(flat_cfs, calc_date, dirty, L)
    assert dm is not None
    assert dm == pytest.approx(margin, abs=15)


def test_flat_forward_curve_convention(calc_date):
    """FlatForwardCurve: конвенция 'level', forward == уровень на любом сегменте."""
    flat = FlatForwardCurve(calc_date, 15.0)
    assert flat.rate_convention == "level"
    f = flat.forward(calc_date + timedelta(days=10), calc_date + timedelta(days=100))
    assert f == pytest.approx(0.15)


def test_sanity_guard_nulls_garbage_output(calc_date, flat_index_15, monkeypatch):
    """C6: заведомо бредовый SM (out-of-range) → None + pricing_status SANITY_FLAG,
    а не мусор в таблице. Эмулируем через monkeypatch солвера."""
    import services.valuation as sv
    from conftest import make_bond, quarterly_periods
    _, idx = flat_index_15
    monkeypatch.setattr("services.coupon_calib.index_history", lambda base: idx)
    # солвер вернёт дичь вне [_SANE_BPS]
    monkeypatch.setattr("services.valuation.solve_dm_bps", lambda *a, **k: -99999)
    monkeypatch.setattr("services.valuation.solve_discount_margin_bps", lambda *a, **k: None)

    from forwards import CurveBootstrapper
    from rates import Quote
    from conftest import CALC_DATE
    q = [Quote("SYN", t, 15.0, CALC_DATE) for t in ["3M", "1Y", "3Y", "5Y", "10Y"]]
    curve = CurveBootstrapper.bootstrap_keyrate(q, CALC_DATE)
    bond = make_bond()
    periods = quarterly_periods(calc_date, bond.maturity_date)
    m = sv.calculate_valuation_metrics(bond, 100.0, curve, calc_date, periods=periods)
    assert m["sm_bps"] is None                       # дичь вычищена
    assert m["pricing_status"] == "SANITY_FLAG"
    assert any("sanity:" in w for w in m["warnings"])
