"""Линкеры RUONIA: облигации с ИНДЕКСИРУЕМЫМ НОМИНАЛОМ и фиксированной ставкой.

Что здесь страхуется:
  • детект отделяет линкер RUONIA от золотых/ИПЦ-линкеров (они торгуются под
    тем же видом MOEX и тоже показывают растущий номинал);
  • поток строится в НОМИНАЛЬНЫХ рублях: купоны и погашение растут по индексу,
    а ставка купона остаётся фиксированной;
  • эхо MOEX (одна и та же сумма купона во всех будущих периодах) не принимается
    за факт — иначе весь хвост потока занижен;
  • признак линкера переживает обёртку build_cashflows_with_spread, через
    которую ходят ВСЕ солверы (SM/DM).
"""
from datetime import date, timedelta

import pytest

from conftest import make_bond, quarterly_periods, CALC_DATE
from core.valuation import build_cashflows_to_maturity, build_cashflows_with_spread


# ── провайдер роста номинала: плоские 15% годовых, дневная капитализация ──────
def _grow_15(frm: date, to: date, fwd_pct) -> float:
    """Тот же контракт, что services.linker.face_grow_provider: множитель роста
    индекса за (frm, to], поддерживает ход назад во времени."""
    days = (to - frm).days
    return (1.0 + 0.15 / 365.0) ** days


def _linker_bond(**kw):
    b = make_bond(base="RUONIA", margin_bps=185, **kw)
    b.face_index = "RUONIA"
    return b


# ── поток ────────────────────────────────────────────────────────────────────

def test_coupon_rate_stays_fixed_and_amount_grows(ruonia_curve, calc_date):
    """Ставка купона у линкера постоянна (S), а рублёвые суммы растут: платит
    фиксированный процент на растущий номинал."""
    bond = _linker_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    cfs = build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                      explicit_periods=periods, face_grow_fn=_grow_15)
    coupons = [cf for cf in cfs if cf.type == "COUPON"]
    assert coupons, "поток без купонов"
    assert all(cf.coupon_rate_pct == pytest.approx(1.85, abs=0.01) for cf in coupons)
    # хвостовой стаб короче регулярного периода — сравниваем только полные
    full = [cf.amount_rub for cf in coupons
            if (cf.period_end - cf.period_start).days == 91]
    assert full == sorted(full), "суммы купонов обязаны расти вместе с номиналом"
    assert full[-1] > full[0] * 1.5, "за 4 года под 15% номинал должен вырасти в ~1.7 раза"


def test_redemption_is_indexed(ruonia_curve, calc_date):
    """Погашение — ПРОИНДЕКСИРОВАННЫЙ номинал, а не биржевой снимок на сегодня."""
    bond = _linker_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    cfs = build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                      explicit_periods=periods, face_grow_fn=_grow_15)
    red = [cf for cf in cfs if cf.type == "REDEMPTION"]
    assert len(red) == 1
    assert red[0].amount_rub == pytest.approx(1000.0 * _grow_15(calc_date, bond.maturity_date, None),
                                              rel=1e-6)


def test_moex_echo_value_is_ignored(ruonia_curve, calc_date):
    """Сумма купона из bondization у линкера — ЭХО по сегодняшнему номиналу
    (MOEX проставляет её сразу во все периоды). Как факт она занижает хвост
    потока, поэтому билдер обязан её игнорировать."""
    bond = _linker_bond()
    echo = [(s, e, 4.5) for s, e, _ in quarterly_periods(bond.issue_date, bond.maturity_date)]
    cfs = build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                      explicit_periods=echo, face_grow_fn=_grow_15)
    amounts = [cf.amount_rub for cf in cfs if cf.type == "COUPON"]
    assert len({round(a, 4) for a in amounts}) > 1, "эхо принято за факт: все купоны равны"


def test_started_period_uses_past_index(ruonia_curve, calc_date):
    """Начавшийся период начислялся на МЕНЬШИЙ номинал: его купон должен быть
    ниже, чем если бы прошлые дни считались по сегодняшнему номиналу."""
    bond = _linker_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    cfs = build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                      explicit_periods=periods, face_grow_fn=_grow_15)
    first = min((cf for cf in cfs if cf.type == "COUPON"), key=lambda c: c.pay_date)
    days = (first.period_end - first.period_start).days
    naive = 1000.0 * 0.0185 * days / 365.0    # без индексации вовсе
    assert first.amount_rub < naive * 1.02, "прошлые дни периода посчитаны по сегодняшнему номиналу"


