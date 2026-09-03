"""Спред стороны считается к горизонту САМОЙ ЭТОЙ ЦЕНЫ — везде одинаково.

Аудит конвейера 03.09, находка 4 (high). В enrich_bond горизонт выбирался один
раз по цене последней сделки, и из его блока брались спреды ВСЕХ альт-цен, а
движок (yidx_exact), лестница стакана (orderbook_svc) и скринер применяли
правило цены к каждой цене отдельно. В поле yoi_ask лежали числа двух разных
методик — какой, зависело от того, кто писал строку последним: десятиминутный
поллер (enrich_bond) или движок.
"""
from datetime import date, timedelta

import pytest

from services.universe import enrich_bond
from services.valuation import calculate_valuation_metrics, horizon_at_price
from tests.conftest import make_bond, quarterly_periods


CD = date(2026, 1, 12)
MATURITY = date(2036, 1, 12)
ISSUE = date(2024, 1, 12)
PUT = date(2026, 4, 13)          # оферта через 3 месяца, по 100
LAST, ASK = 99.0, 100.5


def _full():
    return {"coupons": [{"start": s, "end": e, "value": None}
                        for (s, e, _v) in quarterly_periods(ISSUE, MATURITY)],
            "amorts": None,
            "offers": [{"date": PUT.isoformat(), "type": "put", "price": 100.0}]}


def _expected_ask_yidx(ruonia_curve):
    """Эталон — тот же путь, которым считают движок, стакан и скринер."""
    bond = make_bond(base="RUONIA", maturity=MATURITY, issue=ISSUE)
    m = calculate_valuation_metrics(
        bond, LAST, ruonia_curve, CD, accrued_override=0.0,
        periods=[(date.fromisoformat(s), date.fromisoformat(e), v)
                 for (s, e, v) in quarterly_periods(ISSUE, MATURITY)],
        amorts=None, offers=_full()["offers"], ruonia_curve=ruonia_curve,
        with_margins=False, alt_prices=[ASK])
    hzs = m.get("horizons") or {}
    sel = hzs.get(horizon_at_price(ASK, m)) or hzs.get("maturity") or {}
    v = (sel.get("y_idx_by_price") or {}).get(ASK)
    return v if v is not None else (m.get("y_idx_by_price") or {}).get(ASK)


def test_enrich_side_spread_uses_price_own_horizon(ruonia_curve):
    row = enrich_bond(
        {"isin": "RU_TEST_0001", "base_rate_type": "RUONIA",
         "spread_issue_bps": 150},
        make_bond(base="RUONIA", maturity=MATURITY, issue=ISSUE),
        _full(),
        last=LAST, prev=LAST, accrued=0.0,
        ruonia_curve=ruonia_curve, keyrate_curve=None,
        exp_ks=None, exp_ru=None, g_curve=None, calc_date=CD,
        bid=None, ask=ASK)

    expected = _expected_ask_yidx(ruonia_curve)
    assert expected is not None, "фикстура не дала числа — тест бессмыслен"
    assert row["yoi_ask"] == pytest.approx(expected, abs=1.0)


def test_put_and_maturity_differ_on_this_fixture(ruonia_curve):
    """Страховка фикстуры: если горизонты дают одно и то же число, предыдущий
    тест ничего не проверяет."""
    bond = make_bond(base="RUONIA", maturity=MATURITY, issue=ISSUE)
    m = calculate_valuation_metrics(
        bond, LAST, ruonia_curve, CD, accrued_override=0.0,
        periods=[(date.fromisoformat(s), date.fromisoformat(e), v)
                 for (s, e, v) in quarterly_periods(ISSUE, MATURITY)],
        amorts=None, offers=_full()["offers"], ruonia_curve=ruonia_curve,
        with_margins=False, alt_prices=[ASK])
    hzs = m.get("horizons") or {}
    by_hz = {k: (h.get("y_idx_by_price") or {}).get(ASK) for k, h in hzs.items()}
    vals = [v for v in by_hz.values() if v is not None]
    assert len(vals) >= 2 and max(vals) - min(vals) > 20, by_hz
