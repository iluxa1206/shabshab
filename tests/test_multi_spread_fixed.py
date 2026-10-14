"""/api/history/multi/spread: фиксы в Сравнении (этап C единого монитора).

У фиксов честный Y-IDX лежит только в spread_daily; дневные бары хранят у них
G-спред под именами y_idx_* — отдавать его как Y-IDX нельзя, поэтому на барных
базах фиксы называются в `unsupported`, а не выдаются молча."""
import asyncio
import importlib
from datetime import date, timedelta

import pytest

FIXED = "RU000A0FIX01"
FLOAT = "RU000A0FLT01"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "t.db"))
    import services.portfolio_db as pdb
    importlib.reload(pdb)
    pdb.init_db()
    import services.market_data as md
    monkeypatch.setitem(md.market_cache, "fixed_universe", [{"isin": FIXED}])
    yield pdb
    monkeypatch.delenv("PORTFOLIO_DB", raising=False)
    importlib.reload(pdb)


def _day(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _fill(pdb):
    with pdb._connect() as c:
        for n, (yf, yx) in enumerate([(120.0, 90.0), (125.0, 95.0), (None, 99.0)]):
            c.execute("INSERT INTO spread_daily(isin,date,kind,price_pct,y_idx,src) "
                      "VALUES(?,?,?,?,?,?)", (FLOAT, _day(3 - n), "floater", 99.0 + n, yf, "snap"))
            c.execute("INSERT INTO spread_daily(isin,date,kind,price_pct,y_idx,src) "
                      "VALUES(?,?,?,?,?,?)", (FIXED, _day(3 - n), "fixed", 80.0 + n, yx, "backfill"))


def _call(base, isins):
    from api.routes.history import MultiSpreadBody, multi_spread
    return asyncio.run(multi_spread(MultiSpreadBody(days=30, isins=isins, base=base)))


def test_close_base_returns_both_classes(env):
    _fill(env)
    r = _call("close", [FLOAT, FIXED])
    by = {s["isin"]: s["points"] for s in r["series"]}
    assert [p["y_idx"] for p in by[FIXED]] == [90.0, 95.0, 99.0]
    assert [p["price"] for p in by[FIXED]] == [80.0, 81.0, 82.0]
    assert [p["y_idx"] for p in by[FLOAT]] == [120.0, 125.0, None]
    assert r["kinds"] == {FLOAT: "floater", FIXED: "fixed"}
    assert r["unsupported"] == []


def test_bar_bases_do_not_mix_g_spread_into_yidx(env):
    _fill(env)
    for base in ("vwap", "bar_close"):
        r = _call(base, [FLOAT, FIXED])
        assert r["unsupported"] == [FIXED]
        assert FIXED not in {s["isin"] for s in r["series"]}


def test_only_fixed_on_bar_base_is_empty_but_explained(env):
    _fill(env)
    r = _call("vwap", [FIXED])
    assert r["series"] == [] and r["unsupported"] == [FIXED] and r["dates"] == []


def test_yidx_outside_band_is_dropped_not_point(env):
    with env._connect() as c:
        c.execute("INSERT INTO spread_daily(isin,date,kind,price_pct,y_idx,src) "
                  "VALUES(?,?,?,?,?,?)", (FIXED, _day(1), "fixed", 70.0, 99999.0, "backfill"))
    r = _call("close", [FIXED])
    p = r["series"][0]["points"][0]
    assert p["y_idx"] is None and p["price"] == 70.0       # цена жива, мусор спреда погашен
