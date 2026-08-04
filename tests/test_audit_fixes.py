"""Регрессия аудит-фиксов (2026-07): put/call оферты, кэп/флор купона,
стейл-индекс, residual амортизации, spread-парс, index-yield, duration,
MOEX-праздники. Всё детерминировано, без сети.
"""
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods, CALC_DATE


# ── put/call оферты ──────────────────────────────────────────────────────
from core.valuation import offer_kind, first_offer_date, settle_date


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
from core.valuation import build_cashflows_with_spread


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


def test_realized_internal_hole():
    """ЛОКАЛЬНОЕ покрытие: внутренняя дыра (плотно до −15д, затем свежая точка −1д)
    не должна выдавать день В ДЫРЕ за факт, хотя max-дата свежая."""
    d = CALC_DATE
    dts = [d - timedelta(days=17), d - timedelta(days=16), d - timedelta(days=15),
           d - timedelta(days=1)]           # дыра −14..−2
    idx = (dts, [15.0] * 4)
    assert _realized(idx, d - timedelta(days=15), d) is True    # край плотного участка
    assert _realized(idx, d - timedelta(days=8), d) is False    # в дыре → форвард
    assert _realized(idx, d - timedelta(days=1), d) is True     # свежая точка


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


def test_display_future_matches_pricing(keyrate_curve, calc_date, flat_index_15):
    """Консолидация: БУДУЩИЕ купонные суммы display-таблицы == суммы канонического
    build_cashflows_with_spread (единый источник, карточка не расходится с SM/z)."""
    from core.valuation import build_cashflows_with_spread, settle_date
    fn, _ = flat_index_15
    bond = make_bond(margin_bps=150)
    periods = quarterly_periods(calc_date, bond.maturity_date)
    coupons = [{"start": s, "end": e, "value": v} for s, e, v in periods]
    items, _ = build_cashflow_from_moex(bond, keyrate_curve, calc_date, coupons, [], "КС+1.5%")
    canon = build_cashflows_with_spread(bond, keyrate_curve, calc_date, 150,
                                        explicit_periods=periods, index_pct_fn=fn)
    disp = {c["payment_date"]: c["amount_rub"] for c in items if c["type"] == "COUPON"}
    checked = 0
    for cf in canon:
        # чисто будущие периоды (start > calc) — forward-проекция, детерминирована;
        # начавшийся период зависит от индекс-истории, в тесте не сверяем
        if cf.type == "COUPON" and cf.period_start and cf.period_start > calc_date:
            assert disp.get(cf.pay_date) == pytest.approx(round(cf.amount_rub, 2), abs=0.01), \
                f"{cf.pay_date}: display {disp.get(cf.pay_date)} != pricing {cf.amount_rub}"
            checked += 1
    assert checked >= 3


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
from core.cashflow import parse_base_and_spread


@pytest.mark.parametrize("formula,bps", [
    ("Ключевая ставка + 1,5%", 150),
    ("Ключевая ставка + 1.5%", 150),
    ("RUONIA + 2%", 200),
    ("КС + 0,75% годовых", 75),
])
def test_spread_parse_comma(formula, bps):
    _base, sp = parse_base_and_spread(formula, None)
    assert sp == bps


# ── ruonia rolling yield (единая база Y-IDX) ──────────────────────────────
from core.valuation import ruonia_rolling_yield_pct


def test_ruonia_rolling_yield(ruonia_curve, calc_date):
    mat = date(calc_date.year + 3, calc_date.month, calc_date.day)
    ru = ruonia_rolling_yield_pct(ruonia_curve, calc_date, mat)
    # плоская 15% par-кривая → эффективная годовая ~15% (компаундинг в рабочие дни
    # даёт чуть меньше сплошного дневного, но заметно больше простой ставки)
    assert 14.5 < ru < 17.0
    f = ruonia_curve.forward(settle_date(calc_date), mat)
    daily_comp = ((1.0 + f / 365.0) ** 365 - 1.0) * 100.0
    assert ru < daily_comp, "рабочие дни капитализируются реже сплошного дневного"
    # погашение в прошлом → None
    assert ruonia_rolling_yield_pct(ruonia_curve, mat, calc_date) is None


