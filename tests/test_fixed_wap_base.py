"""Аналитика ФИКСОВ считается от средневзвешенной цены дня, а не от last.

Было: `compute_fixed_row` знал только цену last→prev, и вкладка ФИКСЫ →
Аналитика строила scatter и box-графики по цене последней сделки. В неликвиде
это один случайный принт, часто на закрытии: облако точек дрожало сильнее, чем
двигался рынок. У флоатеров та же аналитика давно стоит на средневзвесе
(`y_idx_wap_bps`), у фиксов эквивалента не было вовсе.

Считаем прямым пересчётом на средневзвешенной цене, а не линеаризацией от last:
у флоатеров наклон нужен ради стакана (спред по каждому уровню), здесь число
одно — второй проход дешевле двухточечной пробы и точен.
"""
from datetime import date

import pytest

from api.routes.calc import build_custom_schedule, _accrued
from core.valuation import settle_date
from services import fixed_income as fi


CD = date(2026, 8, 6)


@pytest.fixture
def sched():
    return build_custom_schedule(date(2029, 8, 15), 14.5, 4, 1000.0, CD)


@pytest.fixture(autouse=True)
def no_live_ticks(monkeypatch):
    """По умолчанию своего тикового средневзвеса нет — берётся биржевой."""
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: None)


def _row(sched, **kw):
    a = _accrued(sched, settle_date(CD), CD)
    base = {"isin": "RU_F", "last": 100.0, "prev": 99.5, "accrued": a}
    base.update(kw)
    return base


def test_wap_spread_differs_from_last_spread(sched):
    """Цена средневзвеса выше last → спред по ней НИЖЕ: это две разные цифры,
    и аналитика обязана брать вторую."""
    row = _row(sched, last=100.0, wap=101.0)
    out = fi.compute_fixed_row(row, sched, None, CD)
    assert out["wap_pct"] == 101.0
    assert out["ytm_wap"] is not None and out["ytm_wap"] < out["ytm"]


def test_wap_equal_last_reuses_number(sched):
    """Совпали цены — второго прохода солвера нет, число то же."""
    row = _row(sched, last=100.0, wap=100.0)
    out = fi.compute_fixed_row(row, sched, None, CD)
    assert out["g_spread_wap_bps"] == out["g_spread_bps"]


def test_no_wap_leaves_field_empty(sched):
    """Бумага сегодня не торговалась: средневзвеса нет — не выдумываем его из
    last, поле пустое (фронт откатится на спред по last и покажет как есть)."""
    row = _row(sched, last=100.0, wap=None)
    out = fi.compute_fixed_row(row, sched, None, CD)
    assert out["wap_pct"] is None and out.get("g_spread_wap_bps") is None


def test_live_tick_vwap_beats_exchange_waprice(sched, monkeypatch):
    """Свой тиковый средневзвес впереди биржевого: WAPRICE из ISS отстаёт."""
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: {"vwap_pct": 102.0})
    out = fi.compute_fixed_row(_row(sched, last=100.0, wap=101.0), sched, None, CD)
    assert out["wap_pct"] == 102.0


def test_calculator_price_override_has_no_wap(sched):
    """Калькулятор карточки считает под ВВЕДЁННУЮ цену — средневзвес там не при
    чём, и лишнего прохода солвера быть не должно."""
    out = fi.compute_fixed_row(_row(sched, wap=101.0), sched, None, CD,
                               price_override=98.0)
    assert out["last"] == 98.0
    assert "wap_pct" not in out and "g_spread_wap_bps" not in out
