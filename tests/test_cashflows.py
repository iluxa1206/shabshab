"""Golden-тесты построения потоков (valuation.build_cashflows_to_maturity).
Ключевые инварианты: сумма принципала, T+1 ex-coupon, обрезка к оферте, guard'ы.
"""
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods, CALC_DATE
from core.valuation import (
    build_cashflows_to_maturity, settle_date, face_for_pricing,
    extend_periods_to_maturity, _is_settlement_day_off,
)


def test_principal_sums_to_pricing_face_bullet(keyrate_curve, calc_date, flat_index_15):
    """Bullet: Σ REDEMPTION == pricing_face (номинал целиком на maturity)."""
    bond = make_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, index_pct_fn=fn)
    principal = sum(cf.amount_rub for cf in cfs if cf.type == "REDEMPTION")
    assert principal == pytest.approx(face_for_pricing(bond.face_value, None, calc_date))
    assert principal == pytest.approx(1000.0)


def test_principal_sums_to_pricing_face_amortizing(keyrate_curve, calc_date, flat_index_15):
    """Амортизируемая: Σ всех REDEMPTION (транши + остаток) == pricing_face ровно."""
    bond = make_bond()
    amorts = [
        {"date": date(2028, 1, 12).isoformat(), "value": 300.0},
        {"date": date(2029, 1, 12).isoformat(), "value": 300.0},
        {"date": bond.maturity_date.isoformat(), "value": 400.0},
    ]
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, amorts=amorts, index_pct_fn=fn)
    principal = sum(cf.amount_rub for cf in cfs if cf.type == "REDEMPTION")
    assert principal == pytest.approx(1000.0)


def test_amortizing_coupons_shrink(keyrate_curve, calc_date, flat_index_15):
    """Купоны амортизируемой падают после каждого транша (от остаточного номинала)."""
    bond = make_bond()
    amorts = [{"date": date(2028, 1, 12).isoformat(), "value": 500.0},
              {"date": bond.maturity_date.isoformat(), "value": 500.0}]
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, amorts=amorts, index_pct_fn=fn)
    coupons = [cf for cf in cfs if cf.type == "COUPON"]
    # период может straddle-ить дату транша (сетка 91д не совпадает с датой амортизации),
    # поэтому сравниваем ЯВНО до и ЯВНО после (буфер ±1 период вокруг 2028-01-12)
    before = [c.amount_rub for c in coupons if c.pay_date < date(2027, 10, 1)]
    after = [c.amount_rub for c in coupons if c.pay_date > date(2028, 4, 15)]
    assert min(before) > max(after), "купон после амортизации должен быть меньше"
    assert max(after) == pytest.approx(min(before) / 2, rel=0.05), "остаток 500 из 1000 → купон вдвое"


def test_t_plus_1_ex_coupon_dropped(keyrate_curve, flat_index_15):
    """Купон с pay_date <= settle покупателю не достаётся (T+1 ex-coupon)."""
    bond = make_bond()
    # calc_date за день до купонной даты — этот купон в окне ex
    cpn_date = date(2026, 4, 13)
    calc = cpn_date - timedelta(days=1)
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc,
                                      explicit_periods=periods, index_pct_fn=fn)
    settle = settle_date(calc)
    assert all(cf.pay_date > settle for cf in cfs), "платёж <= settle не должен попасть в поток"


def test_offer_cut_truncates_flow(keyrate_curve, calc_date, flat_index_15):
    """to_offer=True режет поток к оферте: последний платёж — на дату оферты."""
    bond = make_bond()
    offer_date = date(2028, 7, 12)
    offers = [{"date": offer_date.isoformat(), "type": "offer", "price": 100.0}]
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, offers=offers,
                                      to_offer=True, index_pct_fn=fn)
    assert max(cf.pay_date for cf in cfs) == offer_date
    principal = sum(cf.amount_rub for cf in cfs if cf.type == "REDEMPTION")
    assert principal == pytest.approx(1000.0), "выкуп остатка на оферте по 100%"


def test_all_flows_after_calc_date(keyrate_curve, calc_date, flat_index_15):
    bond = make_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, index_pct_fn=fn)
    assert all(cf.pay_date > calc_date for cf in cfs)
    assert cfs == sorted(cfs, key=lambda c: c.pay_date)


