"""price_stale: вчерашняя цена без сделки сегодня (замер 23.09.2026: у 64–75
фиксов и 173–191 флоатеров цена = prev-close, а флаг стоял False — Alor шлёт
в котировке последнюю цену сделки, в том числе вчерашнюю)."""
from datetime import date

import pytest

from core.valuation import settle_date
from services import fixed_income as fi
from services.custom_bond import _accrued, build_custom_schedule
from services.price_flags import price_is_stale

CD = date(2026, 8, 6)


@pytest.mark.parametrize("last,prev,traded,want", [
    (None, 99.5, None, True),      # прежнее правило: цены сделки нет вовсе
    (None, None, None, False),     # и вчерашней нет — нечего помечать
    (99.5, 99.5, False, True),     # цена = вчерашней, оборота сегодня нет
    (99.5, 99.5, True, False),     # сделка по той же цене, но оборот есть
    (99.5, 99.5, None, False),     # оборот неизвестен — правило молчит
    (99.8, 99.5, False, False),    # цена другая: вчерашней быть не может
    (99.5, None, False, False),    # prev неизвестен — сравнивать не с чем
])
def test_truth_table(last, prev, traded, want):
    assert price_is_stale(last, prev, traded) is want


@pytest.fixture
def sched():
    return build_custom_schedule(date(2029, 8, 15), 14.5, 4, 1000.0, CD)


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: None)
    fi.clear_price_metrics_cache()


def _row(sched, **kw):
    base = {"isin": "RU_F", "last": 99.5, "prev": 99.5,
            "accrued": _accrued(sched, settle_date(CD), CD)}
    base.update(kw)
    return base


def test_fixed_prev_close_without_turnover_is_stale(sched):
    out = fi.compute_fixed_row(_row(sched, val_today=0), sched, None, CD)
    assert out["price_stale"] is True


def test_fixed_turnover_today_clears_flag(sched):
    out = fi.compute_fixed_row(_row(sched, val_today=2_000_000), sched, None, CD)
    assert out["price_stale"] is False


def test_fixed_own_tick_turnover_counts(sched, monkeypatch):
    """Биржевой оборот ещё 0 (снапшот отстаёт), а свой счёт по тикам уже есть."""
    from services import live_quotes
    monkeypatch.setattr(live_quotes, "get", lambda isin: {"val_today": 1_500_000.0})
    out = fi.compute_fixed_row(_row(sched, val_today=0), sched, None, CD)
    assert out["price_stale"] is False


def test_fixed_unknown_turnover_keeps_old_behaviour(sched):
    out = fi.compute_fixed_row(_row(sched), sched, None, CD)      # val_today нет вовсе
    assert out["price_stale"] is False


def test_fixed_price_override_never_stale(sched):
    """Калькулятор карточки считает под произвольную цену — она не «вчерашняя»."""
    out = fi.compute_fixed_row(_row(sched, val_today=0), sched, None, CD, price_override=99.5)
    assert out["price_stale"] is False
