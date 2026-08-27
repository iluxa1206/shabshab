"""Горизонт флоатера выбирается СПРЕДОМ (Y-IDX), а не доходностью.

Правка 2026-08-27. Сравнение YTM верно для бумаги с ФИКСИРОВАННЫМ купоном, но
у флоатера YTM — это IRR спроецированного потока, и на длинном горизонте он
определяется формой форвардной кривой, а не ценностью опциона.

На проде расходились 26 бумаг из 114 с будущей офертой; 24 из них — случай
«правило говорит погашение, спред говорит оферта».
"""
from datetime import date

import pytest

from services.valuation import _preferred_horizon

MAT = date(2036, 8, 18)
PUT = date(2029, 11, 8)
CALL = date(2029, 11, 8)


def _hz(y_mat=None, y_put=None, s_mat=None, s_put=None, s_call=None, y_call=None):
    out = {"maturity": {"date": MAT, "price_pct": 100.0}}
    if y_mat is not None:
        out["maturity"]["yield_xirr_pct"] = y_mat
    if s_mat is not None:
        out["maturity"]["yield_over_index_bps"] = s_mat
    if y_put is not None or s_put is not None:
        out["put"] = {"date": PUT, "price_pct": 100.0}
        if y_put is not None:
            out["put"]["yield_xirr_pct"] = y_put
        if s_put is not None:
            out["put"]["yield_over_index_bps"] = s_put
    if y_call is not None or s_call is not None:
        out["call"] = {"date": CALL, "price_pct": 100.0}
        if y_call is not None:
            out["call"]["yield_xirr_pct"] = y_call
        if s_call is not None:
            out["call"]["yield_over_index_bps"] = s_call
    return out


def test_rzd_54r_prices_to_offer():
    """БОЕВОЙ КЕЙС РЖД 1Р-54R (RU000A10FV69) при цене 99.45.

    YTM: 17.21 к погашению против 16.84 к оферте → старое правило выбирало
    ПОГАШЕНИЕ. Y-IDX: 206 против 229 → дисконт к номиналу реализуется в 2029-м,
    а не в 2036-м, держатель предъявит пут. Рынок так и прайсит."""
    hz = _hz(y_mat=17.2089, y_put=16.8383, s_mat=206.0, s_put=229.0)
    assert _preferred_horizon(99.45, hz) == "put"


def test_spread_wins_over_yield():
    """Метрики спорят — решает СПРЕД. Пин против возврата к YTM."""
    # YTM говорит «погашение», спред — «оферта»
    assert _preferred_horizon(99.5, _hz(y_mat=17.0, y_put=16.0,
                                        s_mat=100.0, s_put=200.0)) == "put"
    # и наоборот: YTM говорит «оферта», спред — «погашение»
    assert _preferred_horizon(99.5, _hz(y_mat=16.0, y_put=17.0,
                                        s_mat=200.0, s_put=100.0)) == "maturity"


def test_buffer_in_bps_of_spread():
    """Буфер меряется в bps СПРЕДА: внутри него горизонт не дребезжит."""
    # +9 bps — в буфере (10 bps), держим погашение
    assert _preferred_horizon(99.9, _hz(s_mat=200.0, s_put=209.0)) == "maturity"
    # +11 bps — за буфером
    assert _preferred_horizon(99.9, _hz(s_mat=200.0, s_put=211.0)) == "put"


def test_call_is_mirror():
    """CALL — право ЭМИТЕНТА: отзовёт, когда держателю ХУЖЕ (спред ниже)."""
    assert _preferred_horizon(100.5, _hz(s_mat=200.0, s_call=150.0)) == "call"
    assert _preferred_horizon(100.5, _hz(s_mat=200.0, s_call=250.0)) == "maturity"


def test_falls_back_to_yield_when_spread_missing():
    """У экзотики Y-IDX может не посчитаться — тогда работаем по YTM,
    приведённой к тем же bps."""
    # спреда нет, YTM к оферте выше на 100 bps (1 пп) → оферта
    assert _preferred_horizon(99.0, _hz(y_mat=16.0, y_put=17.0)) == "put"
    assert _preferred_horizon(99.0, _hz(y_mat=17.0, y_put=16.0)) == "maturity"


def test_falls_back_to_price_when_nothing_computed():
    """Ни спреда, ни доходности — остаётся грубое правило цены."""
    hz = {"maturity": {"date": MAT, "price_pct": 100.0},
          "put": {"date": PUT, "price_pct": 100.0}}
    assert _preferred_horizon(98.0, hz) == "put"      # заметно ниже выкупа
    assert _preferred_horizon(100.0, hz) == "maturity"


def test_no_price_no_metrics_is_maturity():
    hz = {"maturity": {"date": MAT}, "put": {"date": PUT}}
    assert _preferred_horizon(None, hz) == "maturity"
