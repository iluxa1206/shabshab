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
    """basis='calc' (НКД на дату торгов — history-ACCINT as-of движка): dirty =
    номинал·цена/100 + НКД НА ДАТУ ПОСТАВКИ, доначисленный из переданного."""
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None: (flat_index_15[0], list(zip(*flat_index_15[1]))))
    bond = make_bond(margin_bps=150)
    periods = _periods(calc_date - timedelta(days=40), value=25.0)
    acc_calc = accrued_at(periods, calc_date)

    m = calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                    accrued_override=acc_calc, periods=periods,
                                    accrued_basis="calc")

    assert m["settlement_date"] == settle_date(calc_date)
    assert m["accrued_calc_rub"] == pytest.approx(acc_calc, abs=1e-4)
    assert m["accrued_settle_rub"] > m["accrued_calc_rub"], "НКД на поставку должен быть больше"
    assert m["dirty_price_rub"] == pytest.approx(
        dirty_price_rub(bond.face_value, 100.0, m["accrued_settle_rub"]), abs=1e-6)


def test_metrics_settle_basis_is_not_accrued_twice(keyrate_curve, calc_date, flat_index_15,
                                                   monkeypatch):
    """basis='settle' (дефолт; биржевой ACCRUEDINT блока securities уже НА T+1):
    НКД кладётся в dirty КАК ЕСТЬ. Раньше он доначислялся ещё раз — на пятницу и
    праздники это 3-4 лишних дня, dirty завышался, YTM/Y-IDX/SM/DM занижались."""
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None: (flat_index_15[0], list(zip(*flat_index_15[1]))))
    bond = make_bond(margin_bps=150)
    periods = _periods(calc_date - timedelta(days=40), value=25.0)
    acc_settle = accrued_at(periods, settle_date(calc_date))   # так отдаёт биржа

    m = calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                    accrued_override=acc_settle, periods=periods)

    assert m["accrued_settle_rub"] == pytest.approx(acc_settle, abs=1e-4), "НКД доначислен повторно"
    assert m["dirty_price_rub"] == pytest.approx(
        dirty_price_rub(bond.face_value, 100.0, acc_settle), abs=1e-6)
    # справочный НКД на дату расчёта считается из графика и меньше поставочного
    assert m["accrued_calc_rub"] == pytest.approx(accrued_at(periods, calc_date), abs=1e-4)
    assert m["accrued_calc_rub"] < m["accrued_settle_rub"]
    # и ни одного варнинга про доначисление
    assert not any("доначислен" in w for w in m["warnings"])


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


# ── дни без сделок не прайсим ───────────────────────────────────────────────

def test_keep_trade_days_drops_calendar_only_rows():
    """История спредов чистится по свечам: honest-бэкфилл подставляет
    LEGALCLOSEPRICE в дни без сделок, снапшот пишется по календарю — такие даты
    рисовали точку спреда там, где на графике цены дня нет."""
    from services.spread_history import keep_trade_days
    rows = [{"date": "2026-08-03", "y_idx": 180},
            {"date": "2026-08-04", "y_idx": 181},   # сделок не было
            {"date": "2026-08-05", "y_idx": 179}]
    kept, dropped = keep_trade_days(rows, {"2026-08-03", "2026-08-05"})
    assert [r["date"] for r in kept] == ["2026-08-03", "2026-08-05"]
    assert dropped == 1


def test_keep_trade_days_empty_candles_drops_everything():
    """Свечей нет вовсе → истории тоже нет: молча показывать календарную серию,
    не подтверждённую сделками, нельзя (эндпоинт отдаёт warning раньше)."""
    from services.spread_history import keep_trade_days
    kept, dropped = keep_trade_days([{"date": "2026-08-03"}], set())
    assert kept == [] and dropped == 1


def test_irr_matches_analytic_effective_rate(keyrate_curve, calc_date, flat_index_15):
    """IRR проверяется независимо от солвера: бумага по номиналу, плоский индекс
    15%, маржа 150 б.п. → купон 16.5% simple с квартальной выплатой, значит
    эффективная годовая = (1+0.165/4)^4−1. Движок обязан выдать ровно её —
    расхождение ловит любую ошибку в day-count, якоре даты или компаундировании."""
    margin = 150
    bond = make_bond(margin_bps=margin, accrued=0.0)
    settle = settle_date(calc_date)
    periods = quarterly_periods(settle, bond.maturity_date)   # период стартует в день поставки
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, margin,
                                      explicit_periods=periods, index_pct_fn=fn)
    y = xirr_yield_pct(dirty_price_rub(bond.face_value, 100.0, 0.0), cfs, calc_date)
    eff = ((1 + (15.0 + margin / 100.0) / 100 / 4) ** 4 - 1) * 100
    assert y == pytest.approx(eff, abs=0.02), f"IRR={y:.4f}% != аналитика {eff:.4f}%"


def test_asof_accrual_resets_on_coupon_payment_date():
    """As-of на день выплаты купона (выходной): факт биржи — с последних торгов,
    ещё СТАРОГО периода почти в полный купон. Новый купон флоатера не опубликован,
    поэтому график НКД не даёт — раньше факт оставался как есть и dirty был завышен
    на целый купон (ФосАгро П2 @ 2026-08-09: 11.74₽, YTM 0.29%, R-spread −1453bps).
    Купон 09.08 → на 09.08 НКД = 0, на 10.08 = один день нового периода."""
    from datetime import date as _d
    from services.backdate import _accrue_to_date
    periods = [(_d(2026, 5, 9), _d(2026, 8, 9), 34.11),
               (_d(2026, 8, 9), _d(2026, 11, 9), None)]

    acc, note = _accrue_to_date(11.74, _d(2026, 8, 7), _d(2026, 8, 9), periods, 1000.0)
    assert acc == 0.0, "в день выплаты НКД нового периода = 0"
    assert "не удалось" not in (note or "")

    acc2, _ = _accrue_to_date(11.74, _d(2026, 8, 7), _d(2026, 8, 10), periods, 1000.0)
    daily = 34.11 / (_d(2026, 8, 9) - _d(2026, 5, 9)).days
    assert acc2 == pytest.approx(daily, abs=1e-3), "один день нового периода"


def test_accrue_to_settle_on_coupon_payment_date():
    """calc_date == старт купонного периода (день выплаты): накопления ноль,
    пропорцию строить не из чего. Раньше НКД на поставку возвращался как есть
    (0), т.е. dirty занижен на 1-3 дня. Теперь дни поставки начисляются по
    дневной ставке ПРЕДЫДУЩЕГО купона."""
    from datetime import date as _d
    from core.valuation import accrue_to_settle
    periods = [(_d(2026, 5, 8), _d(2026, 8, 7), 34.11),
               (_d(2026, 8, 7), _d(2026, 11, 7), None)]
    acc, note = accrue_to_settle(0.0, _d(2026, 8, 7), periods)   # пт → settle пн 10.08
    daily = 34.11 / (_d(2026, 8, 7) - _d(2026, 5, 8)).days
    assert acc == pytest.approx(daily * 3, abs=1e-3), "3 дня нового периода"
    assert note
