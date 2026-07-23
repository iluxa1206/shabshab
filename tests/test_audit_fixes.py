"""Регрессия аудит-фиксов (2026-07): put/call оферты, кэп/флор купона,
стейл-индекс, residual амортизации, spread-парс, index-yield, duration,
MOEX-праздники. Всё детерминировано, без сети.
"""
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods, CALC_DATE


# ── put/call оферты ──────────────────────────────────────────────────────
from valuation import offer_kind, first_offer_date, settle_date


@pytest.mark.parametrize("txt,kind", [
    ("Оферта", "put"),
    ("Оферта/Погашение", "put"),
    ("Оферта (состоялось)", "put"),
    ("", "put"),
    (None, "put"),
    ("Call-опцион", "call"),
    ("опцион эмитента", "call"),
    ("Досрочное погашение по усмотрению эмитента", "call"),
    ("КОЛЛ-оферта", "call"),
])
def test_offer_kind(txt, kind):
    assert offer_kind(txt) == kind


def test_first_offer_date_skips_call_and_completed():
    settle = settle_date(CALC_DATE)
    fut = (CALC_DATE + timedelta(days=200)).isoformat()
    near = (CALC_DATE + timedelta(days=100)).isoformat()
    offers = [
        {"date": near, "type": "опцион эмитента", "price": 100},   # call — игнор
        {"date": fut, "type": "Оферта", "price": 100},             # put — берём
    ]
    assert first_offer_date(offers, settle) == date.fromisoformat(fut)
    # состоявшаяся — игнор даже если дата будущая
    done = [{"date": near, "type": "Оферта (состоялось)", "price": 100}]
    assert first_offer_date(done, settle) is None
    # только call → нет горизонта держателя
    only_call = [{"date": near, "type": "call", "price": 100}]
    assert first_offer_date(only_call, settle) is None


# ── кэп/флор купона: парс числа ───────────────────────────────────────────
from services.coupon_calib import parse_prospectus_formula


@pytest.mark.parametrize("txt,cap,floor", [
    ("Ключевая ставка + 2%, но не более 18% годовых", 18.0, None),
    ("MIN(Ключевая ставка + 1.5%; 16%)", 16.0, None),
    ("среднее значение ставок RUONIA + спред, но не выше 20,5% годовых", 20.5, None),
    ("MAX(КС + 1%; 8%)", None, 8.0),
    ("Ключевая ставка + 2%, не менее 10% и не более 22%", 22.0, 10.0),
    ("RUONIA + 1.45%", None, None),
])
def test_cap_floor_parse(txt, cap, floor):
    ps = parse_prospectus_formula(txt) or {}
    assert ps.get("cap_pct") == cap
    assert ps.get("floor_pct") == floor


# ── кэп клэмпит прогнозный купон в pricing ────────────────────────────────
from valuation import build_cashflows_with_spread


def test_cap_clamps_future_coupons(keyrate_curve, calc_date, flat_index_15, monkeypatch):
    """Кривая 15% + спред 150 → купон ~16.5%. Кэп 15.5% срезает прогнозные."""
    import services.ref_data as rd
    orig = rd.coupon_formula
    monkeypatch.setattr(rd, "coupon_formula",
                        lambda i, *a, **k: {**orig(i, *a, **k), "cap_pct": 15.5, "capped": True})
    bond = make_bond(margin_bps=150)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, 150,
                                      explicit_periods=periods, index_pct_fn=fn)
    prev = calc_date
    for cf in cfs:
        if cf.type == "COUPON" and cf.pay_date > calc_date:
            days = (cf.pay_date - prev).days or 91
            rate = cf.amount_rub / bond.face_value * 365.0 / days * 100.0
            assert rate <= 15.5 + 1e-6, f"купон {rate} > кэп 15.5"
        prev = cf.pay_date


def test_floor_lifts_future_coupons(keyrate_curve, calc_date, flat_index_15, monkeypatch):
    """Флор 20% поднимает купоны (кривая 15%+1.5% ≈ 16.5% < 20)."""
    import services.ref_data as rd
    orig = rd.coupon_formula
    monkeypatch.setattr(rd, "coupon_formula",
                        lambda i, *a, **k: {**orig(i, *a, **k), "floor_pct": 20.0, "capped": True})
    bond = make_bond(margin_bps=150)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_with_spread(bond, keyrate_curve, calc_date, 150,
                                      explicit_periods=periods, index_pct_fn=fn)
    prev = calc_date
    seen_future = False
    for cf in cfs:
        if cf.type == "COUPON" and cf.pay_date > calc_date:
            days = (cf.pay_date - prev).days or 91
            rate = cf.amount_rub / bond.face_value * 365.0 / days * 100.0
            assert rate >= 20.0 - 1e-6, f"купон {rate} < флор 20"
            seen_future = True
        prev = cf.pay_date
    assert seen_future


# ── стейл-индекс: за пределом покрытия → форвард ──────────────────────────
from services.coupon_calib import _realized, projected_ks_pct


