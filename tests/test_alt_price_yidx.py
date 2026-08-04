"""Y-IDX по верху стакана (bid/ask) — колонки таблицы.

Проверяем, что alt_prices считает ту же метрику, что и основной путь (тот же
поток, та же база RUONIA), только с другим dirty на входе XIRR: цена ниже →
доходность выше → Y-IDX шире. Это дешёвый реюз, а не второй прогон модели.
"""
from datetime import timedelta

import pytest

from conftest import make_bond, quarterly_periods
from services.valuation import calculate_valuation_metrics


@pytest.fixture(autouse=True)
def _index(flat_index_15, monkeypatch):
    monkeypatch.setattr("services.valuation._index_provider",
                        lambda base, warnings, calc_date=None:
                        (flat_index_15[0], list(zip(*flat_index_15[1]))))


def _metrics(ruonia_curve, calc_date, price, alt_prices=None):
    bond = make_bond(base="RUONIA", margin_bps=150)
    periods = quarterly_periods(calc_date - timedelta(days=40), bond.maturity_date)
    return calculate_valuation_metrics(bond, price, ruonia_curve, calc_date,
                                       accrued_override=0.0, periods=periods,
                                       ruonia_curve=ruonia_curve,
                                       alt_prices=alt_prices)


def test_alt_price_matches_direct_run(ruonia_curve, calc_date):
    """Y-IDX на alt-цене == Y-IDX отдельного прогона на той же цене."""
    m = _metrics(ruonia_curve, calc_date, 100.0, alt_prices=[99.5])
    direct = _metrics(ruonia_curve, calc_date, 99.5)
    assert m["y_idx_by_price"][99.5] == direct["yield_over_index_bps"]


def test_bid_wider_than_ask(ruonia_curve, calc_date):
    """bid < ask по цене → Y-IDX в биде шире (дешевле купил — больше спред)."""
    bid, ask = 99.5, 100.5
    m = _metrics(ruonia_curve, calc_date, 100.0, alt_prices=[bid, ask])
    assert m["y_idx_by_price"][bid] > m["y_idx_by_price"][ask]


def test_no_alt_prices_keeps_empty_map(ruonia_curve, calc_date):
    """Без alt_prices — пустая карта, лишних XIRR не гоняем."""
    assert _metrics(ruonia_curve, calc_date, 100.0)["y_idx_by_price"] == {}


def test_absurd_alt_price_is_none(ruonia_curve, calc_date):
    """Битая alt-цена (dirty > Σ будущих потоков) — None, как и для mid."""
    m = _metrics(ruonia_curve, calc_date, 100.0, alt_prices=[400.0])
    assert m["y_idx_by_price"][400.0] is None
