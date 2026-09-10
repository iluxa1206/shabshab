"""Возврат в очередь метрик, посчитанных на суррогатных данных.

10.09.2026 биржевой НКД пропал на полдня, и спреды по графику купонов легли в
архив как обычные числа. Чинить это надо по ОКНУ РАСЧЁТА, а не по времени
сделки: испорчены строки, которым спред считали во время аварии.
"""
import sqlite3

import pytest

from services import bars, block_trades, spread_history
from services.portfolio_db import _connect


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"

    def _conn():
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    for mod in (spread_history, bars, block_trades):
        monkeypatch.setattr(mod, "_connect", _conn, raising=False)
    monkeypatch.setattr("services.portfolio_db._connect", _conn, raising=False)

    with _conn() as c:
        c.executescript("""
            CREATE TABLE spread_daily(isin TEXT, date TEXT, kind TEXT, price_pct REAL,
              dm_bps REAL, g_spread_bps REAL, z_bps REAL, ytm REAL, y_idx REAL,
              src TEXT, engine_ver INT, horizon TEXT, y_idx_alt REAL, alt_horizon TEXT,
              PRIMARY KEY(isin,date));
            CREATE TABLE bar_hourly(isin TEXT, ts TEXT, y_idx_bps REAL,
              g_spread_bps REAL, metrics_ver INT);
            CREATE TABLE bar_daily(isin TEXT, date TEXT);
            CREATE TABLE trade_tick(isin TEXT, trade_id TEXT, ts TEXT,
              y_idx_bps REAL, dm_bps REAL, metrics_at TEXT);
            CREATE TABLE block_trade(trade_id TEXT, isin TEXT, ts TEXT,
              y_idx_bps REAL, dm_bps REAL, metrics_at TEXT);
        """)
    return _conn


def test_only_degraded_snapshots_are_dropped(db):
    with db() as c:
        c.executemany("INSERT INTO spread_daily(isin,date,src) VALUES(?,?,?)", [
            ("A", "2026-09-10", "snap_degraded"),
            ("B", "2026-09-10", "snap"),
            ("C", "2026-09-10", "honest"),
            ("D", "2026-09-09", "snap_degraded"),
        ])
    assert spread_history.drop_degraded(["2026-09-10"]) == 1
    with db() as c:
        left = {r[0] for r in c.execute("SELECT isin FROM spread_daily")}
    assert left == {"B", "C", "D"}, "снят только суррогат за указанный день"


def test_requeue_keys_on_calc_time_not_trade_time(db):
    """Вчерашняя сделка, оценённая в аварию, испорчена; сегодняшняя, оценённая
    после починки, — нет. Ключ — metrics_at."""
    with db() as c:
        c.executemany(
            "INSERT INTO trade_tick(isin,trade_id,ts,y_idx_bps,dm_bps,metrics_at) "
            "VALUES(?,?,?,?,?,?)", [
                ("A", "1", "2026-09-09 15:00:00", 228.0, 239.0, "2026-09-10 10:30:00"),
                ("A", "2", "2026-09-10 11:00:00", 112.0, 118.0, "2026-09-10 23:10:00"),
            ])
    out = block_trades.requeue_metrics_window("2026-09-10 00:00:00", "2026-09-10 19:00:00")
    assert out["trade_tick"] == 1
    with db() as c:
        rows = {r["trade_id"]: r["y_idx_bps"] for r in c.execute("SELECT * FROM trade_tick")}
    assert rows["1"] is None and rows["2"] == 112.0


def test_bars_window_reset_keeps_prices(db):
    with db() as c:
        c.executemany(
            "INSERT INTO bar_hourly(isin,ts,y_idx_bps,metrics_ver) VALUES(?,?,?,?)", [
                ("A", "2026-09-10 11:00:00", 228.0, 10),
                ("A", "2026-09-09 11:00:00", 112.0, 10),
            ])
        c.execute("INSERT INTO bar_daily(isin,date) VALUES('A','2026-09-10')")
    out = bars.reset_metrics_window("2026-09-10 00:00:00", "2026-09-10 23:59:59")
    assert out["hours"] == 1 and out["days"] == 1
    with db() as c:
        vers = {r["ts"]: r["metrics_ver"] for r in c.execute("SELECT * FROM bar_hourly")}
    assert vers["2026-09-10 11:00:00"] == 0
    assert vers["2026-09-09 11:00:00"] == 10, "чужой день трогать нельзя"
