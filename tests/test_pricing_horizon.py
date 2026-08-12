"""Горизонт прайсинга: правило цены (пут/колл) + обрезка потока к явной дате.

Правило (рынок RU): бумага ДОРОЖЕ цены выкупа — держатель выгодно кэррится и
пут не предъявит (горизонт = погашение); ДЕШЕВЛЕ — сдаст на оферту. Для колла
зеркально: эмитент отзывает дорогой для себя долг (цена выше цены выкупа).
"""
from datetime import date

import pytest

from core.valuation import (build_cashflows_to_maturity, first_call_date,
                            first_offer_date, settle_date)
from services.valuation import _preferred_horizon, pick_horizon

from conftest import make_bond, quarterly_periods


PUT_DATE = date(2028, 7, 12)
CALL_DATE = date(2027, 7, 12)


def _offers(kind: str, d: date, price: float = 100.0):
    typ = "Оферта" if kind == "put" else "Оферта (call, по усмотрению эмитента)"
    return [{"date": d.isoformat(), "type": typ, "price": price}]


def test_first_call_date_picks_only_call():
    settle = settle_date(date(2026, 5, 1))
    offers = _offers("put", PUT_DATE) + _offers("call", CALL_DATE)
    assert first_call_date(offers, settle) == CALL_DATE
    assert first_offer_date(offers, settle) == PUT_DATE, "пут-горизонт колл не подхватывает"


def test_cut_date_truncates_flow_to_call(keyrate_curve, calc_date, flat_index_15):
    """cut_date режет поток к ЛЮБОЙ дате, в т.ч. к call (to_offer его игнорирует)."""
    bond = make_bond()
    offers = _offers("call", CALL_DATE)
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    # to_offer=True колл не видит — поток идёт до погашения
    cfs_auto = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                           explicit_periods=periods, offers=offers,
                                           to_offer=True, index_pct_fn=fn)
    assert max(cf.pay_date for cf in cfs_auto) == bond.maturity_date

    cfs_call = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                           explicit_periods=periods, offers=offers,
                                           cut_date=CALL_DATE, index_pct_fn=fn)
    assert max(cf.pay_date for cf in cfs_call) == CALL_DATE
    principal = sum(cf.amount_rub for cf in cfs_call if cf.type == "REDEMPTION")
    assert principal == pytest.approx(1000.0), "выкуп остатка на колле по 100%"


def test_cut_date_uses_offer_buyout_price(keyrate_curve, calc_date, flat_index_15):
    bond = make_bond()
    offers = _offers("put", PUT_DATE, price=101.5)
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    fn, _ = flat_index_15
    cfs = build_cashflows_to_maturity(bond, keyrate_curve, calc_date,
                                      explicit_periods=periods, offers=offers,
                                      cut_date=PUT_DATE, index_pct_fn=fn)
    principal = sum(cf.amount_rub for cf in cfs if cf.type == "REDEMPTION")
    assert principal == pytest.approx(1015.0), "выкуп по цене оферты 101.5%"


HZ = {
    "maturity": {"date": date(2030, 1, 1), "price_pct": 100.0},
    "put": {"date": PUT_DATE, "price_pct": 100.0},
    "call": {"date": CALL_DATE, "price_pct": 100.0},
}


@pytest.mark.parametrize("price,keys,expect", [
    (98.0, ("maturity", "put"), "put"),        # дешевле выкупа → сдаст на оферту
    (100.0, ("maturity", "put"), "maturity"),  # ровно по выкупу → сдавать незачем
    (103.0, ("maturity", "put"), "maturity"),  # дороже выкупа → кэррится дальше
    (103.0, ("maturity", "call"), "call"),     # дороже выкупа → эмитент отзовёт
    (98.0, ("maturity", "call"), "maturity"),  # дешевле → отзывать невыгодно
    (98.0, ("maturity",), "maturity"),         # оферт нет
])
def test_preferred_horizon_rule(price, keys, expect):
    assert _preferred_horizon(price, {k: HZ[k] for k in keys}) == expect


