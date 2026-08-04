"""Верификация калькулятора НА ДАТУ ПОСТАВКИ.

Конвенция: цена — котировка дня расчёта (T0), а деньги и НКД — на дату поставки
(T+1 рабочий, с пропуском выходных и праздников MOEX). Дисконтирование XIRR /
simple margin / discount margin уже якорилось на settle; здесь фиксируем, что и
НКД считается на ту же дату, иначе dirty и поток жили бы в разных днях.
"""
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods
from core.valuation import (
    accrue_to_settle, accrued_at, period_at, settle_date, dirty_price_rub,
    build_cashflows_with_spread, solve_simple_margin_bps, xirr_yield_pct,
)
from services.valuation import calculate_valuation_metrics


# ── чистая арифметика начисления ────────────────────────────────────────────

def _periods(start: date, n=8, step=91, value=None):
    out, s = [], start
    for _ in range(n):
        e = s + timedelta(days=step)
        out.append((s.isoformat(), e.isoformat(), value))
        s = e
    return out


def test_settle_skips_weekend():
    """Пятница → поставка в понедельник (3 календарных дня накопления)."""
    friday = date(2026, 1, 16)
    assert friday.weekday() == 4
    assert settle_date(friday) == date(2026, 1, 19)


def test_accrue_same_period_adds_daily_coupon():
    """Тот же купонный период, купон опубликован → НКД растёт на value/длину за день."""
    start = date(2025, 12, 1)
    per = _periods(start, value=25.0)          # 25₽ за 91 день ≈ 0.2747₽/день
    calc = date(2026, 1, 12)                   # понедельник → поставка 13.01
    acc_calc = accrued_at(per, calc)
    acc_settle, note = accrue_to_settle(acc_calc, calc, per)
    daily = 25.0 / 91
    assert acc_settle == pytest.approx(acc_calc + daily, abs=1e-4)
    assert note and "поставки" in note


def test_accrue_over_weekend_adds_three_days():
    """Пятница → понедельник: три дня накопления, не один."""
    start = date(2025, 12, 1)
    per = _periods(start, value=25.0)
    friday = date(2026, 1, 16)
    acc_f = accrued_at(per, friday)
    acc_s, _ = accrue_to_settle(acc_f, friday, per)
    assert acc_s == pytest.approx(acc_f + 3 * 25.0 / 91, abs=1e-4)


def test_accrue_across_coupon_payment_resets_to_new_period():
    """Купон выплачивается в окне (calc, settle] — уходит продавцу. НКД на дату
    поставки считается от начала НОВОГО периода, а не «старый минус купон»
    (прежняя коррекция давала слегка отрицательный НКД)."""
    start = date(2025, 10, 20)
    per = _periods(start, value=25.0)
    end_first = date.fromisoformat(per[0][1])           # конец первого периода
    calc = end_first - timedelta(days=1)
    settle = settle_date(calc)
    assert settle >= end_first, "тест бессмысленен, если купон не попал в окно"

    acc_calc = accrued_at(per, calc)                    # почти целый купон
    acc_settle, note = accrue_to_settle(acc_calc, calc, per)

    assert acc_settle >= 0.0, "НКД на дату поставки не может быть отрицательным"
    assert acc_settle < 2.0, f"должно быть начало нового периода, получено {acc_settle}"
    assert acc_settle == pytest.approx(accrued_at(per, settle), abs=1e-6)
    assert note and "ex-coupon" in note


def test_accrue_unpublished_coupon_scales_by_days():
    """Купон периода ещё не опубликован (value=None) → пропорция по дням от факта."""
    start = date(2025, 12, 1)
    per = _periods(start, value=None)
    calc = date(2026, 1, 12)
    fact = 10.0                                          # биржевой НКД на calc
    acc, _ = accrue_to_settle(fact, calc, per)
    elapsed = (calc - start).days
    grown = (settle_date(calc) - start).days
    assert acc == pytest.approx(fact * grown / elapsed, abs=1e-4)


def test_accrue_noop_without_schedule():
    """Без расписания доначислять нечем — возвращаем факт как есть, без выдумок."""
    acc, note = accrue_to_settle(12.34, date(2026, 1, 12), None)
    assert acc == 12.34 and note is None


# ── сквозная проверка метрик ────────────────────────────────────────────────

def test_metrics_dirty_uses_settlement_accrued(keyrate_curve, calc_date, flat_index_15, monkeypatch):
    """dirty = номинал·цена/100 + НКД НА ДАТУ ПОСТАВКИ, и это же значение
    возвращается в accrued_settle_rub. Расчёт на calc_date остаётся справочным."""
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None: (flat_index_15[0], list(zip(*flat_index_15[1]))))
    bond = make_bond(margin_bps=150)
    periods = _periods(calc_date - timedelta(days=40), value=25.0)
    acc_calc = accrued_at(periods, calc_date)

    m = calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                    accrued_override=acc_calc, periods=periods)

    assert m["settlement_date"] == settle_date(calc_date)
    assert m["accrued_calc_rub"] == pytest.approx(acc_calc, abs=1e-4)
    assert m["accrued_settle_rub"] > m["accrued_calc_rub"], "НКД на поставку должен быть больше"
    assert m["dirty_price_rub"] == pytest.approx(
        dirty_price_rub(bond.face_value, 100.0, m["accrued_settle_rub"]), abs=1e-6)


def test_metrics_par_identity_holds_at_settlement(keyrate_curve, calc_date, flat_index_15, monkeypatch):
    """Инвариант не сломан переносом НКД: бумага ровно по номиналу на границе
    периода (НКД=0 и на calc, и на поставке нулевой прирост не требуется —
    начало периода совпадает с датой поставки) → SM == марже выпуска."""
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None: (flat_index_15[0], list(zip(*flat_index_15[1]))))
    margin = 150
    settle = settle_date(calc_date)
    bond = make_bond(margin_bps=margin, accrued=0.0)
    periods = quarterly_periods(settle, bond.maturity_date)   # период стартует в день поставки

    m = calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                    accrued_override=0.0, periods=periods)
    assert m["sm_bps"] == pytest.approx(margin, abs=3), f"SM={m['sm_bps']} != {margin}"


def test_xirr_anchored_at_settlement(keyrate_curve, calc_date, flat_index_15):
    """Доходность считается от даты поставки: платёж, попавший в окно
    (calc, settle], в расчёт не входит — деньги за бумагу ещё не ушли."""
    bond = make_bond(margin_bps=150)
    fn, _ = flat_index_15
    periods = quarterly_periods(calc_date, bond.maturity_date)
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, 150,
                                      explicit_periods=periods, index_pct_fn=fn)
    settle = settle_date(calc_date)
    dirty = dirty_price_rub(bond.face_value, 100.0, 0.0)
    y = xirr_yield_pct(dirty, cfs, calc_date)
    assert y is not None and 10.0 < y < 25.0, f"доходность вне разумного диапазона: {y}"

    # тот же поток, но с фиктивным платежом за день ДО поставки: он продавца,
    # доходность покупателя от него меняться не должна
    from core.valuation import Cashflow
    ghost = Cashflow(pay_date=settle - timedelta(days=1), amount_rub=500.0, type="COUPON")
    y_ghost = xirr_yield_pct(dirty, cfs + [ghost], calc_date)
    assert y_ghost == pytest.approx(y, abs=1e-9), "поток до даты поставки просочился в расчёт"
