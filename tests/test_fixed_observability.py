"""Наблюдаемость фиксов: прочерки сторон по причинам и задержка тик → патч."""
from services import universe_stream as us
from services.market_data import market_cache


def _setup(monkeypatch):
    fm = {
        "A": {"bid": 99.0, "ask": 99.5, "y_idx_bid_bps": -10, "y_idx_ask_bps": None, "ytm": 13.0},
        "B": {"bid": 99.0, "ask": None, "y_idx_bid_bps": None, "ytm": 13.0},
        "C": {"bid": 99.0, "y_idx_bid_bps": None, "ytm": None},
        "D": {"bid": 99.0, "y_idx_bid_bps": None, "ytm": 12.0},
        "E": {"bid": 0, "ask": 0, "y_idx_bid_bps": None, "ytm": 12.0},      # стороны нет
        "F": {"bid": 70.0, "y_idx_bid_bps": None, "ytm": 9.0},              # USD
        "P": {"bid": 60.0, "y_idx_bid_bps": None, "ytm": 15.0},             # бессрочная
        "G": {"bid": 120.0, "y_idx_bid_bps": None, "ytm": None, "ytm_guard": "structured"},
    }
    monkeypatch.setitem(market_cache, "fixed_metrics", fm)
    monkeypatch.setattr(us, "_last_ctx", {
        "full_by": {"A": {"coupons": [1]}, "B": {"coupons": [1]}, "C": {"coupons": [1]}},
        "fixed_by": {"F": {"faceunit": "USD"}, "P": {"perpetual": True}}})
    monkeypatch.setattr(us, "_fixed_sides_dirty", {"A": 0.0})


def test_blank_fixed_reasons(monkeypatch):
    _setup(monkeypatch)
    r = us._blank_fixed_count()
    # A.ask — в очереди; B.bid — иное (YTM есть, расписание есть, не в очереди);
    # C.bid — нет YTM; D.bid — нет расписания; E — стороны нет, не считаем
    assert r == {"total": 4, "queued": 1, "no_sched": 1, "no_ytm": 1, "other": 1, "by_design": 3}


def test_latency_summary(monkeypatch):
    monkeypatch.setattr(us, "_fx_lat", [0.1, 0.2, 0.3, 4.0])
    r = us._fx_lat_summary()
    assert r["n"] == 4 and r["max"] == 4.0 and r["p50"] == 0.3
    monkeypatch.setattr(us, "_fx_lat", [])
    assert us._fx_lat_summary() == {"n": 0}


def test_queue_records_first_enqueue_time(monkeypatch):
    monkeypatch.setattr(us, "_fixed_isins", {"X"})
    monkeypatch.setattr(us, "_fixed_sides_dirty", {})
    monkeypatch.setattr(us, "_fixed_t0", {})
    us._queue_sides("X")
    t = us._fixed_t0["X"]
    us._queue_sides("X")                       # повтор не сдвигает момент постановки
    assert us._fixed_t0["X"] == t


def test_heal_queues_blank_sides_with_retry_pause(monkeypatch):
    monkeypatch.setitem(market_cache, "fixed_metrics", {
        "A": {"bid": 99.0, "y_idx_bid_bps": None},      # цена есть, числа нет → лечим
        "B": {"bid": 99.0, "y_idx_bid_bps": -5},        # посчитано
        "C": {"bid": 99.0, "y_idx_bid_bps": None},      # не в пуле стрима
    })
    monkeypatch.setattr(us, "_fixed_isins", {"A", "B"})
    monkeypatch.setattr(us, "_fixed_sides_dirty", {})
    monkeypatch.setattr(us, "_fixed_t0", {})
    monkeypatch.setattr(us, "_fx_healed_at", {})
    monkeypatch.setattr(us, "_fx_heal_last", 0.0)
    assert us.heal_fixed_blank_sides() == 1 and "A" in us._fixed_sides_dirty
    us._fixed_sides_dirty.clear()
    monkeypatch.setattr(us, "_fx_heal_last", 0.0)       # заход разрешён, пауза бумаги — нет
    assert us.heal_fixed_blank_sides() == 0
