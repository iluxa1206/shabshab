"""Календарь выплат: фиксы из графика MOEX как есть (этап C единого монитора).

Своего прогноза у фиксов нет: сумма купона объявлена биржей либо её нет, и
«продлённый» купон (допущение для YTM) в календарь не попадает."""
from datetime import date, timedelta

from services.payments_calendar import fixed_bond_events

TODAY = date(2026, 10, 15)


def _d(n):
    return (TODAY + timedelta(days=n)).isoformat()


U = {"isin": "SU26238RMFS4", "cls": "ofz"}


def _events(full, size=1_000_000):
    return fixed_bond_events(U, "ОФЗ 26238", "Минфин России", full, TODAY, size)


def test_coupons_amorts_and_flags():
    ev = _events({
        "coupons": [{"start": _d(-90), "end": _d(-2), "value": 17.7, "valueprc": 7.1},
                    {"start": _d(-2), "end": _d(90), "value": 17.7, "valueprc": 7.1}],
        "amorts": [{"date": _d(400), "value": 1000.0}],
    })
    by = {(e["type"], e["date"].isoformat()): e for e in ev}
    paid = by[("COUPON", _d(-2))]
    assert paid["paid"] is True and paid["projected"] is False
    fut = by[("COUPON", _d(90))]
    assert fut["paid"] is False and fut["amount_rub"] == 17.7 and fut["rate_pct"] == 7.1
    red = by[("REDEMPTION", _d(400))]
    assert red["amount_rub"] == 1000.0 and red["total_rub"] == 1000.0 * 1_000_000
    assert {e["kind"] for e in ev} == {"fixed"} and {e["base"] for e in ev} == {"FIXED"}


def test_coupon_without_amount_is_not_invented():
    ev = _events({"coupons": [{"start": _d(0), "end": _d(90), "value": None, "valueprc": 8.0}],
                  "amorts": []})
    assert ev == []


def test_old_payments_outside_window_dropped():
    ev = _events({"coupons": [{"start": _d(-200), "end": _d(-30), "value": 10.0}],
                  "amorts": [{"date": _d(-30), "value": 100.0}]})
    assert ev == []


def test_unknown_issue_size_gives_none_total():
    ev = _events({"coupons": [{"start": _d(0), "end": _d(10), "value": 5.0}]}, size=None)
    assert ev[0]["total_rub"] is None


def test_perpetual_has_no_redemption():
    ev = _events({"coupons": [{"start": _d(0), "end": _d(10), "value": 5.0}], "amorts": []})
    assert [e["type"] for e in ev] == ["COUPON"]