def test_plain_floater_is_untouched(ruonia_curve, calc_date, flat_index_15):
    """Бумага без face_index считается ровно как раньше — провайдер роста ей не
    передаётся и ничего не меняет."""
    bond = make_bond(base="RUONIA", margin_bps=185)
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    a = build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                    explicit_periods=periods, index_pct_fn=fn)
    b = build_cashflows_to_maturity(bond, ruonia_curve, calc_date, explicit_periods=periods,
                                    index_pct_fn=fn, face_grow_fn=_grow_15)
    assert [cf.amount_rub for cf in a] == [cf.amount_rub for cf in b]


def test_face_index_survives_spread_wrapper(ruonia_curve, calc_date):
    """build_cashflows_with_spread пересобирает BondRefData — без переноса
    face_index ВСЕ солверы (SM/DM) считали бы линкер на постоянном номинале."""
    bond = _linker_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    cfs = build_cashflows_with_spread(bond, ruonia_curve, calc_date, 185,
                                      explicit_periods=periods, face_grow_fn=_grow_15)
    red = [cf for cf in cfs if cf.type == "REDEMPTION"][0]
    assert red.amount_rub > 1000.0 * 1.5


def test_missing_provider_is_loud(ruonia_curve, calc_date):
    """Без провайдера роста поток занижен — деградация обязана быть помечена,
    а не проглочена (иначе линкер молча прайсится как фикс)."""
    bond = _linker_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    warns = []
    build_cashflows_to_maturity(bond, ruonia_curve, calc_date,
                                explicit_periods=periods, warnings_out=warns)
    assert any("номинал" in w for w in warns), warns


# ── детект ───────────────────────────────────────────────────────────────────

def _coupons(initial_face, rate, issue: date, n=4, step=182):
    out, s = [], issue
    for _ in range(n):
        e = s + timedelta(days=step)
        out.append({"start": s.isoformat(), "end": e.isoformat(),
                    "valueprc": rate, "value": 10.0, "initial_face": initial_face})
        s = e
    return out


def _stub_index(monkeypatch, issue: date, today: date, growth: float):
    """Официальный ряд индекса RUONIA: ровный рост до `growth` за [issue, today]."""
    levels, days = {}, (today - issue).days
    step = growth ** (1.0 / max(days, 1))
    for i in range(days + 1):
        levels[issue + timedelta(days=i)] = step ** i
    monkeypatch.setattr("services.coupon_calib.ruonia_index_levels",
                        lambda: (levels, today))
    monkeypatch.setattr("services.cbr.ruonia_history", lambda: [(today, 15.0)])
    return levels


def test_detects_ruonia_linker(monkeypatch):
    issue, today = date(2026, 5, 15), date(2026, 8, 28)
    _stub_index(monkeypatch, issue, today, 1.04)
    from services import linker
    assert linker.is_ruonia_linked(_coupons(1000.0, 1.85, issue), 1041.0, today)


def test_rejects_foreign_index(monkeypatch):
    """Золотой/ИПЦ-линкер: номинал тоже растёт, но не по RUONIA."""
    issue, today = date(2026, 5, 15), date(2026, 8, 28)
    _stub_index(monkeypatch, issue, today, 1.04)
    from services import linker
    assert not linker.is_ruonia_linked(_coupons(1000.0, 5.5, issue), 12269.0, today)
    assert not linker.is_ruonia_linked(_coupons(1000.0, 4.0, issue), 1399.0, today)


def test_rejects_plain_fixed_and_floater(monkeypatch):
    issue, today = date(2026, 5, 15), date(2026, 8, 28)
    _stub_index(monkeypatch, issue, today, 1.04)
    from services import linker
    # фикс: номинал не вырос
    assert not linker.is_ruonia_linked(_coupons(1000.0, 1.85, issue), 1000.0, today)
    # обычный флоатер: ставка от периода к периоду разная
    cps = _coupons(1000.0, 1.85, issue)
    cps[1]["valueprc"] = 16.5
    assert not linker.is_ruonia_linked(cps, 1041.0, today)
