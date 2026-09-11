"""Сторож здоровья данных: ловит поломку раньше пользователя.

Авария 10.09.2026 прошла мимо всех существующих сторожей — они смотрят на
транспорт (стрим молчит, диск кончился, петля лагает), а транспорт был в
порядке ровно тогда, когда Y-IDX врал вдвое. Эти тесты фиксируют четыре
вопроса, которых не хватало.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from services import data_health as dh

_MSK = timezone(timedelta(hours=3))


# --- источники ------------------------------------------------------------

def test_open_breaker_is_reported(monkeypatch):
    monkeypatch.setattr("services.market_data.iss_breaker_state",
                        lambda: {"open": True, "fails": 8, "reopen_in_sec": 12.0,
                                 "cooldown_sec": 30.0})
    p = dh.source_problems()
    assert "iss" in p and "ISS" in p["iss"]


def test_closed_breaker_is_silent(monkeypatch):
    monkeypatch.setattr("services.market_data.iss_breaker_state",
                        lambda: {"open": False, "fails": 0, "reopen_in_sec": 0.0,
                                 "cooldown_sec": 30.0})
    assert dh.source_problems() == {}


# --- свежесть -------------------------------------------------------------

def test_stale_rates_reported(monkeypatch):
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "rates_date",
                        date.today() - timedelta(days=dh.RATES_STALE_DAYS + 3))
    monkeypatch.setattr(dh, "_age_hours", lambda p: 1.0)
    p = dh.freshness_problems()
    assert "rates" in p


def test_fresh_rates_silent(monkeypatch):
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "rates_date", date.today() - timedelta(days=1))
    monkeypatch.setattr(dh, "_age_hours", lambda p: 1.0)
    monkeypatch.setitem(market_cache, "prewarm_at", __import__("time").time())
    assert dh.freshness_problems() == {}


def test_prewarm_that_never_ran_reported(monkeypatch):
    import time as _t
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "rates_date", date.today())
    monkeypatch.setattr(dh, "_age_hours", lambda p: 1.0)
    monkeypatch.setitem(market_cache, "prewarm_at",
                        _t.time() - (dh.PREWARM_STALE_HOURS + 2) * 3600)
    assert "prewarm" in dh.freshness_problems()


# --- правдоподобие --------------------------------------------------------

def _market(y_idx, price, n=60):
    return {f"RU00TEST{i:04d}": {"y_idx_bps": y_idx, "last": price} for i in range(n)}


def _prev(y_idx, price, n=60):
    return {f"RU00TEST{i:04d}": {"y_idx": y_idx, "price_pct": price} for i in range(n)}


def test_market_wide_yidx_jump_on_quiet_prices_is_caught(monkeypatch):
    """Ровно форма аварии 10.09: цены стоят, спред всего рынка удвоился."""
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "universe_metrics", _market(228.0, 100.20))
    monkeypatch.setattr("services.spread_history.previous_day_spreads",
                        lambda: _prev(112.0, 100.16))
    p = dh.sanity_problems()
    assert "yidx_jump" in p and "+116" in p["yidx_jump"]


def test_same_jump_with_moving_prices_is_not_reported(monkeypatch):
    """Цена уехала — спред обязан был поехать следом. Это рынок, не поломка."""
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "universe_metrics", _market(228.0, 99.0))
    monkeypatch.setattr("services.spread_history.previous_day_spreads",
                        lambda: _prev(112.0, 100.16))
    assert dh.sanity_problems() == {}


def test_small_sample_is_not_judged(monkeypatch):
    """На горстке бумаг медиана — шум; молчим, а не пугаем."""
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "universe_metrics", _market(228.0, 100.2, n=5))
    monkeypatch.setattr("services.spread_history.previous_day_spreads",
                        lambda: _prev(112.0, 100.16, n=5))
    assert dh.sanity_problems() == {}


def test_no_previous_day_is_silent(monkeypatch):
    from services.market_data import market_cache
    monkeypatch.setitem(market_cache, "universe_metrics", _market(228.0, 100.2))
    monkeypatch.setattr("services.spread_history.previous_day_spreads", lambda: {})
    assert dh.sanity_problems() == {}


# --- движок ---------------------------------------------------------------

def _stats(**kw):
    base = {"ctx": 600, "ctx_no_accrued": 0, "dirty": 10,
            "rate": {"rows_per_min": 450, "row_ms": 3}}
    base.update(kw)
    return base


def test_missing_accrued_everywhere_is_reported(monkeypatch):
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: _stats(ctx_no_accrued=619, ctx=619))
    assert "accrued" in dh.engine_problems()


def test_starving_engine_reported_in_trading_hours(monkeypatch):
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: _stats(rate={"rows_per_min": 8, "row_ms": 66}))
    monkeypatch.setattr(dh, "trading_hours", lambda now=None: True)
    assert "engine_rate" in dh.engine_problems()


def test_draining_queue_is_not_an_alarm(monkeypatch):
    """Догрев после рестарта: очередь длинная, но ТАЕТ — тревожить незачем.
    11.09.2026 сторож разбудил на 992 → 814 → 640 → 608, хотя движок справлялся."""
    monkeypatch.setattr(dh, "trading_hours", lambda now=None: True)
    monkeypatch.setattr(dh, "_dirty_seen", {"value": None, "stuck": 0}, raising=False)
    for q in (992, 814, 640, 608):
        monkeypatch.setattr("services.universe_stream.stats", lambda q=q: _stats(dirty=q))
        assert "engine_queue" not in dh.engine_problems(), f"ложная тревога на {q}"


def test_stuck_queue_is_reported(monkeypatch):
    """А вот очередь, которая НЕ уменьшается — настоящий завал."""
    monkeypatch.setattr(dh, "trading_hours", lambda now=None: True)
    monkeypatch.setattr(dh, "_dirty_seen", {"value": None, "stuck": 0}, raising=False)
    monkeypatch.setattr("services.universe_stream.stats", lambda: _stats(dirty=1500))
    seen = [("engine_queue" in dh.engine_problems()) for _ in range(3)]
    assert seen[0] is False, "первая проверка — ещё не тренд"
    assert seen[-1] is True, "очередь стоит несколько проверок — это завал"


def test_growing_queue_is_reported(monkeypatch):
    """Растущая очередь — тем более."""
    monkeypatch.setattr(dh, "trading_hours", lambda now=None: True)
    monkeypatch.setattr(dh, "_dirty_seen", {"value": None, "stuck": 0}, raising=False)
    res = []
    for q in (1000, 1200, 1400):
        monkeypatch.setattr("services.universe_stream.stats", lambda q=q: _stats(dirty=q))
        res.append("engine_queue" in dh.engine_problems())
    assert res[-1] is True


def test_quiet_engine_outside_session_is_normal(monkeypatch):
    """Ночью движок молчит законно — будить админов незачем."""
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: _stats(rate={"rows_per_min": 0, "row_ms": 0},
                                       dirty=5000))
    monkeypatch.setattr(dh, "trading_hours", lambda now=None: False)
    assert dh.engine_problems() == {}


def test_trading_hours_boundaries():
    assert dh.trading_hours(datetime(2026, 9, 10, 12, 0, tzinfo=_MSK)) is True
    assert dh.trading_hours(datetime(2026, 9, 10, 22, 0, tzinfo=_MSK)) is False
    assert dh.trading_hours(datetime(2026, 9, 12, 12, 0, tzinfo=_MSK)) is False  # суббота


def test_all_problems_survives_a_broken_layer(monkeypatch):
    """Сторож, падающий вместе с системой, бесполезен именно тогда, когда нужен."""
    def boom():
        raise RuntimeError("слой сломался")
    monkeypatch.setattr(dh, "source_problems", boom)
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: _stats(ctx_no_accrued=619, ctx=619))
    assert "accrued" in dh.all_problems()


# --- гейт на запись в архив ----------------------------------------------

def test_degraded_snapshot_is_marked(monkeypatch):
    """Снимок, снятый без биржевого НКД, обязан помечаться: пустая дата в
    истории — тоже ложь, но метка позволяет пересчитать день позже."""
    from services import spread_history as sh
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: {"ctx": 619, "ctx_no_accrued": 619})
    assert sh._degraded_now() is True
    monkeypatch.setattr("services.universe_stream.stats",
                        lambda: {"ctx": 619, "ctx_no_accrued": 3})
    assert sh._degraded_now() is False


def test_degraded_day_is_not_used_as_comparison_base(monkeypatch):
    """База сравнения для сторожа — только честные снимки, иначе завтрашняя
    проверка сравнивала бы кривое с кривым и молчала."""
    import inspect
    from services import spread_history as sh
    src = inspect.getsource(sh.previous_day_spreads)
    assert "src='snap'" in src and "snap_degraded" not in src