def test_preferred_horizon_prefers_nearest_event():
    """Сработали оба опциона (редкая конфигурация) → ближайшее по дате событие."""
    hz = {"maturity": HZ["maturity"],
          "put": {"date": PUT_DATE, "price_pct": 105.0},    # цена 103 < 105 → пут сработал
          "call": {"date": CALL_DATE, "price_pct": 100.0}}  # цена 103 > 100 → колл сработал
    assert _preferred_horizon(103.0, hz) == "call", "колл ближе по дате"


def test_preferred_horizon_no_price():
    assert _preferred_horizon(None, HZ) == "maturity"


def test_pick_horizon_selection():
    m = {"preferred_horizon": "put",
         "horizons": {"maturity": {"sm_bps": 100}, "put": {"sm_bps": 250}}}
    assert pick_horizon(m, "auto")["sm_bps"] == 250
    assert pick_horizon(m)["horizon"] == "put"
    assert pick_horizon(m, "maturity")["sm_bps"] == 100
    # запрошен горизонт, которого у бумаги нет → молча погашение
    assert pick_horizon(m, "call")["horizon"] == "maturity"


def _metrics(price, offers, keyrate_curve, ruonia_curve, calc_date, monkeypatch, flat_index_15):
    """calculate_valuation_metrics на синтетике: без сети (провайдер индекса
    инжектируется вместо фетча ЦБ, спека формулы отключена)."""
    from services import valuation as vsvc
    fn, hist = flat_index_15
    monkeypatch.setattr(vsvc, "_index_provider", lambda *a, **k: (fn, hist))
    bond = make_bond()
    periods = quarterly_periods(bond.issue_date, bond.maturity_date)
    return vsvc.calculate_valuation_metrics(
        bond, price, keyrate_curve, calc_date, accrued_override=0.0,
        periods=periods, offers=offers, ruonia_curve=ruonia_curve)


def test_metrics_below_par_price_to_put(keyrate_curve, ruonia_curve, calc_date,
                                        monkeypatch, flat_index_15):
    """Цена ниже цены выкупа → горизонт пут, метрики пута заполнены и ОТЛИЧАЮТСЯ
    от метрик к погашению (иначе свитчер показывал бы одно и то же)."""
    m = _metrics(97.0, _offers("put", PUT_DATE), keyrate_curve, ruonia_curve,
                 calc_date, monkeypatch, flat_index_15)
    assert m["preferred_horizon"] == "put"
    assert m["offer_date"] == PUT_DATE
    put = m["horizons"]["put"]
    mat = m["horizons"]["maturity"]
    assert put["yield_xirr_pct"] is not None and mat["yield_xirr_pct"] is not None
    assert put["yield_xirr_pct"] != mat["yield_xirr_pct"], "к оферте доходность своя"
    assert put["index_yield_pct"] != mat["index_yield_pct"], "база роллирования до оферты"
    # legacy-поля к оферте согласованы с блоком горизонтов
    assert m["yield_to_offer_pct"] == put["yield_xirr_pct"]
    assert m["sm_to_offer_bps"] == put["sm_bps"]


def test_metrics_above_par_price_to_maturity(keyrate_curve, ruonia_curve, calc_date,
                                             monkeypatch, flat_index_15):
    """Цена выше цены выкупа → пут не форсируется, горизонт = погашение, но
    цифры к оферте всё равно посчитаны (свитчер их показывает)."""
    m = _metrics(103.0, _offers("put", PUT_DATE), keyrate_curve, ruonia_curve,
                 calc_date, monkeypatch, flat_index_15)
    assert m["preferred_horizon"] == "maturity"
    assert "put" in m["horizons"], "горизонт оферты доступен для ручного свитчера"
    assert m["horizons"]["put"]["yield_xirr_pct"] is not None


def test_metrics_above_par_call_selected(keyrate_curve, ruonia_curve, calc_date,
                                         monkeypatch, flat_index_15):
    """Правило колла — зеркальное: дороже выкупа → эмитент отзовёт."""
    m = _metrics(104.0, _offers("call", CALL_DATE), keyrate_curve, ruonia_curve,
                 calc_date, monkeypatch, flat_index_15)
    assert m["preferred_horizon"] == "call"
    assert m["offer_date"] == CALL_DATE
    assert m["horizons"]["call"]["sm_bps"] is not None
