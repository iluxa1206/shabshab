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


@pytest.mark.parametrize("price,keys,expect", [
    # МТС 3Р-02: bid/ask 99.95/100.00 — дисконт в копейки не окупает оферту,
    # и горизонт не должен скакать внутри одного спреда
    (99.95, ("maturity", "put"), "maturity"),
    (99.6, ("maturity", "put"), "maturity"),   # в буфере
    (99.5, ("maturity", "put"), "maturity"),   # ровно граница буфера — ещё погашение
    (99.49, ("maturity", "put"), "put"),       # за буфером — к оферте
    (100.4, ("maturity", "call"), "maturity"), # премия в буфере → отзывать незачем
    (100.51, ("maturity", "call"), "call"),
])
def test_preferred_horizon_par_buffer(price, keys, expect):
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


def test_reprice_response_serializes_horizons():
    """Регрессия прода: поле date в HorizonMetrics затеняло тип date в аннотации,
    и pydantic принимал только None → /reprice падал в 500 на любой бумаге с
    офертой. Схема обязана принимать реальную дату горизонта."""
    from api.schemas import RepriceResponse
    payload = {
        "clean_price_pct": 99.9, "dm_bps": None, "dm_label": None,
        "yield_xirr_pct": 15.3, "index_yield_pct": 14.0, "yield_over_index_bps": 129,
        "pricing_status": "SUCCESS", "preferred_horizon": "put",
        "horizons": {
            "maturity": {"date": date(2034, 5, 26), "price_pct": 100.0,
                         "yield_over_index_bps": 108, "y_idx_by_price": {99.5: 110}},
            "put": {"date": PUT_DATE, "price_pct": 100.0, "yield_over_index_bps": 129},
        },
    }
    r = RepriceResponse(**payload)
    assert r.horizons["maturity"].date == date(2034, 5, 26)
    assert r.horizons["put"].date == PUT_DATE
    assert r.horizons["maturity"].y_idx_by_price[99.5] == 110
