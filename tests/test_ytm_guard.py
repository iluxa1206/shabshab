"""Гард на мусорную YTM фиксов: дистресс настоящий, вырожденное — нет."""
from datetime import date

import pytest

from services import fixed_income as fi

CD = date(2026, 10, 14)


def test_guard_rules():
    g = fi.ytm_guard
    assert g(342.39, {"coupon_pct": 26.0}) is None             # дистресс Самолёта — настоящий
    assert g(85.15, {"coupon_pct": 23.7, "cls": "corp"}) is None
    assert g(854042.01, {"coupon_pct": 0.01}) == "range"       # вырожденный поток
    assert g(-99.87, {"coupon_pct": 0.01}) == "range"
    assert g(-4.68, {"coupon_pct": 0.01, "cls": "corp"}) == "structured"   # нота выше номинала
    assert g(-4.68, {"coupon_pct": 6.0, "cls": "corp"}) is None            # мелкий минус — бывает
    assert g(-43.82, {"coupon_pct": 14.25, "cls": "corp"}) == "negative"   # ДОМ.РФ Б-6 без сделок
    assert g(-8.0, {"coupon_pct": 6.84, "cls": "corp"}) == "negative"
    assert g(-4.68, {"coupon_pct": 0.01, "cls": "ofz"}) is None            # ОФЗ структурок нет
    assert g(25.0, {"coupon_pct": 0.01, "cls": "corp"}) is None            # дисконтная бескупонная
    assert g(None, {}) is None


def _note(price):
    row = {"isin": "RU000A0NOTE1", "name": "СберИОС", "maturity_date": "2028-03-09",
           "faceunit": "SUR", "face": 1000.0, "settle_face": 1000.0, "coupon_pct": 0.01,
           "cls": "corp", "accrued": 0.0, "last": price, "prev": price}
    full = {"coupons": [{"start": "2026-03-09", "end": "2028-03-09", "value": 0.1, "face": 1000}],
            "amorts": [{"date": "2028-03-09", "value": 1000}]}
    return row, full


def test_structured_note_above_par_has_no_ytm_or_spreads():
    row, full = _note(142.0)
    out = fi.compute_fixed_row(row, full, None, CD)
    assert out["ytm"] is None and out["ytm_guard"] in ("structured", "range")
    assert out["yield_over_index_bps"] is None and out["g_spread_bps"] is None
    # цена стороны: тот же гард в ветке сторон
    sides = fi.fixed_side_metrics(row, full, None, CD, [142.0])
    assert sides[142.0]["ytm"] is None and sides[142.0]["y_idx_bps"] is None


def test_discount_zero_coupon_keeps_ytm():
    row, full = _note(80.0)                      # бескупонная с дисконтом: ytm > 0
    out = fi.compute_fixed_row(row, full, None, CD)
    assert out["ytm"] is not None and out["ytm"] > 0 and out["ytm_guard"] is None


def test_delta_ignores_garbage_base(monkeypatch):
    import services.spread_history as sh
    monkeypatch.setattr(sh, "previous_day_ytm", lambda d=None, kind="fixed": {"A": -99.87, "B": 10.0})
    m = {"A": {"ytm": 12.0}, "B": {"ytm": 10.5}}
    fi.apply_ytm_delta(m, "2026-10-14")
    assert "delta_ytm" not in m["A"] and m["B"]["delta_ytm"] == 0.5