def test_ruonia_weekend_is_simple_not_compounded(ruonia_curve, calc_date):
    """Выходные не капитализируются: рост за пятницу→понедельник = простая ставка
    за 3 дня, а не (1+r/365)^3. Разница мала, но она — суть конвенции."""
    from core.valuation import _ruonia_path
    start = settle_date(calc_date)
    path = _ruonia_path(ruonia_curve, start)
    horizon = start + timedelta(days=370)
    g = path.growth_to(horizon)
    # число фиксингов < числа календарных дней ровно на выходные/праздники
    n_days = (horizon - start).days
    n_fix = sum(1 for k in path.days if k <= n_days)
    assert n_fix < n_days * 0.75, "капитализация идёт каждый календарный день"
    r = ruonia_curve.daily_forward(start)
    assert g < (1.0 + r / 365.0) ** n_days, "рост не должен превышать сплошной дневной комп"
    assert g > 1.0 + r * n_days / 365.0, "рост должен превышать простую ставку за период"


def test_yidx_base_is_ruonia_for_keyrate_bond(keyrate_curve, ruonia_curve, calc_date,
                                              flat_index_15, monkeypatch):
    """КС-бумага сравнивается с роллированием RUONIA: index_yield_pct равен
    доходности RUONIA-ноги, а без переданной RUONIA-кривой Y-IDX не считается."""
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None: (flat_index_15[0], list(zip(*flat_index_15[1]))))
    import services.valuation as sv
    bond = make_bond(margin_bps=150, accrued=0.0)
    periods = quarterly_periods(settle_date(calc_date), bond.maturity_date)

    m = sv.calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                       accrued_override=0.0, periods=periods,
                                       ruonia_curve=ruonia_curve)
    expect = ruonia_rolling_yield_pct(ruonia_curve, calc_date, bond.maturity_date)
    assert m["index_yield_pct"] == pytest.approx(expect, abs=1e-4)

    m2 = sv.calculate_valuation_metrics(bond, 100.0, keyrate_curve, calc_date,
                                        accrued_override=0.0, periods=periods)
    assert m2["yield_over_index_bps"] is None
    assert any("RUONIA-кривая" in w for w in m2["warnings"])


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


# ── расчётный индекс в дневной раскладке паспорта ─────────────────────────
from services.bond_audit import accrue_index


def _row(day, obs, rate):
    return {"day": day, "obs_date": obs, "rate_pct": rate}


def test_accrue_index_weekend_is_simple_then_capitalizes():
    """Пт 14.25% → сб, вс И ПН прирастают на 14.25/365 каждый (фиксинг дня даёт
    прирост следующего, база заморожена на выходные); пн 15% → вторник
    прирастает на 15/365 уже от капитализированной базы."""
    rows = [
        _row("2026-08-07", "2026-08-07", 14.25),   # пт — фиксинг опубликован
        _row("2026-08-08", "2026-08-08", 14.25),   # сб — повтор пятничного
        _row("2026-08-09", "2026-08-09", 14.25),   # вс — повтор
        _row("2026-08-10", "2026-08-10", 15.00),   # пн — новый фиксинг
        _row("2026-08-11", "2026-08-11", 15.00),   # вт
        _row("2026-08-12", "2026-08-12", 15.00),   # ср
    ]
    st = accrue_index(rows)
    idx = [r["index"] for r in rows]
    inc = [idx[i] - idx[i - 1] for i in range(1, len(idx))]
    assert idx[0] == 1.0, "первый день раскладки — старт индекса"
    # сб, вс, пн: три равных прироста по пятничной ставке от уровня пятницы
    assert inc[0] == pytest.approx(0.1425 / 365, abs=1e-9)
    assert inc[1] == pytest.approx(inc[0], abs=1e-9)
    assert inc[2] == pytest.approx(inc[0], abs=1e-9)
    # вторник: ставка понедельника, база капитализирована в понедельник
    assert inc[3] == pytest.approx(idx[3] * 0.15 / 365, abs=1e-9)
    assert inc[3] > inc[2], "во вторник ступенька вверх"
    assert inc[4] > inc[3], "среда — от ещё подросшей базы"
    assert st["start"] == 1.0
    assert st["end"] > idx[-1], "конец периода = уровень после последнего фиксинга"


