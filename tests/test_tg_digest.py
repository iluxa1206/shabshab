"""Вечерний «разбор дня»: отбор данных и сборка альбома."""
import asyncio
from datetime import date

import pytest

from services import charts_png as ch
from services import tg_digest as dg


def _rows(day, prev):
    return {day: [
        # чистое движение: имя есть, оборот выше порога
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 200.0,
         "g_spread_close_bps": None, "close_pct": 99.0,
         "value": 50e6, "trades": 10},
        # оборот ниже порога — одна сделка не делает движения дня
        {"isin": "B", "kind": "floater", "y_idx_close_bps": 900.0,
         "g_spread_close_bps": None, "close_pct": 80.0, "value": 1e5, "trades": 1},
        # битый спред: в рейтинге он забрал бы весь масштаб
        {"isin": "C", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 999999.0, "close_pct": 10.0,
         "value": 90e6, "trades": 5},
        # вне реестра (имени нет) — дайджест про наш юниверс
        {"isin": "D", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 300.0, "close_pct": 95.0, "value": 80e6, "trades": 7},
    ], prev: [
        {"isin": "A", "kind": "floater", "y_idx_close_bps": 150.0,
         "g_spread_close_bps": None, "close_pct": 99.2, "value": 40e6, "trades": 8},
        {"isin": "B", "kind": "floater", "y_idx_close_bps": 100.0,
         "g_spread_close_bps": None, "close_pct": 81.0, "value": 2e5, "trades": 2},
        {"isin": "C", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 250.0, "close_pct": 10.5, "value": 70e6, "trades": 4},
        {"isin": "D", "kind": "fixed", "y_idx_close_bps": None,
         "g_spread_close_bps": 280.0, "close_pct": 95.1, "value": 60e6, "trades": 6},
    ]}


@pytest.fixture
def fake_day(monkeypatch):
    day, prev = "2026-08-25", "2026-08-22"
    data = _rows(day, prev)
    monkeypatch.setattr(dg, "_last_two_days", lambda: (day, prev))
    monkeypatch.setattr(dg, "_day_rows", lambda d: data[d])
    import services.instruments_registry as reg
    monkeypatch.setattr(reg, "labels_map",
                        lambda isins=None: {"A": {"name": "Тест 1Р-01"},
                                            "B": {"name": "Тест 1Р-02"},
                                            "C": {"name": "Тест 1Р-03"}})
    monkeypatch.setattr(dg, "_curve_series", lambda d: {})
    return day


def test_collect_filters_noise(fake_day):
    d = dg.collect()
    names = [m["name"] for m in d["movers"]]
    assert names == ["Тест 1Р-01"]           # B по обороту, C по санитару, D без имени
    assert d["movers"][0]["delta_bps"] == 50.0
    # обороты — только бумаги реестра, D за бортом
    assert [t["name"] for t in d["turnover"]] == ["Тест 1Р-03", "Тест 1Р-01", "Тест 1Р-02"]
    assert d["traded"] == 3


def test_build_album_makes_four_pngs(fake_day, monkeypatch):
    async def _no_payments(day):
        return [], date.fromisoformat(fake_day)
    monkeypatch.setattr(dg, "_payment_days", _no_payments)
    items, caption = asyncio.run(dg.build_album())
    assert [n for n, _p, _c in items] == ["movers.png", "turnover.png",
                                          "curve.png", "payments.png"]
    assert all(png[:4] == b"\x89PNG" for _n, png, _c in items)
    # подпись — только у первой картинки: Telegram показывает её под альбомом
    assert items[0][2] == caption and all(c is None for _n, _p, c in items[1:])
    assert "Разбор дня" in caption


def test_album_silent_without_data(monkeypatch):
    monkeypatch.setattr(dg, "collect", lambda: {"day": None})
    items, caption = asyncio.run(dg.build_album())
    assert items == [] and caption == ""


def test_payment_days_use_issue_totals(monkeypatch):
    """Суммируем total_rub (платёж по выпуску), а не amount_rub (на бумагу)."""
    base = date(2026, 8, 26)

    async def _cal():
        return {"calc_date": base, "events": [
            {"date": date(2026, 8, 27), "type": "COUPON",
             "amount_rub": 48.17, "total_rub": 3_965_258.06, "paid": False},
            {"date": date(2026, 8, 27), "type": "REDEMPTION",
             "amount_rub": 80.0, "total_rub": 6_585_440.0, "paid": False},
            {"date": date(2026, 8, 20), "type": "COUPON",       # уже прошло
             "amount_rub": 10.0, "total_rub": 1e9, "paid": True},
            {"date": date(2027, 1, 1), "type": "COUPON",        # вне окна
             "amount_rub": 10.0, "total_rub": 1e9, "paid": False},
        ]}

    import services.payments_calendar as pc
    monkeypatch.setattr(pc, "build_payments_calendar", _cal)
    days, frm = asyncio.run(dg._payment_days(date(2026, 8, 25)))
    assert frm == base
    assert len(days) == 1
    assert days[0]["coupon"] == pytest.approx(3_965_258.06)
    assert days[0]["redemption"] == pytest.approx(6_585_440.0)


def test_charts_survive_empty_input():
    assert ch.movers([], "x")[:4] == b"\x89PNG"
    assert ch.turnover([], "x")[:4] == b"\x89PNG"
    assert ch.curve([], [], "x")[:4] == b"\x89PNG"
    assert ch.payments([], "x")[:4] == b"\x89PNG"