def test_realized_boundary():
    dates = [CALC_DATE - timedelta(days=30), CALC_DATE - timedelta(days=20)]
    idx = (dates, [15.0, 15.0])
    last = dates[-1]
    # в пределах grace от последней даты и <= calc → факт
    assert _realized(idx, last, CALC_DATE) is True
    assert _realized(idx, last + timedelta(days=3), CALC_DATE) is True   # grace=4
    # далеко за покрытием (но <= calc) → не факт (форвард)
    assert _realized(idx, CALC_DATE, CALC_DATE) is False
    # будущее относительно calc → не факт
    assert _realized(idx, CALC_DATE + timedelta(days=5), CALC_DATE) is False


def test_stale_history_routes_to_forward():
    """Стейл-история (последняя дата 15 дней назад): начавшийся период за пределом
    покрытия проецируется форвардом (99), не последним фактом (15)."""
    last = CALC_DATE - timedelta(days=15)
    idx = ([last - timedelta(days=5), last], [15.0, 15.0])
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "base": "KEYRATE"}
    start = CALC_DATE - timedelta(days=5)     # период начался
    end = CALC_DATE + timedelta(days=25)
    r = projected_ks_pct(spec, start, end, CALC_DATE, fwd_pct=lambda d: 99.0, idx=idx)
    # большинство дней за last+grace → форвард 99 доминирует, далеко от стейл-15
    assert r > 50.0, f"стейл-ставка утекла в купон: {r}"


# ── residual амортизации в display-builder ────────────────────────────────
from services.cashflow import build_cashflow_from_moex


def test_amort_residual_closes_to_outstanding(keyrate_curve, calc_date):
    """MOEX-список амортизаций недосчитывает финальный принципал → residual
    добивает будущий поток до остатка номинала."""
    bond = make_bond(margin_bps=150, face=1000.0, maturity=date(2027, 1, 12))
    # 3 будущих транша по 200 = 600 из 1000 outstanding; финальные 400 не в списке
    amorts = [
        {"date": (calc_date + timedelta(days=90)).isoformat(), "value": 200},
        {"date": (calc_date + timedelta(days=180)).isoformat(), "value": 200},
        {"date": (calc_date + timedelta(days=270)).isoformat(), "value": 200},
    ]
    coupons = [{"start": calc_date.isoformat(),
                "end": bond.maturity_date.isoformat(), "value": None}]
    items, red_total = build_cashflow_from_moex(
        bond, keyrate_curve, calc_date, coupons, amorts, "Ключевая ставка + 1.5%")
    future_red = sum(it["amount_rub"] for it in items
                     if it["type"] == "REDEMPTION" and it["payment_date"] > calc_date)
    assert future_red == pytest.approx(1000.0, abs=1.0), f"future redemption {future_red} != outstanding 1000"


# ── spread-парс: запятая ──────────────────────────────────────────────────
from cashflow import parse_base_and_spread


@pytest.mark.parametrize("formula,bps", [
    ("Ключевая ставка + 1,5%", 150),
    ("Ключевая ставка + 1.5%", 150),
    ("RUONIA + 2%", 200),
    ("КС + 0,75% годовых", 75),
])
def test_spread_parse_comma(formula, bps):
    _base, sp = parse_base_and_spread(formula, None)
    assert sp == bps


# ── index rolling yield ───────────────────────────────────────────────────
from valuation import index_rolling_yield_pct


def test_index_rolling_yield(keyrate_curve, ruonia_curve, calc_date):
    mat = date(calc_date.year + 3, calc_date.month, calc_date.day)
    ks = index_rolling_yield_pct("KEYRATE", keyrate_curve, calc_date, mat)
    ru = index_rolling_yield_pct("RUONIA", ruonia_curve, calc_date, mat)
    # плоские 15% par-кривые → эффективная годовая ~15% в каждой конвенции
    # (KEYRATE quarterly-simple и RUONIA daily-comp дают разный effective — это
    # свойство бутстрапа «15%», не баг; проверяем лишь вменяемость диапазона).
    assert 14.0 < ks < 17.0
    assert 14.0 < ru < 17.5
    # погашение в прошлом → None
    assert index_rolling_yield_pct("KEYRATE", keyrate_curve, mat, calc_date) is None


# ── duration_metrics ──────────────────────────────────────────────────────
from services.metrics import duration_metrics


def test_duration_metrics_sane():
    cd = CALC_DATE
    # простой поток: 3 годовых купона 15 + номинал 100
    cfs = [(cd + timedelta(days=365), 15.0),
           (cd + timedelta(days=730), 15.0),
           (cd + timedelta(days=1095), 115.0)]
    y = 0.15
    mod, conv, pvbp = duration_metrics(cfs, cd, y, dirty_rub=100.0)
    assert mod is not None and 2.0 < mod < 3.0     # ~2.6 лет
    assert conv is not None and conv > 0
    assert pvbp is not None and pvbp > 0
    # пустой поток → None
    assert duration_metrics([], cd, y, 100.0) == (None, None, None)


# ── MOEX-праздники: январь ────────────────────────────────────────────────
def test_settle_skips_january_holidays():
    # 31 дек → settle перепрыгивает 1-8 янв на первый рабочий (обычно 9-е)
    s = settle_date(date(2026, 12, 31))
    assert (s.month, s.day) not in {(1, d) for d in range(1, 9)}
    assert s.year == 2027 and s.month == 1 and s.day >= 9