def test_accrue_index_is_continuous_across_coupons():
    """Индекс СКВОЗНОЙ: второй купон продолжает первый, а не стартует с 1.0.
    Стык бесшовный — end первого купона == index первой строки второго."""
    c1 = [_row("2026-08-05", "2026-08-05", 14.0),
          _row("2026-08-06", "2026-08-06", 14.0),
          _row("2026-08-07", "2026-08-07", 14.0)]
    c2 = [_row("2026-08-10", "2026-08-10", 16.0),
          _row("2026-08-11", "2026-08-11", 16.0)]
    st1 = accrue_index(c1)
    st2 = accrue_index(c2, st1)

    assert c2[0]["index"] > 1.0, "второй купон не должен зануляться"
    assert c2[0]["index"] == st1["end"], "стык купонов без разрыва"
    assert st2["start"] == pytest.approx(st1["level"], abs=1e-9)   # start округлён до 10 знаков
    # прирост первого дня второго купона — по ставке последней строки первого
    assert c2[0]["index"] - c1[-1]["index"] == pytest.approx(
        c1[-1]["index"] * 0.14 / 365, abs=1e-9)
    # монотонность через границу купонов
    all_idx = [r["index"] for r in c1 + c2]
    assert all_idx == sorted(all_idx)


def test_accrue_index_skips_days_without_rate():
    """День без ставки (нет истории и кривая молчит) индекс не двигает."""
    rows = [_row("2026-08-05", "2026-08-05", 14.0),
            _row("2026-08-06", "2026-08-06", None),
            _row("2026-08-07", "2026-08-07", None),
            _row("2026-08-10", "2026-08-10", 14.0)]
    accrue_index(rows)
    assert rows[1]["index"] > rows[0]["index"], "ставка 06-го есть у пред. строки"
    assert rows[2]["index"] == rows[1]["index"], "у 06-го ставки нет → 07-е не растёт"
    assert rows[3]["index"] == rows[2]["index"]


def test_accrue_index_capitalizes_on_fixing_not_calendar():
    """Капитализация — по ФЛАГУ ФИКСИНГА, а не по календарю MOEX. 03.05.2010 —
    рабочий понедельник биржи, но фиксинга RUONIA нет (ЦБ повторяет пятничный):
    база в этот день замораживается, иначе индекс уезжает вверх (расхождение с
    официальным рядом ЦБ, видное с 04.05.2010)."""
    def _fx(day, rate, is_fixing):
        return {"day": day, "obs_date": day, "rate_pct": rate, "is_fixing": is_fixing}

    rows = [_fx("2010-04-30", 2.91, True),    # пт — фиксинг
            _fx("2010-05-01", 2.91, False),   # сб — повтор
            _fx("2010-05-02", 2.91, False),   # вс — повтор
            _fx("2010-05-03", 2.91, False),   # пн-праздник: биржа работает, ЦБ молчит
            _fx("2010-05-04", 2.84, True)]    # вт — новый фиксинг
    accrue_index(rows)
    idx = [r["index"] for r in rows]
    inc = [idx[i] - idx[i - 1] for i in range(1, len(idx))]
    # четыре прироста по одной ставке от ОДНОЙ замороженной базы — все равны
    for i in range(1, 4):
        assert inc[i] == pytest.approx(inc[0], abs=1e-9)


def test_accrue_index_year_basis_is_of_accrual_day():
    """Делитель — длина года ДНЯ НАЧИСЛЕНИЯ, а не дня прироста: прирост 1 января
    относится к 31 декабря и делится на длину СТАРОГО года."""
    rows = [{"day": "2015-12-31", "obs_date": "2015-12-31", "rate_pct": 10.0, "is_fixing": True},
            {"day": "2016-01-01", "obs_date": "2016-01-01", "rate_pct": 10.0, "is_fixing": False}]
    accrue_index(rows)
    inc = rows[1]["index"] - rows[0]["index"]
    assert inc == pytest.approx(0.10 / 365, abs=1e-9), "2015 — не високосный"


