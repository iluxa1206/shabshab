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


def test_read_bars_hides_legacy_g_close_for_fixed(tmp_path, monkeypatch):
    """y_close_bps фиксов до 16.10 — g-спред под именем Y-IDX: наружу не отдаём."""
    import importlib
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "p.db"))
    from services import portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    from services import bars
    importlib.reload(bars)
    with pdb._connect() as c:
        c.executemany(
            "INSERT INTO bar_hourly(isin,ts,kind,y_idx_bps,y_close_bps) VALUES(?,?,?,?,?)",
            [("F", "2026-10-14 12:00", "fixed", 90.0, 120.0),
             ("F", "2026-10-16 12:00", "fixed", 90.0, 95.0),
             ("L", "2026-10-14 12:00", "floater", 50.0, 55.0)])
    f = {r["ts"]: r["y_close_bps"] for r in bars.read_bars("F")}
    assert f["2026-10-14 12:00"] is None and f["2026-10-16 12:00"] == 95.0
    assert bars.read_bars("L")[0]["y_close_bps"] == 55.0        # флоатер не тронут
