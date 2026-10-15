"""Динамика/разброс Y-IDX с фиксами: фиксы идут из spread_daily, флоатеры — из bar_daily."""
import asyncio
import importlib

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "p.db"))
    from services import portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    from services import bars
    importlib.reload(bars)
    from api.routes import history
    importlib.reload(history)
    return pdb, bars, history


def _put(pdb, bars, day):
    ver = bars.BARS_METRICS_VERSION
    with pdb._connect() as c:
        for i in range(4):                                   # 4 флоатера, у каждого свой спред
            c.execute("INSERT INTO bar_daily(isin,date,kind,y_idx_wap_bps,metrics_ver) "
                      "VALUES(?,?,?,?,?)", (f"FL{i}", day, "floater", 100.0 + i * 10, ver))
        for i in range(4):                                   # 4 фикса в spread_daily
            c.execute("INSERT INTO spread_daily(isin,date,kind,y_idx,src) VALUES(?,?,?,?,?)",
                      (f"FX{i}", day, "fixed", 300.0 + i * 10, "snap"))
        c.execute("INSERT INTO spread_daily(isin,date,kind,y_idx,src) VALUES(?,?,?,?,?)",
                  ("FXD", day, "fixed", 9999.0, "snap_degraded"))      # деградированный — вон


def _monday():
    from datetime import date, timedelta
    d = date.today()
    while d.weekday() != 2:                                  # среда этой/прошлой недели
        d -= timedelta(days=1)
    return d.isoformat()


def test_dispersion_includes_fixed_and_skips_degraded(db):
    pdb, bars, h = db
    day = _monday()
    _put(pdb, bars, day)
    r = asyncio.run(h.yidx_dispersion(h.YidxDispersionBody(days=30)))
    pt = next(p for p in r["points"] if p["date"] == day)
    assert pt["n"] == 8                                      # 4 флоатера + 4 фикса, без degraded
    r0 = asyncio.run(h.yidx_dispersion(h.YidxDispersionBody(days=30, with_fixed=False)))
    assert next(p for p in r0["points"] if p["date"] == day)["n"] == 4


def test_fixed_rows_helper_columns_and_filter(db):
    pdb, bars, h = db
    _put(pdb, bars, _monday())
    rows = h._fixed_daily_rows("2000-01-01")
    assert {r["isin"] for r in rows} == {"FX0", "FX1", "FX2", "FX3"}
    assert set(rows[0]) == {"isin", "date", "y_idx", "y_idx_alt", "horizon", "alt_horizon"}
    merged = h._merge_daily([{"isin": "A", "date": "2026-10-02", "y_idx": 1}],
                            [{"isin": "B", "date": "2026-10-01", "y_idx": 2}])
    assert [r["isin"] for r in merged] == ["B", "A"]         # по возрастанию даты
