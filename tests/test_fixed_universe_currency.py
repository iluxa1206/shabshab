from datetime import date

from services import fixed_income as fi


def test_currency_fixed_is_in_universe_and_ruble_curve_is_not_applied():
    row = {"isin": "RU000A1USD01", "secid": "USD01", "coupon_pct": 7.0,
           "maturity_date": "2030-01-01", "faceunit": "USD"}
    assert fi._is_fixed(row, "TQCB", set()) is True

    schedule = {"coupons": [{"date": "2027-01-01", "value": 70.0, "face": 1000.0}],
                "amorts": [{"date": "2027-01-01", "value": 1000.0}]}
    out = fi.compute_fixed_row({**row, "face": 1000.0, "prev": 100.0, "accrued": 0.0},
                               schedule, object(), date(2026, 9, 13))
    assert out["ytm"] is not None
    assert out["g_spread_bps"] is None
    assert out["face_value_rub"] is None