# ── compounded-купон: ставка из ОФИЦИАЛЬНОГО индекса ЦБ ───────────────────
def test_compounded_uses_official_cbr_index(monkeypatch):
    """Rj = (Index(e−lag)/Index(s−lag) − 1)·365/T по ряду ЦБ, а не произведение
    дневных ставок: индекс подменяем известным рядом и ждём точную формулу."""
    import services.coupon_calib as cc

    s, e, lag = date(2026, 1, 10), date(2026, 4, 10), 7
    levels = {}
    lvl = 1.0
    d = date(2025, 12, 1)
    while d <= date(2026, 5, 1):            # ровно 10% годовых, ежедневный ряд
        levels[d] = lvl
        lvl *= 1.0 + 0.10 / 365.0
        d += timedelta(days=1)
    last = date(2026, 5, 1)
    monkeypatch.setattr(cc, "ruonia_index_levels", lambda: (levels, last))

    spec = {"mode": "average", "lag": lag, "lag_unit": "cal", "base": "RUONIA",
            "compounded": True}
    got = cc.projected_ks_pct(spec, s, e, date(2026, 5, 1),
                              fwd_pct=lambda d: 10.0, idx=([], []))
    days = (e - s).days
    want = (levels[e - timedelta(days=lag)] / levels[s - timedelta(days=lag)] - 1.0) \
        * 365.0 / days * 100.0
    assert got == pytest.approx(want, abs=1e-9)


def test_compounded_falls_back_without_cbr_index(monkeypatch):
    """ЦБ недоступен и кэша нет → приближение произведением дневных ставок,
    а не падение и не ноль."""
    import services.coupon_calib as cc

    monkeypatch.setattr(cc, "ruonia_index_levels", lambda: (None, None))
    spec = {"mode": "average", "lag": 0, "lag_unit": "cal", "base": "RUONIA",
            "compounded": True}
    s, e = date(2026, 1, 10), date(2026, 4, 10)
    got = cc.projected_ks_pct(spec, s, e, date(2026, 1, 1),
                              fwd_pct=lambda d: 10.0, idx=([], []))
    n = (e - s).days
    want = ((1.0 + 0.10 / 365.0) ** n - 1.0) * 365.0 / n * 100.0
    assert got == pytest.approx(want, abs=1e-9)


def test_accrue_index_all_empty_returns_none_end():
    rows = [_row("2026-08-07", "2026-08-07", None),
            _row("2026-08-10", "2026-08-10", None)]
    st = accrue_index(rows)
    assert st["end"] is None and st["seen"] is False


def test_ruonia_base_leg_uses_actual_year_length():
    """База Y-IDX делит на ФАКТИЧЕСКУЮ длину года (ACT/ACT, как индекс ЦБ):
    горизонт через високосный год растёт медленнее, чем при фиксированных 365."""
    from core.valuation import _RuoniaCompoundPath

    class _Flat:                      # плоские 10% годовых, узлов нет
        nodes = []
        def daily_forward(self, d):
            return 0.10

    # окно ровно в високосном 2028-м vs то же число дней в обычном 2027-м
    leap = _RuoniaCompoundPath(_Flat(), date(2028, 1, 3))
    norm = _RuoniaCompoundPath(_Flat(), date(2027, 1, 4))
    g_leap = leap.growth_to(date(2028, 1, 3) + timedelta(days=300))
    g_norm = norm.growth_to(date(2027, 1, 4) + timedelta(days=300))
    assert g_leap < g_norm, "в високосном году дневное начисление меньше"
    # отношение приростов = 365/366 с точностью до капитализации
    assert (g_leap - 1) / (g_norm - 1) == pytest.approx(365 / 366, abs=2e-4)

    # день внутри невисокосного года — ровно ставка/365
    p = _RuoniaCompoundPath(_Flat(), date(2027, 1, 4))       # пн
    assert p.growth_to(date(2027, 1, 5)) - 1 == pytest.approx(0.10 / 365, abs=1e-12)
    # тот же день в високосном — ставка/366
    p2 = _RuoniaCompoundPath(_Flat(), date(2028, 1, 3))      # пн
    assert p2.growth_to(date(2028, 1, 4)) - 1 == pytest.approx(0.10 / 366, abs=1e-12)