def test_settle_skips_weekends_and_holidays():
    """settle_date перепрыгивает выходные И праздники MOEX."""
    # 12 июня (День России) — праздник; 2026-06-11 (чт) → settle пропустит 12-е
    assert _is_settlement_day_off(date(2026, 6, 12))
    s = settle_date(date(2026, 6, 11))
    assert s > date(2026, 6, 12)
    assert not _is_settlement_day_off(s)


def test_settle_skips_new_years_eve():
    """31 декабря расчётов нет (нерабочий с 2023): последний день года — 30-е,
    поставка с него уезжает на первый рабочий день января."""
    assert _is_settlement_day_off(date(2026, 12, 31))
    s = settle_date(date(2026, 12, 30))
    assert s.year == 2027 and s.month == 1 and s.day >= 9
    assert not _is_settlement_day_off(s)


def test_extend_periods_fills_gap_to_maturity():
    """Достройка хвоста: обрыв bondization на 100-м купоне → пробел заполняется."""
    issue = date(2024, 1, 12)
    # только 3 периода, а maturity через годы
    periods = [(issue.isoformat(), (issue + timedelta(days=91)).isoformat(), 20.0),
               ((issue + timedelta(days=91)).isoformat(), (issue + timedelta(days=182)).isoformat(), 20.0)]
    maturity = date(2030, 1, 12)
    ext = extend_periods_to_maturity(periods, maturity)
    assert len(ext) > len(periods)
    assert ext[-1][1] == maturity  # последний период заканчивается на погашении


def test_extend_idempotent_when_no_gap():
    """Пробела нет → no-op (идемпотентность)."""
    issue = date(2024, 1, 12)
    mat = issue + timedelta(days=182)
    periods = [(issue.isoformat(), (issue + timedelta(days=91)).isoformat(), 20.0),
               ((issue + timedelta(days=91)).isoformat(), mat.isoformat(), 20.0)]
    ext = extend_periods_to_maturity(periods, mat)
    assert len(ext) == len(periods)


def test_fixed_value_coupon_taken_as_fact(keyrate_curve, calc_date, flat_index_15):
    """Зафиксированный value купона берётся фактом MOEX, не перепрогнозируется."""
    bond = make_bond()
    future = date(2027, 4, 12)
    periods = [(date(2027, 1, 12).isoformat(), future.isoformat(), 42.42)]
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, index_pct_fn=fn)
    cpn = [cf for cf in cfs if cf.type == "COUPON" and cf.pay_date == future]
    assert cpn and cpn[0].amount_rub == pytest.approx(42.42)


def test_convention_mismatch_raises(ruonia_curve, calc_date, flat_index_15):
    """C4: KEYRATE-бумага на RUONIA-кривой (daily_comp) → явная ошибка конвенции."""
    bond = make_bond(base="KEYRATE")
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    with pytest.raises(ValueError, match="rate_convention"):
        build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                    explicit_periods=periods, index_pct_fn=fn)


def test_margin_schedule_per_period_delta(keyrate_curve, calc_date, flat_index_15, monkeypatch):
    """Лесенка маржи: будущий купон получает спред своей ступени (дельтой к
    скаляру margin_bps), купоны вне диапазонов — скаляр spread_issue_bps."""
    import services.ref_data as rd
    bond = make_bond(margin_bps=150)
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    # купоны 1-12 → 150 (совпадает со скаляром), 13+ → 350
    monkeypatch.setattr(rd, "coupon_formula", lambda isin, *a, **k: {
        "base": "KEYRATE", "margin_bps": 150, "cap_pct": None, "floor_pct": None,
        "capped": False, "coupon_mode": None, "fixing_lag": None,
        "fixing_lag_unit": None,
        "margin_schedule": [{"from": 1, "to": 12, "bps": 150},
                            {"from": 13, "to": 999, "bps": 350}],
    })
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, index_pct_fn=fn)
    coupons = [cf for cf in cfs if cf.type == "COUPON"]
    sp = {cf.spread_bps for cf in coupons}
    assert 150 in sp and 350 in sp, f"обе ступени должны присутствовать: {sp}"
    # ступени идут по времени: сначала 150, потом 350, без чередования
    seq = [cf.spread_bps for cf in sorted(coupons, key=lambda c: c.pay_date)]
    assert seq == sorted(seq)
    # ставка купона ступени 350 выше ставки ступени 150 ровно на 2пп
    r150 = max(c.coupon_rate_pct for c in coupons if c.spread_bps == 150)
    r350 = max(c.coupon_rate_pct for c in coupons if c.spread_bps == 350)
    assert abs((r350 - r150) - 2.0) < 0.05
